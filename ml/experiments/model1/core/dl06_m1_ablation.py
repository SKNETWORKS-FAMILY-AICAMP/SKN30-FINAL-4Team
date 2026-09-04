"""DL06 — 지원성격 19클래스 분류: KLUE-RoBERTa 파인튜닝 + Ablation (모델 1 재학습).

dl01~dl05 는 2023 단독·26클래스 시절 산출물이다(기준선 TF-IDF+LinearSVM
macroF1 0.6428). 그 뒤 2022 엑셀 병합·catch-all 제거·컷오프 조정을 거쳐
지금은 19클래스·1,404건이고 ML 기준선도 바뀌었다. dl01~05 를 고치는 대신
새 번호로 둔다 — 예전 산출물(dl05 의 openapi_support_type_roberta.parquet 등)과
지금 산출물을 같은 이름으로 덮어써 섞이지 않게 하려는 목적이다.

기준선 (machine-learning M01/M05, 2022+2023 병합 · MIN_SUPPORT=10 · 19클래스)
    일반 CV(누수 포함)   TFIDF+LinearSVM  macroF1 0.8200
    그룹 CV(누수 제거)   TFIDF+LinearSVM  macroF1 0.7953   <- 이게 정직한 기준선

    2022/2023 두 해에 걸쳐 같은 사업이 재공고된다(program_stem 215개 그룹).
    일반 K-Fold 로 재면 같은 사업이 학습/검증에 갈라져 성능이 부풀려진다
    (M05 실측: 일반CV-그룹CV 누수분 0.025~0.052). DL 도 같은 데이터를 쓰므로
    반드시 StratifiedGroupKFold 를 쓴다 — 안 그러면 ML 때 잡았던 문제를 DL 에서
    그대로 재현하게 된다.

    catch-all(기타지원/기타) 제외 + 별칭 통합(ALIAS) + MIN_SUPPORT=10 은
    m01_support_type.py 의 coarsen() 과 완전히 같은 로직을 그대로 옮겼다.

구성
    ① 기준 설정으로 그룹 CV 성능 측정 (ML 기준선과 비교)
    ② One-factor-at-a-time ablation — lr/epochs/max_len/batch/class_weight/fields
    ③ 축별 최적값 조합으로 최종 학습 + Open API 적용 + M07 정답 50건 대조

주의 — 선택 편향
    ②의 수치는 하이퍼파라미터 선택에 쓰인 CV 성능이라 그대로 일반화 성능으로
    제시하면 안 된다. ①(튜닝 전)과 ③의 M07 대조(정답셋, 전혀 다른 데이터)가
    편향 없는 평가다.
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
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.preprocessing import LabelEncoder
from torch.utils.data import DataLoader, Dataset

DATA = os.environ.get("DL_DATA", "/workspace/dl/data/business_taxonomy.parquet")
OUTDIR = os.environ.get("DL_OUT", "/workspace/dl/reports")
MODEL = "klue/roberta-base"

MIN_SUPPORT = 10
EXCLUDED_TYPES = {"기타지원", "기타"}
ALIAS = {"기술평가": "기술·IP평가", "입주공간": "입주지원",
        "판로지원": "판로", "헤외수주·실증": "해외수주·실증"}
MULTI_LABEL_SEP = re.compile(r"[,+/]")

BASELINE_ML_PLAIN = 0.8200     # TFIDF+LinearSVM, 일반 CV (누수 포함, 참고용)
BASELINE_ML_GROUP = 0.7953     # TFIDF+LinearSVM, 그룹 CV (누수 제거, 정직한 기준선)

BASE = {"lr": 3e-5, "epochs": 8, "batch": 16, "max_len": 256,
        "class_weight": True, "fields": "all"}

AXES = {
    "lr": [1e-5, 2e-5, 3e-5, 5e-5],
    "epochs": [5, 8, 12],
    "max_len": [128, 256, 384],
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
    """m01_support_type.coarsen() 과 동일 로직."""
    if not isinstance(v, str) or not v.strip():
        return None
    if v.strip() in EXCLUDED_TYPES:
        return None
    stripped = re.sub(r"\([^)]*\)", "", v)
    first = MULTI_LABEL_SEP.split(stripped)[0].strip()
    if not first:
        return None
    first = ALIAS.get(first, first)
    return None if first in EXCLUDED_TYPES else first


def make_groups(sub):
    """program_stem 이 있는 행은 같은 그룹으로 묶는다(M05/M06 와 동일 로직)."""
    stem = sub["program_stem"].fillna("").astype(str)
    dup = stem.duplicated(keep=False) & (stem != "")
    return np.where(dup, stem, "row_" + np.arange(len(sub)).astype(str))


def build_text(df, fields):
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
    del model, opt, sched
    torch.cuda.empty_cache()
    return np.concatenate(preds)


def evaluate(df, y, groups, cfg, n_cls, folds, seed, cw):
    """StratifiedGroupKFold. 같은 program_stem 은 절대 학습/검증에 갈리지 않는다."""
    X = build_text(df, cfg["fields"])
    sgkf = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed)
    oof = np.zeros(len(y), dtype=int)
    for tr, te in sgkf.split(X, y, groups):
        oof[te] = run_fold(X[tr], y[tr], X[te], y[te], n_cls, cfg, seed, cw)
    return {
        "accuracy": round(float(accuracy_score(y, oof)), 4),
        "macro_f1": round(float(f1_score(y, oof, average="macro", zero_division=0)), 4),
        "weighted_f1": round(float(f1_score(y, oof, average="weighted", zero_division=0)), 4),
    }


def load_data():
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
    groups = make_groups(sub)
    return sub, y, le, n_cls, cw, groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--axes", nargs="+", default=list(AXES.keys()))
    ap.add_argument("--tag", default="v1")
    ap.add_argument("--skip-base-check", action="store_true")
    args = ap.parse_args()

    sub, y, le, n_cls, cw, groups = load_data()
    n_groups = len(set(groups))
    print("데이터 %d건 / %d클래스 / 그룹(사업) %d개 / %d-fold"
          % (len(sub), n_cls, n_groups, args.folds))
    print("기준 설정:", BASE)
    print("비교 기준선 — ML 일반CV %.4f / ML 그룹CV(정직) %.4f"
          % (BASELINE_ML_PLAIN, BASELINE_ML_GROUP))
    print(flush=True)

    results = {}
    t0 = time.time()

    print("[기준 설정 측정 — 그룹 CV]", flush=True)
    base_res = evaluate(sub, y, groups, BASE, n_cls, args.folds, args.seed, cw)
    results["__base__"] = {"config": dict(BASE), **base_res}
    print("  macroF1 %.4f  acc %.4f  (%.0fs)  vs ML그룹CV %+.4f"
          % (base_res["macro_f1"], base_res["accuracy"], time.time() - t0,
             base_res["macro_f1"] - BASELINE_ML_GROUP), flush=True)
    print()

    for axis in args.axes:
        if axis not in AXES:
            continue
        print("[축: %s]" % axis, flush=True)
        results[axis] = {}
        for val in AXES[axis]:
            if val == BASE[axis]:
                results[axis][str(val)] = {**base_res, "is_base": True, "delta": 0.0}
                print("  %-24s macroF1 %.4f  (기준)" % (str(val), base_res["macro_f1"]), flush=True)
                continue
            cfg = dict(BASE)
            cfg[axis] = val
            r = evaluate(sub, y, groups, cfg, n_cls, args.folds, args.seed, cw)
            d = r["macro_f1"] - base_res["macro_f1"]
            results[axis][str(val)] = {**r, "is_base": False, "delta": round(d, 4)}
            print("  %-24s macroF1 %.4f  (%+.4f)  [%.0fs 누적]"
                  % (str(val), r["macro_f1"], d, time.time() - t0), flush=True)
        print(flush=True)

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
        for orig in AXES[axis]:
            if str(orig) == best_v:
                best_cfg[axis] = orig
                break

    print()
    print("축별 최적값 조합:", best_cfg)
    print("(one-factor-at-a-time 이므로 이 조합이 실제 최적이라는 보장은 없다 — 아래서 검증)")

    print()
    print("[최종 검증] 축별 최적값 조합으로 그룹 CV 재측정")
    final_res = evaluate(sub, y, groups, best_cfg, n_cls, args.folds, args.seed, cw)
    print("  macroF1 %.4f  acc %.4f  (base 대비 %+.4f)"
          % (final_res["macro_f1"], final_res["accuracy"],
             final_res["macro_f1"] - base_res["macro_f1"]))

    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "dl_m1_ablation_%s.json" % args.tag)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "model": MODEL, "n_rows": int(len(sub)), "n_classes": int(n_cls),
            "n_groups": n_groups, "folds": args.folds, "seed": args.seed,
            "cv": "StratifiedGroupKFold (program_stem)",
            "method": "one-factor-at-a-time",
            "base_config": BASE, "base_result": base_res,
            "baselines": {"ml_plain_cv": BASELINE_ML_PLAIN,
                         "ml_group_cv": BASELINE_ML_GROUP},
            "axes": {k: v for k, v in results.items() if k != "__base__"},
            "best_per_axis": {k: str(v) for k, v in best_cfg.items()},
            "best_config_combined": best_cfg,
            "final_combined_result": final_res,
            "total_seconds": round(time.time() - t0, 1),
            "classes": le.classes_.tolist(),
            "caveat": ("axes 의 수치는 하이퍼파라미터 선택에 사용된 CV 성능이라 "
                       "선택 편향이 있다. 순수 일반화 성능은 별도로 Open API "
                       "정답 50건(M07) 대조로 확인한다."),
        }, f, ensure_ascii=False, indent=2)
    print("\n[report] %s  (총 %.1f분)" % (out, (time.time() - t0) / 60))


if __name__ == "__main__":
    main()
