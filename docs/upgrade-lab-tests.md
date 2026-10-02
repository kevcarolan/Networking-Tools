# Upgrade lab tests (sign-off before production)

NetOps' upgrade procedures have been tested against a **network simulator**
(`backend/tests/fake_network.py`). It reproduces the commands, prompts, reloads,
failovers and the FDM API as documented by the vendors. Real devices and software
releases differ in details: prompt wording, output format, and the FDM API version. So
**every platform and path has to pass these tests on lab kit before it is switched on
for live use** (`NETOPS_UPGRADE_LIVE_PLATFORMS`).

Work through one section per platform. Tick each line, note the NetOps job number, and
keep the job reports with the sign-off. If something doesn't behave as described, send
the job report (it includes the device output), plus
`sudo netops-cli firmware-check <device> --raw`. The fix is usually a one-line change in
the platform's driver (`backend/app/tools/firmware_upgrade/drivers/`).

## 0. Before you start

- [ ] Both services run: `systemctl status netops netops-worker`.
- [ ] The Upgrade jobs tab shows no "worker isn't running" banner.
- [ ] The lab devices are in NetOps, with a backup account and an **upgrade account** (Upgrade settings).
- [ ] Firmware › Versions › Check now reads the model and version of each device correctly.
- [ ] The SCP server is enabled on each device (see upgrades.md).
- [ ] The firewall allows SSH, and HTTPS to the FTDs, from the server (`FDM_HOSTS`).
- [ ] The images are uploaded with the vendor checksum (they show "verified").
- [ ] `NETOPS_UPGRADE_LIVE_PLATFORMS` lists **only the platform under test**. Restart both services after changing it.

## 1. Every platform: the common tests

Do these for each platform before its own section. Use a **dry run** job first.

- [ ] Pre-checks: every row is plausible. Note any check that is wrong, especially a pass or fail for the wrong reason.
- [ ] Introduce a blocker (e.g. unsaved config, disable SCP). The job is **Blocked**; fix it and Re-check → Ready.
- [ ] Override a blocker with a reason: it appears in the report with your name.
- [ ] Stage image (dry run): it says it would copy. Then on a **live** job: the image is copied and the MD5 on the device passes.
- [ ] Corrupt the staged copy (delete it, or copy a different file with the same name). Stage again: the MD5 check fails and can't be overridden.
- [ ] Dry run Start → **Completed**. The Procedure tab shows each step; the log shows "DRY RUN - would …"; nothing changed on the device.
- [ ] The report opens and prints. It shows the circuits and the overrides.

## 2. IOS-XE install mode (Cat9k, standalone; then a stack if you have one)

- [ ] Pre-checks show "Device runs in install mode" and a **Rollback point** (install rollback id).
- [ ] Live Start inside the window. Confirm:
  - [ ] `install add file flash:<image>` runs; the job log shows SUCCESS.
  - [ ] `install activate auto-abort-timer 120 prompt-level none` is accepted, and the switch reloads.
    *Confirm the exact syntax on your release; adjust in `cisco_ios.py`.*
  - [ ] The job waits, sees the switch back, and runs the post-checks.
  - [ ] The banner shows "Not committed: … goes back … at HH:MM" while the post-checks run.
  - [ ] Post-checks pass → `install commit` → **Completed**. `show install summary` shows the new version committed (C).
- [ ] Failure path: before Start, shut a port that is up (or unplug a neighbour) so a post-check fails after the reload. Then:
  - [ ] the job is **Failed**, not committed;
  - [ ] **Roll back** runs `install abort prompt-level none`, the switch returns to the old version, and the post-checks pass → **Rolled back**.
- [ ] Override path: post-check failure → **Override** → Paused → **Continue** commits → **Completed with overrides**.
- [ ] Timer path (optional, use `NETOPS_UPGRADE_ABORT_TIMER_MIN=30` in the lab): leave a failed job alone past the timer. The switch reverts by itself, and the job turns **Needs attention** with that message.
- [ ] Stack: every member upgrades. "All stack members back" passes.

## 3. IOS / IOS-XE bundle mode (or a classic IOS switch)

- [ ] Pre-checks: "Device boots a single image file" and "Current image stays on flash" pass.
- [ ] Live Start: `no boot system`, `boot system flash:<new>`, `boot system <old>`, `write memory`, `reload`. The switch comes back on the new image.
- [ ] The reload question ("Save? [yes/no]", "Proceed with reload? [confirm]") is answered automatically. *If your release asks something else, note the exact text.*
- [ ] Roll back after a forced post-check failure: the switch boots the old image again.

## 4. NX-OS

- [ ] Standalone live Start:
  - [ ] the image is copied and `show file bootflash:<img> md5sum` matches;
  - [ ] `show install all impact` is recorded under "Image on the device" and parsed correctly (try an incompatible image to see it block);
  - [ ] `install all nxos bootflash:<img> non-interruptive` runs; the switch reloads and comes back;
  - [ ] post-checks pass.
