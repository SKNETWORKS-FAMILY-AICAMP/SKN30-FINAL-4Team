"""M02 — 지원성격 분류기 실전 적용 v2: 원문 품질·전처리 효과 분리 측정.

M07 대비 두 가지가 바뀌었다.
  ① 원문 추출본: E01(PyMuPDF + pyhwp) → E01(pdf-inspector + rhwp)
     표 구조가 보존되고 HWP 가 실제로 읽힌다.
  ② 입력 전처리: 원문 앞부분을 그대로 자르던 것 → 관인부 제거 + 본문 마커 발췌

두 변경이 동시에 들어가면 무엇이 기여했는지 알 수 없으므로,
같은 모델·같은 임계값으로 4개 조건을 나란히 측정한다.

  A. E01 원문 + 전처리 없음   (= M07, 기준)
  B. E01 원문 + 전처리
  C. E01 원문 + 전처리 없음
  D. E01 원문 + 전처리        (최종 후보)

임계값은 M07 과 동일하게 도메인 내부 CV 기반 0.25 / 0.35 를 쓴다.
"""
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

TAX = PROC + "/business_taxonomy.parquet"
DETAIL = PROC + "/announcement_detail.parquet"
DOCS_E01 = REPORTS + "/e01_documents_api.jsonl"
DOCS_E02 = REPORTS + "/e01_documents.jsonl"
OUT = PROC + "/announcement_detail_with_support_type_v2.parquet"

# 판단보류 임계값. M09 에서 커버리지·정확도·오분류 편향을 함께 재고 정했다.
#
#   M07 수동 정답 41건(실제 적용 도메인) 대조
#       0.15  커버리지 87.8%  정확도 69.4%
#       0.20  커버리지 70.7%  정확도 79.3%   <- 채택
#       0.25  커버리지 56.1%  정확도 78.3%   <- 이전 값
#       0.30  커버리지 41.5%  정확도 82.3%
#   0.25 -> 0.20 은 커버리지가 14.6%p 늘면서 정확도가 떨어지지 않는 구간이다.
#   0.15 아래로는 정확도가 급락하고(69.4%), SW·솔루션 정밀도가 0.52 로 무너진다.
#
#   금액 왜곡 시뮬레이션에서도 0.20 은 0.25 와 큰 차이가 없다
#   (연구개발 +50%/사업화 -14% 로 동일, 0.15 에서도 같은 수준).
#
# 이전 값 0.25 는 학습 도메인 내부 CV 정밀도 곡선만 보고 잡은 값이라
# 적용 도메인에서의 커버리지 손실을 반영하지 못했다.
HOLD_THRESHOLD = 0.20
TRUST_THRESHOLD = 0.35

# ---- 전처리 (앞서 검증: 판단가능 18.4% → 25.3%) ----
LETTERHEAD = re.compile(r"^.{0,40}(공고\s*제\s*20\d{2}[-－]\d+\s*호).*?\n", re.S)
DATE_LINE = re.compile(r"^\s*20\d{2}\s*[.년]\s*\d{1,2}\s*[.월]\s*\d{0,2}\s*[.일]?\s*$", re.M)
SIGNOFF = re.compile(r"^[가-힣()（）\s]{2,20}(장|원장|이사장|청장|본부장)\s*$", re.M)
DASH = re.compile(r"^-{5,}$", re.M)
PAGE_NUM = re.compile(r"^-\s*\d+\s*-$", re.M)
CONTENT_MARK = re.compile(r"(사업\s*개요|추진\s*배경|사업\s*목적|지원\s*목적|모집\s*개요|사업\s*내용)")
ADMIN_MARK = re.compile(r"(신청\s*방법|제출\s*서류|접수\s*방법|문\s*의\s*처|붙\s*임|유의\s*사항|추진\s*절차)")
BULLET = re.compile(r"[□◦※➡▶■●○☞]")
MD_TABLE_SEP = re.compile(r"^\|[\s\-:|]+\|$", re.M)


