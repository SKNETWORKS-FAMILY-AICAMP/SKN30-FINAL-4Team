"""S07A — 모델 2: 사업 설계유형 군집.

묻는 것은 "이 사업이 어떤 지원성격인가"(모델 1)가 아니라
"이 사업이 어떤 방식으로 설계됐는가"다.
    소수기업·고액·장기지원형 / 다수기업·소액·단기지원형 / 융자·고한도 금융지원형 ...

설계서가 못박은 실패 조건이 하나 있다.
    > 모델 1의 19클래스를 다시 복제하는 군집이면 실패

그래서 이 스크립트는 군집을 만드는 것보다 **그 실패 조건에 걸리는지 재는 것**을
먼저 한다. 군집 라벨과 지원성격의 ARI/AMI 를 두 조건에서 각각 잰다.
    A. 지원성격을 feature 에 넣고 군집  — 지배 여부 확인용
    B. 지원성격을 빼고 설계 수치·구조만으로 군집 — 실제 채택 후보

거리
    feature 가 범주형·수치형 혼합이고 결측이 많다. one-hot + 유클리드는
    결측을 0으로 밀어 넣어 '미기재'가 하나의 군집이 되게 만든다.
    Gower 거리는 결측 feature 를 분모에서 빼므로 결측을 억지로 채우지 않는다
    (전처리 원칙 6: 결측값을 억지로 채우지 않음).

모델 우선순위는 설계서를 따른다: HDBSCAN -> GMM -> K-Means.
"""
import os
import sys

import numpy as np
import pandas as pd
from sklearn.cluster import HDBSCAN, KMeans
from sklearn.metrics import (adjusted_mutual_info_score, adjusted_rand_score,
                             davies_bouldin_score, silhouette_score)
from sklearn.mixture import GaussianMixture

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SRC = os.path.join(C.PROC, "design_features.parquet")
OUT = os.path.join(C.PROC, "design_clusters.parquet")
SEED = 42

CAT_FEATS = ["support_method", "support_unit", "category_large", "industry_grp",
             "agency_type", "amount_type"]
NUM_FEATS = ["log_per_recipient", "log_support_count", "support_ratio", "project_duration"]

# 설계서의 군집 크기 가이드
MIN_INDEPENDENT = 30
MIN_REFERENCE = 15
# 지원성격 복제 판정선. ARI 0.5 를 넘으면 모델 1 을 다시 만든 것으로 본다.
ARI_FAIL = 0.50


def prepare(df):
    t = df[df["cohort"] == "taxonomy"].copy()
    t = t[t["support_type"].notna()].reset_index(drop=True)

    # 업종 94종은 그대로 쓰면 한 건짜리 칸이 거리를 지배한다. 상위 15종 + 기타.
    top = t["industry"].value_counts().head(15).index
    t["industry_grp"] = t["industry"].where(t["industry"].isin(top), "기타업종")

    # 파싱 오류로 표시된 금액은 값으로 쓰지 않는다(행은 남긴다).
    amt = t["per_recipient"].where(~t["amount_outlier"])
    t["log_per_recipient"] = np.log10(amt.where(amt > 0))
    cnt = t["support_count"]
    t["log_support_count"] = np.log10(cnt.where(cnt > 0))
    return t


def gower(df, cat_feats, num_feats, weights=None):
    """혼합형 Gower 거리. 결측 feature 는 분모에서 제외한다.

    d(i,j) = sum_f w_f * d_f(i,j) / sum_f w_f   (양쪽 다 값이 있는 f 만)
    양쪽 모두 값이 없는 쌍은 비교 근거가 없으므로 거리 1(최대 이질)로 둔다.
    """
    n = len(df)
    w = weights or {}
    num = np.zeros((n, n))
    den = np.zeros((n, n))

    for f in num_feats:
        v = df[f].to_numpy(dtype=float)
        ok = ~np.isnan(v)
        rng = np.nanmax(v) - np.nanmin(v) if ok.sum() > 1 else 0.0
        if rng <= 0:
            continue
        d = np.abs(v[:, None] - v[None, :]) / rng
        m = ok[:, None] & ok[None, :]
        wf = w.get(f, 1.0)
        num += np.where(m, d * wf, 0.0)
        den += np.where(m, wf, 0.0)

    for f in cat_feats:
        v = df[f].astype("object").to_numpy()
        ok = pd.notna(df[f]).to_numpy()
        d = (v[:, None] != v[None, :]).astype(float)
        m = ok[:, None] & ok[None, :]
        wf = w.get(f, 1.0)
        num += np.where(m, d * wf, 0.0)
        den += np.where(m, wf, 0.0)

    out = np.where(den > 0, num / np.where(den > 0, den, 1), 1.0)
    np.fill_diagonal(out, 0.0)
    return out


