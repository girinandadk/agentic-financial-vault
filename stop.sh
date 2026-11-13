#!/usr/bin/env bash
# ==============================================================================
# FinSentry: Stop all running services (FastAPI server & watcher)
# ==============================================================================

echo "[FinSentry] Terminating all running server and background watcher processes..."

# Kill processes on port 8000
lsof -ti :8000 | xargs kill -9 2>/dev/null || true

# Kill any stray uvicorn or watcher instances
pkill -9 -f "uvicorn.*app:app" 2>/dev/null || true
pkill -9 -f "python.*watcher.py" 2>/dev/null || true

sleep 1

if lsof -i :8000 >/dev/null 2>&1; then
    echo "[WARNING] Port 8000 is still occupied."
else
    echo "[FinSentry] All services stopped. Port 8000 is clean and free."
fi
