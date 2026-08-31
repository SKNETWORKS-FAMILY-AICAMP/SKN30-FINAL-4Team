"""M15 — 모델 2 성능 개선: Feature Ablation + Gower Weight + HDBSCAN 탐색.

개선계획서 Step 1 을 그대로 실행한다.
    Feature Ablation -> Gower Weight 조정 -> HDBSCAN Parameter Search
    -> Bootstrap ARI -> Cluster 해석

다만 계획서의 목표치 하나는 그대로 쓸 수 없다.

    계획서: "ARI 0.2245는 군집 안정성/재현성이 낮은 편 -> ARI 0.40 이상 목표"

    이 ARI 는 안정성 지표가 아니다. **군집 라벨과 지원성격(모델 1의 19클래스)의
    일치도**이고, 설계서가 "모델 1을 복제하면 실패"라고 못박은 바로 그 지표다.
    올리면 목표에 가까워지는 것이 아니라 실패 조건에 가까워진다.

    안정성 지표는 따로 있다 — bootstrap ARI(표본 80% 재추출 후 재군집했을 때
    원래 배정이 얼마나 남는가)이고 M11 에서 이미 0.9545 다.

그래서 계획서의 의도(안정성·재현성을 올린다)는 살리고 대상을 바로잡는다.

    유지해야 할 것   silhouette >= 0.55, 해석 가능한 군집, 노이즈 허용범위
    올려야 할 것     bootstrap ARI (재현성)
    낮게 묶을 것     지원성격 ARI < 0.50 (모델 1 복제 방지)

계획서가 추가로 요구한 것 중 여기서 실제로 재는 것
    - feature 조합 A/B/C 비교 (support_type_19 포함 여부의 영향)
    - Gower feature weight 조정 (설계 feature 강화 / 분야·성격 축소)
    - min_cluster_size x min_samples x cluster_selection_epsilon 격자
    - bootstrap 30회
    - 군집 4~8개라는 채택 기준의 타당성

기준선 (M11, 지원성격 제외 조건)
    군집 20 / 노이즈 41.1% / 실루엣 0.6012 / 지원성격 ARI 0.2245
    bootstrap ARI 0.9545
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
import os
import sys
import time

import numpy as np
import pandas as pd
from sklearn.cluster import HDBSCAN
from sklearn.metrics import adjusted_rand_score

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m11_m2_cluster import (ARI_FAIL, MIN_INDEPENDENT, SRC, gower, name_clusters,
                            pcoa, prepare, profile, score)

OUT = os.path.join(C.PROC, "design_clusters_tuned.parquet")
SEED = 42

# 계획서 2절의 실험 A/B/C. 설계 feature 만 -> 맥락 추가 -> 지원성격까지.
EXPERIMENTS = {
    "A_설계만": {
        "cat": ["support_method", "support_unit", "amount_type"],
        "num": ["log_per_recipient", "log_support_count", "support_ratio",
                "project_duration"],
    },
    "B_설계+맥락": {
        "cat": ["support_method", "support_unit", "amount_type",
                "category_large", "industry_grp", "agency_type"],
        "num": ["log_per_recipient", "log_support_count", "support_ratio",
                "project_duration"],
    },
    "C_지원성격포함": {
        "cat": ["support_method", "support_unit", "amount_type",
                "category_large", "industry_grp", "agency_type", "support_type"],
        "num": ["log_per_recipient", "log_support_count", "support_ratio",
                "project_duration"],
    },
}

# 계획서 4절. 설계구조가 군집을 결정하고 분야·성격은 물러나게 한다.
WEIGHTS = {
    "균등": {},
    "설계강화": {
        "support_method": 1.5, "amount_type": 1.5, "support_unit": 1.2,
        "log_per_recipient": 1.5, "log_support_count": 1.5,
        "project_duration": 1.2, "support_ratio": 1.2,
        "category_large": 0.5, "industry_grp": 0.5, "agency_type": 0.5,
        "support_type": 0.5,
    },
    "설계극단": {
        "support_method": 2.0, "amount_type": 2.0, "support_unit": 1.5,
        "log_per_recipient": 2.0, "log_support_count": 2.0,
        "project_duration": 1.5, "support_ratio": 1.5,
        "category_large": 0.2, "industry_grp": 0.2, "agency_type": 0.2,
        "support_type": 0.2,
    },
}

GRID_MCS = [15, 20, 30, 40, 50]
GRID_MS = [None, 5, 10, 15, 20]
GRID_EPS = [0.0, 0.05, 0.10]

# 채택 기준 — 계획서 8절에서 ARI 항목만 바로잡았다
MIN_SILHOUETTE = 0.55
MIN_BOOTSTRAP_ARI = 0.60
# 입력 feature 하나를 그대로 복제한 군집은 설계유형이 아니다.
#
# 첫 실행에서 실제로 걸렸다 — weight 를 극단으로 주자 군집 4개가 support_method
# 값(grant/loan/service/other)과 정확히 일치했다. 그때 실루엣 0.79 / bootstrap
# ARI 1.00 이 나왔는데, 범주형 하나로 자른 분할은 원래 잘 갈라지고 재추출해도
# 안 흔들린다. 지표만 보면 개선인데 실제로는 아무것도 안 배운 것이다.
# 설계서의 '모델 1 복제 금지'와 같은 종류의 실패이므로 같은 방식으로 막는다.
MAX_FEATURE_ARI = 0.80
# 계획서 8절 미채택 조건 '대형 cluster 하나에 집중'
MAX_LARGEST_SHARE = 0.50
# 노이즈는 서비스에서 '유형을 못 붙인 사업'이다. 기준선(41.1%)보다 나빠지면
# 실루엣이 올라도 담당자가 받는 답은 줄어든다. 기준선 근처로 묶는다.
MAX_NOISE = 0.45


def fit(D, params):
    """cluster_selection_epsilon > 0 에서 sklearn 이 터지는 조합이 있다.

    상류 버그다(_tree.pyx epsilon_search 에서 TypeError). 격자 탐색이 통째로
    멈추면 나머지 설정도 못 재므로, 실패한 설정만 건너뛰고 몇 개가 실패했는지
    리포트에 남긴다.
    """
    try:
        return HDBSCAN(metric="precomputed", copy=True, **params).fit_predict(D)
    except (TypeError, ValueError):
        return None


def feature_replication(t, labels, cat):
    """군집이 입력 범주형 하나를 그대로 복제했는지 축별로 잰다."""
    lab = np.asarray(labels)
    m = lab >= 0
    out = {}
    for f in cat:
        v = t[f].fillna("__na__").to_numpy()[m]
        if len(set(v)) < 2 or m.sum() < 2:
            continue
        out[f] = round(float(adjusted_rand_score(v, lab[m])), 4)
    return out


def bootstrap(t, cat, num, weights, params, n_iter=30, frac=0.8):
    """표본 80% 로 다시 군집했을 때 원래 배정이 얼마나 남는가 (= 재현성)."""
    rng = np.random.default_rng(SEED)
    base = fit(gower(t, cat, num, weights), params)
    aris = []
    if base is None:
        return {"n_iter": 0, "ari_mean": None, "ari_std": None, "ari_min": None}
    for _ in range(n_iter):
        idx = np.sort(rng.choice(len(t), int(len(t) * frac), replace=False))
        sub = t.iloc[idx].reset_index(drop=True)
        lab = fit(gower(sub, cat, num, weights), params)
        if lab is None:
            continue
        b = base[idx]
        m = (lab >= 0) & (b >= 0)
        if m.sum() > 10:
            aris.append(adjusted_rand_score(b[m], lab[m]))
    return {"n_iter": len(aris),
            "ari_mean": round(float(np.mean(aris)), 4) if aris else None,
            "ari_std": round(float(np.std(aris)), 4) if aris else None,
            "ari_min": round(float(np.min(aris)), 4) if aris else None}


def search(t, y_type, cat, num, weights, quick=False):
    """격자 탐색. 실패 조건에 걸리는 설정은 애초에 후보에서 뺀다."""
    D = gower(t, cat, num, weights)
    X = pcoa(D, k=8)
    rows, skipped = [], 0
    mcs_list = GRID_MCS if not quick else [20, 30]
    ms_list = GRID_MS if not quick else [None, 10]
    eps_list = GRID_EPS if not quick else [0.0]
    for mcs in mcs_list:
        for ms in ms_list:
            for eps in eps_list:
                p = {"min_cluster_size": mcs, "min_samples": ms,
                     "cluster_selection_epsilon": eps,
                     "cluster_selection_method": "eom"}
                lab = fit(D, p)
                if lab is None:
                    skipped += 1
                    continue
                r = {"min_cluster_size": mcs, "min_samples": ms, "eps": eps}
                r.update(score(D, X, lab, y_type))
                fr = feature_replication(t, lab, cat)
                r["max_feature_ari"] = round(max(fr.values()), 4) if fr else 0.0
                r["replicated_feature"] = (max(fr, key=fr.get) if fr else None)
                rows.append(r)
    return pd.DataFrame(rows), D, X, skipped


def pick(grid):
    """실루엣만 보고 고르면 지원성격을 복제하는 설정이 뽑힌다.

    실패 조건(ARI >= 0.50)과 최소 요건(>=30 군집 2개, 노이즈 60% 미만)을
    먼저 걸러낸 뒤 실루엣 최대를 고른다.
    """
    ok = grid[(grid["silhouette"].notna())
              & (grid["ari_support_type"].fillna(1) < ARI_FAIL)
              & (grid["max_feature_ari"] < MAX_FEATURE_ARI)
              & (grid["largest_share"].fillna(1) < MAX_LARGEST_SHARE)
              & (grid["n_ge30"] >= 2) & (grid["noise_ratio"] < MAX_NOISE)]
    pool = ok if len(ok) else grid[grid["silhouette"].notna()]
    return pool.sort_values("silhouette", ascending=False).iloc[0]


def to_params(row):
    return {"min_cluster_size": int(row["min_cluster_size"]),
            "min_samples": None if pd.isna(row["min_samples"]) else int(row["min_samples"]),
            "cluster_selection_epsilon": float(row["eps"]),
            "cluster_selection_method": "eom"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="격자를 줄여 빠르게 확인")
    a = ap.parse_args()

    t = prepare(pd.read_parquet(SRC))
    y_type = t["support_type"].to_numpy()
    print("모델 2 튜닝 대상: taxonomy %d행" % len(t))
    print("기준선(M11): 실루엣 0.6012 / 지원성격 ARI 0.2245 / bootstrap ARI 0.9545")

    t0 = time.time()
    results, best = {}, None
    for ename, feats in EXPERIMENTS.items():
        for wname, w in WEIGHTS.items():
            key = "%s / %s" % (ename, wname)
            grid, D, X, skipped = search(t, y_type, feats["cat"], feats["num"],
                                         w, a.quick)
            row = pick(grid)
            params = to_params(row)
            stab = bootstrap(t, feats["cat"], feats["num"], w, params,
                             n_iter=10 if a.quick else 30)
            r = {"features": feats, "weights": wname, "chosen": params,
                 "n_clusters": int(row["n_clusters"]),
                 "noise_ratio": float(row["noise_ratio"]),
                 "n_ge30": int(row["n_ge30"]),
                 "silhouette": float(row["silhouette"]),
                 "davies_bouldin": float(row["davies_bouldin"]),
                 "ari_support_type": float(row["ari_support_type"]),
                 "largest_share": float(row["largest_share"]),
                 "max_feature_ari": float(row["max_feature_ari"]),
                 "replicated_feature": row["replicated_feature"],
                 "bootstrap": stab,
                 "grid_size": int(len(grid)), "grid_skipped": int(skipped)}
            results[key] = r
            print("  %-22s 군집 %2d / 노이즈 %4.1f%% / 최대군집 %4.1f%% / 실루엣 %.4f"
                  " / 성격ARI %.4f / bootstrap %.4f / 복제 %.2f(%s)"
                  % (key, r["n_clusters"], r["noise_ratio"] * 100,
                     r["largest_share"] * 100, r["silhouette"],
                     r["ari_support_type"], stab["ari_mean"] or 0,
                     r["max_feature_ari"], r["replicated_feature"]))
            if passes(r) and (best is None or better(r, results[best])):
                best = key

    if best is None:
        # 채택 기준을 통과한 조합이 없으면 실루엣 최대를 후보로 보고한다
        best = max(results, key=lambda k: results[k]["silhouette"])
        print("\n채택 기준을 전부 통과한 조합 없음 — 실루엣 최대 조합을 후보로 보고한다")
    b = results[best]
    print("\n== 선택: %s" % best)
    print("  %s" % b["chosen"])

    feats = b["features"]
    D = gower(t, feats["cat"], feats["num"], WEIGHTS[b["weights"]])
    labels = fit(D, b["chosen"])
    prof = profile(t, labels, feats["num"], feats["cat"])
    tags, _ = name_clusters(prof)
    for c, p in prof.items():
        p["tag"] = tags[c]

    base = {"n_clusters": 20, "noise_ratio": 0.4105, "silhouette": 0.6012,
            "ari_support_type": 0.2245, "bootstrap_ari": 0.9545, "n_ge30": 12,
            "largest_share": 0.1477}
    verdict = judge(b, base)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    t2 = t.assign(cluster=labels,
                  cluster_tag=[tags.get(int(l)) if l >= 0 else None for l in labels])
    t2[["row_id", "title", "support_type", "support_method", "support_unit",
        "amount_type", "per_recipient", "support_count", "support_ratio",
        "project_duration", "cluster", "cluster_tag"]].to_parquet(OUT, index=False)
    print("[data] %s" % OUT)

    C.save_report("m15_m2_tuning.json", {
        "n_rows": int(len(t)), "seed": SEED,
        "note": ("계획서의 'ARI 0.40 이상' 목표는 지표를 잘못 읽은 것이다. "
                 "그 ARI 는 지원성격과의 일치도라 낮을수록 좋다(설계서 실패조건). "
                 "재현성 지표인 bootstrap ARI 로 목표를 옮겨 측정했다."),
        "criteria": {"min_silhouette": MIN_SILHOUETTE,
                     "min_bootstrap_ari": MIN_BOOTSTRAP_ARI,
                     "max_ari_support_type": ARI_FAIL,
                     "max_feature_ari": MAX_FEATURE_ARI,
                     "max_largest_share": MAX_LARGEST_SHARE,
                     "max_noise": MAX_NOISE},
        "baseline_m11": base, "experiments": results,
        "best": best, "profile": prof, "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2),
    })
    write_md(results, best, prof, base, verdict)


def passes(r):
    return (r["silhouette"] >= MIN_SILHOUETTE
            and r["ari_support_type"] < ARI_FAIL
            and r["max_feature_ari"] < MAX_FEATURE_ARI
            and r["largest_share"] < MAX_LARGEST_SHARE
            and r["noise_ratio"] < MAX_NOISE
            and (r["bootstrap"]["ari_mean"] or 0) >= MIN_BOOTSTRAP_ARI
            and r["n_ge30"] >= 2)


def better(a, b):
    """재현성이 우선, 같으면 실루엣."""
    ba = a["bootstrap"]["ari_mean"] or 0
    bb = b["bootstrap"]["ari_mean"] or 0
    if abs(ba - bb) > 0.01:
        return ba > bb
    return a["silhouette"] > b["silhouette"]


def judge(b, base):
    reasons, v = [], "개선 없음 — 기준선 유지"
    ba = b["bootstrap"]["ari_mean"] or 0
    if b["ari_support_type"] >= ARI_FAIL:
        return {"verdict": "No-Go", "reasons":
                ["지원성격 ARI %.4f — 설계서 실패 조건" % b["ari_support_type"]]}
    if b["max_feature_ari"] >= MAX_FEATURE_ARI:
        return {"verdict": "No-Go", "reasons":
                ["군집이 입력 feature %s%s%s 를 그대로 복제한다 (ARI %.4f). "
                 "실루엣·bootstrap 이 높아도 아무것도 배우지 않은 것이다"
                 % (BT, b["replicated_feature"], BT, b["max_feature_ari"])]}

    reasons.append("실루엣 %.4f (기준선 %.4f)" % (b["silhouette"], base["silhouette"]))
    reasons.append("bootstrap ARI %.4f (기준선 %.4f) — 재현성 지표"
                   % (ba, base["bootstrap_ari"]))
    reasons.append("지원성격 ARI %.4f (기준선 %.4f) — 낮을수록 좋다"
                   % (b["ari_support_type"], base["ari_support_type"]))
    reasons.append("군집 %d개 / 노이즈 %.1f%% (기준선 %d개 / %.1f%%)"
                   % (b["n_clusters"], b["noise_ratio"] * 100,
                      base["n_clusters"], base["noise_ratio"] * 100))
    reasons.append("최대 군집 비중 %.1f%% / 입력 feature 최대 복제도 %.4f (%s)"
                   % (b["largest_share"] * 100, b["max_feature_ari"],
                      b["replicated_feature"]))

    gain_stab = ba - base["bootstrap_ari"]
    gain_sil = b["silhouette"] - base["silhouette"]
    # 어느 한 축이라도 기준선보다 뚜렷이 나빠지면 개선이라고 부르지 않는다.
    # 실루엣만 보고 채택하면 노이즈가 58%까지 늘어난 설정이 뽑힌다(실측).
    regress = []
    if b["noise_ratio"] > base["noise_ratio"] + 0.03:
        regress.append("노이즈 %.1f%% -> %.1f%% (유형을 못 받는 사업이 늘었다)"
                       % (base["noise_ratio"] * 100, b["noise_ratio"] * 100))
    if b["ari_support_type"] > base["ari_support_type"] + 0.05:
        regress.append("지원성격 ARI %.4f -> %.4f (모델 1 쪽으로 끌려갔다)"
                       % (base["ari_support_type"], b["ari_support_type"]))
    if regress:
        v = "부분 개선 — 조건부"
        for r in regress:
            reasons.append("후퇴: %s" % r)
    elif gain_stab > 0.01 and b["silhouette"] >= MIN_SILHOUETTE:
        v = "개선 — 채택"
    elif gain_sil > 0.01 and ba >= base["bootstrap_ari"] - 0.01:
        v = "개선 — 채택"
    else:
        reasons.append("기준선을 의미 있게 넘지 못했다. M11 설정을 그대로 쓴다")
    return {"verdict": v, "reasons": reasons}


def write_md(results, best, prof, base, verdict):
    L = ["# 모델 2 성능 개선 — Feature Ablation · Gower Weight · HDBSCAN 탐색", "",
         "## 0. 개선계획서의 목표치를 그대로 쓰지 않은 이유", "",
         "> 계획서: \"ARI 0.2245는 군집 안정성/재현성이 낮은 편 → ARI 0.40 이상 목표\"", "",
         "이 ARI 는 안정성 지표가 아닙니다. **군집 라벨과 지원성격(모델 1의 19클래스)의**",
         "**일치도**이고, 설계서가 \"모델 1을 복제하면 실패\"라고 못박은 바로 그 지표입니다.",
         "0.40 으로 올리면 목표에 가까워지는 것이 아니라 실패 조건에 가까워집니다.", "",
         "재현성 지표는 따로 있습니다 — bootstrap ARI(표본 80% 재추출 후 재군집했을 때",
         "원래 배정이 얼마나 남는가). M11 에서 이미 **0.9545** 입니다.", "",
         "그래서 계획서의 의도(안정성·재현성 향상)는 살리고 대상을 바로잡았습니다.", "",
         "| | 지표 | 방향 | 기준선 |", "|---|---|---|---:|",
         "| 유지 | silhouette | 높을수록 | 0.6012 |",
         "| **올림** | **bootstrap ARI** | 높을수록 | **0.9545** |",
         "| 낮게 묶음 | 지원성격 ARI | **낮을수록** | 0.2245 |", "",
         "## 1. 실험 결과", "",
         "feature 조합 3종 × Gower weight 3종 = 9조합. 각 조합에서 HDBSCAN 격자를",
         "탐색하고(min_cluster_size × min_samples × cluster_selection_epsilon),",
         "실패 조건을 통과한 설정 중 실루엣 최대를 골랐습니다.", "",
         "| 조합 | 군집 | 노이즈 | 최대군집 | 실루엣 | 지원성격 ARI |"
         " bootstrap ARI | feature 복제도 |",
         "|---|---:|---:|---:|---:|---:|---:|---|"]
    for k, r in results.items():
        mark = " (선택)" if k == best else ""
        L.append("| %s%s | %d | %.1f%% | %.1f%% | %.4f | %.4f | %.4f | %.2f (%s) |"
                 % (k, mark, r["n_clusters"], r["noise_ratio"] * 100,
                    r["largest_share"] * 100, r["silhouette"],
                    r["ari_support_type"], r["bootstrap"]["ari_mean"] or 0,
                    r["max_feature_ari"], r["replicated_feature"]))
    L += ["", "| 기준선 M11 | %d | %.1f%% | %.1f%% | %.4f | %.4f | %.4f | — |"
          % (base["n_clusters"], base["noise_ratio"] * 100,
             base["largest_share"] * 100, base["silhouette"],
             base["ari_support_type"], base["bootstrap_ari"]), "",
          "> **feature 복제도**는 첫 실행에서 걸린 함정 때문에 넣었습니다. weight 를",
          "> 극단으로 주자 군집 4개가 support_method 값(grant/loan/service/other)과",
          "> 정확히 일치했고, 그때 실루엣 0.79 / bootstrap ARI 1.00 이 나왔습니다.",
          "> 범주형 하나로 자른 분할은 원래 잘 갈라지고 재추출해도 안 흔들립니다.",
          "> 지표만 보면 개선인데 실제로는 아무것도 배우지 않은 것이라, 설계서의",
          "> '모델 1 복제 금지'와 같은 방식으로 막았습니다(ARI %.2f 이상 탈락)."
          % MAX_FEATURE_ARI, ""]

    b = results[best]
    L += ["## 2. 선택된 설정", "", "```text",
          "조합       %s" % best,
          "HDBSCAN    %s" % b["chosen"],
          "범주형     %s" % ", ".join(b["features"]["cat"]),
          "수치형     %s" % ", ".join(b["features"]["num"]),
          "```", "",
          "## 3. 군집 프로파일", "",
          "| 군집 | n | 설계유형 태그 | 최다 지원성격 |", "|---:|---:|---|---|"]
    for c, p in sorted(prof.items(), key=lambda kv: -kv[1]["n"]):
        st = p.get("support_type") or {}
        L.append("| %d | %d | %s | %s (%.0f%%) |"
                 % (c, p["n"], p.get("tag", "—"), st.get("top", "—"),
                    st.get("share", 0) * 100))
    L += ["", "## 4. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L.append("")
    p = os.path.join(C.REPORTS, "m15_m2_tuning.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
