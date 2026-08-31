"""DL20 — 모델 1(KLUE-BERT) 최종 서빙 weight export.

dl12_m1_candidates.py 가 이미 고른 설정을 그대로 재사용한다. 새 탐색·새
설정을 만들지 않는다.

    모델      klue/bert-base (성능결과서 1장 채택 모델)
    lr        5e-5  (dl12 내부 CV 로 고른 값. reports/dl12_m1_candidates_dl.json
              의 KLUE-BERT.lr)
    나머지    epochs=8 batch=16 max_len=256 class_weight=True — dl12.FIXED 그대로
    seed      42 — dl12 의 내부 CV seed 및 저장소 전역 기본 seed(m2/m3)와 동일

dl12 는 CV 로 "어떤 모델이 나은가"를 재고, dl07/dl09(m1_apply) 는
평가·적용까지 하지만 **가중치를 저장하지 않는다.** 이 스크립트가 하는
새로운 일은 하나뿐이다 — 같은 설정으로 학습 전체(1,404건)에 최종 적합해
서빙용 weight 를 디스크에 남기는 것.

외부 131건 평가는 검증이 아니라 **참고 기록**이다(하이퍼파라미터 선택은
이미 내부 CV 로 끝났다 — dl12 docstring 참조). 단일 seed 라 dl12 공표치
(0.8422 ± 0.0072, 3seed 평균)와 정확히 같지 않을 수 있다.
"""
import argparse
import json
import os
import time

import numpy as np
import pandas as pd
import torch
from sklearn.preprocessing import LabelEncoder
from torch.utils.data import DataLoader, Dataset

BUNDLE = os.environ.get("DL_BUNDLE", "/workspace/dl/data/m1_dl_bundle")
OUTDIR = os.environ.get("DL_OUT", "/workspace/dl/reports")
EXPORT_DIR = os.environ.get("DL_EXPORT", "/workspace/dl/models/m1_klue_bert")

MODEL_NAME = "klue/bert-base"
LR = 5e-5          # dl12_m1_candidates_dl.json: results["KLUE-BERT"]["lr"]
SEED = 42
FIXED = {"epochs": 8, "batch": 16, "max_len": 256, "class_weight": True}


class TextDS(Dataset):
    def __init__(self, texts, labels, tok, max_len):
        self.enc = tok(list(texts), truncation=True, padding="max_length",
                       max_length=max_len, return_tensors="pt")
        self.labels = None if labels is None else torch.tensor(labels, dtype=torch.long)

    def __len__(self):
        return self.enc["input_ids"].shape[0]

    def __getitem__(self, i):
        item = {k: v[i] for k, v in self.enc.items()}
        if self.labels is not None:
            item["labels"] = self.labels[i]
        return item


def ext_score(gold, pred):
    gold, pred = np.asarray(gold), np.asarray(pred)
    present = sorted(set(gold))
    from sklearn.metrics import f1_score
    return {
        "n": int(len(gold)),
        "accuracy": round(float((gold == pred).mean()), 4),
        "macro_f1_present": round(float(f1_score(gold, pred, average="macro",
                                                 labels=present, zero_division=0)), 4),
        "n_classes_present": len(present),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--export-dir", default=EXPORT_DIR)
    args = ap.parse_args()

    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    tr = pd.read_parquet(os.path.join(BUNDLE, "train.parquet"))
    ex = pd.read_parquet(os.path.join(BUNDLE, "external.parquet"))
    le = LabelEncoder().fit(tr["label"].values)
    y = le.transform(tr["label"].values)
    n_cls = len(le.classes_)
    counts = np.bincount(y, minlength=n_cls)
    cw = len(y) / (n_cls * np.maximum(counts, 1))
    Xtr = tr["text"].values
    Xex = ex["text"].values
    gold = ex["gold"].values

    print("학습 %d행 / %d클래스 | 외부 %d행 | 모델 %s | lr %.0e | seed %d"
          % (len(tr), n_cls, len(ex), MODEL_NAME, LR, args.seed), flush=True)
    print("고정 설정: %s" % FIXED, flush=True)

    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, num_labels=n_cls).cuda()

    tr_dl = DataLoader(TextDS(Xtr, y, tok, FIXED["max_len"]),
                       batch_size=FIXED["batch"], shuffle=True)
    opt = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=LR, total_steps=len(tr_dl) * FIXED["epochs"], pct_start=0.1)
    w = torch.tensor(cw, dtype=torch.float).cuda() if FIXED["class_weight"] else None
    lossf = torch.nn.CrossEntropyLoss(weight=w)

    model.train()
    for ep in range(FIXED["epochs"]):
        tot = 0.0
        for b in tr_dl:
            b = {k: v.cuda() for k, v in b.items()}
            lab = b.pop("labels")
            loss = lossf(model(**b).logits, lab)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
            tot += float(loss)
        print("  epoch %d/%d  loss %.4f  [%.0fs]"
              % (ep + 1, FIXED["epochs"], tot / len(tr_dl), time.time() - t0), flush=True)

    # ---- 외부 131건 — 참고 기록 (하이퍼파라미터 선택은 dl12 내부 CV 로 이미 끝남) ----
    model.eval()
    te_dl = DataLoader(TextDS(Xex, None, tok, FIXED["max_len"]),
                       batch_size=FIXED["batch"] * 2, shuffle=False)
    probs = []
    with torch.no_grad():
        for b in te_dl:
            b = {k: v.cuda() for k, v in b.items()}
            probs.append(torch.softmax(model(**b).logits, -1).cpu().numpy())
    proba = np.concatenate(probs)
    pred = le.inverse_transform(proba.argmax(axis=1))
    score = ext_score(gold, pred)
    print("\n외부 131건 (참고, 단일 seed=%d)  accuracy %.4f  macroF1(present) %.4f"
          % (args.seed, score["accuracy"], score["macro_f1_present"]), flush=True)
    print("dl12 공표치(3seed 평균) accuracy 0.8422 ± 0.0072 — 비교용")

    # ---- 저장 ----
    os.makedirs(args.export_dir, exist_ok=True)
    model_dir = os.path.join(args.export_dir, "model")
    tok_dir = os.path.join(args.export_dir, "tokenizer")
    model.save_pretrained(model_dir)
    tok.save_pretrained(tok_dir)
    with open(os.path.join(args.export_dir, "label_mapping.json"), "w",
              encoding="utf-8") as f:
        json.dump({"classes": list(le.classes_)}, f, ensure_ascii=False, indent=2)
    print("\n[export] %s" % args.export_dir)

    os.makedirs(OUTDIR, exist_ok=True)
    report = {
        "model": MODEL_NAME, "lr": LR, "seed": args.seed, "fixed": FIXED,
        "n_train": int(len(tr)), "n_classes": int(n_cls),
        "source_of_lr": "dl12_m1_candidates_dl.json:results.KLUE-BERT.lr (내부 CV)",
        "external_131_single_seed": score,
        "external_131_dl12_published_3seed_mean": {"accuracy_mean": 0.8422,
                                                    "accuracy_std": 0.0072},
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "export_dir": args.export_dir,
    }
    with open(os.path.join(OUTDIR, "dl20_m1_final_export.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("[report] %s" % os.path.join(OUTDIR, "dl20_m1_final_export.json"))


if __name__ == "__main__":
    main()
