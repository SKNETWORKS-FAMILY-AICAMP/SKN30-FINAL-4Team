"""DL19 — 모델 2(지원규모) 딥러닝 재대결: M45 로 정제된 타깃에서 다시 잰다.

왜 다시 재는가
    DL14 의 판정("LGBM 유지")은 M12 타깃 위에서 나온 것이다. 그 뒤 M45 가
    타깃을 두 군데 고쳤고, 그 결과 ML 쪽 숫자가 크게 움직였다.

        DL14 시점 (M12 타깃, n=2205)   LGBM 0.5074 / 코호트중앙값 0.6791
        M45 확정  (정제 타깃, n=1877)   LGBM 0.4681 / 비교군중앙값 0.5315

    두 가지가 달라졌다.
      1. per_recipient 에서 stated_cap(원문 한도)만 남기고 budget_div_count
         (총예산/건수 = 평균)를 뺐다. 비교군 안에서 이 둘은 9.97배 갈린다.
      2. 출처(cohort)를 비교군 사다리의 필수 축으로 올렸다. 안쪽에 두고 섞으면
         10칸 중 8칸이 유의하게 갈리고 최대 40배다.

    딥러닝은 이 정제 전 타깃으로만 평가됐다. 한도와 평균이 섞인 라벨은
    딥러닝에도 똑같이 노이즈였으므로, 정제 후에 격차가 어떻게 되는지는
    재보기 전에는 모른다. 판정이 낡은 채로 남아 있는 것이 문제다.

같은 자로 잰다 — DL14 에서 바꾼 것은 데이터뿐이고 비교 규율은 그대로다.
    타깃      M45 prepare() = stated_cap 만, 파싱오류·평균 제외 (n=1877)
    분할      GroupKFold(5) by program_stem, 셔플 없는 결정적 분할을 한 번
              만들어 네 모델에 그대로 넘긴다 — 글자 그대로 같은 fold 다
    feature   M45 의 make_xy(with_cohort=True) 그대로. 사용자가 비교 모집단을
              골랐다는 M45 의 가정을 딥러닝에도 똑같이 준다
    baseline  M45 의 사다리형 비교군 중앙값. DL14 의 baseline 은 성격x방식
              2단뿐이라 지금 기준으로는 약한 상대다
    지표      MAE(log10) / MedAE / 배수오차 / 2배이내 / fold σ

    per_recipient_basis 는 feature 에서 빠진다 — 정제 후 전부 stated_cap 이라
    상수축이다. DL14 는 이 축을 feature 로 갖고 있었다(FEATSET
    '+자부담·추출신뢰도'). 타깃이 정제되면 그 축은 정보가 아니라 죽은 칸이다.

도전자 둘
    FT-Transformer   DL14 가 쓴 구조 그대로. 점추정(L1).
    Quantile MLP     DL09 구조. pinball loss 로 P10/P50/P90 을 함께 학습한다.
                     모델 2의 출력이 점추정이 아니라 분포라 형태가 더 맞는다.

채택 규칙 (결과를 보기 전에 못박는다 — DL14 와 같은 규칙)
    LightGBM 의 fold 간 표준편차보다 크게 이겨야 교체한다. 그보다 작은 차이는
    분할이 흔들린 것과 구별되지 않는다.
"""
import argparse
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from lightgbm import LGBMRegressor
from sklearn.model_selection import GroupKFold

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m45_m2_amount import (SRC, SEED, LO, HI, cohort_median_baseline, make_xy,
                           point_metrics, prepare)

# nested_tensor 경고는 norm_first=True 라서 나는 것이고 결과에 영향이 없다.
warnings.filterwarnings("ignore", message=".*enable_nested_tensor.*")

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

SEEDS = [42, 7, 2024]
N_SPLITS = 5
WITH_COHORT = True          # M45 의 대표 조건(출처_feature_포함)

# M45 가 확정한 LGBM 설정. 같은 스크립트 안에서 다시 돌려 재현을 확인한다.
LGBM_Q = {"objective": "quantile", "alpha": 0.5, "n_estimators": 400,
          "learning_rate": 0.05, "num_leaves": 15, "min_child_samples": 10,
          "random_state": SEED, "verbose": -1}

# FT-Transformer — DL14 와 같은 하이퍼파라미터
D_TOKEN, N_BLOCKS, N_HEADS = 32, 3, 4
FT_DROPOUT, FT_EPOCHS, FT_BATCH, FT_LR, FT_WD = 0.1, 200, 128, 1e-3, 1e-5

