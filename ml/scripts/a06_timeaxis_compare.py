"""A06 — 시간축 비교: 등록일 기준 vs 마감일 기준.

지금까지 모든 시계열은 registered_date(공고 등록일) 기준이었다.
그런데 하순에 등록된 공고의 89%는 다음 달 이후에 마감된다.
"1월 31일 등록 / 3월 마감" 공고는 1월 사업인가 3월 사업인가?

시간축을 바꾸면 계절성·예측성능이 달라지는지 같은 조건에서 측정한다.

절단(censoring) 처리 — 마감일 축에만 필요하다.
  원본은 2013-01-02 ~ 2025-03-27에 "등록된" 공고만 담는다. 따라서
  - 왼쪽: 2012년에 등록되고 2013년에 마감된 공고가 데이터에 없다 → 2013 초반 과소집계
  - 오른쪽: 2025-06에 등록되고 2025-12에 마감될 공고가 아직 없다 → 후반 과소집계
  리드타임 분포에서 안전 구간을 잡아 양끝을 잘라낸다.

등록일 축은 원본 수집 범위와 일치하므로 이 문제가 없다.
"""
import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from common import PROC, CORE8, save_report
from a01_stl_eda import diagnose, verdict
from m02_forecast import FEATS, FOLDS, ml_models, score, fit_sarima

MASTER = f"{PROC}/announcement_master.parquet"

AXES = {
    "registered": ("registered_date", "등록일"),
    "deadline": ("application_end", "마감일"),
}


def lead_time_bounds(m, q=0.99):
    """리드타임 q분위로 마감일 축의 안전 구간을 정한다."""
    lead = (m["application_end"] - m["registered_date"]).dt.days
    lead = lead[lead >= 0]
    cut = int(lead.quantile(q))
    lo = (m["registered_date"].min() + pd.Timedelta(days=cut)).to_period("M")
    hi = (m["registered_date"].max() - pd.Timedelta(days=cut)).to_period("M")
    return lo, hi, cut, lead


def build_ts(m, date_col, lo=None, hi=None):
    """월 × 분야 집계 + 인과 피처. p04와 동일한 누수 통제(shift(1) 이후 rolling)."""
    d = m.dropna(subset=[date_col, "category_large"]).copy()
    d["ym"] = d[date_col].dt.to_period("M")
    if lo is not None:
        d = d[d["ym"] >= lo]
    if hi is not None:
        d = d[d["ym"] <= hi]

    cats = [c for c in CORE8 if c in set(d["category_large"].astype(str))]
    full = pd.period_range(d["ym"].min(), d["ym"].max(), freq="M")
    grid = pd.MultiIndex.from_product([full, cats], names=["ym", "category"])

    cnt = d.groupby(["ym", "category_large"], observed=True).size().rename("announcement_count")
    ts = cnt.reindex(grid, fill_value=0).reset_index()
    ts["month"] = ts["ym"].dt.month
    ts = ts.sort_values(["category", "ym"]).reset_index(drop=True)

    g = ts.groupby("category", observed=True)["announcement_count"]
    for k in (1, 2, 3, 6, 12):
        ts[f"lag_{k}"] = g.shift(k)
    prev = g.shift(1)
    for w in (3, 6, 12):
        ts[f"rolling_mean_{w}"] = (prev.groupby(ts["category"], observed=True)
                                   .rolling(w, min_periods=w).mean()
                                   .reset_index(level=0, drop=True))
    ts["rolling_std_12"] = (prev.groupby(ts["category"], observed=True)
                            .rolling(12, min_periods=12).std()
                            .reset_index(level=0, drop=True))
    ts["yoy_diff"] = ts["lag_1"] - ts["lag_12"]
    ts["quarter"] = ts["ym"].dt.quarter
    ts["month_sin"] = np.sin(2 * np.pi * ts["month"] / 12)
    ts["month_cos"] = np.cos(2 * np.pi * ts["month"] / 12)
    ts["ym_str"] = ts["ym"].astype(str)
    ts["date"] = ts["ym"].dt.to_timestamp()
    ts["target_next"] = g.shift(-1)
    return ts.drop(columns=["ym"]).rename(columns={"ym_str": "ym"}), cats


def stl_all(ts, cats):
    """전체 + 분야별 STL 진단."""
    out = {}
    tot = ts.groupby("ym")["announcement_count"].sum()
    tot.index = pd.PeriodIndex(tot.index, freq="M").to_timestamp()
    out["전체"] = diagnose(tot)[0]          # diagnose는 (진단dict, STL결과) 반환
    for c in cats:
        s = ts[ts["category"] == c].set_index("ym")["announcement_count"]
        s.index = pd.PeriodIndex(s.index, freq="M").to_timestamp()
        out[c] = diagnose(s)[0]
    return out


