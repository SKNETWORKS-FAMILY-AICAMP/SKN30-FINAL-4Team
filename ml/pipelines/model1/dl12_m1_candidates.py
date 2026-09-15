"""DL12 — 모델 1 후보 공정비교: 같은 학습셋 · 같은 외부 hold-out (GPU 박스에서 실행).

계획서 2.3 Step 2 / 6절 Priority 1 을 그대로 실행한다.

    필수 후보  TF-IDF+LinearSVM(기준선) · KLUE-RoBERTa · KLUE-BERT ·
               KoELECTRA · SBERT 임베딩+선형분류기
    같은 것    학습 1,404건(19클래스) / StratifiedGroupKFold(program_stem) /
               외부 hold-out 131건 / 지표(macro F1 · accuracy)
    다른 것    모델 구조뿐

왜 이렇게 나눴는가
    ① 내부 그룹 CV — 하이퍼파라미터(lr)를 여기서만 고른다. 외부 hold-out 은
       보지 않는다. 외부로 고르면 그 순간 외부는 검증셋이 아니라 학습셋이 된다.
    ② 외부 hold-out — 고른 설정으로 학습 전체에 재학습해 131건을 맞힌다.
       시드 3개로 평균±표준편차를 낸다. 최종 채택 판단은 이 숫자로 한다.

내부 CV 와 외부 정확도를 같은 수로 비교하지 않는다
    내부는 macro F1(19클래스 균등가중), 외부는 accuracy 와 present-class macro F1
    이다. 외부 131건은 15클래스뿐이고 판로 35 / 컨설팅 25 로 쏠려 있다.
    dl06 이 "CV 0.8337 vs 외부 51.2%" 를 나란히 놓아 혼동을 만든 지점이라
    여기서는 표를 아예 분리해 낸다.

입력
    /workspace/dl/data/model1_canonical/{train,external}.parquet  (dl11 이 로컬에서 만든 것)
"""
import argparse
import json
import os
import time

import numpy as np
import pandas as pd
import torch
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.preprocessing import LabelEncoder
from sklearn.svm import LinearSVC
from torch.utils.data import DataLoader, Dataset

BUNDLE = os.environ.get("DL_BUNDLE", "/workspace/dl/data/model1_canonical")
OUTDIR = os.environ.get("DL_OUT", "/workspace/dl/reports")

TRANSFORMERS = {
    "roberta": "klue/roberta-base",
    "bert": "klue/bert-base",
    "koelectra": "monologg/koelectra-base-v3-discriminator",
}
NICE = {"roberta": "KLUE-RoBERTa", "bert": "KLUE-BERT", "koelectra": "KoELECTRA"}
SBERT = "jhgan/ko-sroberta-multitask"

# dl06 의 기준 설정에서 lr 만 열어 둔다. 나머지를 고정해야 모델 구조 차이를 본다.
FIXED = {"epochs": 8, "batch": 16, "max_len": 256, "class_weight": True}
LR_GRID = [2e-5, 3e-5, 5e-5]


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


def train_predict(name, tr_t, tr_y, te_t, n_cls, lr, seed, cw):
    """한 번 학습하고 te_t 의 logit 을 돌려준다."""
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    torch.manual_seed(seed)
    np.random.seed(seed)
    tok = AutoTokenizer.from_pretrained(name)
    model = AutoModelForSequenceClassification.from_pretrained(
        name, num_labels=n_cls).cuda()

    tr_dl = DataLoader(TextDS(tr_t, tr_y, tok, FIXED["max_len"]),
                       batch_size=FIXED["batch"], shuffle=True)
    te_dl = DataLoader(TextDS(te_t, None, tok, FIXED["max_len"]),
                       batch_size=FIXED["batch"] * 2, shuffle=False)

    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=lr, total_steps=len(tr_dl) * FIXED["epochs"], pct_start=0.1)
    w = torch.tensor(cw, dtype=torch.float).cuda() if FIXED["class_weight"] else None
    lossf = torch.nn.CrossEntropyLoss(weight=w)

    model.train()
    for _ in range(FIXED["epochs"]):
        for b in tr_dl:
            b = {k: v.cuda() for k, v in b.items()}
            y = b.pop("labels")
            loss = lossf(model(**b).logits, y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()

    model.eval()
    out = []
    with torch.no_grad():
        for b in te_dl:
            b = {k: v.cuda() for k, v in b.items()}
            out.append(model(**b).logits.float().cpu().numpy())
    del model, opt, sched
    torch.cuda.empty_cache()
    return np.concatenate(out)


def embed(texts, batch=32, max_len=256):
    """SBERT 평균풀링 임베딩. sentence-transformers 없이 transformers 만 쓴다."""
    from transformers import AutoModel, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(SBERT)
    model = AutoModel.from_pretrained(SBERT).cuda().eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), batch):
            enc = tok(list(texts[i:i + batch]), truncation=True, padding=True,
                      max_length=max_len, return_tensors="pt")
            enc = {k: v.cuda() for k, v in enc.items()}
            h = model(**enc).last_hidden_state
            m = enc["attention_mask"].unsqueeze(-1).float()
            out.append(((h * m).sum(1) / m.sum(1).clamp(min=1e-9)).float().cpu().numpy())
    del model
    torch.cuda.empty_cache()
    return np.concatenate(out)


