# Telegram Digest Bot

A self-hosted personal assistant that watches your Telegram, learns how you answer questions, and gives you a daily briefing. It also exposes all of this data to Claude Code via an MCP server, so you can ask about your Telegram activity from any project.

## The Big Picture

There are three pieces that work together:

```
                YOUR TELEGRAM
                     │
                     ▼
    ┌────────────────────────────────┐
    │          BOT (long-running)    │
    │                                │
    │  Telethon ──▶ SQLite           │
    │  (listens)    (messages.db)    │
    │                    │           │
    │  Claude AI ◀───────┘           │
    │  (classifies questions,        │
    │   generates digests)           │
    │                    │           │
    │  ChromaDB ◀────────┘           │
    │  (vector embeddings for        │
    │   Q&A similarity search)       │
    │                    │           │
    │  Telegram Bot ◀────┘           │
    │  (sends you digests,           │
    │   suggestions, responds        │
    │   to /commands)                │
    │                                │
    │  Dashboard ──▶ localhost:8000  │
    └────────────────────────────────┘
                     │
              writes to disk
                     │
                     ▼
              ./data/ folder
          (messages.db, chroma/)
                     │
              reads from disk
                     │
                     ▼
    ┌────────────────────────────────┐
    │   MCP SERVER (on-demand)       │
    │                                │
    │  Claude Code spawns this       │
    │  when you ask about Telegram.  │
    │  It reads the DB, returns      │
    │  results, then exits.          │
    └────────────────────────────────┘
```

**The bot** runs 24/7 (via Docker), ingesting messages and building your knowledge base.

**The MCP server** is stateless and read-only. Claude Code starts it automatically when it needs Telegram data, then shuts it down. It doesn't need the bot to be running — it just reads whatever data the bot has already collected.

## What It Actually Does

### 1. Listens to all your Telegram messages
Connects as your user account (not as a bot) via Telethon. Captures every message from every chat — DMs, groups, channels. Stores them in SQLite with full context (who said what, where, when).

### 2. Detects questions directed at you
Every 10 minutes (or every 50 messages), it sends a batch to Claude to classify which messages are questions that need your attention. Looks for @mentions, direct questions, and contextual cues.

### 3. Learns from your answers
When you reply to a detected question, it saves the Q&A pair. These get embedded into ChromaDB for semantic search. If someone asks something similar later, it can suggest your previous answer. Duplicate questions (>95% similarity) get merged automatically.

### 4. Sends you a daily digest
At your configured time (default 7 AM), Claude generates a prioritized summary of the last 24 hours. Chats are ranked by priority level. The digest is sent to you via the Telegram bot.

### 5. Exposes data to Claude Code (MCP)
The MCP server gives Claude Code access to your Telegram data. Ask things like "what did the team discuss yesterday?" or "who's waiting on a response from me?" from any project.

## What You Need

Before you start, have these ready:

