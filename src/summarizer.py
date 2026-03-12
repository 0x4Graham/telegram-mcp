"""Digest generation with chat summarization and aggregation."""

import asyncio
import fnmatch
import os
from datetime import datetime, timedelta
from typing import Optional

import anthropic
import structlog

from .config import get_config


def _should_ignore_chat(chat_name: str, patterns: list[str]) -> bool:
    """Check if chat matches any ignore pattern."""
    for pattern in patterns:
        if fnmatch.fnmatch(chat_name.lower(), pattern.lower()):
            return True
    return False
from .prompts import (
    CHAT_SUMMARY_PROMPT,
    DIGEST_AGGREGATE_PROMPT,
    format_messages_for_summary,
    get_detail_level,
)
from .store import Store

log = structlog.get_logger()

# Retry settings
MAX_RETRIES = 8  # 8 retries * 15 min = 2 hours
RETRY_INTERVAL = 15 * 60  # 15 minutes in seconds


class Summarizer:
    """Generates daily digests from chat messages."""

    def __init__(self, store: Store):
        self.store = store
        self._client: Optional[anthropic.AsyncAnthropic] = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not set")
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        return self._client

    async def summarize_chat(
        self,
        messages: list[dict],
        chat_name: str,
        priority: int = 3,
    ) -> str:
        """Summarize a single chat's messages."""
        config = get_config()

        if not messages:
            return ""

        detail_level = get_detail_level(priority)

        formatted = format_messages_for_summary(
            [
                {
                    "sender": m["sender_name"],
                    "text": m["text"],
                    "timestamp": m.get("timestamp", ""),
                }
                for m in messages
                if m.get("text")
            ]
        )

        prompt = CHAT_SUMMARY_PROMPT.format(
            chat_name=chat_name,
            messages=formatted,
            detail_level=detail_level,
        )

        response = await self.client.messages.create(
            model=config.llm.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        return response.content[0].text.strip()

    async def generate_digest(
        self,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        Generate a full digest for the specified time period.
        Uses retry logic on API failures.
        """
        config = get_config()

        if period_end is None:
            period_end = datetime.now()
        if period_start is None:
            period_start = period_end - timedelta(hours=config.digest.lookback_hours)

        log.info(
            "digest_generation_started",
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )

        for attempt in range(MAX_RETRIES + 1):
            try:
                return await self._generate_digest_impl(period_start, period_end)
            except anthropic.APIError as e:
                if attempt < MAX_RETRIES:
                    log.warning(
                        "digest_api_error_retry",
                        attempt=attempt + 1,
                        max_retries=MAX_RETRIES,
                        error=str(e),
                        retry_in=RETRY_INTERVAL,
                    )
                    await asyncio.sleep(RETRY_INTERVAL)
                else:
                    log.error(
                        "digest_generation_failed",
                        error=str(e),
                        attempts=MAX_RETRIES + 1,
                    )
                    return None

        return None

    async def _generate_digest_impl(
        self,
        period_start: datetime,
        period_end: datetime,
    ) -> str:
        """Internal implementation of digest generation."""
        config = get_config()

        # Get messages grouped by chat
        messages_by_chat = await self.store.get_messages_for_digest(
            period_start, period_end
        )

        if not messages_by_chat:
            # Quiet day
            return self._format_quiet_day_digest(period_start, period_end)

        # Get ignore patterns
        ignore_patterns = config.chats.ignore_patterns

        # Get chat priorities and filter out priority 5 and ignored chats
        chat_priorities = {}
        filtered_chats = {}

        for chat_id, messages in messages_by_chat.items():
            # Get chat name from first message
            chat_name = messages[0]["chat_name"] if messages else ""

            # Skip ignored chats
            if _should_ignore_chat(chat_name, ignore_patterns):
                log.debug("digest_chat_ignored", chat_name=chat_name)
                continue

            priority = await self.store.get_chat_priority(
                chat_id, config.chats.default_priority
            )
            if priority < 5:  # Priority 5 is excluded
                chat_priorities[chat_id] = priority
                filtered_chats[chat_id] = messages

        if not filtered_chats:
            return self._format_quiet_day_digest(period_start, period_end)

        # Sort chats by priority
        sorted_chat_ids = sorted(
            filtered_chats.keys(), key=lambda cid: chat_priorities.get(cid, 3)
        )

        # Summarize each chat
        summaries = []
        for chat_id in sorted_chat_ids:
            messages = filtered_chats[chat_id]
            chat_name = messages[0]["chat_name"] if messages else f"Chat {chat_id}"
            priority = chat_priorities.get(chat_id, 3)

            summary = await self.summarize_chat(messages, chat_name, priority)
            if summary:
                summaries.append(
                    {
                        "chat_name": chat_name,
                        "priority": priority,
                        "summary": summary,
                        "message_count": len(messages),
                    }
                )

        # Calculate stats
        total_messages = sum(len(msgs) for msgs in messages_by_chat.values())
        total_chats = len(messages_by_chat)

        # Aggregate into final digest
        digest = await self._aggregate_digest(
            summaries=summaries,
            message_count=total_messages,
            chat_count=total_chats,
            period_start=period_start,
            period_end=period_end,
        )

        # Store digest record
        await self.store.store_digest(
            period_start=period_start,
            period_end=period_end,
            content=digest,
            message_count=total_messages,
            chat_count=total_chats,
            metadata={
                "chats": [s["chat_name"] for s in summaries],
                "priorities": {s["chat_name"]: s["priority"] for s in summaries},
            },
        )

        log.info(
            "digest_generated",
            message_count=total_messages,
            chat_count=total_chats,
            summaries=len(summaries),
        )

        return digest

    async def _aggregate_digest(
        self,
        summaries: list[dict],
        message_count: int,
        chat_count: int,
        period_start: datetime,
        period_end: datetime,
    ) -> str:
        """Aggregate chat summaries into a single digest."""
        config = get_config()

        # Format summaries for the prompt
        formatted_summaries = "\n\n".join(
            f"**{s['chat_name']}** ({s['message_count']} messages):\n{s['summary']}"
            for s in summaries
        )

        prompt = DIGEST_AGGREGATE_PROMPT.format(
            summaries=formatted_summaries,
            message_count=message_count,
            chat_count=chat_count,
            period_start=period_start.strftime("%Y-%m-%d %H:%M"),
            period_end=period_end.strftime("%Y-%m-%d %H:%M"),
            target_length=config.digest.target_length,
        )

        response = await self.client.messages.create(
            model=config.llm.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        return response.content[0].text.strip()

    def _format_quiet_day_digest(
        self,
        period_start: datetime,
        period_end: datetime,
    ) -> str:
        """Format a digest for quiet days with minimal activity."""
        return (
            f"**Daily Digest**\n"
            f"_{period_start.strftime('%Y-%m-%d %H:%M')} - {period_end.strftime('%Y-%m-%d %H:%M')}_\n\n"
            f"Quiet day - no significant activity to report.\n\n"
            f"**Stats**\n"
            f"- Messages: 0\n"
            f"- Active chats: 0"
        )

    async def generate_chat_summary(
        self,
        chat_id: int,
        hours: int = 24,
    ) -> Optional[str]:
        """Generate a summary for a single chat (for /recent command)."""
        period_end = datetime.now()
        period_start = period_end - timedelta(hours=hours)

        messages = await self.store.get_messages_since(period_start, chat_id=chat_id)

        if not messages:
            return None

        chat_name = messages[0]["chat_name"] if messages else f"Chat {chat_id}"
        priority = await self.store.get_chat_priority(chat_id, 3)

        return await self.summarize_chat(messages, chat_name, priority)
