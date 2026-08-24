"""S06C — Early stopping vs 고정 epoch 비교 (Nested split).

S06B ablation 에서 epochs=12 가 최적임을 찾았다(Macro F1 0.7194).
그러나 이 값은 '이 데이터에 맞춰 고른 값'이다. 데이터가 바뀌면 다시 탐색해야
하므로, early stopping 으로 그 의존을 없앨 수 있는지 확인한다.

핵심 — 누수를 막는 nested split
    Early stopping 의 정지 시점을 outer val 로 판단하면 val 이 학습 과정에
    개입하므로 성능이 부풀려진다. 따라서 outer fold 의 train 을 다시
    inner train / inner val 로 쪼개고, inner val 로만 정지 시점을 정한다.
    outer val 은 최종 평가에만 쓴다.

        전체 900건
          └ outer 5-fold  →  train 720 / val 180   (평가용)
                             └ inner split → 648 / 72   (정지 판단용)

    inner val 비율 10%. 900건 규모에서 train 을 더 떼면 손해가 크다.

비교 대상 (같은 outer fold, 같은 seed)
    A. 고정 epoch 12          — S06B 최적값, inner split 없이 train 720 전체 사용
    B. Early stopping         — max 30 epoch, patience 3, inner val Macro F1 기준
    C. 고정 epoch 12 (축소)    — train 648 만 사용. B와 데이터량을 맞춘 대조군

C 를 넣은 이유: B 가 A 보다 나쁘게 나오면 'early stopping 이 나쁜 것'인지
'train 이 648 로 줄어서 나쁜 것'인지 구분할 수 없다. C 가 그 교란을 분리한다.
"""
import argparse
import json
import os
import re
import time

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedKFold, train_test_split
from sklearn.preprocessing import LabelEncoder
from torch.utils.data import DataLoader, Dataset

DATA = os.environ.get("DL_DATA", "/workspace/dl/data/business_taxonomy.parquet")
OUTDIR = os.environ.get("DL_OUT", "/workspace/dl/reports")
MODEL = "klue/roberta-base"
MIN_SUPPORT = 3

BASELINE_ML = 0.6428      # TF-IDF + LinearSVM
BASELINE_DL12 = 0.7194    # S06B 최적 (epochs=12, train 720 전체)

CFG = {"lr": 3e-5, "batch": 16, "max_len": 256, "class_weight": True}
FIXED_EPOCHS = 12
MAX_EPOCHS = 30
PATIENCE = 3
INNER_VAL_RATIO = 0.10


def coarsen(v):
    if not isinstance(v, str) or not v.strip():
        return None
    stripped = re.sub(r"\([^)]*\)", "", v)
    first = stripped.split(",")[0].strip()
    return first if first else None


class TextDS(Dataset):
    def __init__(self, texts, labels, tok, max_len):
        self.enc = tok(list(texts), truncation=True, padding="max_length",
                       max_length=max_len, return_tensors="pt")
        self.labels = torch.tensor(labels, dtype=torch.long)

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, i):
        item = {k: v[i] for k, v in self.enc.items()}
        item["labels"] = self.labels[i]
        return item


def make_model(n_cls):
    from transformers import AutoModelForSequenceClassification
    return AutoModelForSequenceClassification.from_pretrained(
        MODEL, num_labels=n_cls).cuda()


def predict(model, dl):
    model.eval()
    out = []
    with torch.no_grad():
        for b in dl:
            b = {k: v.cuda() for k, v in b.items()}
            b.pop("labels")
            out.append(model(**b).logits.argmax(-1).cpu().numpy())
    model.train()
    return np.concatenate(out)


