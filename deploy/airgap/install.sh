#!/usr/bin/env bash
# Installs or upgrades NetOps Tools on an air-gapped Ubuntu 24.04 server from an
# offline bundle made by build-bundle.sh. Nothing is downloaded.
#
#     tar -xzf netops-bundle-<version>.tar.gz -C /tmp
#     sudo /tmp/netops-bundle-<version>/install.sh
#
# Options:
#   --skip-os-packages   don't install Ubuntu packages (already installed, or you
#                        use an internal apt mirror)
#
# Layout it creates (see docs/install-airgap.md):
#   /opt/netops/releases/<version>/   code + Python venv, owned by root, read-only
#   /opt/netops/current               symlink to the running release
#   /etc/netops/                      netops.env, credential.key, tls/  (root:netops 0750)
#   /var/lib/netops/                  data: database, config archive, firmware images
#   /var/backups/netops/              nightly backups (root only)
#
# Upgrades: the running service is backed up, stopped, switched to the new
# release and started again. If it doesn't come back healthy, the previous
# release is restored automatically.
set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/netops
ETC=/etc/netops
DATA=/var/lib/netops
BACKUPS=/var/backups/netops
SKIP_OS=""
for arg in "$@"; do
  case "$arg" in
    --skip-os-packages) SKIP_OS=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
have_systemd() { [[ -d /run/systemd/system ]]; }

[[ $EUID -eq 0 ]] || die "run with sudo"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || die "this bundle is for Ubuntu 24.04"
VERSION="$(awk '/^version:/ {print $2}' "$BUNDLE/BUNDLE-INFO.txt")"
[[ -n "$VERSION" ]] || die "BUNDLE-INFO.txt is missing or damaged"
REL="$PREFIX/releases/$VERSION"
APP="$BUNDLE/app"

say "Checking bundle integrity ($VERSION)"
(cd "$BUNDLE" && sha256sum --quiet -c SHA256SUMS) || die "checksum mismatch: the bundle is damaged or has been changed. Do not install it."
echo "All files match SHA256SUMS."

if [[ -z "$SKIP_OS" && -f "$BUNDLE/debs/Packages" ]]; then
  say "Installing Ubuntu packages from the bundle"
  # A temporary apt source that points only at the bundle. apt installs what is
  # missing and never downgrades what is already there.
  APTDIR="$(mktemp -d)"
  trap 'rm -rf "$APTDIR"' EXIT
  mkdir -p "$APTDIR/lists/partial"
  echo "deb [trusted=yes] file:$BUNDLE/debs ./" > "$APTDIR/netops.list"
  APT=(apt-get -o Dir::Etc::SourceList="$APTDIR/netops.list" -o Dir::Etc::SourceParts=-
       -o Dir::State::Lists="$APTDIR/lists" -o APT::Sandbox::User=root)
  "${APT[@]}" update -qq
  mapfile -t PKGS < "$BUNDLE/debs/PACKAGES.txt"
  DEBIAN_FRONTEND=noninteractive "${APT[@]}" install -y -q --no-install-recommends "${PKGS[@]}"
elif [[ -z "$SKIP_OS" ]]; then
  echo "This bundle has no Ubuntu packages; make sure python3.12-venv, git, sqlite3 and nginx are installed."
fi
for cmd in python3.12 git sqlite3; do
  command -v "$cmd" >/dev/null || die "$cmd is not installed"
done

say "Service account and folders"
if ! id netops >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin netops
  echo "Created system user 'netops' (no login shell, no password)."
fi
install -d -m 0755 -o root -g root "$PREFIX" "$PREFIX/releases"
install -d -m 0750 -o root -g netops "$ETC"
install -d -m 0700 -o root -g root "$ETC/tls" "$BACKUPS"
install -d -m 0750 -o netops -g netops "$DATA"

say "Installing release $VERSION"
CURRENT="$(readlink -f "$PREFIX/current" 2>/dev/null || true)"
if [[ "$CURRENT" == "$REL" ]]; then
  echo "Reinstalling the release that is already running."
fi
STAGE="$PREFIX/releases/.staging-$VERSION"
rm -rf "$STAGE"
cp -a "$APP" "$STAGE"
python3.12 -m venv "$STAGE/venv"
# (Always run the venv as "venv/bin/python -m ...": the folder is renamed below,
# which breaks the paths inside launcher scripts such as venv/bin/uvicorn.)
"$STAGE/venv/bin/python" -m pip install -q --no-index --find-links "$BUNDLE/wheels" \
  -r "$STAGE/backend/requirements.txt"
# Code is owned by root and not writable by the service account.
chown -R root:root "$STAGE"
chmod -R u=rwX,go=rX "$STAGE"
(cd "$STAGE/backend" && "$STAGE/venv/bin/python" -c "import app.main") \
  || die "the new release failed to load; nothing was changed"