def pcoa(D, k=8):
    """거리행렬 -> 좌표. Davies-Bouldin 과 GMM/K-Means 가 좌표를 요구한다."""
    n = D.shape[0]
    J = np.eye(n) - np.ones((n, n)) / n
    B = -0.5 * J @ (D ** 2) @ J
    vals, vecs = np.linalg.eigh(B)
    idx = np.argsort(vals)[::-1][:k]
    vals, vecs = vals[idx], vecs[:, idx]
    vals = np.clip(vals, 0, None)
    return vecs * np.sqrt(vals)


def score(D, X, labels, y_type):
    """군집 품질 + 지원성격 복제 여부를 한 번에 잰다."""
    lab = np.asarray(labels)
    mask = lab >= 0
    k = len(set(lab[mask]))
    sizes = pd.Series(lab[mask]).value_counts()
    out = {
        "n_clusters": int(k),
        "noise_ratio": round(float((~mask).mean()), 4),
        "n_ge30": int((sizes >= MIN_INDEPENDENT).sum()),
        "n_15_29": int(((sizes >= MIN_REFERENCE) & (sizes < MIN_INDEPENDENT)).sum()),
        "largest_share": round(float(sizes.max() / mask.sum()), 4) if k else None,
        "silhouette": None, "davies_bouldin": None,
        "ari_support_type": None, "ami_support_type": None,
    }
    if k >= 2 and mask.sum() > k:
        out["silhouette"] = round(float(silhouette_score(
            D[np.ix_(mask, mask)], lab[mask], metric="precomputed")), 4)
        out["davies_bouldin"] = round(float(davies_bouldin_score(X[mask], lab[mask])), 4)
        # 노이즈를 뺀 부분집합에서 지원성격과 비교한다
        out["ari_support_type"] = round(float(adjusted_rand_score(y_type[mask], lab[mask])), 4)
        out["ami_support_type"] = round(float(adjusted_mutual_info_score(
            y_type[mask], lab[mask])), 4)
    return out


def hdbscan_grid(D, X, y_type):
    rows = []
    for mcs in (15, 20, 30, 40, 60):
        for ms in (None, 5, 10):
            # copy=True 필수 — precomputed 행렬을 제자리에서 고쳐 대각선이 오염된다
            m = HDBSCAN(min_cluster_size=mcs, min_samples=ms, metric="precomputed",
                        cluster_selection_method="eom", copy=True)
            lab = m.fit_predict(D)
            r = {"min_cluster_size": mcs, "min_samples": ms}
            r.update(score(D, X, lab, y_type))
            rows.append(r)
    return pd.DataFrame(rows)


def bootstrap_stability(df, cat, num, y_type, params, n_iter=20, frac=0.8):
    """80% 재표집으로 다시 군집했을 때 원래 배정이 얼마나 남는가.

    표본이 바뀌면 군집이 통째로 달라지는 결과는 설계유형이라 부를 수 없다.
    """
    rng = np.random.default_rng(SEED)
    D_full = gower(df, cat, num)
    base = HDBSCAN(metric="precomputed", copy=True, **params).fit_predict(D_full)
    aris = []
    for _ in range(n_iter):
        idx = np.sort(rng.choice(len(df), int(len(df) * frac), replace=False))
        sub = df.iloc[idx].reset_index(drop=True)
        lab = HDBSCAN(metric="precomputed", copy=True, **params).fit_predict(gower(sub, cat, num))
        b = base[idx]
        m = (lab >= 0) & (b >= 0)
        if m.sum() > 10:
            aris.append(adjusted_rand_score(b[m], lab[m]))
    return {"n_iter": len(aris),
            "ari_mean": round(float(np.mean(aris)), 4) if aris else None,
            "ari_std": round(float(np.std(aris)), 4) if aris else None,
            "ari_min": round(float(np.min(aris)), 4) if aris else None}


