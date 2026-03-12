"""Main entry point for Telegram Digest."""

import asyncio
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

# Configure structlog
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger()

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import load_config, get_config, get_lock_path
from src.store import get_store, close_store
from src.vectors import get_vector_store
from src.classifier import Classifier
from src.suggester import Suggester
from src.delivery import get_delivery_bot
from src.ingester import Ingester
from src.scheduler import get_scheduler
from src.dashboard import run_dashboard


# Global state
_shutdown_event: Optional[asyncio.Event] = None
_start_time: Optional[datetime] = None


def acquire_lock() -> None:
    """Acquire the instance lock file."""
    lock_path = get_lock_path()

    if lock_path.exists():
        try:
            with open(lock_path) as f:
                pid = int(f.read().strip())

            # Check if process is running
            try:
                os.kill(pid, 0)
                raise RuntimeError(
                    f"Another instance is already running (PID {pid}). "
                    f"Delete {lock_path} if this is incorrect."
                )
            except OSError:
                # Process not running, stale lock
                log.warning("stale_lock_removed", pid=pid)

        except ValueError:
            pass  # Invalid lock file content

    # Write our PID
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))

    log.info("lock_acquired", pid=os.getpid())


def release_lock() -> None:
    """Release the instance lock file."""
    lock_path = get_lock_path()
    if lock_path.exists():
        lock_path.unlink()
        log.info("lock_released")


async def handle_shutdown(sig: signal.Signals) -> None:
    """Handle shutdown signals."""
    log.info("shutdown_signal_received", signal=sig.name)
    if _shutdown_event:
        _shutdown_event.set()


async def generate_and_send_digest(store, bot) -> None:
    """Generate a simple message summary and send it (no LLM calls)."""
    from datetime import timedelta
    config = get_config()
    period_end = datetime.now()
    period_start = period_end - timedelta(hours=config.digest.lookback_hours)

    messages_by_chat = await store.get_messages_for_digest(period_start, period_end)
    if not messages_by_chat:
        await bot.send_digest("Quiet day - no significant activity to report.")
        return

    import fnmatch
    ignore_patterns = config.chats.ignore_patterns
    lines = [f"**Messages from last {config.digest.lookback_hours}h:**\n"]
    total = 0
    for chat_id, msgs in messages_by_chat.items():
        if not msgs:
            continue
        chat_name = msgs[0]["chat_name"]
        ignored = any(fnmatch.fnmatch(chat_name.lower(), p.lower()) for p in ignore_patterns)
        if ignored:
            continue
        count = len([m for m in msgs if m.get("text")])
        if count:
            lines.append(f"- **{chat_name}**: {count} messages")
            total += count

    lines.append(f"\n_Total: {total} messages across {len(lines) - 1} chats_")
    lines.append("_Use the MCP server to get a full AI-powered digest._")
    await bot.send_digest("\n".join(lines))


async def get_stats_text(store, vector_store, scheduler) -> str:
    """Generate stats text for /stats command."""
    stats = await store.get_stats()

    uptime = datetime.now() - _start_time if _start_time else "Unknown"
    if isinstance(uptime, datetime):
        uptime = str(uptime).split(".")[0]

    text = f"""📊 System Statistics

Messages:
  Total: {stats['total_messages']}
  Today: {stats['messages_today']}

Knowledge Base:
  Chats tracked: {stats['total_chats']}
  Q&A pairs: {stats['total_qa_pairs']} (DB) / {vector_store.count()} (vectors)
  Suggestions sent: {stats['total_suggestions']}
  Digests generated: {stats['total_digests']}

System:
  Uptime: {uptime}
  Status: {'Paused' if scheduler.is_paused() else 'Running'}
  Quiet hours: {'Active' if scheduler.is_quiet_hours() else 'Inactive'}
"""
    return text


async def search_qa(vector_store, query: str) -> str:
    """Search the Q&A knowledge base."""
    matches = vector_store.query_similar(query, threshold=0.0, limit=3)

    if not matches:
        return "No matches found."

    text = f"Search: {query[:50]}\n\n"

    for i, match in enumerate(matches, 1):
        text += f"{i}. Similarity: {match['similarity']:.0%}\n"
        text += f"Q: {match['question'][:100]}...\n"
        text += f"A: {match['answer'][:200]}...\n"
        text += f"From: {match['chat_name']}\n\n"

    return text


async def get_recent_summary(store, chat_query: str) -> str:
    """Get recent messages for a chat (no LLM)."""
    from datetime import timedelta

    chats = await store.get_all_chats()
    matching = [
        c for c in chats
        if chat_query.lower() in c["name"].lower()
    ]

    if not matching:
        return f"No chat found matching '{chat_query}'"

    if len(matching) > 1:
        names = ", ".join(c["name"] for c in matching[:5])
        return f"Multiple matches: {names}. Please be more specific."

    chat = matching[0]
    since = datetime.now() - timedelta(hours=24)
    messages = await store.get_messages_since(since, chat_id=chat["chat_id"])
    messages = [m for m in messages if m.get("text")]

    if not messages:
        return f"No messages in {chat['name']} in the last 24 hours."

    lines = [f"{chat['name']} - Last 24h ({len(messages)} messages)\n"]
    for m in messages[-30:]:  # Last 30 messages
        lines.append(f"{m['sender_name']}: {m['text'][:200]}")

    if len(messages) > 30:
        lines.insert(1, f"_Showing last 30 of {len(messages)} messages_\n")

    return "\n".join(lines)


