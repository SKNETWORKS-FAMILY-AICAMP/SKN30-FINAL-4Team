"""M14 — 지원성격 분류기를 목록 표본(2019~2025)에 적용.

왜 필요한가
    모델 2(지원규모·공고량)의 분해 축이 그동안 `large_category`(경영/기술/수출…)
    였다. 그런데 그 값은 기업마당이 원천에 이미 담아 주는 필드라, 그것만으로
    쪼개면 모델 1 이 모델 2 에 아무 기여도 하지 않는다. 모델 1 을 만든 이유가
    원천에 없는 '지원성격'(융자/판로/설비/컨설팅…) 축을 만드는 것인데 정작
    쓰지 않고 있었다.

    기업 입장의 질문도 "금융 분야는 얼마쯤?"이 아니라 "융자를 받으면 얼마쯤?"이다.
    실측으로도 같은 표본에서 지원성격이 금액 분산을 더 잘 설명한다
    (support_type 56.2% vs large_category 52.7%).

    막혀 있던 건 커버리지였다. M08 은 Open API(2025~2026) 1,570건만 다루고,
    장기 시계열의 뼈대인 목록 표본(2019~2025)에는 라벨이 없었다. 이 스크립트가
    그 구멍을 메운다.

적용 조건 — M08 과 맞춘다
    학습        business_taxonomy 전량 (2022+2023, MIN_SUPPORT=10 → 19클래스)
    입력        e02_documents.jsonl 의 source="list" 원문 + clean_text 전처리
    임계값      0.25 / 0.35 (M08 과 동일)
    PDF 제외    표 셀이 뭉쳐 원문이 깨진다. A02 가 같은 이유로 이미 제외하고 있어,
                목록 표본 관측 1,720건은 전부 HWP 계열이다. 여기서도 같게 맞춘다.

    M08 은 조건 B(E01 원문 + 전처리)를 골랐지만, 목록 표본 쪽은 A02 가 E02 를
    쓰므로 여기서도 E02 로 맞춘다. 같은 문서를 두 스크립트가 다르게 읽으면
    관측과 라벨이 어긋난다.

한계
    목록 표본은 제목·원문만 있고 Open API 의 요약문 같은 정제된 필드가 없다.
    학습 텍스트(제목+목적+내용+대상)와 형식이 더 멀어 판단보류가 더 나올 수 있다.
    그 비율을 리포트에 남긴다.
"""
import argparse
import json
import os

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from common import PROC, REPORTS, save_report
from m06_support_type import MIN_SUPPORT, coarsen, tfidf
from m08_apply_v2 import HOLD_THRESHOLD, TRUST_THRESHOLD, clean_text, tier

TAX = PROC + "/business_taxonomy.parquet"
MASTER = PROC + "/announcement_master.parquet"
DOCS = REPORTS + "/e02_documents.jsonl"
OUT = PROC + "/list_sample_support_type.parquet"

EXCLUDE_EXT = {"pdf"}          # A02 와 동일 기준
DOC_SOURCE = "list"


def load_list_docs(path, exclude_ext=EXCLUDE_EXT):
    """source=list 원문 중 가장 긴 것을 공고별로 하나 고른다(A02 와 같은 규칙)."""
    best = {}
    if not os.path.exists(path):
        raise FileNotFoundError(
            "원문 추출본이 없다: %s\n  e02_extract_text_v2.py 를 먼저 실행해야 한다." % path)
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("source") != DOC_SOURCE or r.get("n_chars", 0) <= 0:
                continue
            if exclude_ext and (r.get("ext") or "").lower() in exclude_ext:
                continue
            pid = str(r["announcement_id"])
            if pid not in best or r["n_chars"] > best[pid]["n_chars"]:
                best[pid] = r
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    # ---- 학습: M08 과 동일 설정 ----
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
    print("학습: %d건 / %d클래스 (M08 과 동일)" % (len(sub), len(classes)))

    # ---- 적용 대상: 목록 표본 중 원문이 있는 건 ----
    docs = load_list_docs(DOCS)
    print("목록 표본 원문 %d건 (PDF 제외)" % len(docs))

    m = pd.read_parquet(MASTER)[
        ["announcement_id", "title", "category_large", "registered_date"]]
    m["announcement_id"] = m["announcement_id"].astype(str)
    m = m[m["announcement_id"].isin(docs.keys())].reset_index(drop=True)
    print("메타 결합 후 %d건" % len(m))

    texts = [clean_text(docs[pid]["text"]) for pid in m["announcement_id"]]
    proba = clf.predict_proba(texts)
    conf = proba.max(axis=1)
    pred = classes[proba.argmax(axis=1)]
    tiers = np.array([tier(c) for c in conf])

    out = m.copy()
    out["support_type_pred"] = pred
    out["support_type_confidence"] = conf
    out["support_type_status"] = tiers
    out["doc_ext"] = [(docs[pid].get("ext") or "").lower() for pid in m["announcement_id"]]
    out.to_parquet(OUT, index=False)

    n_hold = int((tiers == "판단보류").sum())
    usable = out[tiers != "판단보류"]
    print()
    print("판단보류 %d건 (%.1f%%) / 사용가능 %d건 (%.1f%%)"
          % (n_hold, n_hold / len(out) * 100, len(usable), len(usable) / len(out) * 100))
    print("평균 확신도 %.4f" % conf.mean())
    print()
    print("사용가능 예측의 지원성격 분포 (상위 10):")
    dist = usable["support_type_pred"].value_counts().head(10).to_dict()
    for k, v in dist.items():
        print("  %-14s%5d건" % (k, v))

    yr = pd.to_datetime(out["registered_date"], errors="coerce").dt.year
    by_year = out.assign(year=yr).groupby("year")["support_type_status"].apply(
        lambda s: round(float((s != "판단보류").mean()), 3)).to_dict()
    print()
    print("연도별 사용가능률:", by_year)

    save_report("m14_apply_list_sample.json", {
        "purpose": ("모델 2 의 분해 축을 large_category(원천 제공 필드)에서 "
                    "support_type(모델 1 산출)으로 바꾸기 위해, 장기 시계열의 뼈대인 "
                    "목록 표본에 지원성격 라벨을 부여한다."),
        "train_rows": int(len(sub)), "train_classes": int(len(classes)),
        "applied_rows": int(len(out)),
        "doc_source": DOC_SOURCE,
        "excluded_ext": sorted(EXCLUDE_EXT),
        "exclusion_reason": ("PDF 는 표 셀이 뭉쳐 원문이 깨진다. A02 가 같은 기준으로 "
                             "이미 제외하고 있어 목록 표본 관측은 전부 HWP 계열이다."),
        "thresholds": {"hold": HOLD_THRESHOLD, "trust": TRUST_THRESHOLD},
        "n_hold": n_hold,
        "hold_rate": round(n_hold / len(out), 4),
        "usable_rate": round(len(usable) / len(out), 4),
        "mean_confidence": round(float(conf.mean()), 4),
        "predicted_class_dist": dist,
        "usable_rate_by_year": by_year,
        "caveat": ("목록 표본은 원문만 있고 Open API 의 요약문 같은 정제 필드가 없다. "
                   "학습 텍스트와 형식이 더 멀어 판단보류가 더 나올 수 있다."),
        "output": OUT,
    })
    print("\n→ %s" % OUT)


if __name__ == "__main__":
    main()