def quarter_folds(lo, hi, start="2020-Q1"):
    """분기 단위 walk-forward fold 목록.

    연 단위는 마감일 축이 2024-04까지라 fold가 2개뿐이다. 분기로 쪼개면
    같은 구간에서 fold를 늘려 두 축을 대등하게 비교할 수 있고,
    fold별 편차(표준편차)도 볼 수 있다.
    """
    qs = pd.period_range(pd.Period(start, "Q"), pd.Period(hi, "M").asfreq("Q"), freq="Q")
    out = []
    for q in qs:
        months = pd.period_range(q.start_time, q.end_time, freq="M")
        if months[-1] > pd.Period(hi, "M"):      # 부분 분기 제외
            continue
        cut = (months[0] - 1).strftime("%Y-%m")
        out.append((cut, [m.strftime("%Y-%m") for m in months]))
    return out


def forecast(ts, cats, seed=42, skip_slow=False, folds=FOLDS, return_store=False):
    """m02와 동일한 walk-forward. fold는 두 축이 공통으로 커버하는 것만 쓴다.

    folds 원소: (train_cut, valyr_str) 또는 (train_cut, [val_month, ...])
    """
    t = ts.dropna(subset=FEATS).sort_values(["category", "ym"]).reset_index(drop=True)
    results, store = {}, {}

    for cut, valspec in folds:
        tr = t[t["ym"] <= cut]
        if isinstance(valspec, str):
            va = t[t["ym"].str.startswith(valspec)]
            valyr = valspec
        else:
            va = t[t["ym"].isin(valspec)]
            valyr = valspec[0][:7] + "~" + valspec[-1][-2:]
        if va.empty or len(tr) < 50:
            continue
        y, base = va["announcement_count"].values, va["lag_1"].values

        for name, col in [("Last Value", "lag_1"), ("Seasonal Naive", "lag_12"),
                          ("Moving Average(3)", "rolling_mean_3")]:
            p = va[col].values
            results.setdefault(name, {})[valyr] = score(y, p, base)
            store.setdefault(name, []).append((valyr, y, p, base))

        Xtr = pd.get_dummies(tr[FEATS + ["category"]], columns=["category"])
        Xva = pd.get_dummies(va[FEATS + ["category"]], columns=["category"])
        Xva = Xva.reindex(columns=Xtr.columns, fill_value=0)
        ytr = tr["announcement_count"].values
        for name, mdl in ml_models(seed).items():
            mdl.fit(Xtr, ytr)
            p = np.asarray(mdl.predict(Xva), float)
            results.setdefault(name, {})[valyr] = score(y, p, base)
            store.setdefault(name, []).append((valyr, y, p, base))

        if not skip_slow:
            parts = []
            for c in cats:
                trc = tr[tr["category"] == c].sort_values("ym")
                vac = va[va["category"] == c].sort_values("ym")
                if vac.empty:
                    continue
                if len(trc) < 30:
                    parts.append(pd.Series(np.repeat(trc["announcement_count"].mean(), len(vac)),
                                           index=vac.index))
                    continue
                try:
                    f = fit_sarima(trc["announcement_count"].values, len(vac))
                except Exception:
                    f = np.repeat(trc["announcement_count"].mean(), len(vac))
                parts.append(pd.Series(np.asarray(f, float), index=vac.index))
            p = pd.concat(parts).reindex(va.index).values
            results.setdefault("SARIMA", {})[valyr] = score(y, p, base)
            store.setdefault("SARIMA", []).append((valyr, y, p, base))

    members = [m for m in ("XGBoost", "LightGBM", "CatBoost", "SARIMA", "Seasonal Naive")
               if m in store]
    if len(members) >= 2:
        for key in dict.fromkeys(x[0] for x in store[members[0]]):
            rows = {m: next((x for x in store[m] if x[0] == key), None) for m in members}
            if any(v is None for v in rows.values()):
                continue
            y, base = rows[members[0]][1], rows[members[0]][3]
            p = np.mean([rows[m][2] for m in members], axis=0)
            results.setdefault("Ensemble", {})[key] = score(y, p, base)
            store.setdefault("Ensemble", []).append((key, y, p, base))

    out = {}
    for n, fd in results.items():
        if not fd:
            continue
        e = {}
        for k in ("MAE", "RMSE", "sMAPE", "direction_acc"):
            v = [f[k] for f in fd.values()]
            e[k] = round(float(np.mean(v)), 3)
            e[k + "_std"] = round(float(np.std(v, ddof=1)) if len(v) > 1 else 0.0, 3)
        e["n_folds"] = len(fd)
        e["per_fold_MAE"] = {k: round(v["MAE"], 2) for k, v in fd.items()}
        out[n] = e
    return (out, store) if return_store else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-slow", action="store_true")
    ap.add_argument("--granularity", choices=["year", "quarter"], default="quarter",
                    help="fold 단위. 연 단위는 마감일 축이 2024-04까지라 2개뿐이다")
    ap.add_argument("--q-start", default="2020-Q1", help="분기 fold 시작")
    args = ap.parse_args()

    m = pd.read_parquet(MASTER, columns=["registered_date", "application_end", "category_large"])
    m["registered_date"] = pd.to_datetime(m["registered_date"])
    m["application_end"] = pd.to_datetime(m["application_end"], errors="coerce")

    lo, hi, cut, lead = lead_time_bounds(m)
    print("리드타임(마감일-등록일): 중앙값 %d일 / p99 %d일" % (lead.median(), cut))
    print("마감일 축 안전 구간: %s ~ %s\n" % (lo, hi))

    # 경계 넘김 실태
    same = (m["application_end"].dt.to_period("M") == m["registered_date"].dt.to_period("M"))
    dom = m["registered_date"].dt.day
    boundary = {
        "cross_month_rate": round(float((~same).mean()), 4),
        "cross_rate_late_registered": round(float((~same)[dom > 20].mean()), 4),
        "cross_rate_early_registered": round(float((~same)[dom <= 10].mean()), 4),
        "lead_days_median": float(lead.median()),
        "lead_days_p99": cut,
    }
    print("등록월 != 마감월: %.1f%% (하순 등록분 %.1f%%)" % (
        boundary["cross_month_rate"] * 100, boundary["cross_rate_late_registered"] * 100))

    # 두 축을 같은 기간으로 맞춘다. 마감일 축의 안전 구간이 더 좁으므로 그쪽에 맞춘다.
    # 기간이 다르면 계절성·MAE 차이가 시간축 때문인지 기간 때문인지 구분되지 않는다.
    if args.granularity == "quarter":
        use_folds = quarter_folds(lo, hi, args.q_start)
        label_folds = [f"{v[0]}~{v[-1][-2:]}" for _, v in use_folds]
    else:
        use_folds = [(c, v) for c, v in FOLDS
                     if pd.Period(f"{v}-12", "M") <= hi and pd.Period(f"{v}-01", "M") >= lo]
        label_folds = [v for _, v in use_folds]
    print("공통 기간 %s ~ %s / %s fold %d개: %s" % (
        lo, hi, args.granularity, len(use_folds), label_folds))

    out = {}
    for key, (col, label) in AXES.items():
        ts, cats = build_ts(m, col, lo, hi)     # 두 축 모두 동일 구간
        print("\n=== %s 기준 · %d분야 × %d개월 (%s~%s) ===" % (
            label, len(cats), ts["ym"].nunique(), ts["ym"].min(), ts["ym"].max()))

        stl = stl_all(ts, cats)
        print("  %-6s %8s %8s %8s %8s" % ("분야", "평균", "추세", "계절성", "잔차비"))
        for k, v in stl.items():
            print("  %-6s %8.1f %8.4f %8.4f %8.3f" % (
                k, v["mean"], v["trend_strength"], v["seasonal_strength"],
                v["residual_var_ratio"]))

        fc = forecast(ts, cats, args.seed, args.skip_slow, use_folds)
        print("  --- 예측 (walk-forward %d-fold) ---" % len(use_folds))
        for n, v in sorted(fc.items(), key=lambda x: x[1]["MAE"]):
            print("    %-20s MAE %6.2f ±%5.2f   sMAPE %6.2f ±%5.2f" % (
                n, v["MAE"], v["MAE_std"], v["sMAPE"], v["sMAPE_std"]))

        monthly = ts.groupby("month")["announcement_count"].sum()
        out[key] = {
            "label": label, "date_column": col,
            "months": int(ts["ym"].nunique()),
            "period": [ts["ym"].min(), ts["ym"].max()],
            "total_count": int(ts["announcement_count"].sum()),
            "monthly_total": {int(k): int(v) for k, v in monthly.items()},
            "stl": stl, "forecast": fc,
            "verdict": {k: verdict(v) for k, v in stl.items()},
        }

    a = pd.Series(out["registered"]["monthly_total"]).sort_index()
    b = pd.Series(out["deadline"]["monthly_total"]).sort_index()
    corr = float(np.corrcoef(a.values, b.values)[0, 1])
    print("\n=== 월별 분포 상관 (등록일 vs 마감일): %.3f ===" % corr)

    save_report("a06_timeaxis_compare.json", {
        "purpose": "시간축(등록일 vs 마감일) 선택이 계절성·예측성능에 미치는 영향 측정",
        "boundary_effect": boundary,
        "deadline_censoring": {
            "safe_range": [str(lo), str(hi)],
            "reason": "원본은 등록 시점 기준 수집이라 마감일 축은 양끝이 절단된다",
            "cut_days_p99": cut,
            "note": "두 축을 이 구간으로 통일해 비교했다. 기간이 다르면 "
                    "차이가 시간축 때문인지 기간 때문인지 구분되지 않는다.",
        },
        "granularity": args.granularity,
        "folds_used": label_folds,
        "monthly_distribution_corr": round(corr, 4),
        "axes": out,
    })


if __name__ == "__main__":
    main()
