#!/bin/sh
# Consistent backup of the NetOps database and config archive to
# /var/backups/netops/netops-<label>-<time>.tar.gz, keeping KEEP_DAYS days.
#
# Deliberately NOT included:
#   - /etc/netops/credential.key: keep it in your offline key store, so a stolen
#     backup can't be used to decrypt the device passwords
#   - data/firmware/: images can be downloaded from the vendor again
# Copy /var/backups/netops off the server with your normal backup system.
set -eu
DATA="${NETOPS_DATA_DIR:-/var/lib/netops}"
DEST=/var/backups/netops
KEEP_DAYS="${KEEP_DAYS:-14}"
LABEL="${1:-manual}"
STAMP="$(date +%Y%m%d-%H%M%S)"
umask 077
mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

[ -f "$DATA/app.db" ] || { echo "No database at $DATA/app.db yet - nothing to back up."; exit 0; }
sqlite3 "$DATA/app.db" ".backup '$TMP/app.db'"
GIT="git -c safe.directory=* -C $DATA/configs"
if [ -d "$DATA/configs/.git" ] && $GIT rev-parse --verify -q HEAD >/dev/null; then
  $GIT bundle create -q "$TMP/configs.gitbundle" --all
fi
cp /etc/netops/netops.env "$TMP/netops.env" 2>/dev/null || true
tar -C "$TMP" -czf "$DEST/netops-$LABEL-$STAMP.tar.gz" .
find "$DEST" -name 'netops-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "Backup written: $DEST/netops-$LABEL-$STAMP.tar.gz"
