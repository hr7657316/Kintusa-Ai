"""``.kintsua.yml`` parsing."""

from __future__ import annotations

import pytest

from kintsua.indexer.config import (
    config_sources,
    effective_fix_mode,
    fix_base_branch,
    mute_patterns,
    parse_kintsua_yml,
)
from kintsua.indexer.config import test_command as config_test_command

SPEC_EXAMPLE = """
sources:
  - kind: openapi
    url: https://api.vendor.com/openapi.json
  - kind: changelog
    url: https://vendor.com/changelog
mute:
  - stripe:/v1/legacy/*
fix:
  mode: pr          # pr | issue_only
  base: main
"""


def test_spec_example_round_trips():
    cfg = parse_kintsua_yml(SPEC_EXAMPLE)
    assert cfg == {
        "sources": [
            {"kind": "openapi", "url": "https://api.vendor.com/openapi.json"},
            {"kind": "changelog", "url": "https://vendor.com/changelog"},
        ],
        "mute": ["stripe:/v1/legacy/*"],
        "fix": {"mode": "pr", "base": "main"},
    }


def test_empty_and_comment_only_files_give_defaults():
    assert parse_kintsua_yml("") == {"sources": [], "mute": [], "fix": {}}
    assert parse_kintsua_yml(None) == {"sources": [], "mute": [], "fix": {}}
    assert parse_kintsua_yml("# nothing here\n") == {"sources": [], "mute": [], "fix": {}}


def test_unknown_keys_are_ignored():
    cfg = parse_kintsua_yml("foo: bar\nfix:\n  mode: issue_only\n  colour: blue\n")
    assert cfg["fix"] == {"mode": "issue_only"}
    assert "foo" not in cfg


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("fix:\n  mode: maybe\n", "fix.mode"),
        ("sources:\n  - kind: graphql\n    url: https://x\n", "sources.0.kind"),
        ("sources:\n  - kind: openapi\n", "needs a url"),
        ("sources:\n  - kind: openapi\n    url: ftp://x\n", "http(s) URL"),
        ("mute: 5\n", "mute must be a list"),
        ("mute:\n  - 7\n", "non-empty strings"),
        ("fix: pr\n", "fix must be a mapping"),
        ("fix:\n  base: 'two words'\n", "fix.base"),
        ("fix:\n  radius: 99\n", "fix.radius"),
        ("- a\n- b\n", "must be a mapping"),
        ("sources: [\n", "not valid YAML"),
    ],
)
def test_bad_values_raise_readable_errors(text, fragment):
    with pytest.raises(ValueError) as e:
        parse_kintsua_yml(text)
    assert fragment in str(e.value)


def test_extra_optional_keys():
    cfg = parse_kintsua_yml(
        "test_command: pytest -q\nfix:\n  radius: 2\nsources:\n"
        "  - kind: openapi\n    url: https://specs.example/v1.json\n    hosts: [api.example.com]\n"
        "  - kind: mcp\n    command: npx -y @acme/mcp\n"
    )
    assert cfg["test_command"] == "pytest -q"
    assert cfg["fix"] == {"radius": 2}
    assert cfg["sources"][0]["hosts"] == ["api.example.com"]
    assert cfg["sources"][1]["kind"] == "mcp"
    assert config_test_command(cfg) == "pytest -q"
    srcs = config_sources(cfg)
    assert srcs[0].locator == "https://specs.example/v1.json"
    assert srcs[1].locator == "npx -y @acme/mcp"


def test_effective_fix_mode_precedence():
    assert effective_fix_mode({"fix": {"mode": "pr"}}, {"fix_mode": "issue_only"}) == "pr"
    assert effective_fix_mode({"fix": {}}, {"fix_mode": "pr"}) == "pr"
    assert effective_fix_mode({}, {}) == "issue_only"
    assert effective_fix_mode(None, None) == "issue_only"
    assert effective_fix_mode({"fix": {"mode": "bogus"}}, {"fix_mode": "pr"}) == "pr"


def test_mute_patterns_and_base_branch():
    cfg = parse_kintsua_yml(
        "mute:\n  - stripe:/v1/legacy/*\n  - 'mcp:get_*'\nfix:\n  base: develop\n"
    )
    assert mute_patterns(cfg) == ["stripe:/v1/legacy/*", "mcp:get_*"]
    assert mute_patterns({}) == []
    assert fix_base_branch(cfg, "main") == "develop"
    assert fix_base_branch({}, "main") == "main"