def train_eval(tr_t, tr_y, te_t, te_y, n_cls, seed, cw,
               epochs=None, inner=None):
    """inner=(iv_t, iv_y) 를 주면 early stopping, 없으면 고정 epoch."""
    from transformers import AutoTokenizer
    torch.manual_seed(seed)
    np.random.seed(seed)
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = make_model(n_cls)

    tr_dl = DataLoader(TextDS(tr_t, tr_y, tok, CFG["max_len"]),
                       batch_size=CFG["batch"], shuffle=True)
    te_dl = DataLoader(TextDS(te_t, te_y, tok, CFG["max_len"]),
                       batch_size=CFG["batch"] * 2, shuffle=False)
    iv_dl = None
    if inner is not None:
        iv_dl = DataLoader(TextDS(inner[0], inner[1], tok, CFG["max_len"]),
                           batch_size=CFG["batch"] * 2, shuffle=False)

    n_ep = epochs if epochs else MAX_EPOCHS
    opt = torch.optim.AdamW(model.parameters(), lr=CFG["lr"], weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=CFG["lr"], total_steps=len(tr_dl) * n_ep, pct_start=0.1)
    w = torch.tensor(cw, dtype=torch.float).cuda() if CFG["class_weight"] else None
    lossf = torch.nn.CrossEntropyLoss(weight=w)

    best_f1, best_state, best_ep, bad = -1.0, None, 0, 0
    model.train()
    for ep in range(1, n_ep + 1):
        for b in tr_dl:
            b = {k: v.cuda() for k, v in b.items()}
            y = b.pop("labels")
            loss = lossf(model(**b).logits, y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()

        if iv_dl is not None:
            p = predict(model, iv_dl)
            f1 = f1_score(inner[1], p, average="macro", zero_division=0)
            if f1 > best_f1:
                best_f1, best_ep, bad = f1, ep, 0
                best_state = {k: v.detach().cpu().clone()
                              for k, v in model.state_dict().items()}
            else:
                bad += 1
                if bad >= PATIENCE:
                    break

    if iv_dl is not None and best_state is not None:
        model.load_state_dict(best_state)

    preds = predict(model, te_dl)
    stopped = best_ep if iv_dl is not None else (epochs or n_ep)
    del model, best_state
    torch.cuda.empty_cache()
    return preds, stopped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--tag", default="v1")
    args = ap.parse_args()

    t = pd.read_parquet(DATA)
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)].reset_index(drop=True)

    X = sub["text_for_model"].fillna("").astype(str).values
    le = LabelEncoder()
    y = le.fit_transform(sub["support_type"].values)
    n_cls = len(le.classes_)
    counts = np.bincount(y, minlength=n_cls)
    cw = len(y) / (n_cls * np.maximum(counts, 1))

    print("Early stopping vs 고정 epoch — nested split")
    print("데이터 %d건 / %d클래스 / outer %d-fold / inner val %.0f%%"
          % (len(X), n_cls, args.folds, INNER_VAL_RATIO * 100))
    print("설정: %s" % CFG)
    print("기준선 — ML %.4f / DL(epochs12, train720) %.4f"
          % (BASELINE_ML, BASELINE_DL12))
    print(flush=True)

    skf = StratifiedKFold(n_splits=args.folds, shuffle=True, random_state=args.seed)
    oof = {"A_fixed12_full": np.zeros(len(y), dtype=int),
           "B_earlystop": np.zeros(len(y), dtype=int),
           "C_fixed12_reduced": np.zeros(len(y), dtype=int)}
    stop_epochs, sizes = [], {}
    t0 = time.time()

    for k, (tr, te) in enumerate(skf.split(X, y), 1):
        # inner split — 정지 시점 판단 전용. 층화 유지가 안 되는 희소 클래스는
        # stratify 실패하므로 예외 시 무작위 분할로 폴백한다.
        try:
            itr, iva = train_test_split(
                tr, test_size=INNER_VAL_RATIO, random_state=args.seed,
                stratify=y[tr])
        except ValueError:
            itr, iva = train_test_split(
                tr, test_size=INNER_VAL_RATIO, random_state=args.seed)

        sizes = {"outer_train": len(tr), "outer_val": len(te),
                 "inner_train": len(itr), "inner_val": len(iva)}
        print("[fold %d] outer train %d / val %d  |  inner train %d / val %d"
              % (k, len(tr), len(te), len(itr), len(iva)), flush=True)

        # A. 고정 12, train 720 전체
        p, _ = train_eval(X[tr], y[tr], X[te], y[te], n_cls, args.seed, cw,
                          epochs=FIXED_EPOCHS)
        oof["A_fixed12_full"][te] = p
        f1a = f1_score(y[te], p, average="macro", zero_division=0)

        # B. Early stopping (inner val 로 정지)
        p, se = train_eval(X[itr], y[itr], X[te], y[te], n_cls, args.seed, cw,
                           inner=(X[iva], y[iva]))
        oof["B_earlystop"][te] = p
        stop_epochs.append(se)
        f1b = f1_score(y[te], p, average="macro", zero_division=0)

        # C. 고정 12, train 축소(648) — B 와 데이터량 동일
        p, _ = train_eval(X[itr], y[itr], X[te], y[te], n_cls, args.seed, cw,
                          epochs=FIXED_EPOCHS)
        oof["C_fixed12_reduced"][te] = p
        f1c = f1_score(y[te], p, average="macro", zero_division=0)

        print("    A(고정12/720) %.4f   B(조기종료@%dep/648) %.4f   C(고정12/648) %.4f   [%.0fs]"
              % (f1a, se, f1b, f1c, time.time() - t0), flush=True)

    results = {}
    for name, p in oof.items():
        results[name] = {
            "accuracy": round(float(accuracy_score(y, p)), 4),
            "macro_f1": round(float(f1_score(y, p, average="macro", zero_division=0)), 4),
            "weighted_f1": round(float(f1_score(y, p, average="weighted", zero_division=0)), 4),
        }

    print()
    print("=" * 78)
    print("%-30s%10s%10s%12s" % ("설정", "acc", "macroF1", "vs ML기준선"))
    print("-" * 78)
    print("%-30s%10.4f%10.4f%12s" % ("TF-IDF+LinearSVM (ML기준선)", 0.7567, BASELINE_ML, "-"))
    label = {"A_fixed12_full": "A. 고정 12 epoch (train 720)",
             "B_earlystop": "B. Early stopping (train 648)",
             "C_fixed12_reduced": "C. 고정 12 epoch (train 648)"}
    for n in ("A_fixed12_full", "B_earlystop", "C_fixed12_reduced"):
        r = results[n]
        print("%-30s%10.4f%10.4f%+12.4f"
              % (label[n], r["accuracy"], r["macro_f1"], r["macro_f1"] - BASELINE_ML))
    print("=" * 78)
    print("조기 종료 시점 (fold별): %s  평균 %.1f epoch"
          % (stop_epochs, float(np.mean(stop_epochs))))
    print()

    a, b, c = (results["A_fixed12_full"]["macro_f1"],
               results["B_earlystop"]["macro_f1"],
               results["C_fixed12_reduced"]["macro_f1"])
    print("해석")
    print("  B vs C (%+.4f) — 같은 데이터량에서 조기종료의 순수 효과" % (b - c))
    print("  A vs C (%+.4f) — train 720 → 648 축소의 손실" % (c - a))
    print("  B vs A (%+.4f) — 실전에서 조기종료를 쓸 때의 최종 손익" % (b - a))
    verdict = ("조기종료 채택 (데이터량 손실을 감안해도 이득)" if b >= a
               else ("조기종료가 데이터량 손실을 보상하지 못함 — 고정 epoch 유지"
                     if b > c else "조기종료 자체가 불리 — 고정 epoch 유지"))
    print("  결론: %s" % verdict)

    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "s06c_m1_dl_early_stopping_%s.json" % args.tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "model": MODEL, "config": CFG,
            "n_rows": int(len(X)), "n_classes": int(n_cls),
            "outer_folds": args.folds, "inner_val_ratio": INNER_VAL_RATIO,
            "split_sizes_last_fold": sizes,
            "fixed_epochs": FIXED_EPOCHS,
            "early_stop": {"max_epochs": MAX_EPOCHS, "patience": PATIENCE,
                           "monitor": "inner val macro_f1",
                           "stopped_at": [int(s) for s in stop_epochs],
                           "mean_stop_epoch": round(float(np.mean(stop_epochs)), 1)},
            "baselines": {"ml_linearsvm": BASELINE_ML, "dl_epochs12": BASELINE_DL12},
            "results": results,
            "deltas": {"B_minus_C": round(b - c, 4), "C_minus_A": round(c - a, 4),
                       "B_minus_A": round(b - a, 4)},
            "verdict": verdict,
            "total_seconds": round(time.time() - t0, 1),
            "note": ("nested split — inner val 로만 정지 시점을 정하고 outer val 은 "
                     "평가에만 사용. C 는 B 와 데이터량을 맞춘 대조군."),
        }, f, ensure_ascii=False, indent=2)
    print("\n[report] %s  (총 %.0f분)" % (out, (time.time() - t0) / 60))


if __name__ == "__main__":
    main()
