"""Sandboxes for the fixer: a clone of the customer repo where the agent edits files and
runs tests, with hard limits on wall clock, memory and CPU, and nothing leaving it except
the diff and scrubbed logs.

Two backends share one interface (``Sandbox``):

``LocalSandbox``
    A temp directory on the worker and ``subprocess`` with timeouts. Memory and CPU are
    capped best-effort with ``resource`` limits (``RLIMIT_DATA``/``RLIMIT_CPU``) on each
    child; there is **no** network restriction. It is what the tests use and what
    ``SANDBOX_BACKEND=local`` selects for a dev box without Docker.

``DockerSandbox``
    One container per job from ``settings.sandbox_image`` (Python 3.12, Node 20, git,
    pytest, ruff, uv, typescript) with ``mem_limit=settings.sandbox_memory`` and
    ``nano_cpus`` from ``settings.sandbox_cpus``. The container is destroyed in
    ``cleanup``.

    **Network trade-off.** Docker has no per-host egress allowlist. Rather than pretend
    one exists, the Docker backend splits the job into two kinds of step:

    * **networked steps** (``git clone`` and the dependency install from the package
      registries) run in a short-lived helper container attached to the default bridge
      network. Its DNS is pointed at an address that answers nothing and the hosts from
      ``settings.sandbox_egress_allowlist`` are resolved on the worker and injected as
      ``extra_hosts``, so only allowlisted names resolve inside it. This is a
      name-level allowlist: a process that already knows an IP address can still
      connect to it. When ``SANDBOX_HTTPS_PROXY`` is set in the environment the helper
      gets ``HTTPS_PROXY``/``HTTP_PROXY`` instead and the proxy host is the only name
      that resolves, which turns the allowlist into a real one enforced by the proxy.
    * **agent and test steps** (every ``run`` the agent loop, the baseline and the
      validator make) run in the main container with ``network_mode="none"``: a
      prompt-injected changelog that gets code executed through the test suite cannot
      exfiltrate the customer's code because there is no route out at all.

    Both containers share one named volume holding the clone (``/work``), so the install
    step's in-project output (``.venv``, ``node_modules``, poetry's env with
    ``POETRY_VIRTUALENVS_IN_PROJECT=1``) is visible to the no-network steps, and a second
    per-job named volume mounted over the image venv (``/opt/venv``, first on ``PATH``)
    so ``pip install -r requirements.txt`` / ``pip install -e .`` from the networked
    helper land where the test container's ``python`` looks. Docker seeds an empty named
    volume from the image's directory on first mount, so the venv starts as the image
    built it (pytest, ruff, poetry).

Only files the sandbox itself writes (``write_file``, replacement and unified patches)
count as new files in ``diff`` / ``changed_files``; untracked artefacts of the install or
the test run (``package-lock.json``, ``*.egg-info``, report files) never become part of
the patch.

Every backend scrubs the installation token (and any other ``://user:secret@`` URL
credentials) from everything it logs or returns, and clamps every ``run`` to the job's
``Deadline`` so a job fails cleanly at ``settings.sandbox_wall_clock_s``.
"""

from __future__ import annotations

import fnmatch
import io
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kintsua.core.config import Settings, get_settings
from kintsua.core.util import redact_secrets
from kintsua.fixer.patch import (
    ReplacementError,
    apply_replacement,
    looks_binary,
    normalize_repo_path,
    parse_unified_diff,
)

log = logging.getLogger(__name__)

_USERINFO_RE = re.compile(r"://[^/@\s]+@")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_RUN_TIMEOUT_S = 300.0
GIT_IDENTITY = {"name": "kintsua[bot]", "email": "bot@kintsua.dev"}
#: Directories never listed, searched or read by the agent.
IGNORED_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".tox"}
)


class SandboxError(RuntimeError):
    """A sandbox operation failed; the message is safe to store (secrets scrubbed)."""


class SandboxTimeout(SandboxError):
    """The job's wall clock ran out."""


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    command: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        if self.stdout and self.stderr:
            return self.stdout.rstrip("\n") + "\n" + self.stderr
        return self.stdout or self.stderr

    def tail(self, lines: int = 40, max_chars: int = 6000) -> str:
        return tail_text(self.output, lines=lines, max_chars=max_chars)


def tail_text(text: str, *, lines: int = 40, max_chars: int = 6000) -> str:
    chunk = "\n".join((text or "").splitlines()[-lines:])
    if len(chunk) > max_chars:
        chunk = chunk[-max_chars:]
    return chunk


