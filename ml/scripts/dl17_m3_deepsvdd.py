r"""DL17 — Deep SVDD 재평가 + 학습 진단 (계획서 Step 6, §8).

DL13 과 무엇이 다른가
    입력   M32 로 교정된 feature (DL13 은 연도 오파싱이 살아 있던 값)
    정답   M33 의 `atypical_design` 10건 (DL13 은 `비전형` 15건 = 절반이 파서 오류)
    진단   train/validation loss 곡선을 처음으로 남긴다

계획서 §8 이 지적한 것 — loss 가 줄었다는 사실만으로 학습 성공을 판단하면 안 된다.

    Case A  loss 가 거의 안 준다        -> feature 에 정보가 없거나 objective 가 안 맞다
    Case B  train 은 주는데 val 은 악화 -> 과적합 / 합성규칙 암기
    Case C  둘 다 급격히 좋아진다       -> 먼저 누수와 shortcut 을 의심한다

Deep SVDD 는 정상 분포를 초구로 모으는 one-class 목적함수라 val loss 가
'성능'이 아니다. 그래도 반드시 봐야 하는 이유가 있다.

    val loss 가 train loss 를 따라 같이 떨어지면 -> 초구가 데이터의 구조를 잡았다
    val loss 만 정체·발산하면                    -> train 표본을 외운 것이다
    둘 다 0 으로 수렴하면                        -> **붕괴해(collapse)** 다.
                                                   모든 입력을 c 로 보내 손실만 0 이
                                                   되고 점수는 무의미해진다.

그래서 loss 세 줄을 같이 낸다: train / validation / 표현 붕괴 지표
(representation std — 초구 위 표현의 표준편차. 0 으로 가면 붕괴다).

안정성은 성능과 별개로 본다. DL13 에서 시드 간 상위30 겹침이 0.48 이었다 —
같은 데이터로 다시 학습하면 경고 목록의 절반이 바뀐다는 뜻이다. 공무원에게
검토 신호를 주는 서비스에서 그 정도 흔들림은 성능과 무관하게 결격이다.

CPU 로 충분하다. 1,948행 x 8축이라 GPU 를 붙일 이유가 없다.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from scipy.stats import kendalltau, spearmanr
from sklearn.metrics import average_precision_score, roc_auc_score

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CLEAN, EQUAL_BUDGET, _binary, boot_ci
from m36_m3_oneclass import ALERT_RATE, encode

SEEDS = [42, 7, 2024, 1234, 99]
EPOCHS = 200
LR = 1e-3
REP_DIM = 8
HIDDEN = 32
VAL_FRAC = 0.2


class Net(nn.Module):
    """편향 없는 인코더. 편향이 있으면 모든 입력을 c 로 보내는 붕괴해로 간다."""

    def __init__(self, d_in, hidden=HIDDEN, rep=REP_DIM):
        super().__init__()
        self.f = nn.Sequential(
            nn.Linear(d_in, hidden, bias=False), nn.LeakyReLU(0.1),
            nn.Linear(hidden, hidden, bias=False), nn.LeakyReLU(0.1),
            nn.Linear(hidden, rep, bias=False))

    def forward(self, x):
        return self.f(x)


def train_one(X, seed, epochs=EPOCHS, val_frac=VAL_FRAC):
    """초구 중심까지의 제곱거리를 이상점수로 낸다. loss 곡선을 함께 돌려준다.

    validation 은 **학습에 쓰지 않는다.** 조기종료도 하지 않는다 — 여기서
    val 을 보고 멈추면 val 이 더 이상 독립적인 진단이 아니게 된다.
    """
    torch.manual_seed(seed)
    np.random.seed(seed)
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(X))
    n_val = int(len(X) * val_frac)
    vi, ti = idx[:n_val], idx[n_val:]

    Xt = torch.tensor(X, dtype=torch.float32)
    Xtr, Xva = Xt[ti], Xt[vi]
    net = Net(X.shape[1])

    with torch.no_grad():
        c = net(Xtr).mean(0)
        c[(c.abs() < 0.1)] = 0.1        # 0 근처 성분을 밀어낸다 (붕괴 방지)

    opt = torch.optim.Adam(net.parameters(), lr=LR, weight_decay=1e-5)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    curve = []
    n = len(Xtr)
    for ep in range(epochs):
        net.train()
        perm = torch.randperm(n)
        tot = 0.0
        for i in range(0, n, 128):
            b = Xtr[perm[i:i + 128]]
            loss = ((net(b) - c) ** 2).sum(1).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss) * len(b)
        sched.step()
        net.eval()
        with torch.no_grad():
            zva = net(Xva)
            vl = float(((zva - c) ** 2).sum(1).mean())
            rep_std = float(net(Xt).std(0).mean())
        curve.append({"epoch": ep + 1, "train_loss": tot / n,
                      "val_loss": vl, "rep_std": rep_std})

    net.eval()
    with torch.no_grad():
        s = ((net(Xt) - c) ** 2).sum(1).numpy()
    return s, curve


def topk_overlap(a, b, k):
    return len(set(np.argsort(a)[::-1][:k]) & set(np.argsort(b)[::-1][:k])) / k


def evaluate(train, scores, cl):
    s = pd.Series(scores, index=train["row_id"].to_numpy())
    sub = cl[cl["라벨"].isin(["normal", "atypical_design"]) & cl["row_id"].isin(s.index)]
    y = (sub["라벨"] == "atypical_design").to_numpy(int)
    sc = s.loc[sub["row_id"]].to_numpy(float)
    k = max(1, int(round(len(train) * ALERT_RATE)))
    thr = float(np.sort(scores)[::-1][k - 1])
    eb = min(EQUAL_BUDGET, len(sc))
    return {
        "n": int(len(y)), "n_positive": int(y.sum()),
        "roc_auc": round(float(roc_auc_score(y, sc)), 4),
        "pr_auc": round(float(average_precision_score(y, sc)), 4),
        "operating_2pct": _binary(y, sc >= thr),
        "equal_budget": {"top_n": eb, **_binary(y, sc >= np.sort(sc)[::-1][eb - 1])},
    }, y, sc


def read_curve(curve):
    """계획서 §8 의 Case A/B/C 를 곡선에서 기계적으로 읽는다."""
    t = np.array([c["train_loss"] for c in curve])
    v = np.array([c["val_loss"] for c in curve])
    r = np.array([c["rep_std"] for c in curve])
    drop = 1 - t[-1] / max(t[0], 1e-12)
    val_drop = 1 - v[-1] / max(v[0], 1e-12)
    if r[-1] < 0.05:
        case, why = "붕괴(collapse)", "표현 표준편차가 %.4f 로 0 에 붙었다. 점수가 무의미하다." % r[-1]
    elif drop < 0.1:
        case, why = "Case A", "train loss 가 %.0f%% 밖에 안 줄었다. 학습이 안 됐다." % (drop * 100)
    elif val_drop < drop - 0.3:
        case, why = "Case B", ("train %.0f%% 감소 대비 val 은 %.0f%% — train 표본을 외웠다."
                               % (drop * 100, val_drop * 100))
    else:
        case, why = "Case C", ("train %.0f%% / val %.0f%% 동반 감소. 붕괴도 과적합도 아니다 — "
                               "다음은 누수와 shortcut 을 의심할 차례다."
                               % (drop * 100, val_drop * 100))
    return {"case": case, "reading": why,
            "train_loss_first": round(float(t[0]), 6), "train_loss_last": round(float(t[-1]), 6),
            "val_loss_first": round(float(v[0]), 6), "val_loss_last": round(float(v[-1]), 6),
            "train_drop": round(float(drop), 4), "val_drop": round(float(val_drop), 4),
            "rep_std_last": round(float(r[-1]), 6),
            "train_val_gap_last": round(float(v[-1] - t[-1]), 6)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    args = ap.parse_args()
    seeds = [int(s) for s in args.seeds.split(",")]

    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    cl = pd.read_csv(CLEAN, encoding="utf-8-sig")
    X, _, names = encode(train, train)

    print("DL17 — Deep SVDD 재평가 + 학습 진단")
    print("  학습 %d행 x %d축 / 시드 %s / epoch %d"
          % (len(train), X.shape[1], seeds, args.epochs))

    per_seed, all_scores, curves = [], [], {}
    for s in seeds:
        sc, curve = train_one(X, s, args.epochs)
        all_scores.append(sc)
        curves[s] = curve
        ev, y, hs = evaluate(train, sc, cl)
        diag = read_curve(curve)
        per_seed.append({"seed": s, "eval": ev, "curve_reading": diag})
        print("  seed %-5d ROC-AUC %.4f PR-AUC %.4f | %s | train %.4f -> %.4f, "
              "val %.4f -> %.4f, rep_std %.4f"
              % (s, ev["roc_auc"], ev["pr_auc"], diag["case"],
                 diag["train_loss_first"], diag["train_loss_last"],
                 diag["val_loss_first"], diag["val_loss_last"], diag["rep_std_last"]))

    def agg(f):
        v = [f(p) for p in per_seed]
        return {"mean": round(float(np.mean(v)), 4), "std": round(float(np.std(v)), 4)}

    summary = {
        "roc_auc": agg(lambda p: p["eval"]["roc_auc"]),
        "pr_auc": agg(lambda p: p["eval"]["pr_auc"]),
        "equal_budget_recall": agg(lambda p: p["eval"]["equal_budget"]["recall"] or 0.0),
    }

    # 시드 간 목록 재현성과 순위 상관
    pairs = [(i, j) for i in range(len(seeds)) for j in range(i + 1, len(seeds))]
    stab = {}
    for k in (20, 30, 50):
        ov = [topk_overlap(all_scores[i], all_scores[j], k) for i, j in pairs]
        stab["top%d_overlap" % k] = {"mean": round(float(np.mean(ov)), 4),
                                     "min": round(float(np.min(ov)), 4)}
    sp = [spearmanr(all_scores[i], all_scores[j]).statistic for i, j in pairs]
    kt = [kendalltau(all_scores[i], all_scores[j]).statistic for i, j in pairs]
    stab["spearman"] = {"mean": round(float(np.mean(sp)), 4), "min": round(float(np.min(sp)), 4)}
    stab["kendall_tau"] = {"mean": round(float(np.mean(kt)), 4), "min": round(float(np.min(kt)), 4)}

    print("\n== 성능 (시드 %d회 평균±표준편차)" % len(seeds))
    for k, v in summary.items():
        print("  %-22s %.4f ± %.4f" % (k, v["mean"], v["std"]))
    print("\n== 안정성 (시드 쌍 %d개)" % len(pairs))
    for k, v in stab.items():
        print("  %-22s 평균 %.4f / 최저 %.4f" % (k, v["mean"], v["min"]))

    # 같은 hold-out 의 one-class 수치를 그대로 옆에 둔다
    ml = {}
    p36 = os.path.join(C.REPORTS, "m36_m3_oneclass.json")
    if os.path.exists(p36):
        j = json.load(open(p36, encoding="utf-8"))
        ml = {n: {"roc_auc": v["views"]["주 평가(atypical_design)"]["roc_auc"],
                  "resample_overlap": j["stability"][n]["overlap_mean"]}
              for n, v in j["per_model"].items()}
        print("\n== 같은 hold-out 의 ML 기준선")
        for n, v in ml.items():
            print("  %-22s ROC-AUC %.4f / 재표집 유지율 %.3f"
                  % (n, v["roc_auc"], v["resample_overlap"]))

    print("\n== epoch 스윕 — 언제 표현이 죽는가 (선택 기준은 rep_std 와 겹침, AUC 아님)")
    sweep = epoch_sweep(X, train, cl, seeds)

    verdict = judge(summary, stab, per_seed, ml, sweep)
    print("\n== 판정: %s" % verdict["verdict"])
    for r in verdict["reasons"]:
        print("   - %s" % r)

    # 곡선은 부피가 커서 10 epoch 간격만 남긴다
    thin = {str(s): [c for c in cv if c["epoch"] % 10 == 0 or c["epoch"] == 1]
            for s, cv in curves.items()}
    rep = {
        "model": "Deep SVDD (bias-free MLP %d-%d-%d, epochs %d, lr %.0e, val %.0f%%)"
                 % (X.shape[1], HIDDEN, REP_DIM, args.epochs, LR, VAL_FRAC * 100),
        "n_train": int(len(train)), "n_features": int(X.shape[1]),
        "feature_names": names, "seeds": seeds,
        "per_seed": per_seed, "summary_mean_std": summary, "stability": stab,
        "loss_curves_every10": thin,
        "ml_same_holdout": ml, "epoch_sweep": sweep, "verdict": verdict,
        "sweep_selection_rule": "rep_std(붕괴 여부) 와 시드 간 상위30 겹침으로만 고른다. "
                                "hold-out AUC 로 고르면 hold-out 을 학습셋으로 쓰는 것이다",
    }
    C.save_report("dl17_m3_deepsvdd.json", rep)
    write_md(rep)


def epoch_sweep(X, train, cl, seeds, grid=(20, 50, 100, 200)):
    """언제 표현이 죽는가. **hold-out AUC 로 고르지 않는다.**

    고르는 기준은 두 가지뿐이다 — 표현 표준편차(붕괴하지 않았는가)와
    시드 간 상위30 겹침(다시 학습해도 같은 목록인가). AUC 는 고른 뒤에
    확인용으로만 읽는다. AUC 로 epoch 을 고르면 hold-out 을 학습셋으로
    쓰는 것이다(계획서 §11.2).
    """
    out = {}
    for ep in grid:
        scs, stds = [], []
        for sd in seeds:
            sc, curve = train_one(X, sd, ep)
            scs.append(sc)
            stds.append(curve[-1]["rep_std"])
        pairs = [(i, j) for i in range(len(seeds)) for j in range(i + 1, len(seeds))]
        ov = [topk_overlap(scs[i], scs[j], 30) for i, j in pairs]
        aucs = [evaluate(train, sc, cl)[0]["roc_auc"] for sc in scs]
        out["epochs=%d" % ep] = {
            "rep_std_mean": round(float(np.mean(stds)), 5),
            "collapsed": bool(np.mean(stds) < 0.05),
            "top30_overlap_mean": round(float(np.mean(ov)), 4),
            "roc_auc_mean": round(float(np.mean(aucs)), 4),
            "roc_auc_std": round(float(np.std(aucs)), 4),
        }
        print("  epochs=%-4d rep_std %.5f %s | 상위30 겹침 %.3f | (참고) ROC-AUC %.3f±%.3f"
              % (ep, out["epochs=%d" % ep]["rep_std_mean"],
                 "붕괴" if out["epochs=%d" % ep]["collapsed"] else "정상",
                 out["epochs=%d" % ep]["top30_overlap_mean"],
                 out["epochs=%d" % ep]["roc_auc_mean"],
                 out["epochs=%d" % ep]["roc_auc_std"]))
    return out


def judge(summary, stab, per_seed, ml, sweep=None):
    reasons, v = [], "Go"
    cases = {p["curve_reading"]["case"] for p in per_seed}
    if "붕괴(collapse)" in cases:
        reasons.append("일부 시드에서 표현이 붕괴했다. 점수 자체가 무의미하다.")
        v = "No-Go"
    else:
        reasons.append("모든 시드가 %s — 표현 붕괴도 train-only 과적합도 없다."
                       % "/".join(sorted(cases)))

    auc = summary["roc_auc"]
    reasons.append("clean hold-out ROC-AUC %.3f ± %.3f (시드 5회)"
                   % (auc["mean"], auc["std"]))

    ov = stab["top30_overlap"]
    reasons.append("시드 간 상위30 겹침 평균 %.2f (최저 %.2f). DL13 에서는 0.48 이었다."
                   % (ov["mean"], ov["min"]))
    if ov["mean"] < 0.7:
        reasons.append("재학습하면 경고 목록이 크게 바뀐다. 성능과 무관하게 "
                       "운영 경고용으로는 결격이다.")
        v = "No-Go" if v == "No-Go" else "Conditional"

    if ml:
        best_ml = max(ml, key=lambda k: ml[k]["roc_auc"])
        if ml[best_ml]["roc_auc"] >= auc["mean"] - auc["std"]:
            reasons.append("같은 hold-out 에서 %s 가 %.3f — DL 이 ML 을 판정선 밖으로 "
                           "넘지 못했다." % (best_ml, ml[best_ml]["roc_auc"]))
            v = "미채택" if v == "Go" else v
    if sweep:
        alive = [k for k, x in sweep.items() if not x["collapsed"]]
        if alive:
            best = max(alive, key=lambda k: sweep[k]["top30_overlap_mean"])
            reasons.append("epoch 을 줄이면 표현이 살아 있다 — %s 에서 rep_std %.3f, "
                           "상위30 겹침 %.2f. 붕괴 전 지점을 쓰면 안정성이 달라진다."
                           % (best, sweep[best]["rep_std_mean"],
                              sweep[best]["top30_overlap_mean"]))
        else:
            reasons.append("스윕한 모든 epoch 에서 표현이 붕괴했다. epoch 수가 아니라 "
                           "구조(가중치감쇠·표현차원·정규화)를 손봐야 한다.")
    return {"verdict": v, "reasons": reasons}


def write_md(r):
    L = ["# DL17 — Deep SVDD 재평가와 학습 진단", "",
         "> DL13 과 같은 구조입니다. 다른 것은 **입력이 M32 로 교정된 값**이고",
         "> **정답이 M33 의 `atypical_design` 10건**이라는 점, 그리고 여기서 처음으로",
         "> **train/validation loss 곡선**을 남긴다는 점입니다.", "",
         "```text", r["model"],
         "학습 %d행 x %d축 / 시드 %s" % (r["n_train"], r["n_features"], r["seeds"]),
         "```", "",
         "## 1. 학습이 실제로 됐는가 (계획서 §8)", "",
         "loss 가 줄었다는 사실만으로는 판단할 수 없습니다. 세 줄을 같이 봅니다 —",
         "train loss, validation loss, 그리고 **표현 표준편차**(0 으로 가면 모든",
         "입력이 초구 중심으로 뭉갠 붕괴해입니다).", "",
         "| seed | 판독 | train loss | val loss | train-val 격차 | 표현 std |",
         "|---|---|---|---|---:|---:|"]
    for p in r["per_seed"]:
        d = p["curve_reading"]
        L.append("| %d | **%s** | %.4f → %.4f | %.4f → %.4f | %.4f | %.4f |"
                 % (p["seed"], d["case"], d["train_loss_first"], d["train_loss_last"],
                    d["val_loss_first"], d["val_loss_last"], d["train_val_gap_last"],
                    d["rep_std_last"]))
    L += ["", "판독 기준", "", "```text",
          "붕괴      표현 std < 0.05          -> 점수가 무의미하다",
          "Case A    train loss 감소 < 10%    -> 학습이 안 됐다",
          "Case B    val 감소가 train 보다 30%p 이상 뒤처짐 -> train 표본을 외웠다",
          "Case C    둘 다 함께 감소          -> 다음은 누수·shortcut 을 의심한다",
          "```", "",
          "validation 은 학습에 쓰지 않았고 조기종료도 걸지 않았습니다. val 을 보고",
          "멈추면 val 이 더 이상 독립적인 진단이 아니게 됩니다.", "",
          "## 2. 성능 (clean hold-out, 시드 %d회)" % len(r["seeds"]), "",
          "| 지표 | 평균 ± 표준편차 |", "|---|---|"]
    for k, v in r["summary_mean_std"].items():
        L.append("| %s | %.4f ± %.4f |" % (k, v["mean"], v["std"]))
    L += ["", "| seed | ROC-AUC | PR-AUC | 같은예산 상위7 recall |", "|---|---:|---:|---:|"]
    for p in r["per_seed"]:
        L.append("| %d | %.4f | %.4f | %s |"
                 % (p["seed"], p["eval"]["roc_auc"], p["eval"]["pr_auc"],
                    p["eval"]["equal_budget"]["recall"]))
    L += ["", "## 3. 안정성 — 재학습하면 같은 사업을 경고하는가", "",
          "성능과 별개로 봅니다. DL13 에서 상위30 겹침이 0.48 이었습니다 —",
          "같은 데이터로 다시 학습하면 경고 목록의 절반이 바뀐다는 뜻입니다.", "",
          "| 지표 | 평균 | 최저 |", "|---|---:|---:|"]
    for k, v in r["stability"].items():
        L.append("| %s | %.4f | %.4f |" % (k, v["mean"], v["min"]))
    if r.get("ml_same_holdout"):
        L += ["", "## 4. 같은 hold-out 의 ML 기준선", "",
              "| 모델 | ROC-AUC | 재표집 상위30 유지율 |", "|---|---:|---:|"]
        for n, v in sorted(r["ml_same_holdout"].items(), key=lambda kv: -kv[1]["roc_auc"]):
            L.append("| %s | %.4f | %.3f |" % (n, v["roc_auc"], v["resample_overlap"]))
        L.append("| **Deep SVDD** | **%.4f ± %.4f** | (시드 상위30 겹침 %.3f) |"
                 % (r["summary_mean_std"]["roc_auc"]["mean"],
                    r["summary_mean_std"]["roc_auc"]["std"],
                    r["stability"]["top30_overlap"]["mean"]))
    if r.get("epoch_sweep"):
        L += ["", "## 5. epoch 스윕 — 언제 표현이 죽는가", "",
              "**AUC 로 고르지 않았습니다.** 고르는 기준은 표현 표준편차(붕괴 여부)와",
              "시드 간 상위30 겹침 둘뿐이고, AUC 는 고른 뒤 확인용으로만 읽습니다.", "",
              "| epochs | 표현 std | 상태 | 시드 상위30 겹침 | (참고) ROC-AUC |",
              "|---|---:|---|---:|---:|"]
        for k, v in r["epoch_sweep"].items():
            L.append("| %s | %.5f | %s | %.3f | %.3f ± %.3f |"
                     % (k.split("=")[1], v["rep_std_mean"],
                        "**붕괴**" if v["collapsed"] else "정상",
                        v["top30_overlap_mean"], v["roc_auc_mean"], v["roc_auc_std"]))
    L += ["", "## 6. 판정", "", "**%s**" % r["verdict"]["verdict"], ""]
    for x in r["verdict"]["reasons"]:
        L.append("- %s" % x)
    L.append("")
    p = os.path.join(C.REPORTS, "dl17_m3_deepsvdd.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
