"""Conservative native-continuity policy for one PDF paragraph boundary.

The policy is deliberately pure and Gold-independent.  It classifies only a
pair of adjacent native capture items and never reads files, environment
variables, parser sidecars, or reviewed fixtures.  A document-view builder can
therefore apply it to every raw native boundary before resolving overlapping
proposals and replacing atomic fallback leaves.

Version 1 recognizes only a narrow Korean intra-word line wrap in a full-width
body slice.  Other valid boundaries are ordinary non-matches, not errors.
Malformed or internally inconsistent inputs fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence


POLICY_VERSION = "native_continuity_intra_word/v1"

MAX_RELATIVE_FONT_SIZE_DELTA = 0.03
MAX_RELATIVE_LINE_HEIGHT_DELTA = 0.03
MIN_NORMALIZED_VERTICAL_DELTA = 1.25
MAX_NORMALIZED_VERTICAL_DELTA = 1.85
MIN_NORMALIZED_HORIZONTAL_SHIFT = -0.5
MAX_NORMALIZED_HORIZONTAL_SHIFT = 2.0
MAX_LEFT_START_FRACTION = 0.20
MIN_LEFT_END_FRACTION = 0.88
GEOMETRY_TOLERANCE_PT = 1e-5
_COMPARISON_EPSILON = 1e-12

_STYLE_FLAGS = ("is_bold", "is_italic", "is_strikeout", "is_underline")
_QUALIFIED_REASON_CODES = (
    "native_source_adjacent",
    "owned_substantive_text",
    "style_compatible",
    "line_geometry_compatible",
    "full_width_body_slice",
    "hangul_intra_word_continuation",
)


class PrimaryParagraphPolicyError(ValueError):
    """Raised when a boundary input is malformed or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    """Deterministic result for one raw native-item boundary."""

    qualified: bool
    join_class: str | None
    reason_codes: tuple[str, ...]
    rejection_reason_codes: tuple[str, ...]