def cv_macro_f1(fit_predict, X, y, groups, folds, seed):
    sgkf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    oof = np.zeros(len(y), dtype=int)
    for tr, te in sgkf.split(X, y, groups):
        oof[te] = fit_predict(tr, te)
    return {"macro_f1": round(float(f1_score(y, oof, average="macro", zero_division=0)), 4),
            "accuracy": round(float(accuracy_score(y, oof)), 4)}


def ext_score(gold, pred):
    """외부 hold-out 지표. 외부에 없는 클래스를 macro 에 0 으로 넣지 않는다."""
    gold, pred = np.asarray(gold), np.asarray(pred)
    present = sorted(set(gold))
    return {
        "n": int(len(gold)),
        "accuracy": round(float((gold == pred).mean()), 4),
        "macro_f1_present": round(float(f1_score(gold, pred, average="macro",
                                                 labels=present, zero_division=0)), 4),
        "weighted_f1": round(float(f1_score(gold, pred, average="weighted",
                                            zero_division=0)), 4),
        "n_classes_present": len(present),
    }


def per_class(gold, pred):
    gold, pred = np.asarray(gold), np.asarray(pred)
    rows = []
    for L in sorted(set(gold)):
        m = gold == L
        tp = int((pred[m] == L).sum())
        npred = int((pred == L).sum())
        rows.append({"class": L, "n_true": int(m.sum()),
                     "recall": round(tp / m.sum(), 4),
                     "precision": round(tp / npred, 4) if npred else None})
    return sorted(rows, key=lambda r: -r["n_true"])


