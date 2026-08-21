"""A06 — 공고량 월별 시계열 집계 + 시계열 구조 진단.

목적
    "언제 어느 분야 공고가 몰리는가"를 월 단위로 만든다. 지원규모(A02~A05)와
    달리 공고량은 세는 값이라 파싱 오류가 없고, 기업마당 목록 97,794건이
    2013년부터 쌓여 있어 시계열이 길다. 지원규모가 시계열 구조를 못 만든 것과
    대조적으로 여기서는 구조가 나오는지부터 확인한다.

원천
    announcement_master.parquet — 기업마당 목록. registered_date 결측 0%.
    2013-01 ~ 2025-03. 마지막 달은 3/27 까지라 부분 달이다.

부분 기간 처리
    수집이 2025-03-27 에 끊겨 2025-03 은 실제보다 적게 잡힌다. 그대로 두면
    "최근 급감"이라는 가짜 추세가 생긴다. 마지막 달은 집계에서 제외한다.

집계 축
    전체(total)와 분야별(category) 두 벌을 만든다. 분야별은 8종이고 각각
    100개월 이상이라 개별 시계열로 다룰 수 있다.

구조 진단
    A03 과 같은 잣대(STL 추세강도·계절성강도)를 쓴다. 지원규모는 4개 유형이
    전부 0.3 에 못 미쳐 예측을 포기했는데, 공고량은 어떤지 같은 기준으로 본다.

"계절성"이라는 이름에 속지 말 것
    STL 이 하는 일은 1년 주기로 반복되는 성분을 분리하는 것뿐이다. 왜 반복되는지는
    전혀 모른다. "6월에 도서관 이용이 는다"가 시험 일정 때문이어도 STL 은 그냥
    계절성이라 부른다. 즉 계절성은 발견이 아니라 명명 규칙이다.

    이 데이터의 반복도 자연적 계절이 아니라 외부 제도가 만드는 리듬이다.
    따라서 진짜 물어야 할 것은 "주기가 있는가"가 아니라 "그 리듬을 만드는 동인이
    앞으로도 유지되는가"다. 동인이 바뀌면 모델은 옛 패턴을 계속 예측한다.

    그래서 네 가지를 확인한다.

      ① 연도 간 순위 일치도 (Kendall W)
         연도별 총량 차이를 없앤 뒤 월 순위를 매겨, 해마다 같은 달이 같은
         자리에 오는지 본다. 잡음이면 순위가 흔들린다.
      ② 월 효과 유의성 (Kruskal-Wallis)
         월별 비중 차이가 표본 변동으로 설명되는 수준인지 검정한다.
      ③ 동인을 지목할 수 있는가
         여기서는 국가재정법상 회계연도(1/1~12/31)다. 예산이 연초에 배정돼
         상반기에 공고가 몰리고, 연말은 예산 소진 + 차년도 예산 미확정으로
         급감한다. 실측도 그렇다 — 상반기 57%/하반기 43%, 12월이 그 해 최하위인
         해가 12년 중 10년, 12월 순위 표준편차 0.39.
      ④ 그 동인이 관측 구간에서 안 바뀌었는가 (드리프트)
         전반기와 후반기의 월 프로파일을 비교한다. 실측 Spearman rho 0.916,
         최대 변화폭 1.3%p. 코로나(2020)에도 상반기 비중이 58.6%->60.6% 로
         거의 안 흔들렸다. 법에 명문화된 동인이라 잘 안 바뀐다.

    ①②가 통과해도 ③을 못 대면 계절성이라 부르지 않고 보류한다.
    ④는 "관측 구간에서 안 바뀌었다"는 뜻이지 "앞으로도 안 바뀐다"가 아니다.
    ASSUMED_DRIVER 에 전제를 적어 두었으니, 예산 편성 관행이 바뀌면 재검증한다.
"""
import argparse
import warnings

import numpy as np
import pandas as pd

