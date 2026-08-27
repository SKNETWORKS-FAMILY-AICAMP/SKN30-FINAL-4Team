r"""M48 — Model 3 안정성 검증: 재표집 · 비교군 (방향서 §8.1·§8.6, 우선순위 1순위).

무엇을 검증하는가 — 정답률이 아니라 신호의 안정성
    방향서 §1 이 Model 3 를 다시 정의했다. 이 모델은 사업이 정상인지 이상한지
    판정하지 않는다. **유사사업 비교군 대비 얼마나 떨어져 있는지**를 계산해
    검토 우선순위를 보조한다.

    그러면 검증해야 할 것도 바뀐다.

        판정 정확도 (사람 라벨 대조)   -> 도메인 전문가 GT 가 없어 불가 (§6·§7)
        **신호 안정성**                -> 라벨 없이 pool 전체에서 잴 수 있다

    방향서 §8 이 그래서 "사람이 정책적 정상/이상을 판정하는 대신, Model 3 가
    통계적 이례성 신호로서 안정적인가를 검증한다" 로 과제를 바꿨다. 이 실험이
    그 1순위(§13)다.

이 실험이 라벨을 한 건도 쓰지 않는 이유
    측정 대상이 **순위와 목록**이지 정답이 아니다. Spearman 순위상관과 Top-K
    겹침은 pool %d행 전부에서 나온다. 라벨 53건·양성 5건에 기대지 않으므로
    표본이 작아서 생기는 불확실성이 없다.

    M47 이 그 필요성을 보였다 — RobustScaler 는 상위 30건 중 20건(67%)을
    바꾸는데 ROC-AUC 차이는 +0.0041 이었다. **경고 목록이 통째로 달라지는
    변경을 ROC 는 보지 못한다.** 그래서 목록 자체를 직접 잰다.

무엇을 흔들어 보는가

    §8.1 재표집   비교군 대표벡터 C 를 만드는 표본을 90/80/70/50% 로 줄여
                 다시 만든다. 이 방식에는 난수 초기값이 없다 — 흔들리는
                 원인은 오직 표본이다.

    §8.6 비교군   ① 최소 표본수 MIN_COHORT 를 10~50 으로 바꾼다. 이 값은
                    비교군이 얇을 때 상위 단계로 물러나는 문턱이라, 바꾸면
                    **어떤 사업이 어느 비교군과 비교되는지**가 달라진다.
                 ② 그때 fallback 단계 분포가 어떻게 움직이는지 본다.
                 ③ 비교군별로 순위가 얼마나 흔들리는지 따로 낸다. 흔들리는
                    비교군을 찾아내는 것이 §13 2순위(비교군 품질 개선)의
                    입력이 된다.
                 ④ 대표벡터 C 자체가 얼마나 이동하는지 잰다.

읽는 법 — 무엇이 좋은 값인가
    순위상관과 Top-K 겹침은 **1.0 에 가까울수록** 안정적이다. 다만 절대
    기준선을 미리 정할 근거가 없으므로, M44 가 이미 낸 값(hold-out 상위11
    유지율 0.918)을 참조점으로 함께 적는다.

    대표벡터 이동량은 **비교군 자체의 퍼진 정도로 나눈다.** 절대 거리로
    비교하면 원래 넓게 퍼진 비교군이 항상 불안정해 보인다.
"""
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m38_m3_vector_direction import MIN_COHORT
from m47_m3_sensitivity import build_vectors_v, centroid

SEED = 42
FRACS = [0.9, 0.8, 0.7, 0.5]
N_ITER = 15
TOPK = [30, 39]                 # 39 ~= pool 의 2% (운영 검토 상위 비율)
MIN_COHORT_GRID = [10, 15, 20, 30, 50]
M44_REFERENCE = 0.918           # M44 가 잰 hold-out 상위11 유지율 (참조점)


