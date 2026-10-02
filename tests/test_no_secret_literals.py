"""No tracked file may contain a secret-shaped literal.

Redaction tests need fake credentials, but a fake that looks real still trips secret
scanners (GitGuardian, GitHub push protection) on every push. Build test credentials at
runtime from parts instead, e.g. ``"ghp_" + "a" * 36`` or two adjacent string literals."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SHAPES = re.compile(
    "|".join(
        [
            r"AIza[0-9A-Za-z_-]{30,}",
            r"eyJ[0-9A-Za-z_-]{8,}\.eyJ",
            r"hooks\.slack\.com/services/T[0-9A-Z]{6,}/B[0-9A-Z]{6,}/[0-9A-Za-z]{12,}",
            r"[Bb]earer [A-Za-z0-9._~+/-]{12,}",
            r"sk-(?:proj|ant|svcacct|admin)-[A-Za-z0-9_-]{10,}",
            r"sk_(?:live|test)_[A-Za-z0-9]{10,}",
            r"gh[pousr]_[0-9A-Za-z]{20,}",
            r"github_pat_[0-9A-Za-z_]{20,}",
            r"xox[abposr]-[0-9A-Za-z-]{8,}",
            r"xapp-[0-9]-[0-9A-Za-z-]{8,}",
            r"AKIA[0-9A-Z]{16}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"kts_[A-Za-z0-9_-]{20,}",
            r"npm_[A-Za-z0-9]{30,}",
            r"\bsk-[A-Za-z0-9]{20,}",
            # a Basic credential: base64 holding a digit, "+", "/" or "=" (not the prose word)
            r"(?i:basic)\s+(?=[A-Za-z0-9+/]*[\d+/=])[A-Za-z0-9+/]{8,}={0,2}",
            # DER key material (every PKCS#1/#8 key starts with MII...)
            r"MII[A-Za-z0-9+/]{10,}",
        ]
    )
)


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.split("\0") if p and p != "uv.lock"]


def test_no_secret_shaped_literals_in_tracked_files():
    hits = []
    for path in tracked_files():
        try:
            text = path.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            m = SHAPES.search(line)
            if m:
                hits.append(f"{path.relative_to(ROOT)}:{n}: {m.group(0)[:24]}...")
    assert not hits, "secret-shaped literals (build fakes at runtime):\n" + "\n".join(hits)
