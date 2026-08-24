"""M12 — 모델 3: 기존 사업 대비 지원규모 상대 비교.

이 모델이 하지 않는 것 (설계서 명시)
    지원규모 적정/부적정 판단, 예산 과다/과소 판단, 삭감/증액 제안
이 모델이 하는 것
    비교군의 P10~P90 과 신규 사업의 percentile rank, 비교군 수

지원규모를 하나의 숫자로 뭉개지 않는다. 축을 나눠 각각 비교한다.
    per_recipient   기업(과제)당 지원액
    support_count   지원 기업/과제 수
    support_ratio   지원비율
    project_duration 사업기간
축마다 결측률이 다르므로 "이 축은 비교군이 부족하다"를 축 단위로 말할 수 있어야 한다.

비교군은 지원성격 + 지원방식 2단이 원칙이고, 30건에 못 미치면 단계적으로 후퇴한다.
    성격x방식x단위x출처 -> 성격x방식x단위 -> 성격x단위 -> 단위
    (지원단위가 필요없는 축은 단위 자리를 빼고 같은 사다리를 탄다)
지원단위만은 후퇴로 없앨 수 없다. 기업당 1억원과 과제당 1억원은 다른 값이라
같은 분포에 넣는 순간 percentile 이 무의미해진다.
후퇴했다는 사실 자체가 출력에 남는다. 조용히 넓히면 "94%보다 높다"는 문장의
근거가 무엇인지 알 수 없게 된다.

Phase B 는 ML(LightGBM/CatBoost quantile)을 같은 잣대로 재고, cohort median
baseline 을 의미 있게 못 이기면 채택하지 않는다 (설계서 지시).
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SRC = os.path.join(C.PROC, "design_features.parquet")
CLU = os.path.join(C.PROC, "design_clusters.parquet")
OUT = os.path.join(C.PROC, "cohort_reference.parquet")

MIN_COHORT = 30
PCTS = [10, 25, 50, 75, 90]
# 축별 설정.
#   log       금액·건수는 자릿수 범위가 넓어 로그에서 다룬다
#   unit_split 지원단위(기업/과제/인/팀)를 비교군에서 반드시 갈라야 하는가
#
# 기업당 1억원 / 과제당 1억원 / 인당 1억원은 같은 숫자지만 다른 값이다. 섞어 놓은
# 실측: company 1,757건 중앙값 5,000만원, project 148건 7,750만원, person 28건
# 2,325만원. 그래서 지원단위는 표본이 모자라도 완화할 수 있는 축이 아니다 —
# 다른 축은 30건에 못 미치면 넓히지만, 단위는 끝까지 고정하고 대신 '비교군 부족'을 낸다.
AXES = {
    "per_recipient": {"log": True, "unit_split": True},
    "support_count": {"log": True, "unit_split": True},
    "support_ratio": {"log": False, "unit_split": False},
    "project_duration": {"log": False, "unit_split": False},
}
# ML 이 baseline 을 이겼다고 말하려면 이만큼은 줄여야 한다.
# A04(시계열 예측)에서 쓴 규율과 같다 — 개선폭을 미리 못박고 시작한다.
MIN_IMPROVEMENT = 0.10


def prepare(df):
    d = df.copy()
    # 파싱 오류로 표시된 금액은 비교군 분포에 넣지 않는다.
    d.loc[d["amount_outlier"], "per_recipient"] = np.nan
    d = d[d["support_type"].notna()].copy()
    d["industry_grp"] = d["industry"].fillna("미기재")
    top = d["industry_grp"].value_counts().head(15).index
    d["industry_grp"] = d["industry_grp"].where(d["industry_grp"].isin(top), "기타업종")
    return d


# ------------------------------------------------------------ Phase A
def _specs(unit_split):
    """비교군 후퇴 사다리. 안쪽일수록 좁다.

    출처를 가장 안쪽에 둔다. taxonomy(2023 중앙부처)와 bizinfo(공고 원문)의
    중앙값이 칸에 따라 최대 12배까지 갈린다 — 섞으면 percentile 이 두 모집단의
    평균을 가리켜 어느 쪽과 비교한 것인지 말할 수 없게 된다.
    """
    if unit_split:
        # support_unit 은 사다리 전 단계에 고정으로 들어간다(후퇴로 없앨 수 없는 축).
        return [("성격x방식x단위x출처",
                 ["support_type", "support_method", "support_unit", "cohort"]),
                ("성격x방식x단위", ["support_type", "support_method", "support_unit"]),
                ("성격x단위", ["support_type", "support_unit"]),
                ("단위", ["support_unit"])]
    return [("성격x방식x출처", ["support_type", "support_method", "cohort"]),
            ("성격x방식", ["support_type", "support_method"]),
            ("성격", ["support_type"]),
            ("전체", [])]


def build_reference(d):
    """비교군 참고분포 테이블. 사다리의 모든 단계를 미리 만들어 둔다."""
    rows = []
    for axis, cfg in AXES.items():
        is_log, unit_split = cfg["log"], cfg["unit_split"]
        for level, keys in _specs(unit_split):
            sub = d[d[axis].notna() & (d[axis] > 0 if is_log else True)]
            groups = ([(("전체",), sub)] if not keys
                      else list(sub.groupby(keys, dropna=True)))
            for key, g in groups:
                v = g[axis].astype(float)
                if len(v) < 5:
                    continue
                key = key if isinstance(key, tuple) else (key,)
                kv = dict(zip(keys, key))
                r = {"axis": axis, "level": level,
                     "support_type": kv.get("support_type"),
                     "support_method": kv.get("support_method"),
                     "support_unit": kv.get("support_unit"),
                     "ref_cohort": kv.get("cohort"),
                     "n": int(len(v))}
                for p in PCTS:
                    r["p%d" % p] = float(np.percentile(v, p))
                r["iqr"] = r["p75"] - r["p25"]
                # 두 코호트가 같은 이야기를 하는지. 2배 넘게 갈리면 표시한다.
                med = g.groupby("cohort")[axis].median()
                r["cohort_mix"] = "|".join("%s:%d" % (k, v2) for k, v2
                                           in g["cohort"].value_counts().items())
                r["source_divergence"] = (round(float(med.max() / med.min()), 2)
                                          if len(med) > 1 and med.min() > 0 else None)
                rows.append(r)
    return pd.DataFrame(rows)


def lookup(ref, axis, support_type, support_method, unit=None, cohort=None):
    """비교군을 찾는다. 30건에 못 미치면 후퇴하고, 후퇴 사실을 함께 돌려준다.

    지원단위가 필요한 축인데 신규 사업의 단위를 모르면 비교를 포기한다.
    모르는 채로 넓은 분포와 대보면 기업당 금액을 과제당 분포와 견주게 된다.
    """
    unit_split = AXES[axis]["unit_split"]
    if unit_split and not unit:
        return None, None, True

    want = {"support_type": support_type, "support_method": support_method,
            "support_unit": unit, "cohort": cohort}
    for level, keys in _specs(unit_split):
        cond = pd.Series(True, index=ref.index)
        for col in ("support_type", "support_method", "support_unit", "ref_cohort"):
            src = "cohort" if col == "ref_cohort" else col
            cond &= (ref[col] == want[src]) if src in keys else ref[col].isna()
        c = ref[(ref["axis"] == axis) & (ref["level"] == level) & cond]
        if len(c) and int(c.iloc[0]["n"]) >= MIN_COHORT:
            return c.iloc[0], level, False
    # 어느 단계도 30건을 못 채우면 가장 넓은 것을 주되 '부족' 표시를 단다
    widest = _specs(unit_split)[-1][0]
    c = ref[(ref["axis"] == axis) & (ref["level"] == widest)]
    if unit_split:
        c = c[c["support_unit"] == unit]
    return (c.iloc[0] if len(c) else None), widest, True


def compare(ref, axis, value, support_type, support_method, unit=None, cohort=None):
    """설계서가 허용한 출력만 만든다. 적정/과다 같은 판정어는 넣지 않는다."""
    row, level, insufficient = lookup(ref, axis, support_type, support_method, unit, cohort)
    if row is None or value is None or (isinstance(value, float) and np.isnan(value)):
        reason = ("지원단위 미확정 — 기업당/과제당/인당을 섞어 비교할 수 없다"
                  if AXES[axis]["unit_split"] and not unit else
                  "비교군 없음" if row is None else "신규 사업 값 미기재")
        return {"axis": axis, "status": "비교불가", "reason": reason}
    dist = {"p%d" % p: float(row["p%d" % p]) for p in PCTS}
    # percentile rank: 비교군의 몇 %가 이 값 이하인가
    ps = np.array([row["p%d" % p] for p in PCTS], dtype=float)
    rank = float(np.interp(value, ps, PCTS, left=0, right=100))
    return {
        "axis": axis, "value": float(value), "level": level,
        "n": int(row["n"]), "distribution": dist,
        "percentile_rank": round(rank, 1),
        "status": "비교군_부족" if insufficient else "비교가능",
        "statement": "비교군 %d건 중 약 %.0f%%가 이 값 이하다." % (int(row["n"]), rank),
    }


# ------------------------------------------------------------ Phase B
def phase_b(d):
    """cohort median baseline vs ML. 못 이기면 못 이겼다고 적는다."""
    from lightgbm import LGBMRegressor
    from catboost import CatBoostRegressor
    from sklearn.model_selection import GroupKFold

    t = d[d["per_recipient"].notna() & (d["per_recipient"] > 0)].copy()
    t["y"] = np.log10(t["per_recipient"])
    # cohort 는 넣지 않는다. 신규 사업을 조회하는 시점에는 그 사업이 어느 수집
    # 경로에서 왔는지가 존재하지 않는 정보다 — 넣으면 검증 점수만 부풀려진다.
    cats = ["support_type", "support_method", "support_unit", "category_large",
            "industry_grp", "agency_type", "amount_type"]
    nums = ["support_count", "support_ratio", "project_duration"]
    for c in cats:
        t[c] = t[c].fillna("미기재").astype("category")
    X = t[cats + nums]
    y = t["y"].to_numpy()
    # 재공고·동일사업이 train/test 로 갈라지면 성능이 부풀려진다 (전처리 원칙 1).
    groups = t["program_stem"].fillna(t["title"]).astype(str).to_numpy()

    gkf = GroupKFold(n_splits=5)
    preds = {k: np.zeros(len(t)) for k in
             ["전체중앙값", "코호트중앙값(baseline)", "LightGBM", "CatBoost", "LGBM-quantile50"]}
    for tr, te in gkf.split(X, y, groups):
        Xtr, Xte, ytr = X.iloc[tr], X.iloc[te], y[tr]
        preds["전체중앙값"][te] = np.median(ytr)

        # baseline: 지원성격x지원방식 중앙값, 없으면 성격, 없으면 전체
        key = ["support_type", "support_method"]
        m2 = pd.Series(ytr, index=Xtr.index).groupby(
            [Xtr[k] for k in key], observed=True).median()
        m1 = pd.Series(ytr, index=Xtr.index).groupby(
            Xtr["support_type"], observed=True).median()
        base = []
        for _, r in Xte.iterrows():
            v = m2.get((r["support_type"], r["support_method"]), np.nan)
            if pd.isna(v):
                v = m1.get(r["support_type"], np.nan)
            base.append(np.median(ytr) if pd.isna(v) else v)
        preds["코호트중앙값(baseline)"][te] = base

        preds["LightGBM"][te] = LGBMRegressor(
            n_estimators=400, learning_rate=0.05, num_leaves=15, min_child_samples=10,
            random_state=42, verbose=-1).fit(Xtr, ytr).predict(Xte)
        preds["LGBM-quantile50"][te] = LGBMRegressor(
            objective="quantile", alpha=0.5, n_estimators=400, learning_rate=0.05,
            num_leaves=15, min_child_samples=10, random_state=42,
            verbose=-1).fit(Xtr, ytr).predict(Xte)
        cb = CatBoostRegressor(iterations=400, depth=5, learning_rate=0.05, verbose=0,
                               random_seed=42, allow_writing_files=False,
                               cat_features=cats)
        Xtr_c, Xte_c = Xtr.copy(), Xte.copy()
        for c in cats:
            Xtr_c[c] = Xtr_c[c].astype(str)
            Xte_c[c] = Xte_c[c].astype(str)
        preds["CatBoost"][te] = cb.fit(Xtr_c, ytr).predict(Xte_c)

    out = {}
    for k, p in preds.items():
        err = np.abs(p - y)
        out[k] = {
            "MAE_log10": round(float(err.mean()), 4),
            "RMSE_log10": round(float(np.sqrt(((p - y) ** 2).mean())), 4),
            "geo_mean_error_x": round(float(10 ** err.mean()), 3),
            "within_2x": round(float((err <= np.log10(2)).mean()), 4),
            "within_3x": round(float((err <= np.log10(3)).mean()), 4),
        }
    base = out["코호트중앙값(baseline)"]["MAE_log10"]
    best = min((k for k in out if k not in ("전체중앙값", "코호트중앙값(baseline)")),
               key=lambda k: out[k]["MAE_log10"])
    imp = (base - out[best]["MAE_log10"]) / base
    return {"n": int(len(t)), "target": "log10(기업당 지원액)", "cv": "GroupKFold(5) by program_stem",
            "features": cats + nums, "results": out, "baseline_MAE": base,
            "best_ml": best, "best_ml_MAE": out[best]["MAE_log10"],
            "improvement": round(float(imp), 4),
            "min_required": MIN_IMPROVEMENT,
            "adopt": bool(imp >= MIN_IMPROVEMENT)}


def won(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    for unit, mult in (("조원", 1e12), ("억원", 1e8), ("만원", 1e4)):
        if abs(v) >= mult:
            return "%.1f%s" % (v / mult, unit)
    return "%.0f원" % v


def main():
    d = prepare(pd.read_parquet(SRC))
    print("모델 3 대상: %d건 (지원성격 확정 행)" % len(d))

    ref = build_reference(d)
    ref.to_parquet(OUT, index=False)
    print("[data] %s  참고분포 %d행" % (OUT, len(ref)))

    print("\n== 축별 비교군 (사다리 단계별 30건 이상 칸 수)")
    for axis, cfg in AXES.items():
        a = ref[ref["axis"] == axis]
        print("  %-18s %s"
              % (axis, " / ".join("%s %d" % (name, int(((a["level"] == name)
                                                        & (a["n"] >= MIN_COHORT)).sum()))
                                  for name, _ in _specs(cfg["unit_split"]))))

    print("\n== 기업당 지원액 비교군 상위 (성격x방식x단위)")
    top = ref[(ref["axis"] == "per_recipient") & (ref["level"] == "성격x방식x단위")
              & (ref["n"] >= MIN_COHORT)].sort_values("n", ascending=False)
    split = ref[(ref["axis"] == "per_recipient") & (ref["level"] == "성격x방식x단위x출처")
                & (ref["n"] >= MIN_COHORT)].sort_values("n", ascending=False)
    for _, r in top.iterrows():
        print("  %-10s %-10s %-8s n=%4d  P50 %-10s P90 %-10s  출처차이 %s"
              % (r["support_type"], r["support_method"], r["support_unit"], r["n"],
                 won(r["p50"]), won(r["p90"]), r["source_divergence"]))

    # 실제 조회 예시 — 설계서의 출력 형태를 그대로 만든다
    demo = demo_case(ref, d)
    print("\n== 조회 예시")
    print("  대상: %s" % demo["title"])
    for a in demo["axes"]:
        if a["status"] == "비교가능":
            print("   %-16s 값 %-10s → P%.0f (비교군 %d건, %s 기준)"
                  % (a["axis"], won(a["value"]) if a["axis"] == "per_recipient"
                     else "%.4g" % a["value"], a["percentile_rank"], a["n"], a["level"]))
        else:
            print("   %-16s %s (%s)" % (a["axis"], a["status"], a.get("reason", "")))

    print("\n== Phase B — ML 이 cohort median 을 이기는가")
    b = phase_b(d)
    for k, v in sorted(b["results"].items(), key=lambda kv: kv[1]["MAE_log10"]):
        print("  %-24s MAE %.4f  배수오차 %.2fx  2배이내 %.1f%%"
              % (k, v["MAE_log10"], v["geo_mean_error_x"], v["within_2x"] * 100))
    print("  baseline %.4f -> %s %.4f (개선 %.1f%%, 기준 %.0f%%) => %s"
          % (b["baseline_MAE"], b["best_ml"], b["best_ml_MAE"], b["improvement"] * 100,
             MIN_IMPROVEMENT * 100, "채택" if b["adopt"] else "미채택"))

    print("\n== 출처까지 분리한 비교군 (30건 이상) — 섞지 않고 각각")
    for _, r in split.iterrows():
        print("  %-10s %-10s %-8s %-9s n=%4d  P50 %-10s P90 %s"
              % (r["support_type"], r["support_method"], r["support_unit"],
                 r["ref_cohort"], r["n"], won(r["p50"]), won(r["p90"])))

    verdict = ("Go" if len(top) >= 5 else "Conditional")
    print("\n== 판정: %s (Phase A 채택, Phase B %s)"
          % (verdict, "채택" if b["adopt"] else "미채택 — 통계 기준선 유지"))

    C.save_report("m12_m3_cohort.json", {
        "n_rows": int(len(d)), "min_cohort": MIN_COHORT, "axes": list(AXES),
        "reference_rows": int(len(ref)),
        "cohorts_ge30": {a: {name: int(((ref["axis"] == a) & (ref["level"] == name)
                                        & (ref["n"] >= MIN_COHORT)).sum())
                             for name, _ in _specs(cfg["unit_split"])}
                         for a, cfg in AXES.items()},
        "cohorts_ge30_source_split": int(len(split)),
        "demo": demo, "phase_b": b, "verdict": verdict,
    })
    write_md(ref, top, split, demo, b, verdict)


def demo_case(ref, d):
    """비교군이 두꺼운 실제 사업 하나를 골라 4축 조회를 재현한다."""
    cand = d[d["per_recipient"].notna() & d["support_count"].notna()
             & (d["cohort"] == "taxonomy")]
    r = cand.sort_values("per_recipient", ascending=False).iloc[0]
    axes = [compare(ref, a, r[a], r["support_type"], r["support_method"],
                    r["support_unit"], r["cohort"]) for a in AXES]
    return {"row_id": r["row_id"], "title": r["title"],
            "support_type": r["support_type"], "support_method": r["support_method"],
            "axes": axes}


def write_md(ref, top, split, demo, b, verdict):
    L = ["# 모델 3 — 기존 사업 대비 지원규모 상대 비교", "",
         "> 판단하지 않는 것: 지원규모 적정/부적정, 예산 과다/과소, 삭감/증액 필요",
         "> 제공하는 것: P10~P90, percentile rank, 비교군 수, 후퇴 여부", "",
         "## 1. 축을 나눈 이유", "",
         "지원규모는 하나의 숫자가 아니다. 축마다 결측률이 달라 어떤 축은 비교가 되고",
         "어떤 축은 안 된다. 축 단위로 '비교군 부족'을 말할 수 있어야 한다.", "",
         "| 축 | 30건 이상 2단 비교군 | 30건 이상 1단 비교군 | 전체 n |", "|---|---:|---:|---:|"]
    for axis in AXES:
        a = ref[ref["axis"] == axis]
        tot = a[a["level"] == "전체"]
        L.append("| %s | %d | %d | %s |"
                 % (axis, int(((a["level"] == "성격x방식") & (a["n"] >= MIN_COHORT)).sum()),
                    int(((a["level"] == "성격") & (a["n"] >= MIN_COHORT)).sum()),
                    int(tot["n"].iloc[0]) if len(tot) else 0))

    L += ["", "## 2. 기업당 지원액 참고분포 (지원성격 × 지원방식 × 지원단위, 30건 이상)", "",
          "| 지원성격 | 지원방식 | 단위 | n | P10 | P25 | P50 | P75 | P90 | 코호트 구성 | 출처간 중앙값 배수 |",
          "|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|"]
    for _, r in top.iterrows():
        L.append("| %s | %s | %s | %d | %s | %s | %s | %s | %s | %s | %s |"
                 % (r["support_type"], r["support_method"], r["support_unit"], r["n"],
                    won(r["p10"]), won(r["p25"]), won(r["p50"]), won(r["p75"]),
                    won(r["p90"]), r["cohort_mix"], r["source_divergence"] or "—"))
    L += ["",
          "> 마지막 열이 2를 크게 넘으면 taxonomy(2023 중앙부처)와 bizinfo(공고 원문)가",
          "> 서로 다른 모집단이라는 뜻이다. 섞은 채로 낸 percentile 은 두 모집단의",
          "> 평균을 가리켜 어느 쪽과 비교한 것인지 말할 수 없다. 그래서 출처를 분리한",
          "> 비교군을 1순위로 두고, 30건을 못 채울 때만 위 표로 후퇴한다.", "",
          "### 출처를 분리한 비교군 (1순위)", "",
          "| 지원성격 | 지원방식 | 단위 | 출처 | n | P50 | P90 |",
          "|---|---|---|---|---:|---:|---:|"]
    for _, r in split.iterrows():
        L.append("| %s | %s | %s | %s | %d | %s | %s |"
                 % (r["support_type"], r["support_method"], r["support_unit"],
                    r["ref_cohort"], r["n"], won(r["p50"]), won(r["p90"])))
    L += [""]

    L += ["## 3. 조회 예시 — 출력 형태", "",
          "대상: **%s** (%s / %s)" % (demo["title"], demo["support_type"],
                                    demo["support_method"]), "",
          "| 축 | 값 | 비교군 기준 | 비교군 수 | P50 | P90 | percentile |",
          "|---|---:|---|---:|---:|---:|---:|"]
    for a in demo["axes"]:
        if a["status"] in ("비교가능", "비교군_부족"):
            v = won(a["value"]) if a["axis"] == "per_recipient" else "%.4g" % a["value"]
            L.append("| %s | %s | %s | %d | %s | %s | **P%.0f** |"
                     % (a["axis"], v, a["level"], a["n"],
                        won(a["distribution"]["p50"]) if a["axis"] == "per_recipient"
                        else "%.4g" % a["distribution"]["p50"],
                        won(a["distribution"]["p90"]) if a["axis"] == "per_recipient"
                        else "%.4g" % a["distribution"]["p90"],
                        a["percentile_rank"]))
        else:
            L.append("| %s | — | — | — | — | — | %s |" % (a["axis"], a["status"]))
    L += ["", "허용되는 문장:", "",
          "> 신규 사업의 기업당 지원액은 비교군 %d건 중 약 %.0f%%보다 높다."
          % (demo["axes"][0].get("n", 0), demo["axes"][0].get("percentile_rank", 0)), "",
          "금지되는 문장:", "", "> 지원금이 과도하다.", ""]

    L += ["## 4. Phase B — ML 이 cohort median 을 이기는가", "",
          "타깃 %s / 검증 %s" % (b["target"], b["cv"]), "",
          "| 모델 | MAE(log10) | 배수 오차 | 2배 이내 | 3배 이내 |",
          "|---|---:|---:|---:|---:|"]
    for k, v in sorted(b["results"].items(), key=lambda kv: kv[1]["MAE_log10"]):
        L.append("| %s | %.4f | %.2fx | %.1f%% | %.1f%% |"
                 % (k, v["MAE_log10"], v["geo_mean_error_x"], v["within_2x"] * 100,
                    v["within_3x"] * 100))
    L += ["",
          "baseline %.4f → %s %.4f = **개선 %.1f%%** (채택 기준 %.0f%%) → **%s**"
          % (b["baseline_MAE"], b["best_ml"], b["best_ml_MAE"], b["improvement"] * 100,
             b["min_required"] * 100, "채택" if b["adopt"] else "미채택"), "",
          "## 5. 판정", "", "**%s**" % verdict, "",
          "- Phase A(통계 기반 percentile) 채택",
          "- Phase B(ML) %s" % ("채택" if b["adopt"]
                                else "미채택 — 통계 기준선을 유지한다"), ""]
    p = os.path.join(C.REPORTS, "m12_m3_cohort.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
