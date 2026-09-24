# Project progress

The single place to see where NetOps Tools is and what's next. Update it at the end of
every working session. The details of each item are in [design.md](design.md) and
[firmware.md](firmware.md).

**Now:** Firmware phase 1 is built and waiting to be merged.
**Next:** Merge it, check the version parsers against real devices, then start firmware phase 2 (staging and pre-checks).

## Roadmap

### Platform
- [x] Shared core: AD login with admin/viewer roles, device inventory, encrypted credential profiles, audit log
- [x] Docker + Caddy and systemd deployment
- [x] Shared SSH module (`core/ssh.py`) used by every tool
- [ ] Deploy to the production VM (about 100 GB of disk once firmware images are stored)
- [x] Renamed the repository to `Networking-Tools`
- [ ] Add database migrations (Alembic) before the first change to an existing table

### Config Backup
- [x] **MVP:** scheduled SSH backups, git version archive, view/diff/download, failure reasons, retries, CSV import
- [ ] **Phase 2:** email/Teams alerts (failing for N hours, config changed), daily summary
- [ ] **Phase 2:** scheduled off-box copy of `data/`
- [ ] **Phase 3:** compliance checks (NTP, SNMP, AAA, banner, logging)
- [ ] **Phase 3:** search across all configs

### Firmware
- [x] **Phase 1:** version report (model, serial, version, install/bundle mode, failover role, flash)
- [x] **Phase 1:** standards (approved version per platform and model), on standard / behind / ahead, CSV export
- [x] **Phase 1:** image library with streamed upload and MD5/SHA-512 verification
- [x] **Phase 1:** `app.cli firmware-check <device> --raw` for testing the parsers
- [ ] **Phase 1 sign-off:** run `firmware-check --raw` on one device per platform and model; fix any parser that is wrong
- [ ] **Phase 2:** separate upgrade credential per device
- [ ] **Phase 2:** pre-check job (dry run): reachability, model/image match, flash space, unsaved config, HA health
- [ ] **Phase 2:** stage job: SCP push (Cisco) / HTTP pull (AW+), on-device checksum check
- [ ] **Phase 2:** capture "before" state (interfaces, neighbours, port-channels, routing, failover)
- [ ] **Phase 3:** upgrade jobs with maintenance window, waves, failure limit, peer groups
- [ ] **Phase 3:** automatic config backup before and after each upgrade, post-checks and diff
- [ ] **Phase 3:** IOS-XE install mode, IOS classic and AW+ upgrades
- [ ] **Phase 3:** NX-OS (`install all`) upgrades
- [ ] **Phase 3:** ASA failover pair (standby first, fail over, then the other unit)
- [ ] **Phase 3:** FTD via the FDM REST API
- [ ] **Phase 3:** separate upgrade worker process; `NetOps-Upgraders` AD group; optional second-person approval
- [ ] **Phase 3:** lab rehearsal of every upgrade path before production use
- [ ] **Phase 4:** alerts, scheduled jobs, Cisco PSIRT advisories, end-of-life dates

### Later tools (ideas)
- [ ] "Where is this MAC/IP?" lookup
- [ ] Reachability and interface status monitor
- [ ] Port and VLAN documentation export

## Decisions

| Date | Decision | Why |
|---|---|---|
| 2026-09 | One platform for all tools, one shared database | One device inventory, one login, and upgrades can call the backup tool |
| 2026-09 | Python + FastAPI, plain JavaScript GUI, SQLite | Simple to run and maintain; can move to PostgreSQL later |
| 2026-09 | Firmware images on disk, not in the database | Images are up to 2 GB each |
| 2026-09 | FTD upgrades through the FDM REST API | FTD is managed by FDM, not FMC |
| 2026-09 | IOS-XE upgrades use install mode commands | The switches run in install mode |
| 2026-09 | Upgrade worker will be a separate process | A GUI restart must never interrupt a reload |

## Open questions

- Is the failover pair ASA or FTD (FDM HA)? The plan currently assumes ASA.
- Are there any switch stacks (Cat9k StackWise, AW+ VCStack) or NX-OS vPC pairs? These affect the upgrade order.
- Where will the production VM run, and has its disk been sized for firmware images?

## Session log

| Date | Where | What happened |
|---|---|---|
| 2026-09 | Backup chat | Designed the platform and built the Config Backup MVP (merged to `main`) |
| 2026-09-24 | Firmware chat | Planned the firmware tool; built phase 1 on branch `claude/device-firmware-upgrade-app-yey88v`; added `CLAUDE.md` and this file; repository renamed to `Networking-Tools` |
