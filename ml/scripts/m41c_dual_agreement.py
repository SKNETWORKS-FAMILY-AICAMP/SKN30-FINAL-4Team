# -*- coding: utf-8 -*-
import json, sys
import pandas as pd
from sklearn.metrics import cohen_kappa_score, confusion_matrix

B = {
"EXCEL2022_0842":("normal","한도 P75·기업수 P49로 극단축 없음"),
"EXCEL2022_0248":("atypical_design","한도 P100 + 비율 P90 동시 극단"),
"PBLN_000000000104669":("normal","비율 P90 하나뿐이고 지자체 설비 보조율로 설명"),
"PBLN_000000000117711":("normal","한도 P39·기간 P75로 극단축 없음"),
"PBLN_000000000047357":("uncertain","비교군 부족이라 14.3만원의 이례성을 판단할 근거가 없다"),
"EXCEL2022_0492":("normal","세 축 모두 극단 아님"),
"EXCEL2022_0101":("normal","기간 P10이나 R&D 단년도 관행으로 설명"),
"EXCEL2023_0209":("normal","기간 P25만 관측되고 극단축 없음"),
"PBLN_000000000104420":("normal","P80·P78 둘 다 P90 미만"),
"PBLN_000000000073486":("normal","기업수 P76·한도 P40으로 극단축 없음"),
"PBLN_000000000106461":("normal","한도 P20·기업수 P42로 극단축 없음"),
"EXCEL2023_0872":("normal","네 축 모두 극단 아님"),
"PBLN_000000000071283":("uncertain","비교군 부족"),
"PBLN_000000000124726":("uncertain","비교군 부족"),
"PBLN_000000000077818":("atypical_design","기업수 P0이 극단인데 R&D 공개모집에서 1개사는 설명되지 않는다"),
"PBLN_000000000061478":("uncertain","비교군 부족 + budget_div_count 파생값"),
"PBLN_000000000097317":("normal","한도 P77·기업수 P36으로 극단축 없음"),
"EXCEL2022_0376":("normal","한도 P16·비율 P75로 극단축 없음"),
"PBLN_000000000077493":("normal","P77·P68 둘 다 P90 미만"),
"PBLN_000000000050782":("normal","한도 P35·기업수 P64로 극단축 없음"),
}

a = pd.read_csv("data/labels/m41_labelset2_filled.csv", encoding="utf-8-sig")
a = a.set_index("row_id")
ids = list(B)
ya = [a.loc[i, "라벨"] for i in ids]
yb = [B[i][0] for i in ids]

agree = sum(x == y for x, y in zip(ya, yb))
kappa = cohen_kappa_score(ya, yb)
labels = ["normal", "atypical_design", "data_error", "uncertain"]
cm = confusion_matrix(ya, yb, labels=labels)

print("== 2인 라벨링 일치도 (20건)")
print("  단순 agreement %d/%d = %.1f%%" % (agree, len(ids), agree / len(ids) * 100))
print("  Cohen's kappa  %.4f" % kappa)
print("\n== A(행) x B(열)")
print(pd.DataFrame(cm, index=labels, columns=labels).to_string())

per = {}
print("\n== 클래스별 (A 기준)")
for L in labels:
    n = sum(1 for x in ya if x == L)
    if not n:
        continue
    ok = sum(1 for x, y in zip(ya, yb) if x == L and y == L)
    per[L] = {"n_A": n, "agree": ok, "rate": round(ok / n, 4)}
    print("  %-16s %d/%d = %.0f%%" % (L, ok, n, ok / n * 100))

dis = [{"row_id": i, "A": a.loc[i, "라벨"], "B": B[i][0],
        "A_reason": str(a.loc[i, "라벨근거"]), "B_reason": B[i][1],
        "affects_clean_set": bool((a.loc[i, "라벨"] in ("normal", "atypical_design"))
                                  != (B[i][0] in ("normal", "atypical_design")))}
       for i in ids if a.loc[i, "라벨"] != B[i][0]]
print("\n== 불일치 %d건" % len(dis))
for d in dis:
    print("  %s\n    A=%-16s %s\n    B=%-16s %s\n    주평가셋 영향: %s"
          % (d["row_id"], d["A"], d["A_reason"][:70], d["B"], d["B_reason"][:70],
             "있음" if d["affects_clean_set"] else "없음"))

json.dump({
    "n_dual": len(ids), "subset_seed": 777,
    "selection": "70건에서 무작위 20건 (1차 라벨을 보지 않고 시드로 선정)",
    "labeler_A": "claude-A (전체 70건)", "labeler_B": "claude-B (서브에이전트, 20건, A의 라벨 미열람)",
    "caveat": ("두 라벨러가 모두 같은 모델(Claude)이다. 사람 2인이 아니므로 "
               "inter-annotator agreement 의 대용치로만 읽어야 한다."),
    "agreement": round(agree / len(ids), 4), "n_agree": agree,
    "cohen_kappa": round(float(kappa), 4),
    "confusion_A_rows_B_cols": {labels[i]: {labels[j]: int(cm[i][j])
                                            for j in range(len(labels))}
                                for i in range(len(labels))},
    "per_class_A": per, "disagreements": dis,
}, open("reports/m41_dual_labeling.json", "w", encoding="utf-8"),
   ensure_ascii=False, indent=2)
print("\n[report] reports/m41_dual_labeling.json")
