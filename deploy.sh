#!/bin/bash
set -e

git pull

# Force-remove any lingering container before rebuild (prevents name conflict)
docker compose down --remove-orphans 2>/dev/null || true
docker rm -f ai-trading-alltime-record-api-1 2>/dev/null || true

docker compose build --no-cache
docker compose up -d --force-recreate

# Restart NSE option-chain collectors (do NOT auto-start nse-runner here).
docker compose --profile collectors up -d --force-recreate

echo ""
echo "=== Deploy complete. Checking logs... ==="
sleep 3
docker compose logs --tail=20 api
