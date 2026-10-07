"""pub.dev / Dart style version parsing and constraint matching.

Used by :func:`flutter_docs_mcp.server.flutter_mentions` to answer a
version-constrained mention such as ``@flutter_mcp dio:>=5.0.0 <6.0.0`` with
**the newest published version that actually satisfies the constraint** — never
with a different version than the one that was asked for.

Everything here is pure and offline: no network, no shared state, nothing
raises. pub.dev publishes a superset of strict semver, so the accepted shapes
are the ones the site really serves::

    6.1.5            6.1.5+1          6.1.0-dev.1
    5.11.1           0.0.3            3.0.0-beta.2

Constraint grammar (the Dart/pub.dev flavour)::

    ^6.0.0                 caret (upper bound = next breaking change)
    ~>1.2.3                Dart's "tilde-greater", same meaning as ^
    ~1.2.3                 accepted as an alias of ^
    >=5.0.0 <6.0.0         conjunction, whitespace or comma separated
    >1.0.0 <=2.0.0
    =6.1.5  /  6.1.5      exact release (string-exact against pub.dev)
    latest / any / *      no constraint — the published latest

Semver ordering rules are followed: build metadata (``+1``) never affects
ordering, and a pre-release (``-dev.1``) sorts **below** its release, so a
plain range never resolves to a pre-release unless the constraint itself asks
for one.
"""

from __future__ import annotations

import re

__all__ = [
    "parse_version",
    "compare_versions",
    "sort_key",
    "parse_constraint",
    "satisfies",
    "newest_matching",
    "is_prerelease",
]

#: ``major.minor.patch[-pre][+build]`` — the shape pub.dev publishes.
_VERSION_RE = re.compile(
    r"""^
    (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)
    (?:-(?P<pre>[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?
    (?:\+(?P<build>[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?
    $""",
    re.VERBOSE,
)

#: Constraint term: optional comparator + version. A bare version is ``=``.
_TERM_RE = re.compile(
    r"^(?P<op>>=|<=|~>|>=|>|<|=|\^|~)?\s*(?P<version>.+)$"
)

#: A comparator written as its own token (``">=5.0.0 < 6.0.0"``).
_BARE_COMPARATOR_RE = re.compile(r"^(?:>=|<=|~>|>=|>|<|=|\^|~)$")

#: Constraints that mean "whatever pub.dev publishes".
_ANY_CONSTRAINTS = frozenset({"", "*", "any", "latest"})

#: Comparators that make a constraint a *range* rather than an exact pin.
_RANGE_OPS = frozenset({">=", "<=", ">", "<", "^", "~>", "~"})

#: First element of :func:`sort_key` for a string that is not a version at all.
#: Larger than any plausible major version, so garbage sorts last.
_UNPARSEABLE = 10 ** 18


# ---------------------------------------------------------------------------
# Version parsing / ordering
# ---------------------------------------------------------------------------

def _pre_key(pre: str | None) -> tuple:
    """Sort key for a pre-release part (``None`` = a normal release).

    A normal release sorts *above* every pre-release of the same
    ``major.minor.patch`` (semver §11). Pre-release identifiers compare
    numerically when both are numeric, otherwise lexically (numeric identifiers
    sort lower than alphanumeric ones); fewer identifiers sorts first when every
    shared identifier is equal.

    The first element is a plain int marker (``1`` for a normal release, ``0``
    for a pre-release) so the two shapes stay comparable; every identifier is a
    uniform 3-tuple so no comparison ever meets an int and a tuple.
    """
    if pre is None:
        return (1,)  # normal release: above any pre-release key (0, …)
    parts: list = [0]
    for ident in pre.split("."):
        if ident.isdigit():
            parts.append((0, 0, int(ident)))
        else:
            parts.append((0, 1, ident))
    return tuple(parts)


