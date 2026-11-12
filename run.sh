#!/usr/bin/env bash
# ==============================================================================
# FinSentry: Local Autonomous Corporate Financial Auditing & Reconciliation Engine
# Powered by Ollama (gemma4:e4b) and FastAPI Cockpit
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Detect Python virtual environment or system Python
if [ -d "$SCRIPT_DIR/.venv" ]; then
    PYTHON_EXEC="$SCRIPT_DIR/.venv/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON_EXEC="$(command -v python3)"
else
    echo "[ERROR] Python 3 not found! Please install Python 3.10+."
    exit 1
fi

echo "================================================================="
echo "  FinSentry - Local Autonomous Corporate Financial Audit Engine  "
echo "  Local Inference Target: Ollama (gemma4:e4b, num_ctx: 4096)     "
echo "  Cockpit Dashboard:     http://localhost:8000                   "
echo "  Vault Ingestion Drop:  $SCRIPT_DIR/vault_drop                  "
echo "  Vault Output Archive:  $SCRIPT_DIR/vault_output                "
echo "================================================================="

# Create directories
mkdir -p "$SCRIPT_DIR/vault_drop" "$SCRIPT_DIR/vault_output"

# Cleanup function on script exit
cleanup() {
    echo ""
    echo "[FinSentry] Shutting down background services..."
    if [ -n "$WATCHER_PID" ] && kill -0 "$WATCHER_PID" 2>/dev/null; then
        kill "$WATCHER_PID" 2>/dev/null || true
    fi
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
    fi
    echo "[FinSentry] All services terminated cleanly."
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# 1. Start the Resilient Watchdog Folder Watcher in Background
echo "[1/2] Starting Resilient Vault Watcher (watchdog PollingObserver)..."
"$PYTHON_EXEC" watcher.py &
WATCHER_PID=$!
echo "[FinSentry] Watcher running with PID $WATCHER_PID"

# 2. Start the FastAPI Web Cockpit Server on localhost:8000
echo "[2/2] Starting FastAPI Web Cockpit on http://127.0.0.1:8000..."
"$PYTHON_EXEC" -m uvicorn app:app --host 127.0.0.1 --port 8000 --log-level info &
SERVER_PID=$!
echo "[FinSentry] Cockpit Server running with PID $SERVER_PID"

echo ""
echo ">>> FinSentry is fully active and listening on http://127.0.0.1:8000 <<<"
echo "Press Ctrl+C to stop all FinSentry services."
echo ""

# Wait for background processes
wait "$SERVER_PID" "$WATCHER_PID"
