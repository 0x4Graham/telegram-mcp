#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  Telegram Digest — One-command setup
#
#  Usage:  ./setup.sh
#
#  What it does:
#    1. Checks prerequisites (Docker, git)
#    2. Prompts for API keys → writes .env (mode 600)
#    3. Builds the Docker image
#    4. Runs the interactive Telegram auth + config wizard
#    5. Fixes data directory permissions for the container
#    6. Starts the service
#    7. Prints the MCP config snippet for Claude Code
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Colors (disabled if not a terminal)
if [ -t 1 ]; then
  BOLD='\033[1m'
  GREEN='\033[0;32m'
  YELLOW='\033[0;33m'
  RED='\033[0;31m'
  RESET='\033[0m'
else
  BOLD='' GREEN='' YELLOW='' RED='' RESET=''
fi

info()  { echo -e "${GREEN}[+]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[!]${RESET} $*"; }
error() { echo -e "${RED}[x]${RESET} $*"; exit 1; }
header() {
  echo ""
  echo -e "${BOLD}══════════════════════════════════════════════════${RESET}"
  echo -e "${BOLD}  $*${RESET}"
  echo -e "${BOLD}══════════════════════════════════════════════════${RESET}"
}

prompt_value() {
  local message="$1"
  local default="${2:-}"
  local value
  if [ -n "$default" ]; then
    read -rp "$message [$default]: " value
    echo "${value:-$default}"
  else
    while [ -z "${value:-}" ]; do
      read -rp "$message: " value
    done
    echo "$value"
  fi
}

# ──────────────────────────────────────────────────────────────
#  Step 0: Prerequisites
# ──────────────────────────────────────────────────────────────
header "Checking prerequisites"

command -v docker >/dev/null 2>&1 || error "Docker is not installed. Install it from https://docker.com"
docker info >/dev/null 2>&1     || error "Docker daemon is not running. Start Docker Desktop and try again."

# Check for docker compose (v2 plugin or standalone)
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  error "Docker Compose is not installed."
fi

info "Docker: $(docker --version | head -1)"
info "Compose: $($COMPOSE version | head -1)"

# ──────────────────────────────────────────────────────────────
#  Step 1: API keys → .env
# ──────────────────────────────────────────────────────────────
header "API credentials"

ENV_FILE="$SCRIPT_DIR/.env"

# Load existing values if present
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE" 2>/dev/null || true
fi

EXISTING_API_ID="${TELEGRAM_API_ID:-}"
EXISTING_API_HASH="${TELEGRAM_API_HASH:-}"
EXISTING_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
EXISTING_ANTHROPIC="${ANTHROPIC_API_KEY:-}"
EXISTING_VOYAGE="${VOYAGE_API_KEY:-}"

if [ -n "$EXISTING_API_ID" ] && [ -n "$EXISTING_API_HASH" ] && \
   [ -n "$EXISTING_BOT_TOKEN" ] && [ -n "$EXISTING_ANTHROPIC" ]; then
  info "Found existing credentials in .env"
  REUSE=$(prompt_value "Use existing credentials? (y/n)" "y")
  if [ "${REUSE,,}" = "y" ]; then
    info "Keeping existing .env"
  else
    EXISTING_API_ID="" EXISTING_API_HASH="" EXISTING_BOT_TOKEN="" EXISTING_ANTHROPIC="" EXISTING_VOYAGE=""
  fi
fi

if [ -z "$EXISTING_API_ID" ]; then
  echo ""
  echo "You'll need:"
  echo "  1. Telegram API ID + Hash  →  https://my.telegram.org"
  echo "  2. Telegram Bot Token      →  message @BotFather on Telegram"
  echo "  3. Anthropic API Key       →  https://console.anthropic.com"
  echo "  4. Voyage AI API Key       →  https://dash.voyageai.com (optional)"
  echo ""

  TELEGRAM_API_ID=$(prompt_value "Telegram API ID")
  TELEGRAM_API_HASH=$(prompt_value "Telegram API Hash")
  TELEGRAM_BOT_TOKEN=$(prompt_value "Telegram Bot Token")
  ANTHROPIC_API_KEY=$(prompt_value "Anthropic API Key")
  VOYAGE_API_KEY=$(prompt_value "Voyage AI API Key (Enter to skip)" " ")
  VOYAGE_API_KEY="${VOYAGE_API_KEY## }"  # trim the default space

  # Generate a dashboard token automatically
  DASHBOARD_TOKEN=$(openssl rand -base64 32)

  # Write .env with strict permissions (600)
  rm -f "$ENV_FILE"
  (
    umask 077
    cat > "$ENV_FILE" <<EOF
TELEGRAM_API_ID=$TELEGRAM_API_ID
TELEGRAM_API_HASH=$TELEGRAM_API_HASH
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
VOYAGE_API_KEY=$VOYAGE_API_KEY
DASHBOARD_TOKEN=$DASHBOARD_TOKEN
EOF
  )

  info "Saved credentials to .env (mode 600)"
  info "Dashboard token: $DASHBOARD_TOKEN"
  echo "  (save this — you'll need it to access the web dashboard)"
