"""M04 — Open API 추론 표본을 학습 입력 형태로 변환.

배경
    모델 1은 2023 중앙부처 엑셀(909건)로 학습하는데, 그 입력은
    `제목 + ①목적 + ②내용 + ③대상` 이라는 4요소 구조다. 반면 추론 대상인
    Open API 는 이 구조가 아니라 요약문 한 덩어리라, 실제 운영 경로는
    `summary_text + target_text` 를 그대로 넣고 있었다(제목도 빠져 있었다).

    측정을 더 돌리기 전에 두 입력이 실제로 어떻게 생겼는지 눈으로 대조할
    표본이 필요하다. 이 스크립트는 그 표본을 만든다.

변환 규칙
    Open API summary_text 는 '☞' 로 구분된 3단 구조다(1,570건 전부).

        <목적 문단>
        ☞ <대상>
        ☞ <내용>

    이를 학습과 같은 순서(제목 + 목적 + 내용 + 대상)로 재조립한다.

    target_text 는 쓰지 않는다. 이름은 학습의 ③대상 과 같지만 내용이 다르다.
      학습  "개인정보 보호·활용 기술 보유 중소기업 또는 창업기업" (중앙값 36자)
      추론  "중소기업" (4자, 1,570건 중 1,218건이 이 값)
    진짜 대상은 요약문의 '☞' 첫 항목에 있다.

표본 설계
    대분류 8종에서 균등 배분한다. 특정 분야에 쏠리면 도메인 차이를 오해할 수
    있다. 실제로 Open API 는 경영 422 / 기타 18 로 편중돼 있다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m4_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

_ML = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = _os.path.join(_ML, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# -------------------------------------------------------------------------

import argparse
import re

import numpy as np
import pandas as pd

from common import PROC, REPORTS, CORE8, save_report

DETAIL = PROC + "/announcement_detail.parquet"
OUT_PARQUET = PROC + "/inference_sample_aligned.parquet"
OUT_MD = REPORTS + "/m04_inference_sample.md"

MARK = "☞"


def split_summary(s):
    """summary_text -> (목적, 대상, 내용). 조각이 3개 이상이면 뒤쪽을 내용으로 합친다."""
    if not isinstance(s, str) or MARK not in s:
        return (s or "").strip(), "", ""
    parts = [p.strip() for p in s.split(MARK)]
    purpose = re.sub(r"\s+", " ", parts[0]).strip()
    target = re.sub(r"\s+", " ", parts[1]).strip() if len(parts) > 1 else ""
    content = re.sub(r"\s+", " ", " ".join(parts[2:])).strip()
    return purpose, target, content


def build(d):
    """학습(F03)과 같은 4요소·같은 순서로 재조립."""
    rows = []
    for _, r in d.iterrows():
        purpose, target, content = split_summary(r["summary_text"])
        text = "\n".join(x for x in (str(r["title"]).strip(), purpose, content, target) if x)
        rows.append({
            "announcement_id": r["announcement_id"],
            "category_large": r["category_large"],
            "title": str(r["title"]).strip(),
            "purpose": purpose,
            "content": content,
            "target_text": target,
            "target_text_raw": r["target_text"],     # 원본 필드(대부분 "중소기업")
            "text_for_model": text,
            "n_chars": len(text),
        })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="표본 크기(대분류 균등 배분)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    d = pd.read_parquet(DETAIL)
    cats = [c for c in CORE8 if c in set(d["category_large"].dropna())]
    per = max(1, args.n // len(cats))
    rng = np.random.default_rng(args.seed)

    picked = []
    for c in cats:
        g = d[d["category_large"] == c]
        take = min(per, len(g))
        picked.append(g.iloc[rng.choice(len(g), take, replace=False)])
    sample = pd.concat(picked).reset_index(drop=True)

    out = build(sample)
    out.to_parquet(OUT_PARQUET, index=False)

    # 사람이 눈으로 대조할 마크다운
    lines = ["# 추론 표본 — 학습 입력 형태로 재조립", "",
             f"Open API {len(d):,}건에서 대분류 균등 배분으로 {len(out)}건을 뽑았다.",
             "학습(F03)과 같은 순서 `제목 + 목적 + 내용 + 대상` 으로 맞췄다.", "",
             "> `target_text` 원본 필드는 쓰지 않았다. 학습의 ③대상과 이름만 같고",
             "> 내용이 다르다(대부분 \"중소기업\" 4자). 진짜 대상은 요약문의 ☞ 첫 항목에 있다.", ""]
    for c in cats:
        g = out[out["category_large"] == c]
        if g.empty:
            continue
        lines += [f"## {c}", ""]
        for _, r in g.iterrows():
            lines += [f"**{r['title']}**", "",
                      "| 항목 | 내용 |", "|---|---|",
                      f"| ①목적 | {r['purpose'] or '—'} |",
                      f"| ②내용 | {r['content'] or '—'} |",
                      f"| ③대상 | {r['target_text'] or '—'} |",
                      f"| *(원본 target_text)* | *{r['target_text_raw']}* |", ""]
    open(OUT_MD, "w", encoding="utf-8").write("\n".join(lines))

    empty = {k: int((out[k].str.strip() == "").sum()) for k in ("purpose", "content", "target_text")}
    print("표본 %d건 (대분류 %d종 × %d건)" % (len(out), len(cats), per))
    print("분야별:", out["category_large"].value_counts().to_dict())
    print("빈 필드:", empty)
    print("길이 중앙값 %d자 (학습은 181자)" % out["n_chars"].median())
    print("→ %s" % OUT_PARQUET)
    print("→ %s" % OUT_MD)

    save_report("m04_inference_sample.json", {
        "purpose": "학습·추론 입력을 눈으로 대조하기 위한 균등 표본",
        "source_rows": len(d), "sample_rows": len(out),
        "per_category": per, "categories": cats,
        "by_category": out["category_large"].value_counts().to_dict(),
        "empty_fields": empty,
        "n_chars_median": int(out["n_chars"].median()),
        "train_n_chars_median": 181,
        "rule": "summary_text 를 ☞ 로 분해해 제목+목적+내용+대상 순서로 재조립. "
                "원본 target_text 는 학습의 ③대상과 내용이 달라 쓰지 않는다.",
        "outputs": [OUT_PARQUET, OUT_MD],
    })


if __name__ == "__main__":
    main()
