"""Bot message delivery with Telegram Markdown formatting."""

import asyncio
import re
from typing import Optional

import structlog
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import get_config

log = structlog.get_logger()

# Maximum message length for Telegram
MAX_MESSAGE_LENGTH = 4096

# Characters that must be escaped in MarkdownV2
_MARKDOWN_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MARKDOWN_ESCAPE_RE.sub(r'\\\1', text)


class DeliveryBot:
    """Telegram bot for delivering digests and suggestions."""

    def __init__(self):
        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None
        self._delivery_chat_id: Optional[int] = None

        # Callbacks for commands
        self.on_digest_request: Optional[callable] = None
        self.on_pause_request: Optional[callable] = None
        self.on_resume_request: Optional[callable] = None
        self.on_stats_request: Optional[callable] = None
        self.on_search_request: Optional[callable] = None
        self.on_recent_request: Optional[callable] = None
        self.on_pending_request: Optional[callable] = None
        self.on_suggest_request: Optional[callable] = None

    async def start(self) -> None:
        """Start the bot."""
        config = get_config()

        self._delivery_chat_id = config.telegram.delivery_chat_id

        self._app = (
            Application.builder()
            .token(config.telegram.bot_token)
            .build()
        )

        # Register command handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("digest", self._cmd_digest))
        self._app.add_handler(CommandHandler("stats", self._cmd_stats))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("search", self._cmd_search))
        self._app.add_handler(CommandHandler("recent", self._cmd_recent))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))
        self._app.add_handler(CommandHandler("suggest", self._cmd_suggest))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

        self._bot = self._app.bot
        log.info("delivery_bot_started")

    async def stop(self) -> None:
        """Stop the bot."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            log.info("delivery_bot_stopped")

    @property
    def bot(self) -> Bot:
        if self._bot is None:
            raise RuntimeError("Bot not started. Call start() first.")
        return self._bot

    async def send_message(
        self,
        text: str,
        chat_id: Optional[int] = None,
        parse_mode: Optional[str] = ParseMode.MARKDOWN,
    ) -> None:
        """Send a message to the delivery chat or specified chat."""
        target = chat_id or self._delivery_chat_id
        if not target:
            log.error("no_delivery_chat_configured")
            return

        # Split long messages
        messages = self._split_message(text)

        for msg in messages:
            try:
                await self.bot.send_message(
                    chat_id=target,
                    text=msg,
                    parse_mode=parse_mode,
                )
            except Exception as e:
                log.error("send_message_error", error=str(e), chat_id=target)
                # Try without parse mode on error
                try:
                    await self.bot.send_message(chat_id=target, text=msg)
                except Exception as e2:
                    log.error("send_message_fallback_error", error=str(e2))

    def _split_message(self, text: str) -> list[str]:
        """Split a long message into chunks."""
        if len(text) <= MAX_MESSAGE_LENGTH:
            return [text]

        messages = []
        current = ""

        for line in text.split("\n"):
            if len(current) + len(line) + 1 <= MAX_MESSAGE_LENGTH:
                current += line + "\n"
            else:
                if current:
                    messages.append(current.rstrip())
                current = line + "\n"

        if current:
            messages.append(current.rstrip())

        return messages

    async def send_digest(self, digest: str) -> None:
        """Send the daily digest."""
        await self.send_message(digest)
        log.info("digest_sent")

    async def send_suggestion(self, suggestion: dict) -> None:
        """Send a suggestion notification."""
        # All content here comes from Telegram messages (untrusted), so send as plain text
        if suggestion.get("is_burst"):
            chats = ", ".join(
                [suggestion["chat_name"]]
                + [f"Chat {cid}" for cid in suggestion.get("burst_chats", [])]
            )
            text = (
                f"Repeated Question Detected\n\n"
                f"This question has been asked in: {chats}\n\n"
                f"Question: {suggestion['question'][:200]}\n\n"
            )
        else:
            text = f"Suggestion for {suggestion['chat_name']}\n\n"
            text += f"Question: {suggestion['question'][:200]}\n\n"

        for i, match in enumerate(suggestion["matches"], 1):
            if len(suggestion["matches"]) > 1:
                text += f"Option {i} (similarity: {match['similarity']:.0%}):\n"
            text += f"{match['answer']}\n\n"

        await self.send_message(text, parse_mode=None)

    # ==================== Auth & Command Handlers ====================

    def _is_authorized(self, update: Update) -> bool:
        """Check if the sender is the configured owner."""
        user = update.effective_user
        if not user:
            return False
        return user.id == self._delivery_chat_id

    async def _require_auth(self, update: Update) -> bool:
        """Check authorization; send rejection if unauthorized. Returns True if authorized."""
        if self._is_authorized(update):
            return True
        log.warning(
            "unauthorized_command",
            user_id=update.effective_user.id if update.effective_user else None,
            username=update.effective_user.username if update.effective_user else None,
            command=update.message.text if update.message else None,
        )
        # Don't reveal the bot exists to unauthorized users — just ignore silently.
        return False

    async def _cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /start command."""
        if not await self._require_auth(update):
            return
        await update.message.reply_text(
            "Telegram Digest Bot\n\n"
            "Use /help to see available commands."
        )

    async def _cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /help command."""
        if not await self._require_auth(update):
            return
        help_text = (
            "Available Commands\n\n"
            "/status - Check if system is running\n"
            "/digest - Force generate digest now\n"
            "/pending - Show mentions awaiting response\n"
            "/suggest - Pending + AI suggested replies\n"
            "/stats - Show detailed statistics\n"
            "/pause - Pause ingestion and suggestions\n"
            "/resume - Resume from pause\n"
            "/search <query> - Search Q&A knowledge base\n"
            "/recent <chat> - Get 24h summary for a chat\n"
            "/help - Show this message"
        )
        await update.message.reply_text(help_text)

    async def _cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /status command."""
        if not await self._require_auth(update):
            return
        await update.message.reply_text("System is running.")

    async def _cmd_digest(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /digest command."""
        if not await self._require_auth(update):
            return
        if self.on_digest_request:
            await update.message.reply_text("Generating digest...")
            await self.on_digest_request()
        else:
            await update.message.reply_text("Digest generation not configured.")

    async def _cmd_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stats command."""
        if not await self._require_auth(update):
            return
        if self.on_stats_request:
            stats = await self.on_stats_request()
            await update.message.reply_text(stats)
        else:
            await update.message.reply_text("Stats not available.")

    async def _cmd_pause(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /pause command."""
        if not await self._require_auth(update):
            return
        if self.on_pause_request:
            await self.on_pause_request()
            await update.message.reply_text("System paused.")
        else:
            await update.message.reply_text("Pause not configured.")

    async def _cmd_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /resume command."""
        if not await self._require_auth(update):
            return
        if self.on_resume_request:
            await self.on_resume_request()
            await update.message.reply_text("System resumed.")
        else:
            await update.message.reply_text("Resume not configured.")

    async def _cmd_search(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /search command."""
        if not await self._require_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /search <query>")
            return

        query = " ".join(context.args)

        if self.on_search_request:
            result = await self.on_search_request(query)
            # Send as plain text — result contains untrusted Telegram content
            await update.message.reply_text(result)
        else:
            await update.message.reply_text("Search not configured.")

    async def _cmd_recent(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /recent command."""
        if not await self._require_auth(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /recent <chat name>")
            return

        chat_query = " ".join(context.args)

        if self.on_recent_request:
            result = await self.on_recent_request(chat_query)
            # Send as plain text — result contains untrusted Telegram content
            await update.message.reply_text(result)
        else:
            await update.message.reply_text("Recent not configured.")

    async def _cmd_pending(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /pending command."""
        if not await self._require_auth(update):
            return
        if self.on_pending_request:
            await update.message.reply_text("Checking pending mentions...")
            result = await self.on_pending_request()
            await update.message.reply_text(result)
        else:
            await update.message.reply_text("Pending not configured.")

    async def _cmd_suggest(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /suggest command."""
        if not await self._require_auth(update):
            return
        if self.on_suggest_request:
            await update.message.reply_text("Generating suggested responses... (this may take a moment)")
            result = await self.on_suggest_request()
            await update.message.reply_text(result)
        else:
            await update.message.reply_text("Suggest not configured.")


# Global bot instance
_bot: Optional[DeliveryBot] = None


async def get_delivery_bot() -> DeliveryBot:
    """Get the global delivery bot instance."""
    global _bot
    if _bot is None:
        _bot = DeliveryBot()
    return _bot
