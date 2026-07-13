#!/bin/bash
# Updates DuckDNS with this EC2 instance's current public IP.
# Run via cron @reboot so the domain stays pointed at the
# instance every time it starts (EC2 gets a new IP on each boot).
#
# Fill in DUCKDNS_DOMAIN and DUCKDNS_TOKEN before use.
# Add to crontab with:
#   @reboot /path/to/najda-voice/scripts/update_duckdns.sh >> /path/to/najda-voice/logs/duckdns.log 2>&1

DUCKDNS_DOMAIN=""   # e.g. "najda-voice" (without .duckdns.org)
DUCKDNS_TOKEN=""

if [ -z "$DUCKDNS_DOMAIN" ] || [ -z "$DUCKDNS_TOKEN" ]; then
  echo "$(date): DUCKDNS_DOMAIN or DUCKDNS_TOKEN not set. Aborting."
  exit 1
fi

echo "$(date): Updating DuckDNS for ${DUCKDNS_DOMAIN}.duckdns.org"
curl -s "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip="
echo ""
