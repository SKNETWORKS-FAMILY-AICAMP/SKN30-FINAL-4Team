"""DL05 — KLUE-RoBERTa 로 Open API 재적용 (M02 의 딥러닝 버전).

M08은 LogisticRegression 으로 Open API 1,570건에 지원성격을 추론했고
판단보류가 70.5%(1,107건)였다. 그런데 DL04(MC Dropout)에서 학습 도메인
내부의 판단보류는 0.3%(3건)에 불과했다. 차이의 원인이 두 가지 섞여 있다.

    ① 모델        LogisticRegression → KLUE-RoBERTa (확신도 자체가 다름)
    ② 데이터 분포  2023 엑셀(학습 도메인) → Open API 원문(도메인 밖)

이 스크립트는 ①을 바꿔서 판단보류가 실제로 줄어드는지 측정한다.
줄어들면 "70.5%는 26클래스 구조 문제가 아니라 모델 표현력 문제"였다는 뜻이고,
안 줄어들면 "도메인 차이가 지배적"이라는 뜻이다.

M02 과의 비교가 성립하도록 조건을 동일하게 맞춘다.
    - 학습: 2023 엑셀 전량(900건, 26클래스)
    - 적용: Open API 1,570건, 원문 있으면 원문 / 없으면 요약문 대체
    - 전처리: M02 조건 B(관인부 제거 + 본문 마커 발췌) — M02 최적
    - 임계값: 0.25 / 0.35 (동일)

DL04 에서 얻은 epistemic 기준도 함께 산출한다. softmax 임계값은 오답을
거의 못 걸러냈지만 epistemic 은 걸러냈으므로(q75에서 정밀도 87.7%),
실제 적용에서도 그런지 확인한다.
"""
import argparse
import json
import os
import re
import time

import numpy as np
import pandas as pd
import torch
from sklearn.preprocessing import LabelEncoder
from torch.utils.data import DataLoader, Dataset

DATA_DIR = os.environ.get("DL_DATA_DIR", "/workspace/dl/data")
OUTDIR = os.environ.get("DL_OUT", "/workspace/dl/reports")
TAX = DATA_DIR + "/business_taxonomy.parquet"
DETAIL = DATA_DIR + "/announcement_detail.parquet"
DOCS = DATA_DIR + "/e01_documents_api.jsonl"

MODEL = "klue/roberta-base"
MIN_SUPPORT = 3
CFG = {"lr": 3e-5, "epochs": 12, "batch": 16, "max_len": 256}  # DL02 최적
T_SAMPLES = 30
HOLD, TRUST = 0.25, 0.35

# M02 (LogisticRegression) 결과 — 비교 기준
M02 = {"model": "TF-IDF + LogisticRegression", "condition": "B (E01 + 전처리)",
       "n_trust": 182, "n_ref": 281, "n_hold": 1107, "hold_rate": 0.705,
       "mean_confidence": 0.2160}

# ---- 전처리 (M02 조건 B 와 동일) ----
LETTERHEAD = re.compile(r"^.{0,40}(공고\s*제\s*20\d{2}[-－]\d+\s*호).*?\n", re.S)
DATE_LINE = re.compile(r"^\s*20\d{2}\s*[.년]\s*\d{1,2}\s*[.월]\s*\d{0,2}\s*[.일]?\s*$", re.M)
SIGNOFF = re.compile(r"^[가-힣()（）\s]{2,20}(장|원장|이사장|청장|본부장)\s*$", re.M)
DASH = re.compile(r"^-{5,}$", re.M)
PAGE_NUM = re.compile(r"^-\s*\d+\s*-$", re.M)
CONTENT_MARK = re.compile(r"(사업\s*개요|추진\s*배경|사업\s*목적|지원\s*목적|모집\s*개요|사업\s*내용)")
ADMIN_MARK = re.compile(r"(신청\s*방법|제출\s*서류|접수\s*방법|문\s*의\s*처|붙\s*임|유의\s*사항|추진\s*절차)")
BULLET = re.compile(r"[□◦※➡▶■●○☞]")