from common import PROC, FIGURES, save_report

warnings.filterwarnings("ignore")

MASTER = PROC + "/announcement_master.parquet"
OUT_TOTAL = PROC + "/volume_monthly_total.parquet"
OUT_CAT = PROC + "/volume_monthly_category.parquet"
FIG = FIGURES + "/volume_timeseries.png"

CORE8 = ["경영", "기술", "수출", "내수", "창업", "인력", "금융", "기타"]
MIN_MONTHS = 36

# 이 계절성이 무엇을 전제로 성립하는지 명시해 둔다. 예측을 쓰는 쪽이 "무슨 가정
# 위에 얹힌 숫자인지" 알 수 있어야 하고, 전제가 깨지면 재검증할 지점이 된다.
ASSUMED_DRIVER = {
    "driver": "국가재정법상 회계연도 (1/1~12/31) 와 그에 따른 예산 배정·집행 관행",
    "mechanism": "예산이 연초에 배정돼 상반기에 공고가 몰리고, 연말은 예산 소진과 "
                 "차년도 예산 미확정으로 급감한다",
    "why_stable": "자연적 계절이 아니라 제도가 만드는 리듬이다. 법에 명문화돼 있어 "
                  "관행보다 잘 바뀌지 않는다. 실측으로도 코로나(2020)에 상반기 비중이 "
                  "58.6% -> 60.6% 로 거의 흔들리지 않았다.",
    "invalidated_if": ["회계연도 기준 변경",
                       "추경·이월 관행의 구조적 변화로 연말 공고가 늘어나는 경우",
                       "기업마당 등록 정책 변경(예: 지자체 사업 대량 유입)"],
    "recheck": "드리프트 지표(전·후반기 월 프로파일 Spearman rho)가 0.8 아래로 "
               "떨어지면 계절성 가정을 다시 검증한다.",
}
DRIFT_RHO_MIN = 0.8


def load_monthly():
    m = pd.read_parquet(MASTER)
    d = pd.to_datetime(m["registered_date"], errors="coerce")
    m = m[d.notna()].copy()
    m["date"] = d[d.notna()]
    m["ym"] = m["date"].dt.to_period("M")

    # 마지막 달은 수집이 중간에 끊겨 부분 집계다. 빼지 않으면 가짜 급감이 생긴다.
    last = m["ym"].max()
    cut = m[m["ym"] < last]
    dropped = len(m) - len(cut)
    return cut, last, dropped


def fill_months(df, key=None):
    """공고가 0건인 달도 행으로 남긴다. 빠뜨리면 시계열 간격이 어긋난다."""
    full = pd.period_range(df["ym"].min(), df["ym"].max(), freq="M")
    if key is None:
        s = df.groupby("ym").size().reindex(full, fill_value=0)
        return pd.DataFrame({"ym": full.astype(str), "count": s.values})
    out = []
    for k, g in df.groupby(key, observed=True):
        s = g.groupby("ym").size().reindex(full, fill_value=0)
        out.append(pd.DataFrame({"ym": full.astype(str), key: k, "count": s.values}))
    return pd.concat(out, ignore_index=True)


def stl_strength(series, period=12):
    """A03 과 같은 정의. 추세/계절성이 잔차 대비 얼마나 큰지."""
    from statsmodels.tsa.seasonal import STL
    if len(series) < period * 2 + 1:
        return None, None
    res = STL(series, period=period, robust=True).fit()
    var = lambda x: float(np.var(np.asarray(x)))
    r = var(res.resid)
    trend = max(0.0, 1 - r / max(var(res.trend + res.resid), 1e-12))
    seas = max(0.0, 1 - r / max(var(res.seasonal + res.resid), 1e-12))
    return round(trend, 4), round(seas, 4)


