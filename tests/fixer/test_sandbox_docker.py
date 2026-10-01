"""``DockerSandbox`` against a fake docker SDK client.

The fake implements the slice of the SDK the backend uses (volumes.create,
containers.create, start/wait/logs/remove, exec_run, get_archive/put_archive) and runs
every command on this machine with ``/work`` mapped to a temp directory, so the real
code path (networked clone helper, no-network main container, tar-based file access,
``timeout``-wrapped execs, cleanup) is exercised without a daemon.
"""

from __future__ import annotations

import io
import os
import re
import socket
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from kintsua.fixer.sandbox import DockerSandbox, SandboxError
from tests.fixer.conftest import clone_url_for


class FakeVolume:
    def __init__(self, name: str, labels: dict[str, str]):
        self.name = name
        self.labels = labels
        self.removed = False

    def remove(self, force: bool = False) -> None:
        self.removed = True


class FakeContainer:
    def __init__(self, client: FakeDockerClient, image: str, command: Any, kwargs: dict[str, Any]):
        self.client = client
        self.image = image
        self.command = command
        self.kwargs = kwargs
        self.code: int | None = None
        self.out = b""
        self.err = b""
        self.removed = False
        self.started = False

    # ---- host mapping -----------------------------------------------------------------
    def _map(self, s: str) -> str:
        return s.replace("/work", str(self.client.work))

    def _env(self, env: dict[str, str] | None) -> dict[str, str]:
        merged = dict(os.environ)
        merged.update({k: str(v) for k, v in (env or {}).items()})
        merged["HOME"] = str(self.client.home)
        return merged

    def _run(self, argv: list[str], workdir: str | None, env: dict[str, str] | None):
        argv = [self._map(a) for a in argv]
        cwd = self._map(workdir) if workdir else str(self.client.work)
        Path(cwd).mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(argv, cwd=cwd, env=self._env(env), capture_output=True, check=False)
        return proc.returncode, proc.stdout, proc.stderr

    # ---- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        self.started = True
        if self.command == ["sleep", "infinity"]:
            return  # the long-lived main container
        self.code, self.out, self.err = self._run(
            list(self.command), self.kwargs.get("working_dir"), self.kwargs.get("environment")
        )

    def wait(self, timeout: float | None = None) -> dict[str, int]:
        return {"StatusCode": int(self.code or 0)}

    def logs(self, stdout: bool = True, stderr: bool = True) -> bytes:
        return (self.out if stdout else b"") + (self.err if stderr else b"")

    def kill(self) -> None:
        pass

    def remove(self, force: bool = False) -> None:
        self.removed = True

    # ---- exec -------------------------------------------------------------------------
    def exec_run(self, argv, workdir=None, environment=None, demux=False, user=None):
        self.client.execs.append({"argv": list(argv), "workdir": workdir, "user": user})
        code, out, err = self._run(list(argv), workdir, environment)
        return (code, (out, err)) if demux else (code, out + err)

    # ---- files ------------------------------------------------------------------------
    def get_archive(self, path: str):
        host = Path(self._map(path))
        if not host.is_file():
            raise FileNotFoundError(path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(host, arcname=host.name)
        return iter([buf.getvalue()]), {"name": host.name}

    def put_archive(self, path: str, data: bytes) -> bool:
        host = Path(self._map(path))
        host.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            tar.extractall(host, filter="data")
        return True


class _Volumes:
    def __init__(self, client: FakeDockerClient):
        self.client = client

    def create(self, name: str, labels: dict[str, str] | None = None) -> FakeVolume:
        v = FakeVolume(name, labels or {})
        self.client.volumes_created.append(v)
        return v


class _Containers:
    def __init__(self, client: FakeDockerClient):
        self.client = client

    def create(self, image: str, command: Any = None, **kwargs: Any) -> FakeContainer:
        c = FakeContainer(self.client, image, command, kwargs)
        self.client.containers_created.append(c)
        return c


class FakeDockerClient:
    def __init__(self, root: Path):
        self.work = root / "work"
        self.home = root / "home"
        self.work.mkdir(parents=True)
        self.home.mkdir(parents=True)
        self.volumes_created: list[FakeVolume] = []
        self.containers_created: list[FakeContainer] = []
        self.execs: list[dict[str, Any]] = []
        self.volumes = _Volumes(self)
        self.containers = _Containers(self)


@pytest.fixture
def docker_sandbox(settings, tmp_path: Path, fixture_git_repo: Path, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))],
    )
    monkeypatch.delenv("SANDBOX_HTTPS_PROXY", raising=False)
    client = FakeDockerClient(tmp_path)
    sb = DockerSandbox(settings=settings, client=client, job_id="job1", wall_clock_s=600)
    sb.prepare(clone_url_for(fixture_git_repo), "main")
    yield sb, client
    sb.cleanup()


