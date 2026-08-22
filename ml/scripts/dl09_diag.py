"""DL09 진단 — 이미 저장된 예측으로 정확도·보정 상태를 다시 낸다(재학습 없음)."""
import json
import sys
import io

import numpy as np
import pandas as pd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

PRED = "/workspace/dl/data/openapi_support_type_roberta_v2.parquet"
LABELS = "/workspace/dl/data/openapi_manual_50.csv"
M08 = "/workspace/dl/data/m08_pred.parquet"

out = pd.read_parquet(PRED)
out["announcement_id"] = out["announcement_id"].astype(str)

lab = pd.read_csv(LABELS, encoding="utf-8-sig")
lab["announcement_id"] = lab["announcement_id"].astype(str)
lab = lab[lab["label_19class"].fillna("").astype(str) != ""]
lab = lab.drop(columns=[c for c in ("confidence", "status", "title") if c in lab.columns])
m = lab.merge(out, on="announcement_id", how="left")
m = m[m["support_type_pred"].notna()]

print("=== DL09 (KLUE-RoBERTa, DL06 최적조합) ===")
print("적용 %d건 / 판단보류 %d건 / 평균확신 %.4f"
      % (len(out), int((out["status"] == "판단보류").sum()), out["confidence"].mean()))
print()
acc = float((m["support_type_pred"] == m["label_19class"]).mean())
print("M13 정답 %d건 정확도 %.1f%%" % (len(m), acc * 100))
print("참고 — M08(LogReg) 판단보류 제외 29건 79.3%, 커버리지 70.7%")
print()

print("[오분류 상세]")
bad = m[m["support_type_pred"] != m["label_19class"]]
for _, r in bad.iterrows():
    print("  %-12s -> %-12s  확신 %.3f  | %s"
          % (r["label_19class"], r["support_type_pred"], r["confidence"],
             str(r.get("title_x", r.get("title", "")))[:38]))

print()
print("[확신도 구간별 실제 정확도 — 과신 여부]")
bins = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 0.99), (0.99, 1.01)]
for lo, hi in bins:
    s = m[(m["confidence"] >= lo) & (m["confidence"] < hi)]
    if len(s) == 0:
        continue
    a = float((s["support_type_pred"] == s["label_19class"]).mean())
    c = float(s["confidence"].mean())
    print("  %.2f~%.2f  n=%2d  평균확신 %.3f  실제정확도 %.3f  (과신 %+.3f)"
          % (lo, hi, len(s), c, a, c - a))

print()
print("[예측 분포 쏠림]")
vc = out["support_type_pred"].value_counts()
print("  DL:", dict(vc.head(6)))
p8 = pd.read_parquet(M08)
p8u = p8[p8["support_type_status"] != "판단보류"]
print("  M08:", dict(p8u["support_type_pred"].value_counts().head(6)))
print("  DL 최다클래스 비중 %.1f%% / M08 %.1f%%"
      % (vc.iloc[0] / len(out) * 100,
         p8u["support_type_pred"].value_counts().iloc[0] / len(p8u) * 100))
