#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-admin@192.168.7.163}"
PI_PROJECT="${PI_PROJECT:-/home/admin/Desktop/Open Flight Golf/openflight}"
PI_STAGE="${PI_STAGE:-/home/admin/openflight-sync}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

echo "Uploading OpenFlight from: $PROJECT_DIR"
echo "Pi destination: $PI_HOST:$PI_PROJECT"

rsync -av --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'ui/node_modules/' \
  --exclude '**/__pycache__/' \
  --exclude 'openflight_sessions/' \
  --exclude 'session_logs/' \
  "$PROJECT_DIR/" \
  "$PI_HOST:$PI_STAGE/"

ssh "$PI_HOST" "mkdir -p \"$PI_PROJECT\" && rsync -av --exclude '.git/' --exclude '.venv/' --exclude 'ui/node_modules/' --exclude '**/__pycache__/' --exclude 'openflight_sessions/' --exclude 'session_logs/' \"$PI_STAGE/\" \"$PI_PROJECT/\""

echo "Upload complete. On the Pi, run:"
echo "  cd '$PI_PROJECT'"
echo "  uv sync"
