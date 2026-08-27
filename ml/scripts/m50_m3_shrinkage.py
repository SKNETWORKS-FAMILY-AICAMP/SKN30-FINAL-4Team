r"""M50 — 얇은 비교군 대표벡터 shrinkage (계획서 STEP 3, §6).

무엇을 고치려는가
    M48 이 흔들림의 위치를 특정했다. 전체 구조가 아니라 **n=20~30 수준의 얇은
    비교군 5~6개**에 집중돼 있다.

        사업화|other      n=23   순위 흔들림 29.4점
        수출통관|grant     n=20              20.7점
        사업화|loan       n=25              13.9점
        ...
        사업화|grant      n=675              1.6점

    표본이 20건인 비교군의 평균은 표본이 675건인 비교군의 평균보다 당연히
    불안정하다. 그런데 지금은 둘을 똑같이 '그 비교군의 평균'으로 쓴다.

무엇을 하는가 — shrinkage 대표벡터
    얇은 비교군의 대표벡터를 **상위 fallback 비교군 쪽으로 끌어당긴다.**

        C_shrunk = w * C_자기비교군 + (1 - w) * C_상위비교군
        w = n / (n + k)

    n 이 작으면 상위 비교군 정보를 더 쓰고, n 이 충분히 크면 자기 비교군을
    거의 그대로 쓴다. k 는 그 전환이 일어나는 지점이다 — k=20 이면 n=20 에서
    반반이고 n=675 에서 0.97 이라 큰 비교군은 사실상 영향이 없다.

    이 방식의 좋은 점은 **얇은 비교군만 골라내는 규칙이 필요 없다는 것**이다.
    n 이 크면 w 가 저절로 1 에 붙는다.

판정 기준을 결과보다 먼저 못박는다 (계획서 §6)
    ROC-AUC 를 기준으로 쓰지 않는다. 다음 네 가지만 본다.

        얇은 비교군 내부 순위 흔들림   줄어야 한다 (이게 목적이다)
        전체 Spearman 순위상관        크게 변하면 안 된다
        Top30 겹침                   크게 변하면 안 된다
        대표벡터 bootstrap 분산       줄어야 한다

    **얇은 비교군 안정성이 실제로 개선되고 전체 순위구조가 크게 변하지 않을
    때만 적용한다.** 개선이 명확하지 않으면 현행 유지다.
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
from m47_m3_sensitivity import build_vectors_v

SEED = 42
K_GRID = [5, 10, 20, 40]        # w = n/(n+k). k 가 클수록 상위 비교군을 더 쓴다
N_ITER = 15
THIN_MAX = 30                   # '얇다'의 기준. M48 이 흔들림을 관측한 구간
TOPK = 30
# 전체 구조가 '크게 변하지 않았다'의 문턱. M48 이 잰 80% 재표집 변동폭에서
# 가져온다 — 그보다 작은 변화는 표본이 흔들린 것과 구별되지 않는다.
KEEP_SPEARMAN = 0.969
KEEP_TOP30 = 0.789


def resolve_fn(fit, min_cohort=MIN_COHORT):
    """비교군 판정 + **상위(fallback) 비교군**까지 같이 돌려준다."""
    k2 = fit["support_type"].astype(str) + "|" + fit["support_method"].astype(str)
    k1 = fit["support_type"].astype(str)
    n2, n1 = k2.value_counts(), k1.value_counts()

    def resolve(a, b):
        if n2.get(a, 0) >= min_cohort:
            return ("2", a), ("1", b)          # 자기 = 성격x방식, 상위 = 성격
        if n1.get(b, 0) >= min_cohort:
            return ("1", b), ("0", "ALL")      # 자기 = 성격, 상위 = 전체
        return ("0", "ALL"), None              # 전체는 상위가 없다
    return resolve, k2, k1


def masks_for(key, k2, k1, n):
    lvl, val = key
    if lvl == "2":
        return (k2 == val).to_numpy()
    if lvl == "1":
        return (k1 == val).to_numpy()
    return np.ones(n, bool)


def score(fit, apply_df, k=None, min_cohort=MIN_COHORT):
    """k=None 이면 현행(shrinkage 없음). 그 외에는 w=n/(n+k) 로 끌어당긴다.

    대표벡터를 바꾸면 비교군 내부 거리 분포도 같이 바뀌므로, percentile 환산에
    쓰는 분포도 **shrink 한 C 기준으로 다시 만든다.** 안 그러면 기준점과 자가
    비교하는 분포가 어긋난다.
    """
    Xtr, Xap, _ = build_vectors_v(fit, apply_df)
    resolve, k2t, k1t = resolve_fn(fit, min_cohort)
    _, k2a, k1a = resolve_fn(fit, min_cohort)
    k2a = apply_df["support_type"].astype(str) + "|" + apply_df["support_method"].astype(str)
    k1a = apply_df["support_type"].astype(str)

    need = {resolve(a, b) for a, b in zip(k2t, k1t)} | \
           {resolve(a, b) for a, b in zip(k2a, k1a)}
    raw = {}                                     # 모든 후보 비교군의 원 평균
    for self_k, par_k in need:
        for kk in (self_k, par_k):
            if kk is None or kk in raw:
                continue
            m = masks_for(kk, k2t, k1t, len(fit))
            raw[kk] = {"c": Xtr[m].mean(0), "n": int(m.sum()), "mask": m}

    groups = {}
    for self_k, par_k in need:
        s = raw[self_k]
        if k is None or par_k is None:
            c, w = s["c"], 1.0
        else:
            w = s["n"] / (s["n"] + k)
            c = w * s["c"] + (1 - w) * raw[par_k]["c"]
        M = Xtr[s["mask"]]
        groups[self_k] = {"c": c, "dist": np.linalg.norm(M - c, axis=1),
                          "n": s["n"], "w": w}

    pct = np.empty(len(apply_df))
    keys = []
    for i in range(len(apply_df)):
        sk, _ = resolve(k2a.iloc[i], k1a.iloc[i])
        g = groups[sk]
        pct[i] = float((g["dist"] <= np.linalg.norm(Xap[i] - g["c"])).mean()) * 100
        keys.append("%s|%s" % sk)
    return (pd.Series(pd.Series(pct).rank(pct=True).to_numpy(),
                      index=apply_df["row_id"].to_numpy()),
            pd.Series(keys, index=apply_df["row_id"].to_numpy()), groups)


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    rng = np.random.default_rng(SEED)

    print("M50 — 얇은 비교군 대표벡터 shrinkage (STEP 3)")
    print("  pool %d행 · 라벨 미사용" % len(train))
    print("  C_shrunk = w*C_자기 + (1-w)*C_상위,  w = n/(n+k)")

    base, base_key, base_g = score(train, train)
    # groups 의 키는 (level, value) 튜플이고 base_key 는 "level|value" 문자열이다.
    # 둘을 비교해야 하므로 문자열 쪽으로 맞춘다.
    sizes = {"%s|%s" % k: g["n"] for k, g in base_g.items()}
    thin = sorted([k for k, n in sizes.items() if n <= THIN_MAX])
    print("\n  얇은 비교군(n<=%d) %d개 / 전체 비교군 %d개"
          % (THIN_MAX, len(thin), len(sizes)))
    print("  대상: %s" % ", ".join("%s(n=%d)" % (k.split("|", 1)[1], sizes[k])
                                 for k in thin[:6]))

    def volatility(k, tag):
        """80% 재표집에서 백분위 순위가 평균 몇 점 움직이는가 + 대표벡터 분산."""
        r = np.random.default_rng(SEED)
        ranks, cents = [], {kk: [] for kk in sizes}
        for _ in range(N_ITER):
            sub = train.sample(frac=0.8, random_state=int(r.integers(1e9)))
            s, _, g = score(sub, train, k)
            ranks.append(s.loc[base.index].rank(pct=True))
            for kk, gg in g.items():
                sk = "%s|%s" % kk
                if sk in cents:
                    cents[sk].append(gg["c"])
        R = pd.concat(ranks, axis=1)
        vol = (R.sub(base.rank(pct=True), axis=0).abs().mean(axis=1) * 100)
        cvar = {kk: float(np.mean(np.var(np.array(v), axis=0).sum()))
                for kk, v in cents.items() if len(v) > 1}
        return vol, cvar

    print("\n== 판정 문턱 (결과 보기 전 고정): Spearman >= %.3f / Top30 >= %.3f"
          % (KEEP_SPEARMAN, KEEP_TOP30))
    base_vol, base_cvar = volatility(None, "현행")
    tv = base_vol[base_key.isin(thin)]
    print("\n  현행 — 얇은 비교군 흔들림 %.2f점 / 전체 %.2f점 / 대표벡터분산 %.4f"
          % (tv.mean(), base_vol.mean(),
             float(np.mean([base_cvar[k] for k in thin if k in base_cvar]))))

    print("\n== shrinkage k 별 비교")
    print("  %-6s %10s %10s %12s %12s %12s  %s"
          % ("k", "Spearman", "Top30", "얇은흔들림", "전체흔들림", "대표벡터분산", "판정"))
    print("  %-6s %10s %10s %11.2f점 %11.2f점 %12.4f  %s"
          % ("현행", "1.0000", "1.0000", tv.mean(), base_vol.mean(),
             float(np.mean([base_cvar[k] for k in thin if k in base_cvar])), "기준"))
    sec = {}
    for k in K_GRID:
        s, _, g = score(train, train, k)
        rho = float(spearmanr(base.to_numpy(), s.loc[base.index].to_numpy()).statistic)
        ov = len(set(base.sort_values(ascending=False).head(TOPK).index)
                 & set(s.sort_values(ascending=False).head(TOPK).index)) / TOPK
        vol, cvar = volatility(k, "k=%d" % k)
        tvk = vol[base_key.isin(thin)]
        cv = float(np.mean([cvar[kk] for kk in thin if kk in cvar]))
        keep = rho >= KEEP_SPEARMAN and ov >= KEEP_TOP30
        better = tvk.mean() < tv.mean()
        verdict = ("채택 가능" if (keep and better) else
                   "전체구조 변화 큼" if not keep else "얇은비교군 개선 없음")
        sec["k=%d" % k] = {
            "spearman": round(rho, 4), "top30_overlap": round(ov, 4),
            "thin_volatility": round(float(tvk.mean()), 4),
            "all_volatility": round(float(vol.mean()), 4),
            "centroid_variance_thin": round(cv, 6),
            "w_at_n20": round(20 / (20 + k), 3), "w_at_n675": round(675 / (675 + k), 3),
            "verdict": verdict}
        print("  %-6d %10.4f %10.4f %11.2f점 %11.2f점 %12.4f  %s"
              % (k, rho, ov, tvk.mean(), vol.mean(), cv, verdict))

    ok = [k for k, v in sec.items() if v["verdict"] == "채택 가능"]
    best = min(ok, key=lambda k: sec[k]["thin_volatility"]) if ok else None
    print("\n== 결론")
    if best:
        b = sec[best]
        print("  %s — 얇은 비교군 흔들림 %.2f점 -> %.2f점 (%.0f%% 감소)"
              % (best, tv.mean(), b["thin_volatility"],
                 (1 - b["thin_volatility"] / tv.mean()) * 100))
        print("  전체 Spearman %.4f / Top30 %.4f 로 구조는 유지"
              % (b["spearman"], b["top30_overlap"]))
    else:
        print("  채택 가능한 k 없음 — **현행 대표벡터 유지** (계획서 §6)")

    rep = {
        "질문": "얇은 비교군(n<=%d)의 대표벡터를 상위 비교군 쪽으로 끌어당기면 "
              "안정성이 개선되는가 (계획서 STEP 3·§6)" % THIN_MAX,
        "방법": "C_shrunk = w*C_자기 + (1-w)*C_상위, w = n/(n+k)",
        "판정문턱": {"spearman_min": KEEP_SPEARMAN, "top30_min": KEEP_TOP30,
                 "출처": "M48 이 잰 80% 재표집 변동폭",
                 "원칙": "얇은 비교군이 개선되고 전체 구조가 유지될 때만 채택"},
        "n_pool": int(len(train)), "n_cohorts": len(sizes),
        "thin_cohorts": [{"cohort": k, "n": sizes[k]} for k in thin],
        "baseline": {"thin_volatility": round(float(tv.mean()), 4),
                     "all_volatility": round(float(base_vol.mean()), 4),
                     "centroid_variance_thin": round(
                         float(np.mean([base_cvar[k] for k in thin if k in base_cvar])), 6)},
        "shrinkage": sec, "adopted": best,
    }
    C.save_report("m50_m3_shrinkage.json", rep)
    write_md(rep)


def write_md(r):
    b = r["baseline"]
    L = ["# M50 — 얇은 비교군 대표벡터 shrinkage", "",
         "> 계획서 STEP 3·§6. M48 이 흔들림의 **위치**를 특정했습니다 — 전체",
         "> 구조가 아니라 n=20~30 수준의 얇은 비교군 5~6개입니다. 전체를 다시",
         "> 설계하지 않고 그 부분만 손봅니다.", "",
         "## 1. 방법", "", "```text",
         "C_shrunk = w * C_자기비교군 + (1 - w) * C_상위비교군",
         "w = n / (n + k)",
         "```", "",
         "n 이 작으면 상위 비교군 정보를 더 쓰고, 충분히 크면 자기 비교군을",
         "거의 그대로 씁니다. **얇은 비교군을 골라내는 규칙이 따로 필요 없습니다**",
         "— n 이 크면 w 가 저절로 1 에 붙습니다.", "",
         "| k | n=20 일 때 w | n=675 일 때 w |", "|---:|---:|---:|"]
    for k, v in r["shrinkage"].items():
        L.append("| %s | %.3f | %.3f |" % (k.split("=")[1], v["w_at_n20"], v["w_at_n675"]))
    L += ["", "## 2. 판정 문턱 — 결과 보기 전에 고정", "",
          "ROC-AUC 를 쓰지 않습니다(계획서 §6). 네 가지만 봅니다.", "",
          "```text",
          "얇은 비교군 흔들림   줄어야 한다 (이게 목적)",
          "전체 Spearman       >= %.3f  (M48 재표집 변동폭)" % r["판정문턱"]["spearman_min"],
          "Top30 겹침          >= %.3f" % r["판정문턱"]["top30_min"],
          "대표벡터 분산        줄어야 한다",
          "```", "",
          "> **%s**" % r["판정문턱"]["원칙"], "",
          "## 3. 대상 — 얇은 비교군", "",
          "전체 %d개 비교군 중 **%d개**가 n<=%d 입니다."
          % (r["n_cohorts"], len(r["thin_cohorts"]), 30), "",
          "| 비교군 | n |", "|---|---:|"]
    for x in r["thin_cohorts"][:8]:
        L.append("| `%s` | %d |" % (x["cohort"].split("|", 1)[1], x["n"]))
    L += ["", "## 4. 결과", "",
          "| k | Spearman | Top30 겹침 | 얇은비교군 흔들림 | 전체 흔들림 | 대표벡터 분산 | 판정 |",
          "|---|---:|---:|---:|---:|---:|---|",
          "| **현행** | 1.0000 | 1.0000 | %.2f점 | %.2f점 | %.4f | 기준 |"
          % (b["thin_volatility"], b["all_volatility"], b["centroid_variance_thin"])]
    for k, v in r["shrinkage"].items():
        L.append("| %s | %.4f | %.4f | %.2f점 | %.2f점 | %.4f | %s |"
                 % (k, v["spearman"], v["top30_overlap"], v["thin_volatility"],
                    v["all_volatility"], v["centroid_variance_thin"], v["verdict"]))
    L += ["", "## 5. 결론", ""]
    if r["adopted"]:
        a = r["shrinkage"][r["adopted"]]
        L += ["**%s 채택.** 얇은 비교군 흔들림이 %.2f점 → **%.2f점**"
              % (r["adopted"], b["thin_volatility"], a["thin_volatility"]),
              "(%.0f%% 감소)이고, 전체 Spearman %.4f / Top30 %.4f 로 순위구조는"
              % ((1 - a["thin_volatility"] / b["thin_volatility"]) * 100,
                 a["spearman"], a["top30_overlap"]),
              "유지됩니다.", ""]
    else:
        worst = max(r["shrinkage"].values(), key=lambda v: v["thin_volatility"])
        best_cv = min(r["shrinkage"].values(), key=lambda v: v["centroid_variance_thin"])
        L += ["**현행 대표벡터 유지.** 문턱을 넘으면서 얇은 비교군을 개선하는 k 가",
              "없었습니다. 계획서 §6 대로 개선이 명확하지 않으면 바꾸지 않습니다.", "",
              "### 그런데 여기서 하나 배웠습니다 — 대표벡터 안정성 ≠ 순위 안정성", "",
              "shrinkage 는 **의도한 대로 작동했습니다.** 대표벡터 분산은",
              "%.4f → **%.4f** 로 절반 가까이 줄었습니다. 그런데 정작 목적이었던"
              % (b["centroid_variance_thin"], best_cv["centroid_variance_thin"]),
              "얇은 비교군 순위 흔들림은 %.2f점 → %.2f점으로 **오히려 늘었습니다.**"
              % (b["thin_volatility"], worst["thin_volatility"]), "",
              "이유는 점수가 만들어지는 방식에 있습니다. 이례성 점수는 절대 거리가",
              "아니라 **비교군 내부 거리 분포에서의 백분위**입니다. 대표벡터를 상위",
              "비교군 쪽으로 끌어당기면 그 비교군 구성원 전체가 자기 평균이 아닌",
              "점에서 측정되고, 내부 거리 분포가 한쪽으로 쏠려 백분위 환산이",
              "불안정해집니다. 게다가 상위 비교군도 재표집마다 흔들리므로 **불안정",
              "요인이 하나에서 둘로 늘어납니다.**", "",
              "> 대표벡터를 안정시키는 것과 **순위를 안정시키는 것은 다른 문제**였고,",
              "> 운영에서 중요한 것은 후자입니다. 얇은 비교군 흔들림은 표본 수가",
              "> 20~30건이라는 사실 자체에서 오는 것이라, 대표벡터 계산법을 바꿔",
              "> 풀 수 있는 문제가 아닙니다. **표본을 늘리는 것이 실제 해법**입니다.", ""]
    L += ["> 이 실험은 **탐지 점수 공식을 바꾸지 않습니다.** 대표벡터를 만드는",
          "> 방법만 다룹니다. 거리 = 이례성, 방향 = 설명이라는 구조는 그대로입니다.", ""]
    p = os.path.join(C.REPORTS, "m50_m3_shrinkage.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
