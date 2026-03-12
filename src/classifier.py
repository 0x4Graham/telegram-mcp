"""Batch question classification using Claude."""

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Optional

import anthropic
import structlog

from .config import get_config
from .prompts import QUESTION_DETECTION_PROMPT, format_messages_for_classification
from .store import Store

log = structlog.get_logger()


class Classifier:
    """Batch question classifier using Claude."""

    def __init__(self, store: Store):
        self.store = store
        self._client: Optional[anthropic.AsyncAnthropic] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not set")
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        return self._client

    async def classify_batch(self, messages: list[dict]) -> dict[int, bool]:
        """
        Classify a batch of messages as questions or not.
        Returns dict mapping message_id to is_question.
        """
        if not messages:
            return {}

        config = get_config()

        # Format messages for the prompt
        formatted = format_messages_for_classification(
            [
                {
                    "id": m["id"],
                    "sender": m["sender_name"],
                    "text": m["text"] or "",
                }
                for m in messages
            ]
        )

        prompt = QUESTION_DETECTION_PROMPT.format(messages=formatted)

        try:
            response = await self.client.messages.create(
                model=config.llm.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            # Parse JSON response
            content = response.content[0].text.strip()

            # Handle potential markdown code blocks
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content[:-3]

            classifications = json.loads(content)

            results = {}
            for item in classifications:
                msg_id = item.get("message_id")
                is_question = item.get("is_question", False)
                results[msg_id] = is_question

            log.info(
                "batch_classified",
                total=len(messages),
                questions=sum(1 for v in results.values() if v),
            )

            return results

        except json.JSONDecodeError as e:
            log.error("classification_json_error", error=str(e))
            return {}
        except anthropic.APIError as e:
            log.error("classification_api_error", error=str(e))
            return {}

    async def process_queue(self) -> int:
        """
        Process the classification queue.
        Returns number of messages classified.
        """
        config = get_config()
        batch_size = config.question_detection.batch_size

        # Get queued messages
        queued = await self.store.get_classification_queue(limit=batch_size)
        if not queued:
            return 0

        # Classify batch
        results = await self.classify_batch(queued)

        # Update messages and clear queue
        message_ids = []
        for msg in queued:
            msg_id = msg["id"]
            is_question = results.get(msg_id, False)
            await self.store.mark_message_as_question(msg_id, is_question)
            message_ids.append(msg_id)

        await self.store.clear_classification_queue(message_ids)

        return len(message_ids)

    async def should_process_now(self) -> bool:
        """Check if the queue should be processed based on size or age."""
        config = get_config()

        queue_size = await self.store.get_classification_queue_size()
        if queue_size >= config.question_detection.batch_size:
            return True

        oldest = await self.store.get_oldest_queued_time()
        if oldest:
            max_wait = timedelta(minutes=config.question_detection.max_wait_minutes)
            if datetime.now() - oldest >= max_wait:
                return True

        return False

    async def run_loop(self) -> None:
        """Run the classification loop."""
        self._running = True
        log.info("classifier_loop_started")

        while self._running:
            try:
                if await self.should_process_now():
                    await self.process_queue()

                # Check every 30 seconds
                await asyncio.sleep(30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("classifier_loop_error", error=str(e))
                await asyncio.sleep(60)  # Back off on errors

        log.info("classifier_loop_stopped")

    def start(self) -> asyncio.Task:
        """Start the classification loop in the background."""
        self._task = asyncio.create_task(self.run_loop())
        return self._task

    def stop(self) -> None:
        """Stop the classification loop."""
        self._running = False
        if self._task:
            self._task.cancel()
