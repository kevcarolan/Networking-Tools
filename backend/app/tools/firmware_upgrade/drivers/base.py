"""The upgrade driver framework.

A driver knows one platform: which upgrade paths it offers (standalone, HA pair, ...),
the extra pre-checks it needs, and the procedure as a list of Steps. The job engine
(jobs.py) runs the steps one by one, records each one and remembers where it is, so
a job can stop between steps (post-check failure, "stop after this step", end of the
change window) and carry on later.

Dry run and live use the same code: in a dry run StepContext.change(), transfer() and
fdm_change() only record what they would do; show commands still really run. So a dry
run rehearses the exact procedure that a live run would carry out.
"""

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.tools.firmware_upgrade import checks as ck


@dataclass
class UpgradePath:
    id: str
    label: str
    units: int = 1  # 2 = an HA / redundant pair (the job's device plus its peer)
    description: str = ""


@dataclass
class Step:
    id: str
    kind: str  # driver method "step_<kind>" (or an engine step: backup, postcheck)
    label: str
    device_id: int | None = None
    reload: bool = False  # takes the device down: checked against the window first
    gate: bool = False  # a post-check: unresolved failures stop the job after it
    args: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(**d)


class StepFailed(Exception):
    """A step didn't work. attention=True when the device may be in an unclear state
    (e.g. it didn't come back after a reload) - the job then needs a person."""

    def __init__(self, message: str, attention: bool = False):
        super().__init__(message)
        self.attention = attention


class Paused(Exception):
    """Stop cleanly before the next step (stop requested, window ended)."""


@dataclass
class Unit:
    """A device in the job, as the driver sees it. Changes are saved back by the engine."""

    device_id: int
    name: str
    address: str
    platform: str
    position: int
    role: str = ""
    status: str = "pending"
    from_version: str = ""
    running_image: str = ""
    rollback_ref: str = ""
    staged: bool = False
    image_present: bool = False  # the image file is already on the device (from 'dir')
    file_system: str = ""  # per-device override
    fdm_fingerprint: str = ""
    facts: dict = field(default_factory=dict)


@dataclass
class ImageRef:
    id: int
    filename: str
    version: str
    md5: str
    size: int
    path: Path


