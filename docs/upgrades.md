# Upgrade jobs

Every device upgrade goes through an **upgrade job**. The job is the procedure (the same
checks, in the same order, every time) and the record (what was checked, who decided
what and why, and what happened). It is there to stop mistakes and short cuts, not to
slow you down.

> **This release is a dry run.** The checks really log in to the device and read it, but
> Start only *records* the upgrade steps it would carry out. Nothing on the device is
> changed. Copying the image and on-device MD5 checks come in PR 3, and the real IOS-XE
> install upgrade in PR 4.

## Who can do what

| | Viewer | Admin | Upgrader |
|---|---|---|---|
| See jobs, reports and history | yes | yes | yes |
| Create, check, start, override, roll back, cancel | | | yes |
| Set a device's upgrade account, mark an image "Recommended" | | yes | |

Upgraders are the members of the AD group in `NETOPS_LDAP_UPGRADER_GROUP` (it adds to
the admin or viewer role, and lets someone in who is in no other group). The local
break-glass admin is always an upgrader. **If the setting is empty, admins are upgraders**:
fine for testing, but set the group in production.

## Before the first job

1. **Upload the image** in **Firmware → Image library**. Download it from the vendor
   yourself and paste the vendor's MD5 or SHA-512 when you upload; NetOps never downloads
   anything. The library is grouped by vendor. An admin can mark a release
   **Recommended** with a note (for example "Cisco suggested release").
2. **Make sure the device has a version reading** (Firmware → Versions → Check now).
3. **Give the device an upgrade account.** Upgrades log in with a separate credential
   (one that may copy files and reload) and never with the backup account. An admin picks
   it in the New upgrade job window. Without it, the pre-checks are blocked.
4. **Upload the circuit list** ([circuits.md](circuits.md)) so the job can show what the
   device carries.

## The procedure

1. **Firmware → Upgrade jobs → New upgrade job.** Pick the device. Only the images for
   its platform and model are offered. Add the change reference, the window and any notes.
   There is one open job per device. The affected circuits are saved with the job.
2. **Run pre-checks** whenever you like before the window. The job becomes:
   * **Ready to start**: every blocker passed;
   * **Blocked**: the failing blockers are listed at the top of the job window.
     Fix the device and press **Re-check**, or override (below).
3. **Start**, in the change window. Start runs the pre-checks again first, so the device
   is checked as it is right now. Then it saves the circuits again, takes a config backup,
   carries out the upgrade (in this release: records each step as "DRY RUN - would: …"),
   runs the post-checks and takes another config backup.
4. The result is **Completed**, **Completed with overrides** or **Failed post-checks**.

### Overriding a check

Some failures are expected, for example a power supply already known to be faulty, or
other work going on at the same time. Press **Override…** on the failed row and say why
it is safe to go on (at least 10 characters). The override, your name and the time are
in the record and the report.

An override only covers **that exact result**. If the same check fails again in the same
way, the override is carried forward (including from the pre-check to the post-check). If
it fails differently (for example a *new* alarm), it needs a new override.

Warnings (high CPU, a minor alarm, many old images on flash) never block. They are
recorded for you to judge.

### When post-checks fail

You have three choices, and every one is recorded:

* **Investigate and fix it** by hand, then **Re-run post-checks**;
* **Override** the failure with a note, if it is expected;
* **Roll back** to the previous version (a dry run in this release).

## The checks

**Pre-checks, every platform:** upgrade account set; can log in; the image is for this
platform and model; the image file on the server still matches its MD5; the device isn't
already on the target version; enough free flash (image size × 1.1, or × 2.2 for IOS-XE
install mode); a config backup in the last 24 hours (taken there and then if not).

**IOS / IOS-XE, in addition:** install mode (IOS-XE); no critical or major alarms (minor
is a warning); fans, power and temperature OK; CPU and memory (warnings); no install
operation pending; old inactive images (info); no unsaved configuration; every stack member
Ready.

**ASA:** the failover pair is healthy. **NX-OS, AW+ and FTD:** the common checks only.
Their own checks come with each platform's upgrade driver.

**Before and after snapshot (IOS / IOS-XE):** interfaces, CDP and LLDP neighbours,
port-channels, MAC address count, stack members and version. **Post-checks** compare
the two: the device runs the target version; interfaces that were up are still up;
neighbours and port-channel members are back; the MAC count hasn't dropped by more than
20 % (warning); the stack is complete; and the alarm and environment checks again.

## The record

* **Open report** in the job window gives a printable page with everything: the summary,
  every check with its value and detail, every override and who made it, the affected
  circuits and the full step log. **Checks CSV** is the check table on its own.
* Each device's **History → Upgrade history** tab lists all its jobs with their reports.
* A device with upgrade history can't be deleted (disable it instead), and an image used
  by a job can't be deleted, so the record stays complete.
* Every action is also in the audit log (`firmware.job.*`).

If NetOps restarts while a job is checking, the job is marked **Needs attention**: check
the device, then run the checks again.