class Deadline:
    """Wall-clock budget for one job; every ``run`` is clamped to what remains."""

    def __init__(self, total_s: float):
        self.total_s = float(total_s)
        self.started = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float:
        return max(0.0, self.total_s - self.elapsed())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self, what: str = "job") -> None:
        if self.expired:
            raise SandboxTimeout(f"{what}: wall clock of {self.total_s:.0f}s exhausted")

    def clamp(self, timeout_s: float | None) -> float:
        rem = self.remaining()
        if timeout_s is None or timeout_s <= 0:
            return rem
        return min(float(timeout_s), rem)


# --------------------------------------------------------------------------------------
# Secret scrubbing
# --------------------------------------------------------------------------------------


def secrets_in_url(url: str) -> tuple[str, ...]:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ()
    found: list[str] = []
    if parts.password:
        found.append(parts.password)
    if parts.username and parts.username not in ("x-access-token", "oauth2", "git"):
        found.append(parts.username)
    return tuple(found)


def scrub(text: str, secrets: Iterable[str] = (), *, generic: bool = True) -> str:
    """Remove credentials from a string: the explicit secrets (the installation token),
    ``://user:pw@`` URL userinfo and, with ``generic`` (logs and comments, never a diff
    or test output the validator has to reason about byte for byte), the generic secret
    shapes ``redact_secrets`` knows."""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 4:
            out = out.replace(s, "***")
    out = _USERINFO_RE.sub("://***@", out)
    return redact_secrets(out) if generic else out


def parse_memory(spec: str | int | None) -> int | None:
    """``"4g"`` / ``"512m"`` / ``"2048k"`` / bytes -> bytes; None when unparsable."""
    if spec is None:
        return None
    if isinstance(spec, int):
        return spec
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*", str(spec).lower())
    if not m:
        return None
    n = float(m.group(1))
    mult = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[m.group(2)]
    return int(n * mult)


# --------------------------------------------------------------------------------------
# Interface + shared git logic
# --------------------------------------------------------------------------------------


