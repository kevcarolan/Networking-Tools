#!/usr/bin/env bash
# Builds an offline install bundle for an air-gapped NetOps server.
#
# Run on an INTERNET-CONNECTED machine running THE SAME UBUNTU RELEASE AS THE
# SERVER (e.g. 26.04): a VM, WSL, or a container such as
#   docker run --rm -it -v "$PWD":/src -w /src ubuntu:26.04 bash
# from the root of the repository. The bundle is built for the release (and its
# Python version) of the machine it is built on, and only installs on that release.
#
#     sudo deploy/airgap/build-bundle.sh
#
# Output:  dist/netops-bundle-<version>.tar.gz   (+ .sha256)
# The bundle holds the app, every Python package as a wheel, the Ubuntu packages
# the server needs (as a local apt repository), a vulnerability report and
# SHA256SUMS. Copy it across with your normal media-transfer process and follow
# docs/install-airgap.md.
#
# Options (environment variables):
#   SKIP_DEBS=1        don't download Ubuntu packages (you have an internal apt mirror)
#   ALLOW_VULNS=1      build even if pip-audit reports known vulnerabilities
#   TARGET_ARCH=x86_64 target CPU (x86_64 or aarch64)
#   MAKE_ISO=1         also write dist/<bundle>.iso: a CD image holding the bundle, to
#                      upload to a vSphere datastore and attach to the VM's CD drive
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

TARGET_ARCH="${TARGET_ARCH:-x86_64}"
# Ubuntu packages the server needs (their dependencies are added automatically).
OS_PACKAGES=(
  python3-venv git sqlite3 nginx
  nftables chrony auditd aide apparmor-utils rsyslog-gnutls apt-offline open-vm-tools
)

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "run this on Ubuntu (the same release as the server); this is ${PRETTY_NAME:-unknown}"
UBUNTU="$VERSION_ID"
echo "Building for Ubuntu $UBUNTU ($PRETTY_NAME). The server must run the same release."

if [[ $EUID -eq 0 ]]; then
  say "Installing build tools"
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends python3 python3-venv git apt-utils ca-certificates \
    ${MAKE_ISO:+xorriso} >/dev/null
elif [[ -z "${SKIP_DEBS:-}" ]]; then
  die "run with sudo (needed to download the Ubuntu packages)"
fi

VERSION="$(date -u +%Y%m%d)-$(git -c safe.directory='*' rev-parse --short HEAD 2>/dev/null || echo nogit)"
if [[ -n "$(git -c safe.directory='*' status --porcelain 2>/dev/null)" ]]; then
  VERSION="${VERSION}-dirty"
  echo "WARNING: the working tree has uncommitted changes; they are NOT in the bundle (git archive uses HEAD)."
fi
NAME="netops-bundle-${VERSION}"
WORK="$(mktemp -d)"
OUT="$WORK/$NAME"
mkdir -p "$OUT"/{wheels,debs} "$REPO_ROOT/dist"
trap 'rm -rf "$WORK"' EXIT

# The bundle's Python packages are built for this release's own Python.
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

say "Application source ($VERSION)"
git -c safe.directory='*' archive --format=tar --prefix=app/ HEAD | tar -x -C "$OUT"
cp deploy/airgap/install.sh "$OUT/install.sh"
chmod 755 "$OUT/install.sh"

say "Python packages for CPython $PYVER on Linux $TARGET_ARCH"
python3 -m venv "$WORK/buildenv"
"$WORK/buildenv/bin/pip" install -q --upgrade pip pip-audit
PLATFORMS=()
for tag in manylinux_2_39 manylinux_2_34 manylinux_2_28 manylinux_2_17 manylinux2014 linux; do
  PLATFORMS+=(--platform "${tag}_${TARGET_ARCH}")
done
"$WORK/buildenv/bin/pip" download -q -r backend/requirements.txt -d "$OUT/wheels" \
  --only-binary=:all: --implementation cp --python-version "$PYVER" --abi "cp${PYVER/./}" \
  "${PLATFORMS[@]}"
# Exact versions that went into the bundle, for the record and for pip-audit.
"$WORK/buildenv/bin/python" - "$OUT/wheels" > "$OUT/requirements.lock" <<'PY'
import pathlib, re, sys
for whl in sorted(pathlib.Path(sys.argv[1]).glob("*.whl")):
    name, version = whl.name.split("-")[:2]
    print(f"{re.sub(r'[-_.]+', '-', name).lower()}=={version}")
PY
echo "$(wc -l < "$OUT/requirements.lock") packages"

