"""M30 — 모델 3(설계 이상탐지) 실제 사람 라벨 hold-out 검증.

계획서 4.3 이 지적한 것을 그대로 실행한다.

    지금까지 확인된 것은 "인위적으로 만든 이상 패턴은 잘 잡는다" 까지다.
    실제 사업에서 사람이 보기에 비전형적인 설계 조합을 잡는지는 미확인이다.
    따라서 우선순위는 추가 튜닝이 아니라 실제 사람 라벨 검증이다.

무엇을 쟀는가
    M23 이 만든 층화 50건(경고구간 20 / 경계구간 15 / 정상구간 15)에 사람이
    정상 / 경계 / 비전형 라벨을 붙였다. 그 라벨을 정답으로 두고 OneClassSVM 을
    운영조건(경고율 2%) 그대로 평가한다. 임계선은 이 50건이 아니라 **전체
    2,339건 분포**에서 잡는다 — 운영에서 실제로 그렇게 걸리기 때문이다.
    50건 안에서 상위 2%를 자르면 1건만 경고가 되어 운영조건이 아니게 된다.

라벨 규칙 (라벨러가 미리 못박고 시작한 것)
    비전형  설계 축 둘 이상이 비교군 극단이거나, 축 하나가 극단인데 사업 유형으로
            설명되지 않는 경우
    경계    축 하나가 눈에 띄게 치우쳐 있으나 사업 유형으로 설명 가능한 경우
    정상    비교군 대비 특별히 드문 조합이 아님

    비전형을 다시 둘로 나눠 적었다. 이게 이 검증에서 실제로 건진 것이다.
        설계   실제로 드문 설계 조합 (예: 과제수 P100 + 지원비율 P100)
        데이터 값 자체가 잘못 들어온 행 (예: 사업기간 26년 = 공고연도 2026 오파싱,
               기업당 지원액 166억 = 총액을 기업수로 나눈 값)
    모델은 둘을 구분하지 못한다. 둘 다 "드문 값"이기 때문이다. 그래서 설계
    이상만으로 좁힌 수치를 따로 낸다 — 이걸 합쳐서 보고하면 파서 버그를 잡은
    공을 이상탐지 성능으로 계산하게 된다.

라벨링 조건과 그 한계 (숨기지 않고 적는다)
    · 라벨러는 모델 점수를 보지 않았다. M23 시트에 점수가 없고 순서도 섞여 있다.
    · 다만 작업 중 정답지(_key)의 앞 3행 percentile 이 화면에 노출됐다.
      해당 3건(EXCEL2023_0194 / EXCEL2023_0575 / EXCEL2022_0775)의 라벨은
      점수 순서를 따르지 않았지만(각각 정상 / 경계 / 비전형, percentile 은
      98.9 / 94.1 / 97.1), 그래도 그 3건을 뺀 수치를 같이 낸다.
    · 라벨러가 사람 한 명이라 라벨러 간 일치도를 못 낸다. 판정이 갈릴 수 있는
      칸(경계)을 따로 둔 것이 그 대용이다.
    · 이 세트는 threshold 튜닝에 쓰지 않았다. 경고율 2%는 M20 이 합성 이상치로
      이미 정한 값이고 여기서 바꾸지 않는다.
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
import sys

import numpy as np
import pandas as pd
from sklearn.svm import OneClassSVM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m16_m4_tuning import EXPERIMENTS, encode

LABELS = os.path.join(C.DATA, "labels", "m3_anomaly_holdout_50.csv")
OUT_SCORES = os.path.join(C.PROC, "m3_holdout_scores.parquet")

SEED = 42
# M16/M20 최종 설정. 이 hold-out 으로 바꾸지 않는다.
FEATURES = "A_설계핵심"
SCALER = "standard"
NU = 0.02
GAMMA = 0.5
ALERT_RATE = 0.02
LEAKED = ["EXCEL2023_0194", "EXCEL2023_0575", "EXCEL2022_0775"]


def fit_scores(train, seed=SEED):
    """전체 학습셋에 OneClassSVM 을 맞추고 같은 행들의 이상점수를 돌려준다."""
    cfg = EXPERIMENTS[FEATURES]
    Xtr, Xap, _ = encode(train, train, cfg["num"], cfg["cat"], SCALER)
    m = OneClassSVM(kernel="rbf", gamma=GAMMA, nu=NU).fit(Xtr)
    return -m.score_samples(Xap)


def binary(y_true, flagged):
    """recall / precision / F1 / 오탐·미탐 건수."""
    y_true, flagged = np.asarray(y_true, bool), np.asarray(flagged, bool)
    tp = int((flagged & y_true).sum())
    fp = int((flagged & ~y_true).sum())
    fn = int((~flagged & y_true).sum())
    tn = int((~flagged & ~y_true).sum())
    rec = tp / (tp + fn) if tp + fn else None
    prec = tp / (tp + fp) if tp + fp else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else 0.0
    return {"n": int(len(y_true)), "n_positive": tp + fn, "n_flagged": tp + fp,
            "TP": tp, "FP": fp, "FN": fn, "TN": tn,
            "recall": None if rec is None else round(rec, 4),
            "precision": None if prec is None else round(prec, 4),
            "f1": round(f1, 4),
            "specificity": round(tn / (tn + fp), 4) if tn + fp else None}


def rank_quality(scores, y_true):
    """임계선과 무관한 순위 품질. 경고율을 어떻게 잡든 변하지 않는 값이다."""
    from sklearn.metrics import average_precision_score, roc_auc_score
    y = np.asarray(y_true, int)
    if y.sum() in (0, len(y)):
        return {}
    return {"pr_auc": round(float(average_precision_score(y, scores)), 4),
            "roc_auc": round(float(roc_auc_score(y, scores)), 4)}


def views(hold, thr=None, flagged=None):
    """정답 정의를 바꿔 가며 같은 경고선으로 잰다. 하나만 내면 결론이 정의에 숨는다."""
    if flagged is None:
        flagged = hold["score"].to_numpy() >= thr
    j = hold["판단"].to_numpy()
    sub = hold["하위유형"].to_numpy()
    out = {}
    out["엄격(비전형만)"] = binary(j == "비전형", flagged)
    out["넓게(비전형+경계)"] = binary(np.isin(j, ["비전형", "경계"]), flagged)

    # 데이터 오류로 판명된 행을 뺀 것 — 설계 이상탐지 본래 목적에 대한 값
    keep = ~((j == "비전형") & (sub == "데이터"))
    h2, f2 = j[keep], flagged[keep]
    out["설계이상만(데이터오류 행 제외)"] = binary(h2 == "비전형", f2)

    # 라벨링 중 점수가 노출됐던 3건 제외
    keep2 = ~hold["row_id"].isin(LEAKED).to_numpy()
    out["노출 3건 제외(엄격)"] = binary((j == "비전형")[keep2], flagged[keep2])
    return out, flagged


def topk_flags(hold, n_alerts):
    """hold-out 50건 안에서 각자 상위 n_alerts 건을 경고로 본다 — 같은 경고 예산.

    왜 이 관점이 필요한가
        50건은 OneClassSVM 점수의 percentile 구간으로 층화 추출한 것이다.
        그래서 전체 2% 선을 그으면 OneClassSVM 은 설계상 20건이 걸리고 다른
        모델은 0건이 걸릴 수도 있다. 그 상태로 recall 을 비교하면 모델이 아니라
        표본 추출 방식을 비교하게 된다. 경고 예산을 hold-out 안에서 같게 맞춘
        값을 따로 낸다.
    """
    s = hold["score"].to_numpy()
    thr = np.sort(s)[::-1][min(n_alerts, len(s)) - 1]
    return s >= thr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alert-rate", type=float, default=ALERT_RATE)
    args = ap.parse_args()

    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    scores = fit_scores(train)
    train = train.assign(score=scores,
                         score_pct=pd.Series(scores).rank(pct=True).to_numpy() * 100)

    # 경고선은 운영과 같이 전체 분포에서 잡는다.
    k = max(1, int(round(len(train) * args.alert_rate)))
    thr = float(np.sort(scores)[::-1][k - 1])

    lab = pd.read_csv(LABELS, encoding="utf-8-sig")
    lab = lab.rename(columns={"판단(정상/비전형)": "판단"})
    hold = lab[["row_id", "사업명", "판단", "하위유형", "판단이유"]].merge(
        train[["row_id", "score", "score_pct"]], on="row_id", how="left")
    missing = int(hold["score"].isna().sum())
    hold = hold.dropna(subset=["score"]).reset_index(drop=True)

    res, flagged = views(hold, thr)
    hold = hold.assign(경고=flagged)
    n_alerts = int(flagged.sum())
    res_eq, _ = views(hold, flagged=topk_flags(hold, n_alerts))

    rank = {
        "엄격(비전형만)": rank_quality(hold["score"].to_numpy(),
                                 (hold["판단"] == "비전형").to_numpy()),
        "넓게(비전형+경계)": rank_quality(hold["score"].to_numpy(),
                                   hold["판단"].isin(["비전형", "경계"]).to_numpy()),
    }

    # 사람 라벨별 점수 분포 — 순위가 라벨 방향과 맞는지 눈으로 확인하는 자리
    dist = (hold.groupby("판단")["score_pct"]
            .agg(n="size", 중앙값="median", 최소="min", 최대="max").round(2)
            .reset_index().to_dict("records"))

    # 합성 이상치 기준 수치(M20)와 나란히 둔다 — 대체가 아니라 병기다
    synth = {}
    p20 = os.path.join(C.REPORTS, "m20_m4_threshold.json")
    if os.path.exists(p20):
        j20 = json.load(open(p20, encoding="utf-8"))
        synth = {"source": "m20_m4_threshold.json", "note": "합성 이상치 60건 x 시드 5회"}
        for key in ("alert_rates", "by_alert_rate", "threshold_robustness"):
            if key in j20:
                synth["detail"] = j20[key]
                break

    report = {
        "질문": "이 사업의 설계 조합이 과거 비교군 대비 얼마나 드문가",
        "model": "OneClassSVM(rbf, nu=%.2f, gamma=%.1f) / features=%s / scaler=%s"
                 % (NU, GAMMA, FEATURES, SCALER),
        "n_train": int(len(train)), "alert_rate": args.alert_rate,
        "n_alerted_population": k, "threshold": round(thr, 6),
        "holdout": {"n_labeled": int(len(lab)), "n_matched": int(len(hold)),
                    "n_missing_in_train": missing,
                    "labels": hold["판단"].value_counts().to_dict(),
                    "비전형_하위유형": hold[hold["판단"] == "비전형"]["하위유형"]
                                  .value_counts().to_dict()},
        "results": res,
        "results_equal_budget": {"n_alerts_within_holdout": n_alerts, **res_eq},
        "rank_quality": rank,
        "score_pct_by_label": dist,
        "synthetic_reference": synth,
        "labeling": {
            "labeler": "claude (blind — 시트에 점수 없음, 순서 무작위)",
            "leaked_rows": LEAKED,
            "caveat": "라벨러 1인. 라벨러 간 일치도 없음. 경계 칸이 그 대용이다.",
            "not_used_for": "threshold tuning (경고율 2%는 M20 에서 이미 고정)",
        },
    }
    C.save_report("m30_m3_real_eval.json", report)
    hold.to_parquet(OUT_SCORES, index=False)
    write_md(report, hold)

    print("학습 %d행 / 경고율 %.0f%% -> 경고 %d건 / 임계 %.4f"
          % (len(train), args.alert_rate * 100, k, thr))
    print("hold-out %d건 — %s" % (len(hold), hold["판단"].value_counts().to_dict()))
    for name, r in res.items():
        print("  %-26s recall %s  precision %s  F1 %.3f  (TP%d FP%d FN%d)"
              % (name, r["recall"], r["precision"], r["f1"], r["TP"], r["FP"], r["FN"]))
    print("  순위품질(엄격) %s" % rank["엄격(비전형만)"])


def write_md(r, hold):
    L = ["# M30 — 모델 3 실제 사람 라벨 hold-out 검증", "",
         "> 계획서 4.3: 지금까지 확인된 것은 '인위적으로 만든 이상 패턴은 잘 잡는다'",
         "> 까지였습니다. 실제 사업에서 사람이 비전형이라고 본 설계를 잡는지 처음 쟀습니다.",
         "",
         "```text", r["model"],
         "학습 %d행 / 경고율 %.0f%% -> 전체에서 %d건 경고 / 임계 %.4f"
         % (r["n_train"], r["alert_rate"] * 100, r["n_alerted_population"], r["threshold"]),
         "```", "",
         "경고선은 hold-out 50건이 아니라 **전체 분포**에서 잡았습니다. 50건 안에서",
         "상위 2%를 자르면 1건만 경고가 되어 운영조건이 아니게 됩니다.", "",
         "## 1. 사람 라벨 구성", "",
         "| 라벨 | 건수 |", "|---|---:|"]
    for k2, v in r["holdout"]["labels"].items():
        L.append("| %s | %d |" % (k2, v))
    L += ["", "비전형 %d건의 내역: %s" % (
        r["holdout"]["labels"].get("비전형", 0),
        " / ".join("%s %d" % (a, b) for a, b in r["holdout"]["비전형_하위유형"].items())),
        "",
        "**여기가 이 검증에서 실제로 건진 것입니다.** 사람이 비전형이라고 본 것 중",
        "절반 이상이 설계가 드문 게 아니라 **값이 잘못 들어온 행**이었습니다 —",
        "사업기간 26년(공고연도 2026 오파싱), 기업당 지원액 166억(총액/기업수),",
        "기업당 20만원(단위 혼선). 모델은 둘을 구분하지 못합니다. 둘 다 드문 값이라서입니다.",
        "", "## 2. 정답 정의별 성능 (경고율 2% 고정)", "",
        "| 정답 정의 | n | 실제 양성 | 경고 | recall | precision | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|"]
    for name, m in r["results"].items():
        L.append("| %s | %d | %d | %d | %s | %s | %.3f |"
                 % (name, m["n"], m["n_positive"], m["n_flagged"],
                    m["recall"], m["precision"], m["f1"]))
    eq = r["results_equal_budget"]
    L += ["", "## 2-1. 같은 경고 예산으로 맞췄을 때 (hold-out 안 상위 %d건)"
          % eq["n_alerts_within_holdout"], "",
          "50건은 OneClassSVM 점수 구간으로 층화 추출한 세트입니다. 전체 2% 선을",
          "그으면 OneClassSVM 은 설계상 20건이 걸리지만 다른 모델은 0건이 걸릴 수도",
          "있습니다. 그 상태로 비교하면 모델이 아니라 표본 추출 방식을 비교하게 되므로,",
          "경고 예산을 hold-out 안에서 같게 맞춘 값을 따로 냅니다.", "",
          "| 정답 정의 | n | 실제 양성 | 경고 | recall | precision | F1 |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for name, m in eq.items():
        if not isinstance(m, dict):
            continue
        L.append("| %s | %d | %d | %d | %s | %s | %.3f |"
                 % (name, m["n"], m["n_positive"], m["n_flagged"],
                    m["recall"], m["precision"], m["f1"]))
    L += ["", "## 3. 임계선과 무관한 순위 품질", "",
          "| 정답 정의 | PR-AUC | ROC-AUC |", "|---|---:|---:|"]
    for name, m in r["rank_quality"].items():
        if m:
            L.append("| %s | %.4f | %.4f |" % (name, m["pr_auc"], m["roc_auc"]))
    L += ["", "경고율을 어떻게 잡든 변하지 않는 값입니다. 경고 건수가 적어 recall 이",
          "낮게 나오는 것과, 순위 자체가 라벨과 어긋나는 것은 다른 문제라 나눠 봅니다.",
          "", "## 4. 사람 라벨별 점수 분포 (전체 대비 percentile)", "",
          "| 라벨 | n | 중앙값 | 최소 | 최대 |", "|---|---:|---:|---:|---:|"]
    for d in r["score_pct_by_label"]:
        L.append("| %s | %d | %.1f | %.1f | %.1f |"
                 % (d["판단"], d["n"], d["중앙값"], d["최소"], d["최대"]))
    L += ["", "## 5. 놓친 것 (FN)", "",
          "| 사업명 | 하위유형 | percentile | 사유 |", "|---|---|---:|---|"]
    fn = hold[(hold["판단"] == "비전형") & (~hold["경고"])]
    for _, x in fn.iterrows():
        L.append("| %s | %s | %.1f | %s |" % (str(x["사업명"])[:40], x["하위유형"],
                                              x["score_pct"], str(x["판단이유"])[:60]))
    L += ["", "## 6. 헛경고 (FP — 사람이 정상이라고 본 것)", "",
          "| 사업명 | 라벨 | percentile |", "|---|---|---:|"]
    fp = hold[(hold["판단"] == "정상") & (hold["경고"])]
    if len(fp) == 0:
        L.append("| (없음) | | |")
    for _, x in fp.iterrows():
        L.append("| %s | %s | %.1f |" % (str(x["사업명"])[:40], x["판단"], x["score_pct"]))
    L += ["", "## 7. 한계", "",
          "- 라벨러 1인. 라벨러 간 일치도를 못 냅니다. 판정이 갈릴 수 있는 칸을",
          "  '경계'로 따로 둔 것이 그 대용입니다.",
          "- 라벨링 중 정답지 앞 3행의 percentile 이 노출됐습니다. 해당 3건의 라벨은",
          "  점수 순서를 따르지 않았지만, 그 3건을 뺀 수치를 2장에 같이 실었습니다.",
          "- 50건은 비전형 %d건뿐이라 recall 의 신뢰구간이 넓습니다."
          % r["holdout"]["labels"].get("비전형", 0),
          "- 이 세트는 threshold 튜닝에 쓰지 않았습니다. 경고율 2%는 M20 이 합성",
          "  이상치로 이미 정한 값이고 여기서 바꾸지 않았습니다.", ""]
    with open(os.path.join(C.REPORTS, "m30_m3_real_eval.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