class Sandbox:
    """The interface the agent, validator and PR code program against."""

    backend = "base"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        wall_clock_s: float | None = None,
        job_id: str | None = None,
    ):
        self.settings = settings or get_settings()
        self.deadline = Deadline(
            wall_clock_s if wall_clock_s is not None else self.settings.sandbox_wall_clock_s
        )
        self.job_id = job_id or uuid.uuid4().hex[:12]
        self.workdir: str | None = None
        self.base_sha: str | None = None
        self.branch: str | None = None
        self.secrets: tuple[str, ...] = ()
        self.log_lines: list[str] = []
        #: repo paths written through this sandbox (the only untracked files a diff shows)
        self.written_paths: set[str] = set()
        self._cleaned = False

    # ---- abstract -------------------------------------------------------------------

    def prepare(self, clone_url: str, branch: str, *, sha: str | None = None) -> str:
        raise NotImplementedError

    def run(
        self,
        cmd: list[str] | str,
        *,
        timeout_s: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input: str | None = None,
        network: bool = False,
    ) -> RunResult:
        raise NotImplementedError

    def read_file(self, path: str) -> str:
        raise NotImplementedError

    def write_file(self, path: str, text: str) -> None:
        raise NotImplementedError

    def list_files(self, glob: str = "**/*") -> list[str]:
        raise NotImplementedError

    def file_exists(self, path: str) -> bool:
        raise NotImplementedError

    def cleanup(self) -> None:
        raise NotImplementedError

    # ---- shared ---------------------------------------------------------------------

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()

    def record(self, line: str) -> None:
        """Append a scrubbed line to the job log (what leaves the sandbox)."""
        self.log_lines.append(scrub(line, self.secrets))

    def log_excerpt(self, lines: int = 40) -> str:
        return tail_text("\n".join(self.log_lines), lines=lines, max_chars=8000)

    def repo_path(self, path: str) -> str:
        """Validate a repo-relative path from the model; raises ``SandboxError`` when it
        escapes the repository or points into ``.git``."""
        norm = normalize_repo_path(path)
        if norm is None:
            raise SandboxError(f"path {path!r} is outside the repository")
        if norm == ".git" or norm.startswith(".git/"):
            raise SandboxError("paths inside .git are not accessible")
        return norm

    def git(
        self,
        *args: str,
        timeout_s: float = 120.0,
        cwd: str | None = None,
        input: str | None = None,
        network: bool = False,
    ) -> RunResult:
        return self.run(["git", *args], timeout_s=timeout_s, cwd=cwd, input=input, network=network)

    def git_ok(self, *args: str, timeout_s: float = 120.0, cwd: str | None = None) -> str:
        res = self.git(*args, timeout_s=timeout_s, cwd=cwd)
        if not res.ok:
            raise SandboxError(
                f"git {args[0] if args else ''} failed (exit {res.exit_code}): "
                f"{scrub(res.tail(20), self.secrets)}"
            )
        return res.stdout

    def _git_clone_args(self, clone_url: str, branch: str, dest: str, sha: str | None) -> list[str]:
        args = [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--quiet",
            "--no-tags",
            "--single-branch",
        ]
        if sha is None:
            args.append("--depth=1")
        if branch:
            args += ["--branch", branch]
        args += [clone_url, dest]
        return args

    def _after_clone(self, clone_url: str, branch: str, sha: str | None) -> None:
        """Pin the checkout, drop the token from ``.git``, record the base sha."""
        if sha:
            fetched = self.git("fetch", "--quiet", "--depth=1", "origin", sha, network=True)
            if not fetched.ok:
                raise SandboxError(
                    f"could not fetch {sha[:12]}: {scrub(fetched.tail(10), self.secrets)}"
                )
            self.git_ok("checkout", "--quiet", "--detach", sha)
        # the clone's own .git holds the token in its remote URL: drop it right away
        self.git("remote", "set-url", "origin", scrub(clone_url, self.secrets))
        self.git_ok(
            "config", "user.name", GIT_IDENTITY["name"]
        )  # commits are made inside the sandbox
        self.git_ok("config", "user.email", GIT_IDENTITY["email"])
        head = self.git_ok("rev-parse", "HEAD").strip()
        if not _SHA_RE.match(head):
            raise SandboxError(
                f"could not read HEAD after clone ({scrub(head, self.secrets)[:60]!r})"
            )
        self.base_sha = head
        self.branch = branch
        self.record(f"prepared {scrub(clone_url, self.secrets)}@{branch} at {head[:12]}")

    def _note_written(self, path: str) -> None:
        self.written_paths.add(self.repo_path(path))

    def _stage_new_files(self) -> None:
        """``git add --intent-to-add`` the files this sandbox created, so they show up in
        ``git diff``. Never ``--all``: untracked files the install or the test run left
        behind (lockfiles, ``*.egg-info``, coverage/report files) are not the patch."""
        present = [p for p in sorted(self.written_paths) if self.file_exists(p)]
        if present:
            # an ignored path makes git exit 1 but the others are still added
            self.git("add", "--intent-to-add", "--", *present)

    def diff(self) -> str:
        """Unified diff against the base sha: tracked files plus the files this sandbox
        wrote (untracked install/test artefacts are left out)."""
        if not self.workdir or not self.base_sha:
            raise SandboxError("sandbox is not prepared")
        self._stage_new_files()
        res = self.git("diff", "--no-color", "--no-ext-diff", "--binary", self.base_sha, "--", ".")
        if not res.ok:
            raise SandboxError(f"git diff failed: {scrub(res.tail(10), self.secrets)}")
        return res.stdout

    def changed_files(self) -> list[str]:
        if not self.workdir or not self.base_sha:
            raise SandboxError("sandbox is not prepared")
        self._stage_new_files()
        res = self.git("diff", "--name-only", self.base_sha, "--", ".")
        files = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        return sorted(dict.fromkeys(files))

    def apply_patch(self, patch: str | dict[str, Any] | list[dict[str, Any]]) -> tuple[bool, str]:
        """Apply a unified diff (``git apply --check`` then ``git apply``) or one or more
        replacement patches ``{"path", "old", "new"}``. Returns ``(ok, message)``."""
        if isinstance(patch, dict):
            return self._apply_replacement(patch)
        if isinstance(patch, list):
            msgs: list[str] = []
            for p in patch:
                ok, msg = self._apply_replacement(p)
                msgs.append(msg)
                if not ok:
                    return False, "; ".join(msgs)
            return True, "; ".join(msgs)
        return self._apply_unified(str(patch))

    def _apply_replacement(self, patch: dict[str, Any]) -> tuple[bool, str]:
        try:
            path = self.repo_path(str(patch.get("path") or ""))
        except SandboxError as e:
            return False, str(e)
        old = patch.get("old")
        new = patch.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            return False, "'old' and 'new' must be strings"
        if not self.file_exists(path):
            if old == "":
                self.write_file(path, new)
                self.record(f"created {path}")
                return True, f"created {path}"
            return False, f"{path} does not exist"
        try:
            text = self.read_file(path)
        except SandboxError as e:
            return False, str(e)
        try:
            updated = apply_replacement(text, old, new)
        except ReplacementError as e:
            return False, f"{path}: {e}"
        self.write_file(path, updated)
        self.record(f"patched {path} ({len(old.splitlines())} -> {len(new.splitlines())} lines)")
        return True, f"patched {path}"

    def _apply_unified(self, diff_text: str) -> tuple[bool, str]:
        if not diff_text.strip():
            return False, "empty patch"
        if not diff_text.endswith("\n"):
            diff_text += "\n"
        check = self.git("apply", "--check", "--whitespace=nowarn", "-", input=diff_text)
        if not check.ok:
            return False, f"patch does not apply: {scrub(check.tail(10), self.secrets)}"
        res = self.git("apply", "--whitespace=nowarn", "-", input=diff_text)
        if not res.ok:
            return False, f"git apply failed: {scrub(res.tail(10), self.secrets)}"
        for fp in parse_unified_diff(diff_text):
            norm = normalize_repo_path(fp.new_path or "") if fp.new_path else None
            if norm:
                self.written_paths.add(norm)
        self.record("applied unified diff")
        return True, "patch applied"

    def patch_applies_cleanly(self, diff_text: str) -> tuple[bool, str]:
        """Prove ``diff_text`` (normally ``self.diff()``) is a faithful, cleanly applying
        patch: it must reverse-apply on the working tree and forward-apply on a fresh
        checkout of the base sha (``git worktree``)."""
        if not diff_text.strip():
            return False, "empty diff"
        if not diff_text.endswith("\n"):
            diff_text += "\n"
        rev = self.git("apply", "--check", "--reverse", "--whitespace=nowarn", "-", input=diff_text)
        if not rev.ok:
            return (
                False,
                f"diff does not match the working tree: {scrub(rev.tail(8), self.secrets)}",
            )
        wt = self._scratch_path(f"base-check-{uuid.uuid4().hex[:6]}")
        added = self.git("worktree", "add", "--quiet", "--detach", wt, self.base_sha or "HEAD")
        if not added.ok:
            # worktrees are a nice-to-have; the reverse check already passed
            return True, "applies (reverse check; worktree unavailable)"
        try:
            fwd = self.git("apply", "--check", "--whitespace=nowarn", "-", cwd=wt, input=diff_text)
            if not fwd.ok:
                return (
                    False,
                    f"patch does not apply on a fresh checkout: {scrub(fwd.tail(8), self.secrets)}",
                )
        finally:
            self.git("worktree", "remove", "--force", wt)
            self.git("worktree", "prune")
        return True, "applies cleanly on a fresh checkout of the base sha"

    def _scratch_path(self, name: str) -> str:
        raise NotImplementedError

    def head_sha(self) -> str:
        return self.git_ok("rev-parse", "HEAD").strip()


