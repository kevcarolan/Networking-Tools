# NetOps Tools

A self-hosted web platform for network operations tools. Two tools share one
device inventory, one AD login and one database:

* **Config Backup** pulls running configurations from switches and firewalls
  on a schedule, keeps every version in a git archive, and shows in the browser
  which devices are backing up and which are failing, and why.
* **Firmware** shows which software version every device runs compared with the
  approved version for its model, and keeps a checksummed library of firmware images.
  Staging and upgrades come in later phases.

See [docs/design.md](docs/design.md) for the platform design and roadmap, and
[docs/firmware.md](docs/firmware.md) for the firmware tool's design and upgrade plan.

| Supported platform | Key | Method |
|---|---|---|
| Cisco IOS / IOS-XE | `cisco_ios` | SSH `show running-config` |
| Cisco NX-OS | `cisco_nxos` | SSH `show running-config` |
| Cisco ASA / Firepower in ASA mode | `cisco_asa` | SSH `show running-config` |
| Cisco Firepower Threat Defense | `cisco_ftd` | SSH `show running-config` (FTD CLI) |
| Allied Telesis AlliedWare Plus | `allied_awplus` | SSH `show running-config` |

## What it does

* Device list with status (OK / failing / never backed up / disabled), last
  success, last config change, and the failure reason (auth, unreachable,
  timeout, bad command, missing credential).
* Add / edit / delete devices, set the backup frequency per device, "Backup now".
* Every config version is stored in `data/configs/` (a git repo, one file per
  device: `<site>/<device>.cfg`). View any version, see what changed, download it.
* Credential profiles (e.g. `switch-backup`, `firewall-backup`) shared by many
  devices, encrypted at rest.
* Sign in with Active Directory. Members of the admin group can make changes;
  members of the viewer group get read-only access. A local break-glass admin
  covers AD outages.
* Audit log of logins and every change.
* Failed devices are retried hourly (configurable) rather than waiting for the next scheduled run.

**Firmware**

* Version report: model, serial, software version, install/bundle mode, failover role
  and free flash for every device, read over SSH daily (or with **Check now**).
* Standards: the approved version per platform and model pattern (e.g. `C9300-*`).
  Each device shows as on standard, behind, ahead, or no standard set. CSV export.