def parse_version(text: str) -> dict | None:
    """Parse ``major.minor.patch[-pre][+build]`` into a comparable dict.

    Returns ``{"major", "minor", "patch", "pre", "build", "raw"}`` or ``None``
    when ``text`` is not a version. Never raises.
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    match = _VERSION_RE.match(raw)
    if match is None:
        return None
    return {
        "major": int(match.group("major")),
        "minor": int(match.group("minor")),
        "patch": int(match.group("patch")),
        "pre": match.group("pre"),
        "build": match.group("build"),
        "raw": raw,
    }


def is_prerelease(text: str) -> bool:
    """True when ``text`` is a version with a pre-release part."""
    parsed = parse_version(text)
    return bool(parsed and parsed["pre"])


def sort_key(text: str) -> tuple:
    """Total order over version strings; unparseable versions sort last.

    The fallback key is deliberately larger than any real ``major`` so a
    malformed string can never outrank a real release (and never raises by
    meeting a tuple where an int is expected).
    """
    parsed = parse_version(text)
    if parsed is None:
        return (_UNPARSEABLE, 0, 0, (1,))
    return (
        parsed["major"],
        parsed["minor"],
        parsed["patch"],
        _pre_key(parsed["pre"]),
    )


def compare_versions(a: str, b: str) -> int:
    """``-1`` / ``0`` / ``1`` for ``a`` vs ``b`` (build metadata ignored)."""
    ka, kb = sort_key(a), sort_key(b)
    if ka == kb:
        return 0
    return -1 if ka < kb else 1


# ---------------------------------------------------------------------------
# Constraint parsing
# ---------------------------------------------------------------------------

def _caret_upper(parsed: dict) -> tuple[int, int, int]:
    """Upper bound of ``^major.minor.patch`` (next change of the highest non-zero part)."""
    if parsed["major"] > 0:
        return (parsed["major"] + 1, 0, 0)
    if parsed["minor"] > 0:
        return (0, parsed["minor"] + 1, 0)
    return (0, 0, parsed["patch"] + 1)


def _split_terms(raw: str) -> list[str]:
    """Split a constraint into term chunks, re-joining a separated comparator.

    ``">=5.0.0 <6.0.0"`` and ``">=5.0.0 < 6.0.0"`` are the same constraint; the
    second splits into ``">="`` … no, into ``">=5.0.0"``, ``"<"``, ``"6.0.0"``,
    so a comparator written as its own token is glued to the version that
    follows it. A trailing comparator with no version stays as-is and makes the
    whole constraint unparseable (reported, not guessed).
    """
    pieces = [chunk for chunk in re.split(r"[\s,]+", raw) if chunk]
    terms: list[str] = []
    index = 0
    while index < len(pieces):
        chunk = pieces[index]
        if _BARE_COMPARATOR_RE.fullmatch(chunk) and index + 1 < len(pieces):
            terms.append(f"{chunk}{pieces[index + 1]}")
            index += 2
            continue
        terms.append(chunk)
        index += 1
    return terms


def parse_constraint(text: str) -> dict | None:
    """Parse a constraint into ``{"any", "exact", "terms", "has_prerelease", "raw"}``.

    ``terms`` is a list of ``{"op", "version", "parsed"}`` (plus synthetic
    ``<upper`` bounds for caret terms). Returns ``None`` when the text is not a
    constraint at all — the caller then reports it as unsupported instead of
    guessing. ``"latest"`` / ``"any"`` / ``"*"`` give ``{"any": True}``.
    Never raises.
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if raw.lower() in _ANY_CONSTRAINTS:
        return {"any": True, "exact": False, "terms": [], "has_prerelease": False, "raw": raw}

    terms: list[dict] = []
    for chunk in _split_terms(raw):
        match = _TERM_RE.match(chunk)
        if match is None:
            return None
        op = match.group("op") or "="
        version_text = match.group("version").strip()
        parsed = parse_version(version_text)
        if parsed is None:
            return None
        terms.append({"op": op, "version": version_text, "parsed": parsed})
        if op in ("^", "~>", "~"):
            upper = _caret_upper(parsed)
            terms.append(
                {
                    "op": "<",
                    "version": ".".join(str(n) for n in upper),
                    "parsed": {"major": upper[0], "minor": upper[1], "patch": upper[2], "pre": None, "build": None, "raw": None},
                }
            )

    if not terms:
        return None

    exact = len(terms) == 1 and terms[0]["op"] == "="
    has_pre = any(t["parsed"]["pre"] for t in terms)
    return {
        "any": False,
        "exact": exact,
        "terms": terms,
        "has_prerelease": has_pre,
        "raw": raw,
    }


def _cmp_parsed(a: dict, b: dict) -> int:
    ka = (a["major"], a["minor"], a["patch"], _pre_key(a["pre"]))
    kb = (b["major"], b["minor"], b["patch"], _pre_key(b["pre"]))
    if ka == kb:
        return 0
    return -1 if ka < kb else 1


def _satisfies_term(candidate: dict, term: dict) -> bool:
    op = term["op"]
    ref = term["parsed"]
    order = _cmp_parsed(candidate, ref)
    if op == "=":
        # Exact pins are string-exact against the published version whenever
        # both sides are parseable, so ``6.1.5`` never silently matches the
        # different release ``6.1.5+1``.
        if candidate.get("raw") and ref.get("raw"):
            return candidate["raw"] == ref["raw"]
        return order == 0
    if op in (">=", "^", "~>", "~"):
        # A caret/tilde term is its own lower bound; parse_constraint added the
        # matching ``<upper`` term next to it.
        return order >= 0
    if op == ">":
        return order > 0
    if op == "<=":
        return order <= 0
    if op == "<":
        return order < 0
    return False


def satisfies(version: str, constraint: dict) -> bool:
    """True when ``version`` satisfies an already-parsed ``constraint``.

    Pre-releases are excluded unless the constraint itself mentions one, which
    is what keeps ``>=5.0.0 <6.0.0`` from resolving to ``5.9.0-dev.1``.
    """
    if not isinstance(constraint, dict):
        return False
    if constraint.get("any"):
        return True
    parsed = parse_version(version)
    if parsed is None:
        return False
    if parsed["pre"] and not constraint.get("has_prerelease"):
        return False
    return all(_satisfies_term(parsed, term) for term in constraint.get("terms", []))


def newest_matching(versions: list[str], constraint: dict) -> str | None:
    """Newest version from ``versions`` satisfying ``constraint`` (``None`` if none).

    Ties in semver precedence — ``6.1.5`` and ``6.1.5+1`` differ only in build
    metadata, which precedence ignores — are broken by publication order: pub.dev
    lists releases in the order they were published, so the later-listed one
    wins. That is what makes ``^6.0.0`` on ``provider`` answer ``6.1.5+1``, the
    release pub.dev itself calls latest.
    """
    if not isinstance(versions, list) or not isinstance(constraint, dict):
        return None
    matching = [
        (index, version)
        for index, version in enumerate(versions)
        if isinstance(version, str) and satisfies(version, constraint)
    ]
    if not matching:
        return None
    return max(matching, key=lambda item: (sort_key(item[1]), item[0]))[1]
