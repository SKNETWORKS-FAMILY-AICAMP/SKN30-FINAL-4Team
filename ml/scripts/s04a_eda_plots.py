"""S04A — 전처리 검증용 EDA 시각화.

지금까지 발견한 데이터 결함들(PDF 표 셀 병합, "1조 4,517억원" 파싱 오류)은
전부 수치를 파고들다 뒤늦게 나왔다. 분포를 한 번 그려봤으면 훨씬 일찍
잡혔을 문제들이다. 그래서 전처리 결과를 눈으로 확인하는 그림을 남긴다.

그리는 것
  1. 금액 분포 (log10 박스플롯)  — 이상치가 어디에 있는지, SANE_RANGE 가 타당한지
  2. 결측 현황                    — 어떤 컬럼이 왜 비어 있는지(구조적 결측 구분)
  3. 출처·확장자별 관측 구성      — PDF 제외의 영향
  4. 월별 관측 추이               — 수집 설계 차이(2026년 급증)가 보이는지
  5. 클래스 분포 (모델 1)         — 꼬리 클래스가 얼마나 얇은지

축 제목은 영문으로 쓰되, 클래스명 같은 데이터 값은 한글 그대로 둔다.
한글 폰트가 있으면 자동으로 잡고, 없으면 영문만 정상 표시된다.
"""
import os
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from common import PROC, FIGURES, SANE_RANGE, save_report

OBS = PROC + "/support_amount_observations.parquet"
TAX = PROC + "/business_taxonomy.parquet"

TYPES = ["per_company", "per_project", "total_budget", "periodic"]
def _set_korean_font():
    """설치된 한글 폰트를 찾아 지정한다. 없으면 조용히 넘어간다."""
    import matplotlib.font_manager as fm
    have = {f.name for f in fm.fontManager.ttflist}
    for name in ("Malgun Gothic", "NanumGothic", "AppleGothic", "Gulim"):
        if name in have:
            plt.rcParams["font.family"] = name
            return name
    return None


KFONT = _set_korean_font()
plt.rcParams.update({"figure.dpi": 110, "font.size": 9, "axes.grid": True,
                     "grid.alpha": 0.3, "axes.axisbelow": True,
                     "axes.unicode_minus": False})


