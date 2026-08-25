"""DL16 — 채택 모델(KLUE-BERT)의 판단보류 곡선을 외부 131건에서 잰다.

왜 필요한가
    운영 서비스는 확신이 낮은 공고를 '판단보류'로 빼고 나머지만 쓴다(M02/M09).
    모델을 바꾸면 그 임계값도 다시 잡아야 한다. M27 이 낸 곡선은 ML 기준선의
    것이고, 내부 CV 에서 잰 값이다. 여기서는 **채택 모델을 외부 131건에서**
    직접 재서 커버리지-정확도 맞바꿈을 낸다.

    커버리지를 안 재고 정확도만 올리면 '어려운 건을 다 빼서 높아진 정확도'와
    구별되지 않는다. 두 축을 항상 함께 낸다.

재는 것
    max_proba   최고 확률
    top2_gap    1등과 2등의 확률 차 — 두 클래스 사이에서 흔들리는 건을 잡는다
    두 축 각각에 대해 커버리지 100%에서 40%까지의 곡선을 낸다.

주의
    임계값을 외부 hold-out 에서 고르면 그 순간 외부는 검증셋이 아니다.
    여기서는 **곡선만** 낸다. 운영 임계값은 커버리지 목표를 먼저 정하고
    학습셋 OOF 에서 잡아야 한다 — 그 자리를 리포트에 적어 둔다.
"""
import argparse
import json
import os

import numpy as np
import pandas as pd
import torch
from sklearn.preprocessing import LabelEncoder

from dl12_m1_candidates import BUNDLE, OUTDIR, TRANSFORMERS, train_predict

SEEDS = [42, 7, 2024]
COVERAGES = [1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.5, 0.4]


def curve(conf, correct):
    """커버리지를 축으로 고정하고 그 위에서 정확도를 잰다."""
    order = np.argsort(-conf)
    rows = []
    for cov in COVERAGES:
        k = max(1, int(round(len(conf) * cov)))
        idx = order[:k]
        rows.append({"coverage": cov, "n": int(k),
                     "threshold": round(float(conf[idx[-1]]), 4),
                     "accuracy": round(float(correct[idx].mean()), 4)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="bert")
    ap.add_argument("--lr", type=float, default=5e-5)
    ap.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    args = ap.parse_args()
    seeds = [int(s) for s in args.seeds.split(",")]
    name = TRANSFORMERS[args.model]

    tr = pd.read_parquet(os.path.join(BUNDLE, "train.parquet"))
    ex = pd.read_parquet(os.path.join(BUNDLE, "external.parquet"))
    le = LabelEncoder().fit(tr["label"].values)
    y = le.transform(tr["label"].values)
    n_cls = len(le.classes_)
    cw = len(y) / (n_cls * np.maximum(np.bincount(y, minlength=n_cls), 1))
    gold = ex["gold"].values

    per_seed = []
    for s in seeds:
        logit = train_predict(name, tr["text"].values, y, ex["text"].values,
                              n_cls, args.lr, s, cw)
        p = torch.softmax(torch.tensor(logit), dim=1).numpy()
        srt = np.sort(p, axis=1)
        pred = le.inverse_transform(p.argmax(1))
        correct = (gold == pred).astype(float)
        per_seed.append({
            "seed": s,
            "accuracy_full": round(float(correct.mean()), 4),
            "max_proba": curve(srt[:, -1], correct),
            "top2_gap": curve(srt[:, -1] - srt[:, -2], correct),
        })
        print("seed %-5d 전체 정확도 %.4f | 커버리지 70%%: max_proba %.4f / top2_gap %.4f"
              % (s, correct.mean(),
                 [r for r in per_seed[-1]["max_proba"] if r["coverage"] == 0.7][0]["accuracy"],
                 [r for r in per_seed[-1]["top2_gap"] if r["coverage"] == 0.7][0]["accuracy"]),
              flush=True)

    mean = {}
    for axis in ("max_proba", "top2_gap"):
        rows = []
        for i, cov in enumerate(COVERAGES):
            accs = [ps[axis][i]["accuracy"] for ps in per_seed]
            thrs = [ps[axis][i]["threshold"] for ps in per_seed]
            rows.append({"coverage": cov, "n": per_seed[0][axis][i]["n"],
                         "accuracy_mean": round(float(np.mean(accs)), 4),
                         "accuracy_std": round(float(np.std(accs)), 4),
                         "threshold_mean": round(float(np.mean(thrs)), 4)})
        mean[axis] = rows

    out = {
        "model": name, "lr": args.lr, "seeds": seeds,
        "n_external": int(len(ex)),
        "per_seed": per_seed, "mean_by_coverage": mean,
        "caveat": ("곡선만 낸다. 임계값을 이 외부셋에서 고르면 외부가 검증셋이 "
                   "아니게 된다. 운영 임계값은 커버리지 목표를 먼저 정하고 "
                   "학습셋 OOF 에서 잡는다."),
    }
    os.makedirs(OUTDIR, exist_ok=True)
    with open(os.path.join(OUTDIR, "dl16_m1_abstention.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n%-12s%8s%12s%12s" % ("축", "커버리지", "정확도", "±"))
    for axis, rows in mean.items():
        for r in rows:
            print("%-12s%8.0f%%%12.4f%12.4f"
                  % (axis, r["coverage"] * 100, r["accuracy_mean"], r["accuracy_std"]))
    print("\n[report] dl16_m1_abstention.json")


if __name__ == "__main__":
    main()
