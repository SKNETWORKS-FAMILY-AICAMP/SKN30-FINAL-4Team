"""S09B — 모델 4 딥러닝: Autoencoder 복원오차 기반 이상탐지 + Ablation.

설계서의 모델 4 우선순위 4위가 Autoencoder 다.
1~3위(IsolationForest / LOF / One-Class SVM)는 S09A 에서 이미 쟀다.

기준선 (S09A, 같은 2,339행 · 같은 인코딩 · 같은 합성 이상치)
    IsolationForest    top-k 회수율 0.083   <- 설계서 MVP 권장이었으나 실패
    LocalOutlierFactor top-k 회수율 0.633
    OneClassSVM        top-k 회수율 0.783   <- ML 채택. 딥러닝이 넘어야 할 상대

AE 를 쓰는 논리
    정상 패턴만 잘 복원하도록 학습하면, 복원이 안 되는 행이 곧 이례적인 행이다.
    IsolationForest 가 실패한 원인이 "결측 지시자와 희귀 범주 빈도에 점수가
    끌려간 것"(S09A 축별 상관 0.53/0.50)이었으므로, AE 가 같은 함정에 빠지는지
    똑같은 방식으로 다시 잰다.

평가는 S09A 와 동일하게 네 갈래 — 정답이 없는 비지도 모델이라 잣대를 바꾸면
비교 자체가 무의미해진다.
    1. synthetic anomaly injection (같은 4종 · 같은 시드)
    2. 재학습 안정성 (80% 재표집)
    3. 축별 점수 상관 — 무엇이 점수를 끌고 가는가
    4. 상위 사례의 축 단위 설명

Ablation 축 (기존 S06F 방식과 동일 — one-factor-at-a-time)
    latent      2 / 4 / 8 / 16
    hidden      32 / 64 / 128
    epochs      200 / 500 / 1000
    lr          3e-4 / 1e-3 / 3e-3
    dropout     0.0 / 0.1 / 0.3
    denoising   0.0 / 0.1 / 0.2
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from s09a_m4_anomaly import (MIN_AXES, REF, SRC, encode, explain, inject_synthetic,
                            norm01, prepare, score_drivers, status_of)
from s07b_m2_cluster_dl import AE

OUT = os.path.join(C.PROC, "design_anomaly_ae.parquet")
SEED = 42

BASE = {"latent": 4, "hidden": 64, "epochs": 500, "lr": 1e-3,
        "dropout": 0.1, "denoising": 0.1}
GRID = {
    "latent": [2, 4, 8, 16],
    "hidden": [32, 64, 128],
    "epochs": [200, 500, 1000],
    "lr": [3e-4, 1e-3, 3e-3],
    "dropout": [0.0, 0.1, 0.3],
    "denoising": [0.0, 0.1, 0.2],
}


def ae_scores(Xtr, Xap, cfg, seed=SEED):
    """복원오차를 이상점수로 쓴다. 값이 클수록 '학습한 패턴으로 설명이 안 된다'."""
    torch.manual_seed(seed)
    xt = torch.tensor(Xtr, dtype=torch.float32)
    xa = torch.tensor(Xap, dtype=torch.float32)
    m = AE(xt.shape[1], cfg["hidden"], cfg["latent"], cfg["dropout"])
    opt = torch.optim.Adam(m.parameters(), lr=cfg["lr"])
    m.train()
    for _ in range(cfg["epochs"]):
        opt.zero_grad()
        inp = xt + torch.randn_like(xt) * cfg["denoising"] if cfg["denoising"] else xt
        _, rec = m(inp)
        ((rec - xt) ** 2).mean().backward()
        opt.step()
    m.eval()
    with torch.no_grad():
        _, rec = m(xa)
        # 행별 평균이 아니라 합을 쓴다. 축이 많은 행이 희석되지 않게 한다.
        return ((rec - xa) ** 2).sum(dim=1).numpy()


def eval_synthetic(train, cfg, n=60):
    """S09A 와 같은 합성 이상치·같은 시드. 잣대를 바꾸면 비교가 무의미해진다."""
    syn = inject_synthetic(train, n)
    mixed = pd.concat([train.assign(__synthetic=False, __kind=-1), syn],
                      ignore_index=True)
    Xtr, Xap, _ = encode(train, mixed)
    s = ae_scores(Xtr, Xap, cfg)
    is_syn = mixed["__synthetic"].to_numpy()
    order = np.argsort(-s)
    k = int(is_syn.sum())
    ranks = np.argsort(np.argsort(-s))[is_syn] / len(s)
    return {"n_synthetic": k,
            "recall_at_k": round(float(is_syn[order[:k]].mean()), 4),
            "recall_at_2k": round(float(is_syn[order[:2 * k]].mean()), 4),
            "median_rank_pct": round(float(np.median(ranks)) * 100, 2)}


def eval_resample(train, cfg, tops=30, n_iter=10, frac=0.8):
    rng = np.random.default_rng(SEED)
    Xtr, Xap, _ = encode(train, train)
    base = set(train.iloc[np.argsort(-ae_scores(Xtr, Xap, cfg))[:tops]]["row_id"])
    ov = []
    for _ in range(n_iter):
        sub = train.sample(frac=frac, random_state=int(rng.integers(1e6)))
        Xs, Xa, _ = encode(sub, train)
        top = set(train.iloc[np.argsort(-ae_scores(Xs, Xa, cfg))[:tops]]["row_id"])
        ov.append(len(top & base) / tops)
    return {"n_iter": n_iter, "frac": frac, "top_n": tops,
            "overlap_mean": round(float(np.mean(ov)), 4),
            "overlap_min": round(float(np.min(ov)), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()

    df = prepare(pd.read_parquet(SRC))
    ref = pd.read_parquet(REF)
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    Xtr, Xap, names = encode(train, train)
    print("모델 4 DL 대상: %d행 / 입력 %d축" % (len(train), Xtr.shape[1]))
    print("기준선(S09A): IsolationForest 0.083 / LOF 0.633 / OneClassSVM 0.783")

    t0 = time.time()
    base = eval_synthetic(train, BASE)
    print("\n== 기준 설정 (튜닝 전)")
    print("  %s" % BASE)
    print("  top-k 회수율 %.3f / top-2k %.3f / 합성사례 중앙 순위 상위 %.1f%%"
          % (base["recall_at_k"], base["recall_at_2k"], base["median_rank_pct"]))

    grid = GRID if not a.smoke else {"latent": [2, 4], "epochs": [200, 500]}
    print("\n== Ablation (one-factor-at-a-time)")
    abl, best_cfg = {}, dict(BASE)
    for axis, values in grid.items():
        abl[axis] = {}
        for v in values:
            abl[axis][str(v)] = (dict(base, note="기준 설정") if v == BASE[axis]
                                 else eval_synthetic(train, dict(BASE, **{axis: v})))
        best_v = max(abl[axis], key=lambda k: abl[axis][k]["recall_at_k"])
        best_cfg[axis] = float(best_v) if "." in best_v else int(best_v)
        print("  %-10s %s  -> 최적 %s"
              % (axis, "  ".join("%s:%.3f" % (k, v["recall_at_k"])
                                 for k, v in abl[axis].items()), best_v))

    print("\n== 축별 최적값 조합")
    print("  %s" % best_cfg)
    tuned = eval_synthetic(train, best_cfg)
    print("  top-k 회수율 %.3f / top-2k %.3f / 합성사례 중앙 순위 상위 %.1f%%"
          % (tuned["recall_at_k"], tuned["recall_at_2k"], tuned["median_rank_pct"]))

    # OFAT 은 축을 하나씩만 움직여 고른 값이라 축끼리 상호작용하면 조합이 무너진다.
    # 실제로 여기서 조합이 기준 설정보다 나빠졌다. 조합을 무조건 채택하지 않고
    # 둘을 같은 잣대로 다시 재서 나은 쪽을 쓴다 — 안 그러면 튜닝이 성능을 깎는다.
    ofat_failed = tuned["recall_at_k"] < base["recall_at_k"]
    final_cfg, final = ((dict(BASE), base) if ofat_failed else (best_cfg, tuned))
    if ofat_failed:
        print("  -> 조합이 기준 설정보다 나쁘다 (%.3f < %.3f). OFAT 축 간 상호작용이"
              " 있다는 뜻이므로 기준 설정을 최종으로 쓴다."
              % (tuned["recall_at_k"], base["recall_at_k"]))

    s = ae_scores(Xtr, Xap, final_cfg)
    drivers = score_drivers(Xap, names, {"AE-recon": s})
    top4 = sorted(drivers["AE-recon"].items(), key=lambda kv: -abs(kv[1]))[:4]
    print("\n== 점수를 끌고 가는 축 (S09A 의 IF 실패 원인을 같은 방식으로 확인)")
    print("  %s" % ", ".join("%s %.2f" % kv for kv in top4))

    res = eval_resample(train, final_cfg)
    print("\n== 재학습 안정성 (80% 재표집 10회, 상위 30건 유지율)")
    print("  평균 %.3f / 최저 %.3f" % (res["overlap_mean"], res["overlap_min"]))

    train["anomaly_score"] = np.round(norm01(s), 4)
    train["score_pct"] = pd.Series(s).rank(pct=True).to_numpy() * 100
    train[["status", "status_text"]] = pd.DataFrame(
        [status_of(p) for p in train["score_pct"]], index=train.index)

    cases = []
    print("\n== 상위 이상 사례 5건")
    for _, r in train.sort_values("anomaly_score", ascending=False).head(5).iterrows():
        note = explain(r, ref)
        cases.append({"row_id": r["row_id"], "title": r["title"],
                      "anomaly_score": float(r["anomaly_score"]),
                      "status": r["status"],
                      "notable_features": [x["text"] for x in note[:3]]})
        print("  [%.3f] %s" % (r["anomaly_score"], str(r["title"])[:44]))
        for x in note[:2]:
            print("         - %s" % x["text"])

    ml = {"IsolationForest": 0.083, "LocalOutlierFactor": 0.633, "OneClassSVM": 0.783,
          "OneClassSVM_resample_overlap": 0.827}
    verdict = judge(final, res, ml)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    train[["row_id", "cohort", "title", "support_type", "support_method",
           "n_axes", "anomaly_score", "score_pct", "status", "status_text"]] \
        .to_parquet(OUT, index=False)
    print("[data] %s" % OUT)

    C.save_report("s09b_m4_anomaly_dl.json", {
        "n_rows": int(len(train)), "n_input_axes": int(Xtr.shape[1]),
        "base_config": BASE, "base_result": base, "ablation": abl,
        "tuned_config": best_cfg, "tuned_result": tuned,
        "ofat_combination_failed": bool(ofat_failed),
        "final_config": final_cfg, "final_result": final,
        "score_drivers": drivers, "resample_stability": res,
        "ml_reference": ml, "top_cases": cases, "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2),
    })


def judge(dl, res, ml):
    reasons, v = [], "미채택"
    best_ml = ml["OneClassSVM"]
    if dl["recall_at_k"] > best_ml:
        reasons.append("합성 이상치 회수율 %.3f > OneClassSVM %.3f"
                       % (dl["recall_at_k"], best_ml))
        v = "채택"
    else:
        reasons.append("합성 이상치 회수율 %.3f <= OneClassSVM %.3f — 넘지 못했다"
                       % (dl["recall_at_k"], best_ml))
    if dl["recall_at_k"] > ml["IsolationForest"]:
        reasons.append("설계서 MVP 권장이던 IsolationForest(%.3f)보다는 낫다"
                       % ml["IsolationForest"])
    reasons.append("재학습 상위 30건 유지율 평균 %.0f%% (OneClassSVM %.0f%%)"
                   % (res["overlap_mean"] * 100, ml["OneClassSVM_resample_overlap"] * 100))
    if v == "채택" and res["overlap_mean"] < 0.6:
        v = "Conditional"
        reasons.append("회수율은 앞서지만 재학습 시 상위 목록이 흔들린다")
    return {"verdict": v, "reasons": reasons}


if __name__ == "__main__":
    main()