say "Configuration"
FIRST_INSTALL=""
if [[ ! -f "$ETC/netops.env" ]]; then
  FIRST_INSTALL=1
  sed -e "s|^# NETOPS_DATA_DIR=.*|NETOPS_DATA_DIR=$DATA|" \
      -e "s|^NETOPS_CREDENTIAL_KEY_FILE=.*|NETOPS_CREDENTIAL_KEY_FILE=$ETC/credential.key|" \
      "$STAGE/.env.example" > "$ETC/netops.env"
  chown root:netops "$ETC/netops.env"
  chmod 0640 "$ETC/netops.env"
  echo "Created $ETC/netops.env - edit it before starting the service."
fi
if [[ ! -f "$ETC/credential.key" ]]; then
  (umask 027 && "$STAGE/venv/bin/python" -c \
    "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > "$ETC/credential.key")
  chown root:netops "$ETC/credential.key"
  chmod 0640 "$ETC/credential.key"
  echo "Created $ETC/credential.key - copy it to your offline key store now (see the guide)."
fi

install -m 0644 "$STAGE/deploy/netops.service" /etc/systemd/system/netops.service
install -m 0644 "$STAGE/deploy/netops-backup.service" /etc/systemd/system/netops-backup.service
install -m 0644 "$STAGE/deploy/netops-backup.timer" /etc/systemd/system/netops-backup.timer
install -m 0750 "$STAGE/deploy/netops-backup.sh" /usr/local/sbin/netops-backup
install -m 0750 "$STAGE/deploy/netops-cli.sh" /usr/local/sbin/netops-cli
if [[ -d /etc/audit/rules.d ]]; then
  install -m 0640 "$STAGE/deploy/hardening/audit-netops.rules" /etc/audit/rules.d/netops.rules
  command -v augenrules >/dev/null && augenrules --load >/dev/null 2>&1 || true
fi
if [[ -d /etc/nginx/sites-available && ! -f /etc/nginx/sites-available/netops ]]; then
  install -m 0644 "$STAGE/deploy/nginx-netops.conf" /etc/nginx/sites-available/netops
  echo "Created /etc/nginx/sites-available/netops - set server_name and the certificate, then enable it."
fi

say "Switching to $VERSION"
SERVICE_WAS_RUNNING=""
if have_systemd && systemctl is-active --quiet netops; then
  SERVICE_WAS_RUNNING=1
  echo "Backing up before the upgrade..."
  /usr/local/sbin/netops-backup pre-upgrade
  systemctl stop netops
fi
rm -rf "$REL"
mv "$STAGE" "$REL"
ln -sfn "$REL" "$PREFIX/current.new" && mv -T "$PREFIX/current.new" "$PREFIX/current"

health() {
  for _ in $(seq 1 30); do
    "$PREFIX/current/venv/bin/python" -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)" \
      2>/dev/null && return 0
    sleep 1
  done
  return 1
}

if have_systemd; then
  systemctl daemon-reload
  systemctl enable --quiet netops-backup.timer
  systemctl start netops-backup.timer
  if [[ -n "$SERVICE_WAS_RUNNING" ]]; then
    systemctl start netops
    if health; then
      echo "NetOps Tools $VERSION is running."
    else
      echo "The new release did not become healthy - rolling back."
      journalctl -u netops -n 30 --no-pager || true
      if [[ -n "$CURRENT" && -d "$CURRENT" && "$CURRENT" != "$REL" ]]; then
        ln -sfn "$CURRENT" "$PREFIX/current"
        systemctl restart netops
        if health; then
          die "rolled back to $(basename "$CURRENT"); fix the problem above and try again"
        fi
        die "the rollback also failed to start; check 'journalctl -u netops'"
      fi
      die "the service did not start; check 'journalctl -u netops'"
    fi
  fi
else
  echo "systemd is not running here: skipped enabling the service and backup timer."
fi

# Keep the running release plus the two before it, for rollback.
find "$PREFIX/releases" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%T@ %p\n' \
  | sort -rn | awk 'NR>3 {print $2}' | while read -r old; do
      [[ "$old" == "$REL" ]] || { rm -rf "$old"; echo "Removed old release $(basename "$old")"; }
    done

say "Done"
if [[ -n "$FIRST_INSTALL" || -z "$SERVICE_WAS_RUNNING" ]]; then
  cat <<EOF
Next steps (docs/install-airgap.md, section "First-time configuration"):
  1. Edit $ETC/netops.env (AD settings; leave the local admin hash empty unless you need it).
  2. Put the TLS certificate and key in $ETC/tls/ and set server_name in
     /etc/nginx/sites-available/netops, then:
       ln -sf /etc/nginx/sites-available/netops /etc/nginx/sites-enabled/netops
       rm -f /etc/nginx/sites-enabled/default && nginx -t && systemctl reload nginx
  3. sudo systemctl enable --now netops
  4. Apply the hardening steps in docs/security-hardening.md.
EOF
fi
