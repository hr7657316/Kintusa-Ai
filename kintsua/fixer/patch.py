"""Patch helpers shared by the sandbox, the agent and the validator.

Two patch shapes exist in the fixer:

* a **unified diff** (what ``git diff`` produces and ``git apply`` consumes); and
* a **replacement patch** ``{"path": ..., "old": ..., "new": ...}`` (what the agent's
  ``apply_patch`` tool uses: an exact, unambiguous string replacement in one file).

This module parses unified diffs into per-file hunks (so the validator can count changed
lines, find touched files and inspect added lines), performs replacement patches on text,
and holds the **refusal rules**: file-name patterns and added-line patterns that mean a
patch touches authentication or secrets handling, which the fixer never ships in v0.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------
# Refusal rules (authentication / secrets handling)
# --------------------------------------------------------------------------------------

#: Basename globs (case-insensitive). Any touched file matching one is refused.
SECRET_FILE_PATTERNS: tuple[str, ...] = (
    "*.env",
    ".env*",
    "*secret*",
    "*credential*",
    "auth*.py",
    "auth*.ts",
    "auth*.js",
    "*token*",
    "*.pem",
    "*.key",
    "id_rsa*",
)

#: Added lines (``+`` in a hunk) matching one of these are refused.
SECRET_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bapi[_-]?key\s*[:=]"),
    re.compile(r"(?i)\b(access|auth|secret|refresh|bearer|session|id)?[_-]?token\s*[:=]"),
    re.compile(r"(?i)\bsecret(_key)?\s*[:=]"),
    re.compile(r"(?i)\bpassword\s*[:=]"),
    re.compile(r"(?i)\bauthorization\b"),
    re.compile(r"(?i)\bclient[_-]?secret\s*[:=]"),
    re.compile(r"(?i)\bprivate[_-]?key\s*[:=]"),
    re.compile(r"(?i)(sk|rk|pk)_(live|test)_[0-9a-zA-Z]{8,}"),
    re.compile(r"(?i)gh[pousr]_[0-9a-zA-Z]{20,}"),
    re.compile(r"(?i)sk-ant-[0-9a-zA-Z\-_]{20,}"),
)


def is_secret_path(path: str) -> bool:
    """True when ``path`` looks like it holds credentials or auth logic."""
    norm = path.replace("\\", "/").strip("/")
    if not norm:
        return False
    parts = norm.split("/")
    base = parts[-1].lower()
    if any(fnmatch.fnmatchcase(base, pat.lower()) for pat in SECRET_FILE_PATTERNS):
        return True
    # directory names such as ``secrets/`` or ``credentials/``
    return any(("secret" in p.lower() or "credential" in p.lower()) for p in parts[:-1])


def secret_line_hits(line: str) -> list[str]:
    """The refusal patterns an added line matches (empty when it is clean)."""
    hits: list[str] = []
    for pat in SECRET_LINE_PATTERNS:
        if pat.search(line):
            hits.append(pat.pattern)
    return hits


# --------------------------------------------------------------------------------------
# Unified diff parsing
# --------------------------------------------------------------------------------------


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)  # with the leading ' ', '+', '-'

    @property
    def added(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln.startswith("+")]

    @property
    def removed(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln.startswith("-")]

    @property
    def new_range(self) -> tuple[int, int]:
        """Inclusive line range in the new file this hunk covers."""
        return self.new_start, max(self.new_start, self.new_start + self.new_count - 1)

    @property
    def old_range(self) -> tuple[int, int]:
        return self.old_start, max(self.old_start, self.old_start + self.old_count - 1)


@dataclass
class FilePatch:
    path: str  # new path (or old path for a deletion)
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False

    @property
    def added_lines(self) -> int:
        return sum(len(h.added) for h in self.hunks)

    @property
    def removed_lines(self) -> int:
        return sum(len(h.removed) for h in self.hunks)

    @property
    def changed_lines(self) -> int:
        return self.added_lines + self.removed_lines

    def touches_line(self, line: int, slack: int = 0) -> bool:
        """Whether a (new-file) line number falls inside one of this file's hunks."""
        for h in self.hunks:
            lo, hi = h.new_range
            if lo - slack <= line <= hi + slack:
                return True
            lo, hi = h.old_range
            if lo - slack <= line <= hi + slack:
                return True
        return False


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r'^diff --git (?:"?a/(.*?)"?) (?:"?b/(.*?)"?)$')


def _strip_prefix(p: str) -> str | None:
    p = p.strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    # ``--- a/path\tTIMESTAMP`` forms
    return p.split("\t", 1)[0]


