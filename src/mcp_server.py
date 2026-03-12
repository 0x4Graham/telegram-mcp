"""MCP server exposing Telegram digest data to Claude Code.

Implements the MCP (Model Context Protocol) JSON-RPC over stdio directly,
without requiring the mcp SDK (which needs Python 3.10+).
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Suppress all logging to stderr — MCP stdio must be clean
logging.disable(logging.CRITICAL)

import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
)

from .config import load_config, get_config
from .store import Store
from .vectors import VectorStore


# ==================== Protocol Constants ====================

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "tg-summary"
SERVER_VERSION = "0.1.0"


# ==================== Tool Definitions ====================

TOOLS = [
    {
        "name": "search_qa",
        "description": (
            "Semantic search over the Q&A knowledge base. Finds previously answered "
            "questions similar to the query. Returns matches with similarity scores."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The question or topic to search for",
                },
                "threshold": {
                    "type": "number",
                    "description": "Minimum similarity score (0-1). Default 0.5",
                    "default": 0.5,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return. Default 5",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_messages",
        "description": (
            "Search recent Telegram messages by keyword. Optionally filter by "
            "chat name or time range."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Text to search for in messages",
                },
                "chat_name": {
                    "type": "string",
                    "description": "Filter to messages from this chat (partial match)",
                },
                "hours": {
                    "type": "integer",
                    "description": "Look back this many hours. Default 24",
                    "default": 24,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results. Default 50",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_digest",
        "description": (
            "Get a previously generated daily digest. These are pre-generated summaries "
            "that may be hours or days old. For current/live data about what happened "
            "recently, use get_daily_messages instead. Only use this tool when the user "
            "specifically asks for a past digest or you need a pre-written summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format. Omit for latest.",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of recent digests to return. Default 1",
                    "default": 1,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_pending_mentions",
        "description": (
            "Get unanswered questions/mentions directed at you across all chats."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Look back this many hours. Default 48",
                    "default": 48,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_chat_summary",
        "description": (
            "Get recent messages from a specific chat, grouped for review. "
            "Does NOT call the LLM - just returns raw messages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_name": {
                    "type": "string",
                    "description": "Chat name to look up (partial match)",
                },
                "hours": {
                    "type": "integer",
                    "description": "Look back this many hours. Default 24",
                    "default": 24,
                },
            },
            "required": ["chat_name"],
        },
    },
    {
        "name": "get_daily_messages",
        "description": (
            "Get all recent Telegram messages from the last N hours grouped by chat. "
            "This is the PRIMARY tool for answering 'what happened' questions — it returns "
            "live data directly from the database. Use this when the user asks about recent "
            "activity, what happened today, or wants a summary of the last 24 hours. "
            "Respects ignore_patterns from config. Returns raw messages for you to summarize."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Look back this many hours. Default 24",
                    "default": 24,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_stats",
        "description": "Get system statistics: message counts, Q&A pairs, chats, digests.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_chats",
        "description": "List all monitored Telegram chats with their priority levels and last activity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "active_hours": {
                    "type": "integer",
                    "description": "Only show chats active in the last N hours. Omit for all.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_qa_pairs",
        "description": "List all Q&A pairs in the knowledge base, optionally filtered by chat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_name": {
                    "type": "string",
                    "description": "Filter to Q&A from this chat (partial match)",
                },
            },
            "required": [],
        },
    },
]

RESOURCES = [
    {
        "uri": "telegram://stats",
        "name": "Telegram Digest Stats",
        "description": "Current system statistics",
        "mimeType": "application/json",
    },
    {
        "uri": "telegram://digest/latest",
        "name": "Latest Digest",
        "description": "Most recent daily digest content",
        "mimeType": "text/plain",
    },
]


# ==================== Argument Helpers ====================


def _int_arg(arguments: Dict, key: str, default: int, min_val: int = 1, max_val: int = 1000) -> int:
    """Safely extract and clamp an integer argument."""
    try:
        val = int(arguments.get(key, default))
    except (TypeError, ValueError):
        val = default
    return max(min_val, min(val, max_val))


def _float_arg(arguments: Dict, key: str, default: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Safely extract and clamp a float argument."""
    try:
        val = float(arguments.get(key, default))
    except (TypeError, ValueError):
        val = default
    return max(min_val, min(val, max_val))


