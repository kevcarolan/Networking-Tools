# Firmware tool: design and plan

The second tool on the NetOps platform. It shows which software version every device
runs, compares that with the approved version for its platform and model, keeps the
firmware images, and (in later phases) stages and runs upgrades with checks before and after.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Where it lives | A second tool in the same app (`backend/app/tools/firmware_upgrade/`), not a separate application | Same devices, same AD login and roles, same credential encryption and audit log. An upgrade can call the backup tool directly for a config backup before and after |
| Database | The shared `data/app.db`; firmware tables are prefixed `fw_` | One device inventory that can't drift. The tool only adds new tables, so an existing database upgrades itself on start |
| Image files | On disk in `data/firmware/`, never in the database | Images are 0.2–2 GB. The database only stores their name, version, size and checksums |
| Uploads | Streamed straight to disk (raw request body, not multipart) and hashed on the way in | A 2 GB image never sits in memory or `/tmp`; the MD5/SHA-512 is ready when the upload ends |
| Checksums | The vendor's MD5 or SHA-512 can be pasted on upload; a mismatch rejects the file | Catches corrupt or wrong downloads before they reach a device |
| Version reading | SSH `show version` (plus `dir` / `show failover` / `show system`) using the device's existing credential profile | Read-only, so the backup account is enough. Nothing changes on the device |
| Upgrade credential (phase 2+) | A separate, privileged credential per device, stored in a firmware table | Upgrades need write access; the backup account stays read-only |
| Upgrade worker (phase 3) | Runs as a separate process (same code and image, different command) | Restarting the web GUI must never interrupt a 20-minute reload in progress |

## Your environment (from the planning discussion)

* **IOS-XE switches run in install mode:** upgrades use `install add file … activate commit`.
  The version report shows the mode of each switch, so any switch still in bundle mode is easy to spot.
* **FTD is managed locally with FDM:** FTD upgrades go through the **FDM REST API**
  (upload the upgrade package, run the readiness check, install), not the CLI.
* **There is one failover pair:** the report shows each unit's role (e.g. `Primary / Active`).
  Upgrades will do the standby unit first, fail over, then do the other unit. The two
  units are never in the same upgrade wave.

## Phase 1: version report and image library (done)

* **Versions** page: model, serial, version, boot image, install/bundle mode,
  failover role and free flash for every device. Each device is checked daily
  (`NETOPS_FIRMWARE_CHECK_MINUTES`); there are also **Check now** and **Check all now** buttons.
  A failed check keeps the last known version and shows the reason.
* When a device's version changes, the previous version and the time it changed are kept.
* **Standards**: the approved version per platform and model pattern (`C9300-*`, or `*`
  for all models). The most specific pattern wins. Each device is marked **On standard**,
  **Behind**, **Ahead** or **No standard**. Version numbers are compared part by part, so
  `17.09.04a` equals `17.9.4a` and `9.18(4)` is newer than `9.16(4)23`.
* **Image library**: upload, checksum, list and delete images. An image can be linked
  to a standard. Free flash is shown in red when it is too small for the linked image.
* CSV export of the version report.
* `python -m app.cli firmware-check <device> --raw` shows the raw output and what was
  parsed, to check the parsers against real devices before relying on the report.

Who can do what: everyone can see the report. Admins can run checks, edit standards and
upload or delete images.

| Platform | Commands | Reads |
|---|---|---|
| Cisco IOS / IOS-XE | `show version`, `dir` | version, model, serial, boot image, install/bundle mode, flash |
| Cisco NX-OS | `show version`, `dir bootflash:` | version, model, serial, image, flash |
| Cisco ASA | `show version`, `dir`, `show failover` | version, model, serial, image, flash, failover role |
| Cisco FTD (FDM) | `show version`, `show failover` | FTD version and build, model, serial, HA role |
| Allied Telesis AW+ | `show version`, `show system` | release, model, serial |

## Phase 2: staging and pre-checks (no reloads)

* An upgrade credential per device (privileged account, separate from the backup account).
* **Pre-check job** (a dry run): reachability, login, current version, model matches the
  image, enough free flash, no unsaved config (`show archive config differences` / startup vs
  running), and for the failover pair both units healthy.
