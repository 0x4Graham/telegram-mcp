# Telegram Digest

Local app that reads all your Telegram messages and delivers a daily AI-powered digest with answer suggestions for repeated questions.

## Quick Start

```bash
python -m src.setup   # Telegram auth + config generation + optional backfill
python -m src.main    # Start service
```

## Architecture

```
Telethon (user client) → Ingester → SQLite + ChromaDB
                                         ↓
Bot (delivery) ← Digest ← Summarizer (Claude API)
      ↑                        ↓
      └──── Suggester ←────────┘
                ↓
         Dashboard (FastAPI + HTMX)
```

**Two Telegram connections:**
- Telethon (MTProto): Reads messages from all chats using your account
- Bot API: Delivers digest and suggestions to you privately

**Storage:**
- SQLite: Raw messages, chat metadata, Q&A pair references
- ChromaDB: Vector embeddings of questions for similarity search

**Web Interface:**
- FastAPI + HTMX dashboard on port 8000 (no auth, local access only)

## Core Components

| Component | File | Purpose |
|-----------|------|---------|
| Ingester | `src/ingester.py` | Listens to Telegram, stores messages |
| Store | `src/store.py` | SQLite operations + 90-day retention |
| Vectors | `src/vectors.py` | ChromaDB operations + deduplication |
| Summarizer | `src/summarizer.py` | Daily digest generation via Claude |
| Suggester | `src/suggester.py` | Q&A matching + answer suggestions |
| Delivery | `src/delivery.py` | Bot output to user |
| Embeddings | `src/embeddings.py` | Voyage AI embedding client |
| Scheduler | `src/scheduler.py` | APScheduler job management (DST-aware) |
| Config | `src/config.py` | YAML + env var loading |
| Dashboard | `src/dashboard.py` | FastAPI + HTMX web interface |
| Prompts | `src/prompts.py` | All LLM prompts (production-ready) |

## Data Flow

### Message Ingestion
```
Telegram event → parse message → store in SQLite
    → batch question classification (50 msgs or 10 min)
    → if question: check for similar Q&A (threshold 0.85)
    → if similar found & not typing & not in cooldown:
        → if burst (same question multiple times): aggregate notify
        → else: send suggestion via bot (answer only, top 3 matches by similarity)
```

### Q&A Extraction
```
Question detected → find replies within 15 minutes (or explicit reply_to)
    → concatenate all replies
    → synthesize with Claude (preserve attribution: "According to Alice...")
    → check for duplicate questions (>0.95 similarity)
    → if duplicate: merge answers into existing Q&A
    → else: embed question → store in ChromaDB
```

### Suggestion Flow
```
Similar Q&A found (0.85+ threshold)
    → check cooldowns (per chat + Q&A pair, 30 min)
    → check if user is typing (suppress if so)
    → check for burst (same question asked multiple times recently)
    → if burst: send aggregate notification ("Asked 5 times in: Chat A, Chat B...")
    → else: send private preview via bot (answer text only)
    → user manually copies and pastes to target chat
```

### Implicit Learning
```
You reply to a question differently than suggested
    → system detects your new answer
    → replaces old Q&A pair answer with your new response
    → re-embeds if question significantly changed
```

### Daily Digest
```
Scheduler triggers at configured time (wall clock, DST-aware)
    → check quiet hours (if active: skip entirely)
    → fetch messages from last 24h
    → group by chat, order by priority (1=first, 5=excluded)
    → for priority 1-4: summarize each chat (Claude)
    → aggregate into digest with detailed metrics
    → send via bot (Telegram Markdown format)
    → on API failure: retry every 15 min for up to 2 hours
    → on partial failure: discard and retry full digest
```

### Catch-up on Restart
```
System starts → check last processed timestamp
    → fetch all messages from last 24 hours
    → deduplicate against stored messages
    → process normally
```

## Database Schema

### SQLite (`data/messages.db`)

