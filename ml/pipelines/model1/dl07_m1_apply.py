"""DL07 — DL06 최적 조합으로 학습한 KLUE-RoBERTa 를 Open API 에 적용 + 정답 대조.

왜 별도 단계인가
    DL06 의 ablation 수치(macroF1 0.8337)는 하이퍼파라미터를 고르는 데 쓴
    CV 성능이라 선택 편향이 있다. 그대로 일반화 성능이라 부르면 안 된다.
    편향 없는 평가는 두 가지다.
      ① 튜닝 전 base(0.8283) 와 ML 기준선(0.7953) 의 비교 — DL06 에서 이미 냈다
      ② 학습에 전혀 안 쓴 데이터로 재기 — 이 스크립트가 하는 일

    ②의 정답은 M07 의 수동 라벨 41건이다. 원문을 직접 읽고 붙였고, 적용 대상인
    2026 Open API 도메인이라 학습 도메인(2022~2023 중앙부처)과 겹치지 않는다.

M02(LogisticRegression) 과 조건을 맞춘다 — 안 그러면 비교가 성립 안 한다
    학습     business_taxonomy 전량, MIN_SUPPORT=10 -> 19클래스
    적용     announcement_detail 1,570건. 원문 있으면 원문(clean_text 전처리),
             없으면 요약문 대체 — M02 이 고른 조건 B 와 같다.
    임계값   0.20 / 0.35. M09 에서 커버리지·정확도·오분류 편향을 함께 재고
             정한 값이라 모델이 바뀌어도 같은 기준으로 본다.

무엇을 보나
    ㄱ. 판단보류율 — DL 이 확신도를 올려 커버리지를 늘리는가.
        이게 모델 2(A03)의 추세 검정력과 직결된다. M02 은 67.8% 사용가능이었다.
    ㄴ. M07 정답 41건 정확도 — 커버리지가 늘 때 정확도가 유지되는가.
        M02 은 판단보류 제외 29건 중 23건(79.3%) 이었다.
    ㄷ. 확신도 보정 상태 — 확신도 구간별 실제 정확도. DL 이 과신하는지 본다.
        예전 dl05(26클래스 시절)는 98.3%를 '신뢰'로 매겼는데 검증된 적이 없었다.
"""
import argparse
import json
import os
import re

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader, Dataset

DATA = os.environ.get("DL_DATA", "/workspace/dl/data/business_taxonomy.parquet")
DETAIL = os.environ.get("DL_DETAIL", "/workspace/dl/data/announcement_detail_enriched.parquet")
LABELS = os.environ.get("DL_LABELS", "/workspace/dl/data/openapi_manual_50.csv")
M02 = os.environ.get("DL_M08", "/workspace/dl/data/m02_apply_pred.parquet")
OUTDIR = os.environ.get("DL_OUT", "/workspace/dl/reports")
MODEL = "klue/roberta-base"

MIN_SUPPORT = 10
EXCLUDED_TYPES = {"기타지원", "기타"}
ALIAS = {"기술평가": "기술·IP평가", "입주공간": "입주지원",
         "판로지원": "판로", "헤외수주·실증": "해외수주·실증"}
MULTI_LABEL_SEP = re.compile(r"[,+/]")

# DL06 ablation 이 고른 조합
BEST = {"lr": 5e-5, "epochs": 8, "batch": 8, "max_len": 384,
        "class_weight": True}

# M09 에서 정한 값. 모델이 바뀌어도 같은 기준으로 봐야 비교가 된다.
HOLD_THRESHOLD = 0.20
TRUST_THRESHOLD = 0.35

# ---- M02 과 같은 전처리 (조건 B) ----
LETTERHEAD = re.compile(r"^.{0,40}(공고\s*제\s*20\d{2}[-－]\d+\s*호).*?\n", re.S)
DATE_LINE = re.compile(r"^\s*20\d{2}\s*[.년]\s*\d{1,2}\s*[.월]\s*\d{0,2}\s*[.일]?\s*$", re.M)
SIGNOFF = re.compile(r"^[가-힣()（）\s]{2,20}(장|원장|이사장|청장|본부장)\s*$", re.M)
DASH = re.compile(r"^-{5,}$", re.M)
PAGE_NUM = re.compile(r"^-\s*\d+\s*-$", re.M)
CONTENT_MARK = re.compile(r"(사업\s*개요|추진\s*배경|사업\s*목적|지원\s*목적|모집\s*개요|사업\s*내용)")
ADMIN_MARK = re.compile(r"(신청\s*방법|제출\s*서류|접수\s*방법|문\s*의\s*처|붙\s*임|유의\s*사항|추진\s*절차)")
BULLET = re.compile(r"[□◦※➡▶■●○☞]")
MD_TABLE_SEP = re.compile(r"^\|[\s\-:|]+\|$", re.M)


def clean_text(text, budget=900):
    t = LETTERHEAD.sub("", text, count=1)
    for p in (DATE_LINE, SIGNOFF, DASH, PAGE_NUM, MD_TABLE_SEP):
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
    if v.strip() in EXCLUDED_TYPES:
        return None
    stripped = re.sub(r"\([^)]*\)", "", v)
    first = MULTI_LABEL_SEP.split(stripped)[0].strip()
    if not first:
        return None
    first = ALIAS.get(first, first)
    return None if first in EXCLUDED_TYPES else first


