#!/usr/bin/env bash
# Fix ownership of data/ subdirectories after a podman-compose build or force-recreate.
#
# In rootless Podman, the host user (uid 1000) appears as root inside the
# container namespace. The app container runs as appuser (uid 1000 in-container
# = host uid 525287) and the MariaDB container runs as mysql (uid 999
# in-container = host uid 525286). This script restores the correct ownership
# so both containers can write to their respective directories.
#
# Usage: ./scripts/fix-data-permissions.sh
# Run from the project root after any `podman-compose build` + `up --force-recreate`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$(dirname "$SCRIPT_DIR")/data"

echo "Fixing permissions in $DATA_DIR …"

# App container dirs (appuser = container uid 1000 = host uid 525287)
podman unshare chown 1000:1000 \
    "$DATA_DIR/cache" \
    "$DATA_DIR/chroma_db" \
    "$DATA_DIR/library" \
    "$DATA_DIR/processed" \
    "$DATA_DIR/raw" \
    "$DATA_DIR/summary"

# MariaDB data dir (mysql = container uid 999 = host uid 525286)
podman unshare chown -R 999:999 "$DATA_DIR/mysql"

echo "Done."