```sql
-- Core tables
messages (id, telegram_id, chat_id, chat_name, chat_type, sender_id,
          sender_name, text, timestamp, reply_to_id, is_from_me,
          has_media, media_type, is_question, processed, created_at)

chats (id, chat_id, name, type, priority DEFAULT 3, last_message_at,
       first_seen_at)

qa_pairs (id, question_message_id, answer_message_id, question_text,
          answer_text, chat_id, chat_name, question_from, answered_at,
          times_suggested, last_suggested_at, created_at)

suggestions (id, qa_pair_id, target_chat_id, target_message_id,
             suggested_at, similarity_score)

digests (id, generated_at, period_start, period_end, content,
         message_count, chat_count, metadata_json)

-- Classification queue for batch processing
classification_queue (id, message_id, queued_at)

-- Lock table for single instance
app_lock (id, locked_at, pid)
```

### ChromaDB (`data/chroma/`)

Collection: `qa_pairs`
- Document: question text
- Embedding: voyage-3-lite vector
- Metadata: `{answer, chat_id, chat_name, timestamp, qa_pair_id}`

### Data Retention

- **Raw messages**: Rolling 90-day retention, auto-deleted on schedule
- **Q&A pairs**: Deleted when source messages age out
- **Digests**: Kept indefinitely for dashboard history
- **Suggestions**: Kept indefinitely for dashboard history

## Priority System

Numeric priority 1-5 affects digest order and detail level:

| Priority | Digest Position | Detail Level |
|----------|-----------------|--------------|
| 1 | First | Full detailed summary |
| 2 | After 1s | Detailed summary |
| 3 (default) | After 2s | Standard summary |
| 4 | After 3s | Brief bullet points |
| 5 | Excluded | Not included in digest |

New chats default to priority 3. Configure via `config.yaml`.

## Bot Commands

| Command | Description |
|---------|-------------|
| `/help` | List all commands with one-line descriptions |
| `/status` | Check if system is running, show uptime |
| `/digest` | Force generate and send digest immediately |
| `/stats` | Detailed breakdown: messages, Q&A pairs, per-chat counts, storage, uptime |
| `/pause` | Enter quiet mode (stop ingestion and suggestions) |
| `/resume` | Exit quiet mode |
| `/search <query>` | Search Q&A knowledge base, show best match with conversation context |
| `/recent <chat>` | Generate 24h mini-summary for specified chat (fuzzy name match) |

## Dashboard

FastAPI + HTMX web interface at `http://localhost:8000`

**Features:**
- System stats (messages, Q&A pairs, chats, storage usage, uptime)
- Past digests with full content
- Suggestion history (which Q&A was suggested when, to which chat)
- Real-time status indicators

**No authentication** - designed for local access only on trusted network.

## Key Behaviors

### Message Handling
- **Text only**: Media messages (voice, images, documents, stickers) are ignored
- **Edits/deletions**: Ignored once ingested (messages are immutable in system)
- **Forwarded messages**: Treated like any other message
- **Long messages**: Passed in full to Claude for summarization
- **Own messages**: Stored and included for conversation context in summaries

### Chat Types
- **Groups, supergroups, channels, DMs**: All treated equally
- **New chats**: Automatically included with default priority 3

### Suggestion Suppression
- **While typing**: Suggestions suppressed when you're actively typing in Telegram
- **Cooldown**: Per chat + Q&A pair cooldown of 30 minutes
- **Quiet hours**: Full pause of ingestion and suggestions during configured hours
- **Threshold**: Skip entirely if no match above 0.85 similarity

### Burst Detection
When the same question is asked multiple times within a short period:
- Single aggregate notification: "This question asked 5 times in: Chat A, Chat B, Chat C..."
- Instead of 5 separate suggestion notifications

## Key Conventions

### Async Everything
All I/O operations are async. Use `asyncio` throughout.

```python
# Good
async def get_messages(db: aiosqlite.Connection, since: datetime):
    async with db.execute("SELECT ...", [since]) as cursor:
        return await cursor.fetchall()

# Bad - blocks event loop
def get_messages(db, since):
    return db.execute("SELECT ...").fetchall()
```