def confusions(gold, pred, limit=12):
    bad = [(a, b) for a, b in zip(gold, pred) if a != b]
    if not bad:
        return []
    c = pd.Series(bad).value_counts()
    return [{"true": a, "pred": b, "n": int(n)} for (a, b), n in c.head(limit).items()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="tfidf,sbert,roberta,bert,koelectra")
    ap.add_argument("--seeds", default="42,7,2024")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--lr-grid", default="")
    ap.add_argument("--epochs", type=int, default=0)
    ap.add_argument("--tag", default="v1")
    args = ap.parse_args()

    if args.epochs:
        FIXED["epochs"] = args.epochs
    lr_grid = [float(x) for x in args.lr_grid.split(",")] if args.lr_grid else LR_GRID
    seeds = [int(s) for s in args.seeds.split(",")]
    want = [m.strip() for m in args.models.split(",") if m.strip()]

    tr = pd.read_parquet(os.path.join(BUNDLE, "train.parquet"))
    ex = pd.read_parquet(os.path.join(BUNDLE, "external.parquet"))
    le = LabelEncoder().fit(tr["label"].values)
    y = le.transform(tr["label"].values)
    n_cls = len(le.classes_)
    counts = np.bincount(y, minlength=n_cls)
    cw = len(y) / (n_cls * np.maximum(counts, 1))
    stem = pd.Series(tr["group"].values)
    dup = stem.duplicated(keep=False) & (stem != "")
    groups = np.where(dup, stem, "row_" + np.arange(len(tr)).astype(str))
    Xtr = tr["text"].values
    Xex = ex["text"].values
    gold = ex["gold"].values
    ytr_lab = tr["label"].values

    print("학습 %d행 / %d클래스 / 그룹 %d | 외부 %d행 / %d클래스"
          % (len(tr), n_cls, len(set(groups)), len(ex), len(set(gold))), flush=True)
    print("고정 설정: %s | lr 격자 %s | 시드 %s" % (FIXED, lr_grid, seeds), flush=True)

    res = {}
    t0 = time.time()

    # 기준선: TF-IDF + LinearSVM (CPU, ML 쪽과 같은 레시피)
    if "tfidf" in want:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.pipeline import Pipeline

        def mk(seed):
            return Pipeline([
                ("t", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                                      min_df=2, sublinear_tf=True, max_features=60000)),
                ("m", LinearSVC(C=1.0, class_weight="balanced", random_state=seed))])

        cv = cv_macro_f1(
            lambda a, b: le.transform(mk(42).fit(Xtr[a], ytr_lab[a]).predict(Xtr[b])),
            Xtr, y, groups, args.folds, 42)
        exs = []
        for s in seeds:
            p = mk(s).fit(Xtr, ytr_lab).predict(Xex)
            exs.append((ext_score(gold, p), p))
        res["TF-IDF + LinearSVM"] = {
            "family": "ML", "lr": None, "cv": cv,
            "external_per_seed": [e for e, _ in exs],
            "external_pred": list(exs[0][1]),
        }
        print("[TF-IDF+SVM] CV macroF1 %.4f | 외부 acc %.4f (%.0fs)"
              % (cv["macro_f1"], exs[0][0]["accuracy"], time.time() - t0), flush=True)

    # SBERT 임베딩 + 선형분류기
    if "sbert" in want:
        E = embed(np.concatenate([Xtr, Xex]))
        Etr, Eex = E[:len(Xtr)], E[len(Xtr):]
        heads = (("LinearSVM", lambda s: LinearSVC(C=1.0, class_weight="balanced",
                                                   random_state=s)),
                 ("LogReg", lambda s: LogisticRegression(max_iter=3000, C=5.0,
                                                         class_weight="balanced",
                                                         random_state=s)))
        for head, mk2 in heads:
            cv = cv_macro_f1(lambda a, b: mk2(42).fit(Etr[a], y[a]).predict(Etr[b]),
                             Etr, y, groups, args.folds, 42)
            exs = []
            for s in seeds:
                p = le.inverse_transform(mk2(s).fit(Etr, y).predict(Eex))
                exs.append((ext_score(gold, p), p))
            res["SBERT + %s" % head] = {
                "family": "DL(임베딩)", "lr": None, "cv": cv,
                "external_per_seed": [e for e, _ in exs],
                "external_pred": list(exs[0][1]),
            }
            print("[SBERT+%s] CV macroF1 %.4f | 외부 acc %.4f (%.0fs)"
                  % (head, cv["macro_f1"], exs[0][0]["accuracy"],
                     time.time() - t0), flush=True)

    # 트랜스포머 파인튜닝 3종
    for key in [k for k in TRANSFORMERS if k in want]:
        name = TRANSFORMERS[key]
        print("\n[%s] %s — 내부 CV 로 lr 선택" % (key, name), flush=True)
        cvs = {}
        for lr in lr_grid:
            cv = cv_macro_f1(
                lambda a, b: train_predict(name, Xtr[a], y[a], Xtr[b],
                                           n_cls, lr, 42, cw).argmax(1),
                Xtr, y, groups, args.folds, 42)
            cvs[lr] = cv
            print("   lr %.0e  macroF1 %.4f  acc %.4f  [%.0fs]"
                  % (lr, cv["macro_f1"], cv["accuracy"], time.time() - t0), flush=True)
        best_lr = max(cvs, key=lambda k: cvs[k]["macro_f1"])
        exs = []
        for s in seeds:
            logit = train_predict(name, Xtr, y, Xex, n_cls, best_lr, s, cw)
            p = le.inverse_transform(logit.argmax(1))
            exs.append((ext_score(gold, p), p))
            print("   seed %-5d 외부 acc %.4f  macroF1(present) %.4f  [%.0fs]"
                  % (s, exs[-1][0]["accuracy"], exs[-1][0]["macro_f1_present"],
                     time.time() - t0), flush=True)
        res[NICE[key]] = {
            "family": "DL(파인튜닝)", "lr": best_lr, "cv": cvs[best_lr],
            "cv_all_lr": {("%.0e" % k): v for k, v in cvs.items()},
            "external_per_seed": [e for e, _ in exs],
            "external_pred": list(exs[0][1]),
        }

    summary = []
    for k, v in res.items():
        accs = [e["accuracy"] for e in v["external_per_seed"]]
        mf1 = [e["macro_f1_present"] for e in v["external_per_seed"]]
        v["external_mean"] = {
            "accuracy_mean": round(float(np.mean(accs)), 4),
            "accuracy_std": round(float(np.std(accs)), 4),
            "macro_f1_present_mean": round(float(np.mean(mf1)), 4),
            "macro_f1_present_std": round(float(np.std(mf1)), 4),
            "n_seeds": len(accs),
        }
        v["external_per_class_seed0"] = per_class(gold, v["external_pred"])
        v["external_confusions_seed0"] = confusions(gold, v["external_pred"])
        summary.append((k, v["family"], v["cv"]["macro_f1"],
                        v["external_mean"]["accuracy_mean"],
                        v["external_mean"]["accuracy_std"]))

    print("\n" + "=" * 78)
    print("%-24s%-14s%12s%14s%10s" % ("모델", "계열", "내부CV F1", "외부 acc", "±"))
    print("-" * 78)
    for k, fam, cv, m, s in sorted(summary, key=lambda r: -r[3]):
        print("%-24s%-14s%12.4f%14.4f%10.4f" % (k, fam, cv, m, s))

    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "dl12_m1_candidates_%s.json" % args.tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "n_train": int(len(tr)), "n_classes": int(n_cls),
            "n_groups": int(len(set(groups))), "n_external": int(len(ex)),
            "external_classes_present": int(len(set(gold))),
            "folds": args.folds, "seeds": seeds, "fixed": FIXED, "lr_grid": lr_grid,
            "cv": "StratifiedGroupKFold (program_stem), lr 선택에만 사용",
            "external": "M28/M29 정답 131건 — 하이퍼파라미터 선택에 쓰지 않음",
            "gold": list(gold), "results": res,
            "total_minutes": round((time.time() - t0) / 60, 1),
        }, f, ensure_ascii=False, indent=2)
    print("\n[report] %s  (%.1f분)" % (out, (time.time() - t0) / 60))


if __name__ == "__main__":
    main()
