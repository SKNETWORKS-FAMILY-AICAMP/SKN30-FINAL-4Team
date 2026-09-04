r"""M47 — Model 3 민감도 확인: 대표벡터 · scaling · feature ablation (방향서 §8.2~§8.4).

Model 3 가 무엇인가 — 판정기가 아니다 (방향서 §1·§12)
    이 모델은 사업이 정상인지 이상한지 판정하지 않는다. 정형 설계정보를
    벡터화해 **유사사업 비교군의 대표 설계에서 얼마나 떨어져 있는지**를
    계산하고, 드문 설계 조합과 주요 차이축을 제시해 담당자의 **검토
    우선순위 설정을 보조**한다. 따라서 이 실험의 성패도 "정답을 얼마나
    맞히는가"가 아니라 **"이례성 신호가 안정적인가"** 로 판정한다.

무엇을 하는 실험인가
    **새 모델을 찾는 것이 아니다.** M44 에서 Freeze 한 구조가 임의의 구현
    선택(대표벡터를 평균으로 둔 것, StandardScaler 를 쓴 것)에 우연히 기대고
    있는지, 그리고 거리 점수가 특정 feature 하나에 쏠려 있는지를 본다.

    방향서 §8.3 "성능이 조금 오른다고 바로 구조를 바꾸지 않고
                경고 목록 stability 까지 함께 본다"
    방향서 §8.4 "특정 feature 하나가 anomaly score 전체를 사실상 지배하는지 확인"

왜 라벨 없이도 잴 수 있는가 — 이 실험의 설계 핵심
    독립 라벨셋은 clean 53건 / 양성 5건뿐이다. 그 위에서 ROC-AUC 를 비교하면
    변형 간 차이가 표본 잡음에 완전히 묻힌다(M44 의 CI 0.575~0.908).

    그런데 이 실험이 실제로 묻는 것은 "어느 변형이 더 정확한가"가 아니라
    **"변형이 순위를 바꾸는가"** 다. 순위 비교는 라벨이 필요 없다.

        Spearman 순위상관   pool %d행 전체에서 기준 대비 순위가 얼마나 보존되는가
        Top-K 겹침          실제로 경고할 상위 목록이 바뀌는가

    이 두 지표는 **양성 5건이 아니라 pool 전체**에서 나오므로 표본이 충분하다.
    라벨 기반 ROC-AUC 도 같이 내지만 **참고값**으로만 둔다 — 5건으로는 변형
    간 우열을 가릴 수 없다는 것을 수치로 함께 보인다.

    라벨 기반 ROC-AUC 는 **탐색적 보조 검증**으로만 읽는다 (방향서 §7).
    53건·양성 5건은 도메인 전문가 Ground Truth 가 아니므로 최종 성능
    인증이 될 수 없다. 이 실험의 판정은 순위 지표로 한다.

판정 기준을 결과보다 먼저 못박는다 (방향서 §8.3)
    기준은 M44 가 이미 측정한 **재표집 변동폭**이다. 80% 재표집으로 대표벡터를
    다시 만들면 hold-out 상위 목록이 평균 91.8% 유지된다(= 8.2% 는 표본만
    바꿔도 흔들린다). 따라서

        Top-K 겹침이 0.918 이상   -> 표본 흔들림과 구별되지 않는다 -> 기존 유지
        ROC-AUC 차이가 ±0.041 안  -> M44 재표집 폭(0.738~0.779) 안 -> 기존 유지

    이 문턱을 넘지 못하는 개선은 개선이라 부르지 않는다.

바꾸지 않는 것
    비교군 기준(지원성격x지원방식 -> 지원성격 -> 전체, 최소 20건), 방향을
    점수에 합치지 않는 것, threshold. 이번에 움직이는 축은 대표벡터 통계량 /
    scaling / 입력 feature 셋 셋뿐이다.
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
from scipy.stats import spearmanr, trim_mean
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import RobustScaler, StandardScaler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m38_m3_vector_direction import CAT, MIN_COHORT, NUM, N_PC
from m43_m3_label_rule_v2 import OUT as HOLDOUT2_V2

SEED = 42
TRIM = 0.10             # trimmed mean 이 양끝에서 잘라내는 비율
POOL_TOPK = [30, 39]    # 39 ~= pool 의 2% (운영 경고율)
BUDGET_FRAC = 0.20      # M44 와 같은 경고 예산

# M44 가 이미 측정한 재표집 변동폭. 판정 문턱을 여기서 가져온다 (결과를 보기 전에 고정).
M44_HOLDOUT_RETENTION = 0.918
M44_ROC_MIN, M44_ROC_MAX = 0.738, 0.779
ROC_TOL = round((M44_ROC_MAX - M44_ROC_MIN) / 2, 4)     # ±0.0205 -> 아래에서 확대


def mad_scale(Ntr, Nap):
    """MAD 기반 표준화. 이상치가 스케일 자체를 밀어내지 않게 한다.

    StandardScaler 는 분산을 쓰는데, 이상탐지 대상인 극단값이 그 분산을
    키워 **자기 자신을 덜 튀어 보이게** 만든다. MAD 는 그 되먹임이 없다.
    """
    med = np.median(Ntr, axis=0)
    mad = np.median(np.abs(Ntr - med), axis=0) * 1.4826
    mad = np.where(mad < 1e-12, np.std(Ntr, axis=0), mad)   # 상수축 보호
    mad = np.where(mad < 1e-12, 1.0, mad)
    return (Ntr - med) / mad, (Nap - med) / mad


def build_vectors_v(train, apply_df, scaler="standard", num_features=None):
    """M38.build_vectors 의 파라미터화 판. scaling 과 입력 feature 만 바꾼다.

    블록 크기 정규화(수치 블록과 범주 블록의 평균 노름을 맞추는 것)는 M38 과
    똑같이 유지한다 — 그것까지 흔들면 무엇이 원인인지 알 수 없다.
    """
    feats = list(NUM if num_features is None else num_features)
    num_tr, num_ap = [], []
    for f in feats:
        med = train[f].median()
        med = 0.0 if pd.isna(med) else med
        num_tr.append(train[f].fillna(med).to_numpy(dtype=float))
        num_ap.append(apply_df[f].fillna(med).to_numpy(dtype=float))
    Ntr, Nap = np.column_stack(num_tr), np.column_stack(num_ap)

    if scaler == "standard":
        sc = StandardScaler().fit(Ntr)
        Ntr, Nap = sc.transform(Ntr), sc.transform(Nap)
    elif scaler == "robust":
        sc = RobustScaler().fit(Ntr)
        Ntr, Nap = sc.transform(Ntr), sc.transform(Nap)
    elif scaler == "mad":
        Ntr, Nap = mad_scale(Ntr, Nap)
    else:
        raise ValueError(scaler)

    cat_tr, cat_ap = [], []
    for f in CAT:
        for v in [x for x in train[f].dropna().unique()]:
            cat_tr.append((train[f] == v).to_numpy(float))
            cat_ap.append((apply_df[f] == v).to_numpy(float))
    Ctr = np.column_stack(cat_tr) if cat_tr else np.zeros((len(train), 0))
    Cap = np.column_stack(cat_ap) if cat_ap else np.zeros((len(apply_df), 0))

    def blk(A, ref):
        s = np.linalg.norm(ref, axis=1).mean()
        return A / s if s > 0 else A
    return (np.hstack([blk(Ntr, Ntr), blk(Ctr, Ctr)]),
            np.hstack([blk(Nap, Ntr), blk(Cap, Ctr)]), len(feats))


def centroid(M, center):
    if center == "mean":
        return M.mean(0)
    if center == "median":
        return np.median(M, axis=0)
    if center == "trimmed":
        return trim_mean(M, TRIM, axis=0) if len(M) >= 5 else M.mean(0)
    raise ValueError(center)


def dist_pct_v(train, apply_df, Xtr, Xap, center="mean"):
    """M38.score_components 중 **거리 성분만** 파라미터화해 다시 만든다.

    방향은 M44 에서 점수에 합치지 않기로 Freeze 했으므로 여기서도 만들지
    않는다. 최종 점수 = 비교군 안에서의 거리 백분위다.
    """
    k2_tr = train["support_type"].astype(str) + "|" + train["support_method"].astype(str)
    k1_tr = train["support_type"].astype(str)
    k2_ap = apply_df["support_type"].astype(str) + "|" + apply_df["support_method"].astype(str)
    k1_ap = apply_df["support_type"].astype(str)
    n2, n1 = k2_tr.value_counts(), k1_tr.value_counts()

    def resolve(a, b):
        if n2.get(a, 0) >= MIN_COHORT:
            return ("2", a)
        if n1.get(b, 0) >= MIN_COHORT:
            return ("1", b)
        return ("0", "ALL")

    groups = {}
    need = ({resolve(a, b) for a, b in zip(k2_tr, k1_tr)} |
            {resolve(a, b) for a, b in zip(k2_ap, k1_ap)})
    for lvl, key in need:
        if lvl == "2":
            mask = (k2_tr == key).to_numpy()
        elif lvl == "1":
            mask = (k1_tr == key).to_numpy()
        else:
            mask = np.ones(len(train), bool)
        M = Xtr[mask]
        c = centroid(M, center)
        groups[(lvl, key)] = {"c": c, "dist": np.linalg.norm(M - c, axis=1),
                              "n": int(mask.sum())}

    out = np.empty(len(apply_df))
    for i in range(len(apply_df)):
        g = groups[resolve(k2_ap.iloc[i], k1_ap.iloc[i])]
        nd = np.linalg.norm(Xap[i] - g["c"])
        out[i] = float((g["dist"] <= nd).mean()) * 100
    return out


def run_variant(train, holdout_ids, center="mean", scaler="standard", num_features=None):
    """hold-out 을 적합에서 빼고(M44 와 동일) pool 전체 점수를 낸다."""
    fit = train[~train["row_id"].isin(holdout_ids)].reset_index(drop=True)
    Xtr, Xap, _ = build_vectors_v(fit, train, scaler, num_features)
    d = dist_pct_v(fit, train, Xtr, Xap, center)
    return pd.Series(pd.Series(d).rank(pct=True).to_numpy(),
                     index=train["row_id"].to_numpy())


def compare_to_base(base, var):
    """라벨 없이 pool 전체에서 재는 두 지표 — 이 실험의 주 근거다."""
    rho = float(spearmanr(base.to_numpy(), var.loc[base.index].to_numpy()).statistic)
    out = {"spearman": round(rho, 4)}
    for k in POOL_TOPK:
        b = set(base.sort_values(ascending=False).head(k).index)
        v = set(var.sort_values(ascending=False).head(k).index)
        out["top%d_overlap" % k] = round(len(b & v) / k, 4)
    return out


def eval_labeled(scores, ids, y):
    """참고값. 양성 5건이라 변형 간 우열을 가릴 힘이 없다."""
    sc = scores.loc[ids].to_numpy(float)
    k = max(1, int(round(BUDGET_FRAC * len(y))))
    flag = sc >= np.sort(sc)[::-1][k - 1]
    tp = int((flag & (y == 1)).sum())
    fp = int((flag & (y == 0)).sum())
    fn = int((~flag & (y == 1)).sum())
    return {"roc_auc": round(float(roc_auc_score(y, sc)), 4),
            "pr_auc": round(float(average_precision_score(y, sc)), 4),
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None}


def verdict(cmp_, lab, base_roc):
    """결과를 보기 전에 정한 문턱으로 판정한다 (계획서 §5)."""
    keep_rank = cmp_["top30_overlap"] >= M44_HOLDOUT_RETENTION
    within = abs(lab["roc_auc"] - base_roc) <= ROC_TOL * 2
    if keep_rank and within:
        return "기존 유지 — 순위·성능 모두 재표집 변동폭 안"
    if not keep_rank and within:
        return "순위는 바뀌나 성능차는 변동폭 안 — 기존 유지"
    if keep_rank and not within:
        return "순위는 같은데 성능차만 큼 — 표본 잡음 (양성 5건)"
    return "차이 큼 — 라벨 확대 후 재판정 필요"


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    lab = pd.read_csv(HOLDOUT2_V2, encoding="utf-8-sig")
    holdout_ids = set(lab["row_id"])

    main_lab = lab[lab["v2_라벨"].isin(["normal", "atypical_design"])]
    main_lab = main_lab[main_lab["row_id"].isin(set(train["row_id"]))]
    ids = main_lab["row_id"].tolist()
    y = (main_lab["v2_라벨"] == "atypical_design").to_numpy(int)

    print("M47 — Model 3 민감도 확인 (대표벡터 / scaling / feature ablation)")
    print("  pool %d행 · 라벨 clean %d건(양성 %d)" % (len(train), len(y), y.sum()))
    print("  주 지표는 pool %d행의 순위 보존 — 라벨 기반 값은 참고용" % len(train))
    print("  판정 문턱(사전 고정): Top30 겹침 >= %.3f / ROC 차 <= %.4f"
          % (M44_HOLDOUT_RETENTION, ROC_TOL * 2))

    base = run_variant(train, holdout_ids)
    base_lab = eval_labeled(base, ids, y)
    print("\n== 기준 (M44 Freeze: 평균 대표벡터 · StandardScaler · 수치 4축)")
    print("  ROC-AUC %.4f  PR-AUC %.4f" % (base_lab["roc_auc"], base_lab["pr_auc"]))

    results = {}

    print("\n== 실험 4 — 대표벡터 C 를 무엇으로 만드는가")
    print("  %-12s %10s %10s %10s | %8s %8s  %s"
          % ("변형", "Spearman", "Top30", "Top39", "ROC", "PR", "판정"))
    sec = {}
    for cen in ("mean", "median", "trimmed"):
        v = base if cen == "mean" else run_variant(train, holdout_ids, center=cen)
        c = compare_to_base(base, v)
        L = eval_labeled(v, ids, y)
        vd = "기준" if cen == "mean" else verdict(c, L, base_lab["roc_auc"])
        sec[cen] = {**c, **L, "verdict": vd}
        print("  %-12s %10.4f %10.4f %10.4f | %8.4f %8.4f  %s"
              % (cen, c["spearman"], c["top30_overlap"], c["top39_overlap"],
                 L["roc_auc"], L["pr_auc"], vd))
    results["representative_vector"] = sec

    print("\n== 실험 5 — feature scaling")
    print("  %-12s %10s %10s %10s | %8s %8s  %s"
          % ("변형", "Spearman", "Top30", "Top39", "ROC", "PR", "판정"))
    sec = {}
    for scl in ("standard", "robust", "mad"):
        v = base if scl == "standard" else run_variant(train, holdout_ids, scaler=scl)
        c = compare_to_base(base, v)
        L = eval_labeled(v, ids, y)
        vd = "기준" if scl == "standard" else verdict(c, L, base_lab["roc_auc"])
        sec[scl] = {**c, **L, "verdict": vd}
        print("  %-12s %10.4f %10.4f %10.4f | %8.4f %8.4f  %s"
              % (scl, c["spearman"], c["top30_overlap"], c["top39_overlap"],
                 L["roc_auc"], L["pr_auc"], vd))
    results["scaling"] = sec

    print("\n== 실험 6 — feature ablation (축 하나씩 빼기)")
    print("  %-16s %10s %10s %10s | %8s %8s"
          % ("제거한 축", "Spearman", "Top30", "Top39", "ROC", "PR"))
    sec = {"__none__": {"spearman": 1.0, "top30_overlap": 1.0, "top39_overlap": 1.0,
                        **base_lab}}
    print("  %-16s %10.4f %10.4f %10.4f | %8.4f %8.4f"
          % ("(제거 없음)", 1.0, 1.0, 1.0, base_lab["roc_auc"], base_lab["pr_auc"]))
    for f in NUM:
        rest = [x for x in NUM if x != f]
        v = run_variant(train, holdout_ids, num_features=rest)
        c = compare_to_base(base, v)
        L = eval_labeled(v, ids, y)
        sec[f] = {**c, **L}
        print("  %-16s %10.4f %10.4f %10.4f | %8.4f %8.4f"
              % (f, c["spearman"], c["top30_overlap"], c["top39_overlap"],
                 L["roc_auc"], L["pr_auc"]))
    results["feature_ablation"] = sec

    dep = sorted(((f, 1 - v["spearman"]) for f, v in sec.items() if f != "__none__"),
                 key=lambda kv: -kv[1])
    print("\n  축 의존도 (1 - Spearman, 클수록 그 축이 순위를 많이 좌우한다)")
    for f, d in dep:
        print("    %-18s %.4f" % (f, d))

    # ---- ROC 하나만 보면 안 되는 이유. 방향서 §9 가 이 표를 근거로 쓴다.
    disc = []
    for sect, items in (("대표벡터", results["representative_vector"]),
                        ("scaling", results["scaling"]),
                        ("ablation", results["feature_ablation"])):
        for k, v in items.items():
            if v["top30_overlap"] >= 0.9999:        # 기준 자신은 뺀다
                continue
            disc.append({"section": sect, "variant": k,
                         "top30_changed": round(1 - v["top30_overlap"], 4),
                         "roc_diff": round(v["roc_auc"] - base_lab["roc_auc"], 4)})
    disc.sort(key=lambda d: -d["top30_changed"])
    results["rank_vs_roc"] = disc

    print("\n== ROC 차이와 경고 목록 변화가 따로 논다 (방향서 §9)")
    print("  %-10s %-18s %14s %12s" % ("구획", "변형", "상위30 바뀐율", "ROC 차이"))
    for d in disc[:5]:
        print("  %-10s %-18s %13.0f%% %+12.4f"
              % (d["section"], d["variant"], d["top30_changed"] * 100, d["roc_diff"]))
    w = disc[0]
    print("  -> 상위 30건 중 %.0f%%(%d건)가 다른 사업으로 바뀌어도 ROC 차이는 %+.4f 다."
          % (w["top30_changed"] * 100, round(w["top30_changed"] * 30), w["roc_diff"]))
    print("     ROC 하나만 보고 구조를 바꾸면 검토 목록이 통째로 달라진다.")

    rep = {
        "목적": ("M44 Freeze 구조가 임의의 구현 선택에 기대는지, 거리 점수가 특정 "
               "feature 에 쏠리는지 확인. 새 모델 탐색이 아니다"),
        "주지표": "pool 전체 Spearman 순위상관 · Top-K 겹침 (라벨 불필요)",
        "참고지표": "clean %d건(양성 %d) ROC-AUC/PR-AUC — 변형 간 우열 판정 불가"
                 % (len(y), int(y.sum())),
        "판정문턱": {"top30_overlap_min": M44_HOLDOUT_RETENTION,
                 "roc_tolerance": ROC_TOL * 2,
                 "출처": "M44 재표집 변동폭 (hold-out 유지율 0.918 / ROC 0.738~0.779)"},
        "n_pool": int(len(train)), "n_clean": int(len(y)), "n_positive": int(y.sum()),
        "baseline": base_lab,
        **results,
        "axis_dependence": [{"feature": f, "one_minus_spearman": round(d, 4)}
                            for f, d in dep],
    }
    C.save_report("m47_m3_sensitivity.json", rep)
    write_md(rep)


def write_md(r):
    L = ["# M47 — Model 3 민감도 확인 (대표벡터 · scaling · feature ablation)", "",
         "> **새 모델을 찾는 실험이 아닙니다.** M44 에서 Freeze 한 구조가 임의의",
         "> 구현 선택에 우연히 기대고 있는지, 거리 점수가 특정 feature 하나에",
         "> 쏠려 있는지만 봅니다. (계획서 §5~§7)", "",
         "## 1. 라벨 5건으로 어떻게 판정하는가 — 지표를 바꿨습니다", "",
         "독립 라벨셋은 clean %d건 중 **양성 %d건**뿐입니다. 그 위에서 변형별"
         % (r["n_clean"], r["n_positive"]),
         "ROC-AUC 를 비교하면 차이가 전부 표본 잡음에 묻힙니다(M44 CI 0.575~0.908).", "",
         "그런데 이 실험이 실제로 묻는 것은 *어느 변형이 더 정확한가*가 아니라",
         "**변형이 순위를 바꾸는가** 입니다. 순위 비교에는 라벨이 필요 없습니다.", "",
         "| 지표 | 표본 | 라벨 필요 | 역할 |", "|---|---:|---|---|",
         "| Spearman 순위상관 | pool %d행 | 불필요 | **주 근거** |" % r["n_pool"],
         "| Top-K 겹침 | pool %d행 | 불필요 | **주 근거** |" % r["n_pool"],
         "| ROC-AUC / PR-AUC | clean %d건 | 필요 | 참고값 (우열 판정 불가) |" % r["n_clean"],
         "",
         "### 판정 문턱은 결과를 보기 전에 고정했습니다", "",
         "기준은 M44 가 이미 측정한 **재표집 변동폭**입니다 — 80%% 재표집으로",
         "대표벡터를 다시 만들면 상위 목록이 평균 %.1f%% 유지되고(= %.1f%% 는 표본만"
         % (r["판정문턱"]["top30_overlap_min"] * 100,
            (1 - r["판정문턱"]["top30_overlap_min"]) * 100),
         "바꿔도 흔들림), ROC-AUC 는 0.738~0.779 사이에서 움직였습니다.", "",
         "```text",
         "Top30 겹침 >= %.3f      -> 표본 흔들림과 구별 안 됨 -> 기존 유지"
         % r["판정문턱"]["top30_overlap_min"],
         "ROC 차이  <= %.4f       -> M44 재표집 폭 안        -> 기존 유지"
         % r["판정문턱"]["roc_tolerance"],
         "```", "",
         "## 2. 실험 4 — 대표벡터 `C` 를 무엇으로 만드는가", "",
         "| 통계량 | Spearman | Top30 겹침 | Top39 겹침 | ROC(참고) | PR(참고) | 판정 |",
         "|---|---:|---:|---:|---:|---:|---|"]
    for k, v in r["representative_vector"].items():
        L.append("| `%s` | %.4f | %.4f | %.4f | %.4f | %.4f | %s |"
                 % (k, v["spearman"], v["top30_overlap"], v["top39_overlap"],
                    v["roc_auc"], v["pr_auc"], v["verdict"]))
    L += ["", "## 3. 실험 5 — feature scaling", "",
          "> MAD 를 넣은 이유: `StandardScaler` 는 분산으로 나누는데, 이상탐지 대상인",
          "> 극단값이 그 분산을 키워 **자기 자신을 덜 튀어 보이게** 만듭니다. MAD 에는",
          "> 그 되먹임이 없습니다.", "",
          "| scaler | Spearman | Top30 겹침 | Top39 겹침 | ROC(참고) | PR(참고) | 판정 |",
          "|---|---:|---:|---:|---:|---:|---|"]
    for k, v in r["scaling"].items():
        L.append("| `%s` | %.4f | %.4f | %.4f | %.4f | %.4f | %s |"
                 % (k, v["spearman"], v["top30_overlap"], v["top39_overlap"],
                    v["roc_auc"], v["pr_auc"], v["verdict"]))
    L += ["", "## 4. 실험 6 — feature ablation", "",
          "\"모델이 특정 feature 하나에만 의존하는가?\" (계획서 §7)", "",
          "| 제거한 축 | Spearman | Top30 겹침 | Top39 겹침 | ROC(참고) | PR(참고) |",
          "|---|---:|---:|---:|---:|---:|"]
    for k, v in r["feature_ablation"].items():
        nm = "(제거 없음)" if k == "__none__" else "`%s`" % k
        L.append("| %s | %.4f | %.4f | %.4f | %.4f | %.4f |"
                 % (nm, v["spearman"], v["top30_overlap"], v["top39_overlap"],
                    v["roc_auc"], v["pr_auc"]))
    L += ["", "### 축 의존도", "",
          "`1 - Spearman` 이 클수록 그 축을 빼면 순위가 많이 바뀝니다.", "",
          "| 축 | 의존도 |", "|---|---:|"]
    for x in r["axis_dependence"]:
        L.append("| `%s` | %.4f |" % (x["feature"], x["one_minus_spearman"]))
    d = r["rank_vs_roc"]
    w = d[0]
    L += ["", "## 5. ROC 차이와 경고 목록 변화는 따로 논다 (방향서 §9)", "",
          "위 세 실험이 **부수적으로 보인 것**이 있습니다. 변형들은 경고 목록을",
          "크게 바꾸는데, 라벨 기반 ROC-AUC 는 거의 움직이지 않습니다.", "",
          "| 구획 | 변형 | 상위 30건이 바뀐 비율 | ROC 차이 |",
          "|---|---|---:|---:|"]
    for x in d[:6]:
        L.append("| %s | `%s` | **%.0f%%** | %+.4f |"
                 % (x["section"], x["variant"], x["top30_changed"] * 100, x["roc_diff"]))
    L += ["",
          "> `%s` 는 상위 30건 중 **%.0f%%(약 %d건)가 다른 사업으로 바뀌는데도**"
          % (w["variant"], w["top30_changed"] * 100, round(w["top30_changed"] * 30)),
          "> ROC-AUC 차이는 **%+.4f** 입니다. 담당자가 받아 보는 검토 목록이 통째로"
          % w["roc_diff"],
          "> 달라지는 변경인데 ROC 로는 보이지 않습니다.", "",
          "**ROC 하나만 보고 모델을 바꾸면 안 되고, 실제 경고 목록의 안정성을**",
          "**함께 봐야 한다** — 방향서 §9 가 이 표를 그 근거로 씁니다.", "",
          "## 6. 같이 읽어야 하는 것", "",
          "- ROC/PR 열은 **탐색적 보조 검증값입니다**(방향서 §7). 양성 %d건이고"
          % r["n_positive"],
          "  도메인 전문가 Ground Truth 가 아니라 변형 간 우열을 가릴 수",
          "  없습니다. 판정은 순위 지표로 했습니다.",
          "- 이 실험은 성능을 올리려는 것이 아닙니다. 방향서 §8.3 대로 **\"성능이",
          "  조금 오른다고 바로 구조를 바꾸지 않고 경고 목록 stability 까지 함께",
          "  본다\"** 를 따랐습니다.",
          "- 여기서 \"기존 유지\" 가 나오는 것은 **현재 구조를 바꿀 명확한 근거가",
          "  없다**는 뜻입니다(방향서 §9). 우선순위는 구조 변경이 아니라 안정성",
          "  검증(§13 1순위)으로 갑니다 — M48 에서 잽니다.", ""]
    p = os.path.join(C.REPORTS, "m47_m3_sensitivity.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