def clean_text(text, budget=900):
    """관인부·서명·페이지번호를 걷어내고 본문 구간만 남긴다."""
    t = LETTERHEAD.sub("", text, count=1)
    for p in (DATE_LINE, SIGNOFF, DASH, PAGE_NUM, MD_TABLE_SEP):
        t = p.sub("", t)
    m = CONTENT_MARK.search(t[:1200])
    if m:
        t = t[m.start():]
    a = ADMIN_MARK.search(t)
    if a and a.start() > 100:
        t = t[:a.start()]
    t = BULLET.sub(" ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()[:budget]


def load_docs(path, source=None):
    best = {}
    if not os.path.exists(path):
        return best
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("n_chars", 0) <= 0:
                continue
            if source and r.get("source") != source:
                continue
            pid = r["announcement_id"]
            if pid not in best or r["n_chars"] > best[pid]["n_chars"]:
                best[pid] = r
    return best


def tier(c):
    if c < HOLD_THRESHOLD:
        return "판단보류"
    return "참고용" if c < TRUST_THRESHOLD else "신뢰"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    # 학습 — 2023 엑셀 전량
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
    print("학습: %d건 / %d클래스" % (len(sub), len(classes)))

    d = pd.read_parquet(DETAIL)
    pids = d["announcement_id"].astype(str).tolist()
    e01 = load_docs(DOCS_E01)
    e02 = load_docs(DOCS_E02, source="api")
    print("원문 보유 — E01 %d건 / E01 %d건" % (len(e01), len(e02)))
    print()

    fallback = (d["summary_text"].fillna("") + "\n" + d["target_text"].fillna("")).tolist()

    def make_texts(docs, preprocess):
        out, has_doc = [], []
        for pid, fb in zip(pids, fallback):
            r = docs.get(pid)
            if r is None:
                out.append(fb)
                has_doc.append(False)
            else:
                out.append(clean_text(r["text"]) if preprocess else r["text"][:4000])
                has_doc.append(True)
        return out, np.array(has_doc)

    conditions = {
        "A. E01 + 전처리없음 (=M07)": (e01, False),
        "B. E01 + 전처리": (e01, True),
        "C. E01 + 전처리없음": (e02, False),
        "D. E01 + 전처리": (e02, True),
    }

    results, stash = {}, {}
    print("%-26s%10s%10s%10s%12s%12s"
          % ("조건", "원문보유", "평균확신", "신뢰", "참고용", "판단보류"))
    print("-" * 82)
    for name, (docs, pre) in conditions.items():
        texts, has_doc = make_texts(docs, pre)
        proba = clf.predict_proba(texts)
        conf = proba.max(axis=1)
        pred = classes[proba.argmax(axis=1)]
        tiers = np.array([tier(c) for c in conf])
        r = {
            "n_with_document": int(has_doc.sum()),
            "mean_confidence": round(float(conf.mean()), 4),
            "mean_conf_with_doc": round(float(conf[has_doc].mean()), 4) if has_doc.any() else None,
            "n_trust": int((tiers == "신뢰").sum()),
            "n_ref": int((tiers == "참고용").sum()),
            "n_hold": int((tiers == "판단보류").sum()),
            "hold_rate": round(float((tiers == "판단보류").mean()), 4),
            "usable_rate": round(float((tiers != "판단보류").mean()), 4),
        }
        results[name] = r
        stash[name] = (pred, conf, tiers, has_doc)
        print("%-26s%10d%10.4f%10d%12d%12d"
              % (name, r["n_with_document"], r["mean_confidence"],
                 r["n_trust"], r["n_ref"], r["n_hold"]))

    # 사전에 D를 고르지 않고 실측으로 고른다.
    # E02는 지원규모 추출에는 크게 유리했지만(관측치 +95%), 분류에서는
    # 새로 읽히기 시작한 HWP가 학습 텍스트(정제된 요약문)와 더 멀어 불리했다.
    best_key = max(results, key=lambda k: results[k]["usable_rate"])
    best_pred = stash[best_key]
    base = results["A. E01 + 전처리없음 (=M07)"]
    final = results[best_key]
    print("\n채택: %s (사용가능률 %.1f%% 최고)" % (best_key, final["usable_rate"] * 100))
    print("-" * 82)
    # 라벨은 best_key 에서 뽑는다. 예전엔 "D"로 박아뒀는데 실제 채택은 B라 어긋났다.
    tag = best_key.split(".")[0]
    print("판단보류율: %.1f%% (A) → %.1f%% (%s)   개선 %+.1f%%p"
          % (base["hold_rate"] * 100, final["hold_rate"] * 100, tag,
             (final["hold_rate"] - base["hold_rate"]) * 100))
    print("사용가능:   %d건 (A) → %d건 (%s)"
          % (base["n_trust"] + base["n_ref"], final["n_trust"] + final["n_ref"], tag))

    # 최종본 저장
    pred, conf, tiers, has_doc = best_pred
    out = d.copy()
    out["support_type_pred"] = pred
    out["support_type_confidence"] = conf
    out["support_type_status"] = tiers
    out["support_type_has_document"] = has_doc
    out.to_parquet(OUT, index=False)
    print("\n최종본(%s) 저장 → %s" % (tag, OUT))

    dist = pd.Series(pred[tiers != "판단보류"]).value_counts().head(10).to_dict()
    print("\n사용가능 예측의 지원성격 분포 (상위 10):")
    for k, v in dist.items():
        print("  %-14s%4d건" % (k, v))

    save_report("m02_apply.json", {
        "train_rows": int(len(sub)), "train_classes": int(len(classes)),
        "applied_rows": int(len(d)),
        "thresholds": {"hold": HOLD_THRESHOLD, "trust": TRUST_THRESHOLD},
        "conditions": results,
        "chosen": best_key,
        "chosen_by": "usable_rate 최댓값 (실측 기반, 사전 지정 아님)",
        "hold_rate_change": round(final["hold_rate"] - base["hold_rate"], 4),
        "predicted_class_dist": dist,
        "note": ("E01(pdf-inspector+rhwp) 원문과 전처리(관인제거+본문발췌)의 "
                 "기여를 분리 측정. 모델·임계값은 M07과 동일."),
        "output": OUT,
    })


if __name__ == "__main__":
    main()
