from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import sys
from typing import Any, Mapping

import pytest

from worker.contracts.ml_result import MlModelId
from worker.existing_model1 import (
    ExistingModel1Backfill,
    ExistingModel1Prediction,
    ExistingModel1Profile,
    assemble_existing_model1_input,
    current_model1_input_hashes,
    normalize_existing_model1_prediction,
)
from scripts.classify_existing_model1 import (
    BACKEND_RUNTIME_MANIFEST_PATHS,
    PostgresExistingModel1Repository,
    _configured_ml_python,
    _model1_serving_root,
    _verify_model1_artifact,
    _verify_model1_runtime,
    _runtime_manifest_sha256,
)


def _profile(profile_id: str = "profile-1") -> ExistingModel1Profile:
    # Deliberately unordered fields: persisted ordinal, not dict/list encounter
    # order, is the exact Existing evidence order sent to Model 1.
    return ExistingModel1Profile(
        profile_version_id=profile_id,
        portal_metadata={"title": "  지역   성장 사업  "},
        facts=[
            {"field_name": "support_content", "value_raw": "후순위 내용", "status": "identified", "ordinal": 30},
            {"field_name": "purpose_goal", "value_raw": "기업 성장", "status": "identified", "ordinal": 10},
            {"field_name": "support_activities", "value_raw": "컨설팅", "status": "partial", "ordinal": 20},
            {"field_name": "support_target", "value_raw": "중소기업", "status": "identified", "ordinal": 40},
            {"field_name": "exclusions", "value_raw": "중복수혜 제외", "status": "identified", "ordinal": 50},
            {"field_name": "support_methods", "value_raw": "무시", "status": "not_identified", "ordinal": 15},
            # Component-scoped facts are stored in the same table and must be
            # included without a separate component traversal.
            {"field_name": "support_items", "value_raw": "시제품", "status": "identified", "ordinal": 25},
        ],
    )


def test_existing_model1_input_uses_only_approved_facts_in_frozen_field_order() -> None:
    assembled = assemble_existing_model1_input(_profile())

    assert assembled.payload == {
        "title": "지역 성장 사업",
        "purpose": "기업 성장",
        "content": "컨설팅\n시제품\n후순위 내용",
        "target_text": "중소기업\n중복수혜 제외",
    }
    assert assembled.missing_fields == ()
    assert assembled.input_sha256 == assemble_existing_model1_input(_profile()).input_sha256


def test_existing_model1_input_hash_distinguishes_field_boundaries() -> None:
    first = assemble_existing_model1_input(
        ExistingModel1Profile(
            profile_version_id="one",
            portal_metadata={"title": "A"},
            facts=[{"field_name": "purpose_goal", "value_raw": "B\nC", "status": "identified", "ordinal": 0}],
        )
    )
    second = assemble_existing_model1_input(
        ExistingModel1Profile(
            profile_version_id="two",
            portal_metadata={"title": "A\nB"},
            facts=[{"field_name": "purpose_goal", "value_raw": "C", "status": "identified", "ordinal": 0}],
        )
    )
    assert first.input_sha256 != second.input_sha256


def test_withheld_prediction_retains_raw_label_but_has_no_effective_label() -> None:
    prediction = normalize_existing_model1_prediction(
        {"support_type_pred": "판로", "confidence": 0.19, "status": "판단보류"}
    )

    assert prediction.support_type_pred == "판로"
    assert prediction.effective_support_type is None


class _FakeModel:
    model_id = MlModelId.MODEL_1_SUPPORT_TYPE
    artifact_version = "a" * 64

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(inputs))
        return {"support_type_pred": "판로", "confidence": 0.91, "status": "신뢰"}


