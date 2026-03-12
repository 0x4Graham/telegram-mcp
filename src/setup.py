"""First-run setup wizard for Telegram Digest."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import fnmatch

import yaml
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# Add parent to path for imports when run as module
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_data_dir, get_session_path, get_db_path, get_chroma_path
from src.store import Store
from src.vectors import VectorStore


def print_header(text: str) -> None:
    """Print a section header."""
    print(f"\n{'=' * 50}")
    print(f"  {text}")
    print('=' * 50)


def prompt(message: str, default: Optional[str] = None) -> str:
    """Prompt for input with optional default."""
    if default:
        result = input(f"{message} [{default}]: ").strip()
        return result if result else default
    return input(f"{message}: ").strip()


def prompt_int(message: str, default: Optional[int] = None) -> int:
    """Prompt for integer input."""
    while True:
        try:
            result = prompt(message, str(default) if default else None)
            return int(result)
        except ValueError:
            print("Please enter a valid number.")


async def setup_env() -> dict:
    """Set up .env file with API keys."""
    print_header("Environment Setup")

    base_dir = Path(__file__).parent.parent
    env_path = base_dir / ".env"

    if env_path.exists():
        print(f"Found existing .env file at {env_path}")
        load_dotenv(env_path)

    # Check for existing values (from .env file or environment variables)
    env_vars = {
        "TELEGRAM_API_ID": os.getenv("TELEGRAM_API_ID", ""),
        "TELEGRAM_API_HASH": os.getenv("TELEGRAM_API_HASH", ""),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
        "VOYAGE_API_KEY": os.getenv("VOYAGE_API_KEY", ""),
    }

    required_keys = ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY"]
    if all(env_vars.get(k) for k in required_keys):
        use_existing = prompt("Use existing credentials? (y/n)", "y")
        if use_existing.lower() == "y":
            return env_vars

    print("\nYou'll need the following API keys:")
    print("1. Telegram API ID and Hash from https://my.telegram.org")
    print("2. Telegram Bot Token from @BotFather")
    print("3. Anthropic API Key from https://console.anthropic.com")
    print("4. Voyage AI API Key from https://dash.voyageai.com")

    env_vars["TELEGRAM_API_ID"] = prompt(
        "\nTelegram API ID", env_vars.get("TELEGRAM_API_ID")
    )
    env_vars["TELEGRAM_API_HASH"] = prompt(
        "Telegram API Hash", env_vars.get("TELEGRAM_API_HASH")
    )
    env_vars["TELEGRAM_BOT_TOKEN"] = prompt(
        "Telegram Bot Token", env_vars.get("TELEGRAM_BOT_TOKEN")
    )
    env_vars["ANTHROPIC_API_KEY"] = prompt(
        "Anthropic API Key", env_vars.get("ANTHROPIC_API_KEY")
    )
    env_vars["VOYAGE_API_KEY"] = prompt(
        "Voyage AI API Key", env_vars.get("VOYAGE_API_KEY")
    )

    # Write .env file with restricted permissions
    fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for key, value in env_vars.items():
            f.write(f"{key}={value}\n")

    print(f"\nSaved credentials to {env_path} (mode 600)")
    return env_vars


async def setup_telegram(env_vars: dict) -> tuple[TelegramClient, int, str]:
    """Authenticate with Telegram and get user ID and username."""
    print_header("Telegram Authentication")

    session_path = get_session_path()
    client = TelegramClient(
        str(session_path),
        int(env_vars["TELEGRAM_API_ID"]),
        env_vars["TELEGRAM_API_HASH"],
    )

    await client.connect()

    if not await client.is_user_authorized():
        print("\nPlease authenticate with your Telegram account.")
        phone = prompt("Phone number (with country code)")

        await client.send_code_request(phone)
        code = prompt("Enter the code you received")

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = prompt("2FA password required")
            await client.sign_in(password=password)

    me = await client.get_me()
    print(f"\nLogged in as: {me.first_name} (@{me.username})")
    print(f"User ID: {me.id}")

    return client, me.id, me.username or ""


async def verify_bot(env_vars: dict, user_id: int) -> None:
    """Verify the bot token and send a test message."""
    print_header("Bot Verification")

    from telegram import Bot

    bot = Bot(token=env_vars["TELEGRAM_BOT_TOKEN"])
    bot_info = await bot.get_me()
    print(f"Bot: @{bot_info.username}")

    send_test = prompt("Send test message to yourself? (y/n)", "y")
    if send_test.lower() == "y":
        await bot.send_message(
            chat_id=user_id,
            text="Telegram Digest setup successful! This bot will deliver your daily digests and suggestions.",
        )
        print("Test message sent!")


async def select_chats(client: TelegramClient) -> list[int]:
    """Filter chats using ignore_patterns from config.yaml."""
    print_header("Chat Selection for Backfill")

    # Load ignore patterns from config — prefer the mounted/root config
    # (which has user's patterns) over data dir (generated by setup)
    base_dir = Path(__file__).parent.parent
    root_config = base_dir / "config.yaml"
    data_config = base_dir / "data" / "config.yaml"

    ignore_patterns = []
    for config_path in [root_config, data_config]:
        if config_path.exists():
            with open(config_path) as f:
                raw = yaml.safe_load(f)
            patterns = raw.get("chats", {}).get("ignore_patterns", [])
            if patterns:
                ignore_patterns = patterns
                print(f"\nLoaded {len(patterns)} ignore patterns from {config_path}")
                break
    if not ignore_patterns:
        print("\nNo ignore patterns found in config.")

    print("\nFetching your chats...")

    activity_cutoff = datetime.now(timezone.utc) - timedelta(days=60)

    chats = []
    inactive = 0
    async for dialog in client.iter_dialogs():
        if dialog.is_user or dialog.is_group or dialog.is_channel:
            # Skip chats with no activity in the last 60 days
            last_date = dialog.date
            if last_date and last_date.tzinfo is None:
                last_date = last_date.replace(tzinfo=timezone.utc)
            if last_date and last_date < activity_cutoff:
                inactive += 1
                continue

            chats.append(
                {
                    "id": dialog.id,
                    "name": dialog.name,
                    "type": "DM" if dialog.is_user else ("Group" if dialog.is_group else "Channel"),
                }
            )

    # Apply ignore patterns
    def _is_ignored(name: str) -> bool:
        for pattern in ignore_patterns:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                return True
        return False

    included = [c for c in chats if not _is_ignored(c["name"])]
    excluded = [c for c in chats if _is_ignored(c["name"])]

    print(f"\nFound {len(chats) + inactive} chats total.")
    if inactive:
        print(f"Skipped {inactive} chats with no activity in the last 60 days.")
    if ignore_patterns:
        print(f"\nIgnore patterns from config.yaml:")
        for p in ignore_patterns:
            print(f"  - {p}")

    if excluded:
        print(f"\nExcluded by patterns ({len(excluded)} chats):")
        for c in excluded:
            print(f"  [{c['type']:7}] {c['name']}")

    print(f"\nIncluding {len(included)} chats for backfill.")
    confirm = prompt("Proceed? (y/n)", "y")
    if confirm.lower() != "y":
        print("Aborted.")
        return []

    return [c["id"] for c in included]


async def generate_config(env_vars: dict, user_id: int, username: str = "") -> None:
    """Generate config.yaml with defaults."""
    print_header("Configuration")

    base_dir = Path(__file__).parent.parent
    data_dir = get_data_dir()
    # Write to data dir (writable volume in Docker), fall back to project root
    config_path = data_dir / "config.yaml" if not os.access(base_dir / "config.yaml", os.W_OK) else base_dir / "config.yaml"

    # Load existing ignore_patterns to preserve them
    existing_ignore_patterns = []
    for p in [base_dir / "config.yaml", data_dir / "config.yaml"]:
        if p.exists():
            with open(p) as f:
                existing = yaml.safe_load(f)
            existing_ignore_patterns = existing.get("chats", {}).get("ignore_patterns", [])
            if existing_ignore_patterns:
                break

    phone = prompt("Your phone number (for reference)", "+1...")
    tz = prompt("Timezone", "Europe/Zurich")
    digest_time = prompt("Daily digest time (HH:MM)", "07:00")

    config = {
        "telegram": {
            "api_id": "${TELEGRAM_API_ID}",
            "api_hash": "${TELEGRAM_API_HASH}",
            "phone": phone,
            "bot_token": "${TELEGRAM_BOT_TOKEN}",
            "delivery_chat_id": user_id,
            "username": username,
        },
        "llm": {
            "model": "claude-sonnet-4-20250514",
        },
        "embeddings": {
            "model": "voyage-3-lite",
        },
        "digest": {
            "schedule": digest_time,
            "timezone": tz,
            "lookback_hours": 24,
            "target_length": 2000,
        },
        "quiet_hours": {
            "enabled": True,
            "start": "22:00",
            "end": "08:00",
        },
        "answer_suggester": {
            "enabled": True,
            "similarity_threshold": 0.85,
            "cooldown_minutes": 30,
            "suppress_while_typing": True,
            "show_top_matches": 3,
        },
        "question_detection": {
            "batch_size": 50,
            "max_wait_minutes": 10,
        },
        "data_retention": {
            "messages_days": 90,
            "cleanup_schedule": "03:00",
        },
        "chats": {
            "default_priority": 3,
            "priorities": [],
            "ignore_patterns": existing_ignore_patterns,
        },
        "dashboard": {
            "enabled": True,
            "port": 8000,
        },
    }

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\nSaved configuration to {config_path}")


async def run_backfill(
    client: TelegramClient,
    chat_ids: list[int],
    days: int = 30,
) -> int:
    """Backfill messages from selected chats."""
    print_header("Backfilling Messages")

    if not chat_ids:
        print("No chats selected for backfill.")
        return 0

    # Initialize store
    store = Store()
    await store.connect()

    since = datetime.now(timezone.utc) - timedelta(days=days)
    total_count = 0
    me = await client.get_me()

    print(f"\nFetching {days} days of messages from {len(chat_ids)} chats...")

    for i, chat_id in enumerate(chat_ids, 1):
        try:
            entity = await client.get_entity(chat_id)
            chat_name = getattr(entity, "title", None) or getattr(entity, "first_name", f"Chat {chat_id}")

            print(f"[{i}/{len(chat_ids)}] {chat_name}...", end=" ", flush=True)

            count = 0
            async for message in client.iter_messages(entity, offset_date=since, reverse=True):
                if not message.text:
                    continue
                if not message.text:
                    continue

                sender = await message.get_sender()
                sender_id = sender.id if sender else 0
                sender_name = getattr(sender, "first_name", "Unknown") if sender else "Unknown"

                chat_type = "dm" if hasattr(entity, "first_name") else "group"

                await store.store_message(
                    telegram_id=message.id,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    chat_type=chat_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    text=message.text,
                    timestamp=message.date,
                    reply_to_id=message.reply_to_msg_id if message.reply_to else None,
                    is_from_me=sender_id == me.id,
                )
                count += 1

            print(f"{count} messages")
            total_count += count

        except Exception as e:
            print(f"Error: {e}")

    await store.close()
    print(f"\nBackfilled {total_count} messages total.")
    return total_count


async def rebuild_qa_index() -> None:
    """Rebuild the Q&A index from stored messages."""
    print_header("Rebuilding Q&A Index")

    print("This feature extracts Q&A pairs from your message history.")
    print("It requires the classifier to identify questions first.")
    print("\nRun the main service to process messages and build the Q&A index.")


async def main(rebuild_qa: bool = False) -> None:
    """Run the setup wizard."""
    print("\n" + "=" * 50)
    print("  TELEGRAM DIGEST SETUP")
    print("=" * 50)

    if rebuild_qa:
        await rebuild_qa_index()
        return

    # Create data directory
    get_data_dir()

    # Step 1: Environment setup
    env_vars = await setup_env()

    # Step 2: Telegram authentication
    client, user_id, username = await setup_telegram(env_vars)

    try:
        # Step 3: Verify bot
        await verify_bot(env_vars, user_id)

        # Step 4: Select chats for backfill
        selected_chats = await select_chats(client)

        # Step 5: Generate config
        await generate_config(env_vars, user_id, username)

        # Step 6: Run backfill
        if selected_chats:
            await run_backfill(client, selected_chats)

        print_header("Setup Complete!")
        print("\nYou can now run the service with:")
        print("  python -m src.main")
        print("\nOr force a digest now with:")
        print("  python -m src.main --digest-now")

    finally:
        await client.disconnect()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Telegram Digest Setup")
    parser.add_argument(
        "--rebuild-qa",
        action="store_true",
        help="Rebuild the Q&A index from stored messages",
    )

    args = parser.parse_args()
    asyncio.run(main(rebuild_qa=args.rebuild_qa))
