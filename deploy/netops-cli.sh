#!/bin/sh
# Runs the NetOps admin commands as the service account, with the service's
# settings. Installed as /usr/local/sbin/netops-cli. Examples:
#   sudo netops-cli firmware-check core-sw1 --raw
#   sudo netops-cli import-csv /tmp/devices.csv
#   sudo netops-cli hash-password
set -eu
set -a
. /etc/netops/netops.env
set +a
export NETOPS_DATA_DIR=/var/lib/netops NETOPS_CREDENTIAL_KEY_FILE=/etc/netops/credential.key
cd /opt/netops/current/backend
exec runuser -u netops -- /opt/netops/current/venv/bin/python -m app.cli "$@"