def name_clusters(prof):
    """군집 프로파일을 읽어 설계유형 태그를 붙인다.

    설계서: "태그는 사전에 고정하지 않고 군집화 결과를 해석한 뒤 부여".
    그래서 태그 목록을 코드에 박지 않고, 이번 군집의 분포에서 경계를 잡아 만든다.
    경계가 데이터에서 나오므로 재학습하면 태그도 따라 바뀐다.
    """
    amt = np.array([p["per_recipient_median_won"] for p in prof.values()
                    if p.get("per_recipient_median_won")], dtype=float)
    cnt = np.array([p["support_count_median"] for p in prof.values()
                    if p.get("support_count_median")], dtype=float)
    a_lo, a_hi = (np.quantile(amt, [0.33, 0.67]) if len(amt) >= 3 else (0, np.inf))
    c_lo, c_hi = (np.quantile(cnt, [0.33, 0.67]) if len(cnt) >= 3 else (0, np.inf))

    tags = {}
    for c, p in prof.items():
        method = (p.get("support_method") or {}).get("top")
        share = (p.get("support_method") or {}).get("share", 0)
        a, n = p.get("per_recipient_median_won"), p.get("support_count_median")
        # 사업기간은 커버리지가 27% 뿐이다. 군집의 절반 미만만 기재된 상태에서
        # 중앙값을 보고 '장기'를 붙이면 기재한 소수가 군집 전체를 대표해버린다.
        dp = p.get("project_duration") or {}
        dur = dp.get("median") if dp.get("n", 0) >= 0.5 * p["n"] else None

        parts = []
        if n is not None:
            parts.append("다수기업" if n > c_hi else "소수기업" if n <= c_lo else "중규모")
        if a is not None:
            parts.append("고액" if a > a_hi else "소액" if a <= a_lo else "중액")
        elif method == "service":
            parts.append("금액미기재")
        if dur and dur >= 2:
            parts.append("장기")

        if method in ("loan", "guarantee") and share >= 0.7:
            suffix = "금융지원형"
        elif method == "service" and a is None:
            suffix = "서비스제공형"
        elif method == "voucher":
            suffix = "바우처형"
        else:
            suffix = "직접지원형"
        tags[c] = "·".join(parts) + " " + suffix if parts else suffix
    return tags, {"amount_q33": float(a_lo), "amount_q67": float(a_hi),
                  "count_q33": float(c_lo), "count_q67": float(c_hi)}


def profile(t, labels, num_feats, cat_feats):
    """군집별 feature 프로파일. 사람이 읽고 이름을 붙일 수 있어야 채택한다."""
    t = t.copy()
    t["cluster"] = labels
    out = {}
    for c, g in t[t["cluster"] >= 0].groupby("cluster"):
        p = {"n": int(len(g))}
        for f in num_feats:
            v = g[f].dropna()
            p[f] = {"n": int(len(v)),
                    "median": round(float(v.median()), 3) if len(v) else None}
        for f in cat_feats + ["support_type"]:
            vc = g[f].value_counts()
            p[f] = {"top": str(vc.index[0]), "share": round(float(vc.iloc[0] / len(g)), 3)} \
                if len(vc) else None
        p["per_recipient_median_won"] = (
            float(10 ** g["log_per_recipient"].median())
            if g["log_per_recipient"].notna().any() else None)
        p["support_count_median"] = (
            float(10 ** g["log_support_count"].median())
            if g["log_support_count"].notna().any() else None)
        p["examples"] = g["title"].head(3).tolist()
        out[int(c)] = p
    return out


