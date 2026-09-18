#!/usr/bin/env bash
# Sync 4ch diagnostic WAVs to desktop for listening/analysis.
# Uses rsync over SSH (preferred) or local mount fallback.
set -euo pipefail

SRC="${1:-/home/ron/assistant/testwavs}"
DST_HOST="${2:-192.168.5.146}"
DST_PATH="${3:-/mnt/molly/development/lama/testwavs}"

mkdir -p "$SRC"

# Try SSH first
echo "Syncing to $DST_HOST:$DST_PATH ..."
if rsync -av --remove-source-files \
    -e "ssh -o BatchMode=yes -o ConnectTimeout=5" \
    "$SRC/" "$DST_HOST:$DST_PATH/" 2>/dev/null; then
    echo "Sync complete (SSH): $SRC -> $DST_HOST:$DST_PATH"
    exit 0
fi

# SSH failed — try local mount
if [ -d "$DST_PATH" ]; then
    rsync -av --remove-source-files "$SRC/" "$DST_PATH/"
    echo "Sync complete (local): $SRC -> $DST_PATH"
    exit 0
fi

echo "Sync failed — SSH key not authorized and path not mounted."
echo ""
echo "To enable SSH sync, add this key to $DST_HOST:~/.ssh/authorized_keys:"
cat ~/.ssh/id_ed25519.pub 2>/dev/null || echo "  (no key found — run: ssh-keygen -t ed25519)"
echo ""
echo "To mount locally:  sudo mount $DST_HOST:/mnt/molly /mnt/molly"
exit 1
