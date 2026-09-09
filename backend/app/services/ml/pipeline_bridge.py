"""backend ↔ ml/serving 경계.

`ml/serving/shared/ml_orchestrator.py` 는 backend 를 import 하지 않는다(의존
방향). 반대쪽 연결은 여기 한 곳에서만 한다 — analysis_pipeline 이 sys.path 를
만지거나 torch 를 아는 일이 없도록.

세 모델은 backend 의존성이 아니다
--------------------------------
`backend/requirements.txt` 에는 torch 도 transformers 도 없다. 배포에 따라
ml 쪽이 아예 설치되지 않을 수 있다. 그래서 import 실패를 오류로 다루지 않고
"이번 분석에서는 세 모델을 돌리지 않는다" 로 내려앉는다. 기존 CPL·FIT·검색은
그대로 끝난다.

Model 1 은 442MB 가중치를 올린다. 첫 호출에서만 올라가고 이후 프로세스가 살아
있는 동안 재사용된다(러너 쪽 캐시).
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from app.schemas.cpl import CplResult


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
_SERVING_SHARED = PROJECT_ROOT / "ml" / "serving" / "shared"

# import 를 매번 다시 시도하지 않는다. 실패는 대개 '설치되지 않음' 이라 분석
# 건마다 같은 예외를 다시 만들고 같은 경고를 다시 찍을 뿐이다.
_MODULES: dict[str, Any] | None = None
_IMPORT_FAILED = False


def _load() -> dict[str, Any] | None:
    global _MODULES, _IMPORT_FAILED
    if _MODULES is not None:
        return _MODULES
    if _IMPORT_FAILED:
        return None
    if not _SERVING_SHARED.is_dir():
        logger.warning("ML serving not found at %s; models skipped", _SERVING_SHARED)
        _IMPORT_FAILED = True
        return None
    path = str(_SERVING_SHARED)
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import ml_orchestrator
        import preconsultation_adapter
    except Exception as error:  # noqa: BLE001 - 설치 여부는 예외 종류로 갈리지 않는다
        logger.warning(
            "ML serving unavailable (%s); models skipped", type(error).__name__
        )
        _IMPORT_FAILED = True
        return None
    _MODULES = {"orchestrator": ml_orchestrator, "adapter": preconsultation_adapter}
    return _MODULES


def is_available() -> bool:
    return _load() is not None


async def run_models(
    *,
    case_id: int,
    cpl_result: CplResult,
    document_text: str,
    title: str | None = None,
    cohort: str | None = None,
) -> dict[str, Any] | None:
    """Model 1 → Model 2·3. 돌릴 수 없으면 None.

    SIM-R 은 여기서 부르지 않는다. backend 가 기존 `retrieve_top_five()` 를
    이어서 부르고, Model 1 결과는 아래 `routing_inputs()` 로 꺼내 넘긴다.
    """
    modules = _load()
    if modules is None:
        return None

    adapter = modules["adapter"]
    orchestrator = modules["orchestrator"]
    try:
        adapted = adapter.adapt(document_text, row_id=str(case_id))
    except Exception as error:  # noqa: BLE001
        # 어댑터가 죽어도 Model 1 은 CPL 만으로 돌아간다. 수치축이 없으면
        # Model 3 이 스스로 insufficient_data 를 낸다 — 여기서 중단하지 않는다.
        logger.warning(
            "Pre-consultation adapter failed for case %s: %s",
            case_id,
            type(error).__name__,
        )
        adapted = None

    structured_data = (adapted or {}).get("features") if adapted else None
    adapter_meta = adapted if adapted else None

    try:
        return await orchestrator.run_ml_pipeline_async(
            analysis_id=str(case_id),
            cpl_result=cpl_result,
            structured_data=structured_data,
            title=title,
            text=document_text,
            cohort=cohort,
            adapter_meta=adapter_meta,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "ML pipeline failed for case %s: %s", case_id, type(error).__name__
        )
        return None


def routing_inputs(ml_results: dict[str, Any] | None) -> tuple[str | None, str]:
    """Model 1 결과 → SIM-R 라우팅 입력 `(support_type, trust_grade)`.

    Model 1 이 없거나 실패했으면 `(None, "hold")` 다. 좁히지 않는 쪽이 기본이며,
    라벨이 없는데 있는 척하는 것보다 넓게 찾는 편이 안전하다.
    """
    if not ml_results:
        return None, "hold"
    envelope = ml_results.get("model_1") or {}
    if envelope.get("status") != "success":
        return None, "hold"
    result = envelope.get("result") or {}
    support_type = result.get("support_type")
    trust_grade = result.get("trust_grade") or "hold"
    if not isinstance(support_type, str) or not support_type.strip():
        return None, "hold"
    return support_type, trust_grade
