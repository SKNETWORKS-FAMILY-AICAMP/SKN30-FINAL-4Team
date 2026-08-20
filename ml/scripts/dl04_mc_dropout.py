"""DL04 — MC Dropout 으로 불확실성 분해 (BNN 접근 A안).

문제: 지원성격 분류의 판단보류가 70.5%(1,107건)다. 지금은 max(softmax) < 0.25
라는 단일 임계값으로 자르는데, 확신도가 낮은 이유를 구분하지 못한다.

    융자 0.28 / 보증 0.26          두 클래스가 실제로 유사 → aleatoric
    26개가 0.03~0.28 로 흩어짐      모델이 본 적 없는 문서 → epistemic

전자는 데이터를 더 모아도 안 갈라지고, 후자는 라벨을 늘리면 해결된다.
대응이 정반대라 구분이 필요하다.

MC Dropout (Gal & Ghahramani, 2016)
    추론 시에도 dropout 을 켜두고 T회 반복하면, 각 회차가 서로 다른
    서브네트워크의 예측이 된다. 이 T개 예측의 분포가 베이지안 사후분포의
    근사가 된다. 재학습이 필요 없어 B안(변분 레이어)보다 싸게 검증할 수 있다.

불확실성 분해
    예측 분포 p_t(y|x), t=1..T 에 대해
      전체(predictive)  H[E_t p_t]            평균 분포의 엔트로피
      aleatoric         E_t H[p_t]            각 분포 엔트로피의 평균
      epistemic         전체 - aleatoric      = 상호정보량(BALD)

    epistemic 이 크면 "모델이 모르는 것"이므로 라벨 확대가 유효하다.
    aleatoric 이 크면 "클래스 자체가 겹치는 것"이므로 체계 재정의가 필요하다.

누수 통제
    BNN/불확실성 산출에 쓰는 확률은 반드시 OOF(out-of-fold) 예측이어야 한다.
    학습에 쓴 데이터로 만든 확률은 모델이 정답을 외운 상태라 비정상적으로
    높게 나오고, 그것을 입력으로 쓰면 이후 단계가 잘못 학습된다.
    따라서 fold 별로 학습한 모델이 자기 val 만 예측한다.
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
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import LabelEncoder
from torch.utils.data import DataLoader, Dataset

DATA = os.environ.get("DL_DATA", "/workspace/dl/data/business_taxonomy.parquet")
OUTDIR = os.environ.get("DL_OUT", "/workspace/dl/reports")
MODEL = "klue/roberta-base"
MIN_SUPPORT = 3

# DL02 ablation 최적 설정
CFG = {"lr": 3e-5, "epochs": 12, "batch": 16, "max_len": 256, "class_weight": True}
T_SAMPLES = 30          # MC 반복 횟수
BASELINE_ML = 0.6428


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


def enable_dropout(model):
    """eval 모드에서도 dropout 만 학습 모드로 유지 — MC Dropout 의 핵심."""
    n = 0
    for m in model.modules():
        if m.__class__.__name__.startswith("Dropout"):
            m.train()
            n += 1
    return n


def entropy(p, axis=-1, eps=1e-12):
    return -np.sum(p * np.log(p + eps), axis=axis)


def mc_predict(model, dl, T):
    """T회 반복 추론. 반환 (T, N, C) 확률."""
    model.eval()
    n_do = enable_dropout(model)
    if n_do == 0:
        raise RuntimeError("dropout 레이어를 찾지 못했다 — MC Dropout 불가")
    outs = []
    with torch.no_grad():
        for _ in range(T):
            probs = []
            for b in dl:
                b = {k: v.cuda() for k, v in b.items()}
                b.pop("labels")
                logits = model(**b).logits
                probs.append(torch.softmax(logits, dim=-1).cpu().numpy())
            outs.append(np.concatenate(probs, axis=0))
    return np.stack(outs, axis=0)


def train_fold(tr_t, tr_y, n_cls, seed, cw):
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    torch.manual_seed(seed)
    np.random.seed(seed)
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL, num_labels=n_cls).cuda()
    dl = DataLoader(TextDS(tr_t, tr_y, tok, CFG["max_len"]),
                    batch_size=CFG["batch"], shuffle=True)
    opt = torch.optim.AdamW(model.parameters(), lr=CFG["lr"], weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=CFG["lr"], total_steps=len(dl) * CFG["epochs"], pct_start=0.1)
    w = torch.tensor(cw, dtype=torch.float).cuda() if CFG["class_weight"] else None
    lossf = torch.nn.CrossEntropyLoss(weight=w)
    model.train()
    for _ in range(CFG["epochs"]):
        for b in dl:
            b = {k: v.cuda() for k, v in b.items()}
            y = b.pop("labels")
            loss = lossf(model(**b).logits, y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
    return model, tok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--samples", type=int, default=T_SAMPLES)
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

    print("MC Dropout 불확실성 분해 — %s" % MODEL)
    print("데이터 %d건 / %d클래스 / %d-fold / MC %d회"
          % (len(X), n_cls, args.folds, args.samples))
    print("설정: %s\n" % CFG, flush=True)

    # OOF 저장 — 학습에 쓰이지 않은 데이터에 대한 예측만 모은다
    oof_mean = np.zeros((len(y), n_cls))
    oof_det = np.zeros((len(y), n_cls))     # dropout 끈 단일 추론(기존 방식)
    unc = {k: np.zeros(len(y)) for k in ("total", "aleatoric", "epistemic", "std_top")}

    skf = StratifiedKFold(n_splits=args.folds, shuffle=True, random_state=args.seed)
    t0 = time.time()
    for k, (tr, te) in enumerate(skf.split(X, y), 1):
        model, tok = train_fold(X[tr], y[tr], n_cls, args.seed, cw)
        te_dl = DataLoader(TextDS(X[te], y[te], tok, CFG["max_len"]),
                           batch_size=CFG["batch"] * 2, shuffle=False)

        # 기존 방식 — dropout 끈 결정론적 추론
        model.eval()
        det = []
        with torch.no_grad():
            for b in te_dl:
                b = {k2: v.cuda() for k2, v in b.items()}
                b.pop("labels")
                det.append(torch.softmax(model(**b).logits, -1).cpu().numpy())
        oof_det[te] = np.concatenate(det, axis=0)

        # MC Dropout
        P = mc_predict(model, te_dl, args.samples)      # (T, n, C)
        mean_p = P.mean(axis=0)
        oof_mean[te] = mean_p
        H_total = entropy(mean_p)                       # H[E p]
        H_alea = entropy(P, axis=-1).mean(axis=0)       # E H[p]
        unc["total"][te] = H_total
        unc["aleatoric"][te] = H_alea
        unc["epistemic"][te] = H_total - H_alea         # BALD
        unc["std_top"][te] = P.max(axis=-1).std(axis=0)

        f1 = f1_score(y[te], mean_p.argmax(1), average="macro", zero_division=0)
        print("  fold %d/%d  macroF1(MC) %.4f  (%.0fs)"
              % (k, args.folds, f1, time.time() - t0), flush=True)
        del model
        torch.cuda.empty_cache()

    # ---- 성능 비교: 단일 추론 vs MC 평균 ----
    det_pred, mc_pred = oof_det.argmax(1), oof_mean.argmax(1)
    perf = {
        "deterministic": {
            "accuracy": round(float(accuracy_score(y, det_pred)), 4),
            "macro_f1": round(float(f1_score(y, det_pred, average="macro", zero_division=0)), 4)},
        "mc_dropout_mean": {
            "accuracy": round(float(accuracy_score(y, mc_pred)), 4),
            "macro_f1": round(float(f1_score(y, mc_pred, average="macro", zero_division=0)), 4)},
    }
    print()
    print("=== 예측 성능 ===")
    print("  단일 추론(기존)   acc %.4f  macroF1 %.4f"
          % (perf["deterministic"]["accuracy"], perf["deterministic"]["macro_f1"]))
    print("  MC 평균(%d회)     acc %.4f  macroF1 %.4f"
          % (args.samples, perf["mc_dropout_mean"]["accuracy"],
             perf["mc_dropout_mean"]["macro_f1"]))

    # ---- 불확실성 분해 ----
    correct = (mc_pred == y)
    max_unc = float(np.log(n_cls))
    print()
    print("=== 불확실성 분해 (최대 엔트로피 = ln%d = %.3f) ===" % (n_cls, max_unc))
    print("%-14s%10s%12s%12s%12s" % ("구분", "전체", "aleatoric", "epistemic", "epi비중"))
    print("-" * 62)
    for label, m in [("전체", np.ones(len(y), bool)),
                     ("맞춘 예측", correct), ("틀린 예측", ~correct)]:
        tot, al, ep = unc["total"][m].mean(), unc["aleatoric"][m].mean(), unc["epistemic"][m].mean()
        print("%-14s%10.4f%12.4f%12.4f%11.1f%%"
              % (label, tot, al, ep, ep / tot * 100 if tot else 0))

    # ---- 기존 임계값(0.25) 기준 판단보류 집단 분석 ----
    det_conf = oof_det.max(1)
    hold = det_conf < 0.25
    print()
    print("=== 기존 판단보류(max softmax<0.25) 집단의 불확실성 ===")
    print("  판단보류 %d건(%.1f%%) / 판단가능 %d건"
          % (hold.sum(), hold.mean() * 100, (~hold).sum()))
    for label, m in [("판단보류", hold), ("판단가능", ~hold)]:
        if m.sum() == 0:
            continue
        tot, al, ep = unc["total"][m].mean(), unc["aleatoric"][m].mean(), unc["epistemic"][m].mean()
        acc = correct[m].mean()
        print("  %-10s 전체 %.4f  alea %.4f  epi %.4f  epi비중 %.1f%%  실제정확도 %.1f%%"
              % (label, tot, al, ep, ep / tot * 100 if tot else 0, acc * 100))

    # ---- epistemic 기준으로 보류를 다시 정하면? ----
    print()
    print("=== 보류 기준 비교: max softmax vs epistemic ===")
    print("%-22s%10s%12s%12s" % ("기준", "커버리지", "정밀도", "보류율"))
    print("-" * 58)
    for th in (0.20, 0.25, 0.30, 0.35):
        m = det_conf >= th
        print("%-22s%9.1f%%%11.1f%%%11.1f%%"
              % ("softmax >= %.2f" % th, m.mean() * 100,
                 correct[m].mean() * 100 if m.sum() else 0, (~m).mean() * 100))
    for q in (0.9, 0.75, 0.6, 0.5):
        th = float(np.quantile(unc["epistemic"], q))
        m = unc["epistemic"] <= th
        print("%-22s%9.1f%%%11.1f%%%11.1f%%"
              % ("epistemic <= q%.0f" % (q * 100), m.mean() * 100,
                 correct[m].mean() * 100 if m.sum() else 0, (~m).mean() * 100))

    # ---- 결론 ----
    ep_share = float(unc["epistemic"].mean() / unc["total"].mean() * 100)
    if ep_share >= 40:
        verdict = ("epistemic 비중 %.1f%% — 모델이 '본 적 없어서' 모르는 비중이 크다. "
                   "라벨 확대가 유효하고 B안(BNN layer)도 기대할 만하다." % ep_share)
    elif ep_share >= 20:
        verdict = ("epistemic 비중 %.1f%% — 중간. 라벨 확대와 클래스 체계 정리를 "
                   "함께 검토해야 한다." % ep_share)
    else:
        verdict = ("epistemic 비중 %.1f%% — 대부분 aleatoric(클래스 자체가 겹침). "
                   "데이터를 늘려도 한계가 있고 26클래스 체계 재정의가 근본 해법이다."
                   % ep_share)
    print()
    print("결론: %s" % verdict)

    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "dl04_mc_dropout_%s.json" % args.tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "model": MODEL, "config": CFG, "mc_samples": args.samples,
            "n_rows": int(len(X)), "n_classes": int(n_cls),
            "folds": args.folds, "seed": args.seed,
            "performance": perf,
            "baseline_ml_macro_f1": BASELINE_ML,
            "max_entropy": round(max_unc, 4),
            "uncertainty_mean": {k: round(float(v.mean()), 4) for k, v in unc.items()},
            "uncertainty_by_correctness": {
                "correct": {k: round(float(v[correct].mean()), 4) for k, v in unc.items()},
                "wrong": {k: round(float(v[~correct].mean()), 4) for k, v in unc.items()}},
            "hold_group_analysis": {
                "n_hold": int(hold.sum()), "hold_rate": round(float(hold.mean()), 4),
                "hold_accuracy": round(float(correct[hold].mean()), 4) if hold.sum() else None,
                "usable_accuracy": round(float(correct[~hold].mean()), 4) if (~hold).sum() else None,
                "hold_epistemic_share": round(float(
                    unc["epistemic"][hold].mean() / unc["total"][hold].mean() * 100), 1) if hold.sum() else None},
            "epistemic_share_pct": round(ep_share, 1),
            "verdict": verdict,
            "total_seconds": round(time.time() - t0, 1),
            "leak_control": "OOF 예측만 사용 — fold별 모델이 자기 val 만 예측",
        }, f, ensure_ascii=False, indent=2)
    print("[report] %s  (총 %.0f분)" % (out, (time.time() - t0) / 60))


if __name__ == "__main__":
    main()