def _mapping(name: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PrimaryParagraphPolicyError(f"{name} must be an object")
    return value


def _required(mapping: Mapping[str, Any], name: str, key: str) -> object:
    if key not in mapping:
        raise PrimaryParagraphPolicyError(f"{name}.{key} is required")
    return mapping[key]


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PrimaryParagraphPolicyError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PrimaryParagraphPolicyError(
            f"{name} must be a non-negative integer"
        )
    return value


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PrimaryParagraphPolicyError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise PrimaryParagraphPolicyError(f"{name} must be a finite number")
    return 0.0 if result == 0.0 else result


def _positive_finite(name: str, value: object) -> float:
    result = _finite(name, value)
    if result <= 0.0:
        raise PrimaryParagraphPolicyError(f"{name} must be positive")
    return result


def _nonnegative_finite(name: str, value: object) -> float:
    result = _finite(name, value)
    if result < 0.0:
        raise PrimaryParagraphPolicyError(f"{name} must be non-negative")
    return result


def _string(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise PrimaryParagraphPolicyError(f"{name} must be a string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise PrimaryParagraphPolicyError(
            f"{name} must not contain surrogate code points"
        )
    return value


def _optional_owner(name: str, value: object) -> str | None:
    if value is None:
        return None
    owner = _string(name, value)
    if not owner or owner != owner.strip():
        raise PrimaryParagraphPolicyError(
            f"{name} must be null or a non-empty trimmed string"
        )
    return owner


def _boolean(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise PrimaryParagraphPolicyError(f"{name} must be a boolean")
    return value


def _crop_box(value: object) -> tuple[float, float, float, float]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or len(value) != 4
    ):
        raise PrimaryParagraphPolicyError(
            "crop_box must contain exactly four coordinates"
        )
    x0, y0, x1, y1 = (
        _finite(f"crop_box[{index}]", item) for index, item in enumerate(value)
    )
    if not x0 < x1 or not y0 < y1:
        raise PrimaryParagraphPolicyError(
            "crop_box must have strictly increasing bounds"
        )
    return x0, y0, x1, y1


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    page: int
    source_item_index: int
    substantive_status: str
    disposition: str
    primary_owner_unit_id: str | None


@dataclass(frozen=True, slots=True)
class _NativeItem:
    page: int
    item_type: str
    text: str
    x: float
    y: float
    width: float
    height: float
    font_size: float
    style: tuple[bool, bool, bool, bool]

    @property
    def x1(self) -> float:
        return self.x + self.width

    @property
    def y1(self) -> float:
        return self.y + self.height


def _ledger_entry(name: str, value: object) -> _LedgerEntry:
    mapping = _mapping(name, value)
    return _LedgerEntry(
        page=_positive_int(f"{name}.page", _required(mapping, name, "page")),
        source_item_index=_nonnegative_int(
            f"{name}.source_item_index",
            _required(mapping, name, "source_item_index"),
        ),
        substantive_status=_string(
            f"{name}.substantive_status",
            _required(mapping, name, "substantive_status"),
        ),
        disposition=_string(
            f"{name}.disposition", _required(mapping, name, "disposition")
        ),
        primary_owner_unit_id=_optional_owner(
            f"{name}.primary_owner_unit_id",
            _required(mapping, name, "primary_owner_unit_id"),
        ),
    )


def _native_item(name: str, value: object) -> _NativeItem:
    mapping = _mapping(name, value)
    item_type = _string(
        f"{name}.item_type", _required(mapping, name, "item_type")
    )
    width = _positive_finite(
        f"{name}.width", _required(mapping, name, "width")
    )
    height = _positive_finite(
        f"{name}.height", _required(mapping, name, "height")
    )
    # Font names deliberately are type-checked but never compared.  PDF font
    # subset boundaries frequently change the name in the middle of one word.
    _string(f"{name}.font", _required(mapping, name, "font"))
    _string(f"{name}.font_tag", _required(mapping, name, "font_tag"))
    font_size = _nonnegative_finite(
        f"{name}.font_size", _required(mapping, name, "font_size")
    )
    if item_type == "text" and font_size <= 0.0:
        raise PrimaryParagraphPolicyError(
            f"{name}.font_size must be positive for text"
        )
    return _NativeItem(
        page=_positive_int(f"{name}.page", _required(mapping, name, "page")),
        item_type=item_type,
        text=_string(f"{name}.text", _required(mapping, name, "text")),
        x=_finite(f"{name}.x", _required(mapping, name, "x")),
        y=_finite(f"{name}.y", _required(mapping, name, "y")),
        width=width,
        height=height,
        font_size=font_size,
        style=tuple(
            _boolean(
                f"{name}.{flag}",
                _required(mapping, name, flag),
            )
            for flag in _STYLE_FLAGS
        ),
    )


def _inside_crop(
    item: _NativeItem,
    crop: tuple[float, float, float, float],
) -> bool:
    return (
        crop[0] - GEOMETRY_TOLERANCE_PT <= item.x
        and crop[1] - GEOMETRY_TOLERANCE_PT <= item.y
        and item.x1 <= crop[2] + GEOMETRY_TOLERANCE_PT
        and item.y1 <= crop[3] + GEOMETRY_TOLERANCE_PT
    )


def _is_precomposed_hangul_syllable(value: str) -> bool:
    return len(value) == 1 and "\uac00" <= value <= "\ud7a3"


def classify_native_continuity_boundary(
    *,
    left_ledger: Mapping[str, Any],
    right_ledger: Mapping[str, Any],
    left_item: Mapping[str, Any],
    right_item: Mapping[str, Any],
    crop_box: Sequence[float],
    table_context_veto: bool = False,
) -> BoundaryDecision:
    """Classify one raw native-item boundary without consulting Gold.

    Malformed values, invalid geometry, and ledger/capture disagreement raise
    ``PrimaryParagraphPolicyError``.  Well-formed boundaries that do not meet
    the deliberately narrow policy return an unqualified decision containing
    fixed-order rejection reason codes.
    """

    if not isinstance(table_context_veto, bool):
        raise PrimaryParagraphPolicyError("table_context_veto must be a boolean")

    left_owner = _ledger_entry("left_ledger", left_ledger)
    right_owner = _ledger_entry("right_ledger", right_ledger)
    left = _native_item("left_item", left_item)
    right = _native_item("right_item", right_item)
    crop = _crop_box(crop_box)

    if left_owner.page != left.page or right_owner.page != right.page:
        raise PrimaryParagraphPolicyError(
            "ledger page must equal its native item page"
        )
    if not _inside_crop(left, crop) or not _inside_crop(right, crop):
        raise PrimaryParagraphPolicyError(
            "native item bbox must remain inside crop_box"
        )

    rejected: list[str] = []
    if left.page != right.page:
        rejected.append("different_page")
    if right_owner.source_item_index != left_owner.source_item_index + 1:
        rejected.append("non_adjacent_source_items")
    if not all(
        entry.substantive_status == "substantive"
        and entry.disposition == "owned_atomic"
        and entry.primary_owner_unit_id is not None
        for entry in (left_owner, right_owner)
    ):
        rejected.append("non_owned_substantive")
    if left.item_type != "text" or right.item_type != "text":
        rejected.append("non_text_item")
    if table_context_veto:
        rejected.append("table_context_veto")
    if left.style != right.style:
        rejected.append("style_mismatch")

    if left.item_type != "text" or right.item_type != "text":
        return BoundaryDecision(
            qualified=False,
            join_class=None,
            reason_codes=(),
            rejection_reason_codes=tuple(rejected),
        )

    maximum_font_size = max(left.font_size, right.font_size)
    if (
        abs(left.font_size - right.font_size) / maximum_font_size
        > MAX_RELATIVE_FONT_SIZE_DELTA + _COMPARISON_EPSILON
    ):
        rejected.append("font_size_mismatch")

    maximum_height = max(left.height, right.height)
    if (
        abs(left.height - right.height) / maximum_height
        > MAX_RELATIVE_LINE_HEIGHT_DELTA + _COMPARISON_EPSILON
    ):
        rejected.append("line_height_mismatch")

    normalized_vertical_delta = (left.y - right.y) / maximum_height
    if not (
        MIN_NORMALIZED_VERTICAL_DELTA - _COMPARISON_EPSILON
        <= normalized_vertical_delta
        <= MAX_NORMALIZED_VERTICAL_DELTA + _COMPARISON_EPSILON
    ):
        rejected.append("vertical_gap_out_of_range")

    normalized_horizontal_shift = (right.x - left.x) / maximum_font_size
    if not (
        MIN_NORMALIZED_HORIZONTAL_SHIFT - _COMPARISON_EPSILON
        <= normalized_horizontal_shift
        <= MAX_NORMALIZED_HORIZONTAL_SHIFT + _COMPARISON_EPSILON
    ):
        rejected.append("horizontal_shift_out_of_range")

    crop_width = crop[2] - crop[0]
    left_start_fraction = (left.x - crop[0]) / crop_width
    left_end_fraction = (left.x1 - crop[0]) / crop_width
    if left_start_fraction > MAX_LEFT_START_FRACTION + _COMPARISON_EPSILON:
        rejected.append("left_start_out_of_range")
    if left_end_fraction < MIN_LEFT_END_FRACTION - _COMPARISON_EPSILON:
        rejected.append("left_end_out_of_range")

    if left.text != left.text.rstrip():
        rejected.append("left_trailing_whitespace")
    if right.text != right.text.lstrip():
        rejected.append("right_leading_whitespace")

    left_tokens = left.text.split()
    left_orphan = left_tokens[-1] if left_tokens else ""
    if not _is_precomposed_hangul_syllable(left_orphan):
        rejected.append("left_orphan_not_single_hangul")
    if not right.text or not _is_precomposed_hangul_syllable(right.text[0]):
        rejected.append("right_continuation_not_hangul")

    if rejected:
        return BoundaryDecision(
            qualified=False,
            join_class=None,
            reason_codes=(),
            rejection_reason_codes=tuple(rejected),
        )
    return BoundaryDecision(
        qualified=True,
        join_class="intra_word_wrap",
        reason_codes=_QUALIFIED_REASON_CODES,
        rejection_reason_codes=(),
    )


__all__ = [
    "BoundaryDecision",
    "POLICY_VERSION",
    "PrimaryParagraphPolicyError",
    "classify_native_continuity_boundary",
]