say "Checking Python packages for known vulnerabilities (pip-audit)"
if "$WORK/buildenv/bin/pip-audit" -r "$OUT/requirements.lock" --no-deps --disable-pip \
     > "$OUT/pip-audit.txt" 2>&1; then
  echo "No known vulnerabilities."
else
  cat "$OUT/pip-audit.txt"
  [[ -n "${ALLOW_VULNS:-}" ]] || die "known vulnerabilities found (see above). Update backend/requirements.txt, or set ALLOW_VULNS=1 to build anyway."
  echo "WARNING: building with known vulnerabilities because ALLOW_VULNS is set."
fi

if [[ -z "${SKIP_DEBS:-}" ]]; then
  say "Ubuntu packages: ${OS_PACKAGES[*]}"
  # Download the packages and their whole dependency tree, whatever is already
  # installed here, so the bundle doesn't depend on the state of this machine.
  mapfile -t DEBS < <(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
      --no-breaks --no-replaces --no-enhances "${OS_PACKAGES[@]}" | grep -E '^[a-z0-9]' | sort -u)
  (cd "$OUT/debs" && apt-get download -qq "${DEBS[@]}" 2>&1 | grep -v "^W:" || true)
  (cd "$OUT/debs" && apt-ftparchive packages . > Packages 2>/dev/null)
  printf '%s\n' "${OS_PACKAGES[@]}" > "$OUT/debs/PACKAGES.txt"
  echo "$(ls "$OUT"/debs/*.deb | wc -l) packages"
else
  echo "Skipping Ubuntu packages (SKIP_DEBS set)"
fi

say "Manifest (for the change ticket)"
{
  echo "Everything in this bundle. Nothing else is installed or downloaded on the server."
  echo
  echo "== Ubuntu $UBUNTU packages (installed only if missing; never downgraded) =="
  echo "Requested: $(tr '\n' ' ' < "$OUT/debs/PACKAGES.txt" 2>/dev/null || echo 'none (SKIP_DEBS)')"
  for deb in "$OUT"/debs/*.deb; do
    [[ -e "$deb" ]] && dpkg-deb --show --showformat='${Package}\t${Version}\t${Architecture}\n' "$deb"
  done | sort | awk -F'\t' '{printf "  %-34s %-40s %s\n", $1, $2, $3}'
  echo
  echo "== Python packages (installed into the app's own environment, not system-wide) =="
  awk -F'==' '{printf "  %-34s %s\n", $1, $2}' "$OUT/requirements.lock"
  echo
  echo "== Vulnerability check (pip-audit) =="
  cat "$OUT/pip-audit.txt"
} > "$OUT/MANIFEST.txt"
echo "$(grep -c . "$OUT/MANIFEST.txt") lines written to MANIFEST.txt"

say "Checksums and bundle"
{
  echo "NetOps Tools offline bundle"
  echo "version:  $VERSION"
  echo "commit:   $(git -c safe.directory='*' rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "built:    $(date -u +%FT%TZ) on $(hostname)"
  echo "ubuntu:   $UBUNTU"
  echo "python:   $PYVER"
  echo "arch:     $TARGET_ARCH"
} > "$OUT/BUNDLE-INFO.txt"
(cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
tar -C "$WORK" -czf "dist/$NAME.tar.gz" "$NAME"
(cd dist && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
cp "$OUT/MANIFEST.txt" "dist/$NAME.MANIFEST.txt"
if [[ -n "${MAKE_ISO:-}" ]]; then
  xorriso -as mkisofs -quiet -r -J -V NETOPS_BUNDLE -o "dist/$NAME.iso" \
    "dist/$NAME.tar.gz" "dist/$NAME.tar.gz.sha256" "dist/$NAME.MANIFEST.txt"
  (cd dist && sha256sum "$NAME.iso" > "$NAME.iso.sha256")
fi

say "Done"
echo "Bundle:  dist/$NAME.tar.gz ($(du -h "dist/$NAME.tar.gz" | cut -f1))"
echo "SHA-256: $(cut -d' ' -f1 "dist/$NAME.tar.gz.sha256")"
echo "List:    dist/$NAME.MANIFEST.txt (every package and version, for the change ticket)"
[[ -n "${MAKE_ISO:-}" ]] && echo "CD image: dist/$NAME.iso (SHA-256 $(cut -d' ' -f1 "dist/$NAME.iso.sha256"))"
echo
echo "Record the SHA-256 in your change ticket (or send it by a separate channel);"
echo "the install checks it on the server before anything is installed."
