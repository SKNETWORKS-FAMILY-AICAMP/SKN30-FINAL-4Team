"""Shared HTML <table> logical-grid resolver.

Surya's two table signals disagree in kind, not just in noise:

- `high_accuracy_result.blocks[label=Table].html` is a real HTML table with
  `rowspan`/`colspan`, i.e. it encodes *logical* cells (a merged cell is one
  <td>).
- `table_rec_tables[].result.{rows,cols,cells}` is a pure geometric grid: it
  always emits one physical cell per (row_id, col_id) pair, with no notion of
  a cell spanning several of those slots.

Comparing `len(<tr>)` against `len(rows)` (the previous approach) throws that
distinction away and also miscounts whenever a `<td>` carries `colspan`. This
module lays the HTML out the way a browser would (tracking which slots a
rowspan/colspan claims in later rows) so callers get the true logical grid
size and a slot -> logical-cell map, with no per-notice/page hardcoding.
"""
from __future__ import annotations

from html.parser import HTMLParser


def _to_span(value, default=1):
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


class _TableRowParser(HTMLParser):
    """Collects <tr> rows of {rowspan, colspan, text} in document order.
    Ignores everything outside <td>/<th>/<tr> (thead/tbody/table wrappers,
    nested tags inside a cell) — we only need span geometry and cell text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._current_row: list[dict] | None = None
        self._in_cell = False
        self._cell_attrs: dict = {}
        self._cell_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell_attrs = dict(attrs)
            self._cell_text = []

    def handle_startendtag(self, tag, attrs):
        # e.g. an empty <td/> — unusual but handle it like start+end.
        self.handle_starttag(tag, attrs)
        if tag in ("td", "th"):
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self._in_cell:
            self._cell_text.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            if self._current_row is None:
                self._current_row = []
            self._current_row.append({
                "rowspan": _to_span(self._cell_attrs.get("rowspan")),
                "colspan": _to_span(self._cell_attrs.get("colspan")),
                "text": "".join(self._cell_text).strip(),
            })
            self._in_cell = False
        elif tag == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None


def parse_html_table_grid(html: str) -> dict:
    """Lay out every <td>/<th> (with its rowspan/colspan) into the logical
    grid a browser would render.

    Returns:
        rows, cols: total logical grid size.
        cells: one entry per source <td>/<th>: {cell_id, row0, col0,
            row_span, col_span, text} where (row0, col0) is its top-left
            grid slot.
        grid: {(row, col): cell_id} for every physical slot the cell
            occupies, including slots it only covers via a span — this is
            what lets a TableRec physical (row_id, col_id) be resolved back
            to the logical cell that owns it.
    """
    parser = _TableRowParser()
    parser.feed(html or "")
    occupied: dict[tuple[int, int], bool] = {}
    grid: dict[tuple[int, int], int] = {}
    cells: list[dict] = []
    max_row = 0
    max_col = 0
    for row_index, row in enumerate(parser.rows):
        col_cursor = 0
        for raw_cell in row:
            while occupied.get((row_index, col_cursor)):
                col_cursor += 1
            cell_id = len(cells)
            row_span, col_span = raw_cell["rowspan"], raw_cell["colspan"]
            for dr in range(row_span):
                for dc in range(col_span):
                    r, c = row_index + dr, col_cursor + dc
                    occupied[(r, c)] = True
                    grid[(r, c)] = cell_id
                    max_row = max(max_row, r + 1)
                    max_col = max(max_col, c + 1)
            cells.append({
                "cell_id": cell_id,
                "row0": row_index,
                "col0": col_cursor,
                "row_span": row_span,
                "col_span": col_span,
                "text": raw_cell["text"],
            })
            col_cursor += col_span
    return {"rows": max_row, "cols": max_col, "cells": cells, "grid": grid}