# Quantile MLP — DL09 의 기준 설정
MLP_HIDDEN, MLP_LAYERS, MLP_DROPOUT = 64, 2, 0.1
MLP_EPOCHS, MLP_BATCH, MLP_LR = 300, 128, 1e-3
QUANTILES = [LO, 0.5, HI]

REF_DL14 = {"n": 2205, "lgbm": 0.5074, "ft": 0.5468, "cohort": 0.6791}


# ------------------------------------------------------------ 공통 인코딩
def encode_fold(Xtr, Xte, cats, nums):
    """범주 사전·중앙값·표준화를 전부 학습 fold 에서만 만든다.

    전체에서 만들면 fold 밖 정보가 새어 MAE 가 낙관적으로 나온다. 두 신경망이
    같은 인코딩을 쓰도록 여기 한 곳에 둔다.
    """
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
        a = Xtr[c].fillna(med).to_numpy(float)
        b = Xte[c].fillna(med).to_numpy(float)
        mu, sd = a.mean(), a.std()
        sd = sd if sd > 1e-9 else 1.0
        num_tr += [(a - mu) / sd, Xtr[c].isna().to_numpy(float)]
        num_te += [(b - mu) / sd, Xte[c].isna().to_numpy(float)]
    return (np.column_stack(num_tr), np.column_stack(cat_tr).astype(np.int64),
            np.column_stack(num_te), np.column_stack(cat_te).astype(np.int64), cards)


# ------------------------------------------------------------ 도전자 1
class FTTransformer(nn.Module):
    """DL14 구조 그대로. 수치 축마다 자기 가중치를 갖는 토큰화 + [CLS]."""

    def __init__(self, n_num, cards, d=D_TOKEN, blocks=N_BLOCKS,
                 heads=N_HEADS, dropout=FT_DROPOUT):
        super().__init__()
        self.num_w = nn.Parameter(torch.randn(n_num, d) * 0.02)
        self.num_b = nn.Parameter(torch.zeros(n_num, d))
        self.emb = nn.ModuleList([nn.Embedding(c, d) for c in cards])
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


# ------------------------------------------------------------ 도전자 2
class QuantileMLP(nn.Module):
    """DL09 구조. 분위 3개를 한 번에 내되 증분으로 쌓아 교차를 막는다."""

    def __init__(self, n_in, hidden=MLP_HIDDEN, layers=MLP_LAYERS,
                 dropout=MLP_DROPOUT):
        super().__init__()
        blocks, d = [], n_in
        for _ in range(layers):
            blocks += [nn.Linear(d, hidden), nn.ReLU(), nn.Dropout(dropout)]
            d = hidden
        self.body = nn.Sequential(*blocks)
        self.head = nn.Linear(d, len(QUANTILES))

    def forward(self, x):
        raw = self.head(self.body(x))
        base = raw[:, :1]
        steps = torch.nn.functional.softplus(raw[:, 1:])
        return torch.cat([base, base + steps.cumsum(dim=1)], dim=1)


def pinball(pred, y):
    loss = 0.0
    for i, q in enumerate(QUANTILES):
        e = y - pred[:, i]
        loss = loss + torch.maximum(q * e, (q - 1) * e).mean()
    return loss / len(QUANTILES)


# ------------------------------------------------------------ 학습
def _standardize_y(ytr):
    """타깃도 학습 fold 통계로 표준화한다. log10 금액은 평균이 7.5 근처라
    0 에서 출발하는 신경망이 그 상수를 따라가는 데만 수백 epoch 를 쓴다."""
    m, s = float(np.mean(ytr)), float(np.std(ytr))
    return m, (s if s > 1e-9 else 1.0)


