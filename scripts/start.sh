#!/bin/bash
# Starts Najda Voice via Docker Compose, waits for the /health endpoint
# to respond, and logs a clear confirmation. Meant to run on EC2 after
# boot (after update_duckdns.sh has had a chance to propagate).

set -e

cd "$(dirname "$0")/.."
LOG_FILE="logs/app.log"

echo "$(date): Starting Najda Voice (Docker)..." | tee -a "$LOG_FILE"

docker compose up -d --build >> "$LOG_FILE" 2>&1

# Wait up to 60s -- container build + start takes longer than the old
# bare-venv python process did, especially on a t2.micro's limited CPU.
for i in $(seq 1 60); do
  if curl -s http://127.0.0.1:8000/health > /dev/null; then
    echo "$(date): Health check passed -- app is ready." | tee -a "$LOG_FILE"
    exit 0
  fi
  sleep 1
done

echo "$(date): App did not become ready within 60s. Check '${LOG_FILE}' and 'docker compose logs'." | tee -a "$LOG_FILE"
exit 1
