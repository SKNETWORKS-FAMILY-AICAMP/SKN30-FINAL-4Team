"""M03 — 학습/추론 입력 형식 정합이 판단보류율에 미치는 영향.

배경
    학습(2023 엑셀)은 `제목 + ①목적 + ②내용 + ③대상` 이라는 구조화된 4요소를 쓴다.
    그런데 추론(Open API)은 그 구조를 맞추지 않고 다음을 그대로 이어붙였다.

        title + summary_text + target_text + hashtags_safe

    문제가 셋이다.
      ① summary_text 안에 목적/대상/내용이 '☞' 로 구분돼 들어있는데 파싱하지 않았다
      ② target_text 는 이름만 같고 내용이 다르다.
         학습: "개인정보 보호·활용 기술 보유 중소기업 또는 창업기업" (중앙값 36자)
         추론: "중소기업" (중앙값 4자, 1,218/1,570건이 이 값)
      ③ hashtags_safe 는 학습 입력에 아예 없던 필드인데 추론에만 붙었다

    M02 에서 원문 품질(E01/E01)과 전처리 유무를 4조건으로 갈랐지만, 넷 다
    "학습과 형식이 다른 입력" 이라는 공통점을 공유했다. 그래서 원문을 더 많이,
    더 좋은 품질로 넣은 조건 D 가 오히려 보류율이 높았다(70.5% -> 73.8%).
    이 스크립트는 그 공통 전제 자체를 바꿔서 측정한다.

측정 설계
    모델·임계값·학습데이터를 M02 과 완전히 동일하게 두고 **추론 입력 구성만** 바꾼다.
    그래야 차이가 입력 형식에서 온 것임이 분리된다.

    E0. 현행 (title + summary + target + hashtags)      <- M02 fallback 과 동일 계열
    E1. 정합 (title + 목적 + 내용 + 대상)                <- 학습과 같은 4요소·같은 순서
    E2. 정합 - 제목 제외                                  제목 기여 분리
    E3. 정합 + 원문 본문발췌 대체                          원문 경로도 정합 형식으로

주의
    보류율이 낮아지는 것만으로 성공이라 판정하지 않는다. 확신도가 오르는 것은
    과신(overconfidence)일 수도 있다. 그래서 학습 도메인 5-fold 정밀도 곡선으로
    잡은 동일 임계값을 쓰고, 예측 클래스 분포가 학습 분포와 얼마나 닮았는지를
    함께 본다(분포가 한쪽으로 쏠리면 확신도만 오른 것으로 의심).
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
import json
import os
import re

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from common import PROC, REPORTS, save_report
from m01_support_type import MIN_SUPPORT, coarsen, tfidf
from m02_apply import clean_text, load_docs, tier, HOLD_THRESHOLD, TRUST_THRESHOLD

TAX = PROC + "/business_taxonomy.parquet"
DETAIL = PROC + "/announcement_detail.parquet"
DOCS_E01 = REPORTS + "/e01_documents_api.jsonl"

MARK = "☞"


def split_summary(s):
    """Open API summary_text 를 (목적, 대상, 내용) 으로 분해.

    실측: '☞' 가 1,570건 전부에 있고 1,565건이 정확히 2개다.
        <목적 문단>
        ☞ <대상>
        ☞ <내용>
    3개 이상인 5건은 마지막 조각들을 내용으로 합친다.
    """
    if not isinstance(s, str) or MARK not in s:
        return (s or "").strip(), "", ""
    parts = [p.strip() for p in s.split(MARK)]
    purpose = parts[0].strip()
    target = parts[1].strip() if len(parts) > 1 else ""
    content = " ".join(p for p in parts[2:]).strip()
    return purpose, target, content


def build_inputs(d, docs):
    """조건별 추론 입력 텍스트를 만든다. 학습 입력은 title+목적+내용+대상 순서다."""
    title = d["title"].fillna("").astype(str)
    summ = d["summary_text"].fillna("").astype(str)
    tgt_col = d["target_text"].fillna("").astype(str)
    tags = d["hashtags_safe"].fillna("").astype(str)
    pids = d["announcement_id"].astype(str).tolist()

    parsed = [split_summary(s) for s in summ]
    purpose = [p for p, _, _ in parsed]
    target = [t for _, t, _ in parsed]
    content = [c for _, _, c in parsed]

    def join(*cols):
        return ["\n".join(x for x in row if x).strip() for row in zip(*cols)]

    out = {}
    # 현행: M02 이 원문 없는 건에 쓰던 구성 + 해시태그 (p02 의 text_for_model 과 동일 계열)
    out["E0. 현행 (summary 통째 + target + hashtags)"] = join(title, summ, tgt_col, tags)
    # 정합: 학습과 같은 4요소·같은 순서
    out["E1. 정합 (title + 목적 + 내용 + 대상)"] = join(title, purpose, content, target)
    out["E2. 정합 - 제목 제외"] = join(purpose, content, target)

    # 정합 + 원문: 원문이 있으면 본문발췌를, 없으면 정합 요약을 쓴다
    aligned = out["E1. 정합 (title + 목적 + 내용 + 대상)"]
    e3 = []
    for pid, al in zip(pids, aligned):
        r = docs.get(pid)
        e3.append(clean_text(r["text"]) if r is not None else al)
    out["E3. 정합 + 원문 본문발췌"] = e3
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    # ---- 학습: M02 과 동일 ----
    t = pd.read_parquet(TAX)
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)]
    clf = Pipeline([("t", tfidf()),
                    ("m", LogisticRegression(max_iter=2000, C=5.0,
                                             class_weight="balanced",
                                             random_state=args.seed))])
    clf.fit(sub["text_for_model"].fillna("").astype(str).values,
            sub["support_type"].values)
    classes = clf.named_steps["m"].classes_
    train_dist = sub["support_type"].value_counts(normalize=True)
    print("학습 %d건 / %d클래스 (M02 과 동일 설정)" % (len(sub), len(classes)))

    tl = sub["text_for_model"].fillna("").str.len()
    print("학습 입력 길이: 중앙값 %d자\n" % tl.median())

    # ---- 추론 입력 구성 ----
    d = pd.read_parquet(DETAIL)
    docs = load_docs(DOCS_E01)
    inputs = build_inputs(d, docs)

    parsed_ok = sum(1 for s in d["summary_text"].fillna("") if MARK in s)
    print("summary 파싱 가능: %d/%d (%.1f%%)\n"
          % (parsed_ok, len(d), parsed_ok / len(d) * 100))

    print("%-38s%9s%10s%9s%9s%10s"
          % ("조건", "입력길이", "평균확신", "신뢰", "참고용", "판단보류"))
    print("-" * 86)

    results = {}
    for name, texts in inputs.items():
        proba = clf.predict_proba(texts)
        conf = proba.max(axis=1)
        pred = classes[proba.argmax(axis=1)]
        tiers = np.array([tier(c) for c in conf])
        n_hold = int((tiers == "판단보류").sum())

        # 예측 분포가 학습 분포와 얼마나 닮았나 (TVD: 0=동일, 1=완전 상이)
        pd_dist = pd.Series(pred[tiers != "판단보류"]).value_counts(normalize=True)
        tvd = 0.5 * sum(abs(pd_dist.get(c, 0.0) - train_dist.get(c, 0.0)) for c in classes)

        L = pd.Series([len(x) for x in texts])
        r = {
            "input_len_median": int(L.median()),
            "mean_confidence": round(float(conf.mean()), 4),
            "n_trust": int((tiers == "신뢰").sum()),
            "n_ref": int((tiers == "참고용").sum()),
            "n_hold": n_hold,
            "hold_rate": round(n_hold / len(d), 4),
            "usable_rate": round(1 - n_hold / len(d), 4),
            "pred_dist_tvd_vs_train": round(float(tvd), 4),
            "n_pred_classes": int(pd_dist.size),
            "top5_pred": pd_dist.head(5).round(3).to_dict(),
        }
        results[name] = r
        print("%-38s%9d%10.4f%9d%9d%10.1f%%"
              % (name, r["input_len_median"], r["mean_confidence"],
                 r["n_trust"], r["n_ref"], r["hold_rate"] * 100))

    base = results["E0. 현행 (summary 통째 + target + hashtags)"]
    best = min(results.items(), key=lambda kv: kv[1]["hold_rate"])
    print("\n현행 보류율 %.1f%% → 최저 %.1f%% (%s), 차이 %+.1f%%p"
          % (base["hold_rate"] * 100, best[1]["hold_rate"] * 100, best[0],
             (best[1]["hold_rate"] - base["hold_rate"]) * 100))
    print("예측분포 TVD (학습분포 대비): 현행 %.3f → %s %.3f"
          % (base["pred_dist_tvd_vs_train"], best[0], best[1]["pred_dist_tvd_vs_train"]))

    save_report("m03_input_alignment.json", {
        "purpose": "학습/추론 입력 형식 정합이 판단보류율에 미치는 영향 분리 측정",
        "design": "모델·임계값·학습데이터를 M02 과 동일하게 고정하고 추론 입력 구성만 변경",
        "train_rows": len(sub), "train_classes": len(classes),
        "train_input": "title + purpose + content + target_text (2023 엑셀)",
        "train_input_len_median": int(tl.median()),
        "summary_parse_rate": round(parsed_ok / len(d), 4),
        "thresholds": {"hold": HOLD_THRESHOLD, "trust": TRUST_THRESHOLD},
        "conditions": results,
        "caution": "보류율 하락만으로 성공 판정하지 않는다. 확신도 상승은 과신일 수 있어 "
                   "예측 클래스 분포의 학습분포 대비 TVD 를 함께 본다.",
    })


if __name__ == "__main__":
    main()
