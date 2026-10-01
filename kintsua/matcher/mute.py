"""Mute patterns from ``.kintsua.yml``.

::

    mute:
      - stripe:/v1/legacy/*      # a vendor and a path (or operation) glob
      - tool:get_*               # every MCP tool starting with get_
      - "*/beta/*"               # any operation under /beta/ for any vendor
      - slack                    # a bare vendor name mutes the whole vendor

``is_muted(patterns, vendor, operation)`` applies ``fnmatch`` semantics (``*`` also crosses
``/``) to ``"<vendor>:<operation>"`` and to the bare operation, in every spelling the
operation has: as recorded (``POST /v1/legacy/foo``), canonical (``tool:get_weather``), the
HTTP path alone (``/v1/legacy/foo``), the bare tool name, and, for SDK symbols, the HTTP
operations they stand for. Matching is case-insensitive so ``POST`` and ``post`` agree.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from kintsua.matcher.operations import parse_operation, sdk_http_operations


def clean_patterns(patterns: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for p in patterns or ():
        if isinstance(p, str) and p.strip():
            out.append(p.strip())
    return out


def mute_candidates(vendor: str | None, operation: str | None) -> list[str]:
    """Every string a mute pattern may be written against for this operation."""
    raw = (operation or "").strip()
    if not raw:
        return []
    op = parse_operation(raw)
    bare: list[str] = [raw]
    canonical = op.canonical()
    if canonical and canonical not in bare:
        bare.append(canonical)
    if op.kind == "http":
        bare.append(op.path)
        if op.method != "*":
            bare.append(f"{op.method} {op.path}")
    elif op.kind in ("tool", "resource", "prompt", "schema", "package", "sdk"):
        bare.append(op.name)
        if op.kind == "sdk":
            for h in sdk_http_operations(op.name):
                bare.append(h.canonical())
                bare.append(h.path)
    out: list[str] = []
    seen: set[str] = set()
    for c in bare:
        for cand in ((f"{vendor}:{c}",) if vendor else ()) + (c,):
            if cand not in seen:
                seen.add(cand)
                out.append(cand)
    if vendor and vendor not in seen:
        out.append(vendor)
    return out


def is_muted(
    patterns: list[str] | tuple[str, ...] | None, vendor: str | None, operation: str | None
) -> bool:
    pats = clean_patterns(patterns)
    if not pats:
        return False
    cands = [c.lower() for c in mute_candidates(vendor, operation)]
    if not cands:
        return False
    for p in pats:
        pl = p.lower()
        for c in cands:
            if fnmatchcase(c, pl):
                return True
    return False


__all__ = ["clean_patterns", "is_muted", "mute_candidates"]