# --------------------------------------------------------------------------------------
# Local backend
# --------------------------------------------------------------------------------------


class LocalSandbox(Sandbox):
    """Temp directory + subprocess. No network restriction (documented)."""

    backend = "local"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        wall_clock_s: float | None = None,
        job_id: str | None = None,
        root: str | Path | None = None,
        resource_limits: bool = True,
    ):
        super().__init__(settings=settings, wall_clock_s=wall_clock_s, job_id=job_id)
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="kintsua-fix-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.resource_limits = resource_limits
        self._mem_bytes = parse_memory(self.settings.sandbox_memory)
        self._cpu_s = int(self.deadline.total_s * max(1.0, float(self.settings.sandbox_cpus)))

    # ---- lifecycle ------------------------------------------------------------------

    def prepare(self, clone_url: str, branch: str, *, sha: str | None = None) -> str:
        self.secrets = secrets_in_url(clone_url)
        dest = self.root / "repo"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        self._clone(clone_url, branch, str(dest), sha)
        self.workdir = str(dest)
        self._after_clone(clone_url, branch, sha)
        return self.workdir

    def _clone(self, clone_url: str, branch: str, dest: str, sha: str | None) -> None:
        """Run ``git clone``; tests replace this to copy a fixture tree instead."""
        args = self._git_clone_args(clone_url, branch, dest, sha)
        res = self.run(
            args,
            timeout_s=min(float(self.settings.fix_test_timeout_s), self.deadline.remaining()),
            cwd=str(self.root),
        )
        if res.timed_out:
            raise SandboxTimeout("git clone timed out")
        if not res.ok:
            raise SandboxError(
                f"git clone failed (exit {res.exit_code}): {scrub(res.tail(15), self.secrets)}"
            )

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- processes ------------------------------------------------------------------

    def _env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("GIT_") or k in ("GIT_EXEC_PATH", "GIT_TEMPLATE_DIR")
        }
        venv_bin = str(Path(sys.executable).parent)
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "/bin/true" if os.path.exists("/bin/true") else "true",
                "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
                "GIT_LFS_SKIP_SMUDGE": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": GIT_IDENTITY["name"],
                "GIT_AUTHOR_EMAIL": GIT_IDENTITY["email"],
                "GIT_COMMITTER_NAME": GIT_IDENTITY["name"],
                "GIT_COMMITTER_EMAIL": GIT_IDENTITY["email"],
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "CI": "1",
                "KINTSUA_SANDBOX": "local",
            }
        )
        env.setdefault("HOME", str(self.root / "home"))
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        if extra:
            env.update({str(k): str(v) for k, v in extra.items()})
        return env

    def _preexec(self):
        if not self.resource_limits:
            return None
        mem = self._mem_bytes
        cpu = self._cpu_s

        def apply_limits() -> None:  # pragma: no cover - runs in the child
            try:
                import resource

                if mem:
                    resource.setrlimit(resource.RLIMIT_DATA, (mem, mem))
                if cpu:
                    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
            except Exception:  # noqa: BLE001 - best effort
                pass

        return apply_limits

    def run(
        self,
        cmd: list[str] | str,
        *,
        timeout_s: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input: str | None = None,
        network: bool = False,
    ) -> RunResult:
        self.deadline.check("run")
        budget = self.deadline.clamp(timeout_s if timeout_s is not None else DEFAULT_RUN_TIMEOUT_S)
        if isinstance(cmd, str):
            argv: list[str] = ["sh", "-c", cmd]
            shown = cmd
        else:
            argv = [str(c) for c in cmd]
            shown = shlex.join(argv)
        workdir = cwd or self.workdir or str(self.root)
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell unless asked
                argv,
                cwd=workdir,
                env=self._env(env),
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                preexec_fn=self._preexec(),
            )
        except FileNotFoundError as e:
            res = RunResult(127, "", f"command not found: {e}", 0.0, command=shown)
            self.record(f"$ {shown} -> 127 (not found)")
            return res
        timed_out = False
        try:
            out, err = proc.communicate(
                input=input.encode("utf-8") if input is not None else None, timeout=budget
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - the group is dead
                out, err = b"", b""
        duration = time.monotonic() - t0
        stdout = scrub(_decode(out), self.secrets, generic=False)
        stderr = scrub(_decode(err), self.secrets, generic=False)
        code = 124 if timed_out else int(proc.returncode or 0)
        self.record(
            f"$ {scrub(shown, self.secrets)} -> {code}{' (timed out)' if timed_out else ''} "
            f"in {duration:.1f}s"
        )
        return RunResult(code, stdout, stderr, duration, timed_out=timed_out, command=shown)

    # ---- files ----------------------------------------------------------------------

    def _abs(self, path: str) -> Path:
        if not self.workdir:
            raise SandboxError("sandbox is not prepared")
        return Path(self.workdir) / self.repo_path(path)

    def file_exists(self, path: str) -> bool:
        try:
            return self._abs(path).is_file()
        except SandboxError:
            return False

    def read_file(self, path: str) -> str:
        p = self._abs(path)
        if not p.is_file():
            raise SandboxError(f"{path} does not exist")
        data = p.read_bytes()
        if looks_binary(data):
            raise SandboxError(f"{path} is a binary file")
        return data.decode("utf-8", errors="replace")

    def write_file(self, path: str, text: str) -> None:
        p = self._abs(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self._note_written(path)

    def list_files(self, glob: str = "**/*") -> list[str]:
        if not self.workdir:
            raise SandboxError("sandbox is not prepared")
        root = Path(self.workdir)
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            rel_dir = os.path.relpath(dirpath, root)
            for fn in filenames:
                rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
                rel = rel.replace(os.sep, "/")
                if match_glob(rel, glob):
                    out.append(rel)
        return sorted(out)

    def _scratch_path(self, name: str) -> str:
        return str(self.root / name)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:  # noqa: BLE001
        with _suppress():
            proc.kill()


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> bool:
        return True


def _decode(b: bytes | None) -> str:
    return (b or b"").decode("utf-8", errors="replace")


def match_glob(rel: str, glob: str) -> bool:
    """``**`` aware glob match on a POSIX relative path (``**/*.py``, ``src/**``,
    ``*.ts``). A bare ``*.py`` matches files at any depth, as users expect."""
    if not glob or glob in ("**", "**/*", "*"):
        return True
    if "/" not in glob and "**" not in glob:
        return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], glob)
    pattern = re.escape(glob)
    pattern = pattern.replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
    pattern = pattern.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(pattern, rel) is not None