def score_pool(fit, apply_df, min_cohort=MIN_COHORT, center="mean", scaler="standard"):
    """fit 으로 비교군 대표벡터를 만들고 apply_df 전체를 채점한다.

    M44 에서 Freeze 한 구조 그대로다 — 거리만 쓰고 방향은 점수에 넣지 않는다.
    돌려주는 것: 점수(백분위), 각 행이 떨어진 비교군 단계, 비교군 키,
    그리고 비교군별 대표벡터(이동량 측정용).
    """
    Xtr, Xap, _ = build_vectors_v(fit, apply_df, scaler)
    k2_tr = fit["support_type"].astype(str) + "|" + fit["support_method"].astype(str)
    k1_tr = fit["support_type"].astype(str)
    k2_ap = apply_df["support_type"].astype(str) + "|" + apply_df["support_method"].astype(str)
    k1_ap = apply_df["support_type"].astype(str)
    n2, n1 = k2_tr.value_counts(), k1_tr.value_counts()

    def resolve(a, b):
        if n2.get(a, 0) >= min_cohort:
            return ("2_성격x방식", a)
        if n1.get(b, 0) >= min_cohort:
            return ("1_성격", b)
        return ("0_전체", "ALL")

    groups = {}
    need = ({resolve(a, b) for a, b in zip(k2_tr, k1_tr)} |
            {resolve(a, b) for a, b in zip(k2_ap, k1_ap)})
    for lvl, key in need:
        if lvl.startswith("2"):
            mask = (k2_tr == key).to_numpy()
        elif lvl.startswith("1"):
            mask = (k1_tr == key).to_numpy()
        else:
            mask = np.ones(len(fit), bool)
        M = Xtr[mask]
        c = centroid(M, center)
        d = np.linalg.norm(M - c, axis=1)
        groups[(lvl, key)] = {"c": c, "dist": d, "n": int(mask.sum()),
                              "spread": float(d.mean()) if len(d) else 0.0}

    pct, lvls, keys = np.empty(len(apply_df)), [], []
    for i in range(len(apply_df)):
        gk = resolve(k2_ap.iloc[i], k1_ap.iloc[i])
        g = groups[gk]
        nd = np.linalg.norm(Xap[i] - g["c"])
        pct[i] = float((g["dist"] <= nd).mean()) * 100
        lvls.append(gk[0])
        keys.append(gk[1])
    s = pd.Series(pd.Series(pct).rank(pct=True).to_numpy(),
                  index=apply_df["row_id"].to_numpy())
    return s, pd.Series(lvls, index=apply_df["row_id"].to_numpy()), \
        pd.Series(keys, index=apply_df["row_id"].to_numpy()), groups


def compare(base, var):
    """라벨 없이 재는 두 지표."""
    out = {"spearman": round(float(spearmanr(base.to_numpy(),
                                             var.loc[base.index].to_numpy()).statistic), 4)}
    for k in TOPK:
        b = set(base.sort_values(ascending=False).head(k).index)
        v = set(var.sort_values(ascending=False).head(k).index)
        out["top%d_overlap" % k] = round(len(b & v) / k, 4)
    return out