### Structured Logging
Use `structlog` to stdout only.

```python
import structlog
log = structlog.get_logger()

async def process_message(msg):
    log = log.bind(chat_id=msg.chat_id, msg_id=msg.id)
    log.info("processing_message")
    # ...
    log.info("message_processed", is_question=is_q)
```

### Error Handling
Telegram connection can drop. Always handle reconnection. Log errors silently (no bot notifications).

```python
async def run_ingester(client: TelegramClient):
    while True:
        try:
            await client.run_until_disconnected()
        except ConnectionError:
            log.warning("connection_lost", retry_in=5)
            await asyncio.sleep(5)
```

### Single Instance Lock
Prevent multiple instances with lock file.

```python
LOCK_FILE = "data/.lock"

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE) as f:
            pid = int(f.read().strip())
        if is_process_running(pid):
            raise RuntimeError(f"Another instance running (PID {pid})")
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
```

### Claude API Calls
Use structured prompts. Always set `max_tokens`.

```python
async def summarize_chat(messages: list[Message]) -> str:
    response = await anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": CHAT_SUMMARY_PROMPT.format(
                chat_name=messages[0].chat_name,
                messages=format_messages(messages)
            )
        }]
    )
    return response.content[0].text
```

### Embeddings
Batch when possible. Voyage has 128 item batch limit.

```python
async def embed_questions(questions: list[str]) -> list[list[float]]:
    # Batch in chunks of 100
    embeddings = []
    for chunk in chunked(questions, 100):
        result = await voyage.embed(chunk, model="voyage-3-lite")
        embeddings.extend(result.embeddings)
    return embeddings
```

### Q&A Deduplication
Merge similar questions to avoid index bloat.

```python
async def store_qa_pair(question: str, answer: str, metadata: dict):
    # Check for near-duplicate (>0.95 similarity)
    existing = await vector_store.query(question, threshold=0.95, limit=1)
    if existing:
        # Merge: append new answer to existing
        existing_qa = existing[0]
        merged_answer = await synthesize_answers(
            [existing_qa.answer, answer],
            preserve_attribution=True
        )
        await vector_store.update(existing_qa.id, answer=merged_answer)
    else:
        await vector_store.add(question, answer, metadata)
```

## Config Structure

```yaml
# config.yaml (auto-generated by setup)
telegram:
  api_id: ${TELEGRAM_API_ID}
  api_hash: ${TELEGRAM_API_HASH}
  phone: "+41..."
  bot_token: ${TELEGRAM_BOT_TOKEN}
  delivery_chat_id: 123456789  # Your user ID for bot delivery

llm:
  model: claude-sonnet-4-20250514

embeddings:
  model: voyage-3-lite

digest:
  schedule: "07:00"
  timezone: "Europe/Zurich"
  lookback_hours: 24
  target_length: 2000  # words, detailed

quiet_hours:
  enabled: true
  start: "22:00"
  end: "08:00"
  # During quiet hours: full pause (no ingestion, no suggestions)

answer_suggester:
  enabled: true
  similarity_threshold: 0.85
  cooldown_minutes: 30
  suppress_while_typing: true
  show_top_matches: 3

question_detection:
  batch_size: 50
  max_wait_minutes: 10

data_retention:
  messages_days: 90
  cleanup_schedule: "03:00"  # Run cleanup at 3 AM

chats:
  default_priority: 3
  priorities:
    - chat_id: -100123456789
      priority: 1
    - chat_id: -100987654321
      priority: 5  # Excluded from digest

dashboard:
  enabled: true
  port: 8000
```

Environment variables (`.env`):
```
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_BOT_TOKEN=
ANTHROPIC_API_KEY=
VOYAGE_API_KEY=
```

## Setup Flow

```bash
python -m src.setup
```

1. **Check for existing config**
   - If `.env` exists, load it
   - Otherwise, prompt for API keys and create `.env`

2. **Telegram authentication**
   - Connect with Telethon
   - Phone number + code + optional 2FA
   - Save session file

