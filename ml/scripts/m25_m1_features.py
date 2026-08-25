"""M25 — 모델 1: TF-IDF feature 고도화 (최종개선계획서 4순위).

계획서 55~60행이 지시한 탐색 축을 그대로 훑는다.

    char_wb ngram   (2,4) 현행 / (2,5) / (3,5) / (2,6)
    max_features    60k 현행 / 100k / 150k
    C               0.5 / 1 / 2 / 3
    char + word FeatureUnion
    field-wise TF-IDF (title / purpose / content / target 각각)

기준선
    현행 LogisticRegression + char_wb(2,4) + max_features 60k + C=5.0
    그룹CV macroF1 0.7834 (M05)

측정 규율
    M24 와 같은 StratifiedGroupKFold(program_stem). 축 하나를 바꿀 때 나머지는
    현행값으로 고정한다(one-factor-at-a-time). 조합은 축별 최적을 모은 뒤
    한 번만 따로 잰다 — OFAT 은 축 간 상호작용이 있으면 조합이 무너지므로
    조합이 기준선보다 나쁘면 기준선을 쓴다(M20 에서 실제로 겪었다).

field-wise TF-IDF 가 왜 별도 축인가
    현행은 title/purpose/content/target 을 한 문자열로 이어 붙여
    (`text_for_model`) 하나의 TF-IDF 에 넣는다. 그러면 어느 필드에서 나온
    단어인지 구분이 사라진다. 필드별로 따로 벡터화해 이어 붙이면 "제목의
    '융자'"와 "본문의 '융자'"를 다른 feature 로 볼 수 있다.
"""
import argparse
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import FunctionTransformer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m01_support_type import MIN_SUPPORT, coarsen

warnings.filterwarnings("ignore")
TAX = os.path.join(C.PROC, "business_taxonomy.parquet")
SEED = 42
BASELINE_M05 = 0.7834          # 현행 LR 그룹CV macroF1

# 현행 설정 — 축 하나를 바꿀 때 나머지는 여기에 고정한다
BASE = {"ngram": (2, 4), "max_features": 60000, "C": 5.0, "vectorizer": "char"}

FIELDS = ["title", "purpose", "content", "target_text"]


def make_vectorizer(kind, ngram, max_features):
    if kind == "char":
        return TfidfVectorizer(analyzer="char_wb", ngram_range=ngram,
                               min_df=2, sublinear_tf=True,
                               max_features=max_features)
    if kind == "word":
        return TfidfVectorizer(analyzer="word", ngram_range=(1, 2),
                               min_df=2, sublinear_tf=True,
                               max_features=max_features)
    if kind == "char+word":
        # 계획서 59행 — 문자 n-gram 은 조사·어미 변형에 강하고, 단어 n-gram 은
        # '기술개발' 같은 복합어를 통째로 잡는다. 둘을 합쳐 본다.
        return FeatureUnion([
            ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=ngram,
                                     min_df=2, sublinear_tf=True,
                                     max_features=max_features)),
            ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2),
                                     min_df=2, sublinear_tf=True,
                                     max_features=max_features // 2)),
        ])
    raise ValueError(kind)


def build_pipe(cfg, seed=SEED):
    vec = make_vectorizer(cfg["vectorizer"], cfg["ngram"], cfg["max_features"])
    return Pipeline([("t", vec), ("m", LogisticRegression(
        max_iter=2000, C=cfg["C"], class_weight="balanced", random_state=seed))])


