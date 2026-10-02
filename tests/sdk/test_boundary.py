"""The SDK's published surface stays small: under 500 lines counting every line of the four
files, exporting the documented names, and importing only stdlib + httpx."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SDK = Path(__file__).resolve().parents[2] / "kintsua" / "sdk"
FILES = ["__init__.py", "wrap.py", "report.py", "hooks.py"]


def test_sdk_is_under_500_lines_counting_every_line():
    assert {p.name for p in SDK.glob("*.py")} == set(FILES)
    counts = {f: len((SDK / f).read_text("utf-8").splitlines()) for f in FILES}
    assert sum(counts.values()) < 500, counts


def test_sdk_exports():
    import kintsua
    import kintsua.sdk as sdk

    for name in (
        "configure",
        "flush",
        "wrap_mcp",
        "wrap_http",
        "hook_langchain",
        "hook_openai_agents",
        "hook_google_adk",
        "Reporter",
    ):
        assert callable(getattr(sdk, name))
    assert kintsua.wrap_mcp is sdk.wrap_mcp and kintsua.configure is sdk.configure


def test_sdk_imports_only_stdlib_and_httpx():
    code = (
        "import sys, kintsua.sdk\n"
        # httpx itself imports rich/click for its optional CLI; they are not SDK imports.
        "heavy = {'pydantic', 'mcp', 'sqlalchemy', 'fastapi', 'yaml', 'jsonschema', 'typer'}\n"
        "names = sorted(m for m in sys.modules if m.split('.')[0] in heavy"
        " or m.startswith(('kintsua.core', 'kintsua.watchers', 'kintsua.classify', 'kintsua.cli')))\n"
        "print(names)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(SDK.parents[1]),
    )
    assert out.stdout.strip() == "[]", out.stdout
