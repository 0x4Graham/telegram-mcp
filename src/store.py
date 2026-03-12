"""SQLite database operations for message storage and Q&A pairs."""

import json
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
import structlog

from .config import get_db_path

log = structlog.get_logger()

SCHEMA = """
-- Messages table
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_name TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    sender_id INTEGER NOT NULL,
    sender_name TEXT NOT NULL,
    text TEXT,
    timestamp DATETIME NOT NULL,
    reply_to_id INTEGER,
    is_from_me BOOLEAN NOT NULL DEFAULT 0,
    has_media BOOLEAN NOT NULL DEFAULT 0,
    media_type TEXT,
    is_question BOOLEAN,
    processed BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(telegram_id, chat_id)
);

-- Chats table
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 3,
    last_message_at DATETIME,
    first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Q&A pairs table
CREATE TABLE IF NOT EXISTS qa_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_message_id INTEGER,
    answer_message_id INTEGER,
    question_text TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_name TEXT NOT NULL,
    question_from TEXT,
    answered_at DATETIME,
    times_suggested INTEGER NOT NULL DEFAULT 0,
    last_suggested_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (question_message_id) REFERENCES messages(id),
    FOREIGN KEY (answer_message_id) REFERENCES messages(id)
);

-- Suggestions table
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qa_pair_id INTEGER NOT NULL,
    target_chat_id INTEGER NOT NULL,
    target_message_id INTEGER,
    suggested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    similarity_score REAL NOT NULL,
    FOREIGN KEY (qa_pair_id) REFERENCES qa_pairs(id)
);

-- Digests table
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    period_start DATETIME NOT NULL,
    period_end DATETIME NOT NULL,
    content TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    chat_count INTEGER NOT NULL,
    metadata_json TEXT
);

-- Classification queue for batch processing
CREATE TABLE IF NOT EXISTS classification_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE,
    queued_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

-- Cooldowns table for suggestion rate limiting
CREATE TABLE IF NOT EXISTS cooldowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    qa_pair_id INTEGER NOT NULL,
    cooldown_until DATETIME NOT NULL,
    UNIQUE(chat_id, qa_pair_id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_messages_chat_timestamp ON messages(chat_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_is_question ON messages(is_question);
CREATE INDEX IF NOT EXISTS idx_qa_pairs_chat ON qa_pairs(chat_id);
CREATE INDEX IF NOT EXISTS idx_classification_queue_queued ON classification_queue(queued_at);
CREATE INDEX IF NOT EXISTS idx_cooldowns_until ON cooldowns(cooldown_until);
"""


