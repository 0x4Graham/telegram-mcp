"""Quick script to authenticate with Telegram and get user ID."""
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

async def main():
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    session_path = data_dir / "telegram"

    client = TelegramClient(
        str(session_path),
        int(os.getenv("TELEGRAM_API_ID")),
        os.getenv("TELEGRAM_API_HASH"),
    )

    await client.start()
    me = await client.get_me()
    print(f"\nYour Telegram User ID: {me.id}")
    print(f"Logged in as: {me.first_name} (@{me.username})")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
