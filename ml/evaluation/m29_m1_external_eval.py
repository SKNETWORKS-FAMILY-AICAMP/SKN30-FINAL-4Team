"""M29 — 확장한 외부 정답셋 150건으로 모델 1 외부 정확도 재측정.

왜 필요한가
    M07 은 외부 정답 41건으로 "판단보류 제외 78.3%" 를 냈다. 표본이 41건이면
    정확도의 95% 신뢰구간이 ±15%p 안팎이라, 이 값으로 ML/DL 최종 채택을
    결정할 수 없다. M28 이 같은 규칙으로 100건을 더 뽑았고, 이 스크립트가
    150건 전체에서 다시 잰다.

이 스크립트가 하는 일
    ① 정답셋 병합       M07 50건 + M28 100건 → openapi_manual_150.csv
    ② 라벨 기준 정렬    두 배치의 판정 기준이 어긋난 5건을 학습 원천 기준으로 맞춘다
    ③ 외부 정확도 재측정 M02 LogisticRegression(운영 산출물) + LinearSVM(M01 최종 후보)
    ④ 41건 → 131건 신뢰구간이 얼마나 좁아졌는지 같이 낸다

② 라벨 기준 정렬 — 왜 과거 라벨을 고치는가
    정답셋을 두 번에 나눠 붙이면 배치 사이에 기준이 어긋난다. 실제로 두 곳에서
    갈렸고, 둘 다 학습 원천(business_taxonomy)의 실제 용법을 보면 답이 하나다.

    IP 출원·권리화 → 기술·IP평가(M07) vs 컨설팅(M28)
        학습에서 '기술·IP평가' 10건은 전부 기술가치평가·기술금융 평가다
        (SW기술금융, 보건산업 기술가치평가, IP금융연계 평가). 반대로
        'IP(지식재산)디딤돌 프로그램'·'스타트업 지식재산바우처'·'상표권 출원지원'
        은 전부 컨설팅으로 붙어 있다. M07 이 디딤돌·상표출원을 기술·IP평가로
        본 것은 학습 라벨의 용법과 다르다 → 컨설팅으로 맞춘다.
        ('데이터 가치평가'는 평가 사업이 맞으므로 기술·IP평가 유지)

    지자체 특례보증 → 융자(M07) vs 보증(M28)
        학습에서 '보증' 10건은 전부 보증서 발급 사업이다(기술보증기금 보증,
        문화산업완성보증, 예비유니콘 특별보증). '융자'는 직접 융자·정책자금이다.
        지자체 특례보증은 보증재단이 보증서를 발급하는 사업이므로 보증이다
        → 보증으로 맞춘다. 이차보전이 함께 붙은 건도 제목의 주 수단을 따른다.

    고친 5건은 아래 ALIGNED 에 사유와 함께 남긴다. 원본 파일
    (openapi_manual_50.csv)은 건드리지 않는다 — 병합본에서만 정렬한다.

평가 규칙 — M07 과 동일하게 유지
    exclude_reason 이 있으면 정확도에서 뺀다(대상밖·컷오프미달). 대신 그 비율을
    "정답이 없는 공고가 얼마나 들어오는가"로 따로 보고한다.
    판단보류(status) 는 두 관점 모두 낸다 — 전체 / 판단보류 제외.

한계
    150건은 여전히 19클래스를 채우지 못한다. 판로 35 / 컨설팅 22 로 몰리고
    교육훈련·해외인증·수출통관·해외수주는 0건이다. 클래스별 수치는 여전히
    참고치이며, 전체 정확도와 상위 클래스만 결론에 쓴다.
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
import math
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m01_support_type import MIN_SUPPORT, coarsen, tfidf
from m02_apply import HOLD_THRESHOLD, TRUST_THRESHOLD, clean_text, load_docs

LABELS_50 = os.path.join(C.DATA, "labels", "openapi_manual_50.csv")
LABELS_100 = os.path.join(C.DATA, "labels", "openapi_manual_150_candidates.csv")
OUT_LABELS = os.path.join(C.DATA, "labels", "openapi_manual_150.csv")
TAX = os.path.join(C.PROC, "business_taxonomy.parquet")
DETAIL = os.path.join(C.PROC, "announcement_detail.parquet")
PRED_LR = os.path.join(C.PROC, "announcement_detail_with_support_type_v2.parquet")
# 운영 경로가 실제로 쓰는 원문 파일. M02 가 네 조건을 실측 비교해 B(=이 파일 +
# 전처리)를 채택했다. e01_documents.jsonl 로 바꾸면 새로 읽히는 HWP 가 학습
# 텍스트와 멀어 오히려 정확도가 떨어진다 — 그 차이도 같이 잰다.
DOCS_PROD = os.path.join(C.REPORTS, "e01_documents_api.jsonl")
DOCS_ALT = os.path.join(C.REPORTS, "e01_documents.jsonl")

SEED = 42

# 배치 간 기준이 갈린 건. (announcement_id, 새 라벨, 사유)
ALIGNED = [
    ("PBLN_000000000119107", "컨설팅",
     "IP디딤돌 권리화. 학습셋 'IP(지식재산)디딤돌 프로그램 → 컨설팅' 과 맞춘다"),
    ("PBLN_000000000120681", "컨설팅",
     "IP디딤돌 후속지원. 위와 같은 사업 계열"),
    ("PBLN_000000000119083", "컨설팅",
     "상표출원 비용. 학습셋 '상표권 출원지원 → 컨설팅' 과 맞춘다"),
    ("PBLN_000000000117597", "보증",
     "소상공인 특례보증·이차보전. 학습셋 '보증' 은 보증서 발급 사업"),
    ("PBLN_000000000117498", "보증",
     "소상공인 특례보증. 위와 같은 기준"),
]


def wilson(k, n, z=1.96):
    """정확도의 95% 신뢰구간(Wilson). 표본이 작을 때 정규근사보다 안전하다."""
    if not n:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 4), round(c + h, 4))


def build_labelset():
    """두 배치를 합치고 기준을 정렬한다. 정렬 내역을 같이 돌려준다."""
    old = pd.read_csv(LABELS_50, encoding="utf-8-sig")
    old.columns = [c.strip("﻿") for c in old.columns]
    old["batch"] = "M07"
    new = pd.read_csv(LABELS_100, encoding="utf-8-sig")
    new["batch"] = "M28"

    cols = ["idx", "announcement_id", "batch", "category_large", "agency", "title",
            "ext", "label_19class", "confidence", "exclude_reason", "note",
            "evidence", "source_url"]
    comb = pd.concat([old, new], ignore_index=True)
    for c in cols:
        if c not in comb.columns:
            comb[c] = ""
    comb = comb[cols].copy()
    comb["announcement_id"] = comb["announcement_id"].astype(str)
    for c in ("label_19class", "exclude_reason", "note"):
        comb[c] = comb[c].fillna("").astype(str)

    log = []
    for aid, new_label, why in ALIGNED:
        m = comb["announcement_id"] == aid
        if not m.any():
            continue
        before = comb.loc[m, "label_19class"].iloc[0]
        if before == new_label:
            continue
        comb.loc[m, "label_19class"] = new_label
        comb.loc[m, "note"] = (comb.loc[m, "note"] + " | M29 기준정렬: %s → %s"
                               % (before, new_label))
        log.append({"announcement_id": aid,
                    "title": str(comb.loc[m, "title"].iloc[0])[:60],
                    "before": before, "after": new_label, "why": why})
    comb.to_csv(OUT_LABELS, index=False, encoding="utf-8-sig")
    return comb, log


def fit_models(seed=SEED):
    t = pd.read_parquet(TAX)
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)]
    X = sub["text_for_model"].fillna("").astype(str).values
    y = sub["support_type"].values
    svm = Pipeline([("t", tfidf()),
                    ("m", LinearSVC(C=1.0, class_weight="balanced",
                                    random_state=seed))]).fit(X, y)
    lr = Pipeline([("t", tfidf()),
                   ("m", LogisticRegression(max_iter=2000, C=5.0,
                                            class_weight="balanced",
                                            random_state=seed))]).fit(X, y)
    return svm, lr, len(sub), sorted(set(y))


def model_inputs(ids, docs_path, source=None):
    """운영 경로와 같은 입력을 만든다 — 원문 clean_text, 없으면 요약문."""
    d = pd.read_parquet(DETAIL)
    d["announcement_id"] = d["announcement_id"].astype(str)
    d = d.set_index("announcement_id")
    docs = load_docs(docs_path, source=source)
    out, has_doc = [], []
    for pid in ids:
        r = docs.get(pid)
        if r is not None:
            out.append(clean_text(r["text"]))
            has_doc.append(True)
        else:
            row = d.loc[pid] if pid in d.index else None
            fb = "" if row is None else "%s\n%s" % (row.get("summary_text") or "",
                                                    row.get("target_text") or "")
            out.append(fb)
            has_doc.append(False)
    return out, np.array(has_doc)


def score(y_true, y_pred, keep=None):
    """정확도·macro F1. keep 이 주어지면 그 부분집합만 센다(판단보류 제외 등)."""
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    if keep is not None:
        y_true, y_pred = y_true[keep], y_pred[keep]
    n = len(y_true)
    if not n:
        return {"n": 0}
    ok = y_true == y_pred
    labels = sorted(set(y_true))
    f1 = []
    for L in labels:
        tp = int(((y_pred == L) & (y_true == L)).sum())
        fp = int(((y_pred == L) & (y_true != L)).sum())
        fn = int(((y_pred != L) & (y_true == L)).sum())
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1.append(2 * p * r / (p + r) if p + r else 0.0)
    lo, hi = wilson(int(ok.sum()), n)
    return {"n": n, "n_correct": int(ok.sum()), "accuracy": round(float(ok.mean()), 4),
            "acc_ci95": [lo, hi], "macro_f1_on_present_classes": round(float(np.mean(f1)), 4),
            "n_classes_present": len(labels)}


def per_class(y_true, y_pred):
    rows = []
    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    for L in sorted(set(y_true)):
        m = y_true == L
        tp = int((y_pred[m] == L).sum())
        pred_L = int((y_pred == L).sum())
        rows.append({"class": L, "n_true": int(m.sum()), "recall": round(tp / m.sum(), 4),
                     "n_pred": pred_L,
                     "precision": round(tp / pred_L, 4) if pred_L else None})
    return sorted(rows, key=lambda r: -r["n_true"])


def confusions(y_true, y_pred, limit=15):
    bad = [(a, b) for a, b in zip(y_true, y_pred) if a != b]
    c = pd.Series(bad).value_counts() if bad else pd.Series(dtype=int)
    return [{"true": a, "pred": b, "n": int(n)} for (a, b), n in c.head(limit).items()]



def write_md(r):
    """사람이 먼저 읽을 요약. 숫자의 원본은 JSON 이고 여기엔 결론만 둔다."""
    L = ["# M29 — 외부 정답셋 150건 재측정", ""]
    L.append("정답셋 %d건 중 평가 대상 %d건 / 제외 %d건 (%s)."
             % (r["n_labelset"], r["n_evaluable"], r["n_excluded"],
                ", ".join("%s %d" % (k, v) for k, v in r["excluded_reasons"].items())))
    L += ["", "## 외부 정확도", "",
          "| 모델 | 표본 | n | 정확도 | 95% CI |", "|---|---|---:|---:|---|"]
    for name, blocks in r["results"].items():
        for scope, s in blocks.items():
            if s.get("n"):
                L.append("| %s | %s | %d | %.4f | [%.3f, %.3f] |"
                         % (name, scope, s["n"], s["accuracy"],
                            s["acc_ci95"][0], s["acc_ci95"][1]))
    L += ["", "## 판단보류를 걸었을 때", "",
          "| 조건 | n | 정확도 | 95% CI |", "|---|---:|---:|---|"]
    for name, s in r["with_abstention"].items():
        if s.get("n"):
            L.append("| %s | %d | %.4f | [%.3f, %.3f] |"
                     % (name, s["n"], s["accuracy"], s["acc_ci95"][0], s["acc_ci95"][1]))
    L += ["", "## 클래스별 — %s" % r["best_model"], "",
          "| 클래스 | 정답 수 | 재현율 | 예측 수 | 정밀도 |", "|---|---:|---:|---:|---:|"]
    for c in r["per_class_best"]:
        L.append("| %s | %d | %.2f | %d | %s |"
                 % (c["class"], c["n_true"], c["recall"], c["n_pred"],
                    "-" if c["precision"] is None else "%.2f" % c["precision"]))
    L += ["", "## 오답 흐름 (상위)", ""]
    L += ["- %s → %s  %d건" % (c["true"], c["pred"], c["n"])
          for c in r["confusions_best"][:8]]
    L += ["", "## 라벨 기준 민감도", ""]
    L += ["- %s: %.4f (n=%d)" % (k, s["accuracy"], s["n"])
          for k, s in r["label_rule_sensitivity"].items()]
    L += ["", "## 남은 구멍", "",
          "- 외부 150건에 아예 없는 클래스: %s"
          % ", ".join(r["classes_absent_in_external"]),
          "- %s" % r["labeling_caveat"]]
    path = os.path.join(C.REPORTS, "m29_m1_external_eval.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("[report] %s" % path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args()

    comb, log = build_labelset()
    ev = comb[comb["label_19class"] != ""].reset_index(drop=True)
    print("정답셋 %d건 / 평가 대상 %d건 / 제외 %d건"
          % (len(comb), len(ev), len(comb) - len(ev)))
    if log:
        print("라벨 기준 정렬 %d건:" % len(log))
        for r in log:
            print("  %s → %s  %s" % (r["before"], r["after"], r["title"]))

    svm, lr, n_train, classes = fit_models(a.seed)
    print("학습 %d건 / %d클래스" % (n_train, len(classes)))

    ids = ev["announcement_id"].tolist()
    texts, has_doc = model_inputs(ids, DOCS_PROD)                 # 운영 조건 B
    texts_alt, has_doc_alt = model_inputs(ids, DOCS_ALT, source="api")   # 조건 D
    y = ev["label_19class"].values

    # 운영 산출물(M02) 예측 — 판단보류 status 가 붙어 있다
    prod = pd.read_parquet(PRED_LR)
    prod["announcement_id"] = prod["announcement_id"].astype(str)
    prod = prod.set_index("announcement_id")
    y_prod = np.array([prod["support_type_pred"].get(i, None) for i in ids], dtype=object)
    status = np.array([prod["support_type_status"].get(i, "") for i in ids], dtype=object)

    # 새로 학습한 두 모델을 같은 입력으로
    y_svm = svm.predict(texts)
    margin = svm.decision_function(texts)
    part = np.partition(margin, -2, axis=1)
    top2_gap = part[:, -1] - part[:, -2]
    y_lr = lr.predict(texts)
    p_lr = lr.predict_proba(texts).max(axis=1)
    y_svm_alt = svm.predict(texts_alt)
    y_lr_alt = lr.predict(texts_alt)

    # 41건(M07 배치) 과 150건 전체를 나란히
    old_mask = (ev["batch"] == "M07").to_numpy()
    results = {}
    for name, yp, keep_all in (
            ("M02 LogisticRegression (운영 산출물)", y_prod, None),
            ("LinearSVM (운영 입력)", y_svm, None),
            ("LogisticRegression (운영 입력 재학습)", y_lr, None),
            ("LinearSVM (원문 E01 전체 입력)", y_svm_alt, None),
            ("LogisticRegression (원문 E01 전체 입력)", y_lr_alt, None)):
        results[name] = {
            "전체 150건": score(y, yp),
            "기존 41건(M07 배치)": score(y[old_mask], np.asarray(yp)[old_mask]),
            "추가 90건(M28 배치)": score(y[~old_mask], np.asarray(yp)[~old_mask]),
        }

    # 판단보류를 걸었을 때 — 운영 산출물은 status, 새 모델은 M27 의 top2_gap
    hold = {
        "M02 LogisticRegression (판단보류 제외)":
            score(y, y_prod, keep=(status != "판단보류")),
        "LinearSVM (top2_gap 상위 70% 만)":
            score(y, y_svm, keep=(top2_gap >= np.quantile(top2_gap, 0.30))),
        "LogisticRegression (proba>=%.2f)" % TRUST_THRESHOLD:
            score(y, y_lr, keep=(p_lr >= TRUST_THRESHOLD)),
    }

    best = max(results, key=lambda k: results[k]["전체 150건"]["accuracy"])
    yb = {"M02 LogisticRegression (운영 산출물)": y_prod,
          "LinearSVM (운영 입력)": y_svm,
          "LogisticRegression (운영 입력 재학습)": y_lr,
          "LinearSVM (원문 E01 전체 입력)": y_svm_alt,
          "LogisticRegression (원문 E01 전체 입력)": y_lr_alt}[best]

    # 라벨 기준 민감도 — 지자체 특례보증을 '융자'로 보면 숫자가 얼마나 달라지나.
    # 오답의 가장 큰 덩어리가 보증→융자(5건)라, 이 한 줄의 판정 기준이 전체
    # 정확도를 움직인다. 기준을 바꾸자는 게 아니라, 결론이 그 기준에 얼마나
    # 의존하는지 같이 보고한다.
    y_alt = np.where(y == "보증", "융자", y)
    sensitivity = {
        "기준: 특례보증 = 보증 (채택)": score(y, yb),
        "대안: 특례보증 = 융자": score(y_alt, np.where(np.asarray(yb) == "보증",
                                                      "융자", np.asarray(yb))),
    }

    report = {
        "labelset": os.path.relpath(OUT_LABELS, C.ROOT),
        "n_labelset": int(len(comb)),
        "n_evaluable": int(len(ev)),
        "n_excluded": int(len(comb) - len(ev)),
        "excluded_reasons": {k: int(v) for k, v in
                             comb[comb["exclude_reason"] != ""]["exclude_reason"]
                             .value_counts().items()},
        "label_alignment": log,
        "label_distribution": {k: int(v) for k, v in
                               ev["label_19class"].value_counts().items()},
        "classes_absent_in_external": [c for c in classes
                                       if c not in set(ev["label_19class"])],
        "n_train": n_train,
        "n_with_document": {"운영 입력(e01_documents_api)": int(has_doc.sum()),
                            "원문 E01 전체(e01_documents)": int(has_doc_alt.sum())},
        "results": results,
        "with_abstention": hold,
        "best_model": best,
        "label_rule_sensitivity": sensitivity,
        "per_class_best": per_class(y, yb),
        "confusions_best": confusions(y, yb),
        "labeling_caveat": (
            "라벨은 첨부 원문(HWP/HWPX)의 지원내용 절을 읽고 붙였고, 애매한 계열은 "
            "학습 원천의 같은 사업명 용법을 따랐다. 확신도(confidence) 컬럼에 "
            "높음/보통/낮음을 남겼으니 낮음 건은 사람이 다시 볼 것."),
    }
    C.save_report("m29_m1_external_eval.json", report)
    write_md(report)

    print()
    print("%-38s%8s%10s%18s" % ("모델 / 표본", "n", "정확도", "95% CI"))
    print("-" * 76)
    for name, blocks in results.items():
        for scope, s in blocks.items():
            if not s.get("n"):
                continue
            print("%-38s%8d%10.4f   [%.3f, %.3f]"
                  % ((name + " · " + scope)[:38], s["n"], s["accuracy"],
                     s["acc_ci95"][0], s["acc_ci95"][1]))
    print()
    for name, s in hold.items():
        if s.get("n"):
            print("%-38s%8d%10.4f   [%.3f, %.3f]"
                  % (name[:38], s["n"], s["accuracy"], s["acc_ci95"][0], s["acc_ci95"][1]))


if __name__ == "__main__":
    main()