class UpgradeDriver:
    platform = ""
    paths: list[UpgradePath] = [UpgradePath("standalone", "Standalone")]
    default_fs = "flash:"

    def path(self, path_id: str) -> UpgradePath | None:
        return next((p for p in self.paths if p.id == path_id), None)

    def file_system(self, unit: Unit) -> str:
        return unit.file_system or self.default_fs

    # --- pre-checks --------------------------------------------------------------

    def extra_commands(self, unit: Unit, image: ImageRef) -> dict[str, str]:
        """Read-only commands added to the pre/post-check login (key -> command)."""
        return {}

    def extra_prechecks(self, ctx: "StepContext", unit: Unit, outputs: dict[str, str],
                        path: UpgradePath) -> list[ck.CheckResult]:
        """Platform checks; may also fill in unit.role / running_image / rollback_ref."""
        return self.image_on_device_checks(ctx, unit, outputs)

    def pair_checks(self, ctx: "StepContext", units: list[Unit],
                    path: UpgradePath) -> list[ck.CheckResult]:
        """Checks across both units of a pair path (stored against the job's device)."""
        return []

    def image_on_device_checks(self, ctx, unit: Unit, outputs: dict[str, str]) -> list:
        listing = outputs.get("fw:dir") or outputs.get("dir") or ""
        unit.image_present = bool(listing) and ctx.image.filename in listing
        return [ck.CheckResult(
            "image_on_device", "Image already on the device", ck.INFO, ck.PASS,
            "yes - checked again with the MD5 at staging" if unit.image_present
            else "no - copied at Stage image or Start")]

    def order_units(self, units: list[Unit]) -> list[Unit]:
        """The order units are upgraded in (pair drivers: standby / secondary first)."""
        return sorted(units, key=lambda u: u.position)

    # --- the procedure -----------------------------------------------------------

    def upgrade_plan(self, ctx: "StepContext", path: UpgradePath, units: list[Unit]) -> list[Step]:
        """Default: units one after the other (for a pair, in the order given)."""
        steps: list[Step] = []
        for u in units:
            steps += self.unit_steps(ctx, u)
        return steps

    def unit_steps(self, ctx: "StepContext", u: Unit) -> list[Step]:
        d = u.device_id
        return [
            Step(f"backup_before:{d}", "backup", f"{u.name}: config backup before", d,
                 args={"label": "before"}),
            Step(f"stage:{d}", "stage", f"{u.name}: copy the image and check its MD5", d),
            *self.activate_steps(ctx, u),
            Step(f"wait:{d}", "wait", f"{u.name}: wait for the device to come back", d),
            Step(f"postcheck:{d}", "postcheck", f"{u.name}: post-checks", d, gate=True),
            *self.finish_steps(ctx, u),
            Step(f"backup_after:{d}", "backup", f"{u.name}: config backup after", d,
                 args={"label": "after"}),
        ]

    def activate_steps(self, ctx: "StepContext", u: Unit) -> list[Step]:
        raise NotImplementedError

    def finish_steps(self, ctx: "StepContext", u: Unit) -> list[Step]:
        """Steps after a passed post-check (e.g. IOS-XE commit)."""
        return []

    def rollback_plan(self, ctx: "StepContext", path: UpgradePath, units: list[Unit]) -> list[Step]:
        """Default: put each upgraded unit back, last upgraded first."""
        steps: list[Step] = []
        for u in reversed(units):
            if u.status not in ("activated", "checked", "failed"):
                continue
            d = u.device_id
            steps += [*self.rollback_steps(ctx, u),
                      Step(f"rb_wait:{d}", "wait", f"{u.name}: wait for the device to come back", d),
                      Step(f"rb_postcheck:{d}", "postcheck", f"{u.name}: post-checks on the "
                           "previous version", d, gate=True, args={"target": "from"}),
                      Step(f"rb_backup:{d}", "backup", f"{u.name}: config backup after the "
                           "roll back", d, args={"label": "after-rollback"})]
        return steps

    def rollback_steps(self, ctx: "StepContext", u: Unit) -> list[Step]:
        raise NotImplementedError

    # --- generic steps -------------------------------------------------------------

    def step_stage(self, ctx: "StepContext", step: Step, u: Unit) -> None:
        """Copy the image (unless it's already there with the right MD5) and verify it."""
        fs = self.file_system(u)
        if u.staged:
            ctx.event(f"Image already staged and verified on {u.name}", step.kind, device_id=u.device_id)
            return
        if u.image_present:
            if self.verify(ctx, u, record=True):
                u.staged = True
                return
            ctx.event(f"The copy on {u.name} doesn't match the MD5 - copying it again",
                      step.kind, "warn", u.device_id)
        ctx.transfer(u, fs)
        if ctx.dry_run:
            ctx.record_checks(u, "stage", [ck.CheckResult(
                "device_md5", "Image on the device matches the MD5", ck.BLOCKER, ck.SKIP,
                ctx.image.md5, f"DRY RUN: would copy {ctx.image.filename} to {fs} and verify it")])
            return
        if not self.verify(ctx, u, record=True):
            raise StepFailed(f"The image on {u.name} doesn't match its MD5 after the copy")
        u.staged = True

    def verify(self, ctx: "StepContext", u: Unit, record: bool = False) -> bool:
        """Checksum of the file on the device against the library MD5."""
        cmd = self.verify_command(u, ctx.image.filename)
        out = ctx.show(u, [cmd], long=True)[0]
        found = self.parse_checksum(out)
        ok = found == ctx.image.md5.lower()
        if record:
            ctx.record_checks(u, "stage", [ck.CheckResult(
                "device_md5", "Image on the device matches the MD5", ck.BLOCKER,
                ck.PASS if ok else ck.FAIL, found or "not found",
                "" if ok else f"expected {ctx.image.md5}\n{out.strip()[-300:]}")])
        return ok

    def verify_command(self, u: Unit, filename: str) -> str:
        return f"verify /md5 {self.file_system(u)}{filename}"

    @staticmethod
    def parse_checksum(out: str) -> str:
        hexes = re.findall(r"\b[0-9a-fA-F]{32}\b", out or "")
        return hexes[-1].lower() if hexes else ""

    def step_wait(self, ctx: "StepContext", step: Step, u: Unit) -> None:
        ctx.wait_back(u, expect_down=step.args.get("expect_down", True))


class StepContext:
    """Interface between a driver and the engine (implemented in jobs.py)."""

    dry_run: bool
    image: ImageRef

    def show(self, unit: Unit, commands: list[str], long: bool = False) -> list[str]: ...
    def change(self, unit: Unit, commands: list[str], *, config: bool = False,
               expect_reload: bool = False, long: bool = False) -> str: ...
    def transfer(self, unit: Unit, file_system: str) -> None: ...
    def wait_back(self, unit: Unit, expect_down: bool = True) -> None: ...
    def fdm(self, unit: Unit): ...
    def fdm_change(self, unit: Unit, label: str, fn): ...
    def event(self, message: str, step: str = "", level: str = "info",
              device_id: int | None = None) -> None: ...
    def record_checks(self, unit: Unit, phase: str, results: list) -> list: ...
    def set_job(self, **fields) -> None: ...
    def settings(self): ...
