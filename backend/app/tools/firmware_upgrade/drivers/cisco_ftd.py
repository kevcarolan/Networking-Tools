"""Cisco FTD managed by FDM, through the FDM REST API.

Standalone: upload the package, readiness check, upgrade, wait until FDM reports the
new version, post-checks. FDM HA pair: the standby unit first; when it is back and
the pair is healthy, fail over so the upgraded unit is active; then the other unit.
Roll back: FDM's revert-upgrade (goes back to the version before the upgrade).

Each unit is a NetOps device with its own management address, upgrade account
(an FDM admin user) and pinned FDM certificate.
"""

from app.core.ssh import DeviceError
from app.tools.firmware_upgrade import checks as ck
from app.tools.firmware_upgrade.drivers.base import Step, StepFailed, Unit, UpgradeDriver, UpgradePath
from app.tools.firmware_upgrade.facts import compare_versions

_DONE = {"SUCCESS", "SUCCEEDED", "PASSED", "COMPLETED", "COMPLETE"}
_BAD = {"FAILED", "FAILURE", "ERROR", "CANCELLED"}


def version_matches(found: str, target: str) -> bool:
    """FDM reports e.g. '7.2.5-208' for library version '7.2.5'."""
    found, target = (found or "").strip(), (target or "").strip()
    return bool(found) and (compare_versions(found, target) == 0 or found.startswith(target + "-")
                            or target.startswith(found + "-"))


def _ha_role(ha: dict) -> str:
    state = (ha.get("nodeState") or "").upper()
    return "active" if "ACTIVE" in state else "standby" if "STANDBY" in state else \
        ("standalone" if not state or "SINGLE" in state else state.lower())


def _ha_healthy(ha: dict) -> bool:
    me, peer = _ha_role(ha), (ha.get("peerNodeState") or "").upper()
    in_sync = (ha.get("configStatus") or "IN_SYNC").upper() == "IN_SYNC"
    if me == "active":
        return "STANDBY" in peer and in_sync
    if me == "standby":
        return "ACTIVE" in peer and in_sync
    return False


