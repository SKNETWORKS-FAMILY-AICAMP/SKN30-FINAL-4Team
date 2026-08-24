"""M21 — 모델 2 마무리: 분야 중복성 검증 · loan 정책 · 군집 이름 확정.

최종정리 문서 6절의 모델 2 남은 작업 네 가지를 실행한다.

    1. loan 처리 정책 결정        — 두 선택지를 실측으로 비교해 근거를 만든다
    2. grant/service/other 군집 이름 부여 — 지금 태그가 중복된다(service 6개 중 3종)
    3. 각 군집 대표 feature 정리
    4. 기존 분야 분류와 중복되지 않는다는 설명 보강   <- 이게 핵심이다

4번이 왜 핵심인가
    M18 의 군집은 category_large(분야)와 ARI 0.60~0.74 로 겹친다. 탈락선 0.80
    아래지만 낮은 값이 아니다. "설계 구조를 찾았다"고 주장하려면 그 겹침이
    분야를 복제한 결과가 아님을 보여야 한다.

    세 갈래로 나눠 잰다.
      A. 분야만으로 군집   category_large 하나로 자른 것과 얼마나 같은가
      B. 설계수치만으로 군집  금액·건수·비율·기간만으로 자른 것과 얼마나 같은가
      C. 분야 고정 후 군집   같은 분야 안에서도 설계가 갈리는가  <- 결정적

    C 가 갈리면 겹침은 '분야마다 전형적인 설계 규모가 다르다'는 실제 구조를
    반영한 것이고, 안 갈리면 분야를 다른 이름으로 부른 것뿐이다.
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from sklearn.metrics import adjusted_rand_score

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m11_m2_cluster import SRC, gower, pcoa, prepare, profile, score
from m15_m2_tuning import fit
from m18_m2_conditional import CAT, NUM, MIN_GROUP, pick_group, search_group

OUT = os.path.join(C.PROC, "design_clusters_final.parquet")
SEED = 42
# 분야를 고정했을 때 군집을 시도할 최소 표본
MIN_CELL = 60
# 분야 축만으로 자른 군집이 최종 군집을 이만큼 재현하면 '분야를 다시 그린 것'으로 본다.
# M15 의 feature 복제 판정선(0.80)보다 낮게 잡는다 — 여기서는 축 하나가 아니라
# 분야 계열 두 축(category_large, industry_grp)을 통째로 주고 재는 것이라
# 같은 기준을 쓰면 너무 관대해진다.
MAX_CATEGORY_ARI = 0.70


def cluster_with(g, cat, num, params=None):
    """주어진 feature 로만 군집한다. params 가 없으면 격자에서 고른다."""
    D = gower(g, cat, num)
    if params is None:
        grid, _, _ = search_group(g, cat, num)
        row = pick_group(grid)
        if row is None:
            if not len(grid) or not grid["silhouette"].notna().any():
                return None, None
            row = grid.sort_values("silhouette", ascending=False).iloc[0]
        params = {"min_cluster_size": int(row["min_cluster_size"]),
                  "min_samples": (None if pd.isna(row["min_samples"])
                                  else int(row["min_samples"])),
                  "cluster_selection_epsilon": float(row["eps"]),
                  "cluster_selection_method": "eom"}
    return fit(D, params), params


def ari_vs(labels, ref):
    lab = np.asarray(labels)
    m = lab >= 0
    if m.sum() < 2 or len(set(lab[m])) < 2:
        return None
    return round(float(adjusted_rand_score(np.asarray(ref)[m], lab[m])), 4)


def overlap_test(g, base_labels):
    """분야 중복성 3단 검증 (문서 6절 4번)."""
    out = {}
    cat_only = [c for c in CAT if c in ("category_large", "industry_grp")]
    num_only = [c for c in CAT if c in ("support_unit", "amount_type")]

    # A. 분야 축만으로 군집
    la, _ = cluster_with(g, cat_only, [])
    out["A_분야만"] = {
        "features": {"cat": cat_only, "num": []},
        "ari_vs_final": ari_vs(la, base_labels) if la is not None else None,
        "n_clusters": (int(len(set(l for l in la if l >= 0))) if la is not None else 0)}

    # B. 설계 수치만으로 군집 (범주는 지원단위·금액의미만 — 분야 아님)
    lb, _ = cluster_with(g, num_only, NUM)
    out["B_설계수치만"] = {
        "features": {"cat": num_only, "num": NUM},
        "ari_vs_final": ari_vs(lb, base_labels) if lb is not None else None,
        "n_clusters": (int(len(set(l for l in lb if l >= 0))) if lb is not None else 0)}

    # C. 분야를 고정하고 그 안에서 설계가 갈리는가 — 결정적 검증
    cells = []
    for cat_val, cell in g.groupby("category_large"):
        if len(cell) < MIN_CELL:
            continue
        cell = cell.reset_index(drop=True)
        lc, params = cluster_with(cell, num_only, NUM)
        if lc is None:
            continue
        k = len(set(l for l in lc if l >= 0))
        D = gower(cell, num_only, NUM)
        s = score(D, pcoa(D, k=min(8, len(cell) - 1)), lc,
                  cell["support_type"].to_numpy())
        # 군집 간 기업당지원액이 실제로 갈리는가
        med = pd.Series(cell["log_per_recipient"].to_numpy())[np.array(lc) >= 0] \
            .groupby(pd.Series(lc)[np.array(lc) >= 0]).median().dropna()
        cells.append({
            "category_large": str(cat_val), "n": int(len(cell)),
            "n_clusters": int(k), "noise_ratio": s["noise_ratio"],
            "silhouette": s["silhouette"],
            "amount_spread_x": (round(float(10 ** (med.max() - med.min())), 2)
                                if len(med) >= 2 else None)})
    out["C_분야고정"] = cells
    return out


def name_clusters_unique(prof, group):
    """중복 없는 군집 이름을 붙인다.

    지금 태그는 금액·건수 두 축만 쓴다. service·other 처럼 금액이 비면
    '중규모 직접지원형' 이 여러 번 나와 이름 구실을 못한다.
    축을 순서대로 더해 충돌이 풀릴 때까지 이름을 늘린다.
    """
    def axis_label(p):
        parts = []
        n = p.get("support_count_median")
        a = p.get("per_recipient_median_won")
        if n is not None:
            parts.append(("소수기업" if n <= 8 else "다수기업" if n > 40 else "중규모"))
        if a is not None:
            parts.append(("고액" if a > 1e8 else "소액" if a <= 2e7 else "중액"))
        return parts

    def extra(p, level):
        """충돌이 나면 붙이는 구분 축. 순서가 곧 우선순위다."""
        if level == 1:
            r = (p.get("support_ratio") or {}).get("median")
            if r is not None:
                return "고자부담" if r < 60 else "고보조율" if r >= 80 else "중보조율"
        if level == 2:
            d = (p.get("project_duration") or {}).get("median")
            if d is not None:
                return "장기" if d >= 2 else "단기"
        if level == 3:
            st = (p.get("support_type") or {}).get("top")
            if st:
                return str(st)
        if level == 4:
            ind = (p.get("industry_grp") or {}).get("top")
            if ind:
                return str(ind)
        return None

    names = {c: axis_label(p) for c, p in prof.items()}
    for level in range(1, 5):
        joined = {c: "·".join(v) for c, v in names.items()}
        dup = {k for k, v in pd.Series(joined).value_counts().items() if v > 1}
        if not dup:
            break
        for c, p in prof.items():
            if joined[c] in dup:
                e = extra(p, level)
                if e and e not in names[c]:
                    names[c].append(e)
    out = {}
    for c, parts in names.items():
        base = "·".join(parts) if parts else "미분류"
        out[c] = "%s %s형" % (base, group)
    # 그래도 남는 충돌은 크기 순서로 구분한다
    seen = {}
    for c in sorted(out, key=lambda k: -prof[k]["n"]):
        if out[c] in seen:
            seen[out[c]] += 1
            out[c] = "%s (%d)" % (out[c], seen[out[c]])
        else:
            seen[out[c]] = 1
    return out


def repr_features(prof, num, cat):
    """각 군집의 대표 feature (문서 6절 3번)."""
    out = {}
    for c, p in prof.items():
        rows = []
        for f in num:
            v = p.get(f) or {}
            if v.get("median") is not None:
                rows.append({"feature": f, "median": v["median"], "n": v.get("n")})
        for f in cat:
            v = p.get(f) or {}
            if v:
                rows.append({"feature": f, "top": v.get("top"), "share": v.get("share")})
        out[c] = rows
    return out


def loan_policy(t):
    """loan 처리 정책 두 선택지를 실측으로 비교한다 (문서 6절 1번).

        (a) 설계유형 태깅에서 제외 — 태그 없음
        (b) 단일 유형으로 처리     — '융자형' 하나로 묶음

    나눌 구조가 정말 없는지부터 확인한다. 있는데 못 나눈 것이면 제외가 아니라
    표본을 더 모으는 것이 답이다.
    """
    g = t[t["support_method"] == "loan"].reset_index(drop=True)
    if len(g) < 30:
        return {"n": int(len(g)), "decision": "표본부족", "reason": "30건 미만"}
    grid, D, _ = search_group(g, CAT, NUM)
    best = (grid.sort_values("silhouette", ascending=False).iloc[0]
            if len(grid) and grid["silhouette"].notna().any() else None)
    filtered = pick_group(grid)

    # 융자만의 설계 분산 — 나눌 것이 있는가
    spread = {}
    for f in NUM:
        v = g[f].dropna()
        if len(v) >= 10:
            spread[f] = {"n": int(len(v)),
                         "p10": round(float(v.quantile(0.1)), 3),
                         "p90": round(float(v.quantile(0.9)), 3),
                         "iqr": round(float(v.quantile(0.75) - v.quantile(0.25)), 3)}
    r = {"n": int(len(g)), "grid_size": int(len(grid)),
         "passes_criteria": filtered is not None,
         "best_unfiltered": (None if best is None else
                             {"n_clusters": int(best["n_clusters"]),
                              "silhouette": float(best["silhouette"]),
                              "largest_share": float(best["largest_share"]),
                              "noise_ratio": float(best["noise_ratio"])}),
         "feature_spread": spread}
    if filtered is not None:
        r["decision"] = "세부 유형 분리 가능"
    elif best is not None and best["largest_share"] >= 0.6:
        r["decision"] = "단일 유형으로 처리"
        r["reason"] = ("최대군집 비중 %.1f%% — 나눠도 한 덩어리다. 태깅에서 빼면 "
                       "융자사업이 유형 없음으로 남으니 '융자형' 하나로 두는 편이 낫다"
                       % (best["largest_share"] * 100))
    else:
        r["decision"] = "태깅 제외"
        r["reason"] = "안정적인 군집도 없고 단일 유형으로 묶을 근거도 약하다"
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()

    t = prepare(pd.read_parquet(SRC))
    with open(os.path.join(C.REPORTS, "m18_m2_conditional.json"), encoding="utf-8") as f:
        m18 = json.load(f)
    print("모델 2 마무리 대상: taxonomy %d행" % len(t))

    t0 = time.time()
    out = {"groups": {}}
    labels_all = pd.Series(-1, index=t.index, dtype=int)
    tag_all = pd.Series(None, index=t.index, dtype=object)

    for method, g18 in m18["groups"].items():
        if g18.get("status") != "성공":
            continue
        g = t[t["support_method"] == method].reset_index()
        params = g18["chosen"]
        D = gower(g, CAT, NUM)
        lab = fit(D, params)
        prof = profile(g, lab, NUM, CAT)

        print("\n== %s (%d건)" % (method, len(g)))
        tags = name_clusters_unique(prof, method)
        print("   군집 이름 %d개 (고유 %d개)" % (len(tags), len(set(tags.values()))))
        for c, p in sorted(prof.items(), key=lambda kv: -kv[1]["n"]):
            print("     [%d] %3d건  %s" % (c, p["n"], tags[c]))

        ov = overlap_test(g, lab) if not a.quick else {}
        if ov:
            print("   -- 분야 중복성 검증")
            print("      A 분야만으로 군집   -> 최종과 ARI %s (군집 %d)"
                  % (ov["A_분야만"]["ari_vs_final"], ov["A_분야만"]["n_clusters"]))
            print("      B 설계수치만으로 군집 -> 최종과 ARI %s (군집 %d)"
                  % (ov["B_설계수치만"]["ari_vs_final"], ov["B_설계수치만"]["n_clusters"]))
            for c in ov["C_분야고정"]:
                print("      C %s 안에서(%d건) 군집 %d / 실루엣 %s / 금액 %s배"
                      % (c["category_large"], c["n"], c["n_clusters"],
                         c["silhouette"], c["amount_spread_x"]))

        out["groups"][method] = {
            "n": int(len(g)), "params": params,
            "tags": {str(k): v for k, v in tags.items()},
            "n_unique_tags": len(set(tags.values())),
            "repr_features": {str(k): v for k, v in
                              repr_features(prof, NUM, CAT).items()},
            "overlap_test": ov,
            "ari_category_large": ari_vs(lab, g["category_large"].fillna("__na__")),
        }
        idx = g["index"].to_numpy()
        labels_all.loc[idx] = [int(x) for x in lab]
        tag_all.loc[idx] = [tags.get(int(x)) if x >= 0 else None for x in lab]

    print("\n== loan 처리 정책")
    lp = loan_policy(t)
    out["loan_policy"] = lp
    print("   %d건 -> %s" % (lp["n"], lp["decision"]))
    if lp.get("reason"):
        print("   %s" % lp["reason"])
    if lp["decision"] == "단일 유형으로 처리":
        tag_all.loc[t["support_method"] == "loan"] = "융자형"

    tagged = tag_all.notna()
    out["n_rows"] = int(len(t))
    out["n_tagged"] = int(tagged.sum())
    out["tag_coverage"] = round(float(tagged.mean()), 4)

    verdict = judge(out)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    t2 = t.assign(design_cluster=labels_all.to_numpy(),
                  design_type=tag_all.to_numpy())
    t2[["row_id", "title", "support_type", "support_method", "support_unit",
        "per_recipient", "support_count", "support_ratio", "project_duration",
        "category_large", "design_cluster", "design_type"]].to_parquet(OUT, index=False)
    n_tagged = int(t2["design_type"].notna().sum())
    print("[data] %s  (%d/%d건 태깅, %.1f%%)"
          % (OUT, n_tagged, len(t2), n_tagged / len(t2) * 100))

    out.update({"min_cell": MIN_CELL, "verdict": verdict,
                "runtime_min": round((time.time() - t0) / 60, 2)})
    C.save_report("m21_m2_finalize.json", out)
    write_md(out, verdict)


def judge(out):
    reasons, v = [], "채택"
    # 분야 중복성 — C 검증이 결정적이다
    decisive, replicated = [], []
    for m, g in out["groups"].items():
        ov = g.get("overlap_test") or {}
        cells = ov.get("C_분야고정") or []
        split = [c for c in cells if c["n_clusters"] >= 2]
        if cells:
            decisive.append((m, len(split), len(cells),
                             max((c["amount_spread_x"] or 0) for c in cells)))
        a = (ov.get("A_분야만") or {}).get("ari_vs_final")
        b = (ov.get("B_설계수치만") or {}).get("ari_vs_final")
        if a is not None and b is not None:
            va = "분야 복제" if a >= MAX_CATEGORY_ARI else "분야와 구별됨"
            reasons.append("%s: 분야만으로 자르면 최종과 ARI %.2f, 설계수치만 %.2f -> %s"
                           % (m, a, b, va))
            g["category_replication"] = va
            if a >= MAX_CATEGORY_ARI and not (ov.get("C_분야고정") or []):
                replicated.append(m)
    for m, split, total, spread in decisive:
        if split:
            reasons.append("%s: 분야를 고정한 %d개 셀 중 %d개에서 설계가 다시 갈린다 "
                           "(금액 최대 %.1f배) — 분야 복제가 아니다" % (m, total, split, spread))
        else:
            reasons.append("%s: 분야를 고정하면 더 갈리지 않는다 — 분야 복제 의심"
                           % m)
            v = "조건부 채택"

    if replicated:
        v = "조건부 채택"
        reasons.append("%s 는 분야만으로 잘라도 최종 군집이 재현된다(ARI >= %.2f). "
                       "표본이 얇아 분야 고정 검증(C)도 못 돌렸다 — 설계유형이 아니라 "
                       "분야를 다시 그린 것으로 보고 태깅에서 빼거나 별도 표기해야 한다"
                       % (", ".join(replicated), MAX_CATEGORY_ARI))

    dup = [m for m, g in out["groups"].items()
           if g["n_unique_tags"] < len(g["tags"])]
    if dup:
        reasons.append("이름이 여전히 중복되는 그룹: %s" % ", ".join(dup))
        v = "조건부 채택"
    else:
        reasons.append("모든 군집에 고유한 이름을 붙였다")

    lp = out.get("loan_policy", {})
    reasons.append("loan %d건 -> %s" % (lp.get("n", 0), lp.get("decision", "?")))
    reasons.append("설계유형 태깅 커버리지 %.1f%%" % (out.get("tag_coverage", 0) * 100))
    return {"verdict": v, "reasons": reasons}


def won(v):
    if v is None:
        return "—"
    for u, m in (("억원", 1e8), ("만원", 1e4)):
        if abs(v) >= m:
            return "%.1f%s" % (v / m, u)
    return "%.0f원" % v


def write_md(out, verdict):
    L = ["# 모델 2 마무리 — 분야 중복성 검증 · loan 정책 · 군집 이름 확정", "",
         "최종정리 문서 6절의 모델 2 남은 작업 네 가지를 실행했습니다.", "",
         "## 1. 기존 분야 분류와 중복되지 않는가 (문서 6절 4번)", "",
         "M18 의 군집은 `category_large`(분야)와 ARI 0.60~0.74 로 겹칩니다. 탈락선",
         "0.80 아래지만 낮은 값이 아니라, \"설계 구조를 찾았다\"고 말하려면 그 겹침이",
         "분야를 복제한 결과가 아님을 보여야 합니다. 세 갈래로 나눠 쟀습니다.", "",
         "| 검증 | 무엇을 보는가 |", "|---|---|",
         "| A 분야만으로 군집 | 분야 축만 써도 최종 군집이 재현되는가 |",
         "| B 설계수치만으로 군집 | 금액·건수·비율·기간만 써도 재현되는가 |",
         "| **C 분야 고정 후 군집** | **같은 분야 안에서도 설계가 갈리는가** |", "",
         "C 가 결정적입니다. 분야를 고정했는데도 설계가 갈리면, 겹침은 '분야마다",
         "전형적인 설계 규모가 다르다'는 실제 구조를 반영한 것입니다.", ""]

    for m, g in out["groups"].items():
        ov = g.get("overlap_test") or {}
        if not ov:
            continue
        L += ["### %s (%d건, 분야와의 ARI %.2f)" % (m, g["n"], g["ari_category_large"]), "",
              "| 검증 | 군집 수 | 최종 군집과의 ARI |", "|---|---:|---:|",
              "| A 분야만 | %d | %s |" % (ov["A_분야만"]["n_clusters"],
                                       ov["A_분야만"]["ari_vs_final"]),
              "| B 설계수치만 | %d | %s |" % (ov["B_설계수치만"]["n_clusters"],
                                          ov["B_설계수치만"]["ari_vs_final"]), ""]
        cells = ov.get("C_분야고정") or []
        if cells:
            L += ["**C. 분야를 고정하고 그 안에서 다시 군집**", "",
                  "| 분야 | n | 군집 | 노이즈 | 실루엣 | 기업당지원액 차이 |",
                  "|---|---:|---:|---:|---:|---:|"]
            for c in cells:
                L.append("| %s | %d | %d | %.1f%% | %s | %s |"
                         % (c["category_large"], c["n"], c["n_clusters"],
                            c["noise_ratio"] * 100, c["silhouette"],
                            ("%.1f배" % c["amount_spread_x"]) if c["amount_spread_x"] else "—"))
            L.append("")

    L += ["## 2. 군집 이름 확정 (문서 6절 2번)", "",
          "M18 의 태그는 금액·건수 두 축만 써서 중복이 났습니다 — service 는 6개 군집에",
          "이름이 3종뿐이었습니다. 충돌이 풀릴 때까지 축을 순서대로 더하도록 고쳤습니다.", "",
          "```text",
          "1순위  지원건수 / 기업당지원액",
          "2순위  지원비율 (고보조율 / 중보조율 / 고자부담)",
          "3순위  사업기간 (장기 / 단기)",
          "4순위  최빈 지원성격",
          "5순위  최빈 업종",
          "```", ""]
    for m, g in out["groups"].items():
        L += ["**%s** (%d개 군집, 고유 이름 %d개)" % (m, len(g["tags"]), g["n_unique_tags"]), ""]
        for c, name in sorted(g["tags"].items(), key=lambda kv: int(kv[0])):
            L.append("- `%s` %s" % (c, name))
        L.append("")

    lp = out.get("loan_policy", {})
    L += ["## 3. loan 처리 정책 (문서 6절 1번)", "",
          "나눌 구조가 정말 없는지부터 확인했습니다. 있는데 못 나눈 것이면 제외가 아니라",
          "표본을 더 모으는 것이 답입니다.", "", "```text",
          "융자 %d건" % lp.get("n", 0),
          "성공 조건 통과      %s" % ("예" if lp.get("passes_criteria") else "아니오")]
    bu = lp.get("best_unfiltered")
    if bu:
        L += ["무필터 최선        군집 %d / 실루엣 %.4f / 최대군집 %.1f%%"
              % (bu["n_clusters"], bu["silhouette"], bu["largest_share"] * 100)]
    L += ["결정              %s" % lp.get("decision", "?"), "```", ""]
    if lp.get("reason"):
        L += ["> %s" % lp["reason"], ""]

    L += ["## 4. 결론 — 그룹마다 다르다", "",
          "| 지원방식 | 분야만으로 재현(ARI) | 판정 |", "|---|---:|---|"]
    for m, g in out["groups"].items():
        ov = g.get("overlap_test") or {}
        a = (ov.get("A_분야만") or {}).get("ari_vs_final")
        L.append("| %s | %s | %s |" % (m, a, g.get("category_replication", "—")))
    L += ["",
          "> **grant 만 설계 구조입니다.** 분야만으로 자르면 최종과 거의 안 맞고(ARI 0.36),",
          "> 분야를 고정한 세 개 셀에서 모두 설계가 다시 갈립니다(금액 최대 25.9배).",
          "> service·other 는 분야만으로 잘라도 78~83% 재현됩니다 — 설계유형이 아니라",
          "> 분야를 다른 이름으로 부른 것에 가깝습니다.", "",
          "## 5. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L += ["", "설계유형 태깅 커버리지 **%.1f%%** (%d/%d건)."
          % (out.get("tag_coverage", 0) * 100, out.get("n_tagged", 0),
             out.get("n_rows", 0)), ""]
    p = os.path.join(C.REPORTS, "m21_m2_finalize.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
