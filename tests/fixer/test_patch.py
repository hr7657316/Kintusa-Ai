from __future__ import annotations

import pytest

from kintsua.fixer.patch import (
    ReplacementError,
    added_lines,
    apply_replacement,
    changed_line_count,
    diff_stat,
    is_secret_path,
    looks_binary,
    normalize_repo_path,
    parse_unified_diff,
    secret_line_hits,
    touched_paths,
)

DIFF = """diff --git a/agent/billing.py b/agent/billing.py
index 1111111..2222222 100644
--- a/agent/billing.py
+++ b/agent/billing.py
@@ -12,7 +12,7 @@ def charge_customer(amount: float, currency: str, source: str) -> dict[str, str]:
     charge = stripe.Charge.create(
         amount=to_minor_units(amount),
         currency=currency,
-        source=source,
+        payment_method=source,
         description="Kintsua fixture charge",
     )
     return {"id": charge["id"], "status": charge["status"]}
diff --git a/agent/new.py b/agent/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/agent/new.py
@@ -0,0 +1,2 @@
+X = 1
+API_KEY = "abc"
"""


def test_parse_unified_diff_files_hunks_and_counts():
    patches = parse_unified_diff(DIFF)
    assert [p.path for p in patches] == ["agent/billing.py", "agent/new.py"]
    billing, new = patches
    assert billing.hunks[0].new_start == 12 and billing.hunks[0].new_count == 7
    assert billing.added_lines == 1 and billing.removed_lines == 1
    assert billing.touches_line(15) and not billing.touches_line(40)
    assert new.is_new and new.added_lines == 2
    assert changed_line_count(DIFF) == 4
    assert touched_paths(DIFF) == ["agent/billing.py", "agent/new.py"]
    assert diff_stat(DIFF) == {"agent/billing.py": (1, 1), "agent/new.py": (2, 0)}
    assert ("agent/billing.py", "        payment_method=source,") in added_lines(DIFF)


def test_parse_plain_unified_diff_without_git_headers():
    text = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    (p,) = parse_unified_diff(text)
    assert p.path == "x.py" and p.changed_lines == 2


def test_apply_replacement_exact_unique():
    text = "a\nb\nc\n"
    assert apply_replacement(text, "b\n", "B\n") == "a\nB\nc\n"


def test_apply_replacement_rejects_missing_and_ambiguous():
    with pytest.raises(ReplacementError, match="not found"):
        apply_replacement("a\nb\n", "zzz", "y")
    with pytest.raises(ReplacementError, match="occurs 2 times"):
        apply_replacement("x\nx\n", "x", "y")
    with pytest.raises(ReplacementError, match="empty"):
        apply_replacement("x", "", "y")


def test_apply_replacement_tolerates_trailing_whitespace_once():
    text = "def f():\n    return 1   \n"
    out = apply_replacement(text, "    return 1\n", "    return 2\n")
    assert out == "def f():\n    return 2\n"


@pytest.mark.parametrize(
    "path, secret",
    [
        (".env", True),
        ("config/.env.production", True),
        ("app/secrets.py", True),
        ("lib/credentials.json", True),
        ("agent/auth.py", True),
        ("src/auth_client.ts", True),
        ("agent/token_store.py", True),
        ("secrets/x.txt", True),
        ("deploy/id_rsa", True),
        ("agent/billing.py", False),
        ("tests/test_billing.py", False),
        ("README.md", False),
    ],
)
def test_is_secret_path(path, secret):
    assert is_secret_path(path) is secret


def test_secret_line_hits():
    assert secret_line_hits('api_key = "abc"')
    assert secret_line_hits("headers['Authorization'] = token")
    assert secret_line_hits("access_token: foo")
    assert secret_line_hits('client_secret="x"')
    assert not secret_line_hits("payment_method=source,")
    assert not secret_line_hits('units="metric"')


def test_normalize_repo_path():
    assert normalize_repo_path("agent/./billing.py") == "agent/billing.py"
    assert normalize_repo_path("a/../b.py") == "b.py"
    assert normalize_repo_path("../etc/passwd") is None
    assert normalize_repo_path("/etc/passwd") is None
    assert normalize_repo_path("") is None


def test_looks_binary():
    assert looks_binary(b"\x00\x01\x02")
    assert not looks_binary(b"print('hi')\n")