def centroid_shift(base_g, sub_g):
    """대표벡터가 얼마나 움직였는가. **비교군 자체의 퍼진 정도로 나눈다.**

    절대 거리로 비교하면 원래 넓게 퍼진 비교군이 항상 불안정해 보인다.
    """
    out = []
    for k, g in base_g.items():
        if k not in sub_g or g["spread"] <= 0:
            continue
        out.append(float(np.linalg.norm(sub_g[k]["c"] - g["c"]) / g["spread"]))
    return float(np.mean(out)) if out else np.nan


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    rng = np.random.default_rng(SEED)

    print("M48 — Model 3 안정성 검증 (방향서 §8.1·§8.6 / 우선순위 1순위)")
    print("  pool %d행 · 라벨 미사용 (순위·목록만 잰다)" % len(train))
    print("  기준: M44 Freeze 구조 (거리만, 방향은 점수에 미포함)")

    base, base_lvl, base_key, base_g = score_pool(train, train)
    print("\n  비교군 단계 분포: %s"
          % {k: int(v) for k, v in base_lvl.value_counts().sort_index().items()})

    rep = {
        "정의": ("Model 3 = 유사사업 비교군 대비 설계 조합의 통계적 이례성 산출. "
               "정상/이상 판정기가 아니다 (방향서 §1·§12)"),
        "검증대상": "판정 정확도가 아니라 이례성 신호의 안정성 (방향서 §8)",
        "라벨": "이 실험은 사람 라벨을 한 건도 쓰지 않는다",
        "n_pool": int(len(train)),
        "cohort_level_dist": {k: int(v) for k, v in base_lvl.value_counts().items()},
    }

    # ---------------------------------------------------------- §8.1 재표집
    print("\n== §8.1 재표집 안정성 — 대표벡터를 만드는 표본을 줄여 본다")
    print("  %-8s %-6s %10s %10s %10s %12s"
          % ("표집비율", "회수", "Spearman", "Top30", "Top39", "대표벡터이동"))
    sec = {}
    for frac in FRACS:
        rho, t30, t39, shift = [], [], [], []
        for _ in range(N_ITER):
            sub = train.sample(frac=frac, random_state=int(rng.integers(1e9)))
            s, _, _, g = score_pool(sub, train)
            c = compare(base, s)
            rho.append(c["spearman"])
            t30.append(c["top30_overlap"])
            t39.append(c["top39_overlap"])
            shift.append(centroid_shift(base_g, g))
        sec["frac_%.1f" % frac] = {
            "n_iter": N_ITER,
            "spearman_mean": round(float(np.mean(rho)), 4),
            "spearman_min": round(float(np.min(rho)), 4),
            "top30_mean": round(float(np.mean(t30)), 4),
            "top30_min": round(float(np.min(t30)), 4),
            "top39_mean": round(float(np.mean(t39)), 4),
            "top39_min": round(float(np.min(t39)), 4),
            "centroid_shift_mean": round(float(np.nanmean(shift)), 4)}
        v = sec["frac_%.1f" % frac]
        print("  %-8.0f%% %-6d %10.4f %10.4f %10.4f %12.4f"
              % (frac * 100, N_ITER, v["spearman_mean"], v["top30_mean"],
                 v["top39_mean"], v["centroid_shift_mean"]))
    rep["resample_stability"] = sec
    print("  (참조점: M44 가 잰 hold-out 상위11 유지율 %.3f)" % M44_REFERENCE)

    # ---------------------------------------------------- §8.6 최소 표본수
    print("\n== §8.6 비교군 최소 표본수(MIN_COHORT) 민감도")
    print("  %-10s %10s %10s %10s | %s"
          % ("MIN_COHORT", "Spearman", "Top30", "Top39", "비교군 단계 분포"))
    sec = {}
    for mc in MIN_COHORT_GRID:
        s, lvl, _, _ = score_pool(train, train, min_cohort=mc)
        c = compare(base, s) if mc != MIN_COHORT else {
            "spearman": 1.0, "top30_overlap": 1.0, "top39_overlap": 1.0}
        dist = {k: int(v) for k, v in lvl.value_counts().sort_index().items()}
        sec[str(mc)] = {**c, "level_dist": dist}
        mark = " <- 현행" if mc == MIN_COHORT else ""
        print("  %-10d %10.4f %10.4f %10.4f | %s%s"
              % (mc, c["spearman"], c["top30_overlap"], c["top39_overlap"], dist, mark))
    rep["min_cohort_sensitivity"] = sec

    # ------------------------------------------------ §8.6 비교군별 흔들림
    print("\n== §8.6 비교군별 순위 흔들림 — §13 2순위(비교군 품질)의 입력")
    ranks = []
    for _ in range(N_ITER):
        sub = train.sample(frac=0.8, random_state=int(rng.integers(1e9)))
        s, _, _, _ = score_pool(sub, train)
        ranks.append(s.loc[base.index].rank(pct=True))
    R = pd.concat(ranks, axis=1)
    base_r = base.rank(pct=True)
    vol = (R.sub(base_r, axis=0).abs().mean(axis=1) * 100)      # 백분위 점 단위

    g = pd.DataFrame({"key": base_key, "level": base_lvl, "vol": vol})
    per = (g[g["level"] == "2_성격x방식"].groupby("key")
           .agg(n=("vol", "size"), 순위흔들림=("vol", "mean"))
           .query("n >= 20").sort_values("순위흔들림", ascending=False))
    print("  (80%% 재표집 %d회 / 값 = 백분위 순위가 평균 몇 점 움직이는가)" % N_ITER)
    print("  %-34s %6s %12s" % ("비교군", "n", "순위흔들림"))
    for k, r in per.head(6).iterrows():
        print("  %-34s %6d %11.2f점" % (str(k)[:32], r["n"], r["순위흔들림"]))
    print("  ...")
    for k, r in per.tail(3).iterrows():
        print("  %-34s %6d %11.2f점" % (str(k)[:32], r["n"], r["순위흔들림"]))
    print("  전체 평균 %.2f점 / 중앙값 %.2f점" % (vol.mean(), vol.median()))
    rep["cohort_volatility"] = {
        "설명": "80%% 재표집 %d회에서 백분위 순위가 평균 몇 점 움직이는가" % N_ITER,
        "overall_mean": round(float(vol.mean()), 4),
        "overall_median": round(float(vol.median()), 4),
        "by_level": {k: round(float(v), 4)
                     for k, v in g.groupby("level")["vol"].mean().items()},
        "worst_cohorts": [{"cohort": str(k), "n": int(r["n"]),
                           "volatility": round(float(r["순위흔들림"]), 4)}
                          for k, r in per.head(8).iterrows()],
        "best_cohorts": [{"cohort": str(k), "n": int(r["n"]),
                          "volatility": round(float(r["순위흔들림"]), 4)}
                         for k, r in per.tail(5).iterrows()],
    }
    print("\n  비교군 단계별 평균 흔들림: %s"
          % {k: round(float(v), 2) for k, v in g.groupby("level")["vol"].mean().items()})

    C.save_report("m48_m3_stability.json", rep)
    write_md(rep)


