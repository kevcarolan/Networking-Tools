# NetOps Tools: design and roadmap

## Goal

A small, self-hosted **network operations platform**: one server, one web
address and one AD login, with a growing set of tools that share a common device inventory.
The first tool is **config backup** for about 100 Cisco IOS/NX-OS, Cisco Firepower and
Allied Telesis devices.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Host | Dedicated Linux VM (Ubuntu 24.04 / Debian 12) | The network automation libraries are Linux-first; Docker/systemd make it easy to run; the box holds device credentials, so it should be isolated; it can host future tools |
| Backend | Python + FastAPI | Same language as the device libraries (Netmiko); auto-generated API docs at `/docs` |
| Frontend | Browser-based HTML + plain JavaScript, no build step | Reachable from any PC, nothing to install; easy to maintain; can move to Vue/React later if the GUI grows |
| Data | SQLite in `data/` | Zero administration, a single file to back up; SQLAlchemy lets us move to PostgreSQL later without code changes |
| Config storage | Git repository in `data/configs/` | Full history, unchanged configs stored once, real diffs, browsable on disk |
| Device access | Netmiko over SSH | Supports all target platforms (`cisco_ios`, `cisco_nxos`, `cisco_asa`, `cisco_ftd`, `allied_telesis_awplus`) |
| Login | AD via LDAPS, roles from AD groups (nested groups supported) + break-glass local admin | No separate user database; access is managed where the rest of IT manages it |
| Secrets | Device passwords encrypted with Fernet; key file kept outside the data folder | A copied database alone doesn't expose credentials |

## Architecture

```
            Browser (any PC on the management network)
                              │ HTTPS
                    ┌─────────▼─────────┐
                    │  Caddy / nginx    │  TLS, single URL
                    └─────────┬─────────┘
   ┌──────────────────────────▼────────────────────────────────┐
   │ FastAPI app                                               │
   │  core/   auth (AD) · device inventory · credential        │
   │          profiles · platforms · audit log                 │  ◄── shared by every tool
   │  tools/config_backup/                                     │
   │          scheduler ─► worker pool (10) ─► Netmiko SSH     │──► switches / firewalls
   │          clean config ─► git commit ─► record result      │
   └───────────────┬──────────────────────────┬────────────────┘
             data/app.db (SQLite)      data/configs/ (git)
```

### Backup flow

1. The scheduler checks every 30 s for devices that are due (last attempt +
   frequency, or + 60 min retry if the last attempt failed).
2. Due devices go to a pool of 10 workers, so 100 devices at about 20 s each take roughly 3–4 minutes.
3. Each worker logs in over SSH, enters enable mode if needed, and runs `show running-config`.
4. Volatile lines (timestamps, `ntp clock-period`, ASA/FTD `Cryptochecksum`) are
   removed, so a version is only saved when the config actually changed.
5. Empty output or CLI errors are rejected, so a bad run never overwrites a good config.
6. The result is recorded: success/changed, or failure with a type (auth, unreachable,
   timeout, command, setup) and message shown in the GUI.

## Notes on Cisco Firepower

* **FTD managed by FMC:** the SSH `show running-config` backup captures the deployed
  (LINA) configuration, which is the right thing for "what is running on the box".
  Access policies, objects and Snort rules live in **FMC**, so FMC should also be
  backed up. A later driver can export them through the FMC REST API.
* **FTD managed locally (FDM):** SSH works the same way; the FDM API can be added later.
* **Firepower hardware running ASA software:** use the `cisco_asa` platform.

## Roadmap

**Phase 1: MVP (this commit)**
- Device inventory, credential profiles, per-device frequency, Backup now
- SSH collection for Cisco IOS/NX-OS/ASA/FTD and Allied Telesis AW+
- Git version archive, view/diff/download in the GUI
- Status dashboard with failure reasons, retry of failed devices
- AD login with admin/viewer roles, audit log, CSV import, Docker/systemd deployment

**Phase 2: operations**
- Email / Microsoft Teams alerts: device failing for more than N hours; config changed
- Daily summary report
- Backup of FMC through its REST API
- Scheduled off-box copy of `data/` (e.g. to a file share)

**Phase 3: more value from the configs**
- Compliance checks: NTP, SNMP, AAA/TACACS, banner and logging present on every device
- Search across all configs ("which devices have VLAN 30?")
- Firmware/version inventory report

**Phase 4: more tools on the same platform**
- "Where is this MAC/IP?" (MAC and ARP table lookup across switches)
- Reachability / interface status monitor
- Port and VLAN documentation export