def parse_unified_diff(text: str) -> list[FilePatch]:
    """Parse a (git) unified diff into ``FilePatch`` objects. Tolerant of ``diff --git``
    headers, ``index`` lines, mode lines, binary notices and renames."""
    patches: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        m = _DIFF_GIT_RE.match(line)
        if m:
            cur = FilePatch(path=m.group(2), old_path=m.group(1), new_path=m.group(2))
            patches.append(cur)
            hunk = None
            continue
        if line.startswith("--- "):
            old = _strip_prefix(line[4:])
            if cur is None or cur.hunks or cur.is_binary:
                # plain unified diff without ``diff --git`` headers
                cur = FilePatch(path=old or "", old_path=old, new_path=None)
                patches.append(cur)
            cur.old_path = old
            cur.is_new = old is None
            hunk = None
            continue
        if line.startswith("+++ "):
            new = _strip_prefix(line[4:])
            if cur is None:
                cur = FilePatch(path=new or "", old_path=None, new_path=new)
                patches.append(cur)
            cur.new_path = new
            cur.is_deleted = new is None
            if new:
                cur.path = new
            elif cur.old_path:
                cur.path = cur.old_path
            hunk = None
            continue
        if cur is None:
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.is_binary = True
            continue
        if line.startswith("rename from "):
            cur.is_rename = True
            cur.old_path = line[len("rename from ") :]
            continue
        if line.startswith("rename to "):
            cur.is_rename = True
            cur.new_path = line[len("rename to ") :]
            cur.path = cur.new_path
            continue
        if line.startswith("new file mode"):
            cur.is_new = True
            continue
        if line.startswith("deleted file mode"):
            cur.is_deleted = True
            continue
        hm = _HUNK_RE.match(line)
        if hm:
            hunk = Hunk(
                old_start=int(hm.group(1)),
                old_count=int(hm.group(2) or "1"),
                new_start=int(hm.group(3)),
                new_count=int(hm.group(4) or "1"),
            )
            cur.hunks.append(hunk)
            continue
        if hunk is not None and line[:1] in (" ", "+", "-", "\\"):
            if line.startswith("\\"):
                continue  # "\ No newline at end of file"
            hunk.lines.append(line)
            continue
        if hunk is not None and line == "":
            hunk.lines.append(" ")
    return patches


def changed_line_count(diff_text: str) -> int:
    return sum(p.changed_lines for p in parse_unified_diff(diff_text))


def touched_paths(diff_text: str) -> list[str]:
    out: list[str] = []
    for p in parse_unified_diff(diff_text):
        for cand in (p.old_path, p.new_path, p.path):
            if cand and cand not in out:
                out.append(cand)
    return out


def added_lines(diff_text: str) -> list[tuple[str, str]]:
    """``(path, line)`` for every added line in the diff."""
    out: list[tuple[str, str]] = []
    for p in parse_unified_diff(diff_text):
        for h in p.hunks:
            out.extend((p.path, ln) for ln in h.added)
    return out


def diff_stat(diff_text: str) -> dict[str, tuple[int, int]]:
    """``{path: (added, removed)}``."""
    return {p.path: (p.added_lines, p.removed_lines) for p in parse_unified_diff(diff_text)}


# --------------------------------------------------------------------------------------
# Replacement patches
# --------------------------------------------------------------------------------------


class ReplacementError(ValueError):
    """``old`` was not found, or was found more than once."""


def apply_replacement(text: str, old: str, new: str) -> str:
    """Replace exactly one occurrence of ``old`` with ``new``. Raises ``ReplacementError``
    when ``old`` is absent or ambiguous; ``old`` must not be empty."""
    if not old:
        raise ReplacementError("'old' must not be empty")
    count = text.count(old)
    if count == 0:
        # a common model mistake is trailing/leading whitespace differences: try a
        # whitespace-normalised match on whole lines before giving up
        alt = _match_ignoring_trailing_ws(text, old)
        if alt is None:
            raise ReplacementError("'old' text was not found in the file (match must be exact)")
        start, end = alt
        return text[:start] + new + text[end:]
    if count > 1:
        raise ReplacementError(
            f"'old' text occurs {count} times; include more surrounding lines to make it unique"
        )
    return text.replace(old, new, 1)


def _match_ignoring_trailing_ws(text: str, old: str) -> tuple[int, int] | None:
    old_lines = [ln.rstrip() for ln in old.splitlines()]
    if not old_lines:
        return None
    lines = text.splitlines(keepends=True)
    stripped = [ln.rstrip("\r\n").rstrip() for ln in lines]
    hits: list[int] = []
    n = len(old_lines)
    for i in range(0, len(stripped) - n + 1):
        if stripped[i : i + n] == old_lines:
            hits.append(i)
    if len(hits) != 1:
        return None
    i = hits[0]
    start = sum(len(ln) for ln in lines[:i])
    end = start + sum(len(ln) for ln in lines[i : i + n])
    # keep the final newline of the block out of the replaced span when ``old`` had none
    if not old.endswith("\n") and lines[i + n - 1].endswith("\n"):
        end -= 1
        if lines[i + n - 1].endswith("\r\n"):
            end -= 1
    return start, end


def normalize_repo_path(path: str) -> str | None:
    """A repo-relative POSIX path with ``.``/``..`` resolved, or None when it escapes the
    repository root or is absolute."""
    if not path or path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path):
        return None
    norm = posixpath.normpath(path.replace("\\", "/"))
    if norm.startswith("../") or norm == ".." or norm == "." or norm == "":
        return None
    return norm


def looks_binary(data: bytes) -> bool:
    if b"\x00" in data[:8000]:
        return True
    sample = data[:8000]
    if not sample:
        return False
    text_chars = bytes(range(32, 127)) + b"\n\r\t\b\f\x1b"
    nontext = sum(1 for b in sample if b not in text_chars and b < 128)
    return nontext / max(1, len(sample)) > 0.3


__all__ = [
    "SECRET_FILE_PATTERNS",
    "SECRET_LINE_PATTERNS",
    "FilePatch",
    "Hunk",
    "ReplacementError",
    "added_lines",
    "apply_replacement",
    "changed_line_count",
    "diff_stat",
    "is_secret_path",
    "looks_binary",
    "normalize_repo_path",
    "parse_unified_diff",
    "secret_line_hits",
    "touched_paths",
]
