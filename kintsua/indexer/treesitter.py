"""tree-sitter 0.26 helpers shared by the Python and TypeScript extractors.

``Language(ptr)`` / ``Parser(language)`` is the 0.22+ API; the grammars come from the
``tree_sitter_python`` and ``tree_sitter_typescript`` wheels. Parsing never executes repo
code, which is why the indexer can run in-process.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Literal

from tree_sitter import Language, Node, Parser, Tree

log = logging.getLogger(__name__)

LanguageName = Literal["python", "typescript", "tsx"]

# Extensions -> grammar. ``.js`` / ``.mjs`` / ``.cjs`` parse fine with the TypeScript grammar
# (it is a superset for the constructs we look at); ``.jsx`` needs the TSX grammar.
EXTENSION_LANGUAGE: dict[str, LanguageName] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "typescript",
    ".mjs": "typescript",
    ".cjs": "typescript",
    ".tsx": "tsx",
    ".jsx": "tsx",
}


@lru_cache(maxsize=4)
def get_language(name: LanguageName) -> Language:
    if name == "python":
        import tree_sitter_python

        return Language(tree_sitter_python.language())
    if name == "typescript":
        import tree_sitter_typescript

        return Language(tree_sitter_typescript.language_typescript())
    if name == "tsx":
        import tree_sitter_typescript

        return Language(tree_sitter_typescript.language_tsx())
    raise ValueError(f"unknown grammar {name!r}")


def get_parser(name: LanguageName) -> Parser:
    """A fresh parser (parsers are cheap and not thread-safe, so no caching)."""
    return Parser(get_language(name))


def parse(name: LanguageName, source: bytes) -> Tree:
    return get_parser(name).parse(source)


def parse_source(name: LanguageName, source: str | bytes) -> tuple[Tree, bytes]:
    raw = source.encode("utf-8") if isinstance(source, str) else source
    return parse(name, raw), raw


def language_for_path(path: str) -> LanguageName | None:
    lower = path.lower()
    for ext, lang in EXTENSION_LANGUAGE.items():
        if lower.endswith(ext):
            return lang
    return None


# --------------------------------------------------------------------------------------
# Node helpers
# --------------------------------------------------------------------------------------


def node_text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def line_of(node: Node) -> int:
    """1-based line number of the node's first byte."""
    return node.start_point[0] + 1


def walk(node: Node) -> Iterator[Node]:
    """Pre-order traversal without recursion (repo files can nest deeply)."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def iter_type(node: Node, *types: str) -> Iterator[Node]:
    wanted = set(types)
    for n in walk(node):
        if n.type in wanted:
            yield n


def child(node: Node, field: str) -> Node | None:
    return node.child_by_field_name(field)


def named_children(node: Node) -> list[Node]:
    return [c for c in node.children if c.is_named]


def has_errors(tree: Tree) -> bool:
    return tree.root_node.has_error


def error_count(tree: Tree) -> int:
    return sum(1 for n in walk(tree.root_node) if n.type == "ERROR" or n.is_missing)


def ancestors(node: Node) -> Iterator[Node]:
    p = node.parent
    while p is not None:
        yield p
        p = p.parent


def enclosing(node: Node, *types: str) -> Node | None:
    wanted = set(types)
    for a in ancestors(node):
        if a.type in wanted:
            return a
    return None


# --------------------------------------------------------------------------------------
# Repository walking (shared by the extractors)
# --------------------------------------------------------------------------------------

MAX_FILE_BYTES = 1_000_000

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".env",
        "env",
        "node_modules",
        "site-packages",
        "dist-packages",
        "vendor",
        "vendored",
        "_vendor",
        "third_party",
        "dist",
        "build",
        "__pycache__",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        ".next",
        ".nuxt",
        ".turbo",
        "coverage",
        ".cache",
    }
)

_TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "specs", "testing", "e2e"})
_TEST_FILE_RE = re.compile(
    r"(^test_.*\.py$)|(.*_test\.py$)|(^conftest\.py$)|(.*\.(test|spec)\.[cm]?[jt]sx?$)"
)


def is_test_path(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in _TEST_DIRS for p in parts[:-1]):
        return True
    return bool(_TEST_FILE_RE.match(parts[-1]))


def iter_repo_files(
    root: str | Path,
    suffixes: tuple[str, ...] | None = None,
    *,
    max_bytes: int = MAX_FILE_BYTES,
    names: tuple[str, ...] | None = None,
    skip_dirs: frozenset[str] = SKIP_DIRS,
) -> Iterator[tuple[Path, str, bool]]:
    """Yield ``(absolute_path, repo_relative_posix_path, too_large)`` for every regular file
    under ``root`` whose name ends with one of ``suffixes`` (or equals one of ``names``),
    skipping vendored / virtualenv directories and symlinks. Deterministic order."""
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in skip_dirs and not os.path.islink(os.path.join(dirpath, d))
        )
        for fn in sorted(filenames):
            lower = fn.lower()
            if suffixes and not lower.endswith(suffixes) and not (names and fn in names):
                continue
            if not suffixes and names and fn not in names:
                continue
            p = Path(dirpath) / fn
            if p.is_symlink() or not p.is_file():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            yield p, rel, size > max_bytes


def read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None  # binary
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")