def main():
    df = pd.read_parquet(SRC)
    t = prepare(df)
    y_type = t["support_type"].to_numpy()
    print("모델 2 대상: taxonomy %d건 (지원성격 라벨 있는 행)" % len(t))

    results = {}
    for cond, cats in (("A_지원성격포함", ["support_type"] + CAT_FEATS),
                       ("B_지원성격제외", CAT_FEATS)):
        print("\n===== %s  (범주형 %d + 수치형 %d)" % (cond, len(cats), len(NUM_FEATS)))
        D = gower(t, cats, NUM_FEATS)
        X = pcoa(D, k=8)

        grid = hdbscan_grid(D, X, y_type)
        cols = ["min_cluster_size", "min_samples", "n_clusters", "noise_ratio",
                "n_ge30", "silhouette", "davies_bouldin", "ari_support_type"]
        print(grid[cols].to_string(index=False))

        # 채택 기준: 노이즈 60% 미만 & >=30 군집 2개 이상 중 실루엣 최대
        ok = grid[(grid["noise_ratio"] < 0.6) & (grid["n_ge30"] >= 2)
                  & grid["silhouette"].notna()]
        pick = (ok.sort_values("silhouette", ascending=False).iloc[0] if len(ok)
                else grid.sort_values("silhouette", ascending=False).iloc[0])
        params = {"min_cluster_size": int(pick["min_cluster_size"]),
                  "min_samples": (None if pd.isna(pick["min_samples"])
                                  else int(pick["min_samples"])),
                  "cluster_selection_method": "eom"}
        labels = HDBSCAN(metric="precomputed", copy=True, **params).fit_predict(D)
        best = score(D, X, labels, y_type)
        print("  선택: %s -> 군집 %d개, 노이즈 %.1f%%, 실루엣 %s, ARI %s"
              % (params, best["n_clusters"], best["noise_ratio"] * 100,
                 best["silhouette"], best["ari_support_type"]))

        # 같은 좌표에서 GMM / K-Means 비교 (설계서 우선순위 2·3위)
        alt = {}
        k = max(best["n_clusters"], 2)
        for name, mdl in (("GMM(k=%d)" % k,
                           GaussianMixture(n_components=k, covariance_type="full",
                                           random_state=SEED)),
                          ("KMeans(k=%d)" % k, KMeans(n_clusters=k, n_init=10,
                                                      random_state=SEED))):
            lb = mdl.fit_predict(X)
            alt[name] = score(D, X, lb, y_type)
            print("  %-14s 실루엣 %s, DBI %s, ARI %s"
                  % (name, alt[name]["silhouette"], alt[name]["davies_bouldin"],
                     alt[name]["ari_support_type"]))

        stab = bootstrap_stability(t, cats, NUM_FEATS, y_type, params)
        print("  bootstrap 안정성: ARI %s ± %s (최저 %s, %d회)"
              % (stab["ari_mean"], stab["ari_std"], stab["ari_min"], stab["n_iter"]))

        results[cond] = {"features_cat": cats, "features_num": NUM_FEATS,
                         "grid": grid.to_dict("records"), "chosen_params": params,
                         "hdbscan": best, "alternatives": alt, "stability": stab}
        if cond == "B_지원성격제외":
            prof = profile(t, labels, NUM_FEATS, CAT_FEATS)
            tags, cuts = name_clusters(prof)
            for c, p in prof.items():
                p["tag"] = tags[c]
            results[cond]["profile"] = prof
            results[cond]["tag_cutoffs"] = cuts
            t["cluster"] = labels
            t["cluster_tag"] = [tags.get(int(l)) if l >= 0 else None for l in labels]
            t["cluster_source"] = "hdbscan_gower_B"

    verdict = judge(results)
    print("\n== 판정: %s" % verdict["verdict"])
    for line in verdict["reasons"]:
        print("   - %s" % line)

    t[["row_id", "title", "support_type", "support_method", "support_unit",
       "amount_type", "per_recipient", "support_count", "support_ratio",
       "project_duration", "industry_grp", "cluster", "cluster_source"]] \
        .to_parquet(OUT, index=False)
    print("[data] %s" % OUT)
    C.save_report("s07a_m2_cluster.json",
                  {"n_rows": int(len(t)), "seed": SEED, "ari_fail_threshold": ARI_FAIL,
                   "results": results, "verdict": verdict})
    write_md(results, verdict, t)


def judge(res):
    """설계서의 채택 조건을 규칙으로 판정한다."""
    b = res["B_지원성격제외"]
    h, st = b["hdbscan"], b["stability"]
    reasons, fail = [], False

    ari = h["ari_support_type"]
    if ari is not None and ari >= ARI_FAIL:
        fail = True
        reasons.append("군집이 지원성격을 복제한다 (ARI %.3f >= %.2f) — 설계서 실패 조건"
                       % (ari, ARI_FAIL))
    else:
        reasons.append("지원성격 복제 아님 (ARI %s) — 설계 구조가 별도 축을 만든다" % ari)

    if h["noise_ratio"] > 0.5:
        fail = True
        reasons.append("노이즈 %.1f%% — 과반이 어느 유형에도 안 들어간다" % (h["noise_ratio"] * 100))
    else:
        reasons.append("노이즈 %.1f%%" % (h["noise_ratio"] * 100))

    if h["n_ge30"] < 2:
        fail = True
        reasons.append("30건 이상 군집이 %d개뿐 — 독립 설계유형으로 부를 것이 없다" % h["n_ge30"])
    else:
        reasons.append("30건 이상 군집 %d개, 15~29건 %d개" % (h["n_ge30"], h["n_15_29"]))

    if st["ari_mean"] is not None and st["ari_mean"] < 0.5:
        reasons.append("bootstrap 안정성 ARI %.3f — 표본이 바뀌면 유형이 흔들린다. "
                       "고정 태그가 아니라 '참고 패턴'으로만 쓸 수 있다" % st["ari_mean"])
        v = "Conditional"
    else:
        reasons.append("bootstrap 안정성 ARI %s" % st["ari_mean"])
        v = "Go"
    if fail:
        v = "No-Go"
    return {"verdict": v, "reasons": reasons}


