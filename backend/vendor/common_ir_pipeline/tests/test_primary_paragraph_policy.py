from __future__ import annotations

import dataclasses
import inspect
import math
import os
from unittest.mock import patch
import unittest

import common_ir_pipeline.pdf_fusion.primary_paragraph_policy as policy
from common_ir_pipeline.pdf_fusion.primary_paragraph_policy import (
    BoundaryDecision,
    POLICY_VERSION,
    PrimaryParagraphPolicyError,
    classify_native_continuity_boundary,
)


CROP_BOX = (0.0, 0.0, 595.0, 841.0)


def ledger(index: int, *, page: int = 3) -> dict[str, object]:
    return {
        "page": page,
        "source_item_index": index,
        "substantive_status": "substantive",
        "disposition": "owned_atomic",
        "primary_owner_unit_id": f"unit-native-{index}",
    }


def item(
    text: str,
    *,
    page: int = 3,
    x: float,
    y: float,
    width: float,
    height: float = 12.952980041503906,
    font_size: float = 12.952980041503906,
    font: str = "SubsetA+Body",
    font_tag: str = "F1",
    item_type: str = "text",
    is_bold: bool = False,
    is_italic: bool = False,
    is_strikeout: bool = False,
    is_underline: bool = False,
) -> dict[str, object]:
    return {
        "page": page,
        "item_type": item_type,
        "text": text,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "font_size": font_size,
        "font": font,
        "font_tag": font_tag,
        "is_bold": is_bold,
        "is_italic": is_italic,
        "is_strikeout": is_strikeout,
        "is_underline": is_underline,
    }


def purpose_boundary() -> dict[str, object]:
    return {
        "left_ledger": ledger(19),
        "right_ledger": ledger(20),
        "left_item": item(
            "▢  기술경쟁력을 보유한 팹리스 기업에서 설계·개발한 반도체의 상용화 개발을 지",
            x=63.325679779052734,
            y=676.5397338867188,
            width=475.54229736328125,
            font="SubsetA+Body",
            font_tag="F1",
        ),
        "right_item": item(
            "원하여 성공적인 제품화와 시장진입을 촉진",
            x=82.27540588378906,
            y=655.6824951171875,
            width=245.26707458496094,
            font="SubsetB+Body",
            font_tag="F4",
        ),
        "crop_box": CROP_BOX,
    }