def clean_text(text, budget=900):
    t = LETTERHEAD.sub("", text, count=1)
    for p in (DATE_LINE, SIGNOFF, DASH, PAGE_NUM):
        t = p.sub("", t)
    m = CONTENT_MARK.search(t[:1200])
    if m:
        t = t[m.start():]
    a = ADMIN_MARK.search(t)
    if a and a.start() > 100:
        t = t[:a.start()]
    t = BULLET.sub(" ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()[:budget]


def coarsen(v):
    if not isinstance(v, str) or not v.strip():
        return None
    stripped = re.sub(r"\([^)]*\)", "", v)
    first = stripped.split(",")[0].strip()
    return first if first else None


def load_docs(path):
    best = {}
    if not os.path.exists(path):
        return best
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("n_chars", 0) <= 0:
                continue
            pid = r["announcement_id"]
            if pid not in best or r["n_chars"] > best[pid]["n_chars"]:
                best[pid] = r
    return best


class TextDS(Dataset):
    def __init__(self, texts, tok, max_len):
        self.enc = tok(list(texts), truncation=True, padding="max_length",
                       max_length=max_len, return_tensors="pt")

    def __len__(self):
        return self.enc["input_ids"].shape[0]

    def __getitem__(self, i):
        return {k: v[i] for k, v in self.enc.items()}


def enable_dropout(model):
    n = 0
    for m in model.modules():
        if m.__class__.__name__.startswith("Dropout"):
            m.train()
            n += 1
    return n


def entropy(p, axis=-1, eps=1e-12):
    return -np.sum(p * np.log(p + eps), axis=axis)


def tier(c):
    return "판단보류" if c < HOLD else ("참고용" if c < TRUST else "신뢰")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--samples", type=int, default=T_SAMPLES)
    ap.add_argument("--tag", default="v1")
    args = ap.parse_args()

    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    # ---- 학습: 2023 엑셀 전량 ----
    t = pd.read_parquet(TAX)
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)].reset_index(drop=True)

    Xtr = sub["text_for_model"].fillna("").astype(str).values
    le = LabelEncoder()
    ytr = le.fit_transform(sub["support_type"].values)
    n_cls = len(le.classes_)
    counts = np.bincount(ytr, minlength=n_cls)
    cw = torch.tensor(len(ytr) / (n_cls * np.maximum(counts, 1)),
                      dtype=torch.float).cuda()

    print("학습: %d건 / %d클래스 (2023 엑셀 전량)" % (len(Xtr), n_cls))
    print("설정: %s" % CFG)
    print("비교 대상 — M02(%s): 판단보류 %.1f%% (%d건)"
          % (M02["model"], M02["hold_rate"] * 100, M02["n_hold"]))
    print(flush=True)

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL, num_labels=n_cls).cuda()

    tr_enc = tok(list(Xtr), truncation=True, padding="max_length",
                 max_length=CFG["max_len"], return_tensors="pt")
    tr_ds = torch.utils.data.TensorDataset(
        tr_enc["input_ids"], tr_enc["attention_mask"],
        torch.tensor(ytr, dtype=torch.long))
    tr_dl = DataLoader(tr_ds, batch_size=CFG["batch"], shuffle=True)

    opt = torch.optim.AdamW(model.parameters(), lr=CFG["lr"], weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=CFG["lr"], total_steps=len(tr_dl) * CFG["epochs"], pct_start=0.1)
    lossf = torch.nn.CrossEntropyLoss(weight=cw)

    t0 = time.time()
    model.train()
    for ep in range(CFG["epochs"]):
        for ids, mask, y in tr_dl:
            out = model(input_ids=ids.cuda(), attention_mask=mask.cuda())
            loss = lossf(out.logits, y.cuda())
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
    print("학습 완료 (%.0fs)\n" % (time.time() - t0), flush=True)

    # ---- 적용: Open API ----
    d = pd.read_parquet(DETAIL)
    docs = load_docs(DOCS)
    texts, has_doc = [], []
    for pid, summ, tgt in zip(d["announcement_id"].astype(str),
                              d["summary_text"].fillna(""),
                              d["target_text"].fillna("")):
        r = docs.get(pid)
        if r is None:
            texts.append("%s\n%s" % (summ, tgt))
            has_doc.append(False)
        else:
            texts.append(clean_text(r["text"]))
            has_doc.append(True)
    has_doc = np.array(has_doc)
    print("적용 %d건 (원문 %d / 요약대체 %d)"
          % (len(texts), has_doc.sum(), (~has_doc).sum()), flush=True)

    dl = DataLoader(TextDS(texts, tok, CFG["max_len"]),
                    batch_size=CFG["batch"] * 2, shuffle=False)

    # 결정론적 추론
    model.eval()
    det = []
    with torch.no_grad():
        for b in dl:
            b = {k: v.cuda() for k, v in b.items()}
            det.append(torch.softmax(model(**b).logits, -1).cpu().numpy())
    det = np.concatenate(det, axis=0)

    # MC Dropout — 불확실성
    enable_dropout(model)
    mc = []
    with torch.no_grad():
        for _ in range(args.samples):
            ps = []
            for b in dl:
                b = {k: v.cuda() for k, v in b.items()}
                ps.append(torch.softmax(model(**b).logits, -1).cpu().numpy())
            mc.append(np.concatenate(ps, axis=0))
    P = np.stack(mc, axis=0)
    mean_p = P.mean(axis=0)
    H_total = entropy(mean_p)
    H_alea = entropy(P, axis=-1).mean(axis=0)
    epi = H_total - H_alea

    conf = det.max(1)
    pred = le.classes_[det.argmax(1)]
    tiers = np.array([tier(c) for c in conf])

    n_trust = int((tiers == "신뢰").sum())
    n_ref = int((tiers == "참고용").sum())
    n_hold = int((tiers == "판단보류").sum())

    print()
    print("=" * 74)
    print("%-34s%10s%10s%10s%10s" % ("모델", "신뢰", "참고용", "판단보류", "보류율"))
    print("-" * 74)
    print("%-34s%10d%10d%10d%9.1f%%"
          % ("M02 (LogisticRegression)", M02["n_trust"], M02["n_ref"],
             M02["n_hold"], M02["hold_rate"] * 100))
    print("%-34s%10d%10d%10d%9.1f%%"
          % ("DL05 (KLUE-RoBERTa)", n_trust, n_ref, n_hold, n_hold / len(texts) * 100))
    print("=" * 74)
    print("평균 확신도: M02 %.4f → DL05 %.4f (%+.4f)"
          % (M02["mean_confidence"], conf.mean(), conf.mean() - M02["mean_confidence"]))
    print("사용가능:    M02 %d건 → DL05 %d건 (%+d)"
          % (M02["n_trust"] + M02["n_ref"], n_trust + n_ref,
             (n_trust + n_ref) - (M02["n_trust"] + M02["n_ref"])))

    print()
    print("=== 출처별 확신도 ===")
    for lbl, m in [("원문", has_doc), ("요약대체", ~has_doc)]:
        if m.sum():
            print("  %-8s n=%4d  평균확신 %.4f  보류율 %.1f%%  epistemic %.4f"
                  % (lbl, m.sum(), conf[m].mean(),
                     (tiers[m] == "판단보류").mean() * 100, epi[m].mean()))

    print()
    print("=== 불확실성 분해 (최대 ln%d=%.3f) ===" % (n_cls, np.log(n_cls)))
    print("  전체 %.4f  aleatoric %.4f  epistemic %.4f  (epi비중 %.1f%%)"
          % (H_total.mean(), H_alea.mean(), epi.mean(),
             epi.mean() / H_total.mean() * 100))

    print()
    print("=== 예측 지원성격 분포 (판단보류 제외 상위 10) ===")
    usable = tiers != "판단보류"
    for k, v in pd.Series(pred[usable]).value_counts().head(10).items():
        print("  %-14s%4d건" % (k, v))

    out = pd.DataFrame({
        "announcement_id": d["announcement_id"].values,
        "title": d["title"].values,
        "support_type_pred": pred,
        "confidence": conf,
        "status": tiers,
        "has_document": has_doc,
        "uncertainty_total": H_total,
        "uncertainty_aleatoric": H_alea,
        "uncertainty_epistemic": epi,
    })
    op = os.path.join(DATA_DIR, "openapi_support_type_roberta.parquet")
    out.to_parquet(op, index=False)
    print("\n저장 → %s" % op)

    os.makedirs(OUTDIR, exist_ok=True)
    rp = os.path.join(OUTDIR, "dl05_apply_roberta_%s.json" % args.tag)
    with open(rp, "w", encoding="utf-8") as f:
        json.dump({
            "model": MODEL, "config": CFG, "mc_samples": args.samples,
            "train_rows": int(len(Xtr)), "n_classes": int(n_cls),
            "applied_rows": int(len(texts)),
            "with_document": int(has_doc.sum()),
            "thresholds": {"hold": HOLD, "trust": TRUST},
            "m02_apply_comparison": M02,
            "result": {"n_trust": n_trust, "n_ref": n_ref, "n_hold": n_hold,
                       "hold_rate": round(n_hold / len(texts), 4),
                       "usable": n_trust + n_ref,
                       "mean_confidence": round(float(conf.mean()), 4)},
            "hold_rate_change": round(n_hold / len(texts) - M02["hold_rate"], 4),
            "by_source": {
                "document": {"n": int(has_doc.sum()),
                             "mean_conf": round(float(conf[has_doc].mean()), 4),
                             "hold_rate": round(float((tiers[has_doc] == "판단보류").mean()), 4)},
                "summary": {"n": int((~has_doc).sum()),
                            "mean_conf": round(float(conf[~has_doc].mean()), 4),
                            "hold_rate": round(float((tiers[~has_doc] == "판단보류").mean()), 4)}},
            "uncertainty": {"total": round(float(H_total.mean()), 4),
                            "aleatoric": round(float(H_alea.mean()), 4),
                            "epistemic": round(float(epi.mean()), 4),
                            "epistemic_share_pct": round(float(epi.mean() / H_total.mean() * 100), 1)},
            "predicted_dist": pd.Series(pred[usable]).value_counts().head(15).to_dict(),
            "output": op,
        }, f, ensure_ascii=False, indent=2)
    print("[report] %s  (총 %.0f분)" % (rp, (time.time() - t0) / 60))


if __name__ == "__main__":
    main()