3. **Bot verification**
   - Verify bot token works
   - Get your user ID for delivery

4. **Chat selection for backfill**
   - Interactive paginated picker (CLI)
   - Show all chats with recent activity
   - Select chats to backfill (30 days of history)
   - Selected chats get default priority 3

5. **Generate config.yaml**
   - Create with defaults
   - Include selected chat IDs

6. **Run backfill**
   - Fetch 30 days of messages from selected chats
   - Process and store
   - Extract initial Q&A pairs

## File Structure

```
telegram-digest/
├── src/
│   ├── __init__.py
│   ├── main.py           # Entry point, orchestrates components
│   ├── setup.py          # First-run setup wizard
│   ├── ingester.py       # Telethon message listener
│   ├── store.py          # SQLite operations
│   ├── vectors.py        # ChromaDB operations
│   ├── summarizer.py     # Digest generation
│   ├── suggester.py      # Q&A matching engine
│   ├── classifier.py     # Batch question classification
│   ├── delivery.py       # Bot message sending
│   ├── embeddings.py     # Voyage client wrapper
│   ├── scheduler.py      # APScheduler setup
│   ├── config.py         # Config loading
│   ├── prompts.py        # All LLM prompts
│   └── dashboard/
│       ├── __init__.py
│       ├── app.py        # FastAPI application
│       ├── routes.py     # API routes
│       └── templates/    # HTMX templates
├── data/
│   ├── messages.db
│   ├── chroma/
│   └── .lock
├── config.yaml
├── .env
├── requirements.txt
└── SPEC.md
```

## Prompts

All prompts live in `src/prompts.py`. Full production-ready templates:

### QUESTION_DETECTION_PROMPT
```
You are analyzing Telegram messages to identify questions that might benefit from a suggested answer.

A message is a QUESTION if someone is asking for information, help, clarification, or guidance that you (the recipient) might have answered before.

Messages to classify:
{messages}

For each message, respond with JSON:
{{"message_id": <id>, "is_question": true/false, "confidence": 0.0-1.0}}

Only mark as question if:
- It's genuinely seeking information (not rhetorical)
- It's the type of question that gets asked repeatedly
- Someone knowledgeable could provide a useful answer

Do NOT mark as questions:
- Greetings ("How are you?")
- Rhetorical questions
- Questions that are highly context-specific and unlikely to recur
- Questions where the answer changes frequently

Return a JSON array of classifications.
```

### CHAT_SUMMARY_PROMPT
```
Summarize the following conversation from "{chat_name}" for a daily digest.

Messages (chronological order):
{messages}

Write a {detail_level} summary that captures:
- Key topics discussed
- Important decisions or conclusions
- Notable questions raised
- Action items mentioned (if any)

Style guidelines:
- Use present tense ("The team discusses..." not "The team discussed...")
- Be specific about who said what when relevant
- Include concrete details, not vague generalities
- For {detail_level}:
  - "detailed": 200-300 words, comprehensive coverage
  - "standard": 100-150 words, main points
  - "brief": 2-3 bullet points only

Do not include any preamble. Start directly with the summary content.
```

### DIGEST_AGGREGATE_PROMPT
```
Compile these chat summaries into a cohesive daily digest.

Chat summaries (ordered by priority):
{summaries}

Statistics for this period:
- Total messages: {message_count}
- Active chats: {chat_count}
- Time period: {period_start} to {period_end}

Create a digest that:
1. Opens with a 1-2 sentence overview of the day
2. Presents each chat's summary under a clear header
3. Ends with the statistics section

Format for Telegram (use Markdown):
- **Bold** for chat names and emphasis
- `code` for technical terms
- Bullet points for lists

Target length: ~{target_length} words total.
Keep the digest scannable - someone should get the key points in 2 minutes.
```

### ANSWER_SYNTHESIS_PROMPT
```
Synthesize these multiple replies into a single coherent answer.

Original question: "{question}"

Replies to synthesize:
{replies}

Requirements:
- Create one flowing answer that captures all key information
- Preserve attribution: use phrases like "According to Alice..." or "Bob notes that..."
- If replies contradict, present both perspectives with attribution
- Remove redundancy while keeping all unique information
- Maintain the original tone and technical accuracy

Output only the synthesized answer, no preamble.
```