def write_md(r):
    L = ["# M48 — Model 3 안정성 검증 (재표집 · 비교군)", "",
         "> 방향서 §8.1·§8.6, 우선순위 **1순위**. Model 3 는 정상/이상 판정기가",
         "> 아니라 **유사사업 대비 이례성 신호**이므로(§1), 검증할 것도 정답률이",
         "> 아니라 **신호가 안정적인가** 입니다.", "",
         "```text",
         "pool %d행 · 사람 라벨 0건 사용" % r["n_pool"],
         "기준  M44 Freeze 구조 (거리만, 방향은 점수에 미포함)",
         "```", "",
         "## 1. 왜 라벨을 쓰지 않는가", "",
         "재는 대상이 **순위와 목록**이지 정답이 아닙니다. Spearman 순위상관과",
         "Top-K 겹침은 pool 전체에서 나오므로 라벨 53건·양성 5건의 불확실성에",
         "기대지 않습니다.", "",
         "M47 이 그 필요성을 보였습니다 — RobustScaler 는 상위 30건 중 20건(67%)을",
         "바꾸는데 ROC-AUC 차이는 +0.0041 이었습니다. **경고 목록이 통째로**",
         "**달라지는 변경을 ROC 는 보지 못합니다.** 그래서 목록을 직접 잽니다.", "",
         "## 2. §8.1 재표집 안정성", "",
         "비교군 대표벡터 `C` 를 만드는 표본을 줄여 다시 만듭니다. 이 방식에는",
         "난수 초기값이 없어 **흔들리는 원인은 오직 표본**입니다.", "",
         "| 표집비율 | Spearman (평균/최저) | Top30 겹침 | Top39 겹침 | 대표벡터 이동 |",
         "|---:|---:|---:|---:|---:|"]
    for k, v in r["resample_stability"].items():
        L.append("| %s%% | %.4f / %.4f | %.4f / %.4f | %.4f / %.4f | %.4f |"
                 % ("%.0f" % (float(k.split("_")[1]) * 100),
                    v["spearman_mean"], v["spearman_min"],
                    v["top30_mean"], v["top30_min"],
                    v["top39_mean"], v["top39_min"], v["centroid_shift_mean"]))
    L += ["",
          "> 대표벡터 이동량은 **비교군 자체의 퍼진 정도로 나눈 값**입니다. 절대",
          "> 거리로 비교하면 원래 넓게 퍼진 비교군이 항상 불안정해 보입니다.",
          "> 참조점: M44 가 잰 hold-out 상위11 유지율 %.3f." % M44_REFERENCE, "",
          "## 3. §8.6 비교군 최소 표본수 민감도", "",
          "`MIN_COHORT` 는 비교군이 얇을 때 상위 단계로 물러나는 문턱입니다.",
          "바꾸면 **어떤 사업이 어느 비교군과 비교되는지**가 달라집니다.", "",
          "| MIN_COHORT | Spearman | Top30 겹침 | Top39 겹침 | 비교군 단계 분포 |",
          "|---:|---:|---:|---:|---|"]
    for k, v in r["min_cohort_sensitivity"].items():
        mark = " **(현행)**" if int(k) == MIN_COHORT else ""
        L.append("| %s%s | %.4f | %.4f | %.4f | %s |"
                 % (k, mark, v["spearman"], v["top30_overlap"],
                    v["top39_overlap"], v["level_dist"]))
    cv = r["cohort_volatility"]
    L += ["", "## 4. §8.6 비교군별 순위 흔들림", "",
          "%s. 이 표는 **§13 2순위(비교군 품질 개선)의 입력**입니다 — 흔들리는"
          % cv["설명"],
          "비교군을 먼저 손봐야 합니다.", "",
          "전체 평균 **%.2f점** / 중앙값 **%.2f점**"
          % (cv["overall_mean"], cv["overall_median"]), "",
          "비교군 단계별 평균: %s"
          % ", ".join("`%s` %.2f점" % (k, v) for k, v in cv["by_level"].items()), "",
          "**가장 많이 흔들리는 비교군**", "",
          "| 비교군 | n | 순위 흔들림 |", "|---|---:|---:|"]
    for x in cv["worst_cohorts"]:
        L.append("| `%s` | %d | %.2f점 |" % (x["cohort"], x["n"], x["volatility"]))
    L += ["", "**가장 안정적인 비교군**", "",
          "| 비교군 | n | 순위 흔들림 |", "|---|---:|---:|"]
    for x in cv["best_cohorts"]:
        L.append("| `%s` | %d | %.2f점 |" % (x["cohort"], x["n"], x["volatility"]))
    L += ["", "## 5. 같이 읽어야 하는 것", "",
          "- 이 수치는 **모델이 맞았는지**를 말하지 않습니다. 같은 입력에",
          "  같은 순위를 주는가만 말합니다. 방향서 §7 대로 정답 대조는",
          "  도메인 전문가 Ground Truth 가 확보되기 전까지 보류합니다.",
          "- 서비스 문구는 방향서 §5 를 따릅니다 — '이상 사업'이 아니라",
          "  '유사사업 대비 드문 설계 조합 / 검토 우선순위'입니다.", ""]
    p = os.path.join(C.REPORTS, "m48_m3_stability.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