def won(v):
    if v is None:
        return "—"
    for unit, mult in (("억원", 1e8), ("만원", 1e4)):
        if abs(v) >= mult:
            return "%.1f%s" % (v / mult, unit)
    return "%.0f원" % v


def write_md(res, verdict, t):
    L = ["# 모델 2 — 사업 설계유형 군집", "",
         "> 설계서 실패 조건: **모델 1의 19클래스를 다시 복제하는 군집이면 실패**.",
         "> 그래서 군집 품질보다 지원성격과의 ARI 를 먼저 잰다.", "",
         "## 1. 두 조건 비교", "",
         "| 조건 | 군집 수 | 노이즈 | ≥30 군집 | 실루엣 | DBI | 지원성격 ARI | 안정성 ARI |",
         "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for cond, r in res.items():
        h, s = r["hdbscan"], r["stability"]
        L.append("| %s | %d | %.1f%% | %d | %s | %s | **%s** | %s |"
                 % (cond, h["n_clusters"], h["noise_ratio"] * 100, h["n_ge30"],
                    h["silhouette"], h["davies_bouldin"], h["ari_support_type"],
                    s["ari_mean"]))
    L += ["",
          "지원성격을 feature 에 넣으면(A) ARI 가 올라가는 것이 당연하다. 판단은 B 로 한다.", ""]

    L += ["## 2. 모델 비교 (설계서 우선순위 HDBSCAN > GMM > K-Means)", "",
          "| 조건 | 모델 | 실루엣 | DBI | 지원성격 ARI |", "|---|---|---:|---:|---:|"]
    for cond, r in res.items():
        h = r["hdbscan"]
        L.append("| %s | HDBSCAN | %s | %s | %s |"
                 % (cond, h["silhouette"], h["davies_bouldin"], h["ari_support_type"]))
        for name, a in r["alternatives"].items():
            L.append("| %s | %s | %s | %s | %s |"
                     % (cond, name, a["silhouette"], a["davies_bouldin"],
                        a["ari_support_type"]))

    prof = res["B_지원성격제외"].get("profile", {})
    if prof:
        L += ["", "## 3. 군집 프로파일과 설계유형 태그", "",
              "태그는 미리 정하지 않았다. 이번 군집의 금액·건수 분포에서 3분위 경계를 잡아 붙였다.", "",
              "| 군집 | n | 설계유형 태그 | 기업당지원액 중앙값 | 지원건수 중앙값 | 최다 지원방식 | 최다 지원성격 | 예시 |",
              "|---:|---:|---|---:|---:|---|---|---|"]
        for c, p in sorted(prof.items(), key=lambda kv: -kv[1]["n"]):
            sm = p.get("support_method") or {}
            stt = p.get("support_type") or {}
            L.append("| %d | %d | **%s** | %s | %s | %s (%.0f%%) | %s (%.0f%%) | %s |"
                     % (c, p["n"], p.get("tag", "—"),
                        won(p.get("per_recipient_median_won")),
                        ("%.0f" % p["support_count_median"]
                         if p.get("support_count_median") else "—"),
                        sm.get("top", "—"), (sm.get("share", 0)) * 100,
                        stt.get("top", "—"), (stt.get("share", 0)) * 100,
                        (p["examples"][0][:26] + "…") if p["examples"] else ""))
        L += ["",
              "> 최다 지원성격 비중이 군집마다 100% 에 가까우면 지원성격 복제다. "
              "위 표의 비중과 1절의 ARI 를 함께 읽어야 한다.", ""]

    L += ["## 4. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L += ["", "## 5. 쓰지 않은 것", "",
          "```text",
          "support_target / policy_purpose  자유서술 텍스트라 거리 계산에 넣지 않았다",
          "                                 (넣으려면 임베딩이 필요하고, 그러면 군집이",
          "                                  설계 구조가 아니라 문체를 따라간다)",
          "self_burden_ratio                support_ratio 와 100 합이라 중복이다",
          "```", ""]
    p = os.path.join(C.REPORTS, "s07a_m2_cluster.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
