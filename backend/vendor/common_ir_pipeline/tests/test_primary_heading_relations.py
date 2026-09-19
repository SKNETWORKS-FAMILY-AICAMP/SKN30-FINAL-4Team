from __future__ import annotations

import ast
import copy
from dataclasses import replace
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

try:
    import test_primary_document_view as view_fixtures
    import test_reconstruction_plan as reconstruction_fixtures
except ModuleNotFoundError as error:
    if error.name not in {"test_primary_document_view", "test_reconstruction_plan"}:
        raise
    from backend.vendor.common_ir_pipeline.tests import (
        test_primary_document_view as view_fixtures,
        test_reconstruction_plan as reconstruction_fixtures,
    )

import common_ir_pipeline.pdf_fusion.primary_heading_relations as contract
from common_ir_pipeline.pdf_fusion.coordinate_manifest import build_sidecar_binding
from common_ir_pipeline.pdf_fusion.native_capture import capture_pdf_to_native
from common_ir_pipeline.pdf_fusion.opendataloader_coordinate_calibration import (
    REVIEWED_PROOF_CANONICAL_SHA256,
)
from common_ir_pipeline.pdf_fusion.primary_document_view import (
    build_pdf_primary_document_view,
)
from common_ir_pipeline.pdf_fusion.primary_heading_relations import (
    PdfPrimaryHeadingRelationsError,
    build_pdf_primary_heading_relations,
    canonical_pdf_primary_heading_relations_json,
    load_pdf_primary_heading_relations_file,
    parse_pdf_primary_heading_relations_bytes,
    validate_pdf_primary_heading_relations,
    validate_pdf_primary_heading_relations_against_inputs,
)
from common_ir_pipeline.pdf_fusion.reconstruction_plan import (
    build_pdf_reconstruction_plan,
)
from common_ir_pipeline.pdf_fusion.render_manifest import (
    PdfRenderManifest,
    RenderedPage,
)
from common_ir_pipeline.pdf_fusion.surya_layout_artifact import (
    MAX_TOTAL_REGIONS,
    SuryaLayoutArtifact,
    SuryaLayoutPage,
    SuryaLayoutRegion,
    SuryaProducerIdentity,
)


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/common_ir_pipeline/pdf_fusion/schemas/pdf_primary_heading_relations_v1.schema.json"
)


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class HeadingInspector(reconstruction_fixtures.FakeInspector):
    def __init__(
        self,
        *,
        heading_text: str = "1.  목적",
        heading_font_size: float = 12.0,
        body_x: float = 10.0,
        body_y: float = 52.0,
        continuation_y: float = 37.0,
        intervening: bool = False,
    ) -> None:
        super().__init__()
        self.heading_text = heading_text
        self.heading_font_size = heading_font_size
        self.body_x = body_x
        self.body_y = body_y
        self.continuation_y = continuation_y
        self.intervening = intervening

    def extract_text_with_positions_bytes(self, data: bytes) -> list[dict]:
        del data
        values: list[tuple[str, float, float, float, float, float, str]] = [
            (
                self.heading_text,
                10.0,
                72.0,
                30.0,
                12.0,
                self.heading_font_size,
                "HeadingFixture",
            )
        ]
        if self.intervening:
            values.append(("중간", 60.0, 66.0, 15.0, 8.0, 8.0, "BodyFixture"))
        values.extend(
            [
                (
                    "□ 사업화를 지",
                    self.body_x,
                    self.body_y,
                    90.0 - self.body_x,
                    10.0,
                    10.0,
                    "BodyFixture",
                ),
                (
                    "원하여 시장에 진입",
                    self.body_x + 1.0,
                    self.continuation_y,
                    40.0,
                    10.0,
                    10.0,
                    "BodyFixtureSubset",
                ),
            ]
        )
        output: list[dict] = []
        for index, (text, x, y, width, height, font_size, font) in enumerate(values):
            output.append(
                {
                    "text": text,
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                    "font": font,
                    "font_tag": f"F{index + 1}",
                    "font_size": font_size,
                    "page": 1,
                    "is_bold": False,
                    "is_italic": False,
                    "is_underline": False,
                    "is_strikeout": False,
                    "item_type": "text",
                    "mcid": index,
                }
            )
        return output


class PrimaryHeadingRelationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = reconstruction_fixtures.PdfReconstructionPlanTests()
        self.fixture.setUp()
        self.render = self.render_with_real_file()
        self.producer = SuryaProducerIdentity(
            engine_id="surya",
            engine_version="fixture-1",
            model_id="fixture-layout",
            model_revision="r1",
            model_weights_sha256=digest("weights"),
            pipeline_revision="fixture-pipeline",
            config_sha256=digest("config"),
            worker_image_digest="sha256:" + digest("image"),
        )
        self.logical_compute_key = digest("heading-layout")
        self.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(self.schema)
        self.schema_validator = Draft202012Validator(self.schema)

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def render_with_real_file(self) -> PdfRenderManifest:
        base = self.fixture.render()
        encoded = view_fixtures.rgb_png(200, 200)
        image_path = self.fixture.root / "rendered/page-0001.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(encoded)
        image_sha256 = digest(encoded)
        coordinate = replace(
            base.pages[0].coordinate_manifest,
            page_image_sha256=image_sha256,
        )
        page = RenderedPage(
            page=1,
            image_relative_path="rendered/page-0001.png",
            image_sha256=image_sha256,
            image_size_bytes=len(encoded),
            coordinate_manifest=coordinate,
            coordinate_manifest_sha256=coordinate.manifest_sha256(),
        )
        return PdfRenderManifest(
            source_pdf_relative_path="source.pdf",
            source_pdf_sha256=base.source_pdf_sha256,
            source_pdf_size_bytes=base.source_pdf_size_bytes,
            page_count=1,
            renderer=base.renderer,
            renderer_version=base.renderer_version,
            renderer_config_sha256=base.renderer_config_sha256,
            pages=(page,),
        )

    def region(
        self,
        order: int,
        label: str,
        bbox: tuple[float, float, float, float],
        *,
        polygon: tuple[tuple[float, float], ...] | None = None,
    ) -> SuryaLayoutRegion:
        x0, y0, x1, y1 = bbox
        return SuryaLayoutRegion(
            region_id=f"p0001-{label}-{order + 1:04d}",
            label=label,
            bbox_px=bbox,
            polygon_px=polygon
            or ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
            confidence=None,
            reading_order=order,
        )

    def artifact(
        self, regions: list[SuryaLayoutRegion] | None = None
    ) -> SuryaLayoutArtifact:
        regions = regions or [
            self.region(0, "section_header", (18.0, 30.0, 82.0, 58.0)),
            self.region(1, "list_group", (16.0, 72.0, 184.0, 130.0)),
        ]
        page = SuryaLayoutPage(
            page=1,
            sidecar_binding=build_sidecar_binding(
                self.render.pages[0].coordinate_manifest
            ),
            pixel_width=200,
            pixel_height=200,
            rendered_page_px=(0.0, 0.0, 200.0, 200.0),
            regions=tuple(regions),
        )
        return SuryaLayoutArtifact(
            logical_compute_key=self.logical_compute_key,
            source_sha256=self.render.source_pdf_sha256,
            page_count=1,
            render_manifest_schema_version=self.render.schema_version,
            render_manifest_sha256=self.render.manifest_sha256(),
            producer=self.producer,
            requested_pages=(1,),
            pages=(page,),
        )

    def arguments(
        self,
        *,
        inspector: HeadingInspector | None = None,
        artifact: SuryaLayoutArtifact | None = None,
    ) -> dict:
        native = capture_pdf_to_native(
            self.fixture.source,
            notice_id="PBLN_fixture",
            source_relative_path="attachments/source.pdf",
            inspector_module=inspector or HeadingInspector(),
            inspector_version="1.17.0",
        )
        candidates = self.fixture.candidates(
            {"number of pages": 1, "kids": []}
        )
        plan = build_pdf_reconstruction_plan(
            source_pdf=self.fixture.source,
            native_capture=native,
            structure_candidates=candidates,
            render_manifest=self.render,
            calibration_proof=self.fixture.proof,
            expected_calibration_proof_sha256=self.fixture.proof_sha256,
        )
        selected_artifact = artifact or self.artifact()
        return {
            "source_pdf": self.fixture.source,
            "native_capture": native,
            "structure_candidates": candidates,
            "render_manifest": self.render,
            "render_artifact_root": self.fixture.root,
            "calibration_proof": self.fixture.proof,
            "expected_calibration_proof_sha256": self.fixture.proof_sha256,
            "reconstruction_plan": plan,
            "surya_layout_artifact": selected_artifact.to_dict(),
            "expected_surya_producer": self.producer,
            "expected_surya_logical_compute_key": self.logical_compute_key,
            "expected_surya_pages": (1,),
        }

    def test_consensus_emits_one_textless_additive_relation(self) -> None:
        arguments = self.arguments()
        result = build_pdf_primary_heading_relations(**arguments)
        self.assertEqual(len(result["relations"]), 1)
        relation = result["relations"][0]
        self.assertEqual(relation["kind"], "heading_to_body")
        self.assertEqual(relation["page"], 1)
        self.assertEqual(
            result["policy"],
            {"policy_version": "primary_numbered_heading_relation/v1"},
        )
        self.assertEqual(
            relation["surya_region_id"], "p0001-section_header-0001"
        )
        self.assertNotEqual(relation["heading_leaf_id"], relation["body_leaf_id"])
        self.schema_validator.validate(result)
        self.assertEqual(result, validate_pdf_primary_heading_relations(result))

        primary_arguments = {
            key: arguments[key]
            for key in (
                "source_pdf",
                "native_capture",
                "structure_candidates",
                "render_manifest",
                "render_artifact_root",
                "calibration_proof",
                "expected_calibration_proof_sha256",
                "reconstruction_plan",
            )
        }
        primary = build_pdf_primary_document_view(**primary_arguments)
        self.assertEqual(primary["relations"], [])
        heading_leaf, body_leaf = primary["leaves"]
        self.assertEqual(heading_leaf["kind"], "unclassified_text")
        self.assertEqual(body_leaf["kind"], "paragraph")
        self.assertEqual(relation["heading_leaf_id"], heading_leaf["leaf_id"])
        self.assertEqual(relation["body_leaf_id"], body_leaf["leaf_id"])

        encoded = canonical_pdf_primary_heading_relations_json(result).decode("utf-8")
        for forbidden in (
            "목적",
            "사업화를",
            "원하여",
            "occ:inspector",
            '"bbox',
            '"font',
            '"confidence',
            '"odl',
        ):
            self.assertNotIn(forbidden, encoded.lower())
        self.assertEqual(
            result,
            validate_pdf_primary_heading_relations_against_inputs(
                result, **arguments
            ),
        )

    def test_requires_unique_strict_header_and_applies_non_heading_veto(self) -> None:
        broad = self.artifact(
            [
                self.region(0, "section_header", (10.0, 20.0, 190.0, 130.0)),
                self.region(1, "list_group", (16.0, 132.0, 184.0, 160.0)),
            ]
        )
        self.assertEqual(
            build_pdf_primary_heading_relations(
                **self.arguments(artifact=broad)
            )["relations"],
            [],
        )

        competing = self.artifact(
            [
                self.region(0, "section_header", (18.0, 30.0, 82.0, 58.0)),
                self.region(1, "section_header", (17.0, 29.0, 83.0, 59.0)),
                self.region(2, "list_group", (16.0, 72.0, 184.0, 130.0)),
            ]
        )
        self.assertEqual(
            build_pdf_primary_heading_relations(
                **self.arguments(artifact=competing)
            )["relations"],
            [],
        )

        non_heading_overlap = self.artifact(
            [
                self.region(0, "section_header", (18.0, 30.0, 82.0, 58.0)),
                self.region(1, "text", (17.0, 29.0, 83.0, 59.0)),
                self.region(2, "list_group", (16.0, 72.0, 184.0, 130.0)),
            ]
        )
        self.assertEqual(
            build_pdf_primary_heading_relations(
                **self.arguments(artifact=non_heading_overlap)
            )["relations"],
            [],
        )

        diamond = self.region(
            0,
            "section_header",
            (18.0, 30.0, 82.0, 58.0),
            polygon=((50.0, 30.0), (82.0, 44.0), (50.0, 58.0), (18.0, 44.0)),
        )
        non_rectangular = self.artifact(
            [diamond, self.region(1, "list_group", (16.0, 72.0, 184.0, 130.0))]
        )
        self.assertEqual(
            build_pdf_primary_heading_relations(
                **self.arguments(artifact=non_rectangular)
            )["relations"],
            [],
        )

    def test_numbering_typography_gap_left_edge_and_immediacy_fail_closed(self) -> None:
        cases = (
            HeadingInspector(heading_text="목적"),
            HeadingInspector(heading_font_size=10.0),
            HeadingInspector(body_y=30.0, continuation_y=15.0),
            HeadingInspector(body_x=20.0),
            HeadingInspector(intervening=True),
        )
        for inspector in cases:
            with self.subTest(inspector=vars(inspector)):
                result = build_pdf_primary_heading_relations(
                    **self.arguments(inspector=inspector)
                )
                self.assertEqual(result["relations"], [])

    def test_full_raw_surya_and_exact_page_scope_are_mandatory(self) -> None:
        artifact = self.artifact()
        arguments = self.arguments(artifact=artifact)
        arguments["surya_layout_artifact"] = artifact
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationsError, "full raw artifact"
        ):
            build_pdf_primary_heading_relations(**arguments)

        arguments = self.arguments()
        arguments["expected_surya_pages"] = ()
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "page_scope"):
            build_pdf_primary_heading_relations(**arguments)

        arguments = self.arguments()
        arguments["expected_surya_logical_compute_key"] = "0" * 64
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "Surya"):
            build_pdf_primary_heading_relations(**arguments)

    def test_standalone_hash_claim_is_not_trusted_by_replay(self) -> None:
        arguments = self.arguments()
        result = build_pdf_primary_heading_relations(**arguments)
        tampered = copy.deepcopy(result)
        tampered["input_artifacts"]["surya_layout_artifact_sha256"] = "0" * 64
        relation = tampered["relations"][0]
        relation["relation_id"] = contract._relation_id(
            source_pdf_sha256=tampered["source_pdf_sha256"],
            policy_version=tampered["policy"]["policy_version"],
            primary_document_view_sha256=tampered["input_artifacts"][
                "primary_document_view_sha256"
            ],
            surya_layout_artifact_sha256=tampered["input_artifacts"][
                "surya_layout_artifact_sha256"
            ],
            kind=relation["kind"],
            page=relation["page"],
            heading_leaf_id=relation["heading_leaf_id"],
            body_leaf_id=relation["body_leaf_id"],
            surya_region_id=relation["surya_region_id"],
        )
        validate_pdf_primary_heading_relations(tampered)
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationsError, "deterministic replay"
        ):
            validate_pdf_primary_heading_relations_against_inputs(
                tampered, **arguments
            )

    def test_schema_runtime_canonical_parser_and_safe_loader(self) -> None:
        result = build_pdf_primary_heading_relations(**self.arguments())
        encoded = canonical_pdf_primary_heading_relations_json(result)
        parsed = parse_pdf_primary_heading_relations_bytes(encoded)
        self.assertEqual(parsed.to_dict(), result)
        self.assertEqual(parsed.canonical_json(), encoded)
        self.assertFalse(list(self.schema_validator.iter_errors(result)))

        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationsError, "canonical"
        ):
            parse_pdf_primary_heading_relations_bytes(encoded + b"\n")
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "duplicate"):
            parse_pdf_primary_heading_relations_bytes(
                b'{"schema_version":"x","schema_version":"y"}'
            )
        with self.assertRaises(PdfPrimaryHeadingRelationsError):
            parse_pdf_primary_heading_relations_bytes(
                b'{"oversized_integer":' + b"1" * 100 + b"}"
            )

        path = self.fixture.root / "heading-relations.json"
        path.write_bytes(encoded)
        self.assertEqual(load_pdf_primary_heading_relations_file(path).to_dict(), result)
        link = self.fixture.root / "heading-relations-link.json"
        link.symlink_to(path)
        with self.assertRaisesRegex(
            PdfPrimaryHeadingRelationsError, "non-symlink"
        ):
            load_pdf_primary_heading_relations_file(link)

    def test_validator_rejects_relation_mutations_and_enforces_caps(self) -> None:
        result = build_pdf_primary_heading_relations(**self.arguments())
        self.assertEqual(contract.MAX_RELATIONS, MAX_TOTAL_REGIONS)
        self.assertEqual(
            self.schema["properties"]["relations"]["maxItems"],
            MAX_TOTAL_REGIONS,
        )
        extra = copy.deepcopy(result)
        extra["relations"][0]["text"] = "forbidden"
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "keys"):
            validate_pdf_primary_heading_relations(extra)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(extra)

        wrong_id = copy.deepcopy(result)
        wrong_id["relations"][0]["relation_id"] = "heading-relation-" + "0" * 64
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "canonical"):
            validate_pdf_primary_heading_relations(wrong_id)

        wrong_policy = copy.deepcopy(result)
        wrong_policy["policy"]["policy_version"] = "other/v1"
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "policy"):
            validate_pdf_primary_heading_relations(wrong_policy)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(wrong_policy)

        wrong_region = copy.deepcopy(result)
        wrong_region["relations"][0]["surya_region_id"] = (
            "p0001-section_header-0002"
        )
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "canonical"):
            validate_pdf_primary_heading_relations(wrong_region)

        wrong_primary_view_binding = copy.deepcopy(result)
        wrong_primary_view_binding["input_artifacts"][
            "primary_document_view_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "canonical"):
            validate_pdf_primary_heading_relations(wrong_primary_view_binding)

        wrong_source_binding = copy.deepcopy(result)
        wrong_source_binding["source_pdf_sha256"] = "0" * 64
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "canonical"):
            validate_pdf_primary_heading_relations(wrong_source_binding)

        wrong_surya_binding = copy.deepcopy(result)
        wrong_surya_binding["input_artifacts"][
            "surya_layout_artifact_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "canonical"):
            validate_pdf_primary_heading_relations(wrong_surya_binding)

        malformed_region = copy.deepcopy(result)
        malformed_region["relations"][0]["surya_region_id"] = "region-1"
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "Surya"):
            validate_pdf_primary_heading_relations(malformed_region)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(malformed_region)

        reused_region = copy.deepcopy(result)
        reused = copy.deepcopy(reused_region["relations"][0])
        reused["heading_leaf_id"] = "leaf-" + "f" * 64
        reused["body_leaf_id"] = "leaf-" + "e" * 64
        reused["relation_id"] = contract._relation_id(
            source_pdf_sha256=reused_region["source_pdf_sha256"],
            policy_version=reused_region["policy"]["policy_version"],
            primary_document_view_sha256=reused_region["input_artifacts"][
                "primary_document_view_sha256"
            ],
            surya_layout_artifact_sha256=reused_region["input_artifacts"][
                "surya_layout_artifact_sha256"
            ],
            kind=reused["kind"],
            page=reused["page"],
            heading_leaf_id=reused["heading_leaf_id"],
            body_leaf_id=reused["body_leaf_id"],
            surya_region_id=reused["surya_region_id"],
        )
        reused_region["relations"].append(reused)
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "Surya region"):
            validate_pdf_primary_heading_relations(reused_region)

        natural_order = copy.deepcopy(result)
        natural_order["relations"] = []
        for ordinal, heading_digit, body_digit in (
            (1, "f", "e"),
            (2, "0", "1"),
        ):
            relation = {
                "kind": "heading_to_body",
                "page": 1,
                "heading_leaf_id": "leaf-" + heading_digit * 64,
                "body_leaf_id": "leaf-" + body_digit * 64,
                "surya_region_id": f"p0001-section_header-{ordinal:04d}",
            }
            relation["relation_id"] = contract._relation_id(
                source_pdf_sha256=natural_order["source_pdf_sha256"],
                policy_version=natural_order["policy"]["policy_version"],
                primary_document_view_sha256=natural_order["input_artifacts"][
                    "primary_document_view_sha256"
                ],
                surya_layout_artifact_sha256=natural_order["input_artifacts"][
                    "surya_layout_artifact_sha256"
                ],
                **relation,
            )
            natural_order["relations"].append(relation)
        self.assertEqual(
            validate_pdf_primary_heading_relations(natural_order), natural_order
        )
        reversed_order = copy.deepcopy(natural_order)
        reversed_order["relations"].reverse()
        with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "canonical order"):
            validate_pdf_primary_heading_relations(reversed_order)

        with patch.object(contract, "MAX_RELATIONS", 0):
            with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "bounded"):
                validate_pdf_primary_heading_relations(result)
        with patch.object(contract, "MAX_NATIVE_REGION_COMPARISONS", 0):
            with self.assertRaisesRegex(PdfPrimaryHeadingRelationsError, "comparison"):
                build_pdf_primary_heading_relations(**self.arguments())

    def test_builder_import_graph_has_no_gold_or_odl_contract(self) -> None:
        source = Path(contract.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertFalse(any("gold" in name for name in imported))
        self.assertFalse(any("opendataloader" in name for name in imported))
        self.assertNotIn("primary_structure_gold", source)

    def test_actual_114788_replay_when_artifact_environment_is_configured(
        self,
    ) -> None:
        names = {
            "source_pdf": "PRIMARY_DOCUMENT_VIEW_114788_SOURCE_PDF",
            "native_capture": "PRIMARY_DOCUMENT_VIEW_114788_NATIVE_CAPTURE",
            "structure_candidates": "PRIMARY_DOCUMENT_VIEW_114788_STRUCTURE_CANDIDATES",
            "render_manifest": "PRIMARY_DOCUMENT_VIEW_114788_RENDER_MANIFEST",
            "render_artifact_root": "PRIMARY_DOCUMENT_VIEW_114788_RENDER_ROOT",
            "reconstruction_plan": "PRIMARY_DOCUMENT_VIEW_114788_RECONSTRUCTION_PLAN",
            "calibration_proof": "PRIMARY_DOCUMENT_VIEW_114788_CALIBRATION_PROOF",
            "surya_layout_artifact": "PRIMARY_HEADING_RELATIONS_114788_SURYA_LAYOUT_ARTIFACT",
        }
        configured = {key: os.environ.get(name) for key, name in names.items()}
        if not all(configured.values()):
            self.skipTest("actual 114788 heading artifact environment is not configured")

        def mapping(name: str) -> dict:
            return json.loads(
                Path(configured[name]).read_text(encoding="utf-8")
            )

        surya = mapping("surya_layout_artifact")
        arguments = {
            "source_pdf": Path(configured["source_pdf"]),
            "native_capture": mapping("native_capture"),
            "structure_candidates": mapping("structure_candidates"),
            "render_manifest": mapping("render_manifest"),
            "render_artifact_root": Path(configured["render_artifact_root"]),
            "calibration_proof": mapping("calibration_proof"),
            "expected_calibration_proof_sha256": REVIEWED_PROOF_CANONICAL_SHA256,
            "reconstruction_plan": mapping("reconstruction_plan"),
            "surya_layout_artifact": surya,
            "expected_surya_producer": SuryaProducerIdentity.from_dict(
                surya["producer"]
            ),
            "expected_surya_logical_compute_key": surya["logical_compute_key"],
            "expected_surya_pages": surya["requested_pages"],
        }
        result = build_pdf_primary_heading_relations(**arguments)
        self.assertEqual(len(result["relations"]), 1)
        relation = result["relations"][0]
        self.assertEqual(relation["kind"], "heading_to_body")
        self.assertEqual(relation["surya_region_id"], "p0003-section_header-0002")

        primary = build_pdf_primary_document_view(
            **{
                key: arguments[key]
                for key in (
                    "source_pdf",
                    "native_capture",
                    "structure_candidates",
                    "render_manifest",
                    "render_artifact_root",
                    "calibration_proof",
                    "expected_calibration_proof_sha256",
                    "reconstruction_plan",
                )
            }
        )
        heading_occurrence = "occ:inspector:p3:t17"
        body_occurrences = ["occ:inspector:p3:t19", "occ:inspector:p3:t20"]
        heading_leaf = next(
            leaf
            for leaf in primary["leaves"]
            if leaf["occurrence_ids"] == [heading_occurrence]
        )
        body_leaf = next(
            leaf
            for leaf in primary["leaves"]
            if leaf["occurrence_ids"] == body_occurrences
        )
        self.assertEqual(relation["heading_leaf_id"], heading_leaf["leaf_id"])
        self.assertEqual(relation["body_leaf_id"], body_leaf["leaf_id"])
        replayed = validate_pdf_primary_heading_relations_against_inputs(
            result, **arguments
        )
        self.assertEqual(replayed, result)


if __name__ == "__main__":
    unittest.main()
