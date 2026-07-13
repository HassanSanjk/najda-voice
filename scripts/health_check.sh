#!/bin/bash
# Quick standalone check that Najda Voice is up and responding.
# Useful to run manually before a demo, independent of start.sh.

URL="${1:-http://127.0.0.1:8000/health}"

response=$(curl -s -o /dev/null -w "%{http_code}" "$URL")

if [ "$response" = "200" ]; then
  echo "OK — ${URL} responded with 200"
  exit 0
else
  echo "FAIL — ${URL} responded with ${response:-no response}"
  exit 1
fi
