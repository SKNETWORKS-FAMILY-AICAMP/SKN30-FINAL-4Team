"""Deterministic semantic completeness checks for Request Profiles.

The selector may choose only exact source spans.  These checks do not create
facts or rewrite source text; they only reject a successful materialization
when a small, closed set of explicit form labels still has no selected span.
Keeping this policy outside the assembler prevents field-specific omissions
from accumulating in the profile construction code.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .field_regions import RequestFieldRegion, build_field_regions
from .models import CandidatePack


# Parenthesised labels observed in the Request form identify an explicit
# organisation role.  This is intentionally closed: prose such as "지원" or
# "평가" is an action and must not silently become a role.
_EXPLICIT_ROLE = re.compile(
    r"[\(（\[]\s*(?P<role>주관|협력|총괄|전담|운영)\s*[\)）\]]"
)


def _value_source(row: dict[str, Any] | None) -> tuple[str, int, int] | None:
    if not isinstance(row, dict):
        return None
    source = row.get("value_source")
    if not isinstance(source, dict):
        return None
    block_id = source.get("source_block_id")
    start = source.get("start_char")
    end = source.get("end_char")
    if (
        not isinstance(block_id, str)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end <= start
    ):
        return None
    return block_id, start, end


def _selected_sources(rows: Iterable[dict[str, Any]]) -> list[tuple[str, int, int]]:
    return [source for row in rows if (source := _value_source(row)) is not None]


def _region_covered(
    region: RequestFieldRegion, selected: Iterable[tuple[str, int, int]]
) -> bool:
    return any(
        block_id == region.block_id
        and region.contains(start, end)
        for block_id, start, end in selected
    )


def _missing_region_locators(
    pack: CandidatePack,
    *,
    field_name: str,
    selected_rows: Iterable[dict[str, Any]],
) -> list[str]:
    selected = _selected_sources(selected_rows)
    missing = {
        f"{region.block_id}@{region.content_start}:{region.content_end}"
        for region in build_field_regions(pack, field_name=field_name)
        if not _region_covered(region, selected)
    }
    return sorted(missing)


def _explicit_role_obligations(
    pack: CandidatePack,
) -> list[tuple[str, int, int]]:
    obligations: set[tuple[str, int, int]] = set()
    for region in build_field_regions(pack, field_name="delivery_relations"):
        for match in _EXPLICIT_ROLE.finditer(region.content_text):
            start = region.content_start + match.start("role")
            end = region.content_start + match.end("role")
            obligations.add((region.block_id, start, end))
    return sorted(obligations)


def request_semantic_completeness_failures(
    pack: CandidatePack,
    *,
    request_context: dict[str, list[dict[str, Any]]],
    comparison_profile: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Return source-identifier-only failures for explicit Request fields."""

    failures: list[str] = []

    missing_plans = _missing_region_locators(
        pack,
        field_name="implementation_plan",
        selected_rows=request_context.get("implementation_plan", ()),
    )
    if missing_plans:
        failures.append(
            "implementation_plan has no source-visible coverage for labeled "
            "annual/sub-program plan regions=" + ",".join(missing_plans)
        )

    missing_methods = _missing_region_locators(
        pack,
        field_name="delivery_methods",
        selected_rows=comparison_profile.get("delivery_methods", ()),
    )
    if missing_methods:
        failures.append(
            "delivery_methods has no source-visible coverage for explicit "
            "execution-method regions=" + ",".join(missing_methods)
        )

    selected_roles = _selected_sources(
        relation.get("role")
        for relation in comparison_profile.get("delivery_relations", ())
        if isinstance(relation, dict) and isinstance(relation.get("role"), dict)
    )
    missing_roles = [
        f"{block_id}@{start}:{end}"
        for block_id, start, end in _explicit_role_obligations(pack)
        if not any(
            selected_block == block_id
            and selected_start <= start
            and end <= selected_end
            for selected_block, selected_start, selected_end in selected_roles
        )
    ]
    if missing_roles:
        failures.append(
            "delivery_relation_roles have no source-visible coverage for "
            "explicit actor-role markers=" + ",".join(missing_roles)
        )

    return failures


__all__ = ["request_semantic_completeness_failures"]