def plot_amount_distribution(obs, path):
    """금액 분포와 SANE_RANGE 경계. 이상치가 어디 있는지 한눈에 본다."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))

    # (1) 타입별 log10 박스플롯 — 이상치 포함 상태
    data, labels = [], []
    for t in TYPES:
        v = obs.loc[obs["amount_type"] == t, "amount_max"].dropna()
        v = v[v > 0]
        if len(v):
            data.append(np.log10(v))
            labels.append(f"{t}\n(n={len(v)})")
    bp = axes[0].boxplot(data, labels=labels, showfliers=True,
                         flierprops=dict(marker="o", markersize=2.5,
                                         markerfacecolor="crimson", alpha=.45,
                                         markeredgecolor="none"))
    for t, x in zip(TYPES, range(1, len(TYPES) + 1)):
        lo, hi = SANE_RANGE[t]
        axes[0].hlines([np.log10(lo), np.log10(hi)], x - .35, x + .35,
                       colors="steelblue", linestyles="--", lw=1.2)
    axes[0].set_ylabel("log10(amount, KRW)")
    axes[0].set_title("Amount distribution with SANE_RANGE bounds (dashed)")
    for y, lab in [(4, "10K"), (6, "1M"), (8, "100M"), (10, "10B"), (12, "1T")]:
        axes[0].axhline(y, color="gray", lw=.4, alpha=.5)
        axes[0].text(len(TYPES) + .55, y, lab, va="center", fontsize=7, color="gray")

    # (2) 이상치 플래그 건수
    if "is_outlier" in obs.columns:
        cnt = (obs[obs["is_outlier"]].groupby("amount_type").size()
               .reindex(TYPES, fill_value=0))
        tot = obs.groupby("amount_type").size().reindex(TYPES, fill_value=0)
        x = np.arange(len(TYPES))
        axes[1].bar(x, tot, color="lightsteelblue", label="kept")
        axes[1].bar(x, cnt, color="crimson", label="flagged as outlier")
        for i, (c, t) in enumerate(zip(cnt, tot)):
            if c:
                axes[1].text(i, t + 12, f"{c} ({c/t*100:.1f}%)",
                             ha="center", fontsize=7.5, color="crimson")
        axes[1].set_xticks(x)
        axes[1].set_xticklabels(TYPES, rotation=15)
        axes[1].set_ylabel("observations")
        axes[1].set_title("Outliers flagged by SANE_RANGE")
        axes[1].legend(fontsize=8)

    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def plot_missing_and_source(obs, path):
    """결측 현황 + 출처 구성. 구조적 결측인지 아닌지 구분해서 본다."""
    fig, axes = plt.subplots(1, 3, figsize=(13, 4))

    cols = ["amount_max", "amount_min", "amount_type", "large_category",
            "support_type", "support_type_status", "support_ratio", "support_count"]
    cols = [c for c in cols if c in obs.columns]
    rate = obs[cols].isna().mean().sort_values()
    colors = ["crimson" if r > .5 else "steelblue" for r in rate]
    axes[0].barh(range(len(rate)), rate.values, color=colors)
    axes[0].set_yticks(range(len(rate)))
    axes[0].set_yticklabels(rate.index, fontsize=8)
    axes[0].set_xlim(0, 1)
    axes[0].set_xlabel("missing rate")
    axes[0].set_title("Missing by column\n(red = structural, do not impute)")
    for i, v in enumerate(rate.values):
        axes[0].text(min(v + .02, .9), i, f"{v*100:.0f}%", va="center", fontsize=7.5)

    s = obs["source"].value_counts()
    axes[1].pie(s.values, labels=[f"{k}\n{v:,}" for k, v in s.items()],
                autopct="%1.1f%%", startangle=90,
                colors=["#8fb8de", "#f2b880"], textprops={"fontsize": 8})
    axes[1].set_title("Observations by source")

    ts = obs["text_source"].value_counts()
    axes[2].bar(range(len(ts)), ts.values, color="#8fb8de")
    axes[2].set_xticks(range(len(ts)))
    axes[2].set_xticklabels(ts.index, rotation=15, fontsize=8)
    axes[2].set_ylabel("observations")
    axes[2].set_title("By text source\n(PDF excluded → summary fallback)")
    for i, v in enumerate(ts.values):
        axes[2].text(i, v + 15, f"{v:,}", ha="center", fontsize=7.5)

    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def plot_monthly_coverage(obs, path):
    """월별 관측 추이. 2026년 급증은 정책 변화가 아니라 수집 설계 차이다."""
    fig, ax = plt.subplots(figsize=(11, 3.8))
    g = obs.groupby(["ym", "source"]).size().unstack(fill_value=0).sort_index()
    idx = pd.PeriodIndex(g.index, freq="M").to_timestamp()
    bottom = np.zeros(len(g))
    for col, c in zip(g.columns, ["#8fb8de", "#f2b880"]):
        ax.bar(idx, g[col].values, bottom=bottom, width=22, label=col, color=c)
        bottom += g[col].values
    ax.set_ylabel("observations / month")
    ax.set_title("Monthly observation coverage — the 2026 jump is a collection-design "
                 "artifact, not a policy trend")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def plot_class_distribution(path):
    """모델 1 클래스 분포. 꼬리 클래스가 얼마나 얇은지 보여준다."""
    from s05a_m1_ml import MIN_SUPPORT, coarsen
    t = pd.read_parquet(TAX)
    t["support_type"] = t["middle_category"].map(coarsen)
    vc = t["support_type"].dropna().value_counts()

    fig, ax = plt.subplots(figsize=(11, 4))
    colors = ["crimson" if v < MIN_SUPPORT else
              ("#f2b880" if v <= 5 else "#8fb8de") for v in vc.values]
    ax.bar(range(len(vc)), vc.values, color=colors)
    ax.axhline(MIN_SUPPORT, color="crimson", ls="--", lw=1,
               label=f"MIN_SUPPORT = {MIN_SUPPORT} (below → dropped)")
    ax.set_xticks(range(len(vc)))
    ax.set_xticklabels(vc.index, rotation=60, ha="right", fontsize=7.5)
    ax.set_ylabel("samples")
    ax.set_yscale("log")
    ax.set_title("Model 1 class distribution (log scale) — "
                 "long tail is why Macro F1 lags Accuracy")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return vc


def main():
    os.makedirs(FIGURES, exist_ok=True)
    obs = pd.read_parquet(OBS)
    out = {}

    p = FIGURES + "/eda_amount_distribution.png"
    plot_amount_distribution(obs, p); out["amount_distribution"] = p
    print("[figure]", p)

    p = FIGURES + "/eda_missing_and_source.png"
    plot_missing_and_source(obs, p); out["missing_and_source"] = p
    print("[figure]", p)

    p = FIGURES + "/eda_monthly_coverage.png"
    plot_monthly_coverage(obs, p); out["monthly_coverage"] = p
    print("[figure]", p)

    p = FIGURES + "/eda_class_distribution.png"
    vc = plot_class_distribution(p); out["class_distribution"] = p
    print("[figure]", p)

    save_report("s04a_eda_plots.json", {
        "purpose": "전처리 결과를 눈으로 검증하는 EDA 그림. 수치만 보다 놓친 결함을 "
                   "분포로 확인한다(PDF 셀 병합·1조 4,517억 파싱 오류가 그런 사례였다).",
        "korean_font": KFONT,
        "observations": int(len(obs)),
        "outliers_flagged": int(obs["is_outlier"].sum()) if "is_outlier" in obs else None,
        "missing_rate": obs.isna().mean().round(4).to_dict(),
        "class_min": int(vc.min()), "class_max": int(vc.max()),
        "classes_below_3": int((vc < 3).sum()),
        "figures": out,
    })


if __name__ == "__main__":
    main()
