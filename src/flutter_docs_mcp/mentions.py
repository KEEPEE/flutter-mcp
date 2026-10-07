"""Parsing of ``@flutter_mcp <identifier>`` mentions.

Pure text handling — no network, no shared state, nothing raises. Split out
from the server so the grammar is unit-testable on its own and so the server's
resolution loop has nothing to carry between iterations.

Supported mention forms (the same grammar the legacy
``process_flutter_mentions`` documented, with its quirks fixed)::

    @flutter_mcp provider                 pub.dev package, latest
    @flutter_mcp provider:^6.0.0          caret constraint
    @flutter_mcp provider:6.1.5           exact release
    @flutter_mcp dio:>=5.0.0 <6.0.0       range written with a space
    @flutter_mcp provider:latest          explicit "latest"
    @flutter_mcp pub:dio                  explicit pub.dev package
    @flutter_mcp pub:dio:5.4.0            explicit pub.dev package + release
    @flutter_mcp material.AppBar          Flutter class, library given
    @flutter_mcp dart:async.Future        Dart SDK class
    @flutter_mcp Container                plain name (resolved by the server)

A mention ends at whitespace, at the next ``@flutter_mcp``, or at trailing
sentence punctuation — except that a version constraint may contain a space
(``>=5.0.0 <6.0.0``), which is absorbed only when a constraint was already
started by a colon, so prose after a mention is never swallowed.
"""

from __future__ import annotations

import re

__all__ = ["parse_mentions", "MENTION_RE"]

#: Where a mention starts. ``\b`` keeps ``@flutter_mcpx`` from matching.
MENTION_RE = re.compile(r"@flutter_mcp\b", re.IGNORECASE)

#: A whitespace-separated token that continues a space-separated constraint,
#: e.g. the ``<6.0.0`` in ``@flutter_mcp dio:>=5.0.0 <6.0.0``.
_CONST_CONTINUATION_RE = re.compile(r"^(?:>=|<=|~>|>=|>|<|=|\^|~)?\d[0-9A-Za-z.+-]*$")

#: A comparator written as its own token (``dio:>=5.0.0 < 6.0.0``).
_BARE_COMPARATOR_RE = re.compile(r"^(?:>=|<=|~>|>=|>|<|=|\^|~)$")

#: Sentence punctuation that is not part of a mention. ``:`` is included
#: because a mention may end with a colon that introduces prose
#: ("@flutter_mcp provider: see below").
_TRAILING_PUNCTUATION = ".,;:!?)]}\"'"

#: Recognised "give me whatever is newest" keywords.
_LATEST_KEYWORDS = frozenset({"latest", "any", "*"})


def _split_identifier_constraint(raw: str) -> tuple[str, str | None, bool, bool]:
    """Split a raw mention token into ``(identifier, constraint, explicit_pub, version_requested)``.

    ``pub:`` is a package marker; ``dart:`` is part of a Dart library name and
    therefore never a constraint separator.

    ``version_requested`` is True whenever the mention carried a version tail at
    all — including the keywords ``latest`` / ``any`` / ``*``, which leave
    ``constraint`` as ``None`` but still mean "this is a pub.dev package, and
    pub.dev's newest release is what was asked for". Without it,
    ``@flutter_mcp provider:latest`` would be classified as a class name and
    only reach pub.dev by accident, after two pointless 404s.
    """
    explicit_pub = raw.startswith("pub:")
    rest = raw[4:] if explicit_pub else raw

    if rest.startswith("dart:"):
        return rest, None, explicit_pub, False

    head, separator, tail = rest.partition(":")
    if separator and tail.strip():
        return head.strip(), tail.strip(), explicit_pub, True
    return rest.strip(), None, explicit_pub, False


def parse_mentions(text: str) -> list[dict]:
    """Extract every ``@flutter_mcp`` mention from ``text``, in document order.

    Returns one record per mention::

        {"raw": "provider:^6.0.0",       # the mention token as written
         "mention": "@flutter_mcp provider:^6.0.0",  # as it appeared
         "identifier": "provider",       # package / class identifier
         "constraint": "^6.0.0",         # version constraint or None
         "explicit_pub": False,          # "pub:" prefix was used
         "version_requested": True,      # a version tail was present at all
         "start": 12}                    # offset of "@flutter_mcp" in text

    A bare ``@flutter_mcp`` with nothing after it produces no record (there is
    nothing to resolve). Records are built independently — the function keeps no
    state between mentions, which is what makes a stale carry-over entry
    impossible.
    """
    if not isinstance(text, str) or not text:
        return []

    matches = list(MENTION_RE.finditer(text))
    records: list[dict] = []
    for position, match in enumerate(matches):
        # A mention never reaches past the next mention.
        slice_end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        tokens = text[match.end():slice_end].split()
        if not tokens:
            continue

        raw = tokens[0]
        index = 1
        # Absorb a space-separated constraint tail, but only once a constraint
        # has actually been started with a colon.
        while index < len(tokens) and ":" in raw:
            candidate = tokens[index]
            if _CONST_CONTINUATION_RE.match(candidate) or _BARE_COMPARATOR_RE.match(candidate):
                raw = f"{raw} {candidate}"
                index += 1
            else:
                break

        raw = raw.rstrip(_TRAILING_PUNCTUATION)
        if not raw:
            continue

        identifier, constraint, explicit_pub, version_requested = _split_identifier_constraint(raw)
        if not identifier:
            continue
        if constraint is not None and constraint.lower() in _LATEST_KEYWORDS:
            constraint = None  # "latest" == no constraint: pub.dev's newest

        records.append({
            "raw": raw,
            "mention": f"@flutter_mcp {raw}",
            "identifier": identifier,
            "constraint": constraint,
            "explicit_pub": explicit_pub,
            "version_requested": version_requested,
            "start": match.start(),
        })
    return records