class _FakeRepository:
    def __init__(self, profiles: list[ExistingModel1Profile]) -> None:
        self.profiles = profiles
        self.results: dict[tuple[str, str, str], ExistingModel1Prediction] = {}
        self.invocations: list[dict[str, Any]] = []
        self.finished: list[tuple[str, bool, str | None]] = []
        self.promotions: list[dict[str, Any]] = []

    def current_profiles(self) -> list[ExistingModel1Profile]:
        return list(self.profiles)

    def has_result(self, *, profile_version_id: str, configuration_id: str, input_sha256: str) -> bool:
        return (profile_version_id, configuration_id, input_sha256) in self.results

    def start_run(self, *, configuration_id: str, profile_count: int) -> str:
        assert configuration_id == "config-1"
        assert profile_count == len(self.profiles)
        return "run-1"

    def record_invocation(self, **kwargs: Any) -> None:
        self.invocations.append(kwargs)

    def write_result(self, **kwargs: Any) -> None:
        self.results[(kwargs["profile_version_id"], kwargs["configuration_id"], kwargs["input_sha256"])] = kwargs["prediction"]

    def write_failure(self, **kwargs: Any) -> None:
        self.results[(kwargs["profile_version_id"], kwargs["configuration_id"], kwargs["input_sha256"])] = kwargs["reason_code"]

    def verify_and_promote(self, **kwargs: Any) -> None:
        expected = kwargs["expected_inputs"]
        assert len(expected) == len(self.profiles)
        assert all((profile_id, kwargs["configuration_id"], digest) in self.results for profile_id, digest in expected.items())
        self.promotions.append(kwargs)

    def finish_run(self, *, processing_run_id: str, succeeded: bool, error_code: str | None = None) -> None:
        self.finished.append((processing_run_id, succeeded, error_code))


class _RepositorySqlError(RuntimeError):
    pass


class _ScriptedPostgresCursor:
    def __init__(self, connection: "_ScriptedPostgresConnection") -> None:
        self._connection = connection
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> "_ScriptedPostgresCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, parameters: object = None) -> None:
        sql = " ".join(query.split())
        if self._connection.aborted:
            raise RuntimeError("current transaction is aborted")
        self._connection.statements.append((sql, parameters))
        self._rows = []
        if "FROM kb.profile_version AS profile" in sql:
            profile = _profile()
            self._rows = [
                {
                    "profile_version_pk": profile.profile_version_id,
                    "portal_metadata": profile.portal_metadata,
                    "facts": list(profile.facts),
                }
            ]
        elif "INSERT INTO ops.processing_run" in sql:
            self._rows = [{"processing_run_pk": "run-1"}]
        elif (
            "SELECT 1 FROM retrieval.existing_profile_classification" in sql
            and self._connection.fail_has_result
        ):
            self._connection.aborted = True
            raise self._connection.original_error
        elif "INSERT INTO ops.model_invocation" in sql and self._connection.fail_invocation:
            self._connection.aborted = True
            raise self._connection.original_error
        elif "UPDATE ops.processing_run" in sql and self._connection.fail_finish:
            self._connection.aborted = True
            raise ConnectionError("database connection dropped")
        elif "FROM retrieval.classification_configuration" in sql:
            self._rows = [
                {
                    "classification_config_pk": "config-1",
                    "model_id": "model_1_support_type",
                    "artifact_sha256": "a" * 64,
                    "runtime_manifest_sha256": "b" * 64,
                    "input_assembly_version": "existing-profile-model1-input-v1",
                    "is_active": False,
                }
            ]
        elif "FROM retrieval.existing_profile_classification AS classification" in sql:
            self._rows = [
                {
                    "profile_version_pk": "profile-1",
                    "input_sha256": assemble_existing_model1_input(_profile()).input_sha256,
                    "support_type_pred": "판로",
                    "confidence": 0.91,
                    "prediction_status": "신뢰",
                    "execution_status": "OK",
                }
            ]
        elif "SELECT retrieval.promote_classification_configuration" in sql:
            self._rows = [{"promote_classification_configuration": None}]

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _ScriptedPostgresConnection:
    """Minimal psycopg fake that enforces PostgreSQL's aborted state."""

    def __init__(
        self,
        *,
        fail_invocation: bool = True,
        fail_has_result: bool = False,
        fail_finish: bool = False,
        fail_rollback: bool = False,
    ) -> None:
        self.original_error = _RepositorySqlError("repository SQL failed")
        self.fail_invocation = fail_invocation
        self.fail_has_result = fail_has_result
        self.fail_finish = fail_finish
        self.fail_rollback = fail_rollback
        self.aborted = False
        self.commits = 0
        self.rollbacks = 0
        self.statements: list[tuple[str, object]] = []

    def cursor(self) -> _ScriptedPostgresCursor:
        return _ScriptedPostgresCursor(self)

    def commit(self) -> None:
        if self.aborted:
            raise RuntimeError("cannot commit an aborted transaction")
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.fail_rollback:
            raise ConnectionError("postgresql://secret-user:secret-password@host/db")
        self.aborted = False


