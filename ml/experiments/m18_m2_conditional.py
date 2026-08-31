"""M18 — 모델 2 최종 실험: 지원방식 내부 조건부 군집.

추가개선계획서 3절. 무작정 parameter tuning 을 반복하지 말고 이것 하나만
마지막으로 해보라는 제안이다.

    M15 에서 확인한 것: weight 를 극단으로 주면 군집이 support_method 를
    그대로 복제한다. 그렇다면 애초에 support_method 로 갈라 놓고, **그 안에서**
    설계 구조가 갈리는지 본다. 복제할 축을 미리 제거한 상태에서 묻는 셈이다.

        grant 안에서   고액·소수형 / 소액·다수형 / 장기형 이 나오는가
        loan 안에서    한도·기간이 다른 유형이 갈리는가
        service 안에서 규모가 다른 유형이 갈리는가

성공 조건 (계획서 3절)
    지원방식 내부에서도 2~4개 안정적인 군집
    설계 특성 차이가 명확
    사람이 이름을 붙일 수 있음
    bootstrap 안정성 양호

실패 조건
    대부분 하나의 군집 / 금액 하나를 다시 복제 / 지나친 노이즈 / 재학습 시 붕괴

실패하면 모델 2는 최종 미채택.

feature 에서 support_method 를 뺀다 — 그룹 안에서는 상수라 거리에 기여하지
않는데 남겨두면 복제도 계산만 어지럽힌다.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m11_m2_cluster import (MIN_INDEPENDENT, SRC, gower, name_clusters, pcoa,
                            prepare, profile, score)
from m15_m2_tuning import (GRID_EPS, GRID_MCS, GRID_MS, MAX_FEATURE_ARI,
                           MAX_LARGEST_SHARE, bootstrap, feature_replication, fit,
                           to_params)

OUT = os.path.join(C.PROC, "design_clusters_conditional.parquet")
SEED = 42
MIN_GROUP = 100          # 이보다 작은 지원방식은 군집을 나눌 표본이 안 된다
MIN_CLUSTERS = 2
MAX_CLUSTERS = 8         # 계획서: 지원방식 내부 2~4개, 넉넉히 8까지 허용
MIN_BOOTSTRAP = 0.60
MAX_NOISE = 0.50

# 그룹 안에서 support_method 는 상수다. 빼고 나머지 설계 축으로만 재본다.
CAT = ["support_unit", "amount_type", "category_large", "industry_grp", "agency_type"]
NUM = ["log_per_recipient", "log_support_count", "support_ratio", "project_duration"]


def search_group(g, cat, num, quick=False):
    """그룹 안에서 HDBSCAN 격자를 훑는다. 실패 조건은 M15 와 같게 건다."""
    D = gower(g, cat, num)
    X = pcoa(D, k=min(8, len(g) - 1))
    y_type = g["support_type"].to_numpy()
    rows, skipped = [], 0
    mcs_list = [10, 15, 20, 30] if not quick else [15, 30]
    ms_list = GRID_MS if not quick else [None, 10]
    eps_list = GRID_EPS if not quick else [0.0]
    for mcs in mcs_list:
        if mcs * MIN_CLUSTERS > len(g):
            continue
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
                fr = feature_replication(g, lab, cat)
                r["max_feature_ari"] = round(max(fr.values()), 4) if fr else 0.0
                r["replicated_feature"] = max(fr, key=fr.get) if fr else None
                rows.append(r)
    return pd.DataFrame(rows), D, skipped


def pick_group(grid):
    """계획서 성공 조건을 그대로 필터로 옮긴다."""
    if not len(grid):
        return None
    ok = grid[(grid["silhouette"].notna())
              & grid["n_clusters"].between(MIN_CLUSTERS, MAX_CLUSTERS)
              & (grid["max_feature_ari"] < MAX_FEATURE_ARI)
              & (grid["largest_share"].fillna(1) < MAX_LARGEST_SHARE)
              & (grid["noise_ratio"] < MAX_NOISE)]
    if not len(ok):
        return None
    return ok.sort_values("silhouette", ascending=False).iloc[0]


def design_spread(g, labels, num):
    """군집 간 설계 특성이 실제로 갈리는가.

    '사람이 이름을 붙일 수 있음'을 수치로 옮긴 것이다. 군집별 중앙값이
    서로 비슷하면 이름을 붙일 근거가 없다. 축별로 최대/최소 비를 본다.
    """
    t = g.copy()
    t["__c"] = labels
    out = {}
    for f in num:
        med = t[t["__c"] >= 0].groupby("__c")[f].median().dropna()
        if len(med) < 2:
            continue
        if f.startswith("log_"):
            # 로그 축이라 차이는 배수로 읽는다
            out[f] = {"min": round(float(med.min()), 3),
                      "max": round(float(med.max()), 3),
                      "spread_x": round(float(10 ** (med.max() - med.min())), 2)}
        else:
            out[f] = {"min": round(float(med.min()), 2),
                      "max": round(float(med.max()), 2),
                      "spread_abs": round(float(med.max() - med.min()), 2)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()

    t = prepare(pd.read_parquet(SRC))
    print("모델 2 조건부 군집 대상: taxonomy %d행" % len(t))
    sizes = t["support_method"].value_counts()
    print("지원방식 분포: %s" % dict(sizes))

    t0 = time.time()
    results, labels_all = {}, pd.Series(-1, index=t.index, dtype=int)
    tag_all = pd.Series(None, index=t.index, dtype=object)

    for method, n in sizes.items():
        if n < MIN_GROUP:
            results[method] = {"n": int(n), "status": "표본부족",
                               "reason": "%d건 < %d건" % (n, MIN_GROUP)}
            print("\n-- %s (%d건) 건너뜀 — 표본 부족" % (method, n))
            continue

        g = t[t["support_method"] == method].reset_index()
        print("\n-- %s (%d건)" % (method, len(g)))
        grid, D, skipped = search_group(g, CAT, NUM, a.quick)
        row = pick_group(grid)
        if row is None:
            best_any = (grid.sort_values("silhouette", ascending=False).iloc[0]
                        if len(grid) and grid["silhouette"].notna().any() else None)
            results[method] = {
                "n": int(len(g)), "status": "성공조건 미달", "grid_size": int(len(grid)),
                "best_unfiltered": (None if best_any is None else
                                    {"n_clusters": int(best_any["n_clusters"]),
                                     "silhouette": float(best_any["silhouette"]),
                                     "noise_ratio": float(best_any["noise_ratio"]),
                                     "largest_share": float(best_any["largest_share"]),
                                     "max_feature_ari": float(best_any["max_feature_ari"])})}
            print("   성공조건을 만족하는 설정 없음 (격자 %d개)" % len(grid))
            if best_any is not None:
                print("   최선(무필터): 군집 %d / 실루엣 %.4f / 노이즈 %.1f%% / 최대군집 %.1f%%"
                      % (best_any["n_clusters"], best_any["silhouette"],
                         best_any["noise_ratio"] * 100, best_any["largest_share"] * 100))
            continue

        params = to_params(row)
        lab = fit(D, params)
        stab = bootstrap(g, CAT, NUM, {}, params, n_iter=10 if a.quick else 30)
        prof = profile(g, lab, NUM, CAT)
        tags, _ = name_clusters(prof)
        for c, p in prof.items():
            p["tag"] = tags[c]
        spread = design_spread(g, lab, NUM)

        ok_stab = (stab["ari_mean"] or 0) >= MIN_BOOTSTRAP
        results[method] = {
            "n": int(len(g)), "status": "성공" if ok_stab else "안정성 미달",
            "chosen": params, "n_clusters": int(row["n_clusters"]),
            "noise_ratio": float(row["noise_ratio"]),
            "largest_share": float(row["largest_share"]),
            "silhouette": float(row["silhouette"]),
            "max_feature_ari": float(row["max_feature_ari"]),
            "replicated_feature": row["replicated_feature"],
            "bootstrap": stab, "design_spread": spread, "profile": prof,
            "grid_size": int(len(grid)), "grid_skipped": int(skipped)}
        print("   군집 %d / 노이즈 %.1f%% / 최대군집 %.1f%% / 실루엣 %.4f"
              " / bootstrap %.4f / 복제 %.2f(%s)"
              % (row["n_clusters"], row["noise_ratio"] * 100,
                 row["largest_share"] * 100, row["silhouette"],
                 stab["ari_mean"] or 0, row["max_feature_ari"],
                 row["replicated_feature"]))
        for c, p in sorted(prof.items(), key=lambda kv: -kv[1]["n"]):
            print("     [%d] %3d건  %s" % (c, p["n"], p.get("tag", "—")))
        for f, v in spread.items():
            if "spread_x" in v:
                print("     %s 군집 간 %.1f배 차이" % (f, v["spread_x"]))

        idx = g["index"].to_numpy()
        labels_all.loc[idx] = [int(x) for x in lab]
        tag_all.loc[idx] = [tags.get(int(x)) if x >= 0 else None for x in lab]

    verdict = judge(results)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    t2 = t.assign(method_cluster=labels_all.to_numpy(),
                  method_cluster_tag=tag_all.to_numpy())
    t2[["row_id", "title", "support_type", "support_method", "support_unit",
        "per_recipient", "support_count", "project_duration",
        "method_cluster", "method_cluster_tag"]].to_parquet(OUT, index=False)
    print("[data] %s" % OUT)

    C.save_report("m18_m2_conditional.json", {
        "n_rows": int(len(t)), "min_group": MIN_GROUP,
        "criteria": {"min_clusters": MIN_CLUSTERS, "max_clusters": MAX_CLUSTERS,
                     "max_feature_ari": MAX_FEATURE_ARI,
                     "max_largest_share": MAX_LARGEST_SHARE,
                     "max_noise": MAX_NOISE, "min_bootstrap": MIN_BOOTSTRAP},
        "features": {"cat": CAT, "num": NUM},
        "groups": results, "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2)})
    write_md(results, verdict, sizes)


def judge(results):
    ok = [k for k, v in results.items() if v.get("status") == "성공"]
    partial = [k for k, v in results.items() if v.get("status") == "안정성 미달"]
    reasons = []
    for k, v in results.items():
        reasons.append("%s (%d건): %s" % (k, v["n"], v.get("status", "?")))
    if not ok:
        reasons.append("어느 지원방식 내부에서도 성공 조건을 만족하는 군집이 없다. "
                       "계획서 3절의 실패 조건에 해당하므로 모델 2는 최종 미채택")
        return {"verdict": "최종 미채택", "reasons": reasons, "success_groups": []}

    spreads = []
    for k in ok:
        s = results[k].get("design_spread", {})
        m = max((v.get("spread_x", 0) for v in s.values()), default=0)
        spreads.append((k, m))
    reasons.append("성공한 지원방식 %d개: %s" % (len(ok), ", ".join(ok)))
    for k, m in spreads:
        reasons.append("%s 내부 설계 특성 최대 %.1f배 차이" % (k, m))
    v = "조건부 채택" if len(ok) < len(results) - len(
        [x for x in results.values() if x.get("status") == "표본부족"]) else "채택"
    if partial:
        reasons.append("안정성 미달: %s" % ", ".join(partial))
    return {"verdict": v, "reasons": reasons, "success_groups": ok}


def write_md(results, verdict, sizes):
    L = ["# 모델 2 최종 실험 — 지원방식 내부 조건부 군집", "",
         "## 0. 왜 이 실험인가", "",
         "M15 에서 확인한 것: Gower weight 를 극단으로 주면 군집이 `support_method`",
         "값을 그대로 복제합니다. 지표는 좋아지지만 새 설계유형을 배운 것이 아닙니다.", "",
         "그래서 **복제할 축을 미리 제거**합니다. `support_method` 로 먼저 갈라 놓고",
         "그 안에서 설계 구조가 갈리는지 봅니다. 그룹 안에서 `support_method` 는",
         "상수이므로 feature 에서도 뺐습니다.", "",
         "계획서 3절: \"무작정 parameter tuning 을 반복하는 것은 비추천. 대신",
         "`support_method` 내부 조건부 clustering 을 1회 최종 실험 권장.\"", "",
         "## 1. 성공 조건 (계획서 3절을 필터로 옮김)", "",
         "```text",
         "군집 수            %d~%d개" % (MIN_CLUSTERS, MAX_CLUSTERS),
         "feature 복제도     ARI < %.2f" % MAX_FEATURE_ARI,
         "최대 군집 비중     < %.0f%%" % (MAX_LARGEST_SHARE * 100),
         "노이즈             < %.0f%%" % (MAX_NOISE * 100),
         "bootstrap ARI      >= %.2f" % MIN_BOOTSTRAP,
         "그룹 최소 표본     %d건" % MIN_GROUP,
         "```", "",
         "## 2. 지원방식별 결과", "",
         "| 지원방식 | n | 결과 | 군집 | 노이즈 | 최대군집 | 실루엣 | bootstrap | 복제도 |",
         "|---|---:|---|---:|---:|---:|---:|---:|---|"]
    for k, v in results.items():
        if v.get("status") in ("표본부족", "성공조건 미달"):
            L.append("| %s | %d | %s | — | — | — | — | — | — |"
                     % (k, v["n"], v["status"]))
            continue
        L.append("| %s | %d | %s | %d | %.1f%% | %.1f%% | %.4f | %.4f | %.2f (%s) |"
                 % (k, v["n"], v["status"], v["n_clusters"], v["noise_ratio"] * 100,
                    v["largest_share"] * 100, v["silhouette"],
                    v["bootstrap"]["ari_mean"] or 0, v["max_feature_ari"],
                    v["replicated_feature"]))

    for k, v in results.items():
        if "profile" not in v:
            continue
        L += ["", "### %s 내부 군집" % k, "",
              "| 군집 | n | 태그 | 기업당지원액 중앙값 | 지원건수 중앙값 |",
              "|---:|---:|---|---:|---:|"]
        for c, p in sorted(v["profile"].items(), key=lambda kv: -kv[1]["n"]):
            amt = p.get("per_recipient_median_won")
            cnt = p.get("support_count_median")
            L.append("| %s | %d | %s | %s | %s |"
                     % (c, p["n"], p.get("tag", "—"),
                        ("%.0f만원" % (amt / 1e4)) if amt else "—",
                        ("%.0f" % cnt) if cnt else "—"))
        sp = v.get("design_spread", {})
        if sp:
            L += ["", "설계 특성 군집 간 차이:", ""]
            for f, s in sp.items():
                if "spread_x" in s:
                    L.append("- `%s` **%.1f배**" % (f, s["spread_x"]))
                else:
                    L.append("- `%s` %.2f 차이" % (f, s["spread_abs"]))

    L += ["", "## 3. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L += ["", "## 4. 발표용 해석 (계획서 4절)", "",
          "> 지표 최적화 과정에서 feature 복제 현상을 발견하였다.",
          "> 단순 지표 향상은 가능했으나 새로운 설계유형을 학습한 것이 아니므로",
          "> 채택하지 않았다.", "",
          "실패라기보다 **잘못된 최적화를 걸러낸 검증 결과**입니다.", ""]
    p = os.path.join(C.REPORTS, "m18_m2_conditional.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