class Store:
    """Async SQLite store for messages and Q&A pairs."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(get_db_path())
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Connect to the database and create schema."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("database_connected", path=self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            log.info("database_closed")

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    # ==================== Messages ====================

    async def store_message(
        self,
        telegram_id: int,
        chat_id: int,
        chat_name: str,
        chat_type: str,
        sender_id: int,
        sender_name: str,
        text: Optional[str],
        timestamp: datetime,
        reply_to_id: Optional[int] = None,
        is_from_me: bool = False,
        has_media: bool = False,
        media_type: Optional[str] = None,
    ) -> Optional[int]:
        """Store a message, returning the row ID or None if duplicate."""
        try:
            cursor = await self.db.execute(
                """
                INSERT INTO messages (
                    telegram_id, chat_id, chat_name, chat_type, sender_id,
                    sender_name, text, timestamp, reply_to_id, is_from_me,
                    has_media, media_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_id, chat_id, chat_name, chat_type, sender_id,
                    sender_name, text, timestamp.isoformat(), reply_to_id,
                    is_from_me, has_media, media_type
                ),
            )
            await self.db.commit()

            # Update chat record
            await self._upsert_chat(chat_id, chat_name, chat_type, timestamp)

            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            # Duplicate message
            return None

    async def get_message_by_telegram_id(
        self, telegram_id: int, chat_id: int
    ) -> Optional[dict]:
        """Get a message by its Telegram ID and chat ID."""
        async with self.db.execute(
            "SELECT * FROM messages WHERE telegram_id = ? AND chat_id = ?",
            (telegram_id, chat_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_messages_since(
        self, since: datetime, chat_id: Optional[int] = None
    ) -> list[dict]:
        """Get messages since a timestamp, optionally filtered by chat."""
        if chat_id:
            query = """
                SELECT * FROM messages
                WHERE timestamp >= ? AND chat_id = ?
                ORDER BY timestamp ASC
            """
            params = (since.isoformat(), chat_id)
        else:
            query = """
                SELECT * FROM messages
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
            """
            params = (since.isoformat(),)

        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_messages_for_digest(
        self, since: datetime, until: datetime
    ) -> dict[int, list[dict]]:
        """Get messages grouped by chat for digest generation."""
        query = """
            SELECT m.*, c.priority
            FROM messages m
            LEFT JOIN chats c ON m.chat_id = c.chat_id
            WHERE m.timestamp >= ? AND m.timestamp < ?
            AND m.text IS NOT NULL AND m.text != ''
            ORDER BY COALESCE(c.priority, 3), m.timestamp ASC
        """
        async with self.db.execute(
            query, (since.isoformat(), until.isoformat())
        ) as cursor:
            rows = await cursor.fetchall()

        # Group by chat_id
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            row_dict = dict(row)
            chat_id = row_dict["chat_id"]
            if chat_id not in grouped:
                grouped[chat_id] = []
            grouped[chat_id].append(row_dict)

        return grouped

    async def mark_message_as_question(
        self, message_id: int, is_question: bool
    ) -> None:
        """Mark a message as a question or not."""
        await self.db.execute(
            "UPDATE messages SET is_question = ?, processed = 1 WHERE id = ?",
            (is_question, message_id),
        )
        await self.db.commit()

    async def get_replies_to_message(
        self, telegram_id: int, chat_id: int, within_minutes: int = 15
    ) -> list[dict]:
        """Get replies to a message within a time window."""
        # First get the original message timestamp
        async with self.db.execute(
            "SELECT timestamp FROM messages WHERE telegram_id = ? AND chat_id = ?",
            (telegram_id, chat_id),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return []
            msg_time = datetime.fromisoformat(row["timestamp"])

        end_time = msg_time + timedelta(minutes=within_minutes)

        # Get direct replies or messages in the time window
        query = """
            SELECT * FROM messages
            WHERE chat_id = ?
            AND (
                reply_to_id = ?
                OR (timestamp > ? AND timestamp <= ?)
            )
            ORDER BY timestamp ASC
        """
        async with self.db.execute(
            query,
            (chat_id, telegram_id, msg_time.isoformat(), end_time.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== Chats ====================

    async def _upsert_chat(
        self, chat_id: int, name: str, chat_type: str, last_message_at: datetime
    ) -> None:
        """Insert or update a chat record."""
        await self.db.execute(
            """
            INSERT INTO chats (chat_id, name, type, last_message_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                name = excluded.name,
                last_message_at = MAX(chats.last_message_at, excluded.last_message_at)
            """,
            (chat_id, name, chat_type, last_message_at.isoformat()),
        )
        await self.db.commit()

    async def get_chat(self, chat_id: int) -> Optional[dict]:
        """Get a chat by ID."""
        async with self.db.execute(
            "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_chats(self) -> list[dict]:
        """Get all chats ordered by last message time."""
        async with self.db.execute(
            "SELECT * FROM chats ORDER BY last_message_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def set_chat_priority(self, chat_id: int, priority: int) -> None:
        """Set the priority for a chat."""
        await self.db.execute(
            "UPDATE chats SET priority = ? WHERE chat_id = ?",
            (priority, chat_id),
        )
        await self.db.commit()

    async def get_chat_priority(self, chat_id: int, default: int = 3) -> int:
        """Get the priority for a chat."""
        async with self.db.execute(
            "SELECT priority FROM chats WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["priority"] if row else default

    # ==================== Q&A Pairs ====================

    async def store_qa_pair(
        self,
        question_text: str,
        answer_text: str,
        chat_id: int,
        chat_name: str,
        question_message_id: Optional[int] = None,
        answer_message_id: Optional[int] = None,
        question_from: Optional[str] = None,
        answered_at: Optional[datetime] = None,
    ) -> int:
        """Store a Q&A pair, returning the row ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO qa_pairs (
                question_message_id, answer_message_id, question_text,
                answer_text, chat_id, chat_name, question_from, answered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question_message_id,
                answer_message_id,
                question_text,
                answer_text,
                chat_id,
                chat_name,
                question_from,
                answered_at.isoformat() if answered_at else None,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_qa_pair(self, qa_pair_id: int) -> Optional[dict]:
        """Get a Q&A pair by ID."""
        async with self.db.execute(
            "SELECT * FROM qa_pairs WHERE id = ?", (qa_pair_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_qa_pair_answer(
        self, qa_pair_id: int, new_answer: str
    ) -> None:
        """Update the answer text for a Q&A pair (implicit learning)."""
        await self.db.execute(
            "UPDATE qa_pairs SET answer_text = ? WHERE id = ?",
            (new_answer, qa_pair_id),
        )
        await self.db.commit()
        log.info("qa_pair_answer_updated", qa_pair_id=qa_pair_id)

    async def increment_qa_suggestion_count(self, qa_pair_id: int) -> None:
        """Increment the suggestion count for a Q&A pair."""
        await self.db.execute(
            """
            UPDATE qa_pairs
            SET times_suggested = times_suggested + 1,
                last_suggested_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (qa_pair_id,),
        )
        await self.db.commit()

    async def get_all_qa_pairs(self) -> list[dict]:
        """Get all Q&A pairs."""
        async with self.db.execute(
            "SELECT * FROM qa_pairs ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== Classification Queue ====================

    async def add_to_classification_queue(self, message_id: int) -> bool:
        """Add a message to the classification queue. Returns False if already queued."""
        try:
            await self.db.execute(
                "INSERT INTO classification_queue (message_id) VALUES (?)",
                (message_id,),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_classification_queue(
        self, limit: Optional[int] = None
    ) -> list[dict]:
        """Get messages pending classification."""
        query = """
            SELECT cq.id as queue_id, cq.queued_at, m.*
            FROM classification_queue cq
            JOIN messages m ON cq.message_id = m.id
            ORDER BY cq.queued_at ASC
        """
        params = []
        if limit:
            query += " LIMIT ?"
            params.append(int(limit))

        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_classification_queue_size(self) -> int:
        """Get the number of messages pending classification."""
        async with self.db.execute(
            "SELECT COUNT(*) as count FROM classification_queue"
        ) as cursor:
            row = await cursor.fetchone()
            return row["count"]

    async def get_oldest_queued_time(self) -> Optional[datetime]:
        """Get the timestamp of the oldest queued message."""
        async with self.db.execute(
            "SELECT MIN(queued_at) as oldest FROM classification_queue"
        ) as cursor:
            row = await cursor.fetchone()
            if row and row["oldest"]:
                return datetime.fromisoformat(row["oldest"])
            return None

    async def clear_classification_queue(self, message_ids: list[int]) -> None:
        """Remove messages from the classification queue."""
        if not message_ids:
            return
        placeholders = ",".join("?" * len(message_ids))
        await self.db.execute(
            f"DELETE FROM classification_queue WHERE message_id IN ({placeholders})",
            message_ids,
        )
        await self.db.commit()

    # ==================== Cooldowns ====================

    async def set_cooldown(
        self, chat_id: int, qa_pair_id: int, minutes: int
    ) -> None:
        """Set a cooldown for a chat + Q&A pair combination."""
        cooldown_until = datetime.now() + timedelta(minutes=minutes)
        await self.db.execute(
            """
            INSERT INTO cooldowns (chat_id, qa_pair_id, cooldown_until)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, qa_pair_id) DO UPDATE SET
                cooldown_until = excluded.cooldown_until
            """,
            (chat_id, qa_pair_id, cooldown_until.isoformat()),
        )
        await self.db.commit()

    async def is_on_cooldown(self, chat_id: int, qa_pair_id: int) -> bool:
        """Check if a chat + Q&A pair is on cooldown."""
        async with self.db.execute(
            """
            SELECT cooldown_until FROM cooldowns
            WHERE chat_id = ? AND qa_pair_id = ? AND cooldown_until > ?
            """,
            (chat_id, qa_pair_id, datetime.now().isoformat()),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def cleanup_expired_cooldowns(self) -> int:
        """Remove expired cooldowns. Returns count of removed rows."""
        cursor = await self.db.execute(
            "DELETE FROM cooldowns WHERE cooldown_until <= ?",
            (datetime.now().isoformat(),),
        )
        await self.db.commit()
        return cursor.rowcount

    # ==================== Suggestions ====================

    async def store_suggestion(
        self,
        qa_pair_id: int,
        target_chat_id: int,
        similarity_score: float,
        target_message_id: Optional[int] = None,
    ) -> int:
        """Store a suggestion record."""
        cursor = await self.db.execute(
            """
            INSERT INTO suggestions (
                qa_pair_id, target_chat_id, target_message_id, similarity_score
            ) VALUES (?, ?, ?, ?)
            """,
            (qa_pair_id, target_chat_id, target_message_id, similarity_score),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_recent_suggestions(self, limit: int = 50) -> list[dict]:
        """Get recent suggestions with Q&A pair details."""
        query = """
            SELECT s.*, q.question_text, q.answer_text, q.chat_name as source_chat
            FROM suggestions s
            JOIN qa_pairs q ON s.qa_pair_id = q.id
            ORDER BY s.suggested_at DESC
            LIMIT ?
        """
        async with self.db.execute(query, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== Digests ====================

    async def store_digest(
        self,
        period_start: datetime,
        period_end: datetime,
        content: str,
        message_count: int,
        chat_count: int,
        metadata: Optional[dict] = None,
    ) -> int:
        """Store a digest record."""
        cursor = await self.db.execute(
            """
            INSERT INTO digests (
                period_start, period_end, content, message_count,
                chat_count, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                period_start.isoformat(),
                period_end.isoformat(),
                content,
                message_count,
                chat_count,
                json.dumps(metadata) if metadata else None,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_recent_digests(self, limit: int = 10) -> list[dict]:
        """Get recent digests."""
        async with self.db.execute(
            "SELECT * FROM digests ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== Data Retention ====================

    async def cleanup_old_messages(self, days: int = 90) -> int:
        """Delete messages older than specified days. Returns count of deleted rows."""
        cutoff = datetime.now() - timedelta(days=days)

        # First, delete Q&A pairs that reference old messages
        await self.db.execute(
            """
            DELETE FROM qa_pairs
            WHERE question_message_id IN (
                SELECT id FROM messages WHERE timestamp < ?
            )
            """,
            (cutoff.isoformat(),),
        )

        # Then delete the messages
        cursor = await self.db.execute(
            "DELETE FROM messages WHERE timestamp < ?",
            (cutoff.isoformat(),),
        )
        await self.db.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            log.info("old_messages_cleaned", count=deleted, days=days)
        return deleted

    # ==================== Pending Responses ====================

    async def get_pending_mentions(
        self,
        username: str,
        since: datetime,
        ignore_patterns: list[str] = None,
    ) -> list[dict]:
        """
        Get messages where username was mentioned but no response was given.
        A response is defined as any message from the user in that chat after the mention.
        """
        import fnmatch

        # Find messages mentioning the user
        query = """
            SELECT m.*,
                (SELECT COUNT(*) FROM messages m2
                 WHERE m2.chat_id = m.chat_id
                 AND m2.is_from_me = 1
                 AND m2.timestamp > m.timestamp) as responses_after
            FROM messages m
            WHERE m.timestamp >= ?
            AND m.is_from_me = 0
            AND (m.text LIKE ? OR m.text LIKE ?)
            ORDER BY m.timestamp DESC
        """
        mention_pattern = f"%@{username}%"
        mention_pattern_lower = f"%@{username.lower()}%"

        async with self.db.execute(
            query, (since.isoformat(), mention_pattern, mention_pattern_lower)
        ) as cursor:
            rows = await cursor.fetchall()

        results = []
        for row in rows:
            row_dict = dict(row)
            # Filter ignored chats
            if ignore_patterns:
                chat_name = row_dict["chat_name"]
                ignored = any(
                    fnmatch.fnmatch(chat_name.lower(), p.lower())
                    for p in ignore_patterns
                )
                if ignored:
                    continue
            # Only include if no response after
            if row_dict["responses_after"] == 0:
                results.append(row_dict)

        return results

    # ==================== Stats ====================

    async def get_stats(self) -> dict:
        """Get database statistics."""
        stats = {}

        async with self.db.execute("SELECT COUNT(*) as c FROM messages") as cur:
            stats["total_messages"] = (await cur.fetchone())["c"]

        async with self.db.execute("SELECT COUNT(*) as c FROM chats") as cur:
            stats["total_chats"] = (await cur.fetchone())["c"]

        async with self.db.execute("SELECT COUNT(*) as c FROM qa_pairs") as cur:
            stats["total_qa_pairs"] = (await cur.fetchone())["c"]

        async with self.db.execute("SELECT COUNT(*) as c FROM suggestions") as cur:
            stats["total_suggestions"] = (await cur.fetchone())["c"]

        async with self.db.execute("SELECT COUNT(*) as c FROM digests") as cur:
            stats["total_digests"] = (await cur.fetchone())["c"]

        # Messages today
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.db.execute(
            "SELECT COUNT(*) as c FROM messages WHERE timestamp >= ?",
            (today.isoformat(),),
        ) as cur:
            stats["messages_today"] = (await cur.fetchone())["c"]

        return stats


# Global store instance
_store: Optional[Store] = None


async def get_store() -> Store:
    """Get the global store instance, creating if necessary."""
    global _store
    if _store is None:
        _store = Store()
        await _store.connect()
    return _store


async def close_store() -> None:
    """Close the global store instance."""
    global _store
    if _store:
        await _store.close()
        _store = None
