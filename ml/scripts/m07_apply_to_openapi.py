"""M07 — 지원성격 분류기를 Open API 원문에 적용 (추론 확장, A안).

배경: 지원성격(중분류를 묶은 support_type) 라벨은 2023 중앙부처 엑셀
909건에만 있다. Open API 1,570건(원문 확보 906건)에는 라벨이 없다.

이 스크립트는 새 라벨을 만드는 게 아니라, 2023 엑셀로 학습한 분류기를
라벨 없는 Open API 원문에 그대로 적용한다. 이것이 실제 서비스가 하게 될
일과 같다 — 신규 사업계획서는 애초에 라벨이 없으니까.

원문 텍스트를 검토했을 때 키워드 규칙으로 라벨을 직접 만드는 것은
신뢰할 수 없었다(74%가 2개 이상 카테고리에 동시 매칭, 명시적 표기는 4.2%뿐).
그래서 규칙이 아니라 학습된 모델의 확률 예측을 사용한다.

주의: 짧은 CSV 요약(sub-line)이 아니라 공고문 원문 전체를 입력으로 쓴다.
원문이 없는 공고(약 42%)는 요약문으로 대체하되 출처를 구분해서 남긴다.

임계값 0.25는 도메인 내부(2023 엑셀, 5-fold CV) 실측 기반이다. 0.4로
시작했을 때 94.1%가 보류돼 확인한 결과, 자체 데이터에서도 평균 확신도가
0.32뿐이었다 — 900건 26클래스 모델이 원래 그렇게 퍼진다. 임계값별
정밀도(같은 CV)는 0.15→78.3% / 0.20→82.5% / 0.25→86.8%(커버리지 60.6%) /
0.30→90.5% / 0.35→92.0% / 0.40→92.3%(커버리지 26.1%)다.
0.25를 하한, 0.35를 상한으로 잡아 3단계 신뢰 등급을 매긴다.
계획서 20장의 "판단 불가/추가자료 필요" 원칙과 같은 맥락이다.
"""
import json

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer

from common import PROC, REPORTS, save_report
from m06_support_type import coarsen, tfidf, MIN_SUPPORT

TAX = PROC + "/business_taxonomy.parquet"
DETAIL = PROC + "/announcement_detail.parquet"
DOCS = REPORTS + "/e01_documents.jsonl"
OUT = PROC + "/announcement_detail_with_support_type.parquet"

# 도메인 내부 5-fold CV 정밀도 곡선 기반. 0.25 미만은 판단보류,
# 0.25~0.35는 참고용(정밀도 ~87~92%), 0.35 이상은 신뢰(정밀도 ~92%+).
HOLD_THRESHOLD = 0.25
TRUST_THRESHOLD = 0.35


def tier(conf):
    if conf < HOLD_THRESHOLD:
        return "판단보류"
    if conf < TRUST_THRESHOLD:
        return "참고용"
    return "신뢰"