* Image library: upload firmware images (streamed to disk, MD5/SHA-512 computed
  and optionally checked against the vendor's checksum), and link them to standards.

## Quick start (development / trial)

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# Create a local admin login
python -m app.cli hash-password            # prints NETOPS_LOCAL_ADMIN_PASSWORD_HASH=...
export NETOPS_LOCAL_ADMIN_PASSWORD_HASH='scrypt$...'

uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000
```

Open `http://<server>:8000`, sign in as `admin`, add a credential profile under
**Credentials**, then add devices (or bulk-import them, see below).

Run the tests with `pytest` from `backend/`.

## Production install (Linux server, Docker)

Recommended host: a small Ubuntu 24.04 / Debian 12 VM (2 vCPU, 4 GB RAM; 20 GB of disk is plenty
for backups of a few hundred devices, but allow about 100 GB if you keep firmware images on it) that can reach the devices' management addresses on TCP/22.

```bash
git clone <this repo> /opt/netops && cd /opt/netops
cp .env.example .env && nano .env                    # AD settings, admin hash, etc.
mkdir -p deploy/secrets && sudo chown 1000 deploy/secrets
cp /path/to/corp-root-ca.pem deploy/secrets/         # CA for LDAPS; set NETOPS_LDAP_CA_FILE=/secrets/corp-root-ca.pem
nano deploy/Caddyfile                                # set the server's DNS name
docker compose -f deploy/docker-compose.yml up -d --build
```

Caddy serves the app over HTTPS on port 443. **Back up `deploy/secrets/credential.key`
somewhere safe.** Without it, the stored device passwords cannot be decrypted.

### Install without Docker (systemd)

```bash
sudo useradd --system --home /opt/netops netops
sudo git clone <this repo> /opt/netops
sudo python3 -m venv /opt/netops/venv
sudo /opt/netops/venv/bin/pip install -r /opt/netops/backend/requirements.txt
sudo mkdir -p /etc/netops /opt/netops/data && sudo cp .env.example /etc/netops/netops.env
# edit /etc/netops/netops.env, set NETOPS_DATA_DIR=/opt/netops/data
sudo chown -R netops: /opt/netops/data /etc/netops
sudo cp deploy/netops.service /etc/systemd/system/ && sudo systemctl enable --now netops
```

Put nginx or Caddy in front for HTTPS.

## Active Directory setup

1. Create two AD groups, e.g. `NetOps-Admins` and `NetOps-Viewers`. Nested groups work.
2. Set `NETOPS_LDAP_URL` (use `ldaps://`; `ldap://` is upgraded with StartTLS),
   `NETOPS_LDAP_DOMAIN`, `NETOPS_LDAP_BASE_DN` and the two group DNs.
3. Users sign in with their normal username (`jsmith`), `jsmith@corp.local` or `CORP\jsmith`.
   The app binds as the user, so no AD service account is needed.

Leave `NETOPS_LDAP_VIEWER_GROUP` empty to give every domain user read-only access.

## Device credentials: recommendations

* Use a dedicated, **read-only** backup account on the devices, ideally through
  TACACS+/RADIUS so it is audited and easy to rotate.
  The account needs enough privilege to run `show running-config` (on IOS and
  AlliedWare Plus that normally means privileged/enable mode; supply an enable
  secret in the credential profile if the account doesn't land there directly).
  Test each platform with `python -m app.cli backup <device>` before rolling out.
* Stored configs contain password hashes, SNMP communities and VPN keys, so only admins can
  view them in the GUI unless `NETOPS_VIEWERS_CAN_READ_CONFIGS=true`.

## Bulk import devices

```bash
cd backend
python -m app.cli import-csv ../docs/devices-example.csv
```

The CSV columns are `name,address,platform,site,credential,frequency_minutes,notes`.
`credential` must be the name of an existing credential profile. Existing names are skipped.

Test a single device from the command line: `python -m app.cli backup core-sw1`.
Check what the firmware tool reads from a device: `python -m app.cli firmware-check core-sw1 --raw`.

## Where things are stored

```
data/
├── app.db            SQLite: devices, credentials (encrypted), schedules, run history, audit log, firmware (fw_*)
├── configs/          git repository of every config version (browse with any git tool)
├── firmware/         uploaded firmware images
├── credential.key    encryption key (move it outside data/ in production!)
└── session.key       signs login cookies
```

Back up the whole `data/` folder and the credential key (`data/firmware/` can be left out:
the images can be downloaded from the vendor again). Deleting a device removes it
from the GUI, but its config history stays in `configs/`.

## Project layout

```
backend/app/
├── core/                    shared by all tools
│   ├── auth.py              AD/LDAP + local admin login, roles
│   ├── inventory.py         devices & credential profiles API
│   ├── platforms.py         supported vendors (add new ones here)
│   ├── ssh.py               SSH login, show commands, error classification (Netmiko)
│   ├── models.py, db.py, crypto.py, config.py, audit.py
├── tools/config_backup/     the config backup tool
│   ├── collector.py         SSH collection (Netmiko), config cleaning, error classification
│   ├── storage.py           git config archive
│   ├── service.py           scheduler + worker pool
│   └── api.py               HTTP API
├── tools/firmware_upgrade/  the firmware tool
│   ├── facts.py             show version parsers per platform, version comparison
│   ├── images.py            image library on disk (streamed upload + checksums)
│   ├── service.py           scheduled version checks
│   └── api.py               HTTP API
├── static/                  web GUI (plain HTML/CSS/JavaScript, no build step)
├── cli.py                   admin commands
└── main.py                  app factory
deploy/                      Dockerfile, docker-compose, Caddyfile, systemd unit
```
