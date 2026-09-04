r"""M49 — Model 3 Synthetic Stress Test: 방향성이 상식적으로 작동하는가 (방향서 §8.5).

이 실험의 존재 이유 — 사람 라벨 없이 모델을 검증하는 길
    방향서 §6·§7 이 대규모 사람 라벨링을 우선순위에서 뺐다. 비전문가가
    "이 사업이 행정적으로 비전형인가"를 판정하기 어렵기 때문이다. 그러면
    모델이 제대로 도는지 무엇으로 확인하는가.

    §8.5 의 답: **사업 하나를 잡고 설계 수치를 인위적으로 움직여 본다.**

        기업당 지원액 1배 -> 2배 -> 5배 -> 10배
        사업기간     1년 -> 3년 -> 5년 -> 10년

    비교군에서 멀어질수록 이례성 점수가 올라가야 한다. 이건 정책 판단이
    아니라 **산수**라서 사람 라벨이 필요 없다. 안 오르면 모델이 고장난 것이다.

무엇을 재는가 — 세 가지

    1. 단조성      비교군 중앙값에서 멀어질수록 점수가 오르는가.
                  |중앙값에서의 거리| 와 점수의 Spearman 순위상관으로 잰다.

    2. U자 형태    점수의 최소값이 스윕 **가운데**에 있는가. 끝에 있으면
                  한쪽 방향으로만 반응한다는 뜻이라 대칭성이 깨진 것이다.

    3. 축 귀속     `기업당 지원액`만 키웠는데 차이벡터 D 의 최대 성분이
                  그 축인가. 방향서 §13 4순위(어떤 설계축이 왜 다른지
                  문장화)가 성립하려면 이게 맞아야 한다. 틀리면 설명이
                  엉뚱한 축을 지목한다.

무엇을 하지 않는가
    구조를 바꾸지 않는다. M44 Freeze 그대로 채점하고, 결과가 나빠도 여기서
    고치지 않는다. 이 실험은 **진단**이지 튜닝이 아니다.

읽을 때 주의
    원래 값이 이미 비교군 중앙에서 벗어난 사업이 있다. 그런 사업은 배수 1배
    (원본)가 최저점이 아니다. 그래서 기준을 '배수 1배'가 아니라 **'비교군
    중앙값에서의 거리'** 로 잡는다. 이걸 안 맞추면 멀쩡한 모델도 단조성이
    깨진 것처럼 보인다.
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
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m38_m3_vector_direction import CAT, MIN_COHORT, NUM
from m47_m3_sensitivity import build_vectors_v

SEED = 42
N_CASES = 60                       # 스트레스를 걸 실제 사업 수
MULTS = [0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]
LOG_AXES = {"log_per_recipient", "log_support_count"}    # 이미 log 스케일인 축
AXIS_KR = {"log_per_recipient": "기업당 지원액", "log_support_count": "지원 기업수",
           "project_duration": "사업기간", "support_ratio": "지원비율"}


def perturb(row, axis, mult):
    """한 축만 배수로 움직인 사본. log 축은 더하고, 원 스케일 축은 곱한다."""
    r = row.copy()
    if axis in LOG_AXES:
        r[axis] = r[axis] + np.log10(mult)
    else:
        v = r[axis] * mult
        r[axis] = min(v, 100.0) if axis == "support_ratio" else v
    return r


def score_and_direction(fit, apply_df):
    """M44 Freeze 구조로 채점하고, 차이벡터 D 의 수치축 성분도 같이 낸다.

    D 성분이 필요한 이유는 '축 귀속' 검증 때문이다 — 어느 축을 흔들었을 때
    모델이 그 축을 지목하는지 보려면 점수만으로는 알 수 없다.
    """
    Xtr, Xap, n_num = build_vectors_v(fit, apply_df)
    k2_tr = fit["support_type"].astype(str) + "|" + fit["support_method"].astype(str)
    k1_tr = fit["support_type"].astype(str)
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
    for lvl, key in ({resolve(a, b) for a, b in zip(k2_tr, k1_tr)} |
                     {resolve(a, b) for a, b in zip(k2_ap, k1_ap)}):
        if lvl == "2":
            mask = (k2_tr == key).to_numpy()
        elif lvl == "1":
            mask = (k1_tr == key).to_numpy()
        else:
            mask = np.ones(len(fit), bool)
        M = Xtr[mask]
        c = M.mean(0)
        groups[(lvl, key)] = {"c": c, "dist": np.linalg.norm(M - c, axis=1),
                              "med": np.median(M[:, :n_num], axis=0)}

    pct = np.empty(len(apply_df))
    Dnum = np.empty((len(apply_df), n_num))
    med = np.empty((len(apply_df), n_num))
    for i in range(len(apply_df)):
        g = groups[resolve(k2_ap.iloc[i], k1_ap.iloc[i])]
        d = Xap[i] - g["c"]
        pct[i] = float((g["dist"] <= np.linalg.norm(d)).mean()) * 100
        Dnum[i] = d[:n_num]
        med[i] = g["med"]
    return pct, Dnum, Xap[:, :n_num], med


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    rng = np.random.default_rng(SEED)

    print("M49 — Synthetic Stress Test (방향서 §8.5)")
    print("  pool %d행 · 사람 라벨 0건" % len(train))
    print("  실제 사업 %d건을 잡아 설계축을 %s배로 흔든다"
          % (N_CASES, "/".join(str(m) for m in MULTS)))

    # 큰 비교군에서 고르게 뽑는다. 비교군이 얇으면 percentile 자체가 불안정해
    # 단조성이 깨져도 모델 탓인지 표본 탓인지 못 가린다.
    key = train["support_type"].astype(str) + "|" + train["support_method"].astype(str)
    big = key.value_counts()
    big = big[big >= 50].index
    pick = train[key.isin(big)].sample(n=N_CASES, random_state=SEED).reset_index(drop=True)
    print("  대상 비교군 %d종 (각 50건 이상)" % len(big))

    rows, meta = [], []
    for ci, (_, r) in enumerate(pick.iterrows()):
        for axis in NUM:
            if pd.isna(r[axis]):
                continue
            for m in MULTS:
                rows.append(perturb(r, axis, m))
                meta.append({"case": ci, "axis": axis, "mult": m,
                             "row_id": r["row_id"]})
    pert = pd.DataFrame(rows).reset_index(drop=True)
    meta = pd.DataFrame(meta)
    print("  생성된 변형 %d건 (%d사업 x 축 x 배수)" % (len(pert), N_CASES))

    pct, Dnum, Xnum, med = score_and_direction(train, pert)
    meta["score"] = pct
    # 비교군 중앙값에서의 거리. 원래 값이 중앙에서 벗어난 사업 때문에 필요하다.
    ai = {a: i for i, a in enumerate(NUM)}
    meta["dev"] = [abs(Xnum[i, ai[meta.loc[i, "axis"]]] - med[i, ai[meta.loc[i, "axis"]]])
                   for i in range(len(meta))]
    meta["argmax_axis"] = [NUM[int(np.argmax(np.abs(Dnum[i])))] for i in range(len(meta))]

    # ---------------------------------------------------------- 1. 단조성
    print("\n== 1. 단조성 — 비교군 중앙값에서 멀어질수록 점수가 오르는가")
    print("  %-16s %8s %12s %12s" % ("축", "사례수", "Spearman", "단조 비율"))
    mono = {}
    for axis, g in meta.groupby("axis"):
        rhos = []
        for _, gg in g.groupby("case"):
            if gg["dev"].nunique() > 1:
                rhos.append(float(spearmanr(gg["dev"], gg["score"]).statistic))
        rhos = [x for x in rhos if not np.isnan(x)]
        pos = float(np.mean([x > 0 for x in rhos]))
        mono[axis] = {"n_cases": len(rhos), "spearman_mean": round(float(np.mean(rhos)), 4),
                      "spearman_median": round(float(np.median(rhos)), 4),
                      "positive_rate": round(pos, 4)}
        print("  %-16s %8d %12.4f %11.0f%%"
              % (AXIS_KR[axis], len(rhos), mono[axis]["spearman_mean"], pos * 100))

    # ------------------------------------------------------------ 2. U자
    print("\n== 2. U자 형태 — 점수 최저점이 스윕 가운데에 있는가")
    print("  %-16s %10s %12s" % ("축", "내부 최저", "끝 최저"))
    ushape = {}
    for axis, g in meta.groupby("axis"):
        interior = 0
        tot = 0
        for _, gg in g.groupby("case"):
            gg = gg.sort_values("mult")
            if len(gg) < 3:
                continue
            tot += 1
            if 0 < int(np.argmin(gg["score"].to_numpy())) < len(gg) - 1:
                interior += 1
        ushape[axis] = {"n": tot, "interior_min_rate": round(interior / max(1, tot), 4)}
        print("  %-16s %9.0f%% %11.0f%%"
              % (AXIS_KR[axis], ushape[axis]["interior_min_rate"] * 100,
                 (1 - ushape[axis]["interior_min_rate"]) * 100))

    # -------------------------------------------------------- 3. 축 귀속
    print("\n== 3. 축 귀속 — 흔든 축을 모델이 지목하는가 (방향서 §13 4순위)")
    print("  %-16s %10s %10s %10s" % ("흔든 축", "x0.1", "x10", "극단 평균"))
    attrib = {}
    for axis, g in meta.groupby("axis"):
        row = {}
        for m in (0.1, 10.0):
            sub = g[g["mult"] == m]
            row["x%.1f" % m] = round(float((sub["argmax_axis"] == axis).mean()), 4)
        ext = g[g["mult"].isin([0.1, 10.0])]
        row["extreme_mean"] = round(float((ext["argmax_axis"] == axis).mean()), 4)
        attrib[axis] = row
        print("  %-16s %9.0f%% %9.0f%% %9.0f%%"
              % (AXIS_KR[axis], row["x0.1"] * 100, row["x10.0"] * 100,
                 row["extreme_mean"] * 100))

    # ------------------------------------------------------ 점수 궤적 예시
    print("\n== 점수 궤적 예시 (기업당 지원액)")
    ex = meta[(meta["axis"] == "log_per_recipient")
              & (meta["case"] == meta["case"].iloc[0])].sort_values("mult")
    t = pick.iloc[int(ex["case"].iloc[0])]
    print("  %s" % str(t["title"])[:56])
    print("  %-8s %s" % ("배수", " ".join("%7.1f" % m for m in ex["mult"])))
    print("  %-8s %s" % ("점수", " ".join("%6.1f%%" % s for s in ex["score"])))

    rep = {
        "질문": "사람 라벨 없이 모델 방향성이 상식적으로 작동하는지 (방향서 §8.5)",
        "구조": "M44 Freeze 그대로. 이 실험은 진단이지 튜닝이 아니다",
        "n_pool": int(len(train)), "n_cases": N_CASES,
        "multipliers": MULTS, "n_perturbed": int(len(pert)),
        "기준": ("배수 1배가 아니라 '비교군 중앙값에서의 거리'를 x축으로 쓴다. "
               "원래 값이 이미 중앙에서 벗어난 사업이 있기 때문이다"),
        "monotonicity": mono, "u_shape": ushape, "axis_attribution": attrib,
    }
    C.save_report("m49_m3_stress.json", rep)
    write_md(rep)


def write_md(r):
    L = ["# M49 — Synthetic Stress Test: 방향성이 상식적으로 작동하는가", "",
         "> 방향서 §8.5. 대규모 사람 라벨링을 우선순위에서 뺀 뒤(§6·§7), **사람",
         "> 판단 없이 모델을 검증하는 길**입니다. 사업 하나를 잡고 설계 수치를",
         "> 인위적으로 움직여 이례성 점수가 합리적으로 반응하는지 봅니다.", "",
         "```text",
         "pool %d행 · 사람 라벨 0건" % r["n_pool"],
         "실제 사업 %d건 x 설계축 x 배수 %s = 변형 %d건"
         % (r["n_cases"], "/".join(str(m) for m in r["multipliers"]), r["n_perturbed"]),
         "구조  %s" % r["구조"],
         "```", "",
         "> **기준을 배수 1배로 잡지 않았습니다.** %s" % r["기준"], "",
         "## 1. 단조성 — 멀어질수록 점수가 오르는가", "",
         "| 설계축 | 사례수 | Spearman 평균 | 단조 비율 |", "|---|---:|---:|---:|"]
    for a, v in r["monotonicity"].items():
        L.append("| %s | %d | %.4f | **%.0f%%** |"
                 % (AXIS_KR[a], v["n_cases"], v["spearman_mean"],
                    v["positive_rate"] * 100))
    L += ["",
          "비교군 중앙값에서의 거리와 점수의 순위상관입니다. 양수면 멀어질수록",
          "점수가 오른다는 뜻이고, **단조 비율**은 그 방향이 맞은 사업의 비율입니다.", "",
          "## 2. U자 형태 — 최저점이 가운데 있는가", "",
          "| 설계축 | 내부 최저 | 끝 최저 |", "|---|---:|---:|"]
    for a, v in r["u_shape"].items():
        L.append("| %s | **%.0f%%** | %.0f%% |"
                 % (AXIS_KR[a], v["interior_min_rate"] * 100,
                    (1 - v["interior_min_rate"]) * 100))
    L += ["",
          "> 최저점이 스윕 끝에 있으면 한쪽 방향으로만 반응한다는 뜻입니다.",
          "> 다만 원래 값이 비교군 극단에 있던 사업은 정상적으로도 끝이 최저가",
          "> 될 수 있어, 이 값만으로 고장을 판정하지 않습니다.", "",
          "## 3. 축 귀속 — 흔든 축을 모델이 지목하는가", "",
          "`기업당 지원액`만 키웠을 때 차이벡터 `D` 의 최대 성분이 그 축인지",
          "봅니다. 방향서 §13 4순위(**어떤 설계축이 왜 다른지 문장화**)가",
          "성립하려면 이게 맞아야 합니다 — 틀리면 설명이 엉뚱한 축을 지목합니다.", "",
          "| 흔든 축 | ×0.1 | ×10 | 극단 평균 |", "|---|---:|---:|---:|"]
    for a, v in r["axis_attribution"].items():
        L.append("| %s | %.0f%% | %.0f%% | **%.0f%%** |"
                 % (AXIS_KR[a], v["x0.1"] * 100, v["x10.0"] * 100,
                    v["extreme_mean"] * 100))
    L += ["", "## 4. 같이 읽어야 하는 것", "",
          "- 이 실험은 **진단이지 튜닝이 아닙니다.** 결과가 나빠도 여기서 구조를",
          "  고치지 않습니다(방향서 §8.3 의 태도와 같습니다).",
          "- 점수가 오르는 것이 \"그 사업이 나쁘다\"는 뜻이 아닙니다. 방향서 §5",
          "  대로 **'유사사업 대비 드문 설계 조합'** 이라는 뜻입니다.",
          "- 사람 라벨을 한 건도 쓰지 않았습니다. 도메인 전문가 Ground Truth 가",
          "  없는 현 단계에서 모델 건전성을 확인하는 유일한 경로입니다.", ""]
    p = os.path.join(C.REPORTS, "m49_m3_stress.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
