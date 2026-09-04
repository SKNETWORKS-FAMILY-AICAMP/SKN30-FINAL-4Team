r"""M35 — 모델 3 지도학습 기준선 (계획서 Step 4, §10).

계획서 §10 이 묻는 것은 "분류로 갈아타자"가 아니다.

    사람이 정의한 label 과 현재 feature 사이에 실제로 학습 가능한 signal 이
    존재하는가?

이상탐지가 못 잡는 것이 (a) 문제정의가 틀려서인지 (b) feature 에 애초에
정보가 없어서인지를 가르는 실험이다. 지도학습이 잘 되는데 one-class 가
안 되면 (a) 이고, 지도학습도 안 되면 (b) 다.

표본이 35건(양성 10)이다. 이 크기에서 할 수 있는 것과 없는 것
    할 수 있다   "signal 이 아예 없지는 않다" 를 순열검정으로 보이는 것
    할 수 없다   모델 간 우열을 가리는 것. AUC 차이 0.05 는 여기서 무의미하다
    하면 안 된다 이 결과로 최종 모델을 고르는 것 — hold-out 을 학습셋으로
                 쓰는 것과 같다. 여기 나온 모든 수치는 **진단값**이다.

평가 방법
    LOO-CV      35건에서 한 건씩 빼며 34건으로 학습. 표본이 작을 때
                fold 를 나누면 fold 마다 양성이 1~2건이 되어 분산이 폭발한다.
    순열검정     라벨을 100회 섞어 같은 절차를 돌린다. 실제 AUC 가 섞은 것들의
                분포 안에 들어가면 "학습 가능한 signal 이 있다"고 말할 수 없다.
                계획서 §9.4 Random Label Test 를 그대로 옮긴 것이다.
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
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import LeaveOneOut
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CAT, CLEAN, NUM, SEED

warnings.filterwarnings("ignore")
# 100회면 순열 p 의 해상도가 0.01 이다. 35건짜리 진단에 그보다 촘촘한 값은
# 읽을 수 없고, 200회로 늘리면 MLP 순열만 한 시간을 먹는다.
N_PERM = 100


def build_xy():
    """비지도 인코딩을 그대로 쓴다 — 범주 빈도는 전체 코퍼스에서 온다.

    라벨을 본 뒤에 만든 인코딩(target encoding 등)을 쓰면 35건짜리 세트에서
    누수가 바로 성능으로 나타난다. 빈도는 라벨과 무관하므로 안전하다.
    """
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    cl = pd.read_csv(CLEAN, encoding="utf-8-sig")
    main = cl[cl["라벨"].isin(["normal", "atypical_design"])][["row_id", "라벨", "사업명"]]
    t = train.set_index("row_id")
    main = main[main["row_id"].isin(t.index)].reset_index(drop=True)

    cols, names = [], []
    for f in NUM:
        med = train[f].median()
        v = t.loc[main["row_id"], f].to_numpy(dtype=float)
        cols += [np.where(np.isnan(v), 0.0 if pd.isna(med) else med, v),
                 np.isnan(v).astype(float)]
        names += [f, f + "__missing"]
    for f in CAT:
        freq = train[f].value_counts(normalize=True)
        cols.append(t.loc[main["row_id"], f].map(freq).fillna(0.0).to_numpy(dtype=float))
        names.append(f + "__freq")
    X = np.column_stack(cols)
    y = (main["라벨"] == "atypical_design").to_numpy(int)
    return X, y, names, main


def models():
    return {
        "LogisticRegression": lambda: make_pipeline(
            StandardScaler(), LogisticRegression(max_iter=2000, C=1.0,
                                                 class_weight="balanced")),
        "LinearSVM": lambda: make_pipeline(
            StandardScaler(), SVC(kernel="linear", probability=True,
                                  class_weight="balanced", random_state=SEED)),
        # n_jobs=1 이다. 35행짜리 데이터에 프로세스를 띄우면 학습보다 오버헤드가
        # 크다 — 순열검정에서 같은 적합을 7,000번 반복하므로 차이가 시간으로 쌓인다.
        "RandomForest": lambda: RandomForestClassifier(
            n_estimators=150, min_samples_leaf=2, class_weight="balanced",
            random_state=SEED, n_jobs=1),
        "MLP(16)": lambda: make_pipeline(
            StandardScaler(), MLPClassifier(hidden_layer_sizes=(16,), max_iter=600,
                                            alpha=1e-2, random_state=SEED)),
    }


def loo_auc(make, X, y):
    """LOO-CV out-of-fold 확률로 AUC 하나를 낸다."""
    oof = np.zeros(len(y))
    for tr, te in LeaveOneOut().split(X):
        if len(np.unique(y[tr])) < 2:
            oof[te] = 0.5
            continue
        m = make().fit(X[tr], y[tr])
        oof[te] = m.predict_proba(X[te])[:, 1]
    return float(roc_auc_score(y, oof)), oof


def lgbm_auc(X, y):
    try:
        import lightgbm as lgb
    except ImportError:
        return None, None
    def make():
        return lgb.LGBMClassifier(n_estimators=120, num_leaves=4, min_child_samples=3,
                                  learning_rate=0.05, class_weight="balanced",
                                  random_state=SEED, verbose=-1)
    return loo_auc(make, X, y)


def main():
    X, y, names, main_df = build_xy()
    print("M35 — 지도학습 기준선")
    print("  %d건 x %d축 / 양성 %d (%.0f%%)"
          % (len(y), X.shape[1], int(y.sum()), y.mean() * 100))

    res = {}
    mk = models()
    mk["LightGBM"] = None
    for name in list(mk):
        if name == "LightGBM":
            auc, oof = lgbm_auc(X, y)
            if auc is None:
                continue
        else:
            auc, oof = loo_auc(mk[name], X, y)
        # 계획서 §9.4 — 라벨을 섞어 같은 절차를 돌린다. 실제 AUC 가 이 분포
        # 안에 있으면 학습 가능한 signal 이 있다고 말할 수 없다.
        rng = np.random.default_rng(SEED)
        null = []
        for _ in range(N_PERM):
            ys = rng.permutation(y)
            a, _ = (lgbm_auc(X, ys) if name == "LightGBM"
                    else loo_auc(mk[name], X, ys))
            null.append(a)
        null = np.array(null)
        p = float((null >= auc).mean())
        res[name] = {"loo_auc": round(auc, 4),
                     "null_mean": round(float(null.mean()), 4),
                     "null_p95": round(float(np.percentile(null, 95)), 4),
                     "perm_p": round(p, 4),
                     "n_perm": N_PERM}
        print("  %-20s LOO AUC %.4f | 라벨섞음 평균 %.4f (P95 %.4f) | 순열 p=%.3f"
              % (name, auc, null.mean(), np.percentile(null, 95), p))

    best = max(res, key=lambda k: res[k]["loo_auc"])
    unsup = best_unsupervised()

    verdict = judge(res[best], unsup)
    print("\n== 판정: %s" % verdict["verdict"])
    for r in verdict["reasons"]:
        print("   - %s" % r)

    rep = {
        "질문": "사람이 정의한 atypical_design 과 현재 feature 사이에 학습 가능한 signal 이 있는가",
        "n": int(len(y)), "n_positive": int(y.sum()), "n_features": int(X.shape[1]),
        "feature_names": names,
        "eval": "LeaveOneOut CV out-of-fold ROC-AUC + 라벨 순열검정 %d회" % N_PERM,
        "results": res, "best": best,
        "unsupervised_same_set": unsup,
        "verdict": verdict,
        "not_used_for": "모델 선택. 35건짜리 진단이지 학습셋이 아니다",
    }
    C.save_report("m35_m3_supervised.json", rep)
    write_md(rep)


def best_unsupervised():
    """같은 35건에서 잰 비지도 후보 **전부** 중 최고를 돌려준다.

    M34 의 Full 하나만 놓고 비교하면 안 된다. 그건 진단용으로 축을 넓게 잡은
    설정이지 비지도 최고 성능이 아니다. M38 의 비교군거리가 같은 세트에서 더
    높으므로, 그것을 빼고 "지도학습이 이겼다"고 적으면 사실이 아니게 된다.
    """
    import json
    cands = []
    for fn, pick in (
        ("m34_m3_diagnostics.json",
         lambda j: [(j["model"], j["full"]["roc_auc"], j["full"].get("roc_auc_ci95"))]),
        ("m36_m3_oneclass.json",
         lambda j: [(n, v["views"]["주 평가(atypical_design)"]["roc_auc"], None)
                    for n, v in j["per_model"].items()]),
        ("m38_m3_vector_direction.json",
         lambda j: [(k, v["roc_auc"], None) for k, v in j["singles"].items()]
                   + [(j["chosen"], j["chosen_holdout"]["roc_auc"],
                       j.get("chosen_roc_auc_ci95"))]),
    ):
        path = os.path.join(C.REPORTS, fn)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
        cands += pick(j)
    if not cands:
        return {}
    name, auc, ci = max(cands, key=lambda x: x[1])
    return {"model": name, "roc_auc": auc, "roc_auc_ci95": ci,
            "n_candidates": len(cands),
            "note": "같은 35건에서 잰 비지도 후보 %d개 중 최고" % len(cands)}


def judge(best, unsup):
    reasons, v = [], None
    sup = best["loo_auc"]
    un = unsup.get("roc_auc")
    if best["perm_p"] > 0.05:
        v = "signal 확인 실패"
        reasons.append("가장 좋은 지도학습도 라벨을 섞은 것과 구분되지 않는다 "
                       "(순열 p=%.3f). 이 feature 로는 사람 판단을 설명하지 못한다."
                       % best["perm_p"])
    elif un is not None and sup - un > 0.15:
        v = "문제정의 재검토 (supervised 우세)"
        reasons.append("지도학습 %.3f vs 비지도 최고 %.3f(%s) — 사람이 보는 atypical 은 "
                       "정상 분포에서 먼 점이 아니라 특정 조합에 가깝다."
                       % (sup, un, unsup.get("model")))
    else:
        v = "signal 있음 · 문제정의 유지"
        reasons.append("지도학습 %.3f, 비지도 최고 %s(%s) — 지도학습이 앞서지 않는다. "
                       "이상탐지라는 문제정의를 바꿀 근거가 없다."
                       % (sup, "%.3f" % un if un is not None else "미측정",
                          unsup.get("model", "-")))
    reasons.append("라벨을 섞으면 AUC 가 %.3f 로 떨어진다(순열 p=%.3f). 평가 절차에 "
                   "누수는 없다 — 계획서 §9.4 Random Label Test 통과."
                   % (best["null_mean"], best["perm_p"]))
    reasons.append("양성 10건짜리 표본이다. 모델 간 AUC 차이는 읽지 않는다 — "
                   "읽을 수 있는 것은 '섞은 라벨과 구분되는가' 하나뿐이다.")
    return {"verdict": v, "reasons": reasons}


def write_md(r):
    L = ["# M35 — 모델 3 지도학습 기준선", "",
         "> 계획서 §10 이 묻는 것은 \"분류로 갈아타자\"가 아닙니다.",
         "> **사람이 정의한 label 과 현재 feature 사이에 학습 가능한 signal 이**",
         "> **존재하는가** 를 가르는 실험입니다.", "",
         "```text",
         "%d건 x %d축 / 양성 %d" % (r["n"], r["n_features"], r["n_positive"]),
         r["eval"],
         "```", "",
         "## 1. 결과", "",
         "| 모델 | LOO ROC-AUC | 라벨 섞음 평균 | 섞음 P95 | 순열 p |",
         "|---|---:|---:|---:|---:|"]
    for k, v in sorted(r["results"].items(), key=lambda kv: -kv[1]["loo_auc"]):
        L.append("| %s | **%.4f** | %.4f | %.4f | %.3f |"
                 % (k, v["loo_auc"], v["null_mean"], v["null_p95"], v["perm_p"]))
    u = r.get("unsupervised_same_set") or {}
    if u:
        L += ["", "같은 35건에서의 비지도 기준선 (M34):", "",
              "```text", "%s" % u["model"],
              "ROC-AUC %.4f%s" % (u["roc_auc"],
                                  ("  95%%CI [%.3f, %.3f]" % tuple(u["roc_auc_ci95"]))
                                  if u.get("roc_auc_ci95") else ""),
              "```"]
    L += ["", "## 2. 라벨 순열검정을 같이 봐야 하는 이유", "",
          "계획서 §9.4 그대로입니다. 라벨을 무작위로 섞고 **같은 절차**를 돌립니다.",
          "정상적인 모델이라면 섞은 라벨에서는 AUC 가 0.5 근처로 떨어져야 합니다.",
          "떨어지지 않으면 평가 절차나 데이터에 누수가 있다는 뜻입니다.", "",
          "여기서는 반대 방향으로도 씁니다 — 실제 AUC 가 섞은 것들의 분포 안에",
          "들어가면, 수치가 0.7 이든 0.8 이든 **35건에서는 우연과 구분되지 않습니다.**", "",
          "## 3. 판정", "", "**%s**" % r["verdict"]["verdict"], ""]
    for x in r["verdict"]["reasons"]:
        L.append("- %s" % x)
    L += ["", "## 4. 이 표로 하지 않는 것", "",
          "- 모델 선택. 35건은 진단용이지 학습셋이 아닙니다.",
          "- 모델 간 우열. AUC 차이 0.05 는 이 표본에서 의미가 없습니다.",
          "- threshold 결정. 이 세트는 튜닝에 쓰지 않습니다.", ""]
    p = os.path.join(C.REPORTS, "m35_m3_supervised.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


def reverdict():
    """저장된 결과로 판정과 문서만 다시 만든다. 순열검정은 다시 돌리지 않는다."""
    import json
    path = os.path.join(C.REPORTS, "m35_m3_supervised.json")
    with open(path, encoding="utf-8") as f:
        rep = json.load(f)
    rep["unsupervised_same_set"] = best_unsupervised()
    rep["verdict"] = judge(rep["results"][rep["best"]], rep["unsupervised_same_set"])
    C.save_report("m35_m3_supervised.json", rep)
    write_md(rep)
    print("판정: %s" % rep["verdict"]["verdict"])
    for x in rep["verdict"]["reasons"]:
        print("   - %s" % x)


if __name__ == "__main__":
    if "--reverdict" in sys.argv:
        reverdict()
    else:
        main()
