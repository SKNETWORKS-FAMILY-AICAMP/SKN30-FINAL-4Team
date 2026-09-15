"""Pure lexical policy for source-visible per-unit support-scale scopes.

This module deliberately knows nothing about profiles, projections, or
CandidatePack identifiers.  It recognizes only a closed set of Korean
``<unit>당/별`` surface forms already present in source text.  Unknown and
conflicting units fail closed rather than being guessed from context.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_PER_UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "COMPANY": (
        "참여기업",
        "기업",
        "업체",
        "회사",
        "개사",
        "개社",
        "社",
    ),
    "TEAM": ("개팀", "팀"),
    "PERSON": ("개인", "사람", "인", "명"),
    "PROJECT": ("프로젝트", "개과제", "과제"),
}
_PER_UNIT_MARKERS = ("당", "별")


def _compact(text: str) -> str:
    """Remove presentation whitespace without rewriting source characters."""

    return "".join(text.split())


@dataclass(frozen=True, slots=True)
class PerUnitScopeOccurrence:
    """One source-visible unit marker and its exact character coordinates."""

    scope: str
    start: int
    end: int
    text: str


def per_unit_scope_occurrences(text: str) -> tuple[PerUnitScopeOccurrence, ...]:
    """Return every closed-vocabulary marker in source order."""

    found: dict[tuple[int, int, str], PerUnitScopeOccurrence] = {}
    for scope, aliases in _PER_UNIT_ALIASES.items():
        for alias in aliases:
            alias_pattern = r"\s*".join(re.escape(char) for char in alias)
            for marker in _PER_UNIT_MARKERS:
                pattern = re.compile(
                    rf"(?<![0-9A-Za-z가-힣社])(?:[0-9]+\s*)?"
                    rf"{alias_pattern}\s*{marker}"
                )
                for match in pattern.finditer(text):
                    key = (match.start(), match.end(), scope)
                    found[key] = PerUnitScopeOccurrence(
                        scope=scope,
                        start=match.start(),
                        end=match.end(),
                        text=match.group(0),
                    )
    return tuple(
        found[key]
        for key in sorted(found, key=lambda row: (row[0], row[1], row[2]))
    )


def explicit_per_unit_scope(
    text: str,
    *,
    require_full_text: bool = False,
) -> str | None:
    """Return exactly one explicit per-unit scope, otherwise ``None``.

    Whitespace is presentation-only, so ``과제 당`` and ``과제당`` are the
    same literal marker.  Aliases are data-driven and closed; wording such as
    ``센터별`` is not promoted.  If more than one canonical scope occurs,
    the result is ambiguous and therefore ``None``.

    ``require_full_text`` is for callers that have already isolated a label
    slot.  It prevents arbitrary prose surrounding a known marker from being
    accepted as that label while still allowing a visible numeric prefix such
    as ``1개社 당`` or ``1인 당``.
    """

    compact = _compact(text)
    if not compact:
        return None
    matched = {row.scope for row in per_unit_scope_occurrences(text)}
    if len(matched) != 1:
        return None
    scope = next(iter(matched))
    if not require_full_text:
        return scope

    aliases = _PER_UNIT_ALIASES[scope]
    if any(
        compact == f"{alias}{marker}"
        or (
            compact[:-len(f"{alias}{marker}")].isdigit()
            and compact.endswith(f"{alias}{marker}")
        )
        for alias in aliases
        for marker in _PER_UNIT_MARKERS
    ):
        return scope
    return None


__all__ = [
    "PerUnitScopeOccurrence",
    "explicit_per_unit_scope",
    "per_unit_scope_occurrences",
]