def build_fieldwise_pipe(cfg, seed=SEED):
    """필드별로 따로 벡터화해 이어 붙인다 (계획서 60행).

    각 필드를 뽑는 셀렉터를 FeatureUnion 가지마다 달아, 같은 단어라도 어느
    필드에서 나왔는지가 서로 다른 feature 가 되게 한다.
    """
    branches = []
    for i, f in enumerate(FIELDS):
        branches.append((f, Pipeline([
            ("sel", FunctionTransformer(lambda X, i=i: X[:, i],
                                        validate=False)),
            ("t", TfidfVectorizer(analyzer="char_wb", ngram_range=cfg["ngram"],
                                  min_df=2, sublinear_tf=True,
                                  max_features=cfg["max_features"] // len(FIELDS))),
        ])))
    return Pipeline([("u", FeatureUnion(branches)), ("m", LogisticRegression(
        max_iter=2000, C=cfg["C"], class_weight="balanced", random_state=seed))])


def group_cv(X, y, groups, pipe, folds=5, seed=SEED):
    pred = np.zeros(len(y), dtype=int)
    skf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    for tr, te in skf.split(X, y, groups):
        m = clone(pipe)
        m.fit(X[tr], y[tr])
        pred[te] = np.asarray(m.predict(X[te])).ravel()
    return {
        "macro_f1": round(float(f1_score(y, pred, average="macro",
                                         zero_division=0)), 4),
        "accuracy": round(float(accuracy_score(y, pred)), 4),
        "weighted_f1": round(float(f1_score(y, pred, average="weighted",
                                            zero_division=0)), 4),
    }


def prepare_fields(full):
    """필드별 텍스트 행렬과 이어붙인 텍스트를 함께 만든다."""
    t = full.copy()
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)].reset_index(drop=True)

    from sklearn.preprocessing import LabelEncoder
    y = LabelEncoder().fit_transform(sub["support_type"].values)
    X_flat = np.asarray(sub["text_for_model"].fillna("").astype(str).values,
                        dtype=object)
    cols = []
    for f in FIELDS:
        col = sub[f] if f in sub.columns else pd.Series([""] * len(sub))
        cols.append(col.fillna("").astype(str).values)
    X_fields = np.array(cols, dtype=object).T

    stem = sub["program_stem"].fillna("").astype(str)
    dup = stem.duplicated(keep=False) & (stem != "")
    groups = np.where(dup, stem, "row_" + np.arange(len(sub)).astype(str))
    return X_flat, X_fields, np.asarray(y), np.asarray(groups), sub


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()

    full = pd.read_parquet(TAX)
    X, X_fields, y, groups, sub = prepare_fields(full)
    print("모델 1 feature 탐색: %d행 / %d클래스 / %d그룹"
          % (len(y), len(set(y)), len(set(groups))))
    print("기준선(M05 현행 LR 그룹CV): macroF1 %.4f" % BASELINE_M05)

    t0 = time.time()
    base = group_cv(X, y, groups, build_pipe(BASE), a.folds)
    print("\n== 기준 설정 재현: macroF1 %.4f / Acc %.4f"
          % (base["macro_f1"], base["accuracy"]))

    grid = {
        "ngram": [(2, 4), (2, 5), (3, 5), (2, 6)],
        "max_features": [60000, 100000, 150000],
        "C": [0.5, 1.0, 2.0, 3.0, 5.0],
        "vectorizer": ["char", "word", "char+word"],
    }
    if a.quick:
        grid = {"ngram": [(2, 4), (2, 5)], "C": [1.0, 5.0]}

    print("\n== 축별 탐색 (one-factor-at-a-time)")
    axes, best_cfg = {}, dict(BASE)
    for axis, values in grid.items():
        axes[axis] = {}
        for v in values:
            if v == BASE[axis]:
                axes[axis][str(v)] = dict(base, note="기준값")
                continue
            cfg = dict(BASE)
            cfg[axis] = v
            axes[axis][str(v)] = group_cv(X, y, groups, build_pipe(cfg), a.folds)
        best_v = max(axes[axis], key=lambda k: axes[axis][k]["macro_f1"])
        # 문자열 키를 원래 타입으로 되돌린다
        for v in values:
            if str(v) == best_v:
                best_cfg[axis] = v
        print("  %-14s %s  -> 최적 %s"
              % (axis, "  ".join("%s:%.4f" % (k, r["macro_f1"])
                                 for k, r in axes[axis].items()), best_v))

    print("\n== field-wise TF-IDF (계획서 60행)")
    fw = group_cv(X_fields, y, groups, build_fieldwise_pipe(BASE), a.folds)
    print("  필드별 분리 벡터화: macroF1 %.4f / Acc %.4f (기준 %.4f)"
          % (fw["macro_f1"], fw["accuracy"], base["macro_f1"]))

    print("\n== 축별 최적값 조합")
    print("  %s" % best_cfg)
    combined = group_cv(X, y, groups, build_pipe(best_cfg), a.folds)
    print("  macroF1 %.4f / Acc %.4f" % (combined["macro_f1"], combined["accuracy"]))

    # OFAT 안전장치 — 조합이 기준선보다 나쁘면 기준선을 쓴다
    ofat_failed = combined["macro_f1"] < base["macro_f1"]
    if ofat_failed:
        print("  -> 조합이 기준 설정보다 나쁘다 (%.4f < %.4f). 축 간 상호작용이 "
              "있다는 뜻이므로 기준 설정을 유지한다"
              % (combined["macro_f1"], base["macro_f1"]))
    final_cfg = dict(BASE) if ofat_failed else best_cfg
    final = base if ofat_failed else combined

    verdict = judge(base, combined, fw, final, ofat_failed)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    C.save_report("m25_m1_features.json", {
        "n_rows": int(len(y)), "n_classes": int(len(set(y))),
        "n_groups": int(len(set(groups))), "folds": a.folds, "seed": SEED,
        "cv": "StratifiedGroupKFold (program_stem)",
        "baseline_m05": BASELINE_M05,
        "base_config": {k: str(v) for k, v in BASE.items()}, "base_result": base,
        "axes": axes, "fieldwise": fw,
        "best_config": {k: str(v) for k, v in best_cfg.items()},
        "combined_result": combined,
        "ofat_combination_failed": bool(ofat_failed),
        "final_config": {k: str(v) for k, v in final_cfg.items()},
        "final_result": final, "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2),
    })
    write_md(base, axes, fw, best_cfg, combined, final, ofat_failed, verdict)


