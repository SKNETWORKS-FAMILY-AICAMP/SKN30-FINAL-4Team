#!/usr/bin/env python3
"""Resolve a diagram node's text label against native pdf-inspector
occurrences inside the diagram's own bbox.

This module never picks a winner among duplicate/ambiguous matches. A label
either resolves to exactly one native occurrence ("unique"), or it does not
("ambiguous" when 2+ occurrences share the identical text, "unresolved" when
none do) — callers decide what to do with ambiguity; this module only ever
reports what is literally on the page, never a guess. There is no multi-item
concatenation fallback: only a single native text_item whose normalized text
equals the label counts as a match, so a compound label physically split
across two PDF text runs (e.g. "사업계획서" + "/IR 피칭 멘토링") is reported
as unresolved rather than silently stitched together.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_WS = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def diagram_pdf_bbox(render_bbox: list[float], scale: float, page_height: float) -> list[float]:
    """Convert a Diagram block bbox from render-pixel space (Surya) to
    pdf_user_space (pdf-inspector), matching the transform used everywhere
    else in this pipeline: x_pdf=x_render/scale; y_pdf=(height-y_render)/scale."""
    x0, y0, x1, y1 = render_bbox
    return [x0 / scale, (page_height - y1) / scale, x1 / scale, (page_height - y0) / scale]


def collect_bbox_native_candidates(native_items: list[dict], page: int, pdf_bbox: list[float]) -> list[tuple[int, str]]:
    """(native_index, raw_text) for every native text_item on `page` whose
    bbox is fully contained in pdf_bbox, in the native document's own
    (reading) order."""
    x0, y0, x1, y1 = pdf_bbox
    out = []
    for index, item in enumerate(native_items):
        if item.get("page") != page:
            continue
        bx0, by0 = item["x"], item["y"]
        bx1, by1 = item["x"] + item["width"], item["y"] + item["height"]
        if bx0 >= x0 and by0 >= y0 and bx1 <= x1 and by1 <= y1:
            out.append((index, item["text"]))
    return out


@dataclass
class NodeEvidence:
    label: str
    matches: list = field(default_factory=list)  # native_index list, ALL exact-text matches, never trimmed
    mapping_status: str = "unresolved"  # "unique" | "ambiguous" | "unresolved"

    def occurrence_ids(self, page: int) -> list[str]:
        return [f"occ:inspector:p{page}:t{i}" for i in self.matches]


def resolve_label(label: str, candidates: list[tuple[int, str]]) -> NodeEvidence:
    """candidates: (native_index, raw_text) pairs already filtered to one
    page and the diagram's bbox. Returns every native item whose normalized
    text exactly equals the normalized label -- 0, 1, or many -- and never
    reduces that set to a single "chosen" occurrence."""
    norm_label = normalize(label)
    if not norm_label:
        return NodeEvidence(label=label, matches=[], mapping_status="unresolved")
    matches = sorted(index for index, text in candidates if normalize(text) == norm_label)
    if not matches:
        status = "unresolved"
    elif len(matches) == 1:
        status = "unique"
    else:
        status = "ambiguous"
    return NodeEvidence(label=label, matches=matches, mapping_status=status)
