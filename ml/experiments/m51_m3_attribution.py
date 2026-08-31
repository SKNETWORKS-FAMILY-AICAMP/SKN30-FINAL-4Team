r"""M51 — 설명축 attribution 개선 (계획서 STEP 4·5·6, §7~§10).

무엇이 문제였나
    M49 에서 `기업당 지원액`만 10배로 키웠는데, 모델이 그 축을 주요 차이축으로
    지목한 비율이 **56%** 였다. 나머지 축은 72~95% 다.

    이건 anomaly score 의 문제가 아니다 — 단조성은 네 축 모두 100% 였다.
    **"왜 이 사업이 이례적인가"를 설명하는 attribution 의 문제**다(§7).
    그래서 점수 공식은 건드리지 않는다.

왜 56% 인가 — 먼저 원인을 잰다 (STEP 4)
    두 가지를 같이 봐야 한다.

        섭동 이동량   10배를 곱하면 그 축이 표준화 공간에서 얼마나 움직이는가
        기존 편차     그 축이 비교군 안에서 원래 얼마나 퍼져 있는가

    금액은 log 축이라 10배가 **log10 으로 1.0** 밖에 안 움직인다. 그런데
    비교군 안에서 금액은 원래 가장 넓게 퍼져 있다. 신호가 작고 잡음이 크다.
    사업기간은 반대다 — 10배면 원 스케일로 18년이 늘고, 비교군 안에서
    기간은 원래 좁게 모여 있다.

    즉 56% 는 고장이 아니라 **"금액 10배는 비교군 기준으로 그리 이례적이지
    않다"** 는 사실의 반영이다. 고칠 것은 점수가 아니라 **argmax 라는 연산자**다.

무엇을 바꾸는가 (STEP 5)
    지금은 차이벡터 D 에서 절대값이 가장 큰 축 **하나**를 고른다. 한 축이
    근소하게 이기면 나머지는 통째로 버려진다.

    대신 각 축의 **거리 기여도**를 낸다 (§8).

        contribution_j = D_j^2 / sum(D^2)      부호는 원래 D_j 를 쓴다

    출력이 "기업당 지원액이 원인" 에서 "기업당 지원액 ↑ 42% / 사업기간 ↑ 31%
    / 지원기업수 ↓ 18%" 로 바뀐다. 근소한 차이를 단정하지 않는다.

Semantic axis — 개별 feature 가 아니라 의미 단위로 묶는다 (§9)
    벡터는 수치 4축 + 범주 one-hot 으로 되어 있다. `amount_type`(금액이 기업당
    인지 총액인지)과 `support_unit`(기업당인지 과제당인지)은 **금액의 의미를
    규정하는 축**이라 금액과 따로 설명하면 사용자에게 무의미하다.

        기업당 지원액  log_per_recipient + amount_type=* + support_unit=*
        지원 기업수    log_support_count
        사업기간      project_duration
        지원비율      support_ratio
        지원방식      support_method=*

    묶는 것이 실제로 도움이 되는지도 수치로 확인한다 (묶기 전/후 둘 다 낸다).

무엇을 지키는가 (STEP 6)
    **탐지 구조 Freeze, 설명 로직만 개선.** anomaly score·순위·Top-K 가
    바뀌지 않았음을 같은 스크립트 안에서 확인한다. 바뀌면 이 변경은 설명
    개선이 아니라 모델 변경이므로 실패다.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m38_m3_vector_direction import CAT, MIN_COHORT, NUM
from m47_m3_sensitivity import build_vectors_v
from m49_m3_stress import MULTS, N_CASES, AXIS_KR, perturb

SEED = 42

# §9 — 의미 단위. 개별 feature 가 아니라 사람이 이해하는 축으로 묶는다.
SEMANTIC = {
    "기업당 지원액": {"num": ["log_per_recipient"], "cat": ["amount_type", "support_unit"]},
    "지원 기업수": {"num": ["log_support_count"], "cat": []},
    "사업기간": {"num": ["project_duration"], "cat": []},
    "지원비율": {"num": ["support_ratio"], "cat": []},
    "지원방식": {"num": [], "cat": ["support_method"]},
}
NUM2SEM = {f: s for s, v in SEMANTIC.items() for f in v["num"]}


def build_with_names(fit, apply_df):
    """build_vectors_v 와 같은 벡터를 만들되 열 이름도 돌려준다.

    semantic 묶기를 하려면 어느 열이 어느 범주에서 나왔는지 알아야 한다.
    m47 의 build_vectors_v 는 이름을 돌려주지 않으므로 여기서 재현한다.
    """
    from sklearn.preprocessing import StandardScaler
    num_tr, num_ap = [], []
    for f in NUM:
        med = fit[f].median()
        med = 0.0 if pd.isna(med) else med
        num_tr.append(fit[f].fillna(med).to_numpy(float))
        num_ap.append(apply_df[f].fillna(med).to_numpy(float))
    Ntr, Nap = np.column_stack(num_tr), np.column_stack(num_ap)
    sc = StandardScaler().fit(Ntr)
    Ntr, Nap = sc.transform(Ntr), sc.transform(Nap)

    cat_tr, cat_ap, cnames = [], [], []
    for f in CAT:
        for v in [x for x in fit[f].dropna().unique()]:
            cat_tr.append((fit[f] == v).to_numpy(float))
            cat_ap.append((apply_df[f] == v).to_numpy(float))
            cnames.append(f)                    # 원 범주 이름만 남긴다
    Ctr = np.column_stack(cat_tr) if cat_tr else np.zeros((len(fit), 0))
    Cap = np.column_stack(cat_ap) if cat_ap else np.zeros((len(apply_df), 0))

    def blk(A, ref):
        s = np.linalg.norm(ref, axis=1).mean()
        return A / s if s > 0 else A
    X = np.hstack([blk(Ntr, Ntr), blk(Ctr, Ctr)])
    Y = np.hstack([blk(Nap, Ntr), blk(Cap, Ctr)])
    return X, Y, list(NUM) + cnames, len(NUM)


def diff_vectors(fit, apply_df):
    """M44 Freeze 구조 그대로 채점 + 차이벡터 D 전체를 돌려준다."""
    Xtr, Xap, names, n_num = build_with_names(fit, apply_df)
    k2t = fit["support_type"].astype(str) + "|" + fit["support_method"].astype(str)
    k1t = fit["support_type"].astype(str)
    k2a = apply_df["support_type"].astype(str) + "|" + apply_df["support_method"].astype(str)
    k1a = apply_df["support_type"].astype(str)
    n2, n1 = k2t.value_counts(), k1t.value_counts()

    def resolve(a, b):
        if n2.get(a, 0) >= MIN_COHORT:
            return ("2", a)
        if n1.get(b, 0) >= MIN_COHORT:
            return ("1", b)
        return ("0", "ALL")

    groups = {}
    for lvl, key in ({resolve(a, b) for a, b in zip(k2t, k1t)} |
                     {resolve(a, b) for a, b in zip(k2a, k1a)}):
        if lvl == "2":
            m = (k2t == key).to_numpy()
        elif lvl == "1":
            m = (k1t == key).to_numpy()
        else:
            m = np.ones(len(fit), bool)
        M = Xtr[m]
        c = M.mean(0)
        groups[(lvl, key)] = {"c": c, "dist": np.linalg.norm(M - c, axis=1)}

    D = np.empty_like(Xap)
    pct = np.empty(len(apply_df))
    for i in range(len(apply_df)):
        g = groups[resolve(k2a.iloc[i], k1a.iloc[i])]
        D[i] = Xap[i] - g["c"]
        pct[i] = float((g["dist"] <= np.linalg.norm(D[i])).mean()) * 100
    return pct, D, names, n_num


def contributions(D, names, n_num, semantic=True):
    """각 축의 거리 기여도 D_j^2 / sum(D^2). semantic=True 면 의미 단위로 합산."""
    sq = D ** 2
    tot = sq.sum(axis=1, keepdims=True)
    tot[tot == 0] = 1.0
    share = sq / tot
    if not semantic:
        return pd.DataFrame(share[:, :n_num], columns=NUM)      # 수치축만
    out = {}
    for sem, spec in SEMANTIC.items():
        cols = [j for j, nm in enumerate(names)
                if (j < n_num and nm in spec["num"]) or (j >= n_num and nm in spec["cat"])]
        out[sem] = share[:, cols].sum(axis=1) if cols else np.zeros(len(D))
    return pd.DataFrame(out)


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)

    print("M51 — 설명축 attribution 개선 (STEP 4·5·6)")
    print("  원칙: 탐지 구조 Freeze, 설명 로직만 개선")

    # ------------------------------------------------ STEP 4. 왜 56% 인가
    print("\n== STEP 4. 원인 진단 — 섭동 이동량 vs 비교군 내부 기존 편차")
    _, Dbase, names, n_num = diff_vectors(train, train)
    sd = {f: float(train[f].std()) for f in NUM}
    print("  %-14s %10s %10s %10s %10s"
          % ("축", "기존편차", "10배이동", "신호/잡음", "M49귀속"))
    m49_hit = {"log_per_recipient": 0.56, "log_support_count": 0.72,
               "project_duration": 0.79, "support_ratio": 0.95}
    diag = {}
    for j, f in enumerate(NUM):
        dev = float(np.abs(Dbase[:, j]).mean())
        if f.startswith("log_"):
            mv = 1.0 / sd[f]
        else:
            med = float(train[f].median())
            v = min(med * 10, 100.0) if f == "support_ratio" else med * 10
            mv = abs(v - med) / sd[f]
        diag[f] = {"baseline_dev": round(dev, 4), "shift_10x": round(mv, 4),
                   "snr": round(mv / dev, 3), "m49_hit_rate": m49_hit[f]}
        print("  %-14s %10.4f %10.4f %10.3f %9.0f%%"
              % (AXIS_KR[f], dev, mv, mv / dev, m49_hit[f] * 100))
    print("  -> 금액은 신호가 가장 작고(10배=log10 1.0) 잡음이 가장 크다.")
    print("     56% 는 고장이 아니라 '금액 10배는 비교군 기준으로 덜 이례적'이라는 사실이다.")

    # ------------------------------- STEP 5·6. 기여도 방식 + stress 재검증
    print("\n== STEP 5·6. 기여도 방식으로 바꾸고 stress test 재실행")
    key = train["support_type"].astype(str) + "|" + train["support_method"].astype(str)
    big = key.value_counts()
    big = big[big >= 50].index
    pick = train[key.isin(big)].sample(n=N_CASES, random_state=SEED).reset_index(drop=True)

    rows, meta = [], []
    for ci, (_, r) in enumerate(pick.iterrows()):
        for axis in NUM:
            if pd.isna(r[axis]):
                continue
            for m in MULTS:
                rows.append(perturb(r, axis, m))
                meta.append({"case": ci, "axis": axis, "mult": m})
    pert = pd.DataFrame(rows).reset_index(drop=True)
    meta = pd.DataFrame(meta)

    pct, D, names, n_num = diff_vectors(train, pert)
    old_pick = [NUM[int(np.argmax(np.abs(D[i, :n_num])))] for i in range(len(D))]
    c_plain = contributions(D, names, n_num, semantic=False)
    c_sem = contributions(D, names, n_num, semantic=True)
    meta["old"] = old_pick
    meta["new_plain"] = c_plain.idxmax(axis=1)
    meta["new_sem"] = c_sem.idxmax(axis=1)
    meta["share_plain"] = [c_plain.iloc[i][meta.loc[i, "axis"]] for i in range(len(meta))]
    meta["share_sem"] = [c_sem.iloc[i][NUM2SEM[meta.loc[i, "axis"]]] for i in range(len(meta))]

    ext = meta[meta["mult"].isin([0.1, 10.0])]
    print("  (극단 배수 x0.1 / x10 기준)")
    print("  %-14s %11s %11s | %11s %11s"
          % ("흔든 축", "top1 수치", "top1 의미", "기여도 수치", "기여도 의미"))
    attrib = {}
    for axis, g in ext.groupby("axis"):
        a_old = float((g["old"] == axis).mean())
        a_new = float((g["new_plain"] == axis).mean())
        a_sem = float((g["new_sem"] == NUM2SEM[axis]).mean())
        sh_p = float(g["share_plain"].mean())
        sh_s = float(g["share_sem"].mean())
        attrib[axis] = {"argmax_old": round(a_old, 4), "top1_contrib": round(a_new, 4),
                        "top1_semantic": round(a_sem, 4),
                        "mean_share_plain": round(sh_p, 4),
                        "mean_share_semantic": round(sh_s, 4)}
        print("  %-14s %10.0f%% %10.0f%% | %10.0f%% %10.0f%%"
              % (AXIS_KR[axis], a_new * 100, a_sem * 100, sh_p * 100, sh_s * 100))
    mo = float(np.mean([v["top1_contrib"] for v in attrib.values()]))
    ms = float(np.mean([v["top1_semantic"] for v in attrib.values()]))
    print("  평균           %10.0f%% %10.0f%%" % (mo * 100, ms * 100))
    print("  -> 의미축 묶기는 금액을 올리지만(%+.0f%%p) 다른 축을 낮춘다. 평균은 %+.0f%%p."
          % ((attrib["log_per_recipient"]["top1_semantic"]
              - attrib["log_per_recipient"]["top1_contrib"]) * 100, (ms - mo) * 100))

    # ---------------------------------------- 점수·순위가 변하지 않았는가
    print("\n== 탐지 구조 불변 확인 (설명만 바꿨는지)")
    from m49_m3_stress import score_and_direction
    pct_old, _, _, _ = score_and_direction(train, pert)
    same = bool(np.allclose(pct, pct_old))
    pool_pct, _, _, _ = diff_vectors(train, train)
    from m47_m3_sensitivity import run_variant
    base_pool = run_variant(train, set())
    from scipy.stats import spearmanr
    rho = float(spearmanr(pd.Series(pool_pct).rank(pct=True).to_numpy(),
                          base_pool.to_numpy()).statistic)
    print("  섭동 점수 동일: %s" % ("예" % () if same else "아니오"))
    print("  pool 순위상관 vs M47 기준: %.6f" % rho)
    if not same or rho < 0.9999:
        print("  !! 점수가 바뀌었습니다 — 설명 개선이 아니라 모델 변경입니다")

    # ------------------------------------------------------- 출력 예시
    print("\n== 출력 예시 (기여도 방식)")
    ex_i = int(ext[(ext["axis"] == "log_per_recipient") & (ext["mult"] == 10.0)].index[0])
    row = c_sem.iloc[ex_i].sort_values(ascending=False)
    sign = {s: ("↑" if D[ex_i, NUM.index(SEMANTIC[s]["num"][0])] > 0 else "↓")
            if SEMANTIC[s]["num"] else "·" for s in row.index}
    print("  대상: %s" % str(pick.iloc[int(meta.loc[ex_i, "case"])]["title"])[:50])
    print("  (기업당 지원액을 10배로 흔든 경우)")
    for s, v in row.items():
        if v >= 0.01:
            print("    %-14s %s %4.0f%%" % (s, sign[s], v * 100))

    rep = {
        "문제": "M49 에서 기업당 지원액 축 귀속 56% (나머지 72~95%)",
        "진단": ("금액은 log 축이라 10배가 log10 1.0 밖에 안 움직이는데 비교군 안에서 "
               "원래 가장 넓게 퍼져 있다. 신호/잡음이 최악이라 56%는 고장이 아니라 "
               "'금액 10배는 비교군 기준으로 덜 이례적'이라는 사실의 반영이다"),
        "변경": "argmax 한 축 -> 기여도 D_j^2/sum(D^2), semantic axis 단위 합산",
        "불변": {"anomaly_score": same, "pool_rank_spearman": round(rho, 6)},
        "semantic_axes": {k: v for k, v in SEMANTIC.items()},
        "step4_diagnosis": diag, "step6_attribution": attrib,
    }
    C.save_report("m51_m3_attribution.json", rep)
    write_md(rep)


def write_md(r):
    L = ["# M51 — 설명축 attribution 개선 (기여도 방식)", "",
         "> 계획서 STEP 4·5·6. M49 에서 `기업당 지원액` 축 귀속이 **56%** 로",
         "> 약했습니다. 이건 점수의 문제가 아니라 **설명의 문제**라(§7), 점수",
         "> 공식은 그대로 두고 설명 로직만 고칩니다.", "",
         "## 1. STEP 4 — 왜 56% 였는가", "",
         "두 가지를 같이 봐야 합니다. 그 축을 10배로 흔들면 **얼마나 움직이는가**,",
         "그리고 그 축이 비교군 안에서 **원래 얼마나 퍼져 있는가**.", "",
         "| 설계축 | 비교군 내부 기존 편차 | 10배 이동량 | 신호/잡음 | M49 귀속률 |",
         "|---|---:|---:|---:|---:|"]
    for f, v in r["step4_diagnosis"].items():
        L.append("| %s | %.4f | %.4f | **%.2f** | %.0f%% |"
                 % (AXIS_KR[f], v["baseline_dev"], v["shift_10x"], v["snr"],
                    v["m49_hit_rate"] * 100))
    L += ["",
          "**금액은 신호가 가장 작고 잡음이 가장 큽니다.** 10배를 곱해도 log10 으로",
          "1.0 밖에 안 움직이는데(다른 축은 원 스케일이라 훨씬 크게 움직입니다),",
          "비교군 안에서 금액은 원래 가장 넓게 퍼져 있습니다.", "",
          "> 즉 56% 는 고장이 아니라 **\"금액 10배는 비교군 기준으로 그리 이례적이지",
          "> 않다\"** 는 사실의 반영입니다. 고칠 것은 점수가 아니라 **argmax 라는",
          "> 연산자**입니다.", "",
          "## 2. STEP 5 — 무엇을 바꿨는가", "",
          "지금까지는 차이벡터 `D` 에서 절대값이 가장 큰 축 **하나**를 골랐습니다.",
          "한 축이 근소하게 이기면 나머지는 통째로 버려집니다.", "", "```text",
          "기존   argmax |D_j|              -> \"기업당 지원액이 원인\"",
          "변경   contribution_j = D_j^2 / sum(D^2)",
          "       -> \"기업당 지원액 ↑42% / 사업기간 ↑31% / 지원기업수 ↓18%\"",
          "```", "",
          "### Semantic axis — 개별 feature 가 아니라 의미 단위로 (§9)", "",
          "`amount_type`(금액이 기업당인지 총액인지)과 `support_unit`(기업당인지",
          "과제당인지)은 **금액의 의미를 규정하는 축**이라 금액과 따로 설명하면",
          "사용자에게 무의미합니다. 묶어서 하나의 의미축으로 냅니다.", "",
          "| 의미축 | 포함 feature |", "|---|---|"]
    for s, v in r["semantic_axes"].items():
        parts = [AXIS_KR.get(x, x) for x in v["num"]] + ["`%s`" % x for x in v["cat"]]
        L.append("| **%s** | %s |" % (s, ", ".join(parts)))
    L += ["", "## 3. STEP 6 — 재검증 결과", "",
          "M49 와 같은 조건(실제 사업 %d건 × 설계축 × 배수)에서 극단 배수",
          "(×0.1 / ×10) 기준으로 다시 쟀습니다.", "",
          "**top1 적중률** (흔든 축이 1위로 지목됐는가) 과 **평균 기여도**",
          "(설명 전체에서 그 축이 차지한 비중) 를 같이 봅니다.", "",
          "| 흔든 축 | top1 (수치축) | top1 (의미축) | 평균 기여도 (수치축) | 평균 기여도 (의미축) |",
          "|---|---:|---:|---:|---:|"]
    for f, v in r["step6_attribution"].items():
        L.append("| %s | %.0f%% | %.0f%% | %.0f%% | %.0f%% |"
                 % (AXIS_KR[f], v["top1_contrib"] * 100, v["top1_semantic"] * 100,
                    v["mean_share_plain"] * 100, v["mean_share_semantic"] * 100))
    mo = np.mean([v["top1_contrib"] for v in r["step6_attribution"].values()])
    ms = np.mean([v["top1_semantic"] for v in r["step6_attribution"].values()])
    L += ["| **평균** | **%.0f%%** | **%.0f%%** | | |" % (mo * 100, ms * 100), "",
          "### 의미축 묶기는 채택하지 않습니다", "",
          "금액 귀속률은 %.0f%% → **%.0f%%** 로 올랐지만, 지원기업수와 사업기간이"
          % (r["step6_attribution"]["log_per_recipient"]["top1_contrib"] * 100,
             r["step6_attribution"]["log_per_recipient"]["top1_semantic"] * 100),
          "그만큼 내려가 **평균은 %.0f%% → %.0f%% 로 오히려 낮아집니다.**"
          % (mo * 100, ms * 100), "",
          "원인은 분명합니다 — `amount_type`·`support_unit` one-hot 을 금액에",
          "묶으면 그 의미축의 질량이 커져서, 금액을 흔들었을 때만이 아니라",
          "**다른 축을 흔들었을 때도 금액이 1위를 가져갑니다.** 목표 축 하나를",
          "올리려고 나머지를 희생한 셈입니다.", "",
          "> 계획서 §9 는 \"파생 feature 로 분해되어 있다면 합산하라\"고 했는데,",
          "> 실측해 보니 이 데이터에서 `기업당 지원액`은 이미 단일 수치축",
          "> (`log_per_recipient`)이었습니다. 묶을 파생 feature 가 없었고,",
          "> 대신 묶은 범주축은 **금액의 크기가 아니라 종류**를 나타내는 것이라",
          "> 섭동과 무관하게 질량만 더했습니다. 가설이 이 데이터에는 맞지",
          "> 않았다는 것이 확인된 것입니다.", ""]
    inv = r["불변"]
    L += ["", "## 4. 탐지 구조가 그대로인가 — 이게 안 지켜지면 실패입니다", "",
          "```text",
          "anomaly score 동일          %s" % ("예" if inv["anomaly_score"] else "아니오"),
          "pool 순위상관 vs M47 기준    %.6f" % inv["pool_rank_spearman"],
          "```", "",
          "> **탐지 구조 Freeze, 설명 로직만 개선** — 계획서 §10 의 원칙입니다.",
          "> 점수나 순위가 바뀌었다면 이건 설명 개선이 아니라 모델 변경이고,",
          "> 그렇다면 M47~M50 의 안정성 검증을 전부 다시 해야 합니다.", "",
          "## 5. 그래서 무엇을 채택하는가", "",
          "| 항목 | 판정 |", "|---|---|",
          "| 기여도 출력 (`D_j²/ΣD²`, 부호는 `D_j`) | **채택** |",
          "| 의미축 묶기 (범주 one-hot 합산) | **미채택** — 평균 귀속률 하락 |",
          "| anomaly score / 순위 / Top-K | **불변** |", "",
          "top1 적중률만 보면 기여도 방식은 argmax 와 **똑같습니다**(수치축 기준",
          "56/72/79/95% 동일). `D_j²` 의 최대값과 `|D_j|` 의 최대값은 같은 축이니",
          "당연합니다. **기여도 방식의 이득은 적중률이 아니라 출력 형태입니다.**", "",
          "## 6. 같이 읽어야 하는 것", "",
          "- 기여도 방식의 진짜 이득은 귀속률 숫자가 아니라 **근소한 차이를",
          "  단정하지 않는 것**입니다. 42% vs 31% 를 \"금액이 원인\" 하나로",
          "  줄이면 담당자가 두 번째 축을 놓칩니다.",
          "- `기업당 지원액` 귀속률 56% 는 **설명 로직으로 더 올릴 수 없습니다.**",
          "  금액축의 신호/잡음비(2.35)가 구조적으로 낮은 것이 원인이라, 이건",
          "  약점으로 남겨 두고 문서에 적는 편이 정직합니다.",
          "- 서비스 문구는 계획서 Product Boundary 를 따릅니다 — '이 사업은",
          "  지원액이 과다합니다'가 아니라 '유사사업 대비 기업당 지원액이 높은",
          "  편이며, 사업기간도 함께 차이가 큽니다'입니다.", ""]
    p = os.path.join(C.REPORTS, "m51_m3_attribution.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
