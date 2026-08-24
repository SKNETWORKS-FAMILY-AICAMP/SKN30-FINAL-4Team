"""S06B — 승자 모델 하이퍼파라미터 Ablation Study (설계서 v3 27장).

DL01에서 KLUE-RoBERTa가 기준선(TF-IDF+LinearSVM, Macro F1 0.6428)을
넘은 것을 확인했다. 이 스크립트는 그 승자를 대상으로 각 설정이 성능에
얼마나 기여하는지 분리 측정한다.

DL01과의 차이
  S06A: 모델 비교 — "어떤 구조가 나은가"
  S06B: 설정 탐색 — "어떤 값이 최적인가"

방식: One-factor-at-a-time
  기준 설정(base)을 정해두고 축 하나씩만 바꾼다. 격자 전체를 도는 대신
  각 축의 기여를 독립적으로 읽을 수 있고, 실행 횟수가 선형으로 늘어난다.

  base: lr=3e-5, epochs=8, batch=16, max_len=256, class_weight=True,
        fields=all(제목+목적+내용+대상)

축
  lr          1e-5 / 2e-5 / 3e-5 / 5e-5
  epochs      5 / 8 / 12
  max_len     128 / 256 / 512
  batch       8 / 16 / 32
  fields      title / title+purpose / title+purpose+content / all
  class_weight  on / off

주의 — 이 수치는 튜닝에 사용된 값이다.
  같은 5-fold CV로 설정을 고르고 같은 CV로 성능을 보고하면 낙관적으로
  치우친다(선택 편향). 따라서 최종 보고 시 "이 값은 하이퍼파라미터 선택에
  사용된 CV 성능"임을 명시해야 하며, 순수한 일반화 성능으로 제시하면 안 된다.
  DL01의 기준선 비교(0.6428 vs 0.6940)는 튜닝 전 값이므로 그 비교는 유효하다.
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
BASELINE_ML = 0.6428          # TF-IDF + LinearSVM
BASELINE_DL = 0.6940          # S06A 튜닝 전 KLUE-RoBERTa

BASE = {"lr": 3e-5, "epochs": 8, "batch": 16, "max_len": 256,
        "class_weight": True, "fields": "all"}

AXES = {
    "lr": [1e-5, 2e-5, 3e-5, 5e-5],
    "epochs": [5, 8, 12],
    "max_len": [128, 256, 512],
    "batch": [8, 16, 32],
    "fields": ["title", "title_purpose", "title_purpose_content", "all"],
    "class_weight": [True, False],
}

FIELD_SETS = {
    "title": ["title"],
    "title_purpose": ["title", "purpose"],
    "title_purpose_content": ["title", "purpose", "content"],
    "all": ["title", "purpose", "content", "target_text"],
}


def coarsen(v):
    if not isinstance(v, str) or not v.strip():
        return None
    stripped = re.sub(r"\([^)]*\)", "", v)
    first = stripped.split(",")[0].strip()
    return first if first else None


def build_text(df, fields):
    """입력 필드 조합. 메타 줄은 어느 조합에서도 쓰지 않는다(누수 통제)."""
    cols = FIELD_SETS[fields]
    s = df[cols[0]].fillna("").astype(str)
    for c in cols[1:]:
        s = s + "\n" + df[c].fillna("").astype(str)
    return s.str.strip().values


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


def run_fold(tr_t, tr_y, te_t, te_y, n_cls, cfg, seed, cw):
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    torch.manual_seed(seed)
    np.random.seed(seed)
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL, num_labels=n_cls).cuda()

    tr_dl = DataLoader(TextDS(tr_t, tr_y, tok, cfg["max_len"]),
                       batch_size=cfg["batch"], shuffle=True)
    te_dl = DataLoader(TextDS(te_t, te_y, tok, cfg["max_len"]),
                       batch_size=cfg["batch"] * 2, shuffle=False)

    opt = torch.optim.AdamW(model.parameters(), lr=cfg["lr"], weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=cfg["lr"], total_steps=len(tr_dl) * cfg["epochs"], pct_start=0.1)
    w = torch.tensor(cw, dtype=torch.float).cuda() if (cfg["class_weight"] and cw is not None) else None
    lossf = torch.nn.CrossEntropyLoss(weight=w)

    model.train()
    for _ in range(cfg["epochs"]):
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
    preds = []
    with torch.no_grad():
        for b in te_dl:
            b = {k: v.cuda() for k, v in b.items()}
            b.pop("labels")
            preds.append(model(**b).logits.argmax(-1).cpu().numpy())
    del model
    torch.cuda.empty_cache()
    return np.concatenate(preds)


def evaluate(df, y, cfg, n_cls, folds, seed, cw):
    X = build_text(df, cfg["fields"])
    skf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
    oof = np.zeros(len(y), dtype=int)
    for tr, te in skf.split(X, y):
        oof[te] = run_fold(X[tr], y[tr], X[te], y[te], n_cls, cfg, seed, cw)
    return {
        "accuracy": round(float(accuracy_score(y, oof)), 4),
        "macro_f1": round(float(f1_score(y, oof, average="macro", zero_division=0)), 4),
        "weighted_f1": round(float(f1_score(y, oof, average="weighted", zero_division=0)), 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--axes", nargs="+", default=list(AXES.keys()))
    ap.add_argument("--tag", default="v1")
    args = ap.parse_args()

    t = pd.read_parquet(DATA)
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)].reset_index(drop=True)

    le = LabelEncoder()
    y = le.fit_transform(sub["support_type"].values)
    n_cls = len(le.classes_)
    counts = np.bincount(y, minlength=n_cls)
    cw = len(y) / (n_cls * np.maximum(counts, 1))

    print("Ablation Study — %s" % MODEL)
    print("데이터 %d건 / %d클래스 / %d-fold" % (len(sub), n_cls, args.folds))
    print("기준 설정:", BASE)
    print("비교 기준선 — ML %.4f / DL(튜닝전) %.4f" % (BASELINE_ML, BASELINE_DL))
    print(flush=True)

    results = {}
    t0 = time.time()

    print("[기준 설정 측정]", flush=True)
    base_res = evaluate(sub, y, BASE, n_cls, args.folds, args.seed, cw)
    results["__base__"] = {"config": dict(BASE), **base_res}
    print("  macroF1 %.4f  acc %.4f  (%.0fs)\n"
          % (base_res["macro_f1"], base_res["accuracy"], time.time() - t0), flush=True)

    for axis in args.axes:
        if axis not in AXES:
            continue
        print("[축: %s]" % axis, flush=True)
        results[axis] = {}
        for val in AXES[axis]:
            if val == BASE[axis]:
                results[axis][str(val)] = {**base_res, "is_base": True,
                                           "delta": 0.0}
                print("  %-24s macroF1 %.4f  (기준)" % (str(val), base_res["macro_f1"]), flush=True)
                continue
            cfg = dict(BASE)
            cfg[axis] = val
            r = evaluate(sub, y, cfg, n_cls, args.folds, args.seed, cw)
            d = r["macro_f1"] - base_res["macro_f1"]
            results[axis][str(val)] = {**r, "is_base": False, "delta": round(d, 4)}
            print("  %-24s macroF1 %.4f  (%+.4f)  [%.0fs]"
                  % (str(val), r["macro_f1"], d, time.time() - t0), flush=True)
        print(flush=True)

    # 축별 최적값과 영향력
    print("=" * 72)
    print("%-14s%22s%12s%12s" % ("축", "최적값", "macroF1", "영향폭"))
    print("-" * 72)
    best_cfg = dict(BASE)
    for axis in args.axes:
        if axis not in results or axis == "__base__":
            continue
        vals = results[axis]
        best_v = max(vals, key=lambda k: vals[k]["macro_f1"])
        f1s = [v["macro_f1"] for v in vals.values()]
        print("%-14s%22s%12.4f%12.4f"
              % (axis, best_v, vals[best_v]["macro_f1"], max(f1s) - min(f1s)))
        # 문자열로 저장된 값을 원 타입으로 복원
        for orig in AXES[axis]:
            if str(orig) == best_v:
                best_cfg[axis] = orig
                break

    print()
    print("축별 최적값 조합:", best_cfg)
    print("(주의: one-factor-at-a-time 이므로 이 조합이 실제 최적이라는 보장은 없다)")

    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "s06b_m1_dl_ablation_%s.json" % args.tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "model": MODEL, "n_rows": int(len(sub)), "n_classes": int(n_cls),
            "folds": args.folds, "seed": args.seed,
            "method": "one-factor-at-a-time",
            "base_config": BASE, "base_result": base_res,
            "baselines": {"ml_linearsvm": BASELINE_ML, "dl_untuned": BASELINE_DL},
            "axes": {k: v for k, v in results.items() if k != "__base__"},
            "best_per_axis": {k: str(v) for k, v in best_cfg.items()},
            "total_seconds": round(time.time() - t0, 1),
            "caveat": ("이 수치는 하이퍼파라미터 선택에 사용된 CV 성능이다. "
                       "선택 편향이 있으므로 순수 일반화 성능으로 제시하면 안 된다."),
        }, f, ensure_ascii=False, indent=2)
    print("\n[report] %s  (총 %.0f분)" % (out, (time.time() - t0) / 60))


if __name__ == "__main__":
    main()