- [ ] vPC pair (if you have one): set each switch as the other's peer. The pre-checks show the vPC healthy, with one primary and one secondary.
  - [ ] Live Start upgrades the **secondary** first.
  - [ ] It checks the vPC is healthy again, then upgrades the primary.
- [ ] Roll back: `install all nxos <previous image>`.

## 5. ASA

- [ ] Standalone: `boot system` lines are set (new first, old as fallback), `write memory`, `reload noconfirm`; the ASA comes back on the new version.
  *Confirm that `clear configure boot system` is accepted on your release.*
- [ ] Failover pair: in NetOps, the **active address** device has the **standby address** device as its peer. Then:
  - [ ] pre-checks show "One active and one standby unit";
  - [ ] the image is staged on **both** units;
  - [ ] `boot system` on the active unit replicates to the standby;
  - [ ] `write memory` and `failover exec mate write memory` both run;
  - [ ] `failover reload-standby`: the standby comes back as **Standby Ready** on the new version, and its post-checks pass;
  - [ ] `failover active` on the standby address: the upgraded unit takes over, and traffic continues (watch a continuous ping through the firewall);
  - [ ] `failover reload-standby`: the other unit comes back on the new version, and both units' post-checks pass.
- [ ] Roll back the pair: the same sequence, back to the old image.

## 6. FTD (FDM)

These steps confirm the **FDM API** behaves as the driver expects. The endpoint paths are
in `ENDPOINTS` in `backend/app/tools/firmware_upgrade/fdm.py`; compare them with your
FDM's API Explorer (`https://<ftd>/#/api-explorer`).

- [ ] Upgrade settings → **Fetch from the device**: the fingerprint matches the FDM certificate. Save it.
- [ ] Pre-checks:
  - [ ] "FDM API reachable…" passes;
  - [ ] make a change in FDM without deploying it: "No undeployed changes in FDM" fails; deploy, then Re-check;
  - [ ] HA path: "HA pair healthy and in sync" passes.
- [ ] Replace the FTD certificate (or point the device at another FTD): pre-checks fail with "certificate changed".
- [ ] Stage image: the package appears in FDM › Device › Updates.
- [ ] Standalone live Start:
  - [ ] the readiness check starts, and its result is recorded;
  - [ ] **note the field FDM uses for the readiness result** (`readinessCheckState`?) if the job waits for 30 minutes without one;
  - [ ] the upgrade starts; FDM reboots; the job waits until FDM reports the new version (it allows up to 90 minutes);
  - [ ] post-checks pass.
- [ ] HA pair (your current design): set each FTD as the other's peer, each with its own trusted fingerprint. Then live Start:
  - [ ] the **standby** unit upgrades first;
  - [ ] HA is healthy again;
  - [ ] **HA failover**: the upgraded unit becomes active, and traffic continues;
  - [ ] the other unit upgrades;
  - [ ] the final HA check passes.
- [ ] Roll back: FDM **revert upgrade**. The unit returns to the previous version.

## 7. AlliedWare Plus (standalone; then a VCStack)

- [ ] The SCP copy works. *Note where the file lands and the remote path used. If AW+ wants `flash:/<file>`, set the device's file system to `flash:/` in Upgrade settings.*
- [ ] The size check on the device passes (AW+ has no MD5 command).
- [ ] Live Start: `boot system flash:/<new>.rel`, `boot system backup flash:/<old>.rel`, `write memory`, `reload` (the reboot question is answered). The switch comes back on the new release.
- [ ] VCStack: every member is on the new release afterwards.
- [ ] Roll back: the switch boots the old release again.

## 8. Operations

- [ ] **Stop after this step** during a live job (e.g. while it copies): it pauses before the next step; Continue finishes it.
- [ ] Window end: make a window that ends while the job is copying. The job pauses before the reload; Continue is refused outside the window.
- [ ] `sudo systemctl restart netops` during a live job: the job carries on (it runs in the worker).
- [ ] `sudo systemctl stop netops-worker` during a copy: the job pauses before its next step (or, if the copy is cut off, shows Needs attention). Start the worker again.
- [ ] Reinstall the bundle while a live job runs: `install.sh` refuses ("a live upgrade job is running").
- [ ] PRTG: "Upgrade worker up" goes to 0 when the worker is stopped; "Upgrade jobs failed or needing attention" counts a failed job.

## Sign-off

| Platform / path | Lab job numbers | Tested by | Date | Live enabled |
|---|---|---|---|---|
| IOS-XE install | | | | |
| IOS bundle | | | | |
| NX-OS standalone | | | | |
| NX-OS vPC pair | | | | |
| ASA standalone | | | | |
| ASA failover pair | | | | |
| FTD standalone | | | | |
| FTD HA pair | | | | |
| AW+ / VCStack | | | | |