class PrimaryParagraphPolicyTests(unittest.TestCase):
    def classify(self, **updates: object) -> BoundaryDecision:
        arguments = purpose_boundary()
        arguments.update(updates)
        return classify_native_continuity_boundary(**arguments)  # type: ignore[arg-type]

    def test_policy_identity_and_decision_are_immutable(self) -> None:
        self.assertEqual(POLICY_VERSION, "native_continuity_intra_word/v1")
        decision = self.classify()
        self.assertTrue(decision.qualified)
        self.assertEqual(decision.join_class, "intra_word_wrap")
        self.assertEqual(
            decision.reason_codes,
            (
                "native_source_adjacent",
                "owned_substantive_text",
                "style_compatible",
                "line_geometry_compatible",
                "full_width_body_slice",
                "hangul_intra_word_continuation",
            ),
        )
        self.assertEqual(decision.rejection_reason_codes, ())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.qualified = False  # type: ignore[misc]

    def test_font_subset_names_and_tags_do_not_need_to_match(self) -> None:
        arguments = purpose_boundary()
        left = dict(arguments["left_item"])  # type: ignore[arg-type]
        right = dict(arguments["right_item"])  # type: ignore[arg-type]
        left.update(font="AAA+Haansoft", font_tag="F1")
        right.update(font="BBB+Unknown", font_tag="F99")
        self.assertTrue(
            self.classify(left_item=left, right_item=right).qualified
        )

    def test_heading_body_boundary_is_rejected_by_general_features(self) -> None:
        left = item(
            "1.  추진목적",
            x=64.0452880859375,
            y=711.181884765625,
            width=79.39697265625,
            height=14.991874694824219,
            font_size=14.991874694824219,
        )
        right = purpose_boundary()["left_item"]
        decision = self.classify(
            left_ledger=ledger(17),
            right_ledger=ledger(19),
            left_item=left,
            right_item=right,
        )
        self.assertFalse(decision.qualified)
        self.assertEqual(decision.join_class, None)
        self.assertIn("non_adjacent_source_items", decision.rejection_reason_codes)
        self.assertIn("font_size_mismatch", decision.rejection_reason_codes)
        self.assertIn("vertical_gap_out_of_range", decision.rejection_reason_codes)
        self.assertIn("left_end_out_of_range", decision.rejection_reason_codes)
        self.assertIn(
            "left_orphan_not_single_hangul",
            decision.rejection_reason_codes,
        )

    def test_authority_item_type_style_and_table_veto_are_nonmatches(self) -> None:
        bad_owner = ledger(19)
        bad_owner["primary_owner_unit_id"] = None
        decision = self.classify(left_ledger=bad_owner)
        self.assertEqual(
            decision.rejection_reason_codes, ("non_owned_substantive",)
        )

        right = dict(purpose_boundary()["right_item"])  # type: ignore[arg-type]
        right["item_type"] = "image"
        right["font_size"] = 0.0
        decision = self.classify(right_item=right)
        self.assertIn("non_text_item", decision.rejection_reason_codes)

        right = dict(purpose_boundary()["right_item"])  # type: ignore[arg-type]
        right["is_bold"] = True
        decision = self.classify(right_item=right)
        self.assertIn("style_mismatch", decision.rejection_reason_codes)

        decision = self.classify(table_context_veto=True)
        self.assertEqual(decision.rejection_reason_codes, ("table_context_veto",))

    def test_relative_font_and_height_thresholds_are_inclusive(self) -> None:
        arguments = purpose_boundary()
        left = dict(arguments["left_item"])  # type: ignore[arg-type]
        right = dict(arguments["right_item"])  # type: ignore[arg-type]
        left["font_size"] = 10.0
        right["font_size"] = 10.0 / 0.97
        left["height"] = 10.0
        right["height"] = 10.0 / 0.97
        left["y"] = 400.0
        right["y"] = 400.0 - 1.5 * float(right["height"])
        right["x"] = float(left["x"]) + float(right["font_size"])
        self.assertTrue(
            self.classify(left_item=left, right_item=right).qualified
        )

        right["font_size"] = 10.0 / 0.969
        decision = self.classify(left_item=left, right_item=right)
        self.assertIn("font_size_mismatch", decision.rejection_reason_codes)

    def test_geometry_threshold_boundaries_are_inclusive(self) -> None:
        for vertical_delta in (1.25, 1.85):
            arguments = purpose_boundary()
            left = dict(arguments["left_item"])  # type: ignore[arg-type]
            right = dict(arguments["right_item"])  # type: ignore[arg-type]
            right["y"] = float(left["y"]) - vertical_delta * float(left["height"])
            self.assertTrue(
                self.classify(left_item=left, right_item=right).qualified
            )

        for horizontal_shift in (-0.5, 2.0):
            arguments = purpose_boundary()
            left = dict(arguments["left_item"])  # type: ignore[arg-type]
            right = dict(arguments["right_item"])  # type: ignore[arg-type]
            right["x"] = float(left["x"]) + horizontal_shift * float(left["font_size"])
            self.assertTrue(
                self.classify(left_item=left, right_item=right).qualified
            )

        arguments = purpose_boundary()
        left = dict(arguments["left_item"])  # type: ignore[arg-type]
        right = dict(arguments["right_item"])  # type: ignore[arg-type]
        left["x"] = CROP_BOX[2] * 0.20
        left["width"] = CROP_BOX[2] * 0.88 - float(left["x"])
        right["x"] = float(left["x"]) + float(left["font_size"])
        self.assertTrue(
            self.classify(left_item=left, right_item=right).qualified
        )

    def test_geometry_outside_thresholds_is_an_ordinary_nonmatch(self) -> None:
        cases = (
            ("y", 676.5397338867188 - 1.86 * 12.952980041503906, "vertical_gap_out_of_range"),
            ("x", 63.325679779052734 + 2.01 * 12.952980041503906, "horizontal_shift_out_of_range"),
        )
        for key, value, reason in cases:
            right = dict(purpose_boundary()["right_item"])  # type: ignore[arg-type]
            right[key] = value
            with self.subTest(reason=reason):
                self.assertIn(
                    reason,
                    self.classify(right_item=right).rejection_reason_codes,
                )

        left = dict(purpose_boundary()["left_item"])  # type: ignore[arg-type]
        left["x"] = CROP_BOX[2] * 0.201
        left["width"] = CROP_BOX[2] * 0.90 - float(left["x"])
        self.assertIn(
            "left_start_out_of_range",
            self.classify(left_item=left).rejection_reason_codes,
        )
        left["x"] = 50.0
        left["width"] = CROP_BOX[2] * 0.879 - 50.0
        self.assertIn(
            "left_end_out_of_range",
            self.classify(left_item=left).rejection_reason_codes,
        )

    def test_crop_membership_uses_reconstruction_plan_tolerance(self) -> None:
        left_x = float(purpose_boundary()["left_item"]["x"])  # type: ignore[index]
        tolerated_crop = (left_x + 0.5e-5, 0.0, 595.0, 841.0)
        self.assertTrue(self.classify(crop_box=tolerated_crop).qualified)

        rejected_crop = (left_x + 2e-5, 0.0, 595.0, 841.0)
        with self.assertRaisesRegex(PrimaryParagraphPolicyError, "inside crop"):
            self.classify(crop_box=rejected_crop)

    def test_whitespace_and_hangul_boundary_fail_closed_to_nonmatch(self) -> None:
        left = dict(purpose_boundary()["left_item"])  # type: ignore[arg-type]
        left["text"] = str(left["text"]) + " "
        self.assertIn(
            "left_trailing_whitespace",
            self.classify(left_item=left).rejection_reason_codes,
        )

        right = dict(purpose_boundary()["right_item"])  # type: ignore[arg-type]
        right["text"] = " 원하여 성공적인 제품화"
        self.assertIn(
            "right_leading_whitespace",
            self.classify(right_item=right).rejection_reason_codes,
        )

        left = dict(purpose_boundary()["left_item"])  # type: ignore[arg-type]
        left["text"] = "상용화 개발을 지원"
        self.assertIn(
            "left_orphan_not_single_hangul",
            self.classify(left_item=left).rejection_reason_codes,
        )

        right = dict(purpose_boundary()["right_item"])  # type: ignore[arg-type]
        right["text"] = "1차 후속 문장"
        self.assertIn(
            "right_continuation_not_hangul",
            self.classify(right_item=right).rejection_reason_codes,
        )

    def test_malformed_values_raise_instead_of_becoming_nonmatches(self) -> None:
        with self.assertRaisesRegex(PrimaryParagraphPolicyError, "object"):
            self.classify(left_ledger=[])  # type: ignore[arg-type]
        with self.assertRaisesRegex(PrimaryParagraphPolicyError, "required"):
            self.classify(left_ledger={})
        with self.assertRaisesRegex(PrimaryParagraphPolicyError, "finite"):
            left = dict(purpose_boundary()["left_item"])  # type: ignore[arg-type]
            left["x"] = math.nan
            self.classify(left_item=left)
        with self.assertRaisesRegex(PrimaryParagraphPolicyError, "positive"):
            left = dict(purpose_boundary()["left_item"])  # type: ignore[arg-type]
            left["width"] = 0.0
            self.classify(left_item=left)
        with self.assertRaisesRegex(PrimaryParagraphPolicyError, "inside crop"):
            left = dict(purpose_boundary()["left_item"])  # type: ignore[arg-type]
            left["x"] = -1.0
            self.classify(left_item=left)
        with self.assertRaisesRegex(PrimaryParagraphPolicyError, "ledger page"):
            left = dict(purpose_boundary()["left_item"])  # type: ignore[arg-type]
            left["page"] = 2
            self.classify(left_item=left)
        with self.assertRaisesRegex(PrimaryParagraphPolicyError, "boolean"):
            self.classify(table_context_veto=1)

    def test_policy_has_no_gold_path_or_environment_dependency(self) -> None:
        source = inspect.getsource(policy)
        self.assertNotIn("primary_structure_gold", source)
        self.assertNotIn("Path(", source)
        self.assertFalse(hasattr(policy, "os"))
        self.assertFalse(hasattr(policy, "Path"))
        with patch.dict(os.environ, {}, clear=True):
            first = self.classify()
        with patch.dict(
            os.environ,
            {
                "PDF_PRIMARY_STRUCTURE_GOLD": "/does/not/exist.json",
                "GOLD_PATH": "/also/missing",
            },
            clear=True,
        ):
            second = self.classify()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
