# Installing NetOps Tools on an air-gapped server

This is the supported way to install the platform on a network with no internet
access. Everything the server needs goes across in **one offline bundle**:

* the app;
* every Python package, pinned and checked for known vulnerabilities;
* the Ubuntu packages, as a local package repository;
* checksums.

Nothing is downloaded on the server, and nothing needs compiling there.

```
 Internet side                        Transfer                 Air-gapped side
 ┌──────────────────────────┐   ┌───────────────────┐   ┌──────────────────────────────┐
 │ Build machine            │   │ Approved media     │   │ NetOps server (Ubuntu 24.04) │
 │ (Ubuntu 24.04 VM / WSL)  │──►│ scanned per policy │──►│ verify SHA-256 ─► install.sh │
 │ build-bundle.sh          │   │ + SHA-256 in the   │   │ ─► configure ─► harden       │
 │ ─► netops-bundle-*.tar.gz│   │   change ticket    │   │                              │
 └──────────────────────────┘   └───────────────────┘   └──────────────────────────────┘
```

Do the hardening in [security-hardening.md](security-hardening.md) **before** you add real
device credentials. This guide says at which point.

---

## 1. What you need

| Item | Details |
|---|---|
| Server | A VM with 2 vCPU, 4 GB RAM, a 30 GB system disk, and a separate **100 GB data disk** (firmware images). x86_64 |
| Operating system | Ubuntu Server 24.04 LTS ISO (the same release as the bundle) |
| Build machine | Any Ubuntu 24.04 with internet access and sudo: a VM, Windows WSL (`wsl --install -d Ubuntu-24.04`), or an `ubuntu:24.04` Docker container. Use a clean, patched machine that is used only for this |
| DNS name | e.g. `netops.corp.local`, with a DNS record on the air-gapped network |
| TLS certificate | Issued by your internal CA for that name, with the key. PEM format; include any intermediate CA in the certificate file |
| AD | Two groups: `NetOps-Admins` and `NetOps-Viewers`. The CA certificate that signed the domain controllers' LDAPS certificates |
| Network facts | For the firewall: admin/jump-host subnets, the subnets allowed to use the GUI, device management subnets, and the DC, DNS, NTP and syslog addresses |
| People | Named admin accounts for the server (no shared logins), each with an SSH key. Hardware keys (YubiKey or similar) if you have them |

---

## 2. Build the server (operating system)

1. **Create the VM:**
   * Two virtual disks: 30 GB for the system and 100 GB for data.
   * Remove the virtual floppy, sound and USB controllers, and the CD drive once
     the install is done.
   * Turn off shared folders, clipboard and drag-and-drop in the hypervisor.
   * If your hypervisor supports VM encryption with a vTPM (vSphere VM Encryption,
     Hyper-V shielded VMs), turn it on. See security-hardening.md §2.
2. **Install Ubuntu Server 24.04:**
   * Choose **Ubuntu Server (minimized)**.
   * Storage: use LVM on the 30 GB disk. Tick **Encrypt the LVM group with LUKS** if
     the hypervisor can't encrypt the VM; you'll then enter the passphrase at the
     console on every reboot.
   * Tick **Install OpenSSH server**. Don't import SSH keys from GitHub or Launchpad.
   * **Don't** select any of the "featured server snaps".
   * Create your own named admin account.
3. **Set up the data disk** as `/var/lib/netops`, so firmware images can't fill the
   system disk. The mount options stop anything on it being run as a program:
   ```bash
   sudo mkfs.ext4 -L netops-data /dev/sdb          # check the device name with lsblk first!
   sudo mkdir -p /var/lib/netops
   echo 'LABEL=netops-data /var/lib/netops ext4 defaults,nodev,nosuid,noexec 0 2' | sudo tee -a /etc/fstab
   sudo mount /var/lib/netops
   ```
4. **Set the time source** before anything else, because TLS, AD logins and logs
   all depend on correct time. You can do this after step 5 (chrony comes in the bundle):
   edit `/etc/chrony/chrony.conf`, replace the `pool ...` lines with
   `server <your-internal-ntp> iburst`, then run `sudo systemctl restart chrony && chronyc tracking`.

---

## 3. Build the bundle (internet side)

On the build machine, get the repository at the version you want to deploy and run:

```bash
git clone https://github.com/kevcarolan/Networking-Tools.git
cd Networking-Tools
sudo deploy/airgap/build-bundle.sh
```

Or, from any machine with Docker:

```bash
docker run --rm -v "$PWD":/src -w /src ubuntu:24.04 deploy/airgap/build-bundle.sh
```

This takes about a minute and produces `dist/netops-bundle-<date>-<commit>.tar.gz`
(about 75 MB) and a `.sha256` file. It:

