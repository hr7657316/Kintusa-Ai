"""Shallow clone of a customer repo with an installation token, token never logged.

``clone_repo`` runs ``git clone --depth N --single-branch --branch <default>`` with
``GIT_TERMINAL_PROMPT=0`` (no interactive credential prompts), a wall-clock timeout, and
returns the HEAD sha. Any error message is scrubbed of the token before it leaves this
module. The indexer parses the tree in-process (tree-sitter never executes repo code)
inside ``temp_clone_dir`` which is always removed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300.0
_USERINFO_RE = re.compile(r"://[^/@\s]+@")


class CloneError(RuntimeError):
    """Clone failed; the message is safe to store (token scrubbed)."""


def scrub(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove credentials from anything git printed: explicit secrets and ``://user:pw@``."""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 4:
            out = out.replace(s, "***")
    return _USERINFO_RE.sub("://***@", out)


def _secrets_in(clone_url: str) -> tuple[str, ...]:
    try:
        parts = urlsplit(clone_url)
    except ValueError:
        return ()
    found = []
    if parts.password:
        found.append(parts.password)
    if parts.username and parts.username not in ("x-access-token", "oauth2", "git"):
        found.append(parts.username)
    return tuple(found)


def _git_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") or k in ("GIT_EXEC_PATH", "GIT_TEMPLATE_DIR")
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/true" if os.path.exists("/bin/true") else "true",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
    )
    env.setdefault("HOME", tempfile.gettempdir())
    return env


def _run_git(
    args: list[str], *, cwd: str | None, timeout_s: float, secrets: tuple[str, ...]
) -> str:
    cmd = ["git", *args]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            cwd=cwd,
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise CloneError("git is not installed on this worker") from e
    except subprocess.TimeoutExpired as e:
        raise CloneError(f"git {_subcommand(args)} timed out after {timeout_s:.0f}s") from e
    if proc.returncode != 0:
        err = scrub((proc.stderr or proc.stdout or "").strip(), secrets)
        raise CloneError(f"git {_subcommand(args)} failed (exit {proc.returncode}): {err[-1500:]}")
    return proc.stdout


def _subcommand(args: list[str]) -> str:
    """``["-c", "x=y", "clone", ...]`` -> ``clone``."""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "-c":
            skip = True
            continue
        if not a.startswith("-"):
            return a
    return args[0] if args else "git"


def clone_repo(
    clone_url: str,
    default_branch: str | None,
    dest: str | Path,
    *,
    depth: int = 1,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Shallow-clone ``clone_url`` at ``default_branch`` into ``dest``; return the HEAD sha.

    If the named branch does not exist on the remote the remote HEAD is cloned instead.
    """
    dest = Path(dest)
    secrets = _secrets_in(clone_url)
    base = [
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
        f"--depth={max(1, int(depth))}",
    ]
    attempts: list[list[str]] = []
    if default_branch:
        attempts.append([*base, "--branch", default_branch, clone_url, str(dest)])
    attempts.append([*base, clone_url, str(dest)])
    last: CloneError | None = None
    for args in attempts:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        try:
            _run_git(args, cwd=None, timeout_s=timeout_s, secrets=secrets)
            last = None
            break
        except CloneError as e:
            last = e
            msg = str(e)
            branch_missing = "not found in upstream" in msg or "Remote branch" in msg
            if not branch_missing:
                break
            log.info(
                "branch %r missing on remote; cloning the default HEAD instead", default_branch
            )
    if last is not None:
        raise last
    sha = _run_git(["rev-parse", "HEAD"], cwd=str(dest), timeout_s=60, secrets=secrets).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CloneError(f"could not read HEAD sha after clone ({scrub(sha, secrets)[:80]!r})")
    # the clone's own .git holds the token in its remote URL: drop it right away
    with contextlib.suppress(CloneError):
        _run_git(
            ["remote", "set-url", "origin", scrub(clone_url, secrets)],
            cwd=str(dest),
            timeout_s=30,
            secrets=secrets,
        )
    return sha


@contextmanager
def temp_clone_dir(prefix: str = "kintsua-index-") -> Iterator[Path]:
    """A fresh temp directory that is removed no matter what (the clone's lifetime)."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def read_repo_file(
    root: str | Path, *names: str, max_bytes: int = 256_000
) -> tuple[str, str] | None:
    """First existing regular (non-symlink) file among ``names``; returns (name, text)."""
    root = Path(root)
    for name in names:
        p = root / name
        if p.is_symlink() or not p.is_file():
            continue
        try:
            if p.stat().st_size > max_bytes:
                continue
            return name, p.read_text("utf-8", errors="replace")
        except OSError:
            continue
    return None
