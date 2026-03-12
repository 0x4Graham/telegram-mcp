"""Q&A matching engine with suggestions, cooldowns, and implicit learning."""

import asyncio
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable, Optional

import anthropic
import structlog

from .config import get_config
from .prompts import ANSWER_SYNTHESIS_PROMPT, format_replies_for_synthesis
from .store import Store
from .vectors import VectorStore

log = structlog.get_logger()


class Suggester:
    """Q&A suggestion engine with cooldowns and burst detection."""

    def __init__(
        self,
        store: Store,
        vector_store: VectorStore,
        on_suggestion: Optional[Callable] = None,
    ):
        self.store = store
        self.vector_store = vector_store
        self.on_suggestion = on_suggestion  # Callback for sending suggestions

        # Burst detection: track recent questions by normalized text
        self._recent_questions: dict[str, list[tuple[int, datetime]]] = defaultdict(list)
        self._burst_window = timedelta(minutes=10)

        self._client: Optional[anthropic.AsyncAnthropic] = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not set")
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        return self._client

    def _normalize_question(self, text: str) -> str:
        """Normalize question text for burst detection."""
        return text.lower().strip()

    def _is_burst(self, question: str, chat_id: int) -> list[int]:
        """
        Check if this question is part of a burst.
        Returns list of chat_ids where the same question was recently asked.
        """
        normalized = self._normalize_question(question)
        now = datetime.now()

        # Clean old entries
        cutoff = now - self._burst_window
        self._recent_questions[normalized] = [
            (cid, ts)
            for cid, ts in self._recent_questions[normalized]
            if ts > cutoff
        ]

        # Get other chats with same question
        other_chats = [
            cid for cid, _ in self._recent_questions[normalized] if cid != chat_id
        ]

        # Add current occurrence
        self._recent_questions[normalized].append((chat_id, now))

        return other_chats

    async def process_question(
        self,
        message_id: int,
        question_text: str,
        chat_id: int,
        chat_name: str,
        is_typing: bool = False,
    ) -> Optional[dict]:
        """
        Process a detected question, potentially generating a suggestion.
        Returns suggestion dict if one was generated, None otherwise.
        """
        config = get_config()

        if not config.answer_suggester.enabled:
            return None

        # Check if user is typing (suppress suggestions)
        if is_typing and config.answer_suggester.suppress_while_typing:
            log.debug("suggestion_suppressed_typing", chat_id=chat_id)
            return None

        # Query for similar Q&A pairs
        matches = self.vector_store.query_similar(
            question_text,
            threshold=config.answer_suggester.similarity_threshold,
            limit=config.answer_suggester.show_top_matches,
        )

        if not matches:
            log.debug("no_similar_qa_found", question=question_text[:50])
            return None

        # Check cooldowns for all matches
        valid_matches = []
        for match in matches:
            if not await self.store.is_on_cooldown(chat_id, match["qa_pair_id"]):
                valid_matches.append(match)

        if not valid_matches:
            log.debug("all_matches_on_cooldown", chat_id=chat_id)
            return None

        # Check for burst
        burst_chats = self._is_burst(question_text, chat_id)
        is_burst = len(burst_chats) > 0

        # Build suggestion
        suggestion = {
            "message_id": message_id,
            "question": question_text,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "matches": valid_matches,
            "is_burst": is_burst,
            "burst_chats": burst_chats if is_burst else None,
            "timestamp": datetime.now(),
        }

        # Set cooldowns for suggested Q&A pairs
        for match in valid_matches:
            await self.store.set_cooldown(
                chat_id, match["qa_pair_id"], config.answer_suggester.cooldown_minutes
            )
            await self.store.increment_qa_suggestion_count(match["qa_pair_id"])

            # Store suggestion record
            await self.store.store_suggestion(
                qa_pair_id=match["qa_pair_id"],
                target_chat_id=chat_id,
                similarity_score=match["similarity"],
                target_message_id=message_id,
            )

        log.info(
            "suggestion_generated",
            chat_id=chat_id,
            matches=len(valid_matches),
            is_burst=is_burst,
        )

        # Trigger callback if set
        if self.on_suggestion:
            await self.on_suggestion(suggestion)

        return suggestion

    async def extract_qa_pair(
        self,
        question_msg: dict,
        reply_msgs: list[dict],
    ) -> Optional[int]:
        """
        Extract a Q&A pair from a question and its replies.
        Returns the Q&A pair ID if created, None otherwise.
        """
        if not reply_msgs:
            return None

        question_text = question_msg.get("text", "")
        if not question_text:
            return None

        config = get_config()

        # Synthesize answer from multiple replies
        if len(reply_msgs) == 1:
            answer_text = reply_msgs[0].get("text", "")
        else:
            answer_text = await self._synthesize_answer(question_text, reply_msgs)

        if not answer_text:
            return None

        # Store Q&A pair in SQLite
        qa_pair_id = await self.store.store_qa_pair(
            question_text=question_text,
            answer_text=answer_text,
            chat_id=question_msg["chat_id"],
            chat_name=question_msg["chat_name"],
            question_message_id=question_msg.get("id"),
            answer_message_id=reply_msgs[-1].get("id"),
            question_from=question_msg.get("sender_name"),
            answered_at=datetime.fromisoformat(reply_msgs[-1]["timestamp"])
            if reply_msgs[-1].get("timestamp")
            else None,
        )

        # Add to vector store (handles dedup)
        added = self.vector_store.add_qa_pair(
            qa_pair_id=qa_pair_id,
            question=question_text,
            answer=answer_text,
            chat_id=question_msg["chat_id"],
            chat_name=question_msg["chat_name"],
            timestamp=datetime.fromisoformat(question_msg["timestamp"])
            if question_msg.get("timestamp")
            else None,
        )

        if not added:
            # Duplicate detected, merge answers
            existing = self.vector_store.query_similar(
                question_text, threshold=0.95, limit=1
            )
            if existing:
                await self._merge_qa_pairs(existing[0]["qa_pair_id"], qa_pair_id)

        log.info(
            "qa_pair_extracted",
            qa_pair_id=qa_pair_id,
            chat_name=question_msg["chat_name"],
            added_to_vectors=added,
        )

        return qa_pair_id

    async def _synthesize_answer(
        self, question: str, replies: list[dict]
    ) -> str:
        """Synthesize multiple replies into a single answer using Claude."""
        config = get_config()

        formatted_replies = format_replies_for_synthesis(
            [
                {"sender": r.get("sender_name", "Unknown"), "text": r.get("text", "")}
                for r in replies
                if r.get("text")
            ]
        )

        prompt = ANSWER_SYNTHESIS_PROMPT.format(
            question=question, replies=formatted_replies
        )

        try:
            response = await self.client.messages.create(
                model=config.llm.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except anthropic.APIError as e:
            log.error("answer_synthesis_error", error=str(e))
            # Fallback: concatenate replies
            return "\n\n".join(
                f"{r.get('sender_name', 'Unknown')}: {r.get('text', '')}"
                for r in replies
                if r.get("text")
            )

    async def _merge_qa_pairs(
        self, existing_id: int, new_id: int
    ) -> None:
        """Merge a new Q&A pair into an existing one."""
        existing = await self.store.get_qa_pair(existing_id)
        new = await self.store.get_qa_pair(new_id)

        if not existing or not new:
            return

        # Synthesize merged answer
        config = get_config()

        prompt = ANSWER_SYNTHESIS_PROMPT.format(
            question=existing["question_text"],
            replies=f"Previous answer: {existing['answer_text']}\n\nNew answer: {new['answer_text']}",
        )

        try:
            response = await self.client.messages.create(
                model=config.llm.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            merged_answer = response.content[0].text.strip()
        except anthropic.APIError:
            # Fallback: concatenate
            merged_answer = f"{existing['answer_text']}\n\nAlternatively: {new['answer_text']}"

        # Update existing Q&A pair
        await self.store.update_qa_pair_answer(existing_id, merged_answer)
        self.vector_store.update_qa_pair(existing_id, answer=merged_answer)

        log.info("qa_pairs_merged", existing_id=existing_id, new_id=new_id)

    async def learn_from_reply(
        self,
        question_text: str,
        your_reply: str,
        chat_id: int,
    ) -> None:
        """
        Implicit learning: update Q&A pair if user replied differently.
        Called when the user sends a message that could be answering a recent question.
        """
        # Find similar questions that were recently suggested
        matches = self.vector_store.query_similar(question_text, threshold=0.85, limit=1)

        if not matches:
            return

        match = matches[0]
        existing_answer = match["answer"]

        # Check if the reply is substantially different
        if self._answers_are_similar(existing_answer, your_reply):
            return

        # Update the Q&A pair with the new answer
        await self.store.update_qa_pair_answer(match["qa_pair_id"], your_reply)
        self.vector_store.update_qa_pair(match["qa_pair_id"], answer=your_reply)

        log.info(
            "implicit_learning",
            qa_pair_id=match["qa_pair_id"],
            old_answer_len=len(existing_answer),
            new_answer_len=len(your_reply),
        )

    def _answers_are_similar(self, a: str, b: str) -> bool:
        """Check if two answers are similar enough to skip learning."""
        # Simple heuristic: if one is a substring of the other, they're similar
        a_lower = a.lower().strip()
        b_lower = b.lower().strip()

        if a_lower in b_lower or b_lower in a_lower:
            return True

        # If lengths are very different, they're not similar
        len_ratio = min(len(a), len(b)) / max(len(a), len(b))
        return len_ratio > 0.9 and a_lower[:50] == b_lower[:50]


async def test_query(query: str) -> None:
    """CLI test: query the Q&A knowledge base."""
    from .store import get_store
    from .vectors import get_vector_store

    store = await get_store()
    vector_store = get_vector_store()

    suggester = Suggester(store, vector_store)

    matches = vector_store.query_similar(query, threshold=0.0, limit=5)

    if not matches:
        print("No matches found.")
        return

    print(f"\nQuery: {query}\n")
    print("-" * 60)

    for i, match in enumerate(matches, 1):
        print(f"\n{i}. Similarity: {match['similarity']:.2%}")
        print(f"   Question: {match['question'][:100]}...")
        print(f"   Answer: {match['answer'][:200]}...")
        print(f"   From: {match['chat_name']}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2 and sys.argv[1] == "--test":
        query = " ".join(sys.argv[2:])
        asyncio.run(test_query(query))
    else:
        print("Usage: python -m src.suggester --test 'your question here'")