async def get_pending_mentions(store, config, with_suggestions: bool = False) -> str:
    """Get mentions awaiting response (no LLM calls)."""
    from datetime import timedelta

    username = config.telegram.username
    since = datetime.now() - timedelta(hours=48)
    ignore_patterns = config.chats.ignore_patterns

    pending = await store.get_pending_mentions(username, since, ignore_patterns)

    if not pending:
        return "No pending mentions in the last 48 hours."

    text = f"Pending Responses ({len(pending)})\n\n"

    for m in pending[:10]:
        msg_time = datetime.fromisoformat(m["timestamp"])
        if msg_time.tzinfo:
            msg_time = msg_time.replace(tzinfo=None)
        hours_ago = (datetime.now() - msg_time).total_seconds() / 3600

        msg_text = m["text"][:200] + "..." if len(m["text"]) > 200 else m["text"]

        text += f"{m['chat_name']}\n"
        text += f"From: {m['sender_name']} ({hours_ago:.1f}h ago)\n"
        text += f"{msg_text}\n\n"

    if len(pending) > 10:
        text += f"...and {len(pending) - 10} more"

    return text


async def run(digest_now: bool = False) -> None:
    """Run the main application."""
    global _shutdown_event, _start_time

    _shutdown_event = asyncio.Event()
    _start_time = datetime.now()

    # Load configuration
    try:
        load_config()
    except FileNotFoundError:
        log.error("config_not_found", hint="Run 'python -m src.setup' first")
        return

    config = get_config()
    log.info("config_loaded")

    # Acquire lock
    try:
        acquire_lock()
    except RuntimeError as e:
        log.error("lock_failed", error=str(e))
        return

    try:
        # Initialize components
        log.info("initializing_components")

        store = await get_store()
        vector_store = get_vector_store()

        classifier = Classifier(store)
        bot = await get_delivery_bot()

        # Create suggester with bot callback
        async def on_suggestion(suggestion: dict):
            await bot.send_suggestion(suggestion)

        suggester = Suggester(store, vector_store, on_suggestion=on_suggestion)

        ingester = Ingester(store, classifier, suggester)
        scheduler = get_scheduler()

        # Set up scheduler callbacks
        async def on_digest_time():
            await generate_and_send_digest(store, bot)

        async def on_cleanup_time():
            deleted = await store.cleanup_old_messages(config.data_retention.messages_days)
            await store.cleanup_expired_cooldowns()
            log.info("cleanup_completed", messages_deleted=deleted)

        scheduler.on_digest_time = on_digest_time
        scheduler.on_cleanup_time = on_cleanup_time

        # Set up bot command callbacks
        bot.on_digest_request = lambda: generate_and_send_digest(store, bot)
        bot.on_pause_request = lambda: (ingester.pause(), scheduler.pause())
        bot.on_resume_request = lambda: (ingester.resume(), scheduler.resume())
        bot.on_stats_request = lambda: get_stats_text(store, vector_store, scheduler)
        bot.on_search_request = lambda q: search_qa(vector_store, q)
        bot.on_recent_request = lambda q: get_recent_summary(store, q)
        bot.on_pending_request = lambda: get_pending_mentions(store, config, with_suggestions=False)
        bot.on_suggest_request = lambda: get_pending_mentions(store, config, with_suggestions=False)

        # Connect to Telegram
        await ingester.connect()

        # Start bot
        await bot.start()

        # If digest-now mode, generate and exit
        if digest_now:
            log.info("digest_now_mode")
            await generate_and_send_digest(store, bot)
            return

        # Start scheduler
        scheduler.setup()
        scheduler.start()

        # Start classifier loop
        classifier.start()

        # Set up signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(handle_shutdown(s))
            )

        # Start services (dashboard, ingester) before catch-up so dashboard is available immediately
        tasks = [
            asyncio.create_task(ingester.run()),
        ]

        if config.dashboard.enabled:
            tasks.append(
                asyncio.create_task(
                    run_dashboard(store, vector_store, scheduler, _start_time)
                )
            )

        log.info(
            "service_started",
            dashboard_port=config.dashboard.port if config.dashboard.enabled else None,
        )

        # Catch up on missed messages (runs while dashboard is already serving)
        await ingester.catch_up(hours=24)

        # Wait for shutdown
        await _shutdown_event.wait()

        log.info("shutting_down")

        # Stop services
        ingester.stop()
        classifier.stop()
        scheduler.stop()

        # Cancel tasks
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Disconnect
        await ingester.disconnect()
        await bot.stop()
        await close_store()

        log.info("shutdown_complete")

    finally:
        release_lock()


def main():
    """Entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Telegram Digest Service")
    parser.add_argument(
        "--digest-now",
        action="store_true",
        help="Generate and send a digest immediately, then exit",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run(digest_now=args.digest_now))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
