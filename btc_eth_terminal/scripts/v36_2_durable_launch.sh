#!/bin/sh
# v36.2 read-only Linux operator launch. Every restart uses a NEW capture.
set -eu
umask 077
ROOT=$(printenv V36_TERMINAL_ROOT 2>/dev/null || printf '/opt/btc-eth-terminal')
CAPTURES=$(printenv V36_CAPTURE_ROOT 2>/dev/null || printf '/var/lib/btceth-live/captures')
if [ ! -d "$ROOT" ] || [ -L "$ROOT" ] || [ ! -f "$ROOT/scripts/v36_observe_existing_collector.py" ]; then
  echo 'BLOCKED_DURABLE_SOURCE_INSTALL_MISSING' >&2
  exit 2
fi
if [ ! -d "$CAPTURES" ] || [ -L "$CAPTURES" ]; then
  echo 'BLOCKED_CAPTURE_DIRECTORY_MISSING_OR_SYMLINK' >&2
  exit 2
fi
# Production-like budget minima are never reduced for a smaller host.
ID=$(python3 -c 'import secrets;print(secrets.token_hex(8))')
DATE=$(date -u +%Y%m%dT%H%M%SZ)
TARGET="$CAPTURES/$DATE-$ID"
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
  echo 'BLOCKED_SOURCE_CAPTURE_NAME_COLLISION' >&2
  exit 2
fi
cd "$ROOT"
exec python3 scripts/v36_observe_existing_collector.py \
  --execute-public-capture --durable-operator-mode \
  --duration 86400 --port 8766 \
  --raw-budget-bytes 40000000000 \
  --archive-budget-bytes 55000000000 \
  --disk-reserve-bytes 10737418240 \
  --workdir "$TARGET"
