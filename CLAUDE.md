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
| Firmware (version report, image library, later upgrades) | `backend/app/tools/firmware_upgrade/` | Phase 1 done, phase 2 next |

Design docs: [docs/design.md](docs/design.md) (platform and backup tool),
[docs/firmware.md](docs/firmware.md) (firmware tool and upgrade plan).

## The network it manages

* About 100 devices: Cisco IOS/IOS-XE, NX-OS, ASA, Firepower FTD and Allied Telesis AlliedWare Plus.
* IOS-XE switches run in **install mode**.
* FTD is managed locally with **FDM** (not FMC), so FTD upgrades go through the FDM REST API.
* There is **one failover pair**, assumed to be ASA. Confirm with the user before building HA upgrades.
* Login is Active Directory over LDAPS, with admin and viewer groups plus a break-glass local admin.

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