### ANSWER_ADAPTATION_PROMPT
```
You previously answered a similar question with this response:
"{previous_answer}"

The original question was: "{original_question}"

Now someone is asking: "{new_question}"

In chat: {chat_name}

If the previous answer is still applicable:
- Return it, possibly with minor adjustments for context
- Keep the same level of detail and tone

If the previous answer needs significant changes:
- Adapt it while preserving the core information
- Note if important context might be missing

Output only the adapted answer, ready to send.
```

## CLI Flags

```bash
# Normal operation
python -m src.main

# Force digest immediately
python -m src.main --digest-now

# Rebuild Q&A index from stored messages
python -m src.setup --rebuild-qa

# Test question matching
python -m src.suggester --test "How do Safe transactions work?"
```

## Startup Sequence

Components initialize in parallel, with internal dependency handling:

```python
async def main():
    acquire_lock()  # Exit if another instance running

    # Parallel initialization
    db, vectors, telethon, bot, dashboard = await asyncio.gather(
        init_database(),
        init_vectors(),
        init_telethon(),
        init_bot(),
        init_dashboard(),
    )

    # Catch-up: fetch last 24h of messages
    await catch_up_messages(telethon, db, hours=24)

    # Start scheduler (DST-aware wall clock time)
    scheduler = setup_scheduler(config)

    # Run all services
    await asyncio.gather(
        run_ingester(telethon, db, vectors),
        run_bot(bot),
        run_dashboard(dashboard),
        scheduler.start(),
    )
```

## Shutdown Sequence

On SIGTERM or Ctrl+C:

```python
async def shutdown():
    log.info("shutdown_initiated")

    # Stop accepting new events
    await ingester.stop()

    # Gracefully close connections
    await telethon.disconnect()
    await bot.close()
    await dashboard.shutdown()
    await db.close()

    # Release lock
    release_lock()

    log.info("shutdown_complete")
```

## API Rate Limits

| Service | Limit | Handling |
|---------|-------|----------|
| Telegram | 30 msgs/sec | Built into Telethon |
| Claude | Tier-dependent | Retry with exponential backoff (15 min intervals, 2 hour max) |
| Voyage | 300 RPM | Batch + backoff |
| ChromaDB | Local, unlimited | N/A |

## Dependencies

```
telethon>=1.34.0
python-telegram-bot>=21.0
anthropic>=0.40.0
chromadb>=0.5.0
voyageai>=0.3.0
aiosqlite>=0.19.0
apscheduler>=3.10.0
pyyaml>=6.0
python-dotenv>=1.0.0
structlog>=24.0.0
httpx>=0.27.0
fastapi>=0.109.0
uvicorn>=0.27.0
jinja2>=3.1.0
```

## Testing

```bash
pytest tests/ -v
pytest tests/test_suggester.py -k "test_similarity"
```

Key test scenarios:
- Question detection accuracy (batch classification)
- Q&A pair extraction from conversation threads
- Similarity threshold behavior (0.85 cutoff)
- Deduplication logic (0.95 merge threshold)
- Answer synthesis with attribution
- Reconnection handling
- Rate limiting and retry behavior
- Quiet hours enforcement
- Burst detection and aggregation

## Known Limitations

- Telethon session can expire after ~30 days of inactivity
- ChromaDB memory usage grows with collection size (~1GB per 500k vectors)
- Bot inline buttons have 64-byte callback data limit
- Text-only: no voice transcription or image analysis
- English only: no multi-language support
- Single user: designed for personal use, not multi-tenant

## Future Enhancements

- [ ] Voice message transcription (Whisper)
- [ ] Image description in digest (Claude vision)
- [ ] Multi-language question matching
- [ ] Export Q&A to Notion/Obsidian
- [ ] Telegram login for dashboard auth
- [ ] Per-chat suggestion rules