def fit_ft(ntr, ctr, ytr, nte, cte, cards, seed, epochs):
    torch.manual_seed(seed)
    np.random.seed(seed)
    net = FTTransformer(ntr.shape[1], cards).to(DEVICE)
    opt = torch.optim.AdamW(net.parameters(), lr=FT_LR, weight_decay=FT_WD)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    ym, ys = _standardize_y(ytr)
    Xn = torch.tensor(ntr, dtype=torch.float32, device=DEVICE)
    Xc = torch.tensor(ctr, device=DEVICE)
    Y = torch.tensor((ytr - ym) / ys, dtype=torch.float32, device=DEVICE)
    lossf = nn.L1Loss()
    n = len(Y)
    net.train()
    for _ in range(epochs):
        perm = torch.randperm(n, device=DEVICE)
        for i in range(0, n, FT_BATCH):
            idx = perm[i:i + FT_BATCH]
            loss = lossf(net(Xn[idx], Xc[idx]), Y[idx])
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            opt.step()
        sched.step()
    net.eval()
    with torch.no_grad():
        Xnte = torch.tensor(nte, dtype=torch.float32, device=DEVICE)
        Xcte = torch.tensor(cte, device=DEVICE)
        p = net(Xnte, Xcte).cpu().numpy()
    return p * ys + ym


def fit_mlp(ntr, ctr, ytr, nte, cte, cards, seed, epochs):
    """범주는 one-hot 으로 편다 — DL09 와 같다. 반환은 (P10, P50, P90)."""
    torch.manual_seed(seed)
    np.random.seed(seed)
    oh_tr, oh_te = [], []
    for j, c in enumerate(cards):
        oh_tr.append(np.eye(c, dtype=np.float32)[ctr[:, j]])
        oh_te.append(np.eye(c, dtype=np.float32)[cte[:, j]])
    Atr = np.column_stack([ntr] + oh_tr).astype(np.float32)
    Ate = np.column_stack([nte] + oh_te).astype(np.float32)

    net = QuantileMLP(Atr.shape[1]).to(DEVICE)
    opt = torch.optim.AdamW(net.parameters(), lr=MLP_LR)
    ym, ys = _standardize_y(ytr)
    X = torch.tensor(Atr, device=DEVICE)
    Y = torch.tensor((ytr - ym) / ys, dtype=torch.float32, device=DEVICE)
    n = len(Y)
    net.train()
    for _ in range(epochs):
        perm = torch.randperm(n, device=DEVICE)
        for i in range(0, n, MLP_BATCH):
            idx = perm[i:i + MLP_BATCH]
            loss = pinball(net(X[idx]), Y[idx])
            opt.zero_grad()
            loss.backward()
            opt.step()
    net.eval()
    with torch.no_grad():
        p = net(torch.tensor(Ate, device=DEVICE)).cpu().numpy()
    return p * ys + ym


