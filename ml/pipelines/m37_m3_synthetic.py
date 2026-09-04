r"""M37 — 합성 이상치를 다시 만들고, 뭉치는지 검사한다 (계획서 §3, Step 8).

왜 Step 8 을 Step 7 보다 먼저 하는가
    Step 7(Vector Direction)은 거리와 방향을 섞는 가중치를 정해야 한다.
    계획서 §6 은 "단, human hold-out 으로 weight 를 선택하면 안 된다"고
    못박았다. 그러면 고를 근거가 될 validation 이 먼저 있어야 한다.
    그래서 합성 이상치를 여기서 먼저 만든다. **합성은 최종 성능 증명이 아니라
    가중치를 고르고 robustness 를 보는 자리다** (계획서 §13).

기존 생성기의 문제 (m13.inject_synthetic)
    네 가지 정형 패턴만 만든다 — 극소수·극고액 / 극다수·극소액 / 30년 /
    지원비율 100%. 값도 `train 최댓값 + 1.0` 처럼 분포 밖으로 고정 이동시킨다.
    그 결과 합성 이상치가 몇 덩어리로 뭉치고, 모델은 '실제 이상 구조'가 아니라
    **생성 규칙 자체**를 외울 수 있다.

새 생성기 (계획서 §3.2)
    정상 사업 한 건을 뽑아 1~3개 축을 무작위로 골라 곱하거나 더한다.
    증가·감소 양방향을 모두 넣고, 배수도 여러 값에서 뽑는다. 핵심은
    **모든 합성 데이터가 같은 방향으로 움직이지 않게** 하는 것이다.

생성 후 반드시 확인 (계획서 §3.3)
    · 합성-합성 거리 vs 정상-정상 거리
    · kNN 순도 — 합성 점의 이웃이 또 합성인 비율. 높으면 뭉친 것이다
    · 생성 규칙별 실루엣 — 규칙끼리 군집을 이루면 규칙을 외울 수 있다
    · PCA 산점도
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
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score
from sklearn.neighbors import NearestNeighbors

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, inject_synthetic, prepare
from m36_m3_oneclass import encode, fit_all

SEED = 42
N_SYN = 120

# 계획서 §3.2 의 배수표. 증가·감소를 모두 넣어 한 방향으로 몰리지 않게 한다.
MULT = {
    "per_recipient": [0.1, 0.2, 0.33, 0.5, 2, 3, 5, 10],
    "support_count": [0.1, 0.2, 0.33, 0.5, 2, 3, 5],
    "project_duration": [0.25, 0.33, 0.5, 2, 3, 4],
}
ADD = {"support_ratio": [-40, -30, -20, 20, 30, 40]}
CAT_SWAP = {"support_method": ["grant", "loan", "guarantee", "voucher", "service"],
            "amount_type": ["per_company", "per_project", "total_budget", "periodic"]}
AXES = list(MULT) + list(ADD) + list(CAT_SWAP)


def make_fake(row, rng):
    """정상 사업 한 건에서 1~3개 축을 무작위로 골라 흔든다.

    값의 자릿수를 넘기지 않는 것이 중요하다. 기존 생성기처럼 `최댓값+1`
    (로그축이므로 10배)로 밀면 합성 이상치가 전부 분포 바깥 한 점에
    모여 '분포 밖인가'를 묻는 문제가 되어 버린다.
    """
    fake = row.copy()
    k = rng.choice([1, 2, 3], p=[0.45, 0.35, 0.20])
    picked = list(rng.choice(AXES, size=k, replace=False))
    applied = []
    for f in picked:
        if f in MULT:
            if pd.isna(row.get(f)):
                continue
            m = rng.choice(MULT[f])
            fake[f] = float(row[f]) * m
            applied.append("%s x%.2g" % (f, m))
        elif f in ADD:
            base = row.get(f)
            base = 60.0 if pd.isna(base) else float(base)
            a = rng.choice(ADD[f])
            fake[f] = float(np.clip(base + a, 0, 100))
            applied.append("%s %+d" % (f, a))
        else:
            opts = [c for c in CAT_SWAP[f] if c != row.get(f)]
            fake[f] = rng.choice(opts)
            applied.append("%s->%s" % (f, fake[f]))
    if not applied:
        return None
    # 파생축을 원본과 같은 규칙으로 다시 만든다. 로그축만 갱신하고 원본 축을
    # 두면 모델이 '두 축이 서로 안 맞는 행'을 탐지하게 되어 다른 문제가 된다.
    for src, dst in (("per_recipient", "log_per_recipient"),
                     ("support_count", "log_support_count")):
        v = fake.get(src)
        fake[dst] = np.log10(v) if (pd.notna(v) and v > 0) else np.nan
    fake["__synthetic"] = True
    fake["__rule"] = " + ".join(sorted(applied))
    fake["__n_changed"] = len(applied)
    return fake


def build(train, n=N_SYN, seed=SEED):
    rng = np.random.default_rng(seed)
    pool = train[train["n_axes"] >= 3]
    pool = pool if len(pool) >= n else train
    base = pool.sample(n, random_state=seed, replace=len(pool) < n)
    rows = []
    for _, r in base.iterrows():
        f = make_fake(r, rng)
        if f is not None:
            rows.append(f)
    return pd.DataFrame(rows).reset_index(drop=True)


# ------------------------------------------------------------ 뭉침 검사
def clumping(X_real, X_syn, rules, k=10):
    """합성 이상치가 몇 덩어리로 몰렸는지 세 가지로 잰다."""
    X = np.vstack([X_real, X_syn])
    is_syn = np.r_[np.zeros(len(X_real), bool), np.ones(len(X_syn), bool)]

    nn = NearestNeighbors(n_neighbors=k + 1).fit(X)
    _, idx = nn.kneighbors(X[is_syn])
    purity = float(np.mean([is_syn[row[1:]].mean() for row in idx]))

    def pdist_sample(A, m=400, rng=np.random.default_rng(SEED)):
        i = rng.choice(len(A), min(m, len(A)), replace=False)
        D = np.linalg.norm(A[i][:, None, :] - A[i][None, :, :], axis=-1)
        return D[np.triu_indices(len(i), 1)]

    d_rr = pdist_sample(X_real)
    d_ss = pdist_sample(X_syn)
    lab = pd.factorize(rules)[0]
    keep = pd.Series(lab).map(pd.Series(lab).value_counts()) >= 2
    sil = None
    if keep.sum() > 2 and len(set(lab[keep.to_numpy()])) > 1:
        sil = round(float(silhouette_score(X_syn[keep.to_numpy()], lab[keep.to_numpy()])), 4)
    return {
        "knn_purity": round(purity, 4),
        "knn_purity_note": "합성 점의 이웃 %d개 중 합성 비율. 무작위면 %.2f 근처, "
                           "1.0 이면 완전히 뭉친 것" % (k, len(X_syn) / len(X)),
        "baseline_purity": round(len(X_syn) / len(X), 4),
        "dist_real_real_median": round(float(np.median(d_rr)), 4),
        "dist_syn_syn_median": round(float(np.median(d_ss)), 4),
        "syn_over_real_spread": round(float(np.median(d_ss) / max(np.median(d_rr), 1e-9)), 4),
        "rule_silhouette": sil,
        "rule_silhouette_note": "생성 규칙별 실루엣. 0.5 이상이면 규칙끼리 군집을 "
                                "이뤄 모델이 규칙을 외울 수 있다",
        "n_unique_rules": int(len(set(rules))),
    }


def detect(train, syn):
    """같은 인코딩으로 정상+합성을 넣고 상위 k 회수율을 잰다."""
    mixed = pd.concat([train.assign(__synthetic=False), syn], ignore_index=True)
    Xtr, Xap, _ = encode(train, mixed)
    is_syn = mixed["__synthetic"].fillna(False).to_numpy(bool)
    k = int(is_syn.sum())
    out = {}
    for name, s in fit_all(Xtr, Xap).items():
        order = np.argsort(-s)
        out[name] = {
            "recall_at_k": round(float(is_syn[order[:k]].mean()), 4),
            "recall_at_2k": round(float(is_syn[order[:2 * k]].mean()), 4),
            "median_rank_pct": round(float(np.median(
                np.argsort(np.argsort(-s))[is_syn] / len(s))) * 100, 2),
        }
    return out


def plot(X_real, X_syn_old, X_syn_new):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    p = PCA(n_components=2, random_state=SEED).fit(X_real)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharex=True, sharey=True)
    for ax, Xs, title in ((axes[0], X_syn_old, "old (m13) — 4 fixed patterns"),
                          (axes[1], X_syn_new, "new (M37) — 1~3 axes, both directions")):
        r, s = p.transform(X_real), p.transform(Xs)
        ax.scatter(r[:, 0], r[:, 1], s=6, c="#c9ccd1", label="real", linewidths=0)
        ax.scatter(s[:, 0], s[:, 1], s=14, c="#c0392b", label="synthetic", linewidths=0)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("PC1")
    axes[0].set_ylabel("PC2")
    axes[0].legend(fontsize=8, loc="best")
    fig.suptitle("Synthetic anomaly placement (PCA of the design feature space)",
                 fontsize=11)
    fig.tight_layout()
    out = os.path.join(C.FIGURES, "m37_synthetic_pca.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)

    old = inject_synthetic(train, N_SYN)
    old["__rule"] = ["kind_%d" % k for k in old["__kind"]]
    new = build(train, N_SYN)

    print("M37 — 합성 이상치 재설계")
    print("  정상 %d행 / 합성 old %d · new %d" % (len(train), len(old), len(new)))
    print("  new 생성규칙 종류 %d (old %d)" % (len(set(new["__rule"])), len(set(old["__rule"]))))
    print("  new 변경축 개수 분포: %s" % dict(new["__n_changed"].value_counts().sort_index()))

    _, Xall_old, _ = encode(train, pd.concat([train, old], ignore_index=True))
    _, Xall_new, _ = encode(train, pd.concat([train, new], ignore_index=True))
    n = len(train)
    X_real, X_old, X_new = Xall_old[:n], Xall_old[n:], Xall_new[n:]

    cl_old = clumping(X_real, X_old, old["__rule"].to_numpy())
    cl_new = clumping(X_real, X_new, new["__rule"].to_numpy())
    print("\n== 뭉침 검사 (계획서 §3.3)")
    print("  %-28s %10s %10s" % ("", "old", "new"))
    for k in ("knn_purity", "syn_over_real_spread", "rule_silhouette", "n_unique_rules"):
        print("  %-28s %10s %10s" % (k, cl_old[k], cl_new[k]))
    print("  (무작위 배치라면 knn_purity 는 %.3f 근처)" % cl_new["baseline_purity"])

    d_old, d_new = detect(train, old), detect(train, new)
    print("\n== 같은 모델이 두 합성셋을 얼마나 잡는가 (상위 k 회수율)")
    print("  %-22s %10s %10s" % ("", "old", "new"))
    for k in d_old:
        print("  %-22s %10.3f %10.3f" % (k, d_old[k]["recall_at_k"], d_new[k]["recall_at_k"]))

    fig = plot(X_real, X_old, X_new)
    print("\n[figure] %s" % fig)

    out = os.path.join(C.PROC, "m37_synthetic.parquet")
    keep = [c for c in new.columns if not c.startswith("__")] + \
           ["__synthetic", "__rule", "__n_changed"]
    new[keep].to_parquet(out, index=False)
    print("[data] %s" % out)

    rep = {
        "목적": "합성은 최종 성능 증명이 아니라 가중치 선택과 robustness 검사용이다 (계획서 §13)",
        "n_train": int(len(train)), "n_synthetic": int(len(new)),
        "generator_old": "m13.inject_synthetic — 4개 고정 패턴, 분포 밖으로 고정 이동",
        "generator_new": "1~3개 축 무작위 선택, 증가·감소 양방향, 배수표에서 추첨",
        "n_rules": {"old": int(len(set(old["__rule"]))), "new": int(len(set(new["__rule"])))},
        "n_changed_dist": {str(k): int(v) for k, v in
                           new["__n_changed"].value_counts().sort_index().items()},
        "clumping": {"old": cl_old, "new": cl_new},
        "detection": {"old": d_old, "new": d_new},
        "figure": fig, "data": out,
    }
    C.save_report("m37_m3_synthetic.json", rep)
    write_md(rep)


def write_md(r):
    co, cn = r["clumping"]["old"], r["clumping"]["new"]
    L = ["# M37 — 합성 이상치 재설계와 뭉침 검사", "",
         "> 계획서 §3. 합성 이상치가 몇 개 덩어리로 몰리면 모델은 실제 이상 구조가",
         "> 아니라 **생성 규칙 자체**를 외웁니다. 규칙을 흩뜨리고, 실제로 흩어졌는지",
         "> 검사합니다.", "",
         "> **Step 8 을 Step 7 보다 먼저 했습니다.** Vector Direction 은 거리와 방향을",
         "> 섞는 가중치를 정해야 하는데, 계획서 §6 이 \"human hold-out 으로 weight 를",
         "> 선택하면 안 된다\"고 못박았습니다. 고를 근거가 될 validation 이 먼저",
         "> 있어야 합니다.", "",
         "## 1. 두 생성기", "",
         "| | old (m13) | new (M37) |", "|---|---|---|",
         "| 방식 | 4개 고정 패턴 | 1~3개 축 무작위 선택 |",
         "| 이동 | `train 최댓값 + 1.0` 로 분포 밖 고정 | 배수표에서 추첨, 자릿수 유지 |",
         "| 방향 | 사실상 한 방향 | 증가·감소 양방향 |",
         "| 규칙 종류 | %d | **%d** |" % (r["n_rules"]["old"], r["n_rules"]["new"]), "",
         "변경 축 개수 분포 (new): `%s`" % r["n_changed_dist"], "",
         "## 2. 뭉침 검사 (계획서 §3.3)", "",
         "| 지표 | old | new | 읽는 법 |", "|---|---:|---:|---|",
         "| kNN 순도 | %.3f | %.3f | 합성 점의 이웃 10개 중 합성 비율. 무작위 배치면 %.3f 근처, 1.0 이면 완전히 뭉친 것 |"
         % (co["knn_purity"], cn["knn_purity"], cn["baseline_purity"]),
         "| 합성 퍼짐 / 정상 퍼짐 | %.3f | %.3f | 1 보다 크게 작으면 합성끼리 좁게 모여 있다 |"
         % (co["syn_over_real_spread"], cn["syn_over_real_spread"]),
         "| 생성규칙 실루엣 | %s | %s | 0.5 이상이면 규칙끼리 군집을 이뤄 규칙을 외울 수 있다 |"
         % (co["rule_silhouette"], cn["rule_silhouette"]), "",
         "![합성 이상치 배치](../figures/%s)" % os.path.basename(r["figure"]), "",
         "## 3. 같은 모델이 두 합성셋을 얼마나 잡는가", "",
         "합성셋을 바꾸면 회수율이 바뀝니다. 그 차이가 곧 **기존 수치가 생성 규칙에",
         "얼마나 기대고 있었는가** 입니다.", "",
         "| 모델 | old 회수율 | new 회수율 | 차이 |", "|---|---:|---:|---:|"]
    for k in r["detection"]["old"]:
        o = r["detection"]["old"][k]["recall_at_k"]
        n = r["detection"]["new"][k]["recall_at_k"]
        L.append("| %s | %.3f | %.3f | %+.3f |" % (k, o, n, n - o))
    L += ["", "## 4. 이 합성셋으로 하는 것과 하지 않는 것", "",
          "```text",
          "한다     Vector Direction 의 거리·방향 가중치 선택 (M38)",
          "         모델 robustness 검사",
          "안 한다  최종 성능 보고 — 그건 clean human hold-out 몫이다 (계획서 §13)",
          "```", ""]
    p = os.path.join(C.REPORTS, "m37_m3_synthetic.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