* runs **pip-audit** and stops if any Python package has a known vulnerability. The
  report is saved in the bundle as `pip-audit.txt`;
* downloads the Ubuntu packages and their full dependency tree, so the result doesn't
  depend on what is installed on the build machine. If you have an internal Ubuntu
  mirror on the air-gapped side, set `SKIP_DEBS=1`;
* prints the bundle's **SHA-256**. Record it in the change ticket (or send it by a
  different channel from the media). This is how you'll know on the server that the
  file wasn't changed in transit.

---

## 4. Transfer and verify

1. Copy **only** the `.tar.gz` and `.sha256` files to the approved transfer media.
2. Scan them at your media-scanning station, as your policy requires.
3. On the server:
   ```bash
   sudo install -d -m 0700 /root/netops-install
   sudo cp /media/<usb>/netops-bundle-*.tar.gz* /root/netops-install/
   cd /root/netops-install
   sha256sum netops-bundle-*.tar.gz        # must match the value in the change ticket
   sudo sha256sum -c netops-bundle-*.tar.gz.sha256
   ```
   **If the hash doesn't match, stop.** Don't extract the file.

---

## 5. Install

```bash
cd /root/netops-install
sudo tar -xzf netops-bundle-*.tar.gz
sudo bash netops-bundle-*/install.sh
```

The installer:

1. checks every file in the bundle against its `SHA256SUMS`;
2. installs the Ubuntu packages from the bundle. It adds only what is missing and
   never downgrades anything:
   * `python3.12-venv`, `git`, `sqlite3`, `nginx`;
   * the hardening tools: `nftables`, `chrony`, `auditd`, `aide`, `apparmor-utils`,
     `rsyslog-gnutls`, `apt-offline`;
3. creates the `netops` system account. It has no password and no login shell;
4. installs the release into `/opt/netops/releases/<version>/` (owned by root and
   read-only to the service) and points `/opt/netops/current` at it;
5. creates `/etc/netops/netops.env` and a new encryption key, `/etc/netops/credential.key`;
6. installs:
   * the sandboxed systemd service `netops`;
   * the nightly backup timer;
   * the `netops-cli` admin command;
   * the audit rules;
   * an nginx site template.

It does **not** start the service on a first install.

| Path | What | Owner / mode |
|---|---|---|
| `/opt/netops/current` → `releases/<version>` | Code and Python environment | root, read-only |
| `/etc/netops/netops.env` | Settings | root:netops 0640 |
| `/etc/netops/credential.key` | Key that encrypts device passwords | root:netops 0640 |
| `/etc/netops/tls/` | nginx certificate and key | root 0700 |
| `/var/lib/netops/` | Database, config archive, firmware images | netops 0750 |
| `/var/backups/netops/` | Nightly backups | root 0700 |

---

## 6. First-time configuration

1. **Save the encryption key offline, now.** Without `/etc/netops/credential.key`, the
   stored device passwords can't be decrypted. It is deliberately **not** in the nightly
   backups. Copy it to your password vault or an encrypted USB key kept in a safe:
   ```bash
   sudo cat /etc/netops/credential.key
   ```
2. **Edit the settings:** `sudo nano /etc/netops/netops.env`
   * `NETOPS_LDAP_URL`, `NETOPS_LDAP_DOMAIN`, `NETOPS_LDAP_BASE_DN` and the two group DNs.
     Always set `NETOPS_LDAP_VIEWER_GROUP`; if it's empty, **every** domain user can sign in.
   * Copy the AD CA certificate to `/etc/netops/ad-ca.pem` (`chmod 0644`) and set
     `NETOPS_LDAP_CA_FILE=/etc/netops/ad-ca.pem`.
   * **Break-glass admin:** leave `NETOPS_LOCAL_ADMIN_PASSWORD_HASH` empty unless you
     want a login that works when AD is down. If you do, use a long random password kept
     in the vault. Generate the hash with `sudo netops-cli hash-password`.
   * Keep `NETOPS_SESSION_HTTPS_ONLY=true`, `NETOPS_API_DOCS=false` and
     `NETOPS_VIEWERS_CAN_READ_CONFIGS=false`.
3. **TLS and nginx:**
   ```bash
   sudo install -m 0600 netops.crt netops.key /etc/netops/tls/     # full chain in netops.crt
   sudo nano /etc/nginx/sites-available/netops                     # set server_name
   sudo ln -sf /etc/nginx/sites-available/netops /etc/nginx/sites-enabled/netops
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   ```
4. **Start the service:**
   ```bash
   sudo systemctl enable --now netops
   systemctl status netops
   journalctl -u netops -f                  # watch the log while you sign in
   ```
5. Open `https://netops.corp.local`, sign in with an AD account from `NetOps-Admins`,
   then check that a `NetOps-Viewers` account can sign in and can't change anything.