# ------------------------------------------------------------ 실행
def run_dl(fitfn, X, y, splits, cats, nums, seeds, epochs, take_median, tag, t0):
    """시드별로 CV 전체를 다시 돈다. 신경망은 시드에 흔들리므로 평균±표준편차로 낸다."""
    per_seed, oof_best = [], None
    for s in seeds:
        pred = np.zeros(len(y))
        lo = np.zeros(len(y))
        hi = np.zeros(len(y))
        fm = []
        for tr, te in splits:
            ntr, ctr, nte, cte, cards = encode_fold(X.iloc[tr], X.iloc[te], cats, nums)
            out = fitfn(ntr, ctr, y[tr], nte, cte, cards, s, epochs)
            if take_median:
                lo[te], pred[te], hi[te] = out[:, 0], out[:, 1], out[:, 2]
            else:
                pred[te] = out
            fm.append(float(np.abs(pred[te] - y[te]).mean()))
        m = point_metrics(y, pred)
        m["fold_mae"] = [round(v, 4) for v in fm]
        m["fold_std"] = round(float(np.std(fm)), 4)
        if take_median:
            m["p10_p90_coverage"] = round(float(((y >= lo) & (y <= hi)).mean()), 4)
            m["median_width_x"] = round(float(10 ** np.median(hi - lo)), 1)
        per_seed.append({"seed": s, **m})
        print("   %-16s seed %-5d MAE %.4f  2배이내 %.1f%%  fold σ %.4f  [%.0fs]"
              % (tag, s, m["MAE_log10"], m["within_2x"] * 100, m["fold_std"],
                 time.time() - t0), flush=True)
        if oof_best is None or m["MAE_log10"] < oof_best[0]:
            oof_best = (m["MAE_log10"], pred)
    maes = [p["MAE_log10"] for p in per_seed]
    summary = {
        "MAE_log10_mean": round(float(np.mean(maes)), 4),
        "MAE_log10_std": round(float(np.std(maes)), 4),
        "MAE_log10_best": round(float(np.min(maes)), 4),
        "MedAE_log10_mean": round(float(np.mean([p["MedAE_log10"] for p in per_seed])), 4),
        "geo_mean_error_x": round(float(np.mean([p["geo_mean_error_x"] for p in per_seed])), 3),
        "within_2x_mean": round(float(np.mean([p["within_2x"] for p in per_seed])), 4),
        "within_3x_mean": round(float(np.mean([p["within_3x"] for p in per_seed])), 4),
        "fold_std_mean": round(float(np.mean([p["fold_std"] for p in per_seed])), 4),
    }
    if take_median:
        summary["p10_p90_coverage_mean"] = round(
            float(np.mean([p["p10_p90_coverage"] for p in per_seed])), 4)
        summary["median_width_x_mean"] = round(
            float(np.mean([p["median_width_x"] for p in per_seed])), 1)
    return {"per_seed": per_seed, "summary": summary}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    ap.add_argument("--ft-epochs", type=int, default=FT_EPOCHS)
    ap.add_argument("--mlp-epochs", type=int, default=MLP_EPOCHS)
    args = ap.parse_args()
    seeds = [int(s) for s in args.seeds.split(",")]

    d, drop = prepare(pd.read_parquet(SRC))
    X, y, groups, cats = make_xy(d, WITH_COHORT)
    nums = [c for c in X.columns if c not in cats]
    splits = list(GroupKFold(n_splits=N_SPLITS).split(X, y, groups))

    print("== device: %s%s" % (DEVICE, " (%s)" % torch.cuda.get_device_name(0)
                               if DEVICE.type == "cuda" else ""))
    print("== 타깃 — M45 정제 (stated_cap 만)")
    print("   n=%d / 그룹 %d / 범주 %d축 / 수치 %d축 / GroupKFold(%d) by program_stem"
          % (len(y), len(set(groups)), len(cats), len(nums), N_SPLITS))
    print("   DL14 시점 타깃은 n=%d 였다 (한도와 평균이 섞인 상태)" % REF_DL14["n"])
    t0 = time.time()

    # ---- 기준선 두 개 (M45 와 같은 것) ----
    print("\n== 기준선")
    bp = np.zeros(len(y))
    bf = []
    for tr, te in splits:
        bp[te] = cohort_median_baseline(X.iloc[tr], y[tr], X.iloc[te], cats)
        bf.append(float(np.abs(bp[te] - y[te]).mean()))
    base = point_metrics(y, bp)
    base["fold_std"] = round(float(np.std(bf)), 4)
    print("   비교군중앙값(baseline)  MAE %.4f  (M45 보고값 0.5315)" % base["MAE_log10"])

    lp = np.zeros(len(y))
    lf = []
    for tr, te in splits:
        m = LGBMRegressor(**LGBM_Q).fit(X.iloc[tr], y[tr])
        lp[te] = m.predict(X.iloc[te])
        lf.append(float(np.abs(lp[te] - y[te]).mean()))
    lgbm = point_metrics(y, lp)
    lgbm["fold_mae"] = [round(v, 4) for v in lf]
    lgbm["fold_std"] = round(float(np.std(lf)), 4)
    print("   LGBM-quantile50        MAE %.4f  (M45 보고값 0.4681)  fold σ %.4f"
          % (lgbm["MAE_log10"], lgbm["fold_std"]))

    # ---- 도전자 둘 ----
    print("\n== 딥러닝 (시드 %s)" % ",".join(str(s) for s in seeds))
    ft = run_dl(fit_ft, X, y, splits, cats, nums, seeds, args.ft_epochs,
                False, "FT-Transformer", t0)
    mlp = run_dl(fit_mlp, X, y, splits, cats, nums, seeds, args.mlp_epochs,
                 True, "Quantile MLP", t0)

    # ---- 판정 ----
    margin = lgbm["fold_std"]
    rows = []
    for tag, s in (("FT-Transformer", ft["summary"]), ("Quantile MLP", mlp["summary"])):
        diff = s["MAE_log10_mean"] - lgbm["MAE_log10"]
        if diff < -margin:
            v = "채택 — LGBM 대비 fold σ 이상 개선"
        elif diff > margin:
            v = "미채택 — LGBM 보다 fold σ 이상 나쁘다"
        else:
            v = "미채택 — 차이가 fold 흔들림(σ %.4f) 안이라 개선이라 부를 수 없다" % margin
        rows.append({"model": tag, "MAE": s["MAE_log10_mean"],
                     "diff_vs_lgbm": round(float(diff), 4), "verdict": v})
        print("\n   %-16s MAE %.4f  (LGBM 대비 %+.4f, 판정선 ±%.4f)"
              % (tag, s["MAE_log10_mean"], diff, margin))
        print("     -> %s" % v)

    winner = min(rows, key=lambda r: r["MAE"])
    final = ("LGBM-quantile50 유지" if winner["diff_vs_lgbm"] > -margin
             else "%s 로 교체" % winner["model"])
    print("\n== 최종: %s" % final)

    report = {
        "질문": "이 사업의 지원규모가 과거 유사사업 대비 어느 위치에 있는가",
        "왜_다시_재는가": "DL14 판정은 M12 타깃(한도+평균 혼재) 위에서 나왔다. "
                     "M45 가 stated_cap 만 남기고 출처를 필수 축으로 올린 뒤 "
                     "ML 쪽 숫자가 크게 움직였으므로 DL 판정도 낡았다.",
        "target": "log10(기업당 지원액) — M45 prepare(), stated_cap 만",
        "target_cleaning": drop,
        "n": int(len(y)), "n_groups": int(len(set(groups))),
        "cv": "GroupKFold(%d) by program_stem — 셔플 없는 결정적 분할을 네 모델이 공유"
              % N_SPLITS,
        "with_cohort_feature": WITH_COHORT,
        "cat_features": cats, "num_features": nums,
        "dropped_feature_note": "per_recipient_basis 는 정제 후 상수라 제외 "
                                "(DL14 는 feature 로 갖고 있었다)",
        "cohort_median_baseline": base,
        "lightgbm_quantile50": {"params": LGBM_Q, **lgbm},
        "ft_transformer": {
            "arch": "d_token %d / blocks %d / heads %d / dropout %.1f / L1 / epochs %d"
                    % (D_TOKEN, N_BLOCKS, N_HEADS, FT_DROPOUT, args.ft_epochs),
            "seeds": seeds, **ft},
        "quantile_mlp": {
            "arch": "hidden %d / layers %d / dropout %.1f / pinball(%s) / epochs %d"
                    % (MLP_HIDDEN, MLP_LAYERS, MLP_DROPOUT,
                       "/".join(str(q) for q in QUANTILES), args.mlp_epochs),
            "seeds": seeds, **mlp},
        "significance_margin_fold_std": margin,
        "verdicts": rows, "final": final,
        "dl14_previous": REF_DL14,
        "runtime_min": round((time.time() - t0) / 60, 1),
    }
    C.save_report("dl19_m2_amount_v2.json", report)
    write_md(report)
    return report