def tier(c):
    if c < HOLD_THRESHOLD:
        return "판단보류"
    return "참고용" if c < TRUST_THRESHOLD else "신뢰"


class TextDS(Dataset):
    def __init__(self, texts, tok, max_len, labels=None):
        self.enc = tok(list(texts), truncation=True, padding="max_length",
                       max_length=max_len, return_tensors="pt")
        self.labels = torch.tensor(labels, dtype=torch.long) if labels is not None else None

    def __len__(self):
        return self.enc["input_ids"].shape[0]

    def __getitem__(self, i):
        item = {k: v[i] for k, v in self.enc.items()}
        if self.labels is not None:
            item["labels"] = self.labels[i]
        return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--tag", default="v1")
    args = ap.parse_args()

    from sklearn.preprocessing import LabelEncoder
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    # ---- 학습: 전량 (CV 아님 — 최종 모델) ----
    t = pd.read_parquet(DATA)
    t["support_type"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["support_type"])
    vc = sub["support_type"].value_counts()
    sub = sub[sub["support_type"].isin(vc[vc >= MIN_SUPPORT].index)].reset_index(drop=True)
    le = LabelEncoder()
    y = le.fit_transform(sub["support_type"].values)
    n_cls = len(le.classes_)
    X = np.asarray(sub["text_for_model"].fillna("").astype(str).tolist(), dtype=object)
    print("학습 %d건 / %d클래스 (설정: %s)" % (len(X), n_cls, BEST), flush=True)

    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL, num_labels=n_cls).cuda()
    dl = DataLoader(TextDS(X, tok, BEST["max_len"], y),
                    batch_size=BEST["batch"], shuffle=True)
    opt = torch.optim.AdamW(model.parameters(), lr=BEST["lr"], weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=BEST["lr"], total_steps=len(dl) * BEST["epochs"], pct_start=0.1)
    counts = np.bincount(y, minlength=n_cls)
    w = torch.tensor(len(y) / (n_cls * np.maximum(counts, 1)),
                     dtype=torch.float).cuda() if BEST["class_weight"] else None
    lossf = torch.nn.CrossEntropyLoss(weight=w)

    model.train()
    for ep in range(BEST["epochs"]):
        tot = 0.0
        for b in dl:
            b = {k: v.cuda() for k, v in b.items()}
            lab = b.pop("labels")
            loss = lossf(model(**b).logits, lab)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            opt.zero_grad()
            tot += float(loss)
        print("  epoch %d/%d  loss %.4f" % (ep + 1, BEST["epochs"], tot / len(dl)), flush=True)

    # ---- 적용: Open API ----
    d = pd.read_parquet(DETAIL)
    d["announcement_id"] = d["announcement_id"].astype(str)
    doc = d["doc_text"].fillna("").astype(str)
    fallback = (d["summary_text"].fillna("") + "\n" + d["target_text"].fillna("")).astype(str)
    texts = [clean_text(x) if x.strip() else fb for x, fb in zip(doc, fallback)]
    has_doc = np.array([bool(x.strip()) for x in doc])
    print("적용 %d건 (원문 보유 %d건)" % (len(texts), int(has_doc.sum())), flush=True)

    model.eval()
    probs = []
    with torch.no_grad():
        for b in DataLoader(TextDS(texts, tok, BEST["max_len"]), batch_size=64):
            b = {k: v.cuda() for k, v in b.items()}
            probs.append(torch.softmax(model(**b).logits, -1).cpu().numpy())
    proba = np.concatenate(probs)
    conf = proba.max(axis=1)
    pred = le.classes_[proba.argmax(axis=1)]
    tiers = np.array([tier(c) for c in conf])

    out = pd.DataFrame({
        "announcement_id": d["announcement_id"], "title": d["title"],
        "support_type_pred": pred, "confidence": conf, "status": tiers,
        "has_document": has_doc,
    })
    outp = os.path.join(OUTDIR, "..", "data", "openapi_support_type_roberta_v2.parquet")
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    out.to_parquet(outp, index=False)

    n_hold = int((tiers == "판단보류").sum())
    usable_rate = float((tiers != "판단보류").mean())
    print()
    print("판단보류 %d건 (%.1f%%) / 사용가능 %.1f%%"
          % (n_hold, n_hold / len(out) * 100, usable_rate * 100))
    print("평균 확신도 %.4f" % conf.mean())

    # ---- M07 정답 대조 (편향 없는 평가) ----
    manual = {}
    if os.path.exists(LABELS):
        lab = pd.read_csv(LABELS, encoding="utf-8-sig")
        lab["announcement_id"] = lab["announcement_id"].astype(str)
        lab = lab[lab["label_19class"].fillna("").astype(str) != ""]
        # M07 라벨 파일에도 confidence(사람이 매긴 태깅 확신도) 컬럼이 있어,
        # 그대로 merge 하면 예측 확신도가 confidence_y 로 밀려 KeyError 가 난다.
        lab = lab.drop(columns=[c for c in ("confidence", "status", "title")
                                if c in lab.columns])
        m = lab.merge(out, on="announcement_id", how="left")
        m = m[m["support_type_pred"].notna()]
        all_acc = float((m["support_type_pred"] == m["label_19class"]).mean())
        u = m[m["status"] != "판단보류"]
        u_acc = float((u["support_type_pred"] == u["label_19class"]).mean()) if len(u) else float("nan")
        manual = {"n_labeled": int(len(m)),
                  "accuracy_all": round(all_acc, 4),
                  "n_usable": int(len(u)),
                  "coverage": round(len(u) / len(m), 4),
                  "accuracy_usable": round(u_acc, 4)}
        print()
        print("[M07 정답 대조 — 학습에 안 쓴 데이터]")
        print("  전체(보류 포함)  %d건 중 정확도 %.1f%%" % (len(m), all_acc * 100))
        print("  판단보류 제외    %d건 중 정확도 %.1f%%  (커버리지 %.1f%%)"
              % (len(u), u_acc * 100, len(u) / len(m) * 100))
        print("  참고 — M02(LogReg): 29건 / 79.3% / 커버리지 70.7%")

        bad = u[u["support_type_pred"] != u["label_19class"]]
        if len(bad):
            print("  오분류:")
            for _, r in bad.iterrows():
                print("    %-12s -> %-12s (확신 %.3f)"
                      % (r["label_19class"], r["support_type_pred"], r["confidence"]))

        # 확신도 구간별 실제 정확도 = 보정 상태
        bins = [(0.2, 0.35), (0.35, 0.5), (0.5, 0.7), (0.7, 1.01)]
        calib = []
        for lo, hi in bins:
            s = m[(m["confidence"] >= lo) & (m["confidence"] < hi)]
            if len(s) >= 3:
                calib.append({"range": "%.2f~%.2f" % (lo, hi), "n": int(len(s)),
                              "mean_conf": round(float(s["confidence"].mean()), 3),
                              "accuracy": round(float((s["support_type_pred"] == s["label_19class"]).mean()), 3)})
        if calib:
            print()
            print("  [확신도 보정] 구간별 평균확신 vs 실제정확도 — 과신 여부")
            for c in calib:
                gap = c["mean_conf"] - c["accuracy"]
                print("    %-12s n=%2d  확신 %.3f  실제 %.3f  (차이 %+.3f)"
                      % (c["range"], c["n"], c["mean_conf"], c["accuracy"], gap))
        manual["calibration"] = calib

    # ---- M02 과 나란히 ----
    cmp = {}
    if os.path.exists(M02):
        p8 = pd.read_parquet(M02)
        p8["announcement_id"] = p8["announcement_id"].astype(str)
        cmp = {"m02_apply_usable_rate": round(float((p8["support_type_status"] != "판단보류").mean()), 4),
               "dl_usable_rate": round(usable_rate, 4)}
        both = p8[["announcement_id", "support_type_pred", "support_type_status"]].merge(
            out[["announcement_id", "support_type_pred", "status"]],
            on="announcement_id", suffixes=("_m08", "_dl"))
        agree = both[(both["support_type_status"] != "판단보류") & (both["status"] != "판단보류")]
        if len(agree):
            cmp["both_usable_n"] = int(len(agree))
            cmp["agreement"] = round(float((agree["support_type_pred_m08"] == agree["support_type_pred_dl"]).mean()), 4)
        print()
        print("[M02 대비] 사용가능률 %.1f%% -> %.1f%%"
              % (cmp["m02_apply_usable_rate"] * 100, cmp["dl_usable_rate"] * 100))
        if "agreement" in cmp:
            print("  둘 다 확신한 %d건의 라벨 일치율 %.1f%%"
                  % (cmp["both_usable_n"], cmp["agreement"] * 100))

    os.makedirs(OUTDIR, exist_ok=True)
    with open(os.path.join(OUTDIR, "dl07_m1_apply_%s.json" % args.tag), "w",
              encoding="utf-8") as f:
        json.dump({
            "model": MODEL, "config": BEST,
            "train_rows": int(len(X)), "n_classes": int(n_cls),
            "applied_rows": int(len(out)),
            "thresholds": {"hold": HOLD_THRESHOLD, "trust": TRUST_THRESHOLD},
            "n_hold": n_hold, "usable_rate": round(usable_rate, 4),
            "mean_confidence": round(float(conf.mean()), 4),
            "predicted_class_dist": out[tiers != "판단보류"]["support_type_pred"]
                                    .value_counts().to_dict(),
            "manual_eval": manual,
            "vs_m08": cmp,
            "caveat": ("DL06 의 ablation 수치는 하이퍼파라미터 선택에 쓴 CV 라 선택 "
                       "편향이 있다. 편향 없는 평가는 여기의 M07 정답 대조다."),
            "output": outp,
        }, f, ensure_ascii=False, indent=2)
    print("\n[report] dl07_m1_apply_%s.json" % args.tag)
    print("→ %s" % outp)


if __name__ == "__main__":
    main()
