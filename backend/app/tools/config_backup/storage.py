"""Stores device configs as files in a local git repository.

Git gives full version history, cheap storage of unchanged configs and
readable diffs, and the folder can be browsed or grepped directly on disk.
"""

import difflib
import re
import subprocess
import threading
from pathlib import Path

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(value: str, default: str) -> str:
    value = _SLUG_RE.sub("-", value.strip()).strip("-.")
    return value or default


def config_path_for(site: str, name: str) -> str:
    return f"{slug(site, 'unassigned')}/{slug(name, 'device')}.cfg"


class GitConfigStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()  # git's index can't take concurrent writers
        root.mkdir(parents=True, exist_ok=True)
        if not (root / ".git").exists():
            self._git("init", "-q")
            self._git("symbolic-ref", "HEAD", "refs/heads/main")
        self._git("config", "user.name", "netops-config-backup")
        self._git("config", "user.email", "netops@localhost")

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args],
                              check=check, capture_output=True, text=True)

    def _safe(self, rel_path: str) -> Path:
        path = (self.root / rel_path).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"path escapes config store: {rel_path}")
        return path

    def save(self, rel_path: str, content: str, message: str,
             previous_path: str | None = None) -> tuple[bool, str | None]:
        """Write and commit a config. Returns (changed, commit_sha)."""
        with self._lock:
            path = self._safe(rel_path)
            if previous_path and previous_path != rel_path:
                old = self._safe(previous_path)
                if old.exists() and not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._git("mv", previous_path, rel_path)
                    self._git("commit", "-q", "-m", f"Rename {previous_path} -> {rel_path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self._git("add", "--", rel_path)
            unchanged = self._git("diff", "--cached", "--quiet", "--", rel_path,
                                  check=False).returncode == 0
            if unchanged:
                return False, self._last_commit(rel_path)
            self._git("commit", "-q", "-m", message, "--", rel_path)
            return True, self._git("rev-parse", "HEAD").stdout.strip()

    def _last_commit(self, rel_path: str) -> str | None:
        out = self._git("log", "-1", "--format=%H", "--", rel_path, check=False).stdout.strip()
        return out or None

    def history(self, rel_path: str, limit: int = 100) -> list[dict]:
        """Versions of a config, newest first, following renames."""
        self._safe(rel_path)
        out = self._git("log", "--follow", f"-n{limit}", "--name-only",
                        "--format=%x1e%H%x1f%aI%x1f%s", "--", rel_path, check=False).stdout
        versions = []
        for record in out.split("\x1e")[1:]:
            lines = [ln for ln in record.splitlines() if ln.strip()]
            sha, date, subject = lines[0].split("\x1f", 2)
            versions.append({"commit": sha, "date": date, "message": subject,
                             "path": lines[1] if len(lines) > 1 else rel_path})
        return versions

    def read(self, rel_path: str, commit: str = "HEAD") -> str:
        self._safe(rel_path)
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}|HEAD", commit):
            raise ValueError("invalid commit id")
        result = self._git("show", f"{commit}:{rel_path}", check=False)
        if result.returncode != 0:
            raise FileNotFoundError(f"{rel_path} not found at {commit}")
        return result.stdout

    def diff(self, old: tuple[str, str], new: tuple[str, str]) -> str:
        """Unified diff between two (path, commit) versions."""
        a = self.read(old[0], old[1]).splitlines(keepends=True)
        b = self.read(new[0], new[1]).splitlines(keepends=True)
        return "".join(difflib.unified_diff(a, b, fromfile=f"{old[0]}@{old[1][:8]}",
                                            tofile=f"{new[0]}@{new[1][:8]}"))