fi

# ──────────────────────────────────────────────────────────────
#  Step 2: Data directory setup
# ──────────────────────────────────────────────────────────────
header "Preparing data directory"

mkdir -p "$SCRIPT_DIR/data"
chmod 700 "$SCRIPT_DIR/data"

# Fix ownership for Docker non-root user (uid 1000)
# Only needed if data dir has files owned by root from a previous run
if [ -n "$(find "$SCRIPT_DIR/data" -maxdepth 0 ! -user "$(id -u)" 2>/dev/null)" ]; then
  warn "Fixing data directory ownership (may need sudo)..."
  sudo chown -R 1000:1000 "$SCRIPT_DIR/data" || {
    warn "Could not fix ownership. If the container fails, run:"
    warn "  sudo chown -R 1000:1000 $SCRIPT_DIR/data"
  }
else
  # If we own it, just make sure container user can write
  # On macOS with Docker Desktop, bind mounts map to the host user automatically
  info "Data directory OK"
fi

# Remove stale lock file from previous runs
if [ -f "$SCRIPT_DIR/data/.lock" ]; then
  rm -f "$SCRIPT_DIR/data/.lock"
  info "Removed stale lock file"
fi

# ──────────────────────────────────────────────────────────────
#  Step 3: Build Docker image
# ──────────────────────────────────────────────────────────────
header "Building Docker image"

$COMPOSE build
info "Image built successfully"

# ──────────────────────────────────────────────────────────────
#  Step 4: Interactive setup wizard (Telegram auth + config)
# ──────────────────────────────────────────────────────────────
header "Running setup wizard"

echo ""
echo "This will:"
echo "  - Authenticate your Telegram account (sends you a code)"
echo "  - Let you configure digest schedule and timezone"
echo "  - Optionally backfill recent message history"
echo ""

$COMPOSE run --rm telegram-digest python -m src.setup

# ──────────────────────────────────────────────────────────────
#  Step 5: Fix permissions after setup wizard
# ──────────────────────────────────────────────────────────────
header "Fixing permissions"

# The setup wizard may have created files as the container user.
# Ensure the data dir and all contents are accessible.
chmod 700 "$SCRIPT_DIR/data" 2>/dev/null || true
chmod 600 "$SCRIPT_DIR/data"/*.db "$SCRIPT_DIR/data"/*.session 2>/dev/null || true

info "Permissions set (data dir: 700, db/session files: 600)"

# ──────────────────────────────────────────────────────────────
#  Step 6: Start the service
# ──────────────────────────────────────────────────────────────
header "Starting Telegram Digest"

# Clean up any leftover containers
docker rm -f telegram-digest 2>/dev/null || true
rm -f "$SCRIPT_DIR/data/.lock"

$COMPOSE up -d
info "Container started"

# Wait for it to come up
echo -n "  Waiting for service to initialize..."
for i in $(seq 1 30); do
  if docker exec telegram-digest python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    2>/dev/null; then
    echo ""
    info "Dashboard is live at http://127.0.0.1:8000"
    break
  fi
  echo -n "."
  sleep 2
done
echo ""

# ──────────────────────────────────────────────────────────────
#  Step 7: Print MCP config
# ──────────────────────────────────────────────────────────────
header "Claude Code MCP integration"

# Determine the Python path inside a venv if available, otherwise use system
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
if [ -f "$VENV_PYTHON" ]; then
  MCP_PYTHON="$VENV_PYTHON"
else
  MCP_PYTHON="$(command -v python3)"
fi

MCP_CONFIG=$(cat <<EOF
{
  "mcpServers": {
    "tg-summary": {
      "command": "$MCP_PYTHON",
      "args": ["-m", "src.mcp_server"],
      "cwd": "$SCRIPT_DIR"
    }
  }
}
EOF
)

echo ""
echo "To connect Claude Code to your Telegram data, add this to"
echo "~/.claude/.mcp.json (or merge with existing config):"
echo ""
echo "$MCP_CONFIG"
echo ""

# Also offer to create a local venv for MCP if none exists
if [ ! -f "$VENV_PYTHON" ]; then
  echo ""
  warn "No local venv found. The MCP server needs Python dependencies."
  echo "  Run these commands to set it up:"
  echo ""
  echo "    python3 -m venv venv"
  echo "    source venv/bin/activate"
  echo "    pip install -r requirements.txt"
  echo ""
fi

# ──────────────────────────────────────────────────────────────
#  Done
# ──────────────────────────────────────────────────────────────
header "Setup complete!"

echo ""
echo "  Service:     running (docker ps to check)"
echo "  Dashboard:   http://127.0.0.1:8000"
echo "  Logs:        $COMPOSE logs -f"
echo "  Stop:        $COMPOSE down"
echo "  Restart:     $COMPOSE restart"
echo ""
echo "  The bot is now monitoring your Telegram and will send"
echo "  you a daily digest via your bot. Use /help in Telegram"
echo "  to see all available commands."
echo ""
