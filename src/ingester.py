"""Telethon message ingester for listening to Telegram messages."""

import asyncio
from datetime import datetime, timedelta
from typing import Optional, Set

import structlog
from telethon import TelegramClient, events
from telethon.tl.types import (
    Channel,
    Chat,
    Message,
    User,
    UpdateUserTyping,
    UpdateChatUserTyping,
)

from .config import get_config, get_session_path
from .store import Store
from .classifier import Classifier
from .suggester import Suggester
import fnmatch

log = structlog.get_logger()


def _should_ignore_chat(chat_name: str, patterns: list[str]) -> bool:
    """Check if a chat should be ignored based on patterns."""
    for pattern in patterns:
        if fnmatch.fnmatch(chat_name.lower(), pattern.lower()):
            return True
    return False


class Ingester:
    """Listens to Telegram messages and processes them."""

    def __init__(
        self,
        store: Store,
        classifier: Classifier,
        suggester: Suggester,
    ):
        self.store = store
        self.classifier = classifier
        self.suggester = suggester

        self._client: Optional[TelegramClient] = None
        self._running = False
        self._paused = False

        # Track typing status per chat
        self._typing_chats: Set[int] = set()
        self._typing_timeouts: dict[int, asyncio.Task] = {}

        # My user ID (set after connection)
        self._my_id: Optional[int] = None

    async def connect(self) -> TelegramClient:
        """Connect to Telegram using Telethon."""
        config = get_config()
        session_path = get_session_path()

        self._client = TelegramClient(
            str(session_path),
            config.telegram.api_id,
            config.telegram.api_hash,
        )

        await self._client.connect()

        if not await self._client.is_user_authorized():
            raise RuntimeError(
                "Telegram session not authorized. Run setup first."
            )

        me = await self._client.get_me()
        self._my_id = me.id
        log.info("telegram_connected", user_id=self._my_id, username=me.username)

        return self._client

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            raise RuntimeError("Ingester not connected. Call connect() first.")
        return self._client

    def _get_chat_type(self, chat) -> str:
        """Determine the chat type."""
        if isinstance(chat, User):
            return "dm"
        elif isinstance(chat, Channel):
            if chat.megagroup:
                return "supergroup"
            return "channel"
        elif isinstance(chat, Chat):
            return "group"
        return "unknown"

    def _get_chat_name(self, chat) -> str:
        """Get the display name for a chat."""
        if isinstance(chat, User):
            parts = [chat.first_name or ""]
            if chat.last_name:
                parts.append(chat.last_name)
            return " ".join(parts) or f"User {chat.id}"
        elif hasattr(chat, "title"):
            return chat.title or f"Chat {chat.id}"
        return f"Chat {chat.id}"

    async def _handle_message(self, event: events.NewMessage.Event) -> None:
        """Handle a new message event."""
        if self._paused:
            return

        message: Message = event.message

        # Skip media-only messages (text-only as per spec)
        if not message.text:
            return

        # Get chat info
        chat = await event.get_chat()
        chat_id = event.chat_id
        chat_name = self._get_chat_name(chat)
        chat_type = self._get_chat_type(chat)

        # Check if chat should be ignored
        config = get_config()
        if _should_ignore_chat(chat_name, config.chats.ignore_patterns):
            return

        # Get sender info
        sender = await event.get_sender()
        sender_id = sender.id if sender else 0
        sender_name = self._get_chat_name(sender) if sender else "Unknown"
        is_from_me = sender_id == self._my_id

        # Store message
        msg_id = await self.store.store_message(
            telegram_id=message.id,
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            sender_id=sender_id,
            sender_name=sender_name,
            text=message.text,
            timestamp=message.date,
            reply_to_id=message.reply_to_msg_id if message.reply_to else None,
            is_from_me=is_from_me,
            has_media=message.media is not None,
            media_type=type(message.media).__name__ if message.media else None,
        )

        if msg_id is None:
            # Duplicate message
            return

        log.debug(
            "message_ingested",
            msg_id=msg_id,
            chat_name=chat_name,
            sender=sender_name,
            text_len=len(message.text),
        )

        # Add to classification queue
        await self.store.add_to_classification_queue(msg_id)

        # If this is my reply to a recent question, check for implicit learning
        if is_from_me and message.reply_to_msg_id:
            await self._check_implicit_learning(
                chat_id, message.reply_to_msg_id, message.text
            )

    async def _check_implicit_learning(
        self,
        chat_id: int,
        reply_to_id: int,
        my_reply: str,
    ) -> None:
        """Check if this reply should trigger implicit learning."""
        # Get the message being replied to
        original = await self.store.get_message_by_telegram_id(reply_to_id, chat_id)
        if not original or not original.get("is_question"):
            return

        # Trigger implicit learning
        await self.suggester.learn_from_reply(
            question_text=original["text"],
            your_reply=my_reply,
            chat_id=chat_id,
        )

    async def _handle_typing(self, event) -> None:
        """Handle typing status updates."""
        config = get_config()
        if not config.answer_suggester.suppress_while_typing:
            return

        # Only track my typing
        user_id = getattr(event, "user_id", None)
        if user_id != self._my_id:
            return

        chat_id = getattr(event, "chat_id", None) or getattr(event, "user_id", None)
        if not chat_id:
            return

        # Mark chat as typing
        self._typing_chats.add(chat_id)

        # Cancel existing timeout
        if chat_id in self._typing_timeouts:
            self._typing_timeouts[chat_id].cancel()

        # Set timeout to clear typing status after 10 seconds
        async def clear_typing():
            await asyncio.sleep(10)
            self._typing_chats.discard(chat_id)
            self._typing_timeouts.pop(chat_id, None)

        self._typing_timeouts[chat_id] = asyncio.create_task(clear_typing())

    def is_typing_in_chat(self, chat_id: int) -> bool:
        """Check if the user is currently typing in a chat."""
        return chat_id in self._typing_chats

    async def catch_up(self, hours: int = 24) -> int:
        """
        Fetch messages from the last N hours on startup.
        Returns count of messages fetched.
        """
        config = get_config()
        from datetime import timezone
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours)
        count = 0

        log.info("catch_up_started", hours=hours)

        async for dialog in self.client.iter_dialogs():
            chat = dialog.entity
            chat_id = dialog.id
            chat_name = self._get_chat_name(chat)
            chat_type = self._get_chat_type(chat)

            # Skip ignored chats
            if _should_ignore_chat(chat_name, config.chats.ignore_patterns):
                log.debug("chat_ignored", chat_name=chat_name)
                continue

            try:
                async for message in self.client.iter_messages(
                    chat,
                    offset_date=now,
                    limit=500,
                ):
                    # Skip messages older than our window
                    if message.date < since:
                        break

                    if not message.text:
                        continue

                    sender = await message.get_sender()
                    sender_id = sender.id if sender else 0
                    sender_name = self._get_chat_name(sender) if sender else "Unknown"

                    msg_id = await self.store.store_message(
                        telegram_id=message.id,
                        chat_id=chat_id,
                        chat_name=chat_name,
                        chat_type=chat_type,
                        sender_id=sender_id,
                        sender_name=sender_name,
                        text=message.text,
                        timestamp=message.date,
                        reply_to_id=message.reply_to_msg_id if message.reply_to else None,
                        is_from_me=sender_id == self._my_id,
                        has_media=message.media is not None,
                        media_type=type(message.media).__name__ if message.media else None,
                    )

                    if msg_id:
                        count += 1
                        await self.store.add_to_classification_queue(msg_id)

            except Exception as e:
                log.warning("catch_up_chat_error", chat_name=chat_name, error=str(e))
                continue

        log.info("catch_up_completed", messages=count)
        return count

    async def run(self) -> None:
        """Run the ingester loop."""
        self._running = True

        # Register event handlers
        self.client.add_event_handler(
            self._handle_message, events.NewMessage(incoming=True)
        )
        self.client.add_event_handler(
            self._handle_message, events.NewMessage(outgoing=True)
        )
        self.client.add_event_handler(
            self._handle_typing, events.Raw(types=[UpdateUserTyping, UpdateChatUserTyping])
        )

        log.info("ingester_started")

        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

        log.info("ingester_stopped")

    def pause(self) -> None:
        """Pause message processing."""
        self._paused = True
        log.info("ingester_paused")

    def resume(self) -> None:
        """Resume message processing."""
        self._paused = False
        log.info("ingester_resumed")

    def stop(self) -> None:
        """Stop the ingester."""
        self._running = False

    async def disconnect(self) -> None:
        """Disconnect from Telegram."""
        if self._client:
            await self._client.disconnect()
            log.info("telegram_disconnected")
