# NetOps Tools: notes for Claude

Read this first in every session, then read [docs/PROGRESS.md](docs/PROGRESS.md) to see
where the project is and what comes next.

## What this is

A self-hosted web platform for network operations, run by one network team. One server,
one web address, one AD login, and a growing set of tools that share a device inventory
and one database.

| Tool | Folder | Status |
|---|---|---|
| Config Backup | `backend/app/tools/config_backup/` | MVP done |
| Firmware (version report, image library, upgrade jobs) | `backend/app/tools/firmware_upgrade/` | Built for every platform; in lab testing ([docs/upgrades.md](docs/upgrades.md), [docs/upgrade-lab-tests.md](docs/upgrade-lab-tests.md)) |
| Circuits (master circuit list from Excel) | `backend/app/tools/circuits/` | Done ([docs/circuits.md](docs/circuits.md)) |

Design docs: [docs/design.md](docs/design.md) (platform and backup tool),
[docs/firmware.md](docs/firmware.md) (firmware tool and upgrade plan).

## The network it manages

* About 100 devices: Cisco IOS/IOS-XE, NX-OS, ASA, Firepower FTD and Allied Telesis AlliedWare Plus.
* IOS-XE switches run in **install mode**.
* FTD is managed locally with **FDM** (not FMC), so FTD upgrades go through the FDM REST API.
* There is **one failover pair: FTD in FDM HA**. ASA failover is supported too; the user wants the
  tool to fit other designs, so procedures are drivers with selectable paths.
* Login is Active Directory over LDAPS, with admin and viewer groups plus a break-glass local admin.
* The production server is on an **air-gapped** network: **Ubuntu 26.04** (Python 3.14, nginx 1.28,
  sudo-rs, Rust coreutils), systemd and nginx, installed
  from an offline bundle (`deploy/airgap/`, [docs/install-airgap.md](docs/install-airgap.md)).
  Nothing can be downloaded on the server, so any new Python or Ubuntu dependency must work
  with `build-bundle.sh` (wheels only, no compiling) and be added to it. The bundle is built for the
  build machine's Ubuntu release and installs only on that release. Security is a priority:
  see [docs/security-hardening.md](docs/security-hardening.md) before changing deployment,
  ports or outbound connections (the host firewall blocks outbound traffic by default).
* The server is a **VMware vSphere** VM (VM Encryption, vTPM). There is no internal Ubuntu mirror
  yet, so the OS is patched with apt-offline; a mirror will follow after sign-off.
* Monitoring is **PRTG**: it polls `/api/monitoring/prtg` and receives syslog. Keep that endpoint's
  channel names stable, because PRTG keys its channels and alert limits on them.

## Code layout and conventions

* Backend: Python 3.11+, FastAPI, SQLAlchemy 2, Netmiko. Settings are env vars prefixed
  `NETOPS_` (`backend/app/core/config.py`, documented in `.env.example`).
* `core/` is shared by every tool: auth, inventory, credentials, platforms, SSH (`core/ssh.py`),
  audit log. Tools live in `tools/<name>/` with `models.py`, `service.py`, `api.py`.
* **Database:** SQLite `data/app.db`, created with `create_all` and no migration tool. New
  tables appear automatically, but **a new column on an existing table does not**. Add a new
  table (prefixed with the tool, e.g. `fw_`), or introduce Alembic first.
* Frontend: plain HTML/CSS/JS in `backend/app/static/`, no build step. Use the `h()` and
  `api()` helpers in `app.js`, keep styles on the CSS variables in `style.css` (light and dark),
  and hide admin-only controls with `data-admin`.
* Anything that changes something is admin-only and is written to the audit log with `audit()`.
  Upgrade job actions need the **upgrader** permission instead (`require_upgrader`, `User.can_upgrade`;
  hide those controls with `data-upgrader`).
* Upgrade jobs:
  * checks and parsers in `checks.py`; one **driver** per platform in `drivers/` (paths, extra
    pre-checks, the procedure as `Step`s); the engine in `jobs.py` (`JobService`, `allowed_actions`);
    device access in `deviceio.py` (SSH change/copy) and `fdm.py` (FDM API);
  * the web app only **queues** requests (`request_action`); the **worker** (`app/worker.py`,
    `netops-worker.service`) runs them. Never talk to a device for a job from the web process;
  * a dry run and a live run use the same steps: only `StepContext.change/transfer/fdm_change`
    differ. Live needs `NETOPS_UPGRADE_LIVE_PLATFORMS`, a window and the typed device name;
  * tests drive every path against `tests/fake_network.py`. Extend the simulator with any new
    command a driver sends.
* Device access is always injectable (`fetcher=` / `fw_collector=` in `create_app`), so tests
  never touch SSH. Parser tests use real sample output in `backend/tests/firmware_samples.py`.

## Working rules

* Run the tests before every commit: `cd backend && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pytest -q`.
  All must pass.
* Work on a feature branch and never push to `main`. The user merges through pull requests.
* Nothing that changes a device (copy, boot, reload, install) runs without an explicit
  admin action, a maintenance window and pre-checks. See the safety rules in `docs/firmware.md`.
* **At the end of every session, update `docs/PROGRESS.md`:** tick finished items, add a
  line to the session log, and record new decisions and open questions.
* Keep the docs in plain language. The users are network engineers, not developers.
