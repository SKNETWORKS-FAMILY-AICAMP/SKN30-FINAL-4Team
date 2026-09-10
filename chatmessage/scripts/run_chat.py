"""챗봇 단독 실행 — FastAPI 도 DB 도 띄우지 않고 질문 하나를 넣어 답을 본다.

CPL·FIT·SIM·ML 에는 각자 실행 스크립트가 있는데 챗봇에만 없었다. 그래서 지금까지
챗봇을 확인하는 방법이 "테스트를 돌린다(LLM 안 부름)" 아니면 "서버 전체를
띄운다" 둘뿐이었다.

여기서 돌아가는 것은 **하나의 Chat LLM** 이다. 분석 Agent 를 다시 실행하지
않는다 — 입력 JSON 에 이미 들어 있는 분석 결과를 읽을 뿐이다.

    # fixture 로
    python chatmessage/scripts/run_chat.py \
      --fixture chatmessage/fixtures/sample_full_context.json \
      --message "확인이 필요한 부분을 알려줘"

    # 실제 분석 결과 JSON 으로
    python chatmessage/scripts/run_chat.py \
      --input-json path/to/report.json \
      --message "전체 리포트를 5줄로 요약해줘"

    # 후속 질문 scope 상속 확인 — --message 를 여러 번 준다
    python chatmessage/scripts/run_chat.py --fixture ... \
      --message "비슷한 기존 사업은 뭐야?" --message "왜 그렇게 판단했어?"

`OPENAI_API_KEY` 가 없으면 LLM 을 부르지 않고 Context 까지만 보여 준다 — 라우팅과
비노출 정책만 확인할 때는 키가 필요 없다.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chatmessage import build_chat_context, check_grounding  # noqa: E402
from chatmessage.loader import DictLoader, JsonFileLoader, LoaderError, describe  # noqa: E402
from chatmessage.llm import LLMError, OpenAIChatClient  # noqa: E402
from chatmessage.prompt import PROMPT_VERSION  # noqa: E402

DEFAULT_RUN_DIR = _ROOT / "chatmessage" / ".runs"

# 챗봇 첫 화면에 쓰기로 한 예시 질문. 화면은 프론트가 그리지만, 이 질문들이
# 실제로 필요한 결과 섹션을 고르는지는 여기서 확인한다.
#
# **내부 이름(CPL·FIT·SIM)을 쓰지 않는다.** 사용자는 그 이름을 모른다. 업무
# 자연어만으로 라우팅이 되는지가 이 세트로 확인하려는 것이다.
SAMPLE_QUESTIONS = (
    "확인이 필요한 부분을 알려줘",
    "내용이 서로 맞지 않는 부분이 있는지 봐줘",
    "비슷한 기존 사업과 어떤 차이가 있는지 알려줘",
    "전체 리포트를 5줄로 요약해줘",
    "수정이 필요한 부분을 제안해줘",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_chat.py",
        description="분석 결과 JSON 하나로 챗봇을 돌려 본다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--fixture",
        help="chatmessage/fixtures 의 검증용 입력.",
    )
    source.add_argument(
        "--input-json",
        help="실제 분석 결과 JSON. fixture 와 같은 입력 계약이다.",
    )
    parser.add_argument(
        "--message",
        action="append",
        metavar="질문",
        help="질문. 여러 번 주면 한 대화로 이어져 후속 질문 scope 상속을 볼 수 있다.",
    )
    parser.add_argument(
        "--sample-questions",
        action="store_true",
        help=(
            "초기 화면 예시 질문 5개를 차례로 돌린다(각각 새 대화). "
            "복수 --message 와 목적이 다르다 — 이쪽은 독립 질문 회귀, "
            "저쪽은 한 대화의 후속 질문 범위 상속이다."
        ),
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="프롬프트에 실제로 실린 Context 를 그대로 출력한다.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="실행 결과를 파일로 남기지 않는다.",
    )
    parser.add_argument(
        "--save-dir",
        default=str(DEFAULT_RUN_DIR),
        help=f"실행 기록을 남길 곳 (기본 {DEFAULT_RUN_DIR}).",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("CHAT_MODEL", "gpt-4o-mini"),
        help="LLM 모델 이름 (기본 gpt-4o-mini, 환경변수 CHAT_MODEL).",
    )
    return parser


def _print_block(title: str, body: Any) -> None:
    print(f"\n[{title}]")
    if isinstance(body, str):
        print(body if body.strip() else "없음")
    elif isinstance(body, (list, tuple)):
        if not body:
            print("없음")
        for line in body:
            print(f"  - {line}")
    elif body is None:
        print("없음")
    else:
        print(json.dumps(body, ensure_ascii=False, indent=2))


def _reference_line(reference: Any) -> str:
    agent = reference.agent
    item = reference.item
    evidence_id = reference.evidence_id or "원문 근거 없음"
    return f"{agent} · {item} ({evidence_id})"


def run_turn(
    report_json: dict[str, Any],
    question: str,
    conversation: list[dict[str, str]],
    client: OpenAIChatClient | None,
    *,
    show_context: bool,
) -> dict[str, Any]:
    """질문 하나. 대화 이력은 호출부가 이어 붙인다."""
    context = build_chat_context(report_json, question, conversation=conversation)

    print("\n" + "=" * 70)
    print(f"Q. {question}")
    print("=" * 70)
    _print_block("context_scope", sorted(context["context_scope"]))
    print("    (상세로 실린 결과 섹션. 나머지는 상태·개수만 실린다.")
    print("     Agent 를 실행한 목록이 아니라 저장된 결과 중 고른 것이다)")
    print(f"    revision_requested = {context['revision_requested']}")
    print(
        f"    evidence {len(context['evidence'])}건"
        + (
            f" · 상한에 걸려 {context['evidence_omitted_count']}건 누락"
            if context["evidence_truncated"]
            else ""
        )
    )
    print(f"    context {len(json.dumps(context, ensure_ascii=False)):,}자")

    if show_context:
        _print_block("chat_context", context)

    record: dict[str, Any] = {
        "question": question,
        "context_scope": sorted(context["context_scope"]),
        "revision_requested": context["revision_requested"],
        "evidence_count": len(context["evidence"]),
        "prompt_version": PROMPT_VERSION,
    }

    if client is None:
        print("\n[answer]")
        print("LLM 을 부르지 않았다 (OPENAI_API_KEY 없음). Context 까지만 확인했다.")
        record["answer"] = None
        record["skipped"] = "no_api_key"
        return record

    try:
        answer = client.answer(context)
    except LLMError as error:
        print("\n[answer]")
        print(f"실패: {error}")
        record["error"] = str(error)
        return record

    _print_block("answer", answer.answer)
    _print_block("references", [_reference_line(item) for item in answer.references])
    _print_block("suggested_revision", answer.suggested_revision)

    warnings = check_grounding(answer.answer, context, question)
    _print_block("grounding_warnings", warnings)

    conversation.append({"role": "USER", "content": question})
    conversation.append({"role": "ASSISTANT", "content": answer.answer})

    record.update(
        {
            "answer": answer.answer,
            "references": [item.model_dump() for item in answer.references],
            "suggested_revision": answer.suggested_revision,
            "grounding_warnings": warnings,
        }
    )
    return record


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    questions = list(args.message or [])
    if args.sample_questions:
        questions.extend(SAMPLE_QUESTIONS)
    if not questions:
        print("질문이 없다. --message 또는 --sample-questions 를 준다.", file=sys.stderr)
        return 2

    loader = JsonFileLoader(args.fixture or args.input_json)
    try:
        report_json = loader.load()
    except LoaderError as error:
        print(str(error), file=sys.stderr)
        return 1

    print(f"입력: {loader.path}")
    for key, value in describe(report_json).items():
        print(f"  {key:16s} {value}")

    api_key = os.getenv("OPENAI_API_KEY")
    client = (
        OpenAIChatClient(api_key=api_key, model=args.model) if api_key else None
    )
    if client is None:
        print("\n  OPENAI_API_KEY 가 없어 LLM 은 건너뛴다 — Context 만 확인한다.")
    else:
        print(f"\n  모델 {args.model} · 프롬프트 {PROMPT_VERSION}")

    # --sample-questions 는 각 질문을 새 대화로 본다(초기 화면에서 하나를 누른
    # 상황). --message 를 여러 번 준 것은 한 대화로 이어 붙인다.
    records = []
    if args.sample_questions and not args.message:
        for question in questions:
            records.append(
                run_turn(report_json, question, [], client, show_context=args.show_context)
            )
    else:
        conversation: list[dict[str, str]] = []
        for question in questions:
            records.append(
                run_turn(
                    report_json,
                    question,
                    conversation,
                    client,
                    show_context=args.show_context,
                )
            )

    if not args.no_save:
        directory = Path(args.save_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"run_chat_{stamp}.json"
        path.write_text(
            json.dumps(
                {"input": str(loader.path), "model": args.model, "turns": records},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n실행 기록: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