def test_prepare_uses_a_networked_helper_then_a_no_network_container(docker_sandbox):
    sb, client = docker_sandbox
    assert client.volumes_created[0].name == "kintsua-fix-job1"
    helpers = [c for c in client.containers_created if c.command != ["sleep", "infinity"]]
    main = [c for c in client.containers_created if c.command == ["sleep", "infinity"]]
    assert helpers and main and len(main) == 1
    clone = next(
        c for c in helpers if "git" in " ".join(c.command) and "clone" in " ".join(c.command)
    )
    assert clone.kwargs["network_mode"] == "bridge"
    assert clone.kwargs["dns"] == ["127.0.0.1"]
    assert clone.kwargs["extra_hosts"] == {
        h: "203.0.113.10" for h in sb.settings.sandbox_egress_allowlist
    }
    assert "pypi.org" in clone.kwargs["environment"]["KINTSUA_EGRESS_ALLOWLIST"]
    assert clone.removed  # helpers are one-shot
    m = main[0]
    assert m.kwargs["network_mode"] == "none" and m.kwargs["working_dir"] == "/work/repo"
    assert m.kwargs["mem_limit"] == sb.settings.sandbox_memory
    assert m.kwargs["nano_cpus"] == int(sb.settings.sandbox_cpus * 1e9)
    assert m.kwargs["user"] == "sandbox" and m.kwargs["cap_drop"] == ["ALL"]
    assert m.kwargs["labels"]["kintsua.job"] == "job1"
    assert sb.workdir == "/work/repo" and re.fullmatch(r"[0-9a-f]{40}", sb.base_sha or "")
    assert (client.work / "repo" / "agent" / "billing.py").is_file()


def test_run_read_write_list_diff_and_timeouts(docker_sandbox):
    sb, client = docker_sandbox
    res = sb.run(["sh", "-c", "echo hi; echo err >&2; exit 2"], timeout_s=10)
    assert res.exit_code == 2 and res.stdout.strip() == "hi" and res.stderr.strip() == "err"
    last = client.execs[-1]["argv"]
    assert last[:3] == ["timeout", "-k", "5"] and last[4:6] == ["sh", "-c"]
    assert client.execs[-1]["user"] == "sandbox"
    res = sb.run(["cat"], timeout_s=10, input="stdin works\n")
    assert res.stdout == "stdin works\n"
    res = sb.run(["sleep", "5"], timeout_s=1)
    assert res.timed_out and res.exit_code == 124

    assert "stripe.Charge.create" in sb.read_file("agent/billing.py")
    assert sb.file_exists("agent/billing.py") and not sb.file_exists("nope.py")
    with pytest.raises(SandboxError):
        sb.read_file("../escape.py")
    with pytest.raises(SandboxError):
        sb.read_file("missing.py")
    files = sb.list_files("**/*.py")
    assert "agent/billing.py" in files and "tests/test_weather.py" in files
    assert not any(f.startswith(".git") for f in sb.list_files())

    ok, msg = sb.apply_patch(
        {
            "path": "agent/billing.py",
            "old": "        source=source,\n",
            "new": "        payment_method=source,\n",
        }
    )
    assert ok, msg
    sb.write_file("agent/extra.py", "X = 1\n")
    assert sb.changed_files() == ["agent/billing.py", "agent/extra.py"]
    diff = sb.diff()
    assert "+        payment_method=source," in diff and "new file mode" in diff
    ok, msg = sb.patch_applies_cleanly(diff)
    assert ok, msg


def test_networked_run_and_cleanup(docker_sandbox):
    sb, client = docker_sandbox
    res = sb.run("echo $KINTSUA_EGRESS_ALLOWLIST", timeout_s=10, network=True)
    assert "registry.npmjs.org" in res.stdout
    helper = client.containers_created[-1]
    assert helper.kwargs["network_mode"] == "bridge" and helper.removed
    main = next(c for c in client.containers_created if c.command == ["sleep", "infinity"])
    sb.cleanup()
    assert main.removed and client.volumes_created[0].removed
    sb.cleanup()  # idempotent


def test_proxy_replaces_the_allowlist(settings, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SANDBOX_HTTPS_PROXY", "http://egress.internal:3128")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 3128))],
    )
    sb = DockerSandbox(settings=settings, client=FakeDockerClient(tmp_path), job_id="p")
    assert sb.egress_hosts() == {"egress.internal": "10.0.0.5"}
    env = sb._network_env()
    assert (
        env["HTTPS_PROXY"] == "http://egress.internal:3128"
        and env["HTTP_PROXY"] == env["HTTPS_PROXY"]
    )