1. **Telegram API credentials** — from [my.telegram.org](https://my.telegram.org) (api_id + api_hash)
2. **Telegram Bot** — create one via [@BotFather](https://t.me/botfather) (for sending you digests/notifications)
3. **Anthropic API key** — for Claude (question classification + summarization)
4. **Docker** — [Install Docker Desktop](https://docker.com) if you don't have it

No other API keys are needed. Embeddings run locally via `sentence-transformers`.

## Quick Start

```bash
git clone <repo-url> && cd tg_summary_project
./setup.sh
```

That's it. The script will:

1. Check that Docker is installed and running
2. Prompt you for API keys (writes `.env` with secure permissions)
3. Build the Docker image
4. Run the Telegram auth wizard (sends you a verification code)
5. Generate your `config.yaml`
6. Fix all file/directory permissions
7. Start the service
8. Print the MCP config snippet for Claude Code

At the end you'll see a JSON block — paste it into `~/.claude/.mcp.json` to connect Claude Code.

## Manual Setup (if you prefer)

<details>
<summary>Click to expand</summary>

### 1. Environment variables

```bash
cp .env.example .env
# Fill in your API keys, then lock it down:
chmod 600 .env
```

### 2. Build and run the setup wizard

```bash
docker compose build
docker compose run --rm telegram-digest python -m src.setup
```

### 3. Fix data directory permissions and start

```bash
rm -f data/.lock
docker compose up -d
```

### 4. Verify

```bash
docker compose logs -f        # Check logs
open http://127.0.0.1:8000    # Dashboard
```

</details>

## Running the Bot

```bash
# Start
docker compose up -d

# Stop
docker compose down

# Restart (e.g. after config changes)
docker compose restart

# Rebuild (after code changes)
docker compose build && docker compose up -d

# View logs
docker compose logs -f

# Force an immediate digest
docker compose exec telegram-digest python -m src.main --digest-now
```

## MCP Server (Claude Code Integration)

The MCP server lets Claude Code query your Telegram data. `setup.sh` prints the config at the end, but if you need it manually, add this to `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "tg-summary": {
      "command": "/path/to/tg_summary_project/venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/path/to/tg_summary_project"
    }
  }
}
```

The MCP server requires a local Python venv with dependencies installed:

```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
```

### Available tools

| Tool | What you can ask |
|------|-----------------|
| `search_qa` | "Has anyone asked about X before?" — semantic search over your Q&A knowledge base |
| `search_messages` | "What did people say about Y?" — keyword search over recent messages |
| `get_digest` | "What happened yesterday?" — fetch daily digests |
| `get_pending_mentions` | "Who's waiting on me?" — unanswered @mentions |
| `get_chat_summary` | "What's going on in #engineering?" — raw messages from a specific chat |
| `get_stats` | "How much data do we have?" — message counts, Q&A pairs, etc. |
| `list_chats` | "What chats are being monitored?" — all chats with priorities |
| `list_qa_pairs` | "What's in the knowledge base?" — browse learned Q&A pairs |

The MCP server is read-only and doesn't need the bot to be running. It reads directly from the SQLite database and ChromaDB files on disk.

## Bot Commands

Send these to your bot in Telegram:

| Command | What it does |
|---------|-------------|
| `/stats` | Message counts, Q&A pairs, storage, uptime |
| `/digest` | Generate and send a digest right now |
| `/pending` | Show @mentions you haven't responded to |
| `/suggest` | Same as /pending but with AI-suggested responses |
| `/search <query>` | Search your Q&A knowledge base |
| `/recent <chat>` | Summary of a specific chat's recent activity |
| `/pause` / `/resume` | Stop/start message ingestion |

## Configuration

Everything is in `config.yaml` (generated by setup). Key settings:

```yaml
telegram:
  delivery_chat_id: 123456789     # Your Telegram user ID (where digests get sent)
  username: your_username          # For @mention detection (without the @)

digest:
  schedule: "07:00"               # When to send the daily digest
  timezone: America/New_York      # Your timezone
  lookback_hours: 24              # How far back to look

quiet_hours:
  enabled: true
  start: "22:00"                  # No ingestion or notifications during quiet hours
  end: "08:00"

chats:
  default_priority: 3             # 1=detailed, 3=standard, 5=excluded from digest
  ignore_patterns:                # Chats to skip entirely (glob patterns)
    - "*Alerts*"
    - "*Bot"
```

## Data

Everything lives in `./data/`:

| Path | What | Size |
|------|------|------|
| `messages.db` | SQLite — messages, Q&A pairs, digests, suggestions | ~1 MB |
| `chroma/` | ChromaDB — vector embeddings for Q&A similarity search | ~few MB |
| `telegram.session` | Telethon auth session (persisted login) | tiny |
| `.lock` | Prevents multiple instances from running | tiny |

Messages are auto-deleted after 90 days (configurable via `data_retention.messages_days`).

## CLI Tools

```bash
# Quick look at recent messages
python fetch_today.py
python fetch_today.py --hours 48 --chat "Team Chat"

# See pending mentions
python fetch_today.py --pending

# See learned Q&A pairs
python fetch_today.py --qa

# Get your Telegram user ID
python get_user_id.py
```

## Dashboard

The web dashboard runs at `http://127.0.0.1:8000`. If you set a `DASHBOARD_TOKEN` in `.env` (setup.sh does this automatically), you'll be prompted for credentials — any username works, the password is your token.

## Troubleshooting

**"Telegram session not authorized"** — The session expired. Re-run setup:
```bash
docker compose run --rm telegram-digest python -m src.setup
docker compose restart
```

**"Another instance is already running"** — Stale lock file:
```bash
rm data/.lock
docker compose restart
```

**Permission errors in container** — Fix data directory ownership:
```bash
sudo chown -R 1000:1000 data/
docker compose restart
```

**No questions being detected** — The classifier runs in batches (every 10 min or 50 messages). Check the logs for `classification_batch_processed`.

**MCP server not showing in Claude Code** — Restart Claude Code after adding `.mcp.json`. Check with `/mcp` in Claude Code to see connected servers.
