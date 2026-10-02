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


# a removed SQL comment ``-- legacy column`` is emitted as ``--- legacy column`` and an
# added ``++ x`` as ``+++ x``: hunk body, not file headers
SQL_COMMENT_DIFF = """diff --git a/agent/q.py b/agent/q.py
index 1111111..2222222 100644
--- a/agent/q.py
+++ b/agent/q.py
@@ -1,5 +1,5 @@
 QUERY = \"\"\"
 SELECT id,
--- legacy column
+++ x
 FROM charges
 \"\"\"
diff --git a/agent/other.py b/agent/other.py
index 3333333..4444444 100644
--- a/agent/other.py
+++ b/agent/other.py
@@ -1 +1 @@
-a = 1
+a = 2
"""


def test_hunk_body_lines_that_look_like_file_headers():
    patches = parse_unified_diff(SQL_COMMENT_DIFF)
    assert [p.path for p in patches] == ["agent/q.py", "agent/other.py"]
    q = patches[0]
    assert q.removed_lines == 1 and q.added_lines == 1
    assert q.hunks[0].removed == ["-- legacy column"] and q.hunks[0].added == ["++ x"]
    assert touched_paths(SQL_COMMENT_DIFF) == ["agent/q.py", "agent/other.py"]
    assert changed_line_count(SQL_COMMENT_DIFF) == 4
    assert diff_stat(SQL_COMMENT_DIFF) == {"agent/q.py": (1, 1), "agent/other.py": (1, 1)}


def test_hunk_counts_from_a_real_git_diff(sandbox):
    """End to end through ``git diff``: removing a ``-- `` line from a call-site file."""
    sandbox.write_file("agent/q.py", 'QUERY = """\nSELECT id,\n-- legacy column\nFROM t\n"""\n')
    sandbox.git_ok("add", "agent/q.py")
    sandbox.git_ok("commit", "-q", "-m", "q")
    sandbox.base_sha = sandbox.head_sha()
    sandbox.write_file("agent/q.py", 'QUERY = """\nSELECT id,\n++ x\nFROM t\n"""\n')
    diff = sandbox.diff()
    assert "\n--- legacy column\n" in diff and "\n+++ x\n" in diff
    assert touched_paths(diff) == ["agent/q.py"]
    assert changed_line_count(diff) == 2
