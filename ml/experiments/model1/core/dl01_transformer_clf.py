"""DL01 — 지원성격 26클래스 분류: Transformer 파인튜닝 (설계서 v3 27장).

기준선은 TF-IDF + LinearSVM (Macro F1 0.6428, Accuracy 0.7567).
이를 넘는지가 이 실험의 유일한 질문이다. 못 넘으면 그것도 결과로 기록한다.

비교 대상
  klue/roberta-base      한국어 표준 벤치마크로 학습된 RoBERTa
  monologg/koelectra-base-v3-discriminator   ELECTRA 계열

검증 설계는 M06과 동일하게 맞춘다 — 그래야 비교가 성립한다.
  - Stratified 5-Fold CV (같은 seed 42, 같은 split)
  - 지원 3건 미만 클래스 제외 → 26클래스 / 900건
  - 입력은 text_for_model (메타 줄 제외, 누수 통제)
  - Macro F1 중심 평가

주의: 900건에 26클래스는 fold당 train 720건이다. 사전학습 모델이라도
데이터가 적어 과적합 위험이 크므로 epoch 수를 보수적으로 잡고
fold마다 모델을 새로 초기화한다.
"""
import argparse
import json
import os
import time

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import accuracy_score, classification_report, f1_score
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import LabelEncoder
from torch.utils.data import DataLoader, Dataset

DATA = os.environ.get("DL_DATA", "/workspace/dl/data/business_taxonomy.parquet")
OUTDIR = os.environ.get("DL_OUT", "/workspace/dl/reports")
MIN_SUPPORT = 3
BASELINE = {"model": "TF-IDF + LinearSVM", "macro_f1": 0.6428, "accuracy": 0.7567}


def coarsen(v):
    """중분류 61종 → 지원성격. M06과 동일 로직."""
    import re
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


def run_fold(model_name, tr_texts, tr_y, te_texts, te_y, n_classes,
             max_len, epochs, batch, lr, seed, class_weight=None):
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.manual_seed(seed)
    np.random.seed(seed)
    tok = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name, num_labels=n_classes).cuda()

    tr_ds = TextDS(tr_texts, tr_y, tok, max_len)
    te_ds = TextDS(te_texts, te_y, tok, max_len)
    tr_dl = DataLoader(tr_ds, batch_size=batch, shuffle=True, drop_last=False)
    te_dl = DataLoader(te_ds, batch_size=batch * 2, shuffle=False)

    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    total = len(tr_dl) * epochs
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=lr, total_steps=total, pct_start=0.1)

    w = None
    if class_weight is not None:
        w = torch.tensor(class_weight, dtype=torch.float).cuda()
    lossf = torch.nn.CrossEntropyLoss(weight=w)

    model.train()
    for _ in range(epochs):
        for b in tr_dl:
            b = {k: v.cuda() for k, v in b.items()}
            labels = b.pop("labels")
            out = model(**b)
            loss = lossf(out.logits, labels)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()

    model.eval()
    preds = []
    with torch.no_grad():
        for b in te_dl:
            b = {k: v.cuda() for k, v in b.items()}
            b.pop("labels")
            preds.append(model(**b).logits.argmax(-1).cpu().numpy())
    del model
    torch.cuda.empty_cache()
    return np.concatenate(preds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+",
                    default=["klue/roberta-base",
                             "monologg/koelectra-base-v3-discriminator"])
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=5)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--max-len", type=int, default=256)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--class-weight", action="store_true",
                    help="클래스 불균형 보정 (LinearSVM의 balanced 와 대응)")
    ap.add_argument("--tag", default="base")
    args = ap.parse_args()

    t = pd.read_parquet(DATA)
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)]

    X = sub["text_for_model"].fillna("").astype(str).values
    le = LabelEncoder()
    y = le.fit_transform(sub["support_type"].values)
    n_classes = len(le.classes_)
    print("데이터 %d건 / %d클래스" % (len(X), n_classes), flush=True)
    print("기준선: %s — Macro F1 %.4f / Acc %.4f"
          % (BASELINE["model"], BASELINE["macro_f1"], BASELINE["accuracy"]), flush=True)
    print("설정: epochs=%d batch=%d lr=%g max_len=%d class_weight=%s"
          % (args.epochs, args.batch, args.lr, args.max_len, args.class_weight), flush=True)
    print(flush=True)

    cw = None
    if args.class_weight:
        counts = np.bincount(y, minlength=n_classes)
        cw = len(y) / (n_classes * np.maximum(counts, 1))

    skf = StratifiedKFold(n_splits=args.folds, shuffle=True, random_state=args.seed)
    results = {}

    for mname in args.models:
        print("=== %s ===" % mname, flush=True)
        oof = np.zeros(len(y), dtype=int)
        t0 = time.time()
        for k, (tr, te) in enumerate(skf.split(X, y), 1):
            p = run_fold(mname, X[tr], y[tr], X[te], y[te], n_classes,
                         args.max_len, args.epochs, args.batch, args.lr,
                         args.seed, cw)
            oof[te] = p
            print("  fold %d/%d  acc %.4f  macroF1 %.4f  (%.0fs)"
                  % (k, args.folds, accuracy_score(y[te], p),
                     f1_score(y[te], p, average="macro", zero_division=0),
                     time.time() - t0), flush=True)

        acc = accuracy_score(y, oof)
        mf1 = f1_score(y, oof, average="macro", zero_division=0)
        wf1 = f1_score(y, oof, average="weighted", zero_division=0)
        rep = classification_report(y, oof, target_names=le.classes_,
                                    output_dict=True, zero_division=0)
        results[mname] = {
            "accuracy": round(float(acc), 4),
            "macro_f1": round(float(mf1), 4),
            "weighted_f1": round(float(wf1), 4),
            "train_seconds": round(time.time() - t0, 1),
            "beats_baseline": bool(mf1 > BASELINE["macro_f1"]),
            "delta_macro_f1": round(float(mf1 - BASELINE["macro_f1"]), 4),
            "per_class": {k: v for k, v in rep.items() if k in le.classes_},
        }
        print("  전체: acc %.4f  macroF1 %.4f  wF1 %.4f  [%.0fs]"
              % (acc, mf1, wf1, results[mname]["train_seconds"]), flush=True)
        print("  기준선 대비: %+.4f  → %s\n"
              % (mf1 - BASELINE["macro_f1"],
                 "개선" if mf1 > BASELINE["macro_f1"] else "미달"), flush=True)

    print("=" * 70)
    print("%-46s%10s%10s" % ("모델", "MacroF1", "vs기준선"))
    print("-" * 70)
    print("%-46s%10.4f%10s" % (BASELINE["model"] + " (기준선)",
                               BASELINE["macro_f1"], "-"))
    for m, r in sorted(results.items(), key=lambda kv: -kv[1]["macro_f1"]):
        print("%-46s%10.4f%+10.4f" % (m, r["macro_f1"], r["delta_macro_f1"]))

    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "dl01_transformer_clf_%s.json" % args.tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "tag": args.tag,
            "n_rows": int(len(X)), "n_classes": int(n_classes),
            "folds": args.folds, "seed": args.seed,
            "hyperparams": {"epochs": args.epochs, "batch": args.batch,
                            "lr": args.lr, "max_len": args.max_len,
                            "class_weight": args.class_weight},
            "baseline": BASELINE,
            "results": results,
            "gpu": torch.cuda.get_device_name(0),
        }, f, ensure_ascii=False, indent=2)
    print("\n[report] %s" % out)


if __name__ == "__main__":
    main()