# --------------------------------------------------------------------------------------
# Docker backend
# --------------------------------------------------------------------------------------


class DockerSandbox(Sandbox):
    """One container per job; see the module docstring for the network model."""

    backend = "docker"
    WORK_MOUNT = "/work"
    REPO_DIR = "/work/repo"
    SCRATCH_DIR = "/work/scratch"
    #: the image's venv (first on PATH); a per-job volume so installs reach the test container
    VENV_DIR = "/opt/venv"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        wall_clock_s: float | None = None,
        job_id: str | None = None,
        client: Any = None,
        user: str = "sandbox",
    ):
        super().__init__(settings=settings, wall_clock_s=wall_clock_s, job_id=job_id)
        if client is None:
            import docker  # lazy: the SDK is only needed by this backend

            client = docker.from_env()
        self.client = client
        self.user = user
        self.image = self.settings.sandbox_image
        self.volume: Any = None
        self.venv_volume: Any = None
        self.container: Any = None
        self._env_base = {
            "HOME": "/home/sandbox",
            "CI": "1",
            "KINTSUA_SANDBOX": "docker",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/true",
            "GIT_AUTHOR_NAME": GIT_IDENTITY["name"],
            "GIT_AUTHOR_EMAIL": GIT_IDENTITY["email"],
            "GIT_COMMITTER_NAME": GIT_IDENTITY["name"],
            "GIT_COMMITTER_EMAIL": GIT_IDENTITY["email"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "npm_config_update_notifier": "false",
            # installs must land on a volume both containers see: pip (and poetry, which
            # honours the image's activated VIRTUAL_ENV) write to the shared /opt/venv; an
            # env poetry creates itself goes in the project (.venv under /work/repo)
            "POETRY_VIRTUALENVS_IN_PROJECT": "true",
        }

    # ---- lifecycle ------------------------------------------------------------------

    def _host_config(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "mem_limit": self.settings.sandbox_memory,
            "nano_cpus": int(float(self.settings.sandbox_cpus) * 1_000_000_000),
            "pids_limit": 512,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
            "user": self.user,
            "labels": {"kintsua.job": self.job_id, "kintsua.component": "fixer"},
        }
        volumes: dict[str, dict[str, str]] = {}
        if self.volume is not None:
            volumes[self.volume.name] = {"bind": self.WORK_MOUNT, "mode": "rw"}
        if self.venv_volume is not None:
            volumes[self.venv_volume.name] = {"bind": self.VENV_DIR, "mode": "rw"}
        if volumes:
            cfg["volumes"] = volumes
        return cfg

    def egress_hosts(self) -> dict[str, str]:
        """Allowlisted host -> resolved IP (resolution happens on the worker)."""
        proxy = self.settings.sandbox_https_proxy or os.environ.get("SANDBOX_HTTPS_PROXY")
        names = list(self.settings.sandbox_egress_allowlist)
        if proxy:
            host = urlsplit(proxy).hostname
            names = [host] if host else []
        out: dict[str, str] = {}
        for name in names:
            try:
                infos = socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
            except OSError:
                log.warning("sandbox egress: could not resolve %s", name)
                continue
            for info in infos:
                ip = info[4][0]
                if ":" not in ip:  # keep it to IPv4 for the bridge network
                    out[name] = ip
                    break
        return out

    def _network_env(self) -> dict[str, str]:
        env = dict(self._env_base)
        proxy = self.settings.sandbox_https_proxy or os.environ.get("SANDBOX_HTTPS_PROXY")
        if proxy:
            env.update(
                {
                    "HTTPS_PROXY": proxy,
                    "HTTP_PROXY": proxy,
                    "https_proxy": proxy,
                    "http_proxy": proxy,
                }
            )
        env["KINTSUA_EGRESS_ALLOWLIST"] = ",".join(self.settings.sandbox_egress_allowlist)
        return env

    def prepare(self, clone_url: str, branch: str, *, sha: str | None = None) -> str:
        self.secrets = secrets_in_url(clone_url)
        self.volume = self.client.volumes.create(
            name=f"kintsua-fix-{self.job_id}", labels={"kintsua.job": self.job_id}
        )
        # created before the first container mounts it, so Docker seeds it from the image
        self.venv_volume = self.client.volumes.create(
            name=f"kintsua-venv-{self.job_id}", labels={"kintsua.job": self.job_id}
        )
        # the image's /work is owned by the sandbox user, so a fresh volume mounted there
        # inherits that ownership; only the scratch dir has to exist
        self._networked_run(["mkdir", "-p", self.SCRATCH_DIR], timeout_s=60)
        args = self._git_clone_args(clone_url, branch, self.REPO_DIR, sha)
        res = self._networked_run(
            args, timeout_s=min(float(self.settings.fix_test_timeout_s), self.deadline.remaining())
        )
        if res.timed_out:
            raise SandboxTimeout("git clone timed out")
        if not res.ok:
            raise SandboxError(
                f"git clone failed (exit {res.exit_code}): {scrub(res.tail(15), self.secrets)}"
            )
        self.container = self.client.containers.create(
            self.image,
            command=["sleep", "infinity"],
            network_mode="none",
            working_dir=self.REPO_DIR,
            environment=self._env_base,
            detach=True,
            **self._host_config(),
        )
        self.container.start()
        self.workdir = self.REPO_DIR
        self._after_clone(clone_url, branch, sha)
        return self.workdir

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        for obj in (self.container, self.volume, self.venv_volume):
            if obj is None:
                continue
            try:
                obj.remove(force=True)
            except Exception as e:  # noqa: BLE001
                log.warning("sandbox cleanup: %s", scrub(str(e), self.secrets))
        self.container = None
        self.volume = None
        self.venv_volume = None

    # ---- processes ------------------------------------------------------------------

    @staticmethod
    def _script(cmd: list[str] | str) -> str:
        return cmd if isinstance(cmd, str) else shlex.join([str(c) for c in cmd])

    def _networked_run(
        self,
        cmd: list[str] | str,
        *,
        timeout_s: float,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input: str | None = None,
    ) -> RunResult:
        """A short-lived container on the bridge network with the name-level allowlist."""
        self.deadline.check("networked step")
        budget = self.deadline.clamp(timeout_s)
        script = self._script(cmd)
        shown = script
        environment = {**self._network_env(), **(env or {})}
        hosts = self.egress_hosts()
        cfg = self._host_config()
        container = self.client.containers.create(
            self.image,
            command=["sh", "-c", script],
            network_mode="bridge",
            dns=["127.0.0.1"],  # nothing answers: only extra_hosts resolve
            extra_hosts=hosts,
            working_dir=cwd or self.WORK_MOUNT,
            environment=environment,
            stdin_open=input is not None,
            detach=True,
            **cfg,
        )
        t0 = time.monotonic()
        timed_out = False
        try:
            container.start()
            if input is not None:
                self._feed_stdin(container, input)
            try:
                status = container.wait(timeout=budget)
                code = int(status.get("StatusCode", 1)) if isinstance(status, dict) else int(status)
            except Exception:  # noqa: BLE001 - requests timeout -> wall clock
                timed_out = True
                code = 124
                with _suppress():
                    container.kill()
            stdout = _decode(container.logs(stdout=True, stderr=False))
            stderr = _decode(container.logs(stdout=False, stderr=True))
        finally:
            with _suppress():
                container.remove(force=True)
        duration = time.monotonic() - t0
        self.record(f"$ [net] {scrub(shown, self.secrets)} -> {code} in {duration:.1f}s")
        return RunResult(
            code,
            scrub(stdout, self.secrets, generic=False),
            scrub(stderr, self.secrets, generic=False),
            duration,
            timed_out,
            shown,
        )

    def _feed_stdin(self, container: Any, data: str) -> None:  # pragma: no cover - needs docker
        sock = container.attach_socket(params={"stdin": 1, "stream": 1})
        try:
            sock._sock.sendall(data.encode("utf-8"))
            sock._sock.shutdown(socket.SHUT_WR)
        finally:
            sock.close()

    def run(
        self,
        cmd: list[str] | str,
        *,
        timeout_s: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input: str | None = None,
        network: bool = False,
    ) -> RunResult:
        self.deadline.check("run")
        budget = self.deadline.clamp(timeout_s if timeout_s is not None else DEFAULT_RUN_TIMEOUT_S)
        if network:
            return self._networked_run(
                cmd, timeout_s=budget, cwd=cwd or self.workdir, env=env, input=input
            )
        if self.container is None:
            raise SandboxError("sandbox is not prepared")
        script = self._script(cmd)
        shown = script
        if input is not None:
            stdin_path = f"{self.SCRATCH_DIR}/stdin-{uuid.uuid4().hex[:8]}"
            self._put_file(stdin_path, input)
            script = f"{script} < {shlex.quote(stdin_path)}"
        secs = max(1, int(budget))
        argv = ["timeout", "-k", "5", str(secs), "sh", "-c", script]
        t0 = time.monotonic()
        code, output = self.container.exec_run(
            argv,
            workdir=cwd or self.workdir,
            environment={**self._env_base, **(env or {})},
            demux=True,
            user=self.user,
        )
        duration = time.monotonic() - t0
        out, err = (output or (b"", b""))[0], (output or (b"", b""))[1]
        code = int(code if code is not None else 1)
        timed_out = code == 124 and duration >= secs - 1
        self.record(
            f"$ {scrub(shown, self.secrets)} -> {code}{' (timed out)' if timed_out else ''} in {duration:.1f}s"
        )
        return RunResult(
            code,
            scrub(_decode(out), self.secrets, generic=False),
            scrub(_decode(err), self.secrets, generic=False),
            duration,
            timed_out,
            shown,
        )

    # ---- files (tar streams through the Docker API) -----------------------------------

    def _abs(self, path: str) -> str:
        if not self.workdir:
            raise SandboxError("sandbox is not prepared")
        return f"{self.workdir}/{self.repo_path(path)}"

    def _get_bytes(self, abs_path: str) -> bytes | None:
        try:
            stream, _stat = self.container.get_archive(abs_path)
        except Exception:  # noqa: BLE001 - NotFound
            return None
        buf = io.BytesIO()
        for chunk in stream:
            buf.write(chunk)
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:*") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    f = tar.extractfile(member)
                    return f.read() if f else b""
        return None

    def _put_file(self, abs_path: str, text: str) -> None:
        directory, name = abs_path.rsplit("/", 1)
        self.container.exec_run(["mkdir", "-p", directory], user=self.user)
        buf = io.BytesIO()
        data = text.encode("utf-8")
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        self.container.put_archive(directory, buf.getvalue())

    def file_exists(self, path: str) -> bool:
        try:
            abs_path = self._abs(path)
        except SandboxError:
            return False
        code, _ = self.container.exec_run(["test", "-f", abs_path], user=self.user)
        return code == 0

    def read_file(self, path: str) -> str:
        data = self._get_bytes(self._abs(path))
        if data is None:
            raise SandboxError(f"{path} does not exist")
        if looks_binary(data):
            raise SandboxError(f"{path} is a binary file")
        return data.decode("utf-8", errors="replace")

    def write_file(self, path: str, text: str) -> None:
        self._put_file(self._abs(path), text)
        self._note_written(path)

    def list_files(self, glob: str = "**/*") -> list[str]:
        if not self.workdir:
            raise SandboxError("sandbox is not prepared")
        prune = " -o ".join(f"-name {shlex.quote(d)}" for d in sorted(IGNORED_DIRS))
        script = (
            f"cd {shlex.quote(self.workdir)} && find . \\( {prune} \\) -prune -o -type f -print"
        )
        code, output = self.container.exec_run(["sh", "-c", script], demux=True, user=self.user)
        out = _decode((output or (b"", b""))[0])
        files = [ln[2:] if ln.startswith("./") else ln for ln in out.splitlines() if ln.strip()]
        return sorted(f for f in files if match_glob(f, glob))

    def _scratch_path(self, name: str) -> str:
        return f"{self.SCRATCH_DIR}/{name}"


# --------------------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------------------


def make_sandbox(
    settings: Settings | None = None,
    *,
    job_id: str | None = None,
    wall_clock_s: float | None = None,
) -> Sandbox:
    """The backend ``settings.sandbox_backend`` selects."""
    s = settings or get_settings()
    if s.sandbox_backend == "docker":
        return DockerSandbox(settings=s, job_id=job_id, wall_clock_s=wall_clock_s)
    return LocalSandbox(settings=s, job_id=job_id, wall_clock_s=wall_clock_s)


_registry_lock = threading.Lock()
_active: dict[str, Sandbox] = {}


def register_active(sb: Sandbox) -> None:
    with _registry_lock:
        _active[sb.job_id] = sb


def unregister_active(sb: Sandbox) -> None:
    with _registry_lock:
        _active.pop(sb.job_id, None)


def active_sandboxes() -> list[Sandbox]:
    with _registry_lock:
        return list(_active.values())


__all__ = [
    "DEFAULT_RUN_TIMEOUT_S",
    "Deadline",
    "DockerSandbox",
    "LocalSandbox",
    "RunResult",
    "Sandbox",
    "SandboxError",
    "SandboxTimeout",
    "make_sandbox",
    "match_glob",
    "parse_memory",
    "scrub",
    "secrets_in_url",
    "tail_text",
]