def test_backfill_is_idempotent_and_promotes_only_after_complete_verification() -> None:
    repository = _FakeRepository([_profile("profile-1"), _profile("profile-2")])
    model = _FakeModel()
    backfill = ExistingModel1Backfill(repository, model, configuration_id="config-1")

    first = backfill.run()
    second = backfill.run()

    assert (first.predicted, first.skipped, first.promoted) == (2, 0, True)
    assert (second.predicted, second.skipped, second.promoted) == (0, 2, True)
    assert len(model.calls) == 2
    assert [item["status"] for item in repository.invocations] == ["succeeded", "succeeded"]
    assert repository.finished == [("run-1", True, None), ("run-1", True, None)]


def test_dry_run_neither_calls_model_nor_writes() -> None:
    repository = _FakeRepository([_profile()])
    model = _FakeModel()

    result = ExistingModel1Backfill(repository, model, configuration_id="config-1").run(dry_run=True)

    assert result.dry_run is True
    assert model.calls == []
    assert repository.results == {}
    assert repository.promotions == []


def test_backfill_rejects_an_empty_current_corpus() -> None:
    with pytest.raises(ValueError, match="empty"):
        ExistingModel1Backfill(_FakeRepository([]), _FakeModel(), configuration_id="config-1").run()


def test_failed_model_call_is_audited_and_persisted_without_prediction() -> None:
    class _BrokenModel(_FakeModel):
        def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("serving unavailable")

    repository = _FakeRepository([_profile()])
    with pytest.raises(RuntimeError, match="serving unavailable"):
        ExistingModel1Backfill(repository, _BrokenModel(), configuration_id="config-1").run()

    assert repository.invocations[0]["status"] == "failed"
    assert list(repository.results.values()) == ["MODEL_EXECUTION_FAILED"]
    assert repository.finished == [("run-1", False, "RuntimeError")]


def test_postgres_mutation_error_is_preserved_and_running_audit_is_finished() -> None:
    connection = _ScriptedPostgresConnection()
    repository = PostgresExistingModel1Repository(
        connection,
        configuration={"model_id": "model_1_support_type", "artifact_sha256": "a" * 64},
    )

    with pytest.raises(_RepositorySqlError) as raised:
        ExistingModel1Backfill(repository, _FakeModel(), configuration_id="config-1").run()

    assert raised.value is connection.original_error
    assert connection.rollbacks == 1
    assert connection.commits == 2  # durable start_run, then durable failed finish_run
    assert connection.aborted is False
    finish_parameters = [
        parameters
        for sql, parameters in connection.statements
        if "UPDATE ops.processing_run" in sql
    ]
    assert finish_parameters == [
        ("failed", "_RepositorySqlError", "_RepositorySqlError", "run-1")
    ]
    finish_queries = [
        sql for sql, _ in connection.statements if "UPDATE ops.processing_run" in sql
    ]
    assert finish_queries and "WHEN %s::TEXT IS NULL" in finish_queries[0]


def test_postgres_read_error_is_rolled_back_before_running_audit_is_finished() -> None:
    connection = _ScriptedPostgresConnection(
        fail_invocation=False,
        fail_has_result=True,
    )
    repository = PostgresExistingModel1Repository(
        connection,
        configuration={"model_id": "model_1_support_type", "artifact_sha256": "a" * 64},
    )

    with pytest.raises(_RepositorySqlError) as raised:
        ExistingModel1Backfill(repository, _FakeModel(), configuration_id="config-1").run()

    assert raised.value is connection.original_error
    assert connection.rollbacks == 1
    assert connection.commits == 2
    assert connection.aborted is False
    assert any("UPDATE ops.processing_run" in sql for sql, _ in connection.statements)


def test_finish_failure_adds_safe_note_without_masking_original_error() -> None:
    class _BrokenFinishRepository(_FakeRepository):
        def __init__(self) -> None:
            super().__init__([_profile()])
            self.original_error = RuntimeError("primary work failed")

        def has_result(self, **_: Any) -> bool:
            raise self.original_error

        def finish_run(self, **_: Any) -> None:
            raise ConnectionError("postgresql://secret-user:secret-password@host/db")

    repository = _BrokenFinishRepository()

    with pytest.raises(RuntimeError) as raised:
        ExistingModel1Backfill(repository, _FakeModel(), configuration_id="config-1").run()

    assert raised.value is repository.original_error
    assert raised.value.__notes__ == [
        "processing run failure finalization also failed: ConnectionError"
    ]
    assert "secret-password" not in raised.value.__notes__[0]


