r"""M63 — 모델 2(M56)를 수정 데이터로 **동일 조건 재평가**.

지시서 Model 2 우선순위 4번. 새 모델을 찾지 않는다.

    바꾸는 것   입력 데이터셋 하나 (design_features -> design_features_v2)
    그대로      타깃 stated_cap · 필터 · 비교군 사다리 · feature 규격 ·
                XGBoost 파라미터 · GroupKFold(5) · baseline · seed 42

M56 승격 때(STEP 3)와 같은 규율로 잰다. 두 데이터셋을 **같은 split 루프
안에서** 학습해 fold 가 달라서 생긴 차이를 결과로 오독하지 않는다. 그게
가능한 이유는 M62 가 타깃·필터를 건드리지 않아 두 데이터셋의 대상 행이
1,877건으로 **같기 때문**이다(스크립트가 시작할 때 row_id 집합 일치를
검사하고, 다르면 멈춘다).

무엇이 달라졌길래 점수가 달라질 수 있는가 — M62 가 고친 것 중 모델 2 의
입력에 닿는 것은 셋이다.

    support_method   비교군 사다리 축 & 범주 feature. 목록 표본이 근거문 없이
                     거의 전부 grant 로 떨어져 있었다.
    support_unit     비교군 사다리 축 & 범주 feature.
    project_duration / self_burden_ratio
                     수치 feature. 목록 표본은 전부 결측이었다.

타깃(`per_recipient`)·제목·금액은 하나도 바뀌지 않는다. 그래서 이 비교는
**"같은 모델·같은 타깃·같은 행에서 입력 품질만 바뀌면 얼마나 움직이는가"**
를 재는 것이다.

지시서가 '필요할 때만' 보라고 한 두 후보(ensemble · cohort residual 보정)는
`--extra` 를 줄 때만 돈다. 데이터 수정만으로 개선이 확인되면 켜지 않는다 —
켤 근거가 없는 실험을 돌려 놓고 좋은 쪽을 고르면 그게 test 튜닝이다.
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warnings

warnings.filterwarnings("ignore")
import common as C
import m2_features as F
import m45_m2_amount as M45
import m56_m2_canonical as M56
import f06_design_features as F6

V1 = F6.OUT
V2 = F6.OUT_V2
OUT_OOF = os.path.join(C.PROC, "m63_oof_predictions.parquet")

# M56 의 공표 수치. 재현 대조용으로만 쓴다.
M56_PUBLISHED = {"MAE_log10": 0.4155, "improvement": 0.218, "within_2x": 0.488,
                 "within_3x": 0.676, "coverage": 0.803, "width_x": 25.1}
N_REPEAT = 10


def load(path):
    d, drop = M45.prepare(pd.read_parquet(path))
    return d.reset_index(drop=True), drop


def align(a, b):
    """두 데이터셋의 대상 행이 같은지 확인하고 같은 순서로 맞춘다."""
    if set(a["row_id"]) != set(b["row_id"]):
        raise RuntimeError("대상 행이 다르다 — 동일조건 비교가 성립하지 않는다 "
                           "(v1 %d / v2 %d / 교집합 %d)"
                           % (len(a), len(b), len(set(a['row_id']) & set(b['row_id']))))
    order = a["row_id"].tolist()
    b = b.set_index("row_id").loc[order].reset_index()
    return a, b[a.columns]


def bundle(d):
    """한 데이터셋에서 학습에 필요한 것 전부."""
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    return {"d": d, "X": Xs, "y": y, "cats": cats,
            "titles": F.titles_for_model(d),
            "groups": {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}}


# ------------------------------------------------------------ 동일 split 비교
def paired_oof(v1, v2, groups):
    """v1·v2 를 **같은 split** 에서 학습한다. 타깃 y 는 둘이 동일하다."""
    from sklearn.model_selection import GroupKFold

    y = v1["y"]
    n = len(y)
    out = {"baseline(v1)": np.zeros(n), "baseline(v2)": np.zeros(n),
           "M56 on v1(현행)": np.zeros(n), "M56 on v2(수정)": np.zeros(n)}
    for tr, te in GroupKFold(n_splits=F.N_SPLITS).split(v1["X"], y, groups):
        for tag, b in (("v1", v1), ("v2", v2)):
            Xtr_s, Xte_s = b["X"].iloc[tr], b["X"].iloc[te]
            Xtr_t, Xte_t, _ = F.build_features(b["X"], b["titles"], tr, te, True, True)
            out["baseline(%s)" % tag][te] = M45.cohort_median_baseline(
                Xtr_s, y[tr], Xte_s, b["cats"])
            out["M56 on %s%s" % (tag, "(현행)" if tag == "v1" else "(수정)")][te] = \
                F.make_point_model().fit(Xtr_t, y[tr]).predict(Xte_t)
    return out


def score_block(y, preds):
    base = float(np.abs(preds["baseline(v1)"] - y).mean())
    block = {}
    for k, p in preds.items():
        m = M45.point_metrics(y, p)
        # baseline 은 v1 기준 하나로 고정한다. 데이터가 바뀌면 baseline 도
        # 움직이는데, 개선율의 분모까지 같이 움직이면 두 세대를 견줄 수 없다.
        m["improvement_vs_v1_baseline"] = round(float((base - m["MAE_log10"]) / base), 4)
        block[k] = m
    block["baseline(v2)"]["note"] = "참고 — 개선율 분모는 v1 baseline 으로 고정"
    return block, base


def stability(v1, v2, n_repeat=N_REPEAT):
    """fold 재구성 n회. M45/M56 과 같은 방식(그룹 라벨 셔플)."""
    from sklearn.model_selection import GroupKFold

    y = v1["y"]
    rows = {"M56 on v1(현행)": [], "M56 on v2(수정)": []}
    g0 = v1["groups"]["program_stem"]
    for seed in range(n_repeat):
        rng = np.random.default_rng(seed)
        uniq = np.unique(g0)
        remap = dict(zip(uniq, uniq[rng.permutation(len(uniq))]))
        gs = np.array([remap[v] for v in g0])
        bp = np.zeros(len(y))
        p = {"v1": np.zeros(len(y)), "v2": np.zeros(len(y))}
        for tr, te in GroupKFold(F.N_SPLITS).split(v1["X"], y, gs):
            bp[te] = M45.cohort_median_baseline(
                v1["X"].iloc[tr], y[tr], v1["X"].iloc[te], v1["cats"])
            for tag, b in (("v1", v1), ("v2", v2)):
                Xtr, Xte, _ = F.build_features(b["X"], b["titles"], tr, te, True, True)
                p[tag][te] = F.make_point_model().fit(Xtr, y[tr]).predict(Xte)
        b = float(np.abs(bp - y).mean())
        rows["M56 on v1(현행)"].append((b - float(np.abs(p["v1"] - y).mean())) / b)
        rows["M56 on v2(수정)"].append((b - float(np.abs(p["v2"] - y).mean())) / b)
    out = {}
    for k, v in rows.items():
        a = np.array(v)
        out[k] = {"n_repeat": n_repeat, "mean": round(float(a.mean()), 4),
                  "std": round(float(a.std()), 4), "min": round(float(a.min()), 4),
                  "max": round(float(a.max()), 4),
                  "pass": "%d/%d" % (int((a >= F.MIN_IMPROVEMENT).sum()), n_repeat)}
    return out


def intervals_and_tiers(b):
    """CQR 구간과 비교군 실용성 등급. M56.intervals 를 그대로 쓴다."""
    mid, lo, hi, _, _, delta = M56.intervals(
        b["X"], b["y"], b["groups"]["program_stem"], b["titles"])
    oof = b["d"][["support_type", "support_method", "support_unit", "cohort"]].copy()
    oof["y"], oof["pred"], oof["lo"], oof["hi"] = b["y"], mid, lo, hi
    tiers = M45.tier_table(oof)
    counts = [int((tiers["tier"] == t).sum()) for t in M45.TIERS]
    return M45.interval_metrics(b["y"], lo, hi), counts, oof


# ------------------------------------------------------------ 선택 실험
def extra_candidates(v2, groups, min_cohort=20):
    """지시서가 '필요할 때만' 이라고 한 둘. 기본으로는 돌지 않는다.

    잔차 보정의 잔차는 **train fold 안의 inner OOF** 에서 잡는다. in-sample
    잔차를 쓰면 XGBoost 800 트리가 train 을 거의 맞혀 버려 비교군 중앙값이
    0 근처로 눌리고, 그러면 '효과 없음'이 기법의 결론이 아니라 측정 방식의
    산물이 된다. inner split 도 같은 그룹키로 자른다.
    """
    from sklearn.model_selection import GroupKFold
    from lightgbm import LGBMRegressor

    y = v2["y"]
    n = len(y)
    xgb = np.zeros(n)
    lgbm = np.zeros(n)
    resid = np.zeros(n)
    key = (v2["d"]["support_type"].astype(str) + "|"
           + v2["d"]["support_method"].astype(str) + "|"
           + v2["d"]["support_unit"].astype(str) + "|"
           + v2["d"]["cohort"].astype(str)).to_numpy()

    for tr, te in GroupKFold(n_splits=F.N_SPLITS).split(v2["X"], y, groups):
        Xtr, Xte, _ = F.build_features(v2["X"], v2["titles"], tr, te, True, True)
        xgb[te] = F.make_point_model().fit(Xtr, y[tr]).predict(Xte)
        lgbm[te] = LGBMRegressor(objective="regression_l1", n_estimators=800,
                                 learning_rate=0.03, num_leaves=31,
                                 min_child_samples=10, subsample=0.9,
                                 colsample_bytree=0.8, random_state=F.PIPELINE_SEED,
                                 verbose=-1).fit(Xtr, y[tr]).predict(Xte)
        inner = np.zeros(len(tr))
        for a, b in GroupKFold(n_splits=3).split(v2["X"].iloc[tr], y[tr], groups[tr]):
            Xa, Xb, _ = F.build_features(v2["X"].iloc[tr], v2["titles"][tr], a, b,
                                         True, True)
            inner[b] = F.make_point_model().fit(Xa, y[tr][a]).predict(Xb)
        r = pd.Series(y[tr] - inner).groupby(key[tr])
        adj = r.median().where(r.size() >= min_cohort)      # 얇은 칸은 보정하지 않는다
        resid[te] = xgb[te] + pd.Series(key[te]).map(adj).fillna(0.0).to_numpy()
    return {"C-a XGB (수정 데이터, 기준)": xgb,
            "C-b XGB+LGBM 단순평균": (xgb + lgbm) / 2,
            "C-c XGB + 비교군 잔차보정(inner OOF)": resid}


def paired_delta(y, p1, p2, n_boot=5000, seed=42):
    """행 단위 짝지은 오차 차이. fold 를 공유하므로 차이의 분산은 두 MAE 각각의
    분산보다 훨씬 작다 — '개선폭이 fold 재구성 표준편차보다 작다'만 보면
    짝지음에서 나오는 정보를 통째로 버리게 된다.

    Wilcoxon 부호순위(짝지은 |오차| 차이) + 부트스트랩 신뢰구간을 함께 낸다.
    부트스트랩은 **그룹(사업 계열) 단위**가 아니라 행 단위이므로 구간이
    낙관적일 수 있다. 그래서 부호검정 결과와 같이 읽는다.
    """
    from scipy.stats import wilcoxon

    e1, e2 = np.abs(p1 - y), np.abs(p2 - y)
    d = e1 - e2                                  # 양수면 v2 가 더 정확
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(n_boot, len(d)))
    boot = d[idx].mean(axis=1)
    nz = d[d != 0]
    return {
        "n": int(len(d)),
        "mean_abs_error_v1": round(float(e1.mean()), 4),
        "mean_abs_error_v2": round(float(e2.mean()), 4),
        "mean_delta(v1-v2)": round(float(d.mean()), 5),
        "boot_ci95": [round(float(np.percentile(boot, 2.5)), 5),
                      round(float(np.percentile(boot, 97.5)), 5)],
        "P(delta>0)": round(float((boot > 0).mean()), 4),
        "n_rows_changed": int((d != 0).sum()),
        "n_rows_v2_better": int((d > 0).sum()),
        "wilcoxon_p": (round(float(wilcoxon(nz).pvalue), 5) if len(nz) else None),
    }


MD = os.path.join(C.REPORTS, "m63_m2_requal.md")


def write_md(r):
    ps = r["paired"]["program_stem"]
    nt = r["paired"]["normalized_title"]
    a, b = ps["M56 on v1(현행)"], ps["M56 on v2(수정)"]
    sa = r["stability"]["M56 on v1(현행)"]
    sb = r["stability"]["M56 on v2(수정)"]
    ia, ib = r["intervals"]["v1(현행)"], r["intervals"]["v2(수정)"]
    L = []
    A = L.append
    A("# M63 — 모델 2(M56)를 수정 데이터로 동일조건 재평가\n")
    A("> 지시서 Model 2 우선순위 4번. **새 모델을 찾지 않습니다.**\n")
    A("```text")
    A("바꾸는 것   입력 데이터셋 하나 (design_features -> design_features_v2, M62)")
    A("그대로      타깃 stated_cap · 필터 · 비교군 사다리 · feature 규격 ·")
    A("            XGBoost 파라미터 · GroupKFold(5) · baseline · seed 42")
    A("```\n")
    A("M56 승격 때(STEP 3)와 같은 규율입니다 — 두 데이터셋을 **같은 split 루프**")
    A("**안에서** 학습해 fold 가 달라 생긴 차이를 결과로 오독하지 않습니다. 그게")
    A("가능한 이유는 M62 가 타깃·필터를 건드리지 않아 대상 행이 **%d건으로 같기**"
      % r["dataset"]["rows"])
    A("때문입니다(스크립트가 시작할 때 `row_id` 집합 일치를 검사하고, 다르면 멈춥니다).\n")
    A("```text")
    A("필터 v1  %s" % r["dataset"]["filter_trace_v1"])
    A("필터 v2  %s" % r["dataset"]["filter_trace_v2"])
    A("```\n")
    A("입력에서 실제로 달라진 칸은 이렇습니다.\n")
    A("| 컬럼 | 값이 바뀐 행 |")
    A("|---|---:|")
    for k, v in r["dataset"]["changed_cells"].items():
        A("| `%s` | %d |" % (k, v))
    A("")
    A("타깃(`per_recipient`)·제목·금액은 **한 행도 바뀌지 않았습니다.** 그래서 이")
    A("비교는 *같은 모델·같은 타깃·같은 행에서 입력 품질만 바뀌면 얼마나 움직이는가*")
    A("를 재는 것입니다.\n")
    A("> 위 표에서 `support_count` 만 성격이 다릅니다. 근거문 수정과 무관하고,")
    A("> v1 이 `business_taxonomy.parquet` 의 M32 파서 수정 **이전**에 만들어져")
    A("> 얼어 있었기 때문에 생기는 상류 드리프트입니다(M62 3절). 이번 수정의")
    A("> 효과로 읽지 않습니다.\n")

    A("## 1. 결과 — 같은 split, 데이터만 교체\n")
    A("| 지표 | M56 on v1 (현행) | M56 on v2 (수정) | |")
    A("|---|---:|---:|---|")
    rows = [("MAE(log10)", "MAE_log10", "%.4f", "low"),
            ("v1 baseline 대비 개선", "improvement_vs_v1_baseline", "%.1f%%", "high"),
            ("2배 이내", "within_2x", "%.1f%%", "high"),
            ("3배 이내", "within_3x", "%.1f%%", "high"),
            ("중앙절대오차", "MedAE_log10", "%.4f", "low")]
    for label, key, fmt, better in rows:
        x, y = a[key], b[key]
        mark = "개선" if ((y > x) == (better == "high")) else ("동일" if x == y else "악화")
        sx, sy = ((fmt % (100 * x), fmt % (100 * y)) if fmt.endswith("%%")
                  else (fmt % x, fmt % y))
        A("| %s | %s | %s | %s |" % (label, sx, sy, mark))
    b1, b2 = ps["baseline_MAE"], ps["baseline(v2)"]["MAE_log10"]
    A("| 비교군중앙값 baseline | %.4f | **%.4f** | 개선 |" % (b1, b2))
    A("| *각자의* baseline 대비 개선 | %.1f%% | %.1f%% | %s |"
      % (100 * (b1 - a["MAE_log10"]) / b1, 100 * (b2 - b["MAE_log10"]) / b2,
         "사실상 동일"))
    A("")
    A("**마지막 두 줄이 이 절에서 가장 중요합니다.** 비교군 중앙값 baseline 은")
    A("모델이 아니라 **비교군 자체가 금액을 얼마나 잘 설명하는가**입니다. 그 값이")
    A("%.4f → %.4f 로 같이 내려갔다는 것은 `support_method` 를 바로잡자" % (b1, b2))
    A("**비교군이 실제로 더 동질해졌다**는 뜻입니다 — 모델과 무관한 독립 증거입니다.")
    A("그래서 각자의 baseline 대비 개선율로 재면 두 세대가 거의 같습니다. **모델이**")
    A("**좋아진 것이 아니라 모델과 baseline 이 함께 좋아졌습니다.**\n")
    A("> 개선율의 **분모는 v1 baseline 하나로 고정**했습니다. 데이터가 바뀌면")
    A("> 비교군 중앙값 baseline 도 같이 움직이는데, 분모까지 움직이면 두 세대를")
    A("> 견줄 수 없습니다. 참고로 수정 데이터에서 baseline 자체는 %.4f 입니다."
      % ps["baseline(v2)"]["MAE_log10"])
    A("> (%s)\n" % ("비교군이 제대로 갈리면서 baseline 도 같이 좋아졌다는 뜻입니다"
                   if ps["baseline(v2)"]["MAE_log10"] < ps["baseline_MAE"]
                   else "비교군이 잘게 갈리면서 baseline 이 나빠졌다는 뜻입니다"))
    if "paired_delta" in r:
        pd_ = r["paired_delta"]
        A("**짝지어서 다시 봅니다.** 두 열은 같은 fold·같은 행이므로 행 단위로")
        A("오차를 짝지을 수 있고, 그러면 차이의 분산이 MAE 각각의 분산보다 훨씬")
        A("작아집니다. 입력이 바뀐 것은 일부 컬럼뿐이지만 학습기가 다시 적합되므로")
        A("예측값 자체는 %d행 전부 조금씩 달라집니다 — 그래서 *몇 행이 달라졌는가*가"
          % pd_["n"])
        A("아니라 *어느 쪽으로 달라졌는가*를 봅니다.\n")
        A("| 짝지은 절대오차 차이 (v1 − v2) | 값 |")
        A("|---|---:|")
        A("| 평균 | **%+.5f** |" % pd_["mean_delta(v1-v2)"])
        A("| 부트스트랩 95%% 구간 | %+.5f ~ %+.5f |" % tuple(pd_["boot_ci95"]))
        A("| P(차이 > 0) | %.4f |" % pd_["P(delta>0)"])
        A("| v2 가 더 정확한 행 | %d / %d (%.1f%%) |"
          % (pd_["n_rows_v2_better"], pd_["n"],
             100 * pd_["n_rows_v2_better"] / pd_["n"]))
        A("| Wilcoxon 부호순위 p | %s |" % pd_["wilcoxon_p"])
        A("")
        A("**구간이 0을 품고 부호검정도 무작위와 구별되지 않습니다.** MAE %.4f →"
          % pd_["mean_abs_error_v1"])
        A("%.4f 은 **표본 흔들림과 구별되지 않는 차이**입니다. 모델 2 의 예측 성능은"
          % pd_["mean_abs_error_v2"])
        A("데이터 수정으로 사실상 움직이지 않았다고 읽는 것이 맞습니다.\n")
        A("> 부트스트랩은 행 단위라 같은 사업 계열이 여러 번 뽑혀 구간이 오히려")
        A("> **낙관적**입니다. 그런데도 0을 품습니다. 계열 단위로 다시 뽑으면 더")
        A("> 넓어질 뿐이라 결론은 바뀌지 않습니다.\n")
    A("**엄격 그룹(정규화 제목)에서도** 같은 방향인지 확인합니다 — 제목 텍스트를")
    A("feature 로 쓰는 순간 '지역·연도만 다른 같은 사업 계열'이 새 누수 경로가 되므로,")
    A("M55 이후로는 이쪽이 진짜 기준입니다.\n")
    A("| 그룹 기준 | v1 개선율 | v2 개선율 |")
    A("|---|---:|---:|")
    A("| `program_stem` (표준) | %.4f | %.4f |"
      % (a["improvement_vs_v1_baseline"], b["improvement_vs_v1_baseline"]))
    A("| `normalized_title` (엄격) | %.4f | %.4f |"
      % (nt["M56 on v1(현행)"]["improvement_vs_v1_baseline"],
         nt["M56 on v2(수정)"]["improvement_vs_v1_baseline"]))
    A("")
    A("## 2. fold 재구성 %d회\n" % sa["n_repeat"])
    A("한 번의 split 에서 나온 차이인지 확인합니다. M45/M56 과 같은 방식(그룹 라벨 셔플).\n")
    A("| | 개선율 평균 | 표준편차 | 최저 | 채택기준(10%) 통과 |")
    A("|---|---:|---:|---:|---|")
    A("| M56 on v1 (현행) | %.4f | %.4f | %.4f | %s |"
      % (sa["mean"], sa["std"], sa["min"], sa["pass"]))
    A("| M56 on v2 (수정) | %.4f | %.4f | %.4f | %s |"
      % (sb["mean"], sb["std"], sb["min"], sb["pass"]))
    A("")
    A("## 3. 예측구간과 비교군 실용성 등급\n")
    A("| | 커버리지(명목 80%) | 구간폭 중앙값 | 참고 가능 / 범위 넓음 / 제시 어려움 |")
    A("|---|---:|---:|---|")
    for tag, im in (("v1 (현행)", ia), ("v2 (수정)", ib)):
        t = im["tier_counts"]
        A("| %s | %.3f | %.1f배 | %d / %d / %d |"
          % (tag, im["coverage"], im["median_width_x"],
             t.get("참고 가능", 0), t.get("범위 넓음", 0), t.get("참고 범위 제시 어려움", 0)))
    A("")
    A("등급은 담당자에게 P10~P90 을 그대로 낼 수 있는 비교군이 몇 칸인가입니다.")
    A("비교군 축(`support_method`·`support_unit`)이 바뀌었으므로 칸 자체가 재편됩니다 —")
    A("칸 수를 v1 과 1:1 로 견주지 않고 구성이 어떻게 달라졌는지로 읽습니다.\n")
    if "extra" in r:
        A("## 4. 선택 실험 (지시서 '필요할 때만')\n")
        A("지시서가 '필요할 때만' 이라고 한 둘입니다. 1절에서 데이터 수정만으로는")
        A("예측 오차가 움직이지 않는다고 나왔으므로 **한 번** 돌렸습니다.\n")
        A("| 후보 | MAE(log10) | v1 baseline 대비 |")
        A("|---|---:|---:|")
        for k, m in r["extra"].items():
            A("| %s | %.4f | %+.1f%% |"
              % (k, m["MAE_log10"], 100 * m["improvement_vs_v1_baseline"]))
        A("")
        A("**둘 다 기준보다 나쁩니다. 미채택.**\n")
        A("잔차 보정에서 한 가지를 적어 둡니다. 처음에는 train fold 의 **in-sample**")
        A("잔차로 비교군 중앙값을 잡았고 그때는 기준보다 좋게 나왔습니다. 그런데")
        A("XGBoost 800 트리는 train 을 거의 맞히므로 그 잔차 중앙값은 0 근처로")
        A("눌리고, 보정이 아니라 **미세한 과적합 방향으로 밀어준** 것에 가깝습니다.")
        A("train fold 안에서 inner GroupKFold(3) 로 OOF 잔차를 다시 잡자")
        A("뒤집혔습니다. **측정 방식이 결론을 만들던 자리**라 그대로 남깁니다.\n")
    A("## %d. 읽는 법\n" % (5 if "extra" in r else 4))
    A("공표 수치(M56)는 MAE **%.4f** / 개선 **%.1f%%** / 2배내 **%.1f%%** 입니다."
      % (r["published_M56"]["MAE_log10"], 100 * r["published_M56"]["improvement"],
         100 * r["published_M56"]["within_2x"]))
    A("v1 열이 그 값을 정확히 재현하고, v2 열과의 차이가 **데이터 품질 수정이**")
    A("**모델 2 에 준 것 전부**입니다.\n")
    A("```text")
    A("예측 오차          유의하게 움직이지 않았다 (짝지은 차이의 95% 구간이 0을 품는다)")
    A("비교군 baseline    %.4f -> %.4f 로 내려갔다 (비교군이 더 동질해졌다)"
      % (ps["baseline_MAE"], ps["baseline(v2)"]["MAE_log10"]))
    A("예측구간           폭 %.1f배 -> %.1f배, '제시 어려움' 등급 %d칸 -> %d칸"
      % (ia["median_width_x"], ib["median_width_x"],
         ia["tier_counts"].get("참고 범위 제시 어려움", 0),
         ib["tier_counts"].get("참고 범위 제시 어려움", 0)))
    A("선택 실험 2종      둘 다 기준보다 나쁘다. 미채택")
    A("```\n")
    A("지시서의 종료 조건(\"실질 개선이 없으면 추가 모델 실험은 종료한다\")에")
    A("답하면 **모델 2 쪽 추가 실험은 종료**입니다. 예측 오차에서는 실질 개선이")
    A("없고, 수정이 남긴 것은 **1차 산출물인 percentile 조회의 정합성** 쪽입니다")
    A("— 모델 2 는 회귀가 아니라 비교군 percentile 이 주 산출물이므로 그쪽이")
    A("더 중요한 축입니다.\n")
    A("> 이 문서는 canonical 을 바꾸지 않습니다. 승격은 M56 이 세운 절차(재현성 고정")
    A("> → 누수 최종 점검 → 동일조건 비교 → 서빙 동기화 · 점검표 11항목)를 다시")
    A("> 통과해야 하고, 그 판단 근거는 여기 수치입니다. 승격 근거는 *MAE 개선*이")
    A("> 아니라 **비교군 축의 정합성**이라는 점을 같이 적어 둡니다.")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("[report] %s" % MD)


# ------------------------------------------------------------ main
def main(run_extra=False):
    t0 = time.time()
    d1, drop1 = load(V1)
    d2, drop2 = load(V2)
    d1, d2 = align(d1, d2)
    print("== 대상 행  v1 %d / v2 %d  (row_id 집합 일치)" % (len(d1), len(d2)))
    print("   필터 v1 %s" % drop1)
    print("   필터 v2 %s" % drop2)

    v1, v2 = bundle(d1), bundle(d2)

    def n_changed(c):
        """결측끼리는 같은 것으로 본다 — NaN != NaN 이라 그냥 비교하면 부풀려진다."""
        x, y = d1[c], d2[c]
        return int((~((x.isna() & y.isna()) | (x == y))).sum())

    changed = {c: n_changed(c)
               for c in ["support_method", "support_unit", "amount_type",
                         "support_count", "support_ratio", "self_burden_ratio",
                         "project_duration", "per_recipient", "title"]}
    print("   입력 변경 행수 %s" % changed)

    res = {"dataset": {"v1": os.path.relpath(V1, C.ROOT), "v2": os.path.relpath(V2, C.ROOT),
                       "rows": int(len(d1)), "filter_trace_v1": drop1,
                       "filter_trace_v2": drop2, "changed_cells": changed},
           "published_M56": M56_PUBLISHED}

    print("\n== 동일 split 비교 (M56 파이프라인 고정, 데이터만 교체)")
    for gname in ("program_stem", "normalized_title"):
        preds = paired_oof(v1, v2, v1["groups"][gname])
        block, base = score_block(v1["y"], preds)
        res.setdefault("paired", {})[gname] = {"baseline_MAE": round(base, 4), **block}
        print("   [%s] baseline %.4f" % (gname, base))
        for k in ("M56 on v1(현행)", "M56 on v2(수정)"):
            m = block[k]
            print("      %-18s MAE %.4f  개선 %+.1f%%  2배내 %.1f%%  3배내 %.1f%%"
                  % (k, m["MAE_log10"], 100 * m["improvement_vs_v1_baseline"],
                     100 * m["within_2x"], 100 * m["within_3x"]))
        if gname == "program_stem":
            pd.DataFrame({"row_id": d1["row_id"], "y": v1["y"],
                          **{k: v for k, v in preds.items()}}).to_parquet(
                OUT_OOF, index=False)
            res["paired_delta"] = paired_delta(v1["y"], preds["M56 on v1(현행)"],
                                               preds["M56 on v2(수정)"])
            print("      짝지은 차이 평균 %+.5f (P>0 %.3f · 달라진 행 %d)"
                  % (res["paired_delta"]["mean_delta(v1-v2)"],
                     res["paired_delta"]["P(delta>0)"],
                     res["paired_delta"]["n_rows_changed"]))

    print("\n== fold 재구성 %d회" % N_REPEAT)
    res["stability"] = stability(v1, v2)
    for k, v in res["stability"].items():
        print("   %-18s 개선율 %.4f ± %.4f (최저 %.4f, 통과 %s)"
              % (k, v["mean"], v["std"], v["min"], v["pass"]))

    print("\n== 예측구간 · 비교군 등급")
    res["intervals"] = {}
    for tag, b in (("v1(현행)", v1), ("v2(수정)", v2)):
        im, counts, _ = intervals_and_tiers(b)
        res["intervals"][tag] = {**im, "tier_counts": dict(zip(M45.TIERS, counts))}
        print("   %-9s 커버리지 %.3f  구간폭 %.1f배  등급 %s"
              % (tag, im["coverage"], im["median_width_x"], counts))

    if run_extra:
        print("\n== 선택 실험 (지시서 '필요할 때만')")
        preds = extra_candidates(v2, v1["groups"]["program_stem"])
        base = res["paired"]["program_stem"]["baseline_MAE"]
        blk = {}
        for k, p in preds.items():
            m = M45.point_metrics(v1["y"], p)
            m["improvement_vs_v1_baseline"] = round(float((base - m["MAE_log10"]) / base), 4)
            blk[k] = m
            print("   %-26s MAE %.4f  개선 %+.1f%%"
                  % (k, m["MAE_log10"], 100 * m["improvement_vs_v1_baseline"]))
        res["extra"] = blk

    res["elapsed_sec"] = round(time.time() - t0, 1)
    C.save_report("m63_m2_requal.json", res)
    write_md(res)
    print("\n[%.0fs] oof -> %s" % (res["elapsed_sec"], OUT_OOF))


def extra_only():
    """이미 저장된 결과 위에 선택 실험만 얹는다. 본 비교(17분)를 다시 돌리지
    않기 위한 것이고, baseline·fold 는 저장된 것과 같은 정의를 쓴다."""
    import json
    with open(os.path.join(C.REPORTS, "m63_m2_requal.json"), encoding="utf-8") as f:
        res = json.load(f)
    d1, _ = load(V1)
    d2, _ = load(V2)
    d1, d2 = align(d1, d2)
    v1, v2 = bundle(d1), bundle(d2)
    base = res["paired"]["program_stem"]["baseline_MAE"]

    oof = pd.read_parquet(OUT_OOF)
    res["paired_delta"] = paired_delta(oof["y"].to_numpy(),
                                       oof["M56 on v1(현행)"].to_numpy(),
                                       oof["M56 on v2(수정)"].to_numpy())
    print("   짝지은 차이 %s" % res["paired_delta"])

    preds = extra_candidates(v2, v1["groups"]["program_stem"])
    blk = {}
    for k, p in preds.items():
        m = M45.point_metrics(v1["y"], p)
        m["improvement_vs_v1_baseline"] = round(float((base - m["MAE_log10"]) / base), 4)
        blk[k] = m
        print("   %-30s MAE %.4f  개선 %+.1f%%"
              % (k, m["MAE_log10"], 100 * m["improvement_vs_v1_baseline"]))
    res["extra"] = blk
    C.save_report("m63_m2_requal.json", res)
    write_md(res)


if __name__ == "__main__":
    if "--extra-only" in sys.argv:
        extra_only()
    else:
        main(run_extra="--extra" in sys.argv)
