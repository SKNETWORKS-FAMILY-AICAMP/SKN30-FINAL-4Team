#!/usr/bin/env python3
"""Direct-arrow diagram edge parser: Surya high-accuracy Diagram HTML only.

Promotes an edge only when both ends are literal step-node text and the link
between them is a literal arrow glyph observed in the HTML itself — never an
inferred/natural-language relation. Two arrow placements are recognized,
matching Surya's own Diagram HTML table convention (see
PBLN_000000000125090 p3):

  1. An arrow-only <td> between two node cells in the same row:
     `<td>A</td><td>→</td><td>B</td>` (A -> B), `←` reversed (right -> left).
  2. A trailing arrow token as the cell's own *last* line, connecting to the
     cell directly below/above it in the next/previous row:
     `<td>A...<br/>↓</td>` over `<td>B...</td>` in the next <tr> at the same
     column index -> A -> B (`↑` reversed).

A node's label is its first bold (`<b>`/`<strong>`) line if present, else its
first non-empty text line — this is what "the node text" means for a step
box that also carries a date/description underneath.

Anything else abstains — returns no edge for that piece of evidence:
  - no `<table>` at all (e.g. a bare `<img/>` Diagram placeholder, or plain
    prose): nothing to parse.
  - any `rowspan`/`colspan` anywhere in the table: column alignment across
    rows would no longer be reliable, so the whole table is skipped rather
    than guessed at.
  - an arrow cell missing a node neighbor on the required side.
  - an arrow-only cell adjacent to *another* arrow-only cell (which side is
    the real node is ambiguous).
  - a bidirectional glyph (↔, ↕) — direction is not literally stated.
  - any symbol not in the recognized arrow set, or an arrow embedded inside
    a longer line of prose rather than alone on its own line/cell.

This module never guesses; ambiguity always resolves to "no edge". It does
not touch native occurrences, coordinates, or the Common IR pipeline — pure
HTML-in, edges-out, stdlib only (no third-party dependency).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Unidirectional arrow glyphs this parser trusts as literal, observed edges.
# Bidirectional glyphs (↔, ↕) are deliberately excluded: which node is "from"
# is not literally stated, so treating them as edges would be inference.
RIGHT = {"→", "⇒"}
LEFT = {"←", "⇐"}
DOWN = {"↓", "⇓"}
UP = {"↑", "⇑"}
HORIZONTAL_ARROWS = RIGHT | LEFT
VERTICAL_ARROWS = DOWN | UP
ALL_ARROWS = HORIZONTAL_ARROWS | VERTICAL_ARROWS

_WS = re.compile(r"\s+")
_BREAK_TAGS = {"br", "p", "div"}


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _to_int(value, default=1):
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass
class _Cell:
    lines: list = field(default_factory=lambda: [""])
    bold_lines: list = field(default_factory=lambda: [""])
    rowspan: int = 1
    colspan: int = 1

    def label(self) -> str:
        """First bold line if present, else the first non-empty text line."""
        for line in self.bold_lines:
            if _clean(line):
                return _clean(line)
        for line in self.lines:
            if _clean(line):
                return _clean(line)
        return ""

    def _last_nonempty_line(self) -> str:
        for line in reversed(self.lines):
            if _clean(line):
                return _clean(line)
        return ""

    def is_arrow_only(self) -> str | None:
        """The arrow glyph if the cell's *entire* content is exactly one
        recognized arrow glyph and nothing else; else None."""
        text = _clean("".join(self.lines))
        return text if text in ALL_ARROWS else None

    def trailing_arrow(self) -> str | None:
        """A vertical arrow glyph if it is the cell's own last line (a token
        alone, not mixed into other text); else None."""
        last = self._last_nonempty_line()
        return last if last in VERTICAL_ARROWS else None


class _DiagramTableParser(HTMLParser):
    """Collects <tr> rows of _Cell, split into lines at <br/>/<p>/<div>.
    Content outside a <table> (e.g. a bare <img/> placeholder) is ignored."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._bold_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            attrs = dict(attrs)
            self._cell = _Cell(rowspan=_to_int(attrs.get("rowspan")), colspan=_to_int(attrs.get("colspan")))
        elif tag in ("b", "strong"):
            self._bold_depth += 1
            if self._cell is not None:
                self._cell.bold_lines.append("")
        elif tag in _BREAK_TAGS and self._cell is not None:
            self._cell.lines.append("")
            if self._bold_depth:
                self._cell.bold_lines.append("")

    def handle_data(self, data):
        if self._cell is None:
            return
        self._cell.lines[-1] += data
        if self._bold_depth:
            self._cell.bold_lines[-1] += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            if self._row is None:
                self._row = []
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag in ("b", "strong"):
            self._bold_depth = max(0, self._bold_depth - 1)


@dataclass
class Edge:
    from_label: str
    to_label: str
    arrow: str
    row: int
    col: int
    placement: str  # "horizontal_arrow_cell" | "vertical_trailing_token"


def extract_direct_arrow_edges(html: str) -> list[Edge]:
    """Parse literal arrow-linked node pairs out of Surya Diagram HTML.

    Returns [] whenever the input isn't a clean, span-free arrow table.
    """
    if not html or "<table" not in html.lower():
        return []
    parser = _DiagramTableParser()
    parser.feed(html)
    rows = parser.rows
    if not rows:
        return []
    if any(cell.rowspan != 1 or cell.colspan != 1 for row in rows for cell in row):
        return []

    edges: list[Edge] = []

    # 1) Arrow-only cell between two node cells in the same row.
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            arrow = cell.is_arrow_only()
            if not arrow or arrow not in HORIZONTAL_ARROWS:
                continue
            left = row[c - 1] if c - 1 >= 0 else None
            right = row[c + 1] if c + 1 < len(row) else None
            if left is None or right is None:
                continue  # missing endpoint -> abstain
            if left.is_arrow_only() or right.is_arrow_only():
                continue  # neighbor is itself an arrow cell -> abstain
            left_label, right_label = left.label(), right.label()
            if not left_label or not right_label:
                continue  # no node text on one side -> abstain
            if arrow in RIGHT:
                edges.append(Edge(left_label, right_label, arrow, r, c, "horizontal_arrow_cell"))
            else:  # LEFT: arrow points left, flow runs right-cell -> left-cell
                edges.append(Edge(right_label, left_label, arrow, r, c, "horizontal_arrow_cell"))

    # 2) Trailing vertical arrow token inside a node cell -> same-column cell
    #    in the next/previous row.
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            arrow = cell.trailing_arrow()
            if not arrow:
                continue
            other_r = r + 1 if arrow in DOWN else r - 1
            if other_r < 0 or other_r >= len(rows) or c >= len(rows[other_r]):
                continue  # no neighbor row/column -> abstain
            other = rows[other_r][c]
            from_label, to_label = cell.label(), other.label()
            if not from_label or not to_label:
                continue
            if arrow in DOWN:
                edges.append(Edge(from_label, to_label, arrow, r, c, "vertical_trailing_token"))
            else:  # UP: this cell receives from the cell above it
                edges.append(Edge(to_label, from_label, arrow, r, c, "vertical_trailing_token"))

    return edges