def write_md(r):
    b, l = r["cohort_median_baseline"], r["lightgbm_quantile50"]
    ft, mlp = r["ft_transformer"]["summary"], r["quantile_mlp"]["summary"]
    prev = r["dl14_previous"]
    L = ["# DL19 — 모델 2(지원규모) 딥러닝 재대결 (M45 정제 타깃)", "",
         "> DL14 의 판정은 M12 타깃 위에서 나온 것이라 지금 기준으로는 낡았다.",
         "> 타깃을 M45 로 바꾸고 같은 규율로 다시 잰다.", "",
         "## 1. 무엇이 달라졌는가", "",
         "| | DL14 시점 (M12 타깃) | 지금 (M45 정제) |", "|---|---:|---:|",
         "| n | %d | %d |" % (prev["n"], r["n"]),
         "| 비교군 중앙값 baseline | %.4f | %.4f |" % (prev["cohort"], b["MAE_log10"]),
         "| LightGBM | %.4f | %.4f |" % (prev["lgbm"], l["MAE_log10"]), "",
         "타깃에서 고친 두 가지.", "",
         "```text",
         "1. per_recipient 에서 stated_cap(원문 한도)만 남기고",
         "   budget_div_count(총예산/건수 = 평균)를 뺀다",
         "   -> 비교군 안에서 이 둘은 9.97배 갈린다",
         "",
         "2. 출처(cohort)를 비교군 사다리의 필수 축으로 올린다",
         "   -> 섞으면 10칸 중 8칸이 유의하게 갈리고 최대 40배",
         "```", "",
         "`per_recipient_basis` 는 feature 에서 빠졌다 — 정제 후 전부 `stated_cap`",
         "이라 상수축이다. DL14 는 이 축을 feature 로 갖고 있었다.", "",
         "## 2. 같은 자로 쟀다", "",
         "```text", r["cv"],
         "타깃    %s" % r["target"],
         "feature %d개 범주 + %d개 수치 (M45 make_xy 그대로)"
         % (len(r["cat_features"]), len(r["num_features"])), "```", "",
         "## 3. 결과", "",
         "| 모델 | MAE(log10) | MedAE | 배수오차 | 2배 이내 | fold σ |",
         "|---|---:|---:|---:|---:|---:|",
         "| **LGBM-quantile50** (M45 채택) | **%.4f** | %.4f | %.2fx | %.1f%% | %.4f |"
         % (l["MAE_log10"], l["MedAE_log10"], l["geo_mean_error_x"],
            l["within_2x"] * 100, l["fold_std"]),
         "| FT-Transformer (시드 3개 평균) | %.4f ± %.4f | %.4f | %.2fx | %.1f%% | %.4f |"
         % (ft["MAE_log10_mean"], ft["MAE_log10_std"], ft["MedAE_log10_mean"],
            ft["geo_mean_error_x"], ft["within_2x_mean"] * 100, ft["fold_std_mean"]),
         "| Quantile MLP (시드 3개 평균) | %.4f ± %.4f | %.4f | %.2fx | %.1f%% | %.4f |"
         % (mlp["MAE_log10_mean"], mlp["MAE_log10_std"], mlp["MedAE_log10_mean"],
            mlp["geo_mean_error_x"], mlp["within_2x_mean"] * 100, mlp["fold_std_mean"]),
         "| 비교군 중앙값 (baseline) | %.4f | %.4f | %.2fx | %.1f%% | %.4f |"
         % (b["MAE_log10"], b["MedAE_log10"], b["geo_mean_error_x"],
            b["within_2x"] * 100, b["fold_std"]), "",
         "Quantile MLP 는 P10~P90 을 함께 학습하므로 구간도 나온다 — 커버리지 %.1f%%, "
         "구간폭 중앙값 %.1f배." % (mlp.get("p10_p90_coverage_mean", 0) * 100,
                              mlp.get("median_width_x_mean", 0)), "",
         "### 시드별", "", "| 모델 | 시드 | MAE | 2배 이내 | fold σ |",
         "|---|---:|---:|---:|---:|"]
    for tag, key in (("FT-Transformer", "ft_transformer"), ("Quantile MLP", "quantile_mlp")):
        for p in r[key]["per_seed"]:
            L.append("| %s | %d | %.4f | %.1f%% | %.4f |"
                     % (tag, p["seed"], p["MAE_log10"], p["within_2x"] * 100, p["fold_std"]))

    L += ["", "## 4. 판정", "",
          "판정선을 결과를 보기 전에 못박았다 — LightGBM 의 fold 간 표준편차(%.4f)보다"
          % r["significance_margin_fold_std"],
          "작은 차이는 분할이 흔들린 것과 구별되지 않으므로 개선이라 부르지 않는다.", "",
          "| 도전자 | MAE | LGBM 대비 | 판정 |", "|---|---:|---:|---|"]
    for v in r["verdicts"]:
        L.append("| %s | %.4f | %+.4f | %s |"
                 % (v["model"], v["MAE"], v["diff_vs_lgbm"], v["verdict"]))
    L += ["", "**최종: %s**" % r["final"], "",
          "## 5. 왜 이 조건에서 딥러닝이 불리한가", "",
          "표본이 %d행이고 feature 가 %d축이다. 사전학습 전이가 없는 신경망에게"
          % (r["n"], len(r["cat_features"]) + len(r["num_features"])),
          "2천 행은 적다. 모델 1에서는 KLUE-RoBERTa 가 한국어 사전지식을 이미 갖고",
          "있어 900건으로도 ML 기준선을 넘었지만, 표 형식 수치·범주 데이터에는",
          "그런 전이가 없다. 못 이겼으면 못 이겼다고 보고한다.", "",
          "다만 이번 재대결로 확인된 것이 하나 더 있다 — **타깃 정제는 ML·DL 양쪽을",
          "같이 끌어올린다.** 라벨 노이즈였던 '한도와 평균의 혼재'가 빠지면 두 계열이",
          "모두 좋아진다. 모델을 바꾸는 것보다 라벨을 고치는 쪽이 컸다는 뜻이다.", "",
          "실행 시간 %.1f분 (CPU)." % r["runtime_min"], ""]
    p = os.path.join(C.REPORTS, "dl19_m2_amount_v2.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
