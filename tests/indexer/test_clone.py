"""clone_repo against a local git repository (no network)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kintsua.indexer.clone import CloneError, clone_repo, read_repo_file, scrub, temp_clone_dir


def _git(*args: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def local_repo(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "origin"
    src.mkdir()
    _git("init", "-q", "-b", "trunk", cwd=src)
    (src / "app.py").write_text("import stripe\nstripe.Charge.create(amount=1)\n")
    _git("add", ".", cwd=src)
    _git("commit", "-q", "-m", "init", cwd=src)
    sha = _git("rev-parse", "HEAD", cwd=src)
    return src, sha


def test_clone_returns_head_sha_and_checks_out_files(local_repo, tmp_path: Path):
    src, sha = local_repo
    dest = tmp_path / "clone"
    got = clone_repo(f"file://{src}", "trunk", dest, timeout_s=60)
    assert got == sha
    assert (dest / "app.py").read_text().startswith("import stripe")
    # shallow and single-branch
    assert (dest / ".git" / "shallow").exists()


def test_missing_branch_falls_back_to_remote_head(local_repo, tmp_path: Path):
    src, sha = local_repo
    assert clone_repo(f"file://{src}", "main", tmp_path / "c2", timeout_s=60) == sha


def test_clone_errors_never_contain_the_token(tmp_path: Path):
    url = f"https://x-access-token:ghs_SECRETTOKEN123@127.0.0.1:9/{tmp_path.name}/nope.git"
    with pytest.raises(CloneError) as e:
        clone_repo(url, "main", tmp_path / "c3", timeout_s=30)
    msg = str(e.value)
    assert "ghs_SECRETTOKEN123" not in msg
    assert "git clone failed" in msg


def test_scrub_removes_userinfo_and_secrets():
    assert scrub("fatal: https://x-access-token:abc123@github.com/a/b.git failed", ("abc123",)) == (
        "fatal: https://***@github.com/a/b.git failed"
    )
    assert (
        "tok" not in scrub("token tokens tok", ("tokens",))
        or scrub("x tokens", ("tokens",)) == "x ***"
    )


def test_temp_clone_dir_is_always_removed():
    kept = None
    with pytest.raises(RuntimeError):
        with temp_clone_dir() as d:
            kept = d
            (d / "x").write_text("x")
            raise RuntimeError("boom")
    assert kept is not None and not kept.exists()


def test_read_repo_file_prefers_first_existing_regular_file(tmp_path: Path):
    (tmp_path / "kintsua.yml").write_text("mute: []\n")
    assert read_repo_file(tmp_path, ".kintsua.yml", "kintsua.yml") == ("kintsua.yml", "mute: []\n")
    assert read_repo_file(tmp_path, ".kintsua.yml") is None
    os.symlink(tmp_path / "kintsua.yml", tmp_path / ".kintsua.yml")
    assert read_repo_file(tmp_path, ".kintsua.yml") is None  # symlinks are ignored