def test_rollback_and_failure_finalization_cannot_mask_primary_sql_error() -> None:
    connection = _ScriptedPostgresConnection(fail_rollback=True)
    repository = PostgresExistingModel1Repository(
        connection,
        configuration={"model_id": "model_1_support_type", "artifact_sha256": "a" * 64},
    )

    with pytest.raises(_RepositorySqlError) as raised:
        ExistingModel1Backfill(repository, _FakeModel(), configuration_id="config-1").run()

    assert raised.value is connection.original_error
    assert raised.value.__notes__ == [
        "database rollback also failed: ConnectionError",
        "processing run failure finalization also failed: RuntimeError",
    ]
    assert all("secret-password" not in note for note in raised.value.__notes__)


def test_postgres_promotion_uses_the_database_promotion_gate() -> None:
    connection = _ScriptedPostgresConnection(fail_invocation=False)
    repository = PostgresExistingModel1Repository(
        connection,
        configuration={"model_id": "model_1_support_type", "artifact_sha256": "a" * 64},
    )
    expected = {"profile-1": assemble_existing_model1_input(_profile()).input_sha256}

    repository.verify_and_promote(
        configuration_id="config-1",
        expected_inputs=expected,
        processing_run_id="run-1",
    )

    promotion_calls = [
        parameters
        for sql, parameters in connection.statements
        if "SELECT retrieval.promote_classification_configuration" in sql
    ]
    direct_activation = [
        sql
        for sql, _ in connection.statements
        if sql.startswith("UPDATE retrieval.classification_configuration")
    ]
    assert promotion_calls == [("config-1",)]
    assert direct_activation == []
    verification_queries = [
        sql
        for sql, _ in connection.statements
        if "FROM retrieval.existing_profile_classification AS classification" in sql
    ]
    assert len(verification_queries) == 1
    assert "SELECT classification.profile_version_pk" in verification_queries[0]
    assert "lower(classification.input_sha256)" in verification_queries[0]
    assert connection.commits == 1
    assert connection.rollbacks == 0


def _fake_serving_root(tmp_path: Path, *, parent_layout: bool) -> tuple[Path, bytes]:
    root = tmp_path / "serving"
    model_root = root / "model1" if parent_layout else root
    (model_root / "model").mkdir(parents=True)
    (model_root / "inference.py").write_text("# test wrapper\n", encoding="utf-8")
    payload = b"not a real safetensors file; never loaded"
    (model_root / "model" / "model.safetensors").write_bytes(payload)
    return root, payload


@pytest.mark.parametrize("parent_layout", [False, True])
def test_runtime_verification_accepts_worker_model1_mount_layouts(
    tmp_path: Path, parent_layout: bool
) -> None:
    configured, payload = _fake_serving_root(tmp_path, parent_layout=parent_layout)
    env = {"PREREVIEW_MODEL1_SERVING_DIR": str(configured)}
    config = {"artifact_sha256": sha256(payload).hexdigest()}

    root, actual = _verify_model1_artifact(config, env)

    assert root == (configured / "model1" if parent_layout else configured)
    assert actual == config["artifact_sha256"]
    assert _model1_serving_root(env) == root


def test_runtime_verification_rejects_weight_digest_mismatch_before_inference(
    tmp_path: Path,
) -> None:
    configured, _ = _fake_serving_root(tmp_path, parent_layout=False)
    with pytest.raises(RuntimeError, match="SHA-256"):
        _verify_model1_artifact(
            {"artifact_sha256": "0" * 64},
            {"PREREVIEW_MODEL1_SERVING_DIR": str(configured)},
        )