def load_docs():
    best = {}
    with open(DOCS, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("n_chars", 0) <= 0:
                continue
            pid = r["announcement_id"]
            if pid not in best or r["n_chars"] > best[pid]["n_chars"]:
                best[pid] = r
    return best


def main():
    # 1) 학습 데이터: 2023 엑셀 909건 전체를 학습에 쓴다
    #    (fold 평가는 이미 m06에서 끝냈으므로, 여기서는 배포용 모델을 전량으로 재학습)
    t = pd.read_parquet(TAX)
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    keep = vc[vc >= MIN_SUPPORT].index
    sub = sub[sub["support_type"].isin(keep)]

    Xtr = sub["text_for_model"].fillna("").astype(str).values
    ytr = sub["support_type"].values
    print("학습: %d건 / %d클래스 (2023 중앙부처 엑셀 전량)" % (len(sub), len(keep)))

    # LinearSVM은 확률을 안 주므로, 확신도 기반 보류 판정을 위해
    # 같은 조건의 LogisticRegression을 배포 모델로 쓴다.
    # (m06에서 LinearSVM 0.6428 > LR 0.5866 이었으나 그 차이보다
    #  확률 기반 보류 판정의 이득이 서비스 관점에서 더 크다고 본다)
    clf = Pipeline([("t", tfidf()),
                    ("m", LogisticRegression(max_iter=2000, C=5.0,
                                             class_weight="balanced",
                                             random_state=42))])
    clf.fit(Xtr, ytr)
    classes = clf.named_steps["m"].classes_

    # 2) 적용 대상: Open API 1,570건. 원문이 있으면 원문, 없으면 요약문.
    d = pd.read_parquet(DETAIL)
    docs = load_docs()
    texts, sources, doc_chars = [], [], []
    for pid, summary, target in zip(d["announcement_id"].astype(str),
                                    d["summary_text"], d["target_text"]):
        r = docs.get(pid)
        if r is not None:
            texts.append(r["text"][:4000])
            sources.append("document")
            doc_chars.append(r["n_chars"])
        else:
            texts.append(f"{summary}\n{target}")
            sources.append("summary_fallback")
            doc_chars.append(0)

    proba = clf.predict_proba(texts)
    pred_idx = proba.argmax(axis=1)
    pred_label = classes[pred_idx]
    pred_conf = proba.max(axis=1)

    result = d.copy()
    result["support_type_pred"] = pred_label
    result["support_type_confidence"] = pred_conf
    result["support_type_source"] = sources
    result["support_type_doc_chars"] = doc_chars
    result["support_type_status"] = [tier(c) for c in pred_conf]

    result.to_parquet(OUT, index=False)

    # 3) 리포트
    by_source_conf = (result.groupby("support_type_source")["support_type_confidence"]
                      .mean().round(4).to_dict())
    tier_counts = result["support_type_status"].value_counts().to_dict()
    dist = result.loc[result["support_type_status"] != "판단보류",
                      "support_type_pred"].value_counts().to_dict()

    rep = {
        "train_rows": len(sub), "train_classes": len(keep),
        "applied_rows": len(result),
        "source_dist": result["support_type_source"].value_counts().to_dict(),
        "mean_confidence_by_source": by_source_conf,
        "hold_threshold": HOLD_THRESHOLD, "trust_threshold": TRUST_THRESHOLD,
        "in_domain_precision_curve": {
            "0.15": 0.783, "0.20": 0.825, "0.25": 0.868,
            "0.30": 0.905, "0.35": 0.920, "0.40": 0.923,
        },
        "tier_counts": tier_counts,
        "tier_rate": {k: round(v / len(result), 4) for k, v in tier_counts.items()},
        "predicted_class_dist": dist,
        "output": OUT,
    }
    save_report("m07_apply_to_openapi.json", rep)

    print()
    print("적용 %d건 (원문 %d / 요약대체 %d)"
          % (len(result), (result.support_type_source == "document").sum(),
             (result.support_type_source == "summary_fallback").sum()))
    print("평균 확신도 — 원문: %.4f  요약대체: %.4f"
          % (by_source_conf.get("document", 0), by_source_conf.get("summary_fallback", 0)))
    print()
    print("신뢰 등급 분포 (임계값 %.2f / %.2f, 도메인내부 CV 정밀도 기준):"
          % (HOLD_THRESHOLD, TRUST_THRESHOLD))
    for k in ("신뢰", "참고용", "판단보류"):
        n = tier_counts.get(k, 0)
        print("  %-6s %5d건 (%.1f%%)" % (k, n, n / len(result) * 100))
    print()
    print("등급별 예측 지원성격 분포 (상위 10, 판단보류 제외):")
    for k, v in list(dist.items())[:10]:
        print("  %-14s%4d건" % (k, v))
    print()
    print("=== 검수용 표본 10건 (원문 기반, 확신도 높은 순) ===")
    check = result[result.support_type_source == "document"].nlargest(10, "support_type_confidence")
    for _, r in check.iterrows():
        print("  [%.2f/%s] %-8s %s"
              % (r.support_type_confidence, r.support_type_status,
                 r.support_type_pred, r.title[:45]))


if __name__ == "__main__":
    main()
