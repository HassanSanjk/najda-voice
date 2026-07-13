#!/bin/bash
# Starts Najda Voice, waits for the /health endpoint to respond,
# and logs a clear confirmation. Meant to be run on EC2 after boot
# (after update_duckdns.sh has had a chance to propagate).

set -e

cd "$(dirname "$0")/.."
LOG_FILE="logs/app.log"

echo "$(date): Starting Najda Voice..." | tee -a "$LOG_FILE"

source venv/bin/activate
nohup python run.py >> "$LOG_FILE" 2>&1 &
APP_PID=$!
echo "$(date): uvicorn started with PID ${APP_PID}" | tee -a "$LOG_FILE"

# Wait for readiness (up to 30s)
for i in $(seq 1 30); do
  if curl -s http://127.0.0.1:8000/health > /dev/null; then
    echo "$(date): Health check passed — app is ready." | tee -a "$LOG_FILE"
    exit 0
  fi
  sleep 1
done

echo "$(date): App did not become ready within 30s. Check ${LOG_FILE}." | tee -a "$LOG_FILE"
exit 1