class CiscoFtdDriver(UpgradeDriver):
    platform = "cisco_ftd"
    default_fs = ""
    paths = [
        UpgradePath("standalone", "Standalone FTD (FDM)", 1, "FDM API: upload, readiness, upgrade"),
        UpgradePath("fdm_ha", "FDM HA pair (standby first, then fail over)", 2,
                    "Upgrade the standby unit, fail over, then upgrade the other unit"),
    ]

    # --- pre-checks --------------------------------------------------------------

    def extra_prechecks(self, ctx, unit: Unit, outputs, path):
        label = "FDM API reachable, certificate trusted, account accepted"
        try:
            api = ctx.fdm(unit)
            info = api.system_info()
            pending = api.pending_changes()
            files = api.upgrade_files()
            try:
                ha = api.ha_status()
            except DeviceError:
                ha = {}
        except DeviceError as exc:
            return [ck.CheckResult("fdm_api", label, ck.BLOCKER, ck.FAIL, "", str(exc))]
        results = [ck.CheckResult("fdm_api", label, ck.BLOCKER, ck.PASS,
                                  f"FDM reports {info.get('softwareVersion', '?')}")]
        results.append(ck.CheckResult(
            "fdm_pending", "No undeployed changes in FDM", ck.BLOCKER,
            ck.FAIL if pending else ck.PASS, f"{len(pending)} pending" if pending else "none",
            "Deploy or discard the pending changes in FDM first" if pending else ""))
        unit.image_present = any(f.get("upgradeFileName") == ctx.image.filename for f in files)
        results.append(ck.CheckResult(
            "image_on_device", "Upgrade package already uploaded", ck.INFO, ck.PASS,
            "yes" if unit.image_present else "no - uploaded at Stage image or Start"))
        unit.role = _ha_role(ha)
        if path.units == 2:
            ok = _ha_healthy(ha)
            results.append(ck.CheckResult(
                "fdm_ha", "HA pair healthy and in sync", ck.BLOCKER, ck.PASS if ok else ck.FAIL,
                f"this: {ha.get('nodeState', '?')}; peer: {ha.get('peerNodeState', '?')}; "
                f"config: {ha.get('configStatus', '?')}"))
        elif unit.role not in ("standalone", ""):
            results.append(ck.CheckResult(
                "standalone", "Not part of an HA pair", ck.BLOCKER, ck.FAIL, unit.role,
                "This FTD is in an HA pair: use the FDM HA path"))
        return results

    def pair_checks(self, ctx, units, path):
        roles = sorted(u.role for u in units)
        ok = roles == ["active", "standby"]
        return [ck.CheckResult("pair_roles", "One active and one standby unit", ck.BLOCKER,
                               ck.PASS if ok else ck.FAIL,
                               ", ".join(f"{u.name}: {u.role or '?'}" for u in units))]

    def order_units(self, units):
        return sorted(units, key=lambda u: 0 if u.role == "standby" else 1)

    # --- plans ---------------------------------------------------------------------

    def unit_steps(self, ctx, u):
        d = u.device_id
        return [
            Step(f"backup_before:{d}", "backup", f"{u.name}: config backup before", d,
                 args={"label": "before"}),
            Step(f"stage:{d}", "stage", f"{u.name}: upload the upgrade package to FDM", d),
            Step(f"readiness:{d}", "readiness", f"{u.name}: FDM readiness check", d),
            Step(f"upgrade:{d}", "upgrade", f"{u.name}: start the upgrade (reboots)", d,
                 reload=True),
            Step(f"wait:{d}", "wait", f"{u.name}: wait for FDM to report the new version", d),
            Step(f"postcheck:{d}", "postcheck", f"{u.name}: post-checks", d, gate=True),
            Step(f"backup_after:{d}", "backup", f"{u.name}: config backup after", d,
                 args={"label": "after"}),
        ]

    def upgrade_plan(self, ctx, path, units):
        if path.units == 1:
            return self.unit_steps(ctx, units[0])
        s, a = units  # standby first
        return [*self.unit_steps(ctx, s),
                Step(f"ha_check:{s.device_id}", "ha_check", "HA pair healthy with the upgraded "
                     "standby", s.device_id),
                Step(f"ha_failover:{a.device_id}", "ha_failover", f"{a.name}: fail over to the "
                     "upgraded unit", a.device_id, reload=True, args={"expect_active": s.device_id}),
                *self.unit_steps(ctx, a),
                Step(f"ha_check_end:{s.device_id}", "ha_check", "HA pair healthy on the new "
                     "version", s.device_id)]

    def rollback_steps(self, ctx, u):
        return [Step(f"rb:{u.device_id}", "revert", f"{u.name}: FDM revert upgrade (reboots)",
                     u.device_id, reload=True)]

    def rollback_plan(self, ctx, path, units):
        steps = super().rollback_plan(ctx, path, units)
        for st in steps:
            if st.kind == "wait":
                st.args["version"] = "from"
        return steps

    # --- steps ---------------------------------------------------------------------

    def _file_id(self, ctx, u) -> str:
        for f in ctx.fdm(u).upgrade_files():
            if f.get("upgradeFileName") == ctx.image.filename:
                return f.get("id", "")
        return ""

    def step_stage(self, ctx, step, u):
        if u.image_present or (not ctx.dry_run and self._file_id(ctx, u)):
            u.staged = True
            ctx.record_checks(u, "stage", [ck.CheckResult(
                "device_package", "Upgrade package accepted by FDM", ck.BLOCKER, ck.PASS,
                ctx.image.filename, "already uploaded")])
            return
        ctx.fdm_change(u, f"upload {ctx.image.filename} ({ctx.image.size // 2**20} MB) to FDM",
                       lambda api: api.upload(ctx.image.path))
        if ctx.dry_run:
            ctx.record_checks(u, "stage", [ck.CheckResult(
                "device_package", "Upgrade package accepted by FDM", ck.BLOCKER, ck.SKIP,
                ctx.image.filename, "DRY RUN: would upload the package")])
            return
        ok = bool(self._file_id(ctx, u))
        ctx.record_checks(u, "stage", [ck.CheckResult(
            "device_package", "Upgrade package accepted by FDM", ck.BLOCKER,
            ck.PASS if ok else ck.FAIL, ctx.image.filename,
            "" if ok else "FDM doesn't list the package after the upload")])
        if not ok:
            raise StepFailed(f"FDM on {u.name} didn't accept the upgrade package")
        u.staged = True

    def step_readiness(self, ctx, step, u):
        if ctx.dry_run:
            ctx.event("DRY RUN - would run the FDM upgrade readiness check", step.kind,
                      "action", u.device_id)
            return
        file_id = self._file_id(ctx, u)
        ctx.fdm_change(u, "start the readiness check", lambda api: api.readiness_check(file_id))
        result = {}

        def finished():
            st = ctx.fdm(u).upgrade_status()
            state = str(st.get("readinessCheckState") or st.get("state") or "").upper()
            result.update(st, state=state)
            return state in _DONE | _BAD

        if not ctx.poll(finished, minutes=30, what="the readiness check"):
            raise StepFailed(f"No readiness result from FDM on {u.name} within 30 minutes")
        ok = result["state"] in _DONE
        ctx.record_checks(u, "stage", [ck.CheckResult(
            "fdm_readiness", "FDM readiness check passed", ck.BLOCKER, ck.PASS if ok else ck.FAIL,
            result["state"], str(result.get("message") or result.get("readinessCheckMessage") or ""))])
        if not ok:
            raise StepFailed(f"The FDM readiness check failed on {u.name}")

    def step_upgrade(self, ctx, step, u):
        file_id = "" if ctx.dry_run else self._file_id(ctx, u)
        u.status = "activated"
        ctx.fdm_change(u, f"start the upgrade to {ctx.image.version}",
                       lambda api: api.start_upgrade(file_id))

    def step_revert(self, ctx, step, u):
        ctx.fdm_change(u, "revert the upgrade", lambda api: api.revert())

    def step_wait(self, ctx, step, u):
        target = u.from_version if step.args.get("version") == "from" else ctx.image.version
        if ctx.dry_run:
            ctx.event(f"DRY RUN - would wait for FDM to report {target}", step.kind, "action",
                      u.device_id)
            return
        seen = {}

        def back():
            try:
                ctx.forget_fdm(u)
                seen["v"] = ctx.fdm(u).system_info().get("softwareVersion", "")
            except DeviceError:
                return False
            return version_matches(seen["v"], target)

        if not ctx.poll(back, minutes=ctx.settings().upgrade_reload_timeout_min * 3,
                        what=f"FDM on {u.name} to report {target}"):
            raise StepFailed(f"{u.name} didn't come back on {target} in time "
                             f"(last seen: {seen.get('v') or 'no answer'})", attention=True)
        ctx.settle(u)

    def step_ha_check(self, ctx, step, u):
        if ctx.dry_run:
            ctx.event("DRY RUN - would check the HA pair is healthy and in sync", step.kind,
                      "action", u.device_id)
            return
        ha = ctx.fdm(u).ha_status()
        if not _ha_healthy(ha):
            raise StepFailed(f"HA pair not healthy: {ha.get('nodeState')} / "
                             f"{ha.get('peerNodeState')} / {ha.get('configStatus')}")
        ctx.event("HA pair healthy and in sync", step.kind, device_id=u.device_id)

    def step_ha_failover(self, ctx, step, u):
        ctx.fdm_change(u, "HA failover (the upgraded unit becomes active)",
                       lambda api: api.ha_failover())
        if ctx.dry_run:
            return
        upgraded = ctx.unit(step.args["expect_active"])

        def swapped():
            try:
                ctx.forget_fdm(upgraded)
                return _ha_role(ctx.fdm(upgraded).ha_status()) == "active"
            except DeviceError:
                return False

        if not ctx.poll(swapped, minutes=15, what="the HA failover"):
            raise StepFailed("The upgraded unit didn't become active after the failover",
                             attention=True)
        upgraded.role, u.role = "active", "standby"