def seasonality_evidence(total):
    """계절성이 제도적 리듬인지 확인한다. 강도 수치만으로는 판단하지 않는다."""
    from scipy import stats
    t = total.copy()
    dt = pd.PeriodIndex(t["ym"], freq="M").to_timestamp()
    t["year"], t["month"] = dt.year, dt.month
    piv = t.pivot_table(index="year", columns="month", values="count",
                        aggfunc="sum").dropna()
    if piv.shape[0] < 3 or piv.shape[1] < 12:
        return None
    share = piv.div(piv.sum(axis=1), axis=0) * 100      # 연도 총량 차이를 없앤다
    rank = share.rank(axis=1, ascending=False)

    n, k = rank.shape
    Rj = rank.sum(axis=0).values
    W = float(12 * ((Rj - Rj.mean()) ** 2).sum() / (n ** 2 * (k ** 3 - k)))
    H, p = stats.kruskal(*[share[m].values for m in range(1, 13)])

    h1 = float(share[[1, 2, 3, 4, 5, 6]].sum(axis=1).mean())
    dec_last = int((rank[12] == 12).sum())

    monthly = {int(m): {"mean_share_pct": round(float(share[m].mean()), 2),
                        "mean_rank": round(float(rank[m].mean()), 2),
                        "rank_std": round(float(rank[m].std()), 2)}
               for m in range(1, 13)}

    # ④ 드리프트 — 동인이 관측 구간에서 바뀌었는지. 연도를 반으로 갈라 비교한다.
    mid = int(np.median(share.index))
    early, late = share[share.index <= mid].mean(), share[share.index > mid].mean()
    drift = None
    if len(share[share.index <= mid]) >= 2 and len(share[share.index > mid]) >= 2:
        rho, prho = stats.spearmanr(early.values, late.values)
        diff = (late - early)
        drift = {
            "split_year": mid,
            "spearman_rho": round(float(rho), 4),
            "spearman_p": float(prho),
            "max_abs_change_pct_point": round(float(diff.abs().max()), 2),
            "max_change_month": int(diff.abs().idxmax()),
            "stable": bool(rho >= DRIFT_RHO_MIN),
            "threshold": DRIFT_RHO_MIN,
            "meaning": ("관측 구간에서 동인이 바뀌지 않았다는 뜻이지, 앞으로도 "
                        "안 바뀐다는 뜻이 아니다."),
        }

    return {
        "years": [int(piv.index.min()), int(piv.index.max())], "n_years": int(n),
        "kendall_w": round(W, 4),
        "kruskal_H": round(float(H), 2), "kruskal_p": float(p),
        "first_half_share_pct": round(h1, 2),
        "second_half_share_pct": round(100 - h1, 2),
        "dec_is_lowest_years": dec_last,
        "monthly": monthly,
        "drift": drift,
        "assumed_driver": ASSUMED_DRIVER,
    }


def find_anomalies(total, z=3.0, ratio_lo=0.5, ratio_hi=2.0):
    """같은 달의 다른 해와 비교해 크게 벗어난 달을 찾는다.

    계절성 자체가 크므로 전체 평균이 아니라 '같은 달' 기준으로 본다.
    수집 누락이 계절성으로 오해되는 것을 막는다.

    두 기준을 OR 로 쓴다.
      robust z  분포 대비 얼마나 벗어났나
      비율      같은 달 중앙값의 몇 배인가
    z 만 쓰면 원래 편차가 큰 달의 이상치를 놓친다. 실제로 2016-01(101건)은
    같은 달 중앙값의 13% 인데 1월 자체 MAD 가 커서 z 가 -2.94 로 임계값을
    아슬아슬하게 비껴갔다.
    """
    t = total.copy()
    dt = pd.PeriodIndex(t["ym"], freq="M").to_timestamp()
    t["month"] = dt.month
    g = t.groupby("month")["count"]
    med = g.transform("median")
    mad = g.transform(lambda s: (s - s.median()).abs().median())
    score = (t["count"] - med) / (mad.replace(0, np.nan) * 1.4826)
    ratio = t["count"] / med.replace(0, np.nan)
    hit = (score.abs() >= z) | (ratio < ratio_lo) | (ratio > ratio_hi)
    return [{"ym": r["ym"], "count": int(r["count"]),
             "same_month_median": int(med.loc[i]),
             "ratio_to_median": round(float(ratio.loc[i]), 3),
             "robust_z": round(float(score.loc[i]), 2)}
            for i, r in t[hit.fillna(False)].iterrows()]