# ==================== Tool Handlers ====================


async def handle_tool_call(
    name: str, arguments: Dict[str, Any], store: Store, vectors: VectorStore
) -> List[Dict]:
    """Execute a tool and return MCP content blocks."""

    if name == "search_qa":
        query = arguments.get("query", "")
        if not query:
            return _text_content({"error": "query is required"})
        threshold = _float_arg(arguments, "threshold", 0.5)
        limit = _int_arg(arguments, "limit", 5, max_val=50)
        matches = vectors.query_similar(query, threshold=threshold, limit=limit)
        if not matches:
            return _text_content({"results": [], "message": "No matching Q&A pairs found."})
        return _text_content({"results": matches, "count": len(matches)})

    elif name == "search_messages":
        hours = _int_arg(arguments, "hours", 24, max_val=720)
        limit = _int_arg(arguments, "limit", 50, max_val=500)
        since = datetime.now() - timedelta(hours=hours)
        messages = await store.get_messages_since(since)

        keyword = arguments.get("keyword")
        if keyword:
            keyword_lower = keyword.lower()
            messages = [
                m for m in messages
                if m.get("text") and keyword_lower in m["text"].lower()
            ]

        chat_name = arguments.get("chat_name")
        if chat_name:
            chat_lower = chat_name.lower()
            messages = [
                m for m in messages
                if chat_lower in m.get("chat_name", "").lower()
            ]

        messages = messages[-limit:]
        results = [
            {
                "chat": m["chat_name"],
                "sender": m["sender_name"],
                "text": m["text"],
                "timestamp": m["timestamp"],
                "is_from_me": bool(m["is_from_me"]),
                "is_question": m.get("is_question"),
            }
            for m in messages
            if m.get("text")
        ]
        return _text_content({"results": results, "count": len(results)})

    elif name == "get_digest":
        count = _int_arg(arguments, "count", 1, max_val=30)
        date_str = arguments.get("date")

        digests = await store.get_recent_digests(limit=count if not date_str else 30)

        if date_str:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
            digests = [
                d for d in digests
                if datetime.fromisoformat(d["period_start"]).date() == target
                or datetime.fromisoformat(d["period_end"]).date() == target
            ]

        results = [
            {
                "generated_at": d["generated_at"],
                "period": "{} - {}".format(d["period_start"], d["period_end"]),
                "content": d["content"],
                "message_count": d["message_count"],
                "chat_count": d["chat_count"],
            }
            for d in digests[:count]
        ]
        if not results:
            return _text_content({"results": [], "message": "No digests found."})

        # Warn if the most recent digest is stale
        freshness_note = None
        if results and not date_str:
            latest_end = digests[0].get("period_end", "")
            if latest_end:
                try:
                    end_dt = datetime.fromisoformat(latest_end)
                    if end_dt.tzinfo:
                        end_dt = end_dt.replace(tzinfo=None)
                    age_hours = (datetime.now() - end_dt).total_seconds() / 3600
                    if age_hours > 36:
                        freshness_note = (
                            "WARNING: This digest is {:.0f} hours old (from {}). "
                            "It does NOT reflect recent activity. Use get_daily_messages "
                            "for current data."
                        ).format(age_hours, latest_end[:10])
                except (ValueError, TypeError):
                    pass

        response = {"results": results}
        if freshness_note:
            response["freshness_warning"] = freshness_note
        return _text_content(response)

    elif name == "get_pending_mentions":
        hours = _int_arg(arguments, "hours", 48, max_val=720)
        config = get_config()
        since = datetime.now() - timedelta(hours=hours)
        mentions = await store.get_pending_mentions(
            username=config.telegram.username,
            since=since,
            ignore_patterns=config.chats.ignore_patterns,
        )
        results = [
            {
                "chat": m["chat_name"],
                "sender": m["sender_name"],
                "text": m["text"],
                "timestamp": m["timestamp"],
            }
            for m in mentions
        ]
        return _text_content({"results": results, "count": len(results)})

    elif name == "get_chat_summary":
        chat_name = arguments.get("chat_name", "")
        if not chat_name:
            return _text_content({"error": "chat_name is required"})
        hours = _int_arg(arguments, "hours", 24, max_val=720)
        since = datetime.now() - timedelta(hours=hours)

        all_chats = await store.get_all_chats()
        chat_lower = chat_name.lower()
        matched = [c for c in all_chats if chat_lower in c["name"].lower()]

        if not matched:
            return _text_content({"error": "No chat found matching '{}'".format(chat_name)})

        chat = matched[0]
        messages = await store.get_messages_since(since, chat_id=chat["chat_id"])
        results = [
            {
                "sender": m["sender_name"],
                "text": m["text"],
                "timestamp": m["timestamp"],
                "is_from_me": bool(m["is_from_me"]),
            }
            for m in messages
            if m.get("text")
        ]
        return _text_content({
            "chat": chat["name"],
            "chat_id": chat["chat_id"],
            "priority": chat["priority"],
            "messages": results,
            "count": len(results),
        })

    elif name == "get_daily_messages":
        import fnmatch
        hours = _int_arg(arguments, "hours", 24, max_val=720)
        config = get_config()
        since = datetime.now() - timedelta(hours=hours)
        ignore_patterns = config.chats.ignore_patterns

        messages_by_chat = await store.get_messages_for_digest(since, datetime.now())

        chats_data = []
        total_msgs = 0
        for chat_id, messages in messages_by_chat.items():
            if not messages:
                continue
            chat_name = messages[0]["chat_name"]
            # Apply ignore patterns
            ignored = False
            for pattern in ignore_patterns:
                if fnmatch.fnmatch(chat_name.lower(), pattern.lower()):
                    ignored = True
                    break
            if ignored:
                continue

            chat_msgs = [
                {
                    "sender": m["sender_name"],
                    "text": m["text"],
                    "timestamp": m["timestamp"],
                    "is_from_me": bool(m["is_from_me"]),
                }
                for m in messages
                if m.get("text")
            ]
            if chat_msgs:
                chats_data.append({
                    "chat_name": chat_name,
                    "message_count": len(chat_msgs),
                    "messages": chat_msgs,
                })
                total_msgs += len(chat_msgs)

        # Sort by message count descending
        chats_data.sort(key=lambda c: c["message_count"], reverse=True)

        return _text_content({
            "period_hours": hours,
            "total_messages": total_msgs,
            "total_chats": len(chats_data),
            "chats": chats_data,
        })

    elif name == "get_stats":
        stats = await store.get_stats()
        stats["qa_pairs_in_vector_store"] = vectors.count()
        return _text_content(stats)

    elif name == "list_chats":
        chats = await store.get_all_chats()
        active_hours = arguments.get("active_hours")
        if active_hours:
            cutoff = datetime.now() - timedelta(hours=active_hours)
            cutoff_iso = cutoff.isoformat()
            chats = [
                c for c in chats
                if c.get("last_message_at")
                and c["last_message_at"] >= cutoff_iso
            ]
        results = [
            {
                "name": c["name"],
                "chat_id": c["chat_id"],
                "type": c["type"],
                "priority": c["priority"],
                "last_message_at": c.get("last_message_at"),
            }
            for c in chats
        ]
        return _text_content({"chats": results, "count": len(results)})

    elif name == "list_qa_pairs":
        pairs = vectors.get_all()
        chat_name = arguments.get("chat_name")
        if chat_name:
            chat_lower = chat_name.lower()
            pairs = [p for p in pairs if chat_lower in p.get("chat_name", "").lower()]
        return _text_content({"qa_pairs": pairs, "count": len(pairs)})

    else:
        return _text_content({"error": "Unknown tool: {}".format(name)})


