# Project progress

The single place to see where NetOps Tools is and what's next. Update it at the end of
every working session. The details of each item are in [design.md](design.md) and
[firmware.md](firmware.md).

**Now:** The whole upgrade tool is built (worker, staging, live procedures for every platform and path) and is in PR #4, unmerged. Lab testing on the test VM, platform by platform: [upgrade-lab-tests.md](upgrade-lab-tests.md). We iterate on the branch from the results, then merge.
**Next:** After lab sign-off: merge, build the production server, enable live upgrades one platform at a time (`NETOPS_UPGRADE_LIVE_PLATFORMS`).

## Roadmap

### Platform
- [x] Shared core: AD login with admin/viewer roles, device inventory, encrypted credential profiles, audit log
- [x] Docker + Caddy and systemd deployment
- [x] Shared SSH module (`core/ssh.py`) used by every tool
- [x] Air-gapped install kit: offline bundle build (pip-audit, pinned wheels, local apt repo, checksums), installer with backup and automatic rollback, hardened systemd service, nginx, nightly backups
- [x] Security hardening guide and templates (nftables in/out, SSH, sysctl, auditd, AIDE, logging, device access)
- [x] Test install on an Ubuntu 26.04 VM from the bundle: app running behind nginx (local admin, self-signed certificate)
- [ ] Build the production VM and install from the bundle (30 GB system disk + 100 GB data disk)
- [ ] Complete the hardening checklist in security-hardening.md and sign it off
- [x] PRTG integration: health endpoint (`/api/monitoring/prtg`), syslog forwarding template, sensor guide
- [ ] Set up the PRTG sensors and syslog alerts ([monitoring-prtg.md](monitoring-prtg.md))
- [ ] After sign-off: connect the server to the internal Ubuntu mirror (replaces apt-offline)
- [ ] First restore test from a nightly backup
- [ ] GitHub Actions: run the tests (and a bundle build) on every pull request
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
- [x] **Phase 2:** separate upgrade credential per device (PR 2)
- [x] **Phase 2:** pre-check job (dry run): reachability, model/image match, flash space, unsaved config, HA health (PR 2)
- [x] **Phase 2:** stage job: SCP push (all CLI platforms, incl. AW+), on-device checksum check (MD5; size on AW+)
- [x] **Phase 2:** capture "before" state: interfaces, neighbours, port-channels, MAC count, stack (PR 2; routing still to do)
- [x] **Phase 3:** upgrade jobs with a maintenance window (live steps only inside it), HA pairs as one job (peer device). Waves/failure limits dropped: one device or pair per job, by agreement
- [x] **Phase 3:** automatic config backup before and after each upgrade, post-checks against the before snapshot
- [x] **Phase 3:** IOS-XE install mode (commit after post-checks, auto-abort timer), IOS bundle mode and AW+ upgrades
- [x] **Phase 3:** NX-OS (`install all`), standalone and vPC pair
- [x] **Phase 3:** ASA standalone and failover pair (standby first, fail over, then the other unit)
- [x] **Phase 3:** FTD via the FDM REST API (pinned certificate), standalone and FDM HA pair
- [x] **Phase 3:** separate upgrade worker process (`netops-worker`); upgrader AD group. Second-person approval dropped by agreement
- [ ] **Phase 3:** lab rehearsal of every upgrade path before production use ([upgrade-lab-tests.md](upgrade-lab-tests.md))
- [ ] **Phase 4:** alerts, scheduled jobs, Cisco PSIRT advisories, end-of-life dates

### Circuits
- [x] **PR 1:** master circuit list tool: Excel upload with preview (new/changed/removed, warnings,
      unlinked switches), versioned imports, switch/port linking, search, CSV export, device Circuits tab
- [ ] Upload the real circuit list on the test VM and review the unlinked switches
- [ ] NetBox sync, once NetBox is in the closed environment

