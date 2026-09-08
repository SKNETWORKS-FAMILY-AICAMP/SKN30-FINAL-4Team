"""실제 DB 조회 Loader — Supabase PoC 스키마.

Intent 별로 읽는 곳이 다르다. 전부 `model_result` 에서 읽는 것이 아니다.

    MODEL_1/2/3  result.model_result                      (analysis_case_pk, model_name)
    REPORT       result.axis_result + result.sim_candidate
    DOCUMENT     result.analysis_case.input_profile_snapshot
                 + result.evidence_snapshot

쿼리를 한 곳에 모은 이유
----------------------
supabase-py 의 체인 호출(`client.schema(...).table(...).select(...)`)을 메서드마다
흩어 쓰면 테스트에서 그 체인을 통째로 흉내 내야 한다. 실제 호출은 `_fetch_one`
/`_fetch_many` 둘로 모으고 나머지는 그 위에 얹는다 — 테스트는 이 둘만 바꿔
끼우면 되고, 라이브러리가 바뀌어도 고칠 곳이 둘뿐이다.

이 파일은 supabase 패키지를 import 하지 않는다. 클라이언트는 주입받는다.
"""
from typing import Any, Dict, List, Optional

from .model_result import INTENT_TO_MODEL_NAME, row_to_context

SCHEMA = "result"


class SupabaseChatContextLoader:
    """`ChatContextLoader` 계약 구현. client 는 supabase-py 클라이언트."""

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------ 실제 쿼리
    def _fetch_one(self, table: str, filters: Dict[str, Any],
                   columns: str = "*") -> Optional[Dict[str, Any]]:
        q = self.client.schema(SCHEMA).table(table).select(columns)
        for k, v in filters.items():
            q = q.eq(k, v)
        res = q.limit(1).execute()
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    def _fetch_many(self, table: str, filters: Dict[str, Any],
                    columns: str = "*") -> List[Dict[str, Any]]:
        q = self.client.schema(SCHEMA).table(table).select(columns)
        for k, v in filters.items():
            q = q.eq(k, v)
        res = q.execute()
        return list(getattr(res, "data", None) or [])

    # ------------------------------------------------------------ 진입점
    def get_context(self, analysis_case_id: str, intent: str,
                    question: str) -> Dict[str, Any]:
        if intent in INTENT_TO_MODEL_NAME:
            return self._model_context(analysis_case_id, intent)
        if intent == "REPORT":
            return self._report_context(analysis_case_id)
        if intent == "DOCUMENT":
            return self._document_context(analysis_case_id)
        return _empty()                      # UNKNOWN 등 조회 대상 없음

    # ------------------------------------------------------------ MODEL_1/2/3
    def _model_context(self, case_id: str, intent: str) -> Dict[str, Any]:
        row = self._fetch_one(
            "model_result",
            {"analysis_case_pk": case_id,
             "model_name": INTENT_TO_MODEL_NAME[intent]},
            columns="model_name,status,result_data,metadata,error")
        # DB status 를 그대로 승계한다(success/failed/not_available/
        # insufficient_data). 매핑 규칙은 model_result.row_to_context 에 있다.
        return row_to_context(row)

    # ------------------------------------------------------------ REPORT
    def _report_context(self, case_id: str) -> Dict[str, Any]:
        axes = self._fetch_many(
            "axis_result", {"analysis_case_pk": case_id},
            columns="axis_code,axis_status,summary_text,result_data")
        sims = self._fetch_many(
            "sim_candidate", {"analysis_case_pk": case_id},
            columns=("announcement_title,issuing_organization,source_url,"
                     "summary_text,comparable_axes"))
        if not axes and not sims:
            return _empty()
        return {
            "status": "success",
            "data": {"axis_results": axes, "similar_programs": sims},
            "evidence": [],
        }

    # ------------------------------------------------------------ DOCUMENT
    def _document_context(self, case_id: str) -> Dict[str, Any]:
        case = self._fetch_one(
            "analysis_case", {"analysis_case_pk": case_id},
            columns=("case_status,program_name,original_filename,"
                     "input_profile_snapshot"))
        if case is None:
            return _empty()
        # 결과가 준비되지 않았으면 근거가 아직 없다 — '없음' 과 구분한다.
        if case.get("case_status") != "ready":
            return {"status": "insufficient_data", "data": None,
                    "evidence": [], "source_status": case.get("case_status")}

        snaps = self._fetch_many(
            "evidence_snapshot", {"analysis_case_pk": case_id},
            columns="evidence_snapshot_pk,field_code,snippet_text")
        return {
            "status": "success",
            "data": {
                "program_name": case.get("program_name"),
                "original_filename": case.get("original_filename"),
                "request_profile": case.get("input_profile_snapshot"),
            },
            "evidence": [
                {"source": "evidence_snapshot",
                 "field": s.get("field_code"),
                 "value": s.get("snippet_text"),
                 "evidence_snapshot_id": s.get("evidence_snapshot_pk")}
                for s in snaps
            ],
        }


def _empty(status: str = "not_available") -> Dict[str, Any]:
    return {"status": status, "data": None, "evidence": []}