async def handle_resource_read(
    uri: str, store: Store, vectors: VectorStore
) -> List[Dict]:
    """Read a resource and return MCP content blocks."""
    if uri == "telegram://stats":
        stats = await store.get_stats()
        stats["qa_pairs_in_vector_store"] = vectors.count()
        return [{"uri": uri, "mimeType": "application/json",
                 "text": json.dumps(stats, indent=2, default=str)}]

    elif uri == "telegram://digest/latest":
        digests = await store.get_recent_digests(limit=1)
        text = digests[0]["content"] if digests else "No digests available."
        return [{"uri": uri, "mimeType": "text/plain", "text": text}]

    raise ValueError("Unknown resource: {}".format(uri))


def _text_content(data: Any) -> List[Dict]:
    """Return a list with a single text content block."""
    return [{"type": "text", "text": json.dumps(data, indent=2, default=str)}]


# ==================== JSON-RPC over stdio ====================


async def handle_request(
    method: str,
    params: Optional[Dict],
    store: Store,
    vectors: VectorStore,
) -> Any:
    """Route a JSON-RPC request to the appropriate handler."""

    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
                "resources": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        }

    elif method == "notifications/initialized":
        # Client acknowledgement, no response needed
        return None

    elif method == "tools/list":
        return {"tools": TOOLS}

    elif method == "tools/call":
        name = params.get("name", "") if params else ""
        arguments = params.get("arguments", {}) if params else {}
        content = await handle_tool_call(name, arguments, store, vectors)
        return {"content": content}

    elif method == "resources/list":
        return {"resources": RESOURCES}

    elif method == "resources/read":
        uri = params.get("uri", "") if params else ""
        contents = await handle_resource_read(uri, store, vectors)
        return {"contents": contents}

    elif method == "ping":
        return {}

    else:
        raise ValueError("Method not found: {}".format(method))


