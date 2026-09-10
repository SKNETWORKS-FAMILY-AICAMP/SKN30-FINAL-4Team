"""Legacy Structured Outputs contract experiment.

It does not use source-selection and must not be used as a production runner.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import OpenAI

from semantic_structuring.models import CandidatePack
from semantic_structuring.pipeline import result_json, run_candidate_pack


def stage_pack() -> CandidatePack:
    return CandidatePack.model_validate(
        {
            "pack_id": "125612-stage-support",
            "notice_id": "PBLN_000000000125612",
            "question": "교육 참여자 20개팀과 최종 지원 대상자 4개팀의 지원을 분리하고, 가점을 올바른 범위에 둔다.",
            "blocks": [
                {"block_id": "body[11]", "relation": "preceding_context", "text": "대상: 동구 내 창업을 희망하는 청년 예비 창업자(19~39세)"},
                {"block_id": "body[12]", "relation": "candidate", "text": "동구 거주자 가산점 부여"},
                {"block_id": "body[13]", "relation": "competing_candidate", "text": "선정규모: 4개팀. 1차 서류심사: 20개팀 정도"},
                {"block_id": "body[15]", "relation": "candidate", "text": "창업기초 및 실무교육 지원"},
                {"block_id": "body[16]", "relation": "candidate", "text": "최종 선정 시 창업지원금 각 300만원 및 멘토링 지원"},
                {"block_id": "body[44]", "relation": "candidate", "text": "1차 서류심사: 교육 참여자 20개팀 내외 선정"},
                {"block_id": "body[45]", "relation": "following_context", "text": "창업교육(1차 서류심사 합격자): 창업기초 및 실무교육 4회 실시"},
                {"block_id": "body[49]", "relation": "candidate", "text": "2차 발표심사: 최종 지원 대상자 4개팀 선정"},
                {"block_id": "body[51]", "relation": "candidate", "text": "가점항목: 동구 거주 여부, 동구 청년 창업 지원사업 참여 이력"},
            ],
        }
    )


def open_innovation_pack() -> CandidatePack:
    return CandidatePack.model_validate({"pack_id":"125002-scale-roles","notice_id":"PBLN_000000000125002","question":"3개 파트너와 최종 6개 스타트업을 구분한다.","blocks":[
        {"block_id":"body[21]","relation":"competing_candidate","text":"선정규모 : 3社 내외"},
        {"block_id":"body[25]","relation":"candidate","text":"지원내용 : 대·중견기업 사업 부서 밋업 연계, 사업화(PoC) 지원금, 투자 연계 검토 등"},
        {"block_id":"body[27]","relation":"candidate","text":"사업화(PoC) 지원금 : 1개社 당 최대 35,000천원 내외"},
        {"block_id":"body[36]","relation":"candidate","text":"최종기업 선정: 6개사 스타트업 선정 및 수요기업-스타트업 간 협약"},
        {"block_id":"body[39]","relation":"candidate","text":"참여 파트너기업: SK에코플랜트, LS일렉트릭, HS효성/효성 총 3개사"},
    ]})


def open_innovation_scale_pack() -> CandidatePack:
    return CandidatePack.model_validate({"pack_id":"125002-final-startup-selection","notice_id":"PBLN_000000000125002","question":"최종기업 선정 단계에서 선정되는 스타트업의 대상과 선정 규모를 확인한다.","blocks":[
        {"block_id":"body[36]","relation":"candidate","text":"최종기업 선정: 6개사 스타트업 선정 및 수요기업-스타트업 간 협약"},
    ]})


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-legacy-experiment", action="store_true")
    args = parser.parse_args()
    if not args.allow_legacy_experiment:
        raise SystemExit("Legacy experiment disabled. Pass --allow-legacy-experiment only for isolated contract testing.")
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set; add it to .env without printing it.")

    selection = os.environ.get("TEST_NOTICE")
    pack = open_innovation_scale_pack() if selection == "125002-scale" else open_innovation_pack() if selection == "125002" else stage_pack()
    client = OpenAI()
    print(result_json(run_candidate_pack(client, pack)))


if __name__ == "__main__":
    main()