def plot(total, cat):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager, rcParams
    for f in ("Malgun Gothic", "AppleGothic", "NanumGothic"):
        if any(f == x.name for x in font_manager.fontManager.ttflist):
            rcParams["font.family"] = f
            break
    rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(2, 1, figsize=(13, 8), sharex=False)
    t = total.copy()
    t["dt"] = pd.PeriodIndex(t["ym"], freq="M").to_timestamp()
    ax[0].plot(t["dt"], t["count"], lw=1.4, color="#2c6fbb")
    ax[0].plot(t["dt"], t["count"].rolling(12, center=True).mean(),
               lw=2.2, color="#d1495b", label="12개월 이동평균")
    ax[0].set_title("월별 공고량 (전체)")
    ax[0].legend(); ax[0].grid(alpha=.3)

    for c in CORE8:
        g = cat[cat["category_large"] == c]
        if g.empty:
            continue
        gd = pd.PeriodIndex(g["ym"], freq="M").to_timestamp()
        ax[1].plot(gd, g["count"].rolling(6).mean(), lw=1.3, label=c)
    ax[1].set_title("분야별 공고량 (6개월 이동평균)")
    ax[1].legend(ncol=4, fontsize=9); ax[1].grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(FIG, dpi=120)
    plt.close(fig)
    print("[figure] %s" % FIG)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", type=int, default=12)
    args = ap.parse_args()

    m, last, dropped = load_monthly()
    print("목록 %d건 | 마지막 부분 달 %s 제외 (%d건)" % (len(m), last, dropped))

    total = fill_months(m)
    cat = fill_months(m[m["category_large"].isin(CORE8)], key="category_large")
    total.to_parquet(OUT_TOTAL, index=False)
    cat.to_parquet(OUT_CAT, index=False)
    print("전체 시계열 %d개월 (%s ~ %s)" % (len(total), total["ym"].iloc[0], total["ym"].iloc[-1]))
    print("분야별 시계열 %d행 (%d분야)" % (len(cat), cat["category_large"].nunique()))
    print()

    # ---- 구조 진단 ----
    print("=== 시계열 구조 (A03 과 같은 잣대) ===")
    print("%-10s%8s%10s%12s%12s%10s" % ("대상", "월수", "월평균", "추세강도", "계절성강도", "조건4"))
    print("-" * 64)
    diag = {}
    rows = [("전체", total["count"].values)]
    for c in CORE8:
        g = cat[cat["category_large"] == c]
        if len(g) >= MIN_MONTHS:
            rows.append((c, g["count"].values))
    for name, v in rows:
        tr, se = stl_strength(pd.Series(v, dtype=float), args.period)
        ok = (len(v) >= MIN_MONTHS) and ((tr or 0) >= 0.3 or (se or 0) >= 0.3)
        diag[name] = {"n_months": int(len(v)), "mean": round(float(np.mean(v)), 1),
                      "trend_strength": tr, "seasonal_strength": se, "passed": bool(ok)}
        print("%-10s%8d%10.1f%12s%12s%10s"
              % (name, len(v), np.mean(v), tr, se, "충족" if ok else "미충족"))

    passed = [k for k, v in diag.items() if v["passed"]]
    print()
    print("조건4 충족: %d/%d — %s" % (len(passed), len(diag), ", ".join(passed) or "없음"))

    # ---- 계절성이 제도적 리듬인지 확인 ----
    ev = seasonality_evidence(total)
    if ev:
        print()
        print("=== 계절성 근거 (강도 수치만으로 인정하지 않는다) ===")
        print("  연도 간 월 순위 일치도 Kendall W : %.3f  (0=무작위, 1=완전일치)"
              % ev["kendall_w"])
        print("  월 효과 유의성 Kruskal-Wallis    : H=%.1f, p=%.3g"
              % (ev["kruskal_H"], ev["kruskal_p"]))
        print("  상반기/하반기 비중               : %.1f%% / %.1f%%"
              % (ev["first_half_share_pct"], ev["second_half_share_pct"]))
        print("  12월이 그 해 최하위인 연도       : %d/%d"
              % (ev["dec_is_lowest_years"], ev["n_years"]))
        print()
        print("  %6s%10s%10s%12s" % ("월", "평균비중", "평균순위", "순위 표준편차"))
        print("  " + "-" * 38)
        for mo, v in ev["monthly"].items():
            print("  %5d월%9.1f%%%10.1f%12.2f"
                  % (mo, v["mean_share_pct"], v["mean_rank"], v["rank_std"]))
        d = ev.get("drift")
        if d:
            print()
            print("  [드리프트] %d년 기준 전·후반 월 프로파일 상관 rho=%.3f — %s"
                  % (d["split_year"], d["spearman_rho"],
                     "안정" if d["stable"] else "불안정(재검증 필요)"))
            print("             최대 변화폭 %.1f%%p (%d월)"
                  % (d["max_abs_change_pct_point"], d["max_change_month"]))
        print()
        print("  전제한 동인: %s" % ASSUMED_DRIVER["driver"])
        print("  → STL 의 '계절성'은 주기를 이름 붙인 것일 뿐 원인을 모른다.")
        print("    이 예측은 위 동인이 유지된다는 가정 위에 있다.")
        print("    깨지는 조건: %s" % " / ".join(ASSUMED_DRIVER["invalidated_if"]))

    anom = find_anomalies(total)
    if anom:
        print()
        print("=== 같은 달 기준 이상치 (계절성이 아니라 수집 문제일 수 있음) ===")
        for a in anom:
            print("  %s  %d건 — 같은 달 중앙값 %d 의 %.0f%% (robust z=%.1f)"
                  % (a["ym"], a["count"], a["same_month_median"],
                     a["ratio_to_median"] * 100, a["robust_z"]))

    try:
        plot(total, cat)
    except Exception as e:
        print("[figure] 생략: %s" % e)

    save_report("a06_volume_timeseries.json", {
        "source": MASTER,
        "rows_used": int(len(m)),
        "partial_month_excluded": str(last),
        "rows_dropped_partial": int(dropped),
        "months": int(len(total)),
        "range": [total["ym"].iloc[0], total["ym"].iloc[-1]],
        "period": args.period,
        "min_months": MIN_MONTHS,
        "condition4_rule": "월수>=36 AND (추세강도>=0.3 OR 계절성강도>=0.3)",
        "diagnostics": diag,
        "condition4_passed": passed,
        "seasonality_evidence": ev,
        "seasonality_rule": ("STL 강도만으로 계절성을 인정하지 않는다. 연도 간 월 순위 "
                             "일치도(Kendall W)·월 효과 유의성(Kruskal-Wallis)·설명 "
                             "가능한 제도적 메커니즘 셋을 함께 본다."),
        "same_month_anomalies": anom,
        "anomaly_note": ("같은 달의 다른 해와 비교해 크게 벗어난 달. 계절성이 아니라 "
                         "수집 누락일 수 있어 따로 표시한다."),
        "note": ("지원규모(A03)는 4개 유형 전부 0.3 미만이라 예측을 포기했다. "
                 "공고량은 세는 값이라 파싱 오류가 없고 시계열이 길어 조건이 다르다."),
        "outputs": [OUT_TOTAL, OUT_CAT, FIG],
    })


if __name__ == "__main__":
    main()
