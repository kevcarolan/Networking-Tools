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
 │ Build machine            │   │ Approved media     │   │ NetOps server (Ubuntu 26.04) │
 │ (Ubuntu 26.04 VM / WSL)  │──►│ scanned per policy │──►│ verify SHA-256 ─► install.sh │
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
| Server | A VMware VM: 2 vCPU, 4 GB RAM, a 30 GB system disk, and a separate **100 GB data disk** (firmware images). x86_64. vCenter with a key provider for VM Encryption (the built-in Native Key Provider is enough) |
| Operating system | Ubuntu Server 26.04 LTS ISO. The bundle must be built on the **same Ubuntu release** as the server; 24.04 works too, if both sides use it |
| Build machine | Ubuntu 26.04 (the server's release) with internet access and sudo: a VM, Windows WSL (`wsl --install -d Ubuntu-26.04`), or an `ubuntu:26.04` Docker container. Use a clean, patched machine that is used only for this |
| DNS name | e.g. `netops.corp.local`, with a DNS record on the air-gapped network |
| TLS certificate | Issued by your internal CA for that name, with the key. PEM format; include any intermediate CA in the certificate file |
| AD | Two groups: `NetOps-Admins` and `NetOps-Viewers`. The CA certificate that signed the domain controllers' LDAPS certificates |
| Network facts | For the firewall: admin/jump-host subnets, the subnets allowed to use the GUI, device management subnets, and the DC, DNS, NTP and syslog addresses |
| Monitoring | The PRTG probe's address, and PRTG access to vCenter ([monitoring-prtg.md](monitoring-prtg.md)) |
| People | Named admin accounts for the server (no shared logins), each with an SSH key. Hardware keys (YubiKey or similar) if you have them |

---

## 2. Build the server (operating system)

1. **Create the VM in vCenter.** The details are in security-hardening.md §2:
   * Guest OS: Ubuntu Linux (64-bit). **EFI firmware with Secure Boot**, and a **vTPM**.
   * Two disks on a PVSCSI controller: 30 GB for the system and 100 GB for data.
     VMXNET3 network adapter on the management port group.
   * Apply the **VM Encryption Policy** to the VM and both disks.
   * Remove the floppy, USB and sound devices. Set the advanced settings from §2. Untick
     *Synchronize guest time with host*.
2. **Install Ubuntu Server 26.04:**
   * Choose **Ubuntu Server (minimized)**.
   * Storage: use LVM on the 30 GB disk. Leave LUKS off: vSphere VM Encryption already
     encrypts the disks, without a passphrase at every boot.
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

You don't download packages one at a time. The build script downloads everything the
server needs, including every dependency, and packs it into one file.

### On a Windows PC (WSL)

Use an internet-connected PC that you're allowed to use for this.

1. **Use Ubuntu under WSL, on the same release as the server.** Check what you have,
   in PowerShell: `wsl -l -v`, then inside Ubuntu: `grep PRETTY /etc/os-release`. If it
   isn't the server's release, install that release (PowerShell as administrator, then reboot):
   ```powershell
   wsl --install -d Ubuntu-26.04
   ```
   Open it from the Start menu and create a user name and password when asked.
   They're local to WSL.
2. **Get the code and build.** In the Ubuntu window:
   ```bash
   sudo apt update && sudo apt install -y git
   git clone https://github.com/kevcarolan/Networking-Tools.git
   cd Networking-Tools
   sudo MAKE_ISO=1 deploy/airgap/build-bundle.sh
   ```
3. **Find the output.** In Windows Explorer, open
   `\\wsl$\<distro name from wsl -l>\home\<your WSL user>\Networking-Tools\dist\`.
   You can also type `explorer.exe dist` in the Ubuntu window.

### On an Ubuntu VM (same release as the server), or with Docker

```bash
git clone https://github.com/kevcarolan/Networking-Tools.git && cd Networking-Tools
sudo MAKE_ISO=1 deploy/airgap/build-bundle.sh
# or, on any machine with Docker:
docker run --rm -e MAKE_ISO=1 -v "$PWD":/src -w /src ubuntu:26.04 deploy/airgap/build-bundle.sh
```

### What you get

The build takes a couple of minutes. It writes these files to `dist/`:

| File | What it is |
|---|---|
| `netops-bundle-<date>-<commit>.tar.gz` | The bundle (about 90 MB): app, Python packages, Ubuntu packages, checksums |
| `….tar.gz.sha256` | Its SHA-256 |
| `….MANIFEST.txt` | **The list of every package and version in the bundle, with the vulnerability report.** Attach it to the change ticket |
| `….iso` and `….iso.sha256` | With `MAKE_ISO=1`: a CD image holding the three files above, for vSphere (§4) |

The build:

* runs **pip-audit** and stops if any Python package has a known vulnerability;
* downloads the Ubuntu packages and their full dependency tree (about 170 packages), so
  the result doesn't depend on what is installed on the build machine. With an internal
  Ubuntu mirror you can set `SKIP_DEBS=1`;
* prints the **SHA-256** of the bundle. Record it in the change ticket, or send it by a
  different channel from the files. On the server it proves the file wasn't changed in transit.

---

## 4. Transfer and verify

1. Copy the files from `dist/` to the approved transfer media, and scan them at your
   media-scanning station as your policy requires.
2. Get them onto the server in one of these ways.

   **A. As a CD image through vSphere (easiest for a VM):**
   1. In the vSphere Client, go to **Storage** → the VM's datastore → **Files** →
      **Upload Files**, and upload the `.iso`.
   2. In **Edit Settings** for the VM, set **CD/DVD drive 1** to *Datastore ISO File*,
      choose the ISO, and tick **Connected**. If you removed the CD drive after
      installing Ubuntu, add one back for now.
   3. On the server:
      ```bash
      sudo mount -o ro /dev/sr0 /mnt
      sudo install -d -m 0700 /root/netops-install
      sudo cp /mnt/netops-bundle-* /root/netops-install/
      sudo umount /mnt
      ```
   4. Disconnect the ISO in the VM settings, and delete it from the datastore when
      you've finished.

   **B. From an admin machine on the air-gapped network** that the files have already
   reached:
   ```bash
   scp netops-bundle-*.tar.gz* <you>@<server>:/tmp/
   # then on the server:
   sudo install -d -m 0700 /root/netops-install && sudo mv /tmp/netops-bundle-* /root/netops-install/
   ```
3. **Verify, on the server.** The folder is readable only by root, so open a root shell first:
   ```bash
   sudo -i                                 # root shell; type "exit" when finished
   cd /root/netops-install
   sha256sum netops-bundle-*.tar.gz        # must match the value in the change ticket
   sha256sum -c netops-bundle-*.tar.gz.sha256
   ```
   **If the hash doesn't match, stop.** Don't extract the file.

---

## 5. Install

In the root shell from §4 (or `sudo -i` again):

```bash
cd /root/netops-install
tar -xzf netops-bundle-*.tar.gz
bash netops-bundle-*/install.sh
exit                                    # leave the root shell
```

If there is more than one bundle in the folder, use the full file names instead of `*`.

The installer:

1. checks every file in the bundle against its `SHA256SUMS`;
2. installs the Ubuntu packages from the bundle. It adds only what is missing and
   never downgrades anything:
   * `python3-venv`, `git`, `sqlite3`, `nginx`;
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

These steps match the numbered **Next steps** that `install.sh` prints at the end.

| Step | What | When |
|---|---|---|
| 1–3 | Settings, certificate and nginx, start the service | **Production**: the real server |
| 3A | Quick test: local admin, self-signed certificate, no AD | **Test machines only**, instead of 1–3 |
| 4 | Harden the server | **Production**, before you add real device credentials |
| 5–6 | Add devices; check the firmware parsers | After 3 (or 3A) |

**Before anything else, save the encryption key offline.** Without
`/etc/netops/credential.key`, the stored device passwords can't be decrypted. It is
deliberately **not** in the nightly backups. Copy it to your password vault, or to an
encrypted USB key kept in a safe:
```bash
sudo cat /etc/netops/credential.key
```

### 1. Settings

`sudo nano /etc/netops/netops.env`

* `NETOPS_LDAP_URL`, `NETOPS_LDAP_DOMAIN`, `NETOPS_LDAP_BASE_DN` and the two group DNs.
  Always set `NETOPS_LDAP_VIEWER_GROUP`; if it's empty, **every** domain user can sign in.
* Copy the AD CA certificate to `/etc/netops/ad-ca.pem` (`chmod 0644`) and set
  `NETOPS_LDAP_CA_FILE=/etc/netops/ad-ca.pem`.
* **Break-glass admin:** leave `NETOPS_LOCAL_ADMIN_PASSWORD_HASH` empty unless you want
  a login that works when AD is down. If you do, use a long random password kept in the
  vault. Generate the hash with `sudo netops-cli hash-password`.
* Keep `NETOPS_SESSION_HTTPS_ONLY=true`, `NETOPS_API_DOCS=false` and
  `NETOPS_VIEWERS_CAN_READ_CONFIGS=false`.

### 2. TLS certificate and nginx

```bash
sudo install -m 0600 netops.crt netops.key /etc/netops/tls/     # full chain in netops.crt
sudo nano /etc/nginx/sites-available/netops                     # set server_name
sudo ln -sf /etc/nginx/sites-available/netops /etc/nginx/sites-enabled/netops
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 3. Start the service and sign in

```bash
sudo systemctl enable --now netops
systemctl status netops --no-pager       # should say "active (running)"
journalctl -u netops -f                  # watch the log while you sign in (Ctrl+C to stop)
```

Open `https://netops.corp.local`. Sign in with an AD account from `NetOps-Admins`, then
check that a `NetOps-Viewers` account can sign in but can't change anything.

### 3A. Quick local test (temporary; test machines only, instead of 1–3)

Use this to see the app running on a test machine before AD and a CA certificate are
ready. It signs in with the local admin and uses a self-signed certificate. **Don't use
it on the production server.** There, do steps 1–3.

```bash
# Turn AD off, and create a local admin login. Do the login part in a root shell,
# so only one thing asks for a password at a time. It asks twice; use 12+ characters.
sudo sed -i 's|^NETOPS_LDAP_URL=.*|NETOPS_LDAP_URL=|' /etc/netops/netops.env
sudo sed -i '/^NETOPS_LOCAL_ADMIN_PASSWORD_HASH=/d' /etc/netops/netops.env
sudo -i
netops-cli hash-password >> /etc/netops/netops.env
grep LOCAL_ADMIN /etc/netops/netops.env      # one line starting NETOPS_LOCAL_ADMIN_PASSWORD_HASH='scrypt$
exit

# Temporary self-signed certificate (browsers will warn about it)
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 90 -subj "/CN=$(hostname)" \
  -addext "subjectAltName=DNS:$(hostname),DNS:localhost" \
  -keyout /etc/netops/tls/netops.key -out /etc/netops/tls/netops.crt
sudo chmod 600 /etc/netops/tls/netops.key

# Turn on the website and start the app
sudo ln -sf /etc/nginx/sites-available/netops /etc/nginx/sites-enabled/netops
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable --now netops
systemctl status netops --no-pager
```

Open **https://localhost** on the machine (or `https://<its IP>` from another PC), accept
the certificate warning, and sign in as **admin**. If the service shows **failed**, check
`journalctl -u netops -n 30 --no-pager`.

To move from 3A to a real setup later: do step 1 (which also replaces the test admin
hash), then step 2 with the CA certificate, then `sudo systemctl restart netops`.

### 4. Harden the server

Work through [security-hardening.md](security-hardening.md) §3–§9: SSH, firewall,
kernel, accounts, logging. Do it on the production server **before** you add real device
credentials. Then set up the PRTG sensors ([monitoring-prtg.md](monitoring-prtg.md)).
On an internet-connected test machine, leave out the firewall (§5): it blocks outbound
traffic, including internet access.

### 5. Add credential profiles and devices

In the GUI, or in bulk:
`sudo cp devices.csv /tmp/ && sudo netops-cli import-csv /tmp/devices.csv`

### 6. Check the firmware parsers

On one device of each platform and model:
`sudo netops-cli firmware-check core-sw1 --raw`

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

For now, patch with **`apt-offline`**. Once testing and sign-off are done, the plan is to
connect the server to an internal Ubuntu mirror, which makes this a normal `apt upgrade`.

* **With an internal Ubuntu mirror (later)** (Landscape, Nexus, Artifactory, apt-mirror),
  point `/etc/apt/sources.list.d/ubuntu.sources` at it and use
  `sudo apt update && sudo apt upgrade`.
* **With `apt-offline` (now).** It's in the bundle:
  ```bash
  # 1. On the server: make a request file listing what needs updating
  sudo apt-offline set /root/netops-install/updates.sig --update --upgrade
  # 2. Carry updates.sig to the build machine (same Ubuntu release, with internet):
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
