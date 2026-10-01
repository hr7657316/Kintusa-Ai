from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from kintsua.fixer.sandbox import (
    Deadline,
    LocalSandbox,
    SandboxError,
    SandboxTimeout,
    match_glob,
    parse_memory,
    scrub,
    secrets_in_url,
)
from tests.fixer.conftest import clone_url_for

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def test_prepare_clones_and_records_base_sha(sandbox: LocalSandbox, fixture_git_repo: Path):
    assert sandbox.workdir and Path(sandbox.workdir, "agent", "billing.py").is_file()
    assert sandbox.base_sha and SHA_RE.match(sandbox.base_sha)
    assert sandbox.branch == "main"
    assert sandbox.changed_files() == []
    assert sandbox.diff() == ""
    assert any("prepared" in line for line in sandbox.log_lines)


def test_prepare_pins_a_specific_sha(fixture_git_repo: Path, settings, tmp_path: Path):
    # add a second commit upstream, then prepare at the first one
    from tests.fixer.conftest import git

    first = git("rev-parse", "HEAD", cwd=fixture_git_repo).strip()
    (fixture_git_repo / "NOTE.md").write_text("later\n")
    git("add", "NOTE.md", cwd=fixture_git_repo)
    git("commit", "-q", "-m", "later", cwd=fixture_git_repo)
    try:
        sb = LocalSandbox(settings=settings, root=tmp_path / "sb")
        sb.prepare(clone_url_for(fixture_git_repo), "main", sha=first)
        assert sb.base_sha == first
        assert not Path(sb.workdir, "NOTE.md").exists()
        sb.cleanup()
    finally:
        git("reset", "-q", "--hard", first, cwd=fixture_git_repo)


def test_run_captures_output_and_exit_code(sandbox: LocalSandbox):
    res = sandbox.run(["sh", "-c", "echo out; echo err >&2; exit 3"], timeout_s=10)
    assert res.exit_code == 3 and res.stdout.strip() == "out" and res.stderr.strip() == "err"
    assert not res.timed_out and res.duration_s < 5
    assert "out" in res.output and "err" in res.output
    res = sandbox.run("echo $KINTSUA_SANDBOX", timeout_s=10)  # str commands go through sh
    assert res.stdout.strip() == "local"
    res = sandbox.run(["definitely-not-a-command-xyz"], timeout_s=10)
    assert res.exit_code == 127


def test_run_times_out_and_kills_the_process_group(sandbox: LocalSandbox):
    t0 = time.monotonic()
    res = sandbox.run(["sh", "-c", "sleep 30 & wait"], timeout_s=0.5)
    assert res.timed_out and res.exit_code == 124
    assert time.monotonic() - t0 < 10
    assert any("timed out" in line for line in sandbox.log_lines)


def test_run_passes_stdin(sandbox: LocalSandbox):
    res = sandbox.run(["cat"], timeout_s=10, input="hello\n")
    assert res.stdout == "hello\n"


def test_deadline_fails_cleanly(settings, tmp_path: Path):
    sb = LocalSandbox(settings=settings, root=tmp_path / "sb", wall_clock_s=0.0)
    assert sb.deadline.expired
    with pytest.raises(SandboxTimeout):
        sb.run(["true"], timeout_s=10)
    sb.cleanup()
    d = Deadline(100)
    assert 99 < d.remaining() <= 100
    assert d.clamp(5) == 5 and d.clamp(None) <= 100


def test_run_clamps_timeout_to_the_deadline(settings, tmp_path: Path, fixture_git_repo: Path):
    sb = LocalSandbox(settings=settings, root=tmp_path / "sb", wall_clock_s=60)
    sb.prepare(clone_url_for(fixture_git_repo), "main")
    sb.deadline.total_s = sb.deadline.elapsed() + 0.5  # half a second left
    res = sb.run(["sleep", "5"], timeout_s=30)
    assert res.timed_out
    sb.cleanup()


def test_scrub_removes_tokens_from_output_and_logs(sandbox: LocalSandbox):
    sandbox.secrets = ("ghs_fake_token_12345",)
    res = sandbox.run(
        ["sh", "-c", "echo https://x-access-token:ghs_fake_token_12345@github.com/acme/x.git"],
        timeout_s=10,
    )
    assert "ghs_fake_token_12345" not in res.stdout
    assert "***" in res.stdout
    assert all("ghs_fake_token_12345" not in line for line in sandbox.log_lines)
    assert secrets_in_url("https://x-access-token:abc123456@github.com/a/b.git") == ("abc123456",)
    scrubbed = scrub("token=ghp_abcdefghijklmnopqrstuvwxyz0123", ())
    assert "ghp_" not in scrubbed and "[REDACTED]" in scrubbed
    raw = scrub("token=ghp_abcdefghijklmnopqrstuvwxyz0123", (), generic=False)
    assert raw.startswith("token=ghp_")