def _fake_runtime_tree(tmp_path: Path) -> tuple[dict[str, str], dict[str, str], Path]:
    serving_parent, weight = _fake_serving_root(tmp_path, parent_layout=True)
    serving_root = serving_parent / "model1"
    (serving_root / "label_mapping.json").write_text("{}", encoding="utf-8")
    (serving_root / "model" / "config.json").write_text("{}", encoding="utf-8")
    (serving_root / "tokenizer").mkdir()
    (serving_root / "tokenizer" / "tokenizer.json").write_text("{}", encoding="utf-8")
    (serving_root / "tokenizer" / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    ml_root = tmp_path / "ml"
    (ml_root / "pipelines" / "model1").mkdir(parents=True)
    (ml_root / "pipelines" / "model1" / "dl07_m1_apply.py").write_text(
        "# preprocessing\n", encoding="utf-8"
    )
    (ml_root / "serving").mkdir()
    (ml_root / "serving" / "requirements.txt").write_text("torch\n", encoding="utf-8")
    env = {
        "PREREVIEW_MODEL1_SERVING_DIR": str(serving_parent),
        "PREREVIEW_ML_ROOT": str(ml_root),
    }
    config = {
        "artifact_sha256": sha256(weight).hexdigest(),
        "runtime_manifest_sha256": _runtime_manifest_sha256(serving_root, ml_root),
    }
    return env, config, ml_root


def test_runtime_manifest_verification_covers_serving_and_pipeline_bytes(tmp_path: Path) -> None:
    env, config, ml_root = _fake_runtime_tree(tmp_path)
    root, _, manifest = _verify_model1_runtime(config, env)

    assert root == Path(env["PREREVIEW_MODEL1_SERVING_DIR"]) / "model1"
    assert manifest == config["runtime_manifest_sha256"]

    (ml_root / "pipelines" / "model1" / "dl07_m1_apply.py").write_text(
        "# changed preprocessing\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="runtime manifest SHA-256"):
        _verify_model1_runtime(config, env)


def test_runtime_manifest_hash_covers_backend_assembler_and_adapter_bytes(
    tmp_path: Path,
) -> None:
    _, _, ml_root = _fake_runtime_tree(tmp_path)
    serving_root = tmp_path / "serving" / "model1"
    backend_root = tmp_path / "backend"
    for logical_name, relative_path in BACKEND_RUNTIME_MANIFEST_PATHS.items():
        path = backend_root.joinpath(*relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {logical_name}\n", encoding="utf-8")

    initial = _runtime_manifest_sha256(
        serving_root, ml_root, backend_root=backend_root
    )
    (backend_root / "worker" / "adapters" / "ml_subprocess.py").write_text(
        "# changed output normalizer\n", encoding="utf-8"
    )

    assert _runtime_manifest_sha256(
        serving_root, ml_root, backend_root=backend_root
    ) != initial


def test_configured_ml_python_uses_verified_external_interpreter() -> None:
    assert _configured_ml_python({"PREREVIEW_ML_PYTHON_EXECUTABLE": sys.executable}) == sys.executable
    with pytest.raises(RuntimeError, match="PREREVIEW_ML_PYTHON_EXECUTABLE"):
        _configured_ml_python({"PREREVIEW_ML_PYTHON_EXECUTABLE": "/missing/ml-python"})


class _PromotionGuardRepository(_FakeRepository):
    """A fake for the script's lock-time current-input reassembly guard."""

    def verify_and_promote(self, **kwargs: Any) -> None:
        if current_model1_input_hashes(self.profiles) != kwargs["expected_inputs"]:
            raise RuntimeError("current Existing Model 1 inputs changed during backfill")
        super().verify_and_promote(**kwargs)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda profile: replace(profile, portal_metadata={"title": "바뀐 공고 제목"}),
        lambda profile: replace(
            profile,
            facts=[
                {
                    **fact,
                    "value_raw": "바뀐 사업 목적",
                }
                if fact["field_name"] == "purpose_goal"
                else fact
                for fact in profile.facts
            ],
        ),
    ],
    ids=("notice_title", "approved_fact"),
)
def test_changed_title_or_fact_cannot_promote_stale_model1_results(mutate: Any) -> None:
    repository = _PromotionGuardRepository([_profile()])

    class _MutatingModel(_FakeModel):
        def predict(self, inputs: dict[str, Any]) -> dict[str, Any]:
            result = super().predict(inputs)
            repository.profiles = [mutate(repository.profiles[0])]
            return result

    with pytest.raises(RuntimeError, match="inputs changed"):
        ExistingModel1Backfill(repository, _MutatingModel(), configuration_id="config-1").run()

    assert repository.promotions == []
    assert repository.finished == [("run-1", False, "RuntimeError")]


def test_migration_contract_invalidates_only_current_notice_title_changes() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "supabase"
        / "migrations"
        / "32_existing_profile_model1_classification_hardening.sql"
    ).read_text(encoding="utf-8")

    assert "TG_TABLE_NAME = 'notice'" in migration
    assert "OLD.portal_metadata ->> 'title'" in migration
    assert "trg_kb_notice_classification_title_activation" in migration
    assert "BEFORE UPDATE OF portal_metadata ON kb.notice" in migration
    assert "ADD COLUMN IF NOT EXISTS runtime_manifest_sha256 TEXT" in migration
    assert "ALTER COLUMN runtime_manifest_sha256 SET NOT NULL" in migration
    assert "uq_retrieval_classification_configuration_identity" in migration
    assert "CLASSIFICATION_CONFIGURATION_EMPTY_CURRENT_CORPUS" in migration
    assert "CLASSIFICATION_CONFIGURATION_IDENTITY_IMMUTABLE" in migration
    assert "GRANT USAGE ON SCHEMA retrieval TO service_role" in migration
