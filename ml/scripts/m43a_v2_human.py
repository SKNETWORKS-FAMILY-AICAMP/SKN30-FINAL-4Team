# -*- coding: utf-8 -*-
"""M43a — v2 기계 후보에 사람 판단을 붙인다.

방향은 하나뿐이다: **후보 -> 사업 유형으로 설명되면 normal 로 하향.**
상향(비후보 -> atypical)은 규칙상 열려 있으나 쓰지 않았다. v1(M33/M41)도
하향 조항만 있었고, 상향을 허용하면 경계 아래 행마다 사후 논리를 만들어
올릴 수 있어 규칙이 무력해진다. 사람 개입 방향을 하나로 묶어 둔다.
"""
import pandas as pd

DOWN = {
    "EXCEL2022_0105": "후속연계 개발은 1년 단기가 설계 전제이고(기간 P10), 자부담 20%(비율 P90)는 중소기업 R&D 통상 출연비율이다. 두 축 모두 사업 유형으로 설명된다",
    "PBLN_000000000104669": "비교 가능 축이 지원비율 1개뿐이라 설계 '조합'을 말할 수 없다. 지자체 시설개선 80% 보조는 통상 보조율이다",
    "PBLN_000000000103925": "비교 가능 축이 지원비율 1개뿐이라 설계 '조합'을 말할 수 없다. 사회적기업가 육성 90% 지원은 통상 구조다",
    "PBLN_000000000118209": "기업수 P100(775개사)은 전국 단위 연구인력 인건비 지원의 실제 규모이고, 나머지 축(한도 P72)은 중앙 근처다",
}
KEEP = {
    "PBLN_000000000106847": "과제당 4.6억 P100 + 지원비율 P90. 사업화 project 비교군 최상단 조합이고 사업 유형으로 설명되지 않는다",
    "EXCEL2022_0570": "기업수 P10 + 비율 P90 + 한도 P18. 소수·소액·고지원율이 한 방향으로 겹친 드문 조합",
    "PBLN_000000000048393": "과제당 45억 P90 + 비율 P90. 인재양성 R&D 로도 단가와 지원율이 함께 최상단이다",
    "EXCEL2023_0653": "기업수 P0 + 한도 P90 + 비율 P90. 세 축 동시 극단",
    "EXCEL2023_0873": "115개사 P100 규모에서 기업당 3억 P75 를 동시에 준다. 규모와 단가가 함께 상단인 드문 조합 — '원래 큰 사업'은 하향 근거가 되지 못한다",
}

s = pd.read_csv("reports/m43_rule_v2_sheet.csv", encoding="utf-8-sig")
clean = s["v1_라벨"].isin(["normal", "atypical_design"])
cand = s["기계후보_v2"] == "candidate"

assert set(s.loc[cand, "row_id"]) == set(DOWN) | set(KEEP), "후보 목록 불일치"

s["v2_라벨"] = s["v1_라벨"]                       # data_error / uncertain 은 그대로
s.loc[clean, "v2_라벨"] = "normal"                # clean 은 일단 전부 normal
s.loc[s["row_id"].isin(KEEP), "v2_라벨"] = "atypical_design"

s["v2_근거"] = ""
s.loc[clean & ~cand, "v2_근거"] = "기계 비후보 (축보정 백분위 상위 20% 밖)"
for rid, why in {**DOWN, **KEEP}.items():
    s.loc[s["row_id"] == rid, "v2_근거"] = why
s.loc[~clean, "v2_근거"] = "v1 유지 (축 개수 편향과 무관)"

s.to_csv("reports/m43_rule_v2_filled.csv", index=False, encoding="utf-8-sig")
print("하향 %d건 / 후보 유지 %d건 / 상향 0건" % (len(DOWN), len(KEEP)))
print(s["v2_라벨"].value_counts().to_string())