### Upgrade programme (agreed plan, 2026-10-01)
Manual firmware repository by vendor. One device per job, started by an engineer in the
change window, with no approval step. The MD5 is checked in the repository and on the
device. A blocking pre-check can be fixed and re-checked, or overridden with a reason.
After a failed post-check the engineer can fix it and re-check, override it with a note,
or roll back. A full report is kept as the device's upgrade history, including the
affected circuits.
- [x] PR 1: circuit list (above)
- [x] PR 2: vendor repository, upgrade jobs, pre-checks, reports and history (Start is a dry run) ([upgrades.md](upgrades.md))
- [ ] PR 2 sign-off: dry run against a real lab switch; check the new parsers (alarms, environment, install summary, show switch) on real output
- [x] Part 3: upgrade worker process, staging and on-device MD5 check
- [x] Part 4: IOS-XE install-mode upgrade, post-checks, re-check/override, rollback
- [x] Part 5: NX-OS (+ vPC), IOS bundle, AW+, ASA (+ failover pair), FTD (+ FDM HA) through a driver framework with a choice of path per job
- [ ] Lab sign-off of every platform and path, then merge PR #4 (the user tests the full build first)

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
| 2026-09-25 | Production runs on an air-gapped network: systemd + nginx from an offline bundle, not Docker | No image registry to pull from; fewer packages and no Docker daemon to secure; nginx is patched with the normal Ubuntu updates |
| 2026-09-25 | Outbound traffic from the server is blocked by default | The server holds credentials for every device, so a compromise must not spread |
| 2026-09-25 | The credential key is kept out of backups and stored offline | A stolen backup can't be used to decrypt device passwords |
| 2026-10-01 | App's internal port is 127.0.0.1:8710, not 8000 | 8000 clashed with NetBox on the test VM; the installer updates old nginx sites itself |
| 2026-10-01 | Server and build machine run Ubuntu 26.04 LTS; the kit builds for whichever release it runs on | Matches the installed server; 26.04 is supported for longer |
| 2026-09-25 | VMware vSphere with VM Encryption (EFI, Secure Boot, vTPM); no LUKS | Disks and snapshots encrypted without a passphrase at every boot |
| 2026-09-25 | Patch with apt-offline until an internal Ubuntu mirror is connected after sign-off | No mirror exists yet |
| 2026-10-02 | Upgraders are an AD group (`NETOPS_LDAP_UPGRADER_GROUP`) on top of admin/viewer; no approval step | The engineer runs their own change; the job enforces the checks instead |
| 2026-10-02 | Overrides only carry forward for an identical failure | A new alarm or fault must be looked at again, not waved through |
| 2026-10-02 | Devices with upgrade history and images used by jobs can't be deleted | The upgrade record must stay complete |
| 2026-10-02 | Build the whole upgrade tool before testing; test and iterate on the branch, merge after sign-off | The user wants to test the complete procedure, not stages |
| 2026-10-02 | IOS-XE: activate with an auto-abort timer, commit only after the post-checks pass or are overridden | A bad upgrade can be undone cleanly with `install abort`, and reverts by itself if nobody decides |
| 2026-10-02 | AW+ images pushed over SCP from NetOps (not pulled by the switch) | Keeps the server outbound-only; no new inbound rule |
| 2026-10-02 | Live upgrades enabled per platform (`NETOPS_UPGRADE_LIVE_PLATFORMS`); dry run and live share one code path | Each platform goes live only after its lab tests; a dry run rehearses the exact live steps |
| 2026-10-02 | Upgrade procedures are drivers with selectable paths (standalone, HA/failover/vPC pair) | The user wants the tool adaptable to other network designs |
| 2026-10-02 | FDM API trust by a pinned certificate fingerprint, confirmed by an admin | FDM uses self-signed certificates; pinning stops a man-in-the-middle without a CA |
| 2026-09-25 | Monitoring with PRTG: HTTP health endpoint, VMware, certificate and syslog sensors; no SNMP or agent on the server | Uses the existing monitoring; nothing extra listening on the server |

## Open questions

- ~~Is the failover pair ASA or FTD?~~ FTD (FDM HA) today; both paths are built so either design works.
- Which IOS-XE release is in use? Confirm `install activate auto-abort-timer` syntax on it in the lab.
- FDM API version on the FTDs: confirm the endpoint paths in `fdm.py` (readiness, upgrade status fields, revert) against the API Explorer.
- Are there any switch stacks (Cat9k StackWise, AW+ VCStack) or NX-OS vPC pairs? These affect the upgrade order.
- Does vCenter already have a key provider for VM Encryption, or do we set up the Native Key Provider?
- Does the PRTG version support custom request headers (HTTP Data Advanced) and syslog over TCP/TLS?

## Session log

| Date | Where | What happened |
|---|---|---|
| 2026-09 | Backup chat | Designed the platform and built the Config Backup MVP (merged to `main`) |
| 2026-09-24 | Firmware chat | Planned the firmware tool; built phase 1 on branch `claude/device-firmware-upgrade-app-yey88v`; added `CLAUDE.md` and this file; repository renamed to `Networking-Tools` |
| 2026-09-25 | NETWORK-TOOLS chat | Air-gapped install kit (bundle build + installer, tested end to end on Ubuntu 24.04), hardened service, nginx, backups; security hardening guide; audit events to the log; API docs off by default |
| 2026-09-25 | NETWORK-TOOLS chat | VMware hardening, apt-offline patching, PRTG monitoring endpoint and guide |
| 2026-09-29 | NETWORK-TOOLS chat | Ubuntu installed on the VM (OS on the 30 GB disk, 100 GB left for data); bundle now includes a package manifest and an optional ISO for vSphere; Windows/WSL build steps |
| 2026-10-01 | NETWORK-TOOLS chat | Kit made release-independent and tested on Ubuntu 26.04 (Python 3.14): build, fresh install, app behind nginx, all tests; fixed nginx duplicate `server_tokens` and the sudo-rs sudoers line; post-quantum SSH key exchange |
| 2026-10-01 | NETWORK-TOOLS chat | First install from the bundle on the 26.04 test VM succeeded; added step 3A (quick local test) to the installer output and guide; fixed root-shell install steps |
| 2026-10-01 | NETWORK-TOOLS chat | NetOps running on the test VM after moving its internal port to 8710 (NetBox uses 8000 there) |
| 2026-10-02 | NETWORK-TOOLS chat | PR 2: upgrade jobs with pre/post-checks, overrides, dry-run Start and rollback, reports and device upgrade history; upgrader role; per-device upgrade account; repository by vendor |
| 2026-10-02 | NETWORK-TOOLS chat | Parts 3-5: upgrade worker, driver framework, staging with on-device MD5, live procedures for IOS-XE (commit after checks), IOS bundle, NX-OS (+vPC), ASA (+failover pair), FTD via FDM (+HA), AW+; network simulator and end-to-end tests; lab test plan |