def _make_response(id: Any, result: Any) -> Dict:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}


def _make_error(id: Any, code: int, message: str) -> Dict:
    """Build a JSON-RPC error response."""
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "error": {"code": code, "message": message}}


async def run_stdio(store: Store, vectors: VectorStore) -> None:
    """Main loop: read JSON-RPC messages from stdin, write responses to stdout.

    Uses a thread-based reader for stdin compatibility across platforms and
    pipe scenarios (Python 3.9 asyncio doesn't handle piped stdin well).
    """
    loop = asyncio.get_event_loop()

    # Read lines from stdin in a thread to avoid async pipe issues
    def _read_line() -> Optional[str]:
        try:
            line = sys.stdin.readline()
            return line if line else None
        except (EOFError, OSError):
            return None

    def _write(msg: Dict) -> None:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    while True:
        line = await loop.run_in_executor(None, _read_line)
        if line is None:
            break  # EOF

        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params")

        # Notifications (no id) don't get responses
        is_notification = msg_id is None

        try:
            result = await handle_request(method, params, store, vectors)
            if not is_notification and result is not None:
                _write(_make_response(msg_id, result))
        except ValueError as e:
            # Expected errors (unknown method, missing params)
            if not is_notification:
                _write(_make_error(msg_id, -32602, str(e)))
        except Exception:
            # Unexpected errors — don't leak internals
            if not is_notification:
                _write(_make_error(msg_id, -32603, "Internal server error"))


# ==================== Entry Point ====================


async def main() -> None:
    # Load config (also loads .env)
    load_config()

    # Initialize data stores
    store = Store()
    await store.connect()

    vectors = VectorStore()
    vectors.connect()

    try:
        await run_stdio(store, vectors)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
