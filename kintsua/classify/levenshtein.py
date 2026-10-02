"""Pure-Python Levenshtein distance.

Kept dependency-free so ``kintsua/classify/rules.py`` (part of the open-source CLI) can
use it without pulling in a C extension.
"""

from __future__ import annotations


def levenshtein(a: str, b: str, *, max_distance: int | None = None) -> int:
    """Edit distance between ``a`` and ``b`` (insert / delete / substitute, cost 1 each).

    With ``max_distance`` the computation stops early once every cell of a row exceeds
    it; the returned value is then *some* number greater than ``max_distance`` (a lower
    bound on the true distance), which is all a threshold comparison needs.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    if max_distance is not None and len(a) - len(b) > max_distance:
        return len(a) - len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(v)
            if v < row_min:
                row_min = v
        if max_distance is not None and row_min > max_distance:
            return row_min
        prev = cur
    return prev[-1]


def names_similar(a: str, b: str, *, max_distance: int = 2) -> bool:
    """True when two *different* identifiers are within ``max_distance`` edits (the
    spec's "Levenshtein distance under 3" rename heuristic)."""
    if a == b or not a or not b:
        return False
    return levenshtein(a, b, max_distance=max_distance) <= max_distance
