"""S08B — 모델 3 딥러닝: 지원규모 quantile 회귀 MLP + Ablation.

기준선 (S08A, 같은 데이터·같은 분할)
    전체 중앙값                  MAE_log10 0.8600
    코호트 중앙값 (baseline)      MAE_log10 0.6790   <- 이걸 이겨야 한다
    LGBM-quantile50 (ML 채택)    MAE_log10 0.5160   <- 딥러닝이 넘어야 할 실제 상대

왜 딥러닝이 불리한 조건인지 먼저 밝힌다
    표본이 2,234행이고 feature 가 10개다. 사전학습 전이가 없는 MLP 에게
    2천 행은 적다. 모델 1에서는 KLUE-RoBERTa 가 한국어 사전지식을 이미 갖고
    있어 900건으로도 이겼지만, 여기엔 그런 전이가 없다.
    그래도 같은 잣대(GroupKFold, 같은 baseline)로 정직하게 재고 못 이기면
    못 이겼다고 보고한다 — A04/A07 에서 지켜온 규율과 같다.

왜 quantile 인가
    모델 3의 출력은 점추정이 아니라 P10~P90 분포다. 평균제곱오차로 학습하면
    분포가 아니라 조건부 평균 하나만 나와 서비스 출력과 형태가 맞지 않는다.
    pinball loss 로 3개 분위(0.1/0.5/0.9)를 동시에 학습한다.

Ablation 축 (기존 S06F 방식과 동일 — one-factor-at-a-time)
    hidden        32 / 64 / 128 / 256
    layers        1 / 2 / 3
    dropout       0.0 / 0.1 / 0.3
    lr            3e-4 / 1e-3 / 3e-3
    epochs        100 / 300 / 600
    batch         32 / 128 / full

주의 — 선택 편향
    ablation 수치는 하이퍼파라미터 선택에 쓰인 CV 성능이라 그대로 일반화
    성능으로 제시하면 안 된다. 기준 설정(튜닝 전) 수치가 편향 없는 비교다.
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.model_selection import GroupKFold
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from s08a_m3_cohort import AXES, MIN_IMPROVEMENT, prepare

SRC = os.path.join(C.PROC, "design_features.parquet")
SEED = 42
QUANTILES = [0.1, 0.5, 0.9]

CATS = ["support_type", "support_method", "support_unit", "category_large",
        "industry_grp", "agency_type", "amount_type"]
NUMS = ["support_count", "support_ratio", "project_duration"]

BASE = {"hidden": 64, "layers": 2, "dropout": 0.1, "lr": 1e-3,
        "epochs": 300, "batch": 128}
GRID = {
    "hidden": [32, 64, 128, 256],
    "layers": [1, 2, 3],
    "dropout": [0.0, 0.1, 0.3],
    "lr": [3e-4, 1e-3, 3e-3],
    "epochs": [100, 300, 600],
    "batch": [32, 128, 0],          # 0 = full batch
}


def build_xy(d):
    t = d[d["per_recipient"].notna() & (d["per_recipient"] > 0)].copy()
    t["y"] = np.log10(t["per_recipient"])
    # 결측을 '미기재' 범주로 명시한다. 0으로 채우면 모델이 실제 0과 구분하지 못한다.
    for c in CATS:
        t[c] = t[c].fillna("미기재").astype(str)
    X = pd.get_dummies(t[CATS], columns=CATS, dtype=float)
    for c in NUMS:
        X[c] = t[c].astype(float)
        X[c + "__missing"] = t[c].isna().astype(float)
    groups = t["program_stem"].fillna(t["title"]).astype(str).to_numpy()
    return t, X, t["y"].to_numpy(), groups


class QuantileMLP(nn.Module):
    """분위 3개를 한 번에 내는 머리. 분위끼리 교차하지 않도록 증분으로 쌓는다."""

    def __init__(self, n_in, hidden, layers, dropout):
        super().__init__()
        blocks, d = [], n_in
        for _ in range(layers):
            blocks += [nn.Linear(d, hidden), nn.ReLU(), nn.Dropout(dropout)]
            d = hidden
        self.body = nn.Sequential(*blocks)
        self.head = nn.Linear(d, len(QUANTILES))

    def forward(self, x):
        raw = self.head(self.body(x))
        # P10 <= P50 <= P90 을 구조로 보장한다. 후처리 정렬보다 학습이 안정된다.
        base = raw[:, :1]
        steps = torch.nn.functional.softplus(raw[:, 1:])
        return torch.cat([base, base + steps.cumsum(dim=1)], dim=1)


def pinball(pred, y):
    q = torch.tensor(QUANTILES, device=pred.device).view(1, -1)
    e = y.view(-1, 1) - pred
    return torch.maximum(q * e, (q - 1) * e).mean()


def fit_predict(Xtr, ytr, Xte, cfg, seed=SEED):
    torch.manual_seed(seed)
    # 결측 대치는 fold train 중앙값으로만 한다. 전체 중앙값을 쓰면 검증 fold 의
    # 분포가 학습에 새어 든다(S04B 에서 잡았던 누수와 같은 종류다).
    # 대치했다는 사실은 `__missing` 지시자가 이미 별도 축으로 들고 있다.
    med = np.nanmedian(Xtr, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    Xtr = np.where(np.isnan(Xtr), med, Xtr)
    Xte = np.where(np.isnan(Xte), med, Xte)

    sc = StandardScaler().fit(Xtr)
    xt = torch.tensor(sc.transform(Xtr), dtype=torch.float32)
    yt = torch.tensor(ytr, dtype=torch.float32)
    xe = torch.tensor(sc.transform(Xte), dtype=torch.float32)

    m = QuantileMLP(xt.shape[1], cfg["hidden"], cfg["layers"], cfg["dropout"])
    opt = torch.optim.Adam(m.parameters(), lr=cfg["lr"])
    bs = cfg["batch"] or len(xt)
    m.train()
    for _ in range(cfg["epochs"]):
        perm = torch.randperm(len(xt))
        for i in range(0, len(xt), bs):
            idx = perm[i:i + bs]
            opt.zero_grad()
            pinball(m(xt[idx]), yt[idx]).backward()
            opt.step()
    m.eval()
    with torch.no_grad():
        return m(xe).numpy()


def cv_score(X, y, groups, cfg):
    Xa = X.to_numpy(dtype=float)
    p50 = np.zeros(len(y))
    cover = np.zeros(len(y), dtype=bool)
    for tr, te in GroupKFold(n_splits=5).split(Xa, y, groups):
        pred = fit_predict(Xa[tr], y[tr], Xa[te], cfg)
        p50[te] = pred[:, 1]
        cover[te] = (y[te] >= pred[:, 0]) & (y[te] <= pred[:, 2])
    err = np.abs(p50 - y)
    return {
        "MAE_log10": round(float(err.mean()), 4),
        "RMSE_log10": round(float(np.sqrt(((p50 - y) ** 2).mean())), 4),
        "geo_mean_error_x": round(float(10 ** err.mean()), 3),
        "within_2x": round(float((err <= np.log10(2)).mean()), 4),
        "within_3x": round(float((err <= np.log10(3)).mean()), 4),
        # P10~P90 구간이 실제로 80%를 덮는가. 분포 출력의 신뢰도 그 자체다.
        "p10_p90_coverage": round(float(cover.mean()), 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="축을 줄여 빠르게 확인")
    a = ap.parse_args()

    d = prepare(pd.read_parquet(SRC))
    t, X, y, groups = build_xy(d)
    print("모델 3 DL 대상: %d행 / feature %d개 / 그룹 %d개"
          % (len(t), X.shape[1], len(set(groups))))
    print("기준선(S08A): 코호트중앙값 0.6790 / LGBM-quantile50 0.5160")

    t0 = time.time()
    base = cv_score(X, y, groups, BASE)
    print("\n== 기준 설정 (튜닝 전, 편향 없는 비교)")
    print("  %s" % BASE)
    print("  MAE %.4f  배수오차 %.2fx  2배이내 %.1f%%  P10~P90 포함률 %.1f%%"
          % (base["MAE_log10"], base["geo_mean_error_x"],
             base["within_2x"] * 100, base["p10_p90_coverage"] * 100))

    grid = GRID if not a.smoke else {"hidden": [32, 128], "epochs": [100, 300]}
    print("\n== Ablation (one-factor-at-a-time, 기준 설정에서 한 축씩만 변경)")
    abl, best_cfg = {}, dict(BASE)
    for axis, values in grid.items():
        abl[axis] = {}
        for v in values:
            if v == BASE[axis]:
                abl[axis][str(v)] = dict(base, note="기준 설정")
                continue
            cfg = dict(BASE)
            cfg[axis] = v
            abl[axis][str(v)] = cv_score(X, y, groups, cfg)
        best_v = min(abl[axis], key=lambda k: abl[axis][k]["MAE_log10"])
        best_cfg[axis] = type(BASE[axis])(float(best_v)) if "." in best_v else int(best_v)
        print("  %-9s %s   -> 최적 %s"
              % (axis, "  ".join("%s:%.4f" % (k, v["MAE_log10"])
                                 for k, v in abl[axis].items()), best_v))

    print("\n== 축별 최적값 조합")
    print("  %s" % best_cfg)
    tuned = cv_score(X, y, groups, best_cfg)
    print("  MAE %.4f  배수오차 %.2fx  2배이내 %.1f%%  P10~P90 포함률 %.1f%%"
          % (tuned["MAE_log10"], tuned["geo_mean_error_x"],
             tuned["within_2x"] * 100, tuned["p10_p90_coverage"] * 100))

    ml_base, ml_best = 0.6790, 0.5160
    imp_base = (ml_base - tuned["MAE_log10"]) / ml_base
    imp_ml = (ml_best - tuned["MAE_log10"]) / ml_best
    verdict = ("채택" if imp_ml >= MIN_IMPROVEMENT else
               "미채택 — ML(LGBM-quantile)을 넘지 못함")
    print("\n== 판정")
    print("  cohort median 대비 %+.1f%% / LGBM-quantile 대비 %+.1f%% (기준 %.0f%%) => %s"
          % (imp_base * 100, imp_ml * 100, MIN_IMPROVEMENT * 100, verdict))
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    C.save_report("s08b_m3_cohort_dl.json", {
        "n_rows": int(len(t)), "n_features": int(X.shape[1]),
        "cv": "GroupKFold(5) by program_stem", "quantiles": QUANTILES,
        "base_config": BASE, "base_result": base,
        "ablation": abl, "tuned_config": best_cfg, "tuned_result": tuned,
        "ml_reference": {"cohort_median": ml_base, "lgbm_quantile50": ml_best},
        "improvement_vs_cohort_median": round(float(imp_base), 4),
        "improvement_vs_ml_best": round(float(imp_ml), 4),
        "min_required": MIN_IMPROVEMENT, "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2),
    })


if __name__ == "__main__":
    main()