def judge(base, combined, fw, final, ofat_failed):
    reasons, v = [], "현행 유지"
    reasons.append("기준 설정 재현 macroF1 %.4f (M05 %.4f)"
                   % (base["macro_f1"], BASELINE_M05))
    reasons.append("축별 최적 조합 %.4f%s"
                   % (combined["macro_f1"],
                      " — 기준보다 나빠 폐기" if ofat_failed else ""))
    reasons.append("field-wise TF-IDF %.4f (기준 대비 %+.4f)"
                   % (fw["macro_f1"], fw["macro_f1"] - base["macro_f1"]))

    gain = final["macro_f1"] - base["macro_f1"]
    if fw["macro_f1"] > final["macro_f1"] and fw["macro_f1"] - base["macro_f1"] > 0.01:
        v = "field-wise TF-IDF 채택 검토"
        reasons.append("필드를 나눠 벡터화한 것이 가장 크게 올랐다")
    elif gain > 0.01:
        v = "조합 설정 채택"
        reasons.append("기준 대비 %+.4f — 반영할 만하다" % gain)
    else:
        reasons.append("어느 축도 기준선을 의미 있게 넘지 못했다. 현행 TF-IDF 설정이 "
                       "이미 이 데이터에 맞게 잡혀 있다는 뜻이다")
    return {"verdict": v, "reasons": reasons}


def write_md(base, axes, fw, best_cfg, combined, final, ofat_failed, verdict):
    L = ["# 모델 1 — TF-IDF feature 고도화", "",
         "> 최종개선계획서 4순위(55~60행). char n-gram 범위·어휘 크기·정규화 강도·",
         "> 벡터라이저 조합·필드별 분리를 전부 훑었습니다.", "",
         "## 1. 측정 규율", "",
         "M24 와 같은 `StratifiedGroupKFold`(program_stem)입니다. 축 하나를 바꿀 때",
         "나머지는 현행값으로 고정했습니다(one-factor-at-a-time).", "",
         "```text",
         "기준 설정  char_wb(2,4) / max_features 60,000 / C=5.0 / LogisticRegression",
         "기준선     그룹CV macroF1 %.4f (M05)" % BASELINE_M05,
         "```", "",
         "## 2. 축별 결과", ""]
    for axis, vals in axes.items():
        L += ["**%s**" % axis, "",
              "| 값 | macroF1 | Accuracy | 기준 대비 |", "|---|---:|---:|---:|"]
        for k, r in vals.items():
            mark = " (기준)" if r.get("note") == "기준값" else ""
            L.append("| %s%s | %.4f | %.4f | %+.4f |"
                     % (k, mark, r["macro_f1"], r["accuracy"],
                        r["macro_f1"] - base["macro_f1"]))
        L.append("")

    L += ["## 3. field-wise TF-IDF (계획서 60행)", "",
          "현행은 title/purpose/content/target 을 한 문자열로 이어 붙여",
          "(`text_for_model`) 하나의 TF-IDF 에 넣습니다. 그러면 어느 필드에서 나온",
          "단어인지 구분이 사라집니다. 필드별로 따로 벡터화해 이어 붙이면 \"제목의",
          "'융자'\"와 \"본문의 '융자'\"를 다른 feature 로 볼 수 있습니다.", "",
          "| 방식 | macroF1 | Accuracy | 기준 대비 |", "|---|---:|---:|---:|",
          "| 이어붙인 텍스트 (현행) | %.4f | %.4f | — |"
          % (base["macro_f1"], base["accuracy"]),
          "| 필드별 분리 벡터화 | %.4f | %.4f | %+.4f |"
          % (fw["macro_f1"], fw["accuracy"], fw["macro_f1"] - base["macro_f1"]), "",
          "## 4. 축별 최적값 조합", "", "```text"]
    for k, v in best_cfg.items():
        L.append("%-14s %s" % (k, v))
    L += ["```", "",
          "| | macroF1 | Accuracy |", "|---|---:|---:|",
          "| 기준 설정 | %.4f | %.4f |" % (base["macro_f1"], base["accuracy"]),
          "| 축별 최적 조합 | %.4f | %.4f |"
          % (combined["macro_f1"], combined["accuracy"]), ""]
    if ofat_failed:
        L += ["> **조합이 기준 설정보다 나쁩니다.** one-factor-at-a-time 은 축을",
              "> 하나씩만 움직여 고르므로 축끼리 상호작용하면 조합이 무너집니다.",
              "> 조합을 무조건 채택하지 않고 기준 설정을 유지합니다 — M20 에서",
              "> 같은 일을 겪고 넣은 안전장치입니다.", ""]

    L += ["## 5. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L.append("")
    p = os.path.join(C.REPORTS, "m25_m1_features.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
