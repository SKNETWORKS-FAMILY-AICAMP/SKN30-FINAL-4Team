"""자연어 회귀 검증 — 질문 세트 전체를 실제 LLM 으로 돌리고 표로 남긴다.

    python chatmessage/scripts/run_regression.py

`run_chat.py` 는 질문 하나를 눈으로 보는 도구이고, 이쪽은 **세트 전체를 돌려
비교표를 남기는** 도구다. prompt 나 어휘 표를 고친 뒤 다시 돌려 무엇이 깨졌는지
본다.

라우팅만 볼 거면 LLM 이 필요 없다.

    python -m pytest chatmessage/test_regression.py

`OPENAI_API_KEY` 가 없으면 여기서도 라우팅까지만 확인하고 끝낸다.
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from chatmessage import (  # noqa: E402
    build_chat_context,
    check_grounding,
    context_scope,
    wants_revision,
    warning_category,
)
from chatmessage.llm import LLMError, OpenAIChatClient  # noqa: E402
from chatmessage.loader import JsonFileLoader, LoaderError  # noqa: E402
from chatmessage.prompt import PROMPT_VERSION  # noqa: E402
from chatmessage.regression import (  # noqa: E402
    ALL,
    CASES,
    FOLLOW_UP_CHAINS,
    NON_DISCLOSURE_PROBES,
)

DEFAULT_FIXTURE = _ROOT / "chatmessage" / "fixtures" / "sample_full_context.json"
DEFAULT_ML_FIXTURE = _ROOT / "chatmessage" / "fixtures" / "sample_ml_context.json"
DEFAULT_OUT = _ROOT / "chatmessage" / ".runs"


def _scope_label(scope: set[str]) -> str:
    return "ALL" if set(scope) == ALL else " ".join(sorted(scope))


def _row(
    report_json: dict[str, Any],
    question: str,
    conversation: list[dict[str, str]],
    client: OpenAIChatClient | None,
) -> dict[str, Any]:
    context = build_chat_context(report_json, question, conversation=conversation)
    scope = set(context["context_scope"])
    row: dict[str, Any] = {
        "question": question,
        "context_scope": sorted(scope),
        "scope_label": _scope_label(scope),
        "revision_requested": context["revision_requested"],
        "evidence_count": len(context["evidence"]),
        "context_chars": len(json.dumps(context, ensure_ascii=False)),
        "references": None,
        "suggested_revision": None,
        "warnings": [],
    }
    if client is None:
        return row

    try:
        answer = client.answer(context)
    except LLMError as error:
        row["error"] = str(error)
        return row

    warnings = check_grounding(answer.answer, context, question)
    row.update(
        {
            "answer": answer.answer,
            "references": len(answer.references),
            "reference_agents": sorted({r.agent for r in answer.references}),
            "suggested_revision": bool(answer.suggested_revision),
            "warnings": warnings,
        }
    )
    conversation.append({"role": "USER", "content": question})
    conversation.append({"role": "ASSISTANT", "content": answer.answer})
    return row


def _print_table(title: str, rows: list[dict[str, Any]]) -> None:
    print(f"\n\n## {title}")
    print(
        f"\n{'질문':44s} {'context_scope':26s} {'ref':>4s} {'수정':>4s} {'경고':>4s}"
    )
    print("-" * 88)
    for row in rows:
        references = "-" if row["references"] is None else str(row["references"])
        revision = "-" if row["suggested_revision"] is None else (
            "O" if row["suggested_revision"] else "."
        )
        warnings = str(len(row["warnings"])) if row["warnings"] else "."
        question = row["question"]
        if len(question) > 42:
            question = question[:41] + "…"
        print(
            f"{question:44s} {row['scope_label']:26s} "
            f"{references:>4s} {revision:>4s} {warnings:>4s}"
        )
        for warning in row["warnings"]:
            print(f"    ! {warning}")
        if row.get("error"):
            print(f"    ! 실패: {row['error']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_regression.py",
        description="자연어 질문 세트 전체를 돌려 회귀 표를 만든다.",
    )
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument(
        "--ml-fixture",
        default=str(DEFAULT_ML_FIXTURE),
        help="비노출 2차 점검용. 내부 진단값이 섞여 있는 입력이어야 의미가 있다.",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--model", default=os.getenv("CHAT_MODEL", "gpt-4o-mini"))
    args = parser.parse_args(argv)

    try:
        report_json = JsonFileLoader(args.fixture).load()
        ml_json = JsonFileLoader(args.ml_fixture).load()
    except LoaderError as error:
        print(str(error), file=sys.stderr)
        return 1

    api_key = os.getenv("OPENAI_API_KEY")
    client = OpenAIChatClient(api_key=api_key, model=args.model) if api_key else None
    print(f"입력  {args.fixture}")
    print(f"프롬프트 {PROMPT_VERSION} · 모델 {args.model if client else '(건너뜀)'}")
    if client is None:
        print("OPENAI_API_KEY 가 없어 라우팅까지만 확인한다.")

    single = [_row(report_json, case.question, [], client) for case in CASES]
    _print_table(f"단일 질문 {len(single)}개", single)

    chains: list[dict[str, Any]] = []
    for index, chain in enumerate(FOLLOW_UP_CHAINS, start=1):
        print(f"\n\n## 후속 질문 {index}")
        conversation: list[dict[str, str]] = []
        rows = [_row(report_json, question, conversation, client) for question in chain]
        _print_table(f"대화 {index}", rows)
        chains.extend(rows)

    probes = [_row(ml_json, question, [], client) for question in NON_DISCLOSURE_PROBES]
    _print_table("비노출 2차 점검 (내부값이 섞인 입력)", probes)

    every = single + chains + probes
    counts = Counter(
        warning_category(warning) for row in every for warning in row["warnings"]
    )
    print("\n\n## 요약")
    print(f"  질문 {len(every)}개")
    print(f"  경고 {sum(counts.values())}건")
    for category, count in counts.most_common():
        print(f"    {category:28s} {count}")
    if not counts:
        print("    (없음)")

    failed = [row for row in every if row.get("error")]
    if failed:
        print(f"  LLM 실패 {len(failed)}건")

    if not args.no_save:
        directory = Path(args.out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"regression_{stamp}.json"
        path.write_text(
            json.dumps(
                {
                    "prompt_version": PROMPT_VERSION,
                    "model": args.model if client else None,
                    "fixture": args.fixture,
                    "single": single,
                    "chains": chains,
                    "probes": probes,
                    "warning_counts": dict(counts),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n기록 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