* **Stage job**: copy the image to the device and verify it there:
  * IOS-XE / IOS / NX-OS / ASA: SCP push from the server (Netmiko `file_transfer`),
    then `verify /md5` on the device against the library checksum.
  * AlliedWare Plus: the switch pulls the `.rel` file from the server over HTTP(S)
    (`copy https://…`), using a one-time download link.
* Capture the "before" state for later comparison: interfaces up, CDP/LLDP neighbours,
  port-channels, routing neighbours, failover state.
* Clean up old images on flash (IOS-XE `install remove inactive`), only when asked.

## Phase 3: upgrades with reload

The upgrade job and the upgrade wizard:

1. Pick devices (filtered by site, platform, model or "behind standard") and the image.
2. Choose the maintenance window, how many devices at once, and when to stop (after N failures).
3. Pre-checks run and show the result before anything is changed.
4. Optional second-person approval (`NETOPS_FIRMWARE_REQUIRE_APPROVAL`).

Steps for each device:

1. Pre-checks (as in phase 2).
2. **Config backup** through the backup tool, labelled "pre-upgrade".
3. Stage the image, if it isn't already staged.
4. Activate and reload, only inside the window:

   | Platform | Method |
   |---|---|
   | IOS-XE (install mode) | `install add file flash:<image> activate commit prompt-level none` |
   | IOS classic | `boot system flash:<image>`, `write memory`, `reload` |
   | NX-OS | `show install all impact nxos bootflash:<image>` as a pre-check, then `install all nxos bootflash:<image>` |
   | ASA failover pair | Upgrade and reload the standby unit, check it is back as Standby Ready, `failover active` on it, then upgrade the other unit |
   | AlliedWare Plus | `boot system flash:/<image>.rel`, `write memory`, `reload`. VCStack members sync automatically |
   | FTD (FDM) | FDM REST API: upload the package, readiness check, install. The API reports the state until the device is back |

5. Wait for SSH (or the FDM API) to come back, with a timeout (default 30 minutes).
6. Post-checks: the version matches the target, compare with the "before" state, config
   backup and diff.
7. Result for each device: success, or failed at a named step, with the log.

Safety rules:

* Upgrades require a separate AD group (`NETOPS_LDAP_UPGRADE_GROUP`, e.g. `NetOps-Upgraders`).
  Admins who are not in it can't start them.
* **Peer groups**, so both units of the failover pair (and any future vPC or HA pairs)
  are never reloaded in the same wave.
* Waves: access switches first, core last. The job stops when the failure limit is reached.
* No reload outside the maintenance window. A job that runs out of time stops before
  the next reload, not in the middle of one.
* Every step goes to the audit log. Before/after state and the device log are kept per job.
* The upgrade worker runs as its own process/container, so a GUI restart doesn't touch
  running upgrades. A restarted worker marks interrupted devices "needs attention"
  instead of retrying them blindly.

GUI: an **Upgrades** tab with the job list, the new-upgrade wizard, and a job page with
live progress for each device and step, the log, before/after differences, and retry /
skip / cancel.

## Phase 4: operations

* Email / Microsoft Teams: a job finished or failed; devices behind standard for more than N days.
* Scheduled jobs (e.g. "upgrade Branch1 switches next Tuesday 22:00").
* Cisco PSIRT openVuln API: flag versions with known security advisories.
* End-of-life dates per model and version.

## Disk space

Plan roughly **2× the images you keep**. For example: Cat9k 1.2 GB, NX-OS 2 GB,
ASA/FTD 0.3–1 GB, AW+ 50 MB. A 100 GB disk comfortably holds two or three versions per
platform (the backup tool on its own needed about 20 GB).

## Testing before production

* Run `python -m app.cli firmware-check <device> --raw` against one device of each
  platform and model, and check the parsed values. Send the raw output of any that are
  wrong so the parser can be fixed and a test added.
* Before phase 3 goes live, rehearse every upgrade path on lab or spare equipment:
  one IOS-XE switch, one NX-OS, the ASA pair (or a lab pair), one FTD and one AW+ switch.
