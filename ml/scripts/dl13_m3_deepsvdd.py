"""DL13 — 모델 3 딥러닝 후보: Deep SVDD 를 OneClassSVM 과 같은 hold-out 에서 비교.

계획서 4.5/6절 Priority 2.

    OneClassSVM 과 동일하게 정상 분포를 학습하고, 같은 실제 hold-out 에서
    ML/DL 을 비교한다.

같은 것 (M30 과 완전히 동일)
    학습 2,339행 / feature A_설계핵심 / StandardScaler / 경고율 2% /
    경고선은 전체 분포에서 / 사람 라벨 50건 / 정답 정의 네 가지

다른 것
    RBF 커널 대신 신경망이 초구(hypersphere) 중심으로 정상 분포를 모은다.

Deep SVDD 구현 메모
    · one-class 목적함수: mean ||f(x) - c||^2. c 는 초기 순전파 평균으로 한 번
      정하고 고정한다.
    · 편향(bias)과 배치정규화의 이동항을 쓰지 않는다. 쓰면 신경망이 모든 입력을
      c 로 보내 손실을 0 으로 만드는 붕괴해(collapse)로 간다.
    · c 의 0 에 가까운 성분은 ±0.1 로 밀어낸다. 원 논문의 같은 이유다.
    · 시드 5개로 평균±표준편차를 낸다. 재학습 안정성은 M14 에서 AE 가 무너진
      지점이라 여기서도 반드시 본다.

CPU 로 충분하다. 2,339행 x 8축이라 GPU 를 붙일 이유가 없다.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m16_m4_tuning import EXPERIMENTS, encode
from m30_m3_real_eval import (ALERT_RATE, FEATURES, GAMMA, LABELS, LEAKED, NU,
                              SCALER, binary, rank_quality, topk_flags, views)

SEEDS = [42, 7, 2024, 1234, 99]
EPOCHS = 200
LR = 1e-3
REP_DIM = 8
HIDDEN = 32


class Net(nn.Module):
    """편향 없는 인코더. 편향이 있으면 붕괴해로 간다."""

    def __init__(self, d_in, hidden=HIDDEN, rep=REP_DIM):
        super().__init__()
        self.f = nn.Sequential(
            nn.Linear(d_in, hidden, bias=False), nn.LeakyReLU(0.1),
            nn.Linear(hidden, hidden, bias=False), nn.LeakyReLU(0.1),
            nn.Linear(hidden, rep, bias=False))

    def forward(self, x):
        return self.f(x)


def deep_svdd_scores(X, seed):
    """정상 분포를 초구로 학습하고 중심까지의 제곱거리를 이상점수로 낸다."""
    torch.manual_seed(seed)
    np.random.seed(seed)
    Xt = torch.tensor(X, dtype=torch.float32)
    net = Net(X.shape[1])

    with torch.no_grad():
        c = net(Xt).mean(0)
        c[(c.abs() < 0.1)] = 0.1        # 0 근처 성분은 밀어낸다(붕괴 방지)

    opt = torch.optim.Adam(net.parameters(), lr=LR, weight_decay=1e-5)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS)
    n = len(Xt)
    for _ in range(EPOCHS):
        perm = torch.randperm(n)
        for i in range(0, n, 128):
            b = Xt[perm[i:i + 128]]
            loss = ((net(b) - c) ** 2).sum(1).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
        sched.step()

    net.eval()
    with torch.no_grad():
        return ((net(Xt) - c) ** 2).sum(1).numpy()


def top_overlap(a, b, k=30):
    """두 점수의 상위 k 목록이 얼마나 겹치는가 — 목록 재현성."""
    ta = set(np.argsort(a)[::-1][:k])
    tb = set(np.argsort(b)[::-1][:k])
    return len(ta & tb) / k


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alert-rate", type=float, default=ALERT_RATE)
    ap.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    ap.add_argument("--equal-budget", type=int, default=20,
                    help="hold-out 안에서 각자 상위 N건을 경고로 보는 비교 (표집 편향 제거)")
    args = ap.parse_args()
    seeds = [int(s) for s in args.seeds.split(",")]

    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    cfg = EXPERIMENTS[FEATURES]
    X, _, names = encode(train, train, cfg["num"], cfg["cat"], SCALER)

    lab = pd.read_csv(LABELS, encoding="utf-8-sig").rename(
        columns={"판단(정상/비전형)": "판단"})
    base = lab[["row_id", "사업명", "판단", "하위유형", "판단이유"]]

    k = max(1, int(round(len(train) * args.alert_rate)))
    per_seed, all_scores = [], []
    for s in seeds:
        sc = deep_svdd_scores(X, s)
        all_scores.append(sc)
        thr = float(np.sort(sc)[::-1][k - 1])
        t = train.assign(score=sc, score_pct=pd.Series(sc).rank(pct=True).to_numpy() * 100)
        hold = base.merge(t[["row_id", "score", "score_pct"]], on="row_id",
                          how="left").dropna(subset=["score"]).reset_index(drop=True)
        res, _ = views(hold, thr)
        res_eq, _ = views(hold, flagged=topk_flags(hold, args.equal_budget))
        rq = rank_quality(hold["score"].to_numpy(), (hold["판단"] == "비전형").to_numpy())
        per_seed.append({"seed": s, "threshold": round(thr, 6),
                         "results": res, "results_equal_budget": res_eq,
                         "rank_quality_strict": rq})
        print("seed %-5d 엄격 recall %s precision %s | ROC-AUC %s"
              % (s, res["엄격(비전형만)"]["recall"], res["엄격(비전형만)"]["precision"],
                 rq.get("roc_auc")))

    def agg(path, block="results"):
        vals = []
        for p in per_seed:
            cur = p[block]
            for step in path[:-1]:
                cur = cur[step]
            v = cur.get(path[-1])
            if v is not None:
                vals.append(v)
        return ({"mean": round(float(np.mean(vals)), 4),
                 "std": round(float(np.std(vals)), 4)} if vals else None)

    summary, summary_eq = {}, {}
    for view in per_seed[0]["results"]:
        summary[view] = {m: agg([view, m]) for m in ("recall", "precision", "f1")}
        summary_eq[view] = {m: agg([view, m], "results_equal_budget")
                            for m in ("recall", "precision", "f1")}
    roc = [p["rank_quality_strict"].get("roc_auc") for p in per_seed
           if p["rank_quality_strict"]]
    pra = [p["rank_quality_strict"].get("pr_auc") for p in per_seed
           if p["rank_quality_strict"]]

    # 시드 간 상위 목록 재현성 — M14 에서 AE 가 무너진 지점
    ov = [top_overlap(all_scores[i], all_scores[j])
          for i in range(len(seeds)) for j in range(i + 1, len(seeds))]

    # 같은 hold-out 의 OneClassSVM 수치를 그대로 옆에 둔다
    ocsvm = {}
    p30 = os.path.join(C.REPORTS, "m30_m3_real_eval.json")
    if os.path.exists(p30):
        j30 = json.load(open(p30, encoding="utf-8"))
        ocsvm = {"results": j30["results"],
                 "results_equal_budget": j30.get("results_equal_budget", {}),
                 "rank_quality": j30["rank_quality"], "model": j30["model"]}

    report = {
        "질문": "이 사업의 설계 조합이 과거 비교군 대비 얼마나 드문가",
        "model": "Deep SVDD (bias-free MLP %d-%d-%d, epochs %d, lr %.0e)"
                 % (X.shape[1], HIDDEN, REP_DIM, EPOCHS, LR),
        "n_train": int(len(train)), "n_features": int(X.shape[1]),
        "feature_names": names,
        "alert_rate": args.alert_rate, "n_alerted_population": k,
        "seeds": seeds,
        "per_seed": per_seed,
        "summary_mean_std": summary,
        "summary_equal_budget_mean_std": {
            "n_alerts_within_holdout": args.equal_budget, **summary_eq},
        "rank_quality_strict_mean": {
            "roc_auc": round(float(np.mean(roc)), 4) if roc else None,
            "roc_auc_std": round(float(np.std(roc)), 4) if roc else None,
            "pr_auc": round(float(np.mean(pra)), 4) if pra else None,
            "pr_auc_std": round(float(np.std(pra)), 4) if pra else None,
        },
        "seed_top30_overlap": {"mean": round(float(np.mean(ov)), 4),
                               "min": round(float(np.min(ov)), 4),
                               "pairs": len(ov)},
        "ocsvm_same_holdout": ocsvm,
        "note": ("경고선·feature·스케일러·hold-out·정답 정의를 M30 과 동일하게 뒀다. "
                 "다른 것은 모델 구조뿐이다."),
    }
    C.save_report("dl13_m3_deepsvdd.json", report)

    print("\n%-26s%16s%16s" % ("정답 정의", "DeepSVDD recall", "OCSVM recall"))
    for view, m in summary.items():
        o = ocsvm.get("results", {}).get(view, {}).get("recall")
        print("%-26s%16s%16s"
              % (view, "%.3f±%.3f" % (m["recall"]["mean"], m["recall"]["std"])
                 if m["recall"] else "-", o))
    print("\nROC-AUC(엄격) DeepSVDD %.4f±%.4f | OCSVM %s"
          % (report["rank_quality_strict_mean"]["roc_auc"],
             report["rank_quality_strict_mean"]["roc_auc_std"],
             ocsvm.get("rank_quality", {}).get("엄격(비전형만)", {}).get("roc_auc")))
    print("\n같은 경고 예산(hold-out 상위 %d건)" % args.equal_budget)
    for view, m in summary_eq.items():
        o = ocsvm.get("results_equal_budget", {}).get(view, {}).get("recall")
        print("%-26s DeepSVDD %s | OCSVM %s"
              % (view, "%.3f±%.3f" % (m["recall"]["mean"], m["recall"]["std"])
                 if m["recall"] else "-", o))
    print("\n시드 간 상위30 겹침 평균 %.2f (최소 %.2f)"
          % (report["seed_top30_overlap"]["mean"], report["seed_top30_overlap"]["min"]))


if __name__ == "__main__":
    main()
