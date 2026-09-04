r"""M34 — 모델 3 feature 진단: Ablation · Single · Shuffle (계획서 Step 3, §9).

계획서 §9 의 가설을 그대로 실험으로 옮긴다.

    "넣은 모든 feature 가 의미가 없을 수 있다"

이 가설은 직접 검증할 수 있다. 축을 하나씩 빼고, 하나씩만 넣고, 하나씩 섞어
같은 hold-out 에서 순위 품질이 어떻게 움직이는지 본다.

무엇을 기준으로 재는가 — M33 의 clean hold-out
    주 평가   normal(25) vs atypical_design(10) = 35건
    분리 보고 data_error(5) / uncertain(10) 은 여기 넣지 않는다.
              둘을 positive 로 섞으면 파서 버그를 잡은 공이 이상탐지 성능으로
              계산된다. M30 이 정확히 그 상태였다.

임계선과 무관한 ROC-AUC 를 1차 지표로 둔다. 경고 건수가 적어 recall 이 낮게
나오는 것과, 순위 자체가 라벨과 어긋나는 것은 다른 문제다. 운영 관점의
경고율 2% 수치도 함께 낸다.

주의 — 이 스크립트는 hold-out 을 **선택에 쓰지 않는다.** 여기서 나온 축별
기여도를 보고 feature 를 고르면 hold-out 을 튜닝셋으로 쓰는 것이다(계획서
§11.2). 진단 결과는 진단으로만 읽고, 최종 설정은 M16/M30 이 합성 이상치로
이미 정한 A_설계핵심 을 그대로 쓴다.
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

import os
import sys

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare

CLEAN = os.path.join(C.DATA, "labels", "m3_clean_holdout.csv")
SEED = 42
NU, GAMMA = 0.02, 0.5
ALERT_RATE = 0.02
N_SHUFFLE = 10
EQUAL_BUDGET = 7        # 주 평가셋 35건 안에서 상위 7건 = 20%. M30 의 20/50 과 같은 비율
N_BOOT = 2000

# 진단용 전체 축. 최종 설정(A_설계핵심)보다 넓게 잡는다 — 무엇이 기여하지
# 않는지 알려면 일단 넣고 빼 봐야 한다.
NUM = ["log_per_recipient", "log_support_count", "project_duration", "support_ratio"]
CAT = ["support_method", "amount_type", "support_type", "support_unit"]
GROUPS = {
    "amount": ["log_per_recipient"],
    "count": ["log_support_count"],
    "duration": ["project_duration"],
    "ratio": ["support_ratio"],
    "method": ["support_method"],
    "amount_type": ["amount_type"],
    "support_type": ["support_type"],
    "unit": ["support_unit"],
}


def load():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    cl = pd.read_csv(CLEAN, encoding="utf-8-sig")
    main = cl[cl["라벨"].isin(["normal", "atypical_design"])][["row_id", "라벨"]]
    main = main.merge(train[["row_id"]], on="row_id", how="inner")
    return train, cl, main


def encode(train, num, cat, rng=None, shuffle_cols=()):
    """수치는 표준화 + 결측 지시자, 범주는 train 빈도. M16 encode 와 같은 규약.

    shuffle_cols 에 든 축은 행 단위로 섞는다 — 값의 분포는 그대로 두고
    행과의 대응만 끊는다. 성능이 그대로면 그 축은 실제로 쓰이지 않는 것이다.
    """
    parts, names = [], []
    for f in num:
        med = train[f].median()
        med = 0.0 if pd.isna(med) else med
        v = train[f].to_numpy(dtype=float)
        miss = np.isnan(v).astype(float)
        v = np.where(np.isnan(v), med, v)
        if f in shuffle_cols:
            idx = rng.permutation(len(v))
            v, miss = v[idx], miss[idx]
        parts += [v, miss]
        names += [f, f + "__missing"]
    for f in cat:
        freq = train[f].value_counts(normalize=True)
        v = train[f].map(freq).fillna(0.0).to_numpy(dtype=float)
        if f in shuffle_cols:
            v = v[rng.permutation(len(v))]
        parts.append(v)
        names.append(f + "__freq")
    if not parts:
        return None, []
    X = np.column_stack(parts)
    return StandardScaler().fit_transform(X), names


def score(train, num, cat, rng=None, shuffle_cols=()):
    X, _ = encode(train, num, cat, rng, shuffle_cols)
    if X is None:
        return None
    m = OneClassSVM(kernel="rbf", gamma=GAMMA, nu=NU).fit(X)
    return -m.score_samples(X)


def _binary(y, flag):
    tp = int((flag & (y == 1)).sum())
    fp = int((flag & (y == 0)).sum())
    fn = int((~flag & (y == 1)).sum())
    rec = tp / (tp + fn) if tp + fn else None
    prec = tp / (tp + fp) if tp + fp else None
    return {"n_flagged": int(flag.sum()), "TP": tp, "FP": fp, "FN": fn,
            "recall": None if rec is None else round(rec, 4),
            "precision": None if prec is None else round(prec, 4)}


def boot_ci(y, sc, n=N_BOOT, seed=SEED):
    """ROC-AUC 의 부트스트랩 95% 구간. 35건·양성 10건이라 반드시 같이 봐야 한다.

    구간이 0.5 를 품으면 그 차이는 이 표본으로는 없는 것과 구분되지 않는다.
    """
    rng = np.random.default_rng(seed)
    pos, neg = np.where(y == 1)[0], np.where(y == 0)[0]
    out = []
    for _ in range(n):
        i = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        out.append(roc_auc_score(y[i], sc[i]))
    return round(float(np.percentile(out, 2.5)), 4), round(float(np.percentile(out, 97.5)), 4)


def evaluate(train, scores, main, ci=False):
    """clean hold-out 주 평가셋에서의 순위 품질과 두 가지 경고 관점.

    운영 경고율   전체 분포의 상위 2% 선. 실제 서비스에서 걸리는 그대로다.
    같은 경고예산 주 평가셋 안에서 상위 EQUAL_BUDGET 건. 50건이 **교체 전 모델
                  점수**로 층화 추출된 세트라 운영선을 그으면 한 건도 안 걸릴 수
                  있다. 그 상태로 모델을 비교하면 표집 방식을 비교하게 된다.
    """
    s = pd.Series(scores, index=train["row_id"].to_numpy())
    y = (main["라벨"] == "atypical_design").to_numpy(int)
    sc = s.loc[main["row_id"]].to_numpy(float)

    k = max(1, int(round(len(train) * ALERT_RATE)))
    thr = float(np.sort(scores)[::-1][k - 1])
    r = {"roc_auc": round(float(roc_auc_score(y, sc)), 4),
         "pr_auc": round(float(average_precision_score(y, sc)), 4),
         "operating_2pct": _binary(y, sc >= thr)}
    thr_eq = np.sort(sc)[::-1][min(EQUAL_BUDGET, len(sc)) - 1]
    r["equal_budget_top%d" % EQUAL_BUDGET] = _binary(y, sc >= thr_eq)
    if ci:
        r["roc_auc_ci95"] = boot_ci(y, sc)
    return r


def main():
    train, cl, m = load()
    y = (m["라벨"] == "atypical_design").to_numpy(int)
    print("M34 — feature 진단")
    print("  학습 %d행 / 주 평가셋 %d건 (양성 %d)" % (len(train), len(m), int(y.sum())))
    print("  분리 보고 data_error %d / uncertain %d"
          % (int((cl["라벨"] == "data_error").sum()),
             int((cl["라벨"] == "uncertain").sum())))

    full = evaluate(train, score(train, NUM, CAT), m, ci=True)
    eq = full["equal_budget_top%d" % EQUAL_BUDGET]
    print("\n== Full (수치4 + 범주4)")
    print("  ROC-AUC %.4f  95%%CI [%.3f, %.3f]  PR-AUC %.4f"
          % (full["roc_auc"], full["roc_auc_ci95"][0], full["roc_auc_ci95"][1],
             full["pr_auc"]))
    print("  운영 경고율 2%%   경고 %d건 (TP %d FP %d FN %d) recall %s"
          % (full["operating_2pct"]["n_flagged"], full["operating_2pct"]["TP"],
             full["operating_2pct"]["FP"], full["operating_2pct"]["FN"],
             full["operating_2pct"]["recall"]))
    print("  같은 경고예산 상위%d  TP %d FP %d recall %s precision %s"
          % (EQUAL_BUDGET, eq["TP"], eq["FP"], eq["recall"], eq["precision"]))

    print("\n== Ablation — 축 하나를 빼면")
    abl = {}
    for g, cols in GROUPS.items():
        num = [f for f in NUM if f not in cols]
        cat = [f for f in CAT if f not in cols]
        r = evaluate(train, score(train, num, cat), m)
        r["delta_roc"] = round(r["roc_auc"] - full["roc_auc"], 4)
        abl[g] = r
        print("  -%-14s ROC-AUC %.4f (%+.4f)  PR-AUC %.4f" % (g, r["roc_auc"],
                                                             r["delta_roc"], r["pr_auc"]))

    print("\n== Single — 축 하나만 넣으면")
    sin = {}
    for g, cols in GROUPS.items():
        num = [f for f in NUM if f in cols]
        cat = [f for f in CAT if f in cols]
        r = evaluate(train, score(train, num, cat), m)
        sin[g] = r
        print("  %-15s ROC-AUC %.4f  PR-AUC %.4f" % (g, r["roc_auc"], r["pr_auc"]))

    print("\n== Shuffle — 축 하나를 행 단위로 섞으면 (%d회)" % N_SHUFFLE)
    shf = {}
    for g, cols in GROUPS.items():
        aucs = []
        for i in range(N_SHUFFLE):
            rng = np.random.default_rng(SEED + i)
            aucs.append(evaluate(train, score(train, NUM, CAT, rng, set(cols)), m)["roc_auc"])
        shf[g] = {"roc_auc_mean": round(float(np.mean(aucs)), 4),
                  "roc_auc_std": round(float(np.std(aucs)), 4),
                  "delta_roc": round(float(np.mean(aucs)) - full["roc_auc"], 4)}
        print("  ~%-14s ROC-AUC %.4f ± %.4f (%+.4f)"
              % (g, shf[g]["roc_auc_mean"], shf[g]["roc_auc_std"], shf[g]["delta_roc"]))

    rep = {
        "질문": "현재 feature 에 사람이 정의한 atypical_design 을 설명할 signal 이 있는가",
        "model": "OneClassSVM(rbf, nu=%.2f, gamma=%.1f) / StandardScaler" % (NU, GAMMA),
        "n_train": int(len(train)),
        "eval_set": {"n": int(len(m)), "positive": int(y.sum()),
                     "definition": "normal vs atypical_design (data_error·uncertain 제외)"},
        "full": full, "ablation": abl, "single": sin, "shuffle": shf,
        "not_used_for": "feature 선택. 여기 결과로 축을 고르면 hold-out 을 튜닝셋으로 쓰는 것이다",
    }
    C.save_report("m34_m3_diagnostics.json", rep)
    write_md(rep)


def write_md(r):
    full = r["full"]
    op = full["operating_2pct"]
    eq = full["equal_budget_top%d" % EQUAL_BUDGET]
    L = ["# M34 — 모델 3 feature 진단 (Ablation · Single · Shuffle)", "",
         "> 계획서 §9: \"넣은 모든 feature 가 의미가 없을 수 있다\" 는 가설을",
         "> 직접 검증합니다. 축을 하나씩 빼고, 하나씩만 넣고, 하나씩 섞습니다.", "",
         "```text", r["model"],
         "학습 %d행 / 주 평가셋 %d건 (atypical_design %d, normal %d)"
         % (r["n_train"], r["eval_set"]["n"], r["eval_set"]["positive"],
            r["eval_set"]["n"] - r["eval_set"]["positive"]),
         "```", "",
         "`data_error` 5건과 `uncertain` 10건은 주 평가에서 뺐습니다. 둘을 positive 로",
         "섞으면 파서 버그를 잡은 공이 이상탐지 성능으로 계산됩니다 — M30 이 정확히",
         "그 상태였습니다.", "",
         "## 1. Full", "",
         "| 지표 | 값 |", "|---|---|",
         "| ROC-AUC | **%.4f** (95%% 부트스트랩 구간 %.3f ~ %.3f) |"
         % (full["roc_auc"], full["roc_auc_ci95"][0], full["roc_auc_ci95"][1]),
         "| PR-AUC | %.4f (양성 비율 %.2f 가 무작위 기준선) |"
         % (full["pr_auc"], r["eval_set"]["positive"] / r["eval_set"]["n"]),
         "| 운영 경고율 2%% | 경고 %d건 · TP %d / FP %d / FN %d · recall %s |"
         % (op["n_flagged"], op["TP"], op["FP"], op["FN"], op["recall"]),
         "| 같은 경고예산 상위 %d건 | TP %d / FP %d · recall %s · precision %s |"
         % (EQUAL_BUDGET, eq["TP"], eq["FP"], eq["recall"], eq["precision"]), "",
         "M30 에서 같은 50건의 ROC-AUC 는 **0.48** 이었습니다. 파서를 고치고",
         "라벨을 다시 붙이자 순위가 처음으로 라벨 방향과 맞기 시작했습니다.",
         "다만 부트스트랩 구간이 넓습니다 — 양성 %d건짜리 표본입니다."
         % r["eval_set"]["positive"], "",
         "**운영 경고율 2%% 에서는 이 35건 중 %d건이 걸립니다.** 50건이 교체 *전*"
         % op["n_flagged"],
         "모델 점수로 층화 추출된 세트라, 교체 후 모델의 상위 2% 와는 자리가",
         "다릅니다. 그래서 같은 경고예산(상위 7건) 관점을 함께 냅니다.", "",
         "## 2. Ablation — 축 하나를 빼면", "",
         "빼도 ROC-AUC 가 거의 안 움직이면 그 축은 현재 모델에서 기여도가 낮습니다.", "",
         "| 뺀 축 | ROC-AUC | Full 대비 | PR-AUC |", "|---|---:|---:|---:|"]
    for g, v in sorted(r["ablation"].items(), key=lambda kv: kv[1]["delta_roc"]):
        L.append("| `-%s` | %.4f | %+.4f | %.4f |" % (g, v["roc_auc"], v["delta_roc"],
                                                     v["pr_auc"]))
    L += ["", "## 3. Single — 축 하나만 넣으면", "",
          "한 축만으로 Full 과 비슷하면 모델이 그 축 하나에 의존하고 있는 것입니다.", "",
          "| 축 | ROC-AUC | PR-AUC |", "|---|---:|---:|"]
    for g, v in sorted(r["single"].items(), key=lambda kv: -kv[1]["roc_auc"]):
        L.append("| `%s` | %.4f | %.4f |" % (g, v["roc_auc"], v["pr_auc"]))
    L += ["", "## 4. Shuffle — 축 하나를 행 단위로 섞으면 (%d회)" % N_SHUFFLE, "",
          "값의 분포는 그대로 두고 행과의 대응만 끊습니다. 성능이 그대로면 그 축은",
          "실제로 쓰이지 않는 것이고, 크게 떨어지면 핵심 signal 입니다.", "",
          "| 섞은 축 | ROC-AUC | Full 대비 |", "|---|---:|---:|"]
    for g, v in sorted(r["shuffle"].items(), key=lambda kv: kv[1]["delta_roc"]):
        L.append("| `~%s` | %.4f ± %.4f | %+.4f |"
                 % (g, v["roc_auc_mean"], v["roc_auc_std"], v["delta_roc"]))
    L += ["", "## 5. 이 표를 feature 선택에 쓰지 않습니다", "",
          "여기서 나온 축별 기여도를 보고 feature 를 고르면 hold-out 을 튜닝셋으로",
          "쓰는 것입니다(계획서 §11.2). 진단은 진단으로만 읽고, 최종 설정은 M16/M30 이",
          "합성 이상치로 이미 정한 `A_설계핵심` 을 그대로 씁니다.", ""]
    p = os.path.join(C.REPORTS, "m34_m3_diagnostics.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
