"""DL14 — 모델 2 딥러닝 후보: FT-Transformer 를 LightGBM 과 같은 분할에서 비교.

계획서 3.3/6절 Priority 3.

    FT-Transformer -> Q50 중심 예측 -> LGBM 동일 split 비교 -> MAE/안정성 비교
    -> 유의미한 개선 없으면 LGBM 유지

모델 2의 서비스 질문은 "이 사업의 지원규모가 과거 유사사업 대비 어느 위치인가"다.
적정 금액을 맞히는 것이 아니라 상대 위치를 주는 것이므로, 여기서 보는 것은
**중앙값 예측의 안정성**이다(계획서 3.4). 구간 폭 최적화는 하지 않는다.

같은 것
    데이터        support_amount_observations -> m12 prepare(), per_recipient>0
    타깃          log10(기업/과제당 지원액)
    분할          GroupKFold(5) by program_stem — 셔플 없는 결정적 분할이라
                  두 모델이 글자 그대로 같은 fold 를 본다
    feature       M17 이 고른 "+자부담·추출신뢰도" 조합 그대로
    지표          MAE(log10) / MedAE / 배수오차 / 2배 이내 / fold σ

다른 것
    트리 부스팅 대신 범주 임베딩 + 셀프어텐션.

FT-Transformer 구현 메모
    · 범주형은 임베딩, 수치형은 축마다 학습되는 선형사영(feature tokenizer)으로
      같은 차원의 토큰이 된다. 여기에 [CLS] 토큰을 붙여 인코더에 넣고 [CLS]
      출력으로 회귀한다 — Gorishniy et al. 의 구조 그대로다.
    · 수치 결측은 학습 fold 중앙값으로 채우고 결측 지시자를 축으로 남긴다.
      M16 과 같은 이유다 — 지시자가 없으면 결측 행이 중앙값 근처로 보인다.
    · 표준화 통계·범주 사전·중앙값은 전부 **fold train 에서만** 만든다.
      전체에서 만들면 fold 밖 정보가 새어 MAE 가 낙관적으로 나온다.
    · L1(MAE) 손실로 학습한다. 평가 지표가 MAE 인데 MSE 로 학습하면 이상치가
      끌고 가 같은 자를 쓰지 않게 된다.
    · 시드 3개 평균±표준편차. 트리는 시드 하나로도 거의 안 흔들리지만 신경망은
      흔들린다 — 계획서 5절이 DL 에 3~5시드를 요구한 이유다.
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
import torch
import torch.nn as nn
from lightgbm import LGBMRegressor
from sklearn.model_selection import GroupKFold

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m12_m3_cohort import SRC, prepare
from m17_m3_tuning import FEATURE_SETS, build

SEED = 42
SEEDS = [42, 7, 2024]
N_SPLITS = 5
FEATSET = "+자부담·추출신뢰도"      # M17 이 고른 조합
BASELINE_COHORT = 0.6790            # 코호트 중앙값
BASELINE_LGBM = 0.5028              # M17 튜닝 후 LightGBM
LGBM_PARAMS = {"num_leaves": 63, "max_depth": 8, "min_child_samples": 5,
               "learning_rate": 0.02, "n_estimators": 800,
               "colsample_bytree": 0.6, "subsample": 1.0,
               "reg_alpha": 0.0, "reg_lambda": 1.0}

D_TOKEN = 32
N_BLOCKS = 3
N_HEADS = 4
DROPOUT = 0.1
EPOCHS = 200
BATCH = 128
LR = 1e-3
WD = 1e-5


class FTTransformer(nn.Module):
    def __init__(self, n_num, cardinalities, d=D_TOKEN, blocks=N_BLOCKS,
                 heads=N_HEADS, dropout=DROPOUT):
        super().__init__()
        # 수치 축마다 자기 가중치·자기 편향을 갖는 토큰화
        self.num_w = nn.Parameter(torch.randn(n_num, d) * 0.02)
        self.num_b = nn.Parameter(torch.zeros(n_num, d))
        self.emb = nn.ModuleList([nn.Embedding(c, d) for c in cardinalities])
        self.cls = nn.Parameter(torch.randn(1, 1, d) * 0.02)
        layer = nn.TransformerEncoderLayer(
            d_model=d, nhead=heads, dim_feedforward=d * 2, dropout=dropout,
            activation="gelu", batch_first=True, norm_first=True)
        self.enc = nn.TransformerEncoder(layer, num_layers=blocks)
        self.head = nn.Sequential(nn.LayerNorm(d), nn.GELU(), nn.Linear(d, 1))

    def forward(self, xnum, xcat):
        toks = [xnum.unsqueeze(-1) * self.num_w + self.num_b]
        for i, e in enumerate(self.emb):
            toks.append(e(xcat[:, i]).unsqueeze(1))
        t = torch.cat([self.cls.expand(xnum.shape[0], -1, -1)] + toks, dim=1)
        return self.head(self.enc(t)[:, 0]).squeeze(-1)


def encode_fold(Xtr, Xte, cats, nums):
    """범주 사전·중앙값·표준화를 전부 학습 fold 에서만 만든다."""
    cat_tr, cat_te, cards = [], [], []
    for c in cats:
        vocab = {v: i + 1 for i, v in enumerate(sorted(map(str, Xtr[c].unique())))}
        cat_tr.append(Xtr[c].astype(str).map(vocab).fillna(0).to_numpy())
        cat_te.append(Xte[c].astype(str).map(vocab).fillna(0).to_numpy())
        cards.append(len(vocab) + 1)          # 0 = 학습에 없던 범주
    num_tr, num_te = [], []
    for c in nums:
        med = Xtr[c].median()
        med = 0.0 if pd.isna(med) else med
        a, b = Xtr[c].fillna(med).to_numpy(float), Xte[c].fillna(med).to_numpy(float)
        mu, sd = a.mean(), a.std()
        sd = sd if sd > 1e-9 else 1.0
        num_tr += [(a - mu) / sd, Xtr[c].isna().to_numpy(float)]
        num_te += [(b - mu) / sd, Xte[c].isna().to_numpy(float)]
    return (np.column_stack(num_tr), np.column_stack(cat_tr).astype(np.int64),
            np.column_stack(num_te), np.column_stack(cat_te).astype(np.int64), cards)


def fit_fold(ntr, ctr, ytr, nte, cte, cards, seed):
    torch.manual_seed(seed)
    np.random.seed(seed)
    net = FTTransformer(ntr.shape[1], cards)
    opt = torch.optim.AdamW(net.parameters(), lr=LR, weight_decay=WD)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS)
    # 타깃도 학습 fold 통계로 표준화한다. log10 금액은 평균이 7.5 근처라
    # 0 에서 출발하는 신경망이 그 상수를 따라가는 데만 수백 epoch 를 쓴다.
    ym, ys = float(np.mean(ytr)), float(np.std(ytr))
    ys = ys if ys > 1e-9 else 1.0
    ytr = (ytr - ym) / ys
    Xn = torch.tensor(ntr, dtype=torch.float32)
    Xc = torch.tensor(ctr)
    Y = torch.tensor(ytr, dtype=torch.float32)
    lossf = nn.L1Loss()
    n = len(Y)
    net.train()
    for _ in range(EPOCHS):
        perm = torch.randperm(n)
        for i in range(0, n, BATCH):
            idx = perm[i:i + BATCH]
            loss = lossf(net(Xn[idx], Xc[idx]), Y[idx])
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            opt.step()
        sched.step()
    net.eval()
    with torch.no_grad():
        p = net(torch.tensor(nte, dtype=torch.float32), torch.tensor(cte)).numpy()
    return p * ys + ym


def metrics(pred, y, fold_mae):
    err = np.abs(pred - y)
    return {
        "MAE_log10": round(float(err.mean()), 4),
        "MedAE_log10": round(float(np.median(err)), 4),
        "RMSE_log10": round(float(np.sqrt(((pred - y) ** 2).mean())), 4),
        "geo_mean_error_x": round(float(10 ** err.mean()), 3),
        "within_2x": round(float((err <= np.log10(2)).mean()), 4),
        "fold_mae": [round(v, 4) for v in fold_mae],
        "fold_std": round(float(np.std(fold_mae)), 4),
    }


def main():
    global EPOCHS
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    args = ap.parse_args()
    EPOCHS = args.epochs
    seeds = [int(s) for s in args.seeds.split(",")]

    d = prepare(pd.read_parquet(SRC))
    t, X, y, groups, cats = build(d, FEATURE_SETS[FEATSET])
    nums = [c for c in X.columns if c not in cats]
    splits = list(GroupKFold(n_splits=N_SPLITS).split(X, y, groups))
    print("n=%d / 범주 %d축 / 수치 %d축 / GroupKFold(%d) by program_stem"
          % (len(y), len(cats), len(nums), N_SPLITS), flush=True)

    t0 = time.time()

    # LightGBM — 같은 fold 인덱스에서 다시 잰다(M17 수치 재현 확인 포함)
    lp = np.zeros(len(y))
    lf = []
    for tr, te in splits:
        m = LGBMRegressor(random_state=SEED, verbose=-1, **LGBM_PARAMS)
        m.fit(X.iloc[tr], y[tr])
        lp[te] = m.predict(X.iloc[te])
        lf.append(float(np.abs(lp[te] - y[te]).mean()))
    lgbm = metrics(lp, y, lf)
    print("LightGBM  MAE %.4f (M17 보고값 %.4f)" % (lgbm["MAE_log10"], BASELINE_LGBM),
          flush=True)

    # 코호트 중앙값 baseline — fold train 안에서만 중앙값을 낸다
    cp = np.zeros(len(y))
    cf = []
    key = t["support_type"].astype(str) + "|" + t["support_method"].astype(str)
    for tr, te in splits:
        med = pd.Series(y[tr]).groupby(key.iloc[tr].to_numpy()).median()
        gm = float(np.median(y[tr]))
        cp[te] = key.iloc[te].map(med).fillna(gm).to_numpy()
        cf.append(float(np.abs(cp[te] - y[te]).mean()))
    cohort = metrics(cp, y, cf)
    print("코호트중앙값 MAE %.4f" % cohort["MAE_log10"], flush=True)

    # FT-Transformer — 시드별로 전체 CV 를 다시 돈다
    per_seed = []
    for s in seeds:
        pred = np.zeros(len(y))
        fm = []
        for tr, te in splits:
            ntr, ctr, nte, cte, cards = encode_fold(X.iloc[tr], X.iloc[te], cats, nums)
            pred[te] = fit_fold(ntr, ctr, y[tr], nte, cte, cards, s)
            fm.append(float(np.abs(pred[te] - y[te]).mean()))
        m = metrics(pred, y, fm)
        per_seed.append({"seed": s, **m})
        print("FT-Transformer seed %-5d MAE %.4f  2배이내 %.1f%%  fold σ %.4f  [%.0fs]"
              % (s, m["MAE_log10"], m["within_2x"] * 100, m["fold_std"],
                 time.time() - t0), flush=True)

    maes = [p["MAE_log10"] for p in per_seed]
    ft = {"MAE_log10_mean": round(float(np.mean(maes)), 4),
          "MAE_log10_std": round(float(np.std(maes)), 4),
          "MAE_log10_best": round(float(np.min(maes)), 4),
          "MedAE_log10_mean": round(float(np.mean([p["MedAE_log10"] for p in per_seed])), 4),
          "within_2x_mean": round(float(np.mean([p["within_2x"] for p in per_seed])), 4),
          "fold_std_mean": round(float(np.mean([p["fold_std"] for p in per_seed])), 4)}

    diff = ft["MAE_log10_mean"] - lgbm["MAE_log10"]
    # "유의미한 개선"의 기준을 미리 못박는다 — 결과를 보고 정하면 기준이 아니다.
    # LightGBM 의 fold 간 표준편차(0.013 수준)보다 작은 차이는 분할 흔들림과
    # 구별되지 않는다. 그 폭을 넘어야 개선이라고 부른다.
    margin = lgbm["fold_std"]
    if diff < -margin:
        verdict = "FT-Transformer 채택 — LGBM 대비 fold σ 이상 개선"
    elif diff > margin:
        verdict = "LGBM 유지 — FT-Transformer 가 fold σ 이상 더 나쁘다"
    else:
        verdict = "LGBM 유지 — 차이가 fold 흔들림(σ %.4f) 안이라 개선이라 부를 수 없다" % margin

    report = {
        "질문": "이 사업의 지원규모가 과거 유사사업 대비 어느 위치에 있는가",
        "n": int(len(y)), "n_groups": int(len(set(groups))),
        "cv": "GroupKFold(%d) by program_stem — 셔플 없는 결정적 분할" % N_SPLITS,
        "feature_set": FEATSET, "cat_features": cats, "num_features": nums,
        "target": "log10(기업/과제당 지원액)",
        "ft_transformer": {
            "arch": "d_token %d / blocks %d / heads %d / dropout %.1f / L1 loss / epochs %d"
                    % (D_TOKEN, N_BLOCKS, N_HEADS, DROPOUT, EPOCHS),
            "seeds": seeds, "per_seed": per_seed, "summary": ft},
        "lightgbm": {"params": LGBM_PARAMS, **lgbm},
        "cohort_median_baseline": cohort,
        "comparison": {
            "ft_minus_lgbm_MAE": round(float(diff), 4),
            "significance_margin_fold_std": margin,
            "ft_improvement_vs_cohort_pct": round(
                (cohort["MAE_log10"] - ft["MAE_log10_mean"]) / cohort["MAE_log10"] * 100, 1),
            "lgbm_improvement_vs_cohort_pct": round(
                (cohort["MAE_log10"] - lgbm["MAE_log10"]) / cohort["MAE_log10"] * 100, 1),
            "verdict": verdict,
        },
        "runtime_min": round((time.time() - t0) / 60, 1),
    }
    C.save_report("dl14_m2_ft_transformer.json", report)
    write_md(report)
    print("\n%s" % verdict)


def write_md(r):
    c = r["comparison"]
    ft, lg, co = r["ft_transformer"], r["lightgbm"], r["cohort_median_baseline"]
    L = ["# DL14 — 모델 2 FT-Transformer vs LightGBM (동일 분할)", "",
         "> 계획서 3.5: 현재 LGBM MAE 0.5028 보다 유의미하게 좋아지지 않으면 LGBM 유지.",
         "",
         "```text",
         "n=%d / 그룹 %d / %s" % (r["n"], r["n_groups"], r["cv"]),
         "타깃 %s / feature %s" % (r["target"], r["feature_set"]),
         ft["arch"],
         "```", "",
         "두 모델이 **글자 그대로 같은 fold 인덱스**를 봅니다. GroupKFold 는 셔플이",
         "없어 결정적이고, 같은 실행 안에서 한 번 만든 분할을 양쪽에 넘겼습니다.", "",
         "## 1. 결과", "",
         "| 모델 | MAE(log10) | MedAE | 배수오차 | 2배 이내 | fold σ |",
         "|---|---:|---:|---:|---:|---:|",
         "| FT-Transformer (시드 %d개 평균) | **%.4f ± %.4f** | %.4f | — | %.1f%% | %.4f |"
         % (len(ft["seeds"]), ft["summary"]["MAE_log10_mean"],
            ft["summary"]["MAE_log10_std"], ft["summary"]["MedAE_log10_mean"],
            ft["summary"]["within_2x_mean"] * 100, ft["summary"]["fold_std_mean"]),
         "| LightGBM (M17 튜닝) | **%.4f** | %.4f | %.2fx | %.1f%% | %.4f |"
         % (lg["MAE_log10"], lg["MedAE_log10"], lg["geo_mean_error_x"],
            lg["within_2x"] * 100, lg["fold_std"]),
         "| 코호트 중앙값 (baseline) | %.4f | %.4f | %.2fx | %.1f%% | %.4f |"
         % (co["MAE_log10"], co["MedAE_log10"], co["geo_mean_error_x"],
            co["within_2x"] * 100, co["fold_std"]),
         "", "## 2. 시드별 FT-Transformer", "",
         "| 시드 | MAE | MedAE | 2배 이내 | fold σ |", "|---:|---:|---:|---:|---:|"]
    for p in ft["per_seed"]:
        L.append("| %d | %.4f | %.4f | %.1f%% | %.4f |"
                 % (p["seed"], p["MAE_log10"], p["MedAE_log10"],
                    p["within_2x"] * 100, p["fold_std"]))
    L += ["", "## 3. 판정", "",
          "| 항목 | 값 |", "|---|---:|",
          "| FT − LGBM (MAE) | %+.4f |" % c["ft_minus_lgbm_MAE"],
          "| 유의미 판정선 (LGBM fold σ) | %.4f |" % c["significance_margin_fold_std"],
          "| baseline 대비 개선 — FT | %.1f%% |" % c["ft_improvement_vs_cohort_pct"],
          "| baseline 대비 개선 — LGBM | %.1f%% |" % c["lgbm_improvement_vs_cohort_pct"],
          "",
          "**%s**" % c["verdict"], "",
          "판정선을 결과를 보기 전에 못박았습니다 — LightGBM 의 fold 간 표준편차보다",
          "작은 차이는 분할이 흔들린 것과 구별되지 않으므로 개선이라 부르지 않습니다.", ""]
    with open(os.path.join(C.REPORTS, "dl14_m2_ft_transformer.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()
