"""Fetch today's messages from the database."""
import asyncio
import argparse
import fnmatch
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from src.store import Store
from src.config import get_db_path, load_config, get_config


def should_ignore(chat_name: str, patterns: list[str]) -> bool:
    """Check if chat matches any ignore pattern."""
    for pattern in patterns:
        if fnmatch.fnmatch(chat_name.lower(), pattern.lower()):
            return True
    return False


async def fetch_messages(hours: int = 24, chat_name: str = None, raw: bool = False):
    """Fetch messages from the last N hours."""
    load_config()
    config = get_config()
    ignore_patterns = config.chats.ignore_patterns

    store = Store()
    await store.connect()

    since = datetime.now() - timedelta(hours=hours)
    messages = await store.get_messages_since(since)

    # Filter ignored chats
    messages = [m for m in messages if not should_ignore(m["chat_name"], ignore_patterns)]

    if chat_name:
        messages = [m for m in messages if chat_name.lower() in m["chat_name"].lower()]

    if not messages:
        print(f"No messages in the last {hours} hours.")
        await store.close()
        return

    if raw:
        # Print raw messages
        for m in messages:
            print(f"[{m['timestamp']}] {m['chat_name']} | {m['sender_name']}: {m['text'][:200]}")
    else:
        # Group by chat
        by_chat = {}
        for m in messages:
            if m["chat_name"] not in by_chat:
                by_chat[m["chat_name"]] = []
            by_chat[m["chat_name"]].append(m)

        print(f"\n=== Messages from last {hours} hours ===\n")
        print(f"Total: {len(messages)} messages across {len(by_chat)} chats\n")

        for chat, msgs in sorted(by_chat.items(), key=lambda x: -len(x[1])):
            print(f"📌 {chat} ({len(msgs)} messages)")
            for m in msgs[-5:]:  # Show last 5 per chat
                text = m["text"][:100] + "..." if len(m["text"] or "") > 100 else m["text"]
                print(f"   {m['sender_name']}: {text}")
            if len(msgs) > 5:
                print(f"   ... and {len(msgs) - 5} more")
            print()

    await store.close()


async def fetch_pending(hours: int = 48, username: str | None = None):
    """Fetch mentions awaiting response."""
    load_config()
    config = get_config()
    ignore_patterns = config.chats.ignore_patterns

    # Use config username if not provided via CLI
    if username is None:
        username = config.telegram.username

    store = Store()
    await store.connect()

    since = datetime.now() - timedelta(hours=hours)
    pending = await store.get_pending_mentions(username, since, ignore_patterns)

    if not pending:
        print(f"No pending mentions in the last {hours} hours.")
        await store.close()
        return

    print(f"\n🔔 PENDING RESPONSES ({len(pending)} items)")
    print("=" * 50)

    for m in pending:
        msg_time = datetime.fromisoformat(m["timestamp"])
        if msg_time.tzinfo:
            msg_time = msg_time.replace(tzinfo=None)
        time_ago = datetime.now() - msg_time
        hours_ago = time_ago.total_seconds() / 3600

        print(f"\n📍 {m['chat_name']}")
        print(f"   From: {m['sender_name']} ({hours_ago:.1f}h ago)")
        text = m["text"][:300] + "..." if len(m["text"]) > 300 else m["text"]
        print(f"   {text}")

    await store.close()


async def generate_digest_now():
    """Generate a digest for today."""
    from src.store import get_store
    from src.summarizer import Summarizer

    load_config()
    store = await get_store()
    summarizer = Summarizer(store)

    print("Generating digest...")
    digest = await summarizer.generate_digest()

    if digest:
        print("\n" + "=" * 50)
        print(digest)
        print("=" * 50)
    else:
        print("No digest generated (no messages or error).")


async def show_qa_pairs():
    """Show all Q&A pairs."""
    load_config()
    store = Store()
    await store.connect()

    pairs = await store.get_all_qa_pairs()

    if not pairs:
        print("No Q&A pairs yet. They're created when you reply to detected questions.")
        await store.close()
        return

    print(f"\n📚 Q&A Knowledge Base ({len(pairs)} pairs)\n")
    print("=" * 60)

    for p in pairs[:20]:  # Show last 20
        print(f"\n📍 {p['chat_name']}")
        print(f"Q: {p['question_text'][:150]}...")
        print(f"A: {p['answer_text'][:200]}...")
        print(f"   (suggested {p['times_suggested']}x)")

    if len(pairs) > 20:
        print(f"\n...and {len(pairs) - 20} more")

    await store.close()


def main():
    parser = argparse.ArgumentParser(description="Fetch today's messages")
    parser.add_argument("--hours", type=int, default=24, help="Hours to look back (default: 24)")
    parser.add_argument("--chat", type=str, help="Filter by chat name")
    parser.add_argument("--raw", action="store_true", help="Show raw message list")
    parser.add_argument("--digest", action="store_true", help="Generate a digest instead")
    parser.add_argument("--pending", action="store_true", help="Show mentions awaiting response")
    parser.add_argument("--qa", action="store_true", help="Show Q&A pairs")
    parser.add_argument("--username", type=str, default=None, help="Your Telegram username (default: from config)")

    args = parser.parse_args()

    if args.digest:
        asyncio.run(generate_digest_now())
    elif args.pending:
        asyncio.run(fetch_pending(hours=args.hours, username=args.username))
    elif args.qa:
        asyncio.run(show_qa_pairs())
    else:
        asyncio.run(fetch_messages(hours=args.hours, chat_name=args.chat, raw=args.raw))


if __name__ == "__main__":
    main()