def test_replacement_patch_diff_and_changed_files(sandbox: LocalSandbox):
    ok, msg = sandbox.apply_patch(
        {
            "path": "agent/billing.py",
            "old": "        source=source,\n",
            "new": "        payment_method=source,\n",
        }
    )
    assert ok, msg
    assert sandbox.changed_files() == ["agent/billing.py"]
    diff = sandbox.diff()
    assert "-        source=source," in diff and "+        payment_method=source," in diff
    assert diff.startswith("diff --git a/agent/billing.py b/agent/billing.py")
    # missing / ambiguous / bad path
    ok, msg = sandbox.apply_patch({"path": "agent/billing.py", "old": "nope", "new": "x"})
    assert not ok and "not found" in msg
    ok, msg = sandbox.apply_patch({"path": "agent/billing.py", "old": "a", "new": "b"})
    assert not ok and "occurs" in msg
    ok, msg = sandbox.apply_patch({"path": "../outside.py", "old": "a", "new": "b"})
    assert not ok and "outside" in msg
    ok, msg = sandbox.apply_patch({"path": "missing.py", "old": "a", "new": "b"})
    assert not ok and "does not exist" in msg


def test_unified_diff_patch_applies_and_rejects_garbage(sandbox: LocalSandbox):
    sandbox.apply_patch(
        {
            "path": "agent/billing.py",
            "old": "        source=source,\n",
            "new": "        payment_method=source,\n",
        }
    )
    diff = sandbox.diff()
    ok, msg = sandbox.patch_applies_cleanly(diff)
    assert ok, msg
    # revert, then apply the unified diff through git apply
    sandbox.git_ok("checkout", "--", ".")
    assert sandbox.changed_files() == []
    ok, msg = sandbox.apply_patch(diff)
    assert ok, msg
    assert "payment_method=source" in sandbox.read_file("agent/billing.py")
    ok, msg = sandbox.apply_patch("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-nothing\n+else\n")
    assert not ok and "does not apply" in msg
    ok, msg = sandbox.apply_patch("")
    assert not ok


def test_new_files_show_up_in_diff(sandbox: LocalSandbox):
    sandbox.write_file("agent/new_helper.py", "X = 1\n")
    assert "agent/new_helper.py" in sandbox.changed_files()
    assert "new file mode" in sandbox.diff()
    assert sandbox.file_exists("agent/new_helper.py")


def test_read_list_and_path_guards(sandbox: LocalSandbox):
    files = sandbox.list_files("**/*.py")
    assert "agent/billing.py" in files and "tests/test_billing.py" in files
    assert sandbox.list_files("*.toml") == ["pyproject.toml"]
    assert "agent/billing.py" in sandbox.list_files("agent/*.py")
    assert all(not f.startswith(".git/") for f in sandbox.list_files())
    with pytest.raises(SandboxError):
        sandbox.read_file("../../etc/passwd")
    with pytest.raises(SandboxError):
        sandbox.read_file(".git/config")
    with pytest.raises(SandboxError):
        sandbox.read_file("does/not/exist.py")
    Path(sandbox.workdir, "blob.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(SandboxError, match="binary"):
        sandbox.read_file("blob.bin")


def test_cleanup_removes_everything(fixture_git_repo: Path, settings, tmp_path: Path):
    sb = LocalSandbox(settings=settings, root=tmp_path / "sb")
    sb.prepare(clone_url_for(fixture_git_repo), "main")
    assert Path(sb.workdir).exists()
    sb.cleanup()
    assert not (tmp_path / "sb").exists()
    sb.cleanup()  # idempotent


def test_clone_failure_is_a_clean_error(settings, tmp_path: Path):
    sb = LocalSandbox(settings=settings, root=tmp_path / "sb")
    with pytest.raises(SandboxError, match="git clone failed"):
        sb.prepare("file:///nonexistent/repo.git", "main")
    sb.cleanup()


def test_match_glob_and_parse_memory():
    assert match_glob("agent/billing.py", "**/*.py")
    assert match_glob("billing.py", "**/*.py")
    assert match_glob("agent/billing.py", "*.py")
    assert match_glob("agent/x/y.ts", "agent/**")
    assert not match_glob("agent/billing.py", "tests/*.py")
    assert match_glob("anything", "**/*")
    assert parse_memory("4g") == 4 * 1024**3
    assert parse_memory("512m") == 512 * 1024**2
    assert parse_memory("bogus") is None