6. **Harden the server now:** work through [security-hardening.md](security-hardening.md)
   §3–§9 (SSH, firewall, kernel, accounts, logging). Do it before you add device credentials.
7. Add the credential profiles and devices. Bulk import:
   `sudo cp devices.csv /tmp/ && sudo netops-cli import-csv /tmp/devices.csv`.
8. **Check the firmware parsers** on one device of each platform and model:
   `sudo netops-cli firmware-check core-sw1 --raw`.

---

## 7. Upgrading NetOps Tools

1. Build a new bundle (step 3), transfer it and verify it (step 4).
2. `sudo bash netops-bundle-<new>/install.sh`

The installer takes a backup (`/var/backups/netops/netops-pre-upgrade-*.tar.gz`), stops
the service, switches to the new release and starts it again. If the new release doesn't
report healthy within 30 seconds, it **switches back automatically**. It keeps the last
three releases.

To roll back by hand:

```bash
ls /opt/netops/releases/
sudo ln -sfn /opt/netops/releases/<previous> /opt/netops/current
sudo systemctl restart netops
```

Settings, the key and the data are never touched by an upgrade.

---

## 8. Patching Ubuntu without internet

Security updates still matter on an air-gapped server: someone who gets onto the
network can exploit unpatched software. Patch at least monthly, and straight away for
critical advisories.

* **If you have an internal Ubuntu mirror** (Landscape, Nexus, Artifactory, apt-mirror),
  point `/etc/apt/sources.list.d/ubuntu.sources` at it and use
  `sudo apt update && sudo apt upgrade`.
* **Otherwise, use `apt-offline`** (it's in the bundle):
  ```bash
  # 1. On the server: make a request file listing what needs updating
  sudo apt-offline set /root/netops-install/updates.sig --update --upgrade
  # 2. Carry updates.sig to the build machine (Ubuntu 24.04 with internet):
  sudo apt install apt-offline
  apt-offline get updates.sig --bundle updates.zip --threads 4
  # 3. Scan updates.zip, carry it back, then on the server:
  sudo apt-offline install /root/netops-install/updates.zip
  sudo apt upgrade
  ```
  apt checks the Ubuntu signatures on every package, so the update files are verified.
* Reboot when a kernel or libc update needs it (`/var/run/reboot-required` exists).
  Then check `systemctl status netops nginx`.
* After patching, update the file-integrity baseline (hardening guide §10).

---

## 9. Backups and restore

* Every night at 02:30, `netops-backup` writes
  `/var/backups/netops/netops-nightly-<time>.tar.gz`. It keeps 14 days. It contains:
  * a consistent copy of the database;
  * the config archive (as a git bundle);
  * `netops.env`.
* The encryption key and firmware images are **left out** on purpose (see §6).
* **Copy `/var/backups/netops` off the server.** Your backup system should pull it, or
  you should copy it to backup media regularly. A backup only on the server itself
  doesn't protect against losing the server.
* Take a backup by hand with `sudo netops-backup manual`.

**Restore test:** do this after installing, then once a quarter.

```bash
sudo systemctl stop netops
sudo mkdir /root/restore && sudo tar -xzf /var/backups/netops/netops-nightly-<time>.tar.gz -C /root/restore
sudo install -o netops -g netops -m 0640 /root/restore/app.db /var/lib/netops/app.db
sudo rm -f /var/lib/netops/app.db-wal /var/lib/netops/app.db-shm
sudo mv /var/lib/netops/configs /var/lib/netops/configs.old
sudo install -o netops -m 0600 /root/restore/configs.gitbundle /var/lib/netops/configs.gitbundle
sudo -u netops git clone -q /var/lib/netops/configs.gitbundle /var/lib/netops/configs
sudo rm /var/lib/netops/configs.gitbundle
sudo systemctl start netops
```

On a **new** server: install the same bundle, put the saved `credential.key` in
`/etc/netops/` (`root:netops`, 0640), restore as above, then check that a backup of one
device still works. That proves the stored passwords can still be decrypted.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| `install.sh` says checksum mismatch | The bundle was damaged or changed. Get a fresh copy; don't install |
| Service won't start | `journalctl -u netops -n 50`. Common causes: a typo in `netops.env`, or the key file missing or unreadable |
| 502 Bad Gateway in the browser | The app isn't running: `systemctl status netops` |
| AD login fails | `journalctl -u netops` shows the LDAP error. Check the time (`chronyc tracking`), the CA file, the firewall rule to the DCs on 636 |
| Device backups time out | The firewall output rule to `DEVICE_NETS` on port 22; the device's SSH access list (security-hardening.md §11) |
| Something was blocked | `journalctl -k | grep nft-` shows dropped traffic in and out |
| Check the sandbox | `systemd-analyze security netops` (a lower score is better) |
