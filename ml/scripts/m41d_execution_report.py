# -*- coding: utf-8 -*-
import json
import pandas as pd

fr = json.load(open("data/labels/m41_frozen_70.json", encoding="utf-8"))
dl = json.load(open("reports/m41_dual_labeling.json", encoding="utf-8"))
fi = json.load(open("reports/m41_m3_labelset2_final.json", encoding="utf-8"))
f = pd.read_csv("data/labels/m3_holdout2.csv", encoding="utf-8-sig")

L = ["# M41b — 70건 라벨링 실행 기록 (freeze · blind · 2인 · finalize)", "",
     "> 실행계획 §1~§4 를 실제로 돌린 기록입니다. 라벨 자체는",
     "> `ml/data/labels/m3_holdout2.csv` 에 있고, 여기에는 **어떻게 붙였는지**를",
     "> 남깁니다.", "",
     "## 1. Freeze (§1)", "",
     "```text",
     "n            %d건 (중복 없음)" % fr["n"],
     "sha256       %s" % fr["sha256"],
     "frozen_at    %s" % fr["frozen_at"],
     "```", "",
     "이 목록은 재추출·교체하지 않았습니다. M38 점수를 보고 일부를 바꾸거나,",
     "라벨 분포가 마음에 들지 않아 다시 뽑거나, 모델 결과를 본 뒤 표본을",
     "재구성하는 일은 없었습니다. 지문(sha256)이 그 증거입니다.", "",
     "## 2. Blind 라벨링 (§2)", "",
     "라벨러에게 보이지 않은 것", "",
     "| 항목 | 시트 포함 여부 |", "|---|---|",
     "| M38 anomaly score | 없음 |",
     "| 기존 모델의 경고 여부 | 없음 |",
     "| 모델 순위 | 없음 |",
     "| 이전 모델 판정 | 없음 |",
     "| 1차 hold-out 라벨 | 없음 (row_id·program_stem 이 겹치지 않음) |", "",
     "### 하나는 남겼습니다 — `비교군_percentile`", "",
     "실행계획 §2 는 percentile 도 가리라고 했지만 남겼습니다. 이유를 적습니다.", "",
     "- 이 값은 **모델 출력이 아니라 관측 분포 통계**입니다. 비교군 안에서",
     "  이 사업의 값이 몇 번째인지일 뿐, 어떤 모델도 거치지 않습니다.",
     "- 같은 §2 가 \"M33 과 동일한 라벨 기준\"을 요구하는데, M33 의",
     "  `atypical_design` 정의가 **\"축 둘 이상이 비교군 극단(P>=90 또는 P<=10)\"**",
     "  으로 percentile 위에 세워져 있습니다. 가리면 규칙을 적용할 수 없고,",
     "  가리지 않은 1차와 기준이 달라집니다.",
     "- M33 도 같은 이유로 시트에 percentile 을 넣었습니다",
     "  (\"점수 없음. 비교군 percentile 은 모델 출력이 아니라 관측 분포값이다\").", "",
     "> 두 요구가 충돌해 후자를 택했습니다. 그 대가로 라벨이 percentile 규칙에",
     "> 묶여 있고, 그것이 M42 에서 드러난 `n_axes` 혼입의 뿌리입니다.", "",
     "### 라벨러 조건 — 숨기지 않고 적습니다", "",
     "- 라벨러 A(claude)는 M30/M33/M39/M40 리포트를 이미 읽은 상태입니다.",
     "  **완전한 blind 가 아닙니다.** 규칙을 시트 열기 전에 문자로 고정하고",
     "  행마다 근거를 남긴 것이 그 대용입니다.",
     "- 라벨링 중 `budget_div_count` 행 14건은 원 parquet 으로 값 산출 경로를",
     "  확인했습니다(총예산·amount_type·지원기업수). 모델 점수는 보지 않았습니다.",
     "  그 확인으로 2건의 판정을 바꿨습니다 — `count=1` 로 나뉜 행은 기업당",
     "  한도 축에 사업 총예산이 그대로 들어간 것이라 `data_error` 로 통일했습니다.", "",
     "## 3. 2인 라벨링 (§3)", "",
     "```text",
     "대상   %s" % dl["selection"],
     "A      %s" % dl["labeler_A"],
     "B      %s" % dl["labeler_B"],
     "```", "",
     "| 지표 | 값 |", "|---|---:|",
     "| 단순 agreement | %d/%d = **%.0f%%** |" % (dl["n_agree"], dl["n_dual"], dl["agreement"] * 100),
     "| Cohen's kappa | **%.4f** |" % dl["cohen_kappa"], "",
     "클래스별 (A 기준)", "", "| 라벨 | 일치 / A의 건수 |", "|---|---:|"]
for k, v in dl["per_class_A"].items():
    L.append("| `%s` | %d / %d |" % (k, v["agree"], v["n_A"]))
L += ["", "### 불일치 %d건 — 전부 `data_error` 경계입니다" % len(dl["disagreements"]), "",
      "| row_id | A | B | 주 평가셋 영향 |", "|---|---|---|---|"]
for d in dl["disagreements"]:
    L.append("| `%s` | `%s` | `%s` | %s |"
             % (d["row_id"], d["A"], d["B"], "**있음**" if d["affects_clean_set"] else "없음"))
L += ["", "두 건 모두 `기업당액_산출=budget_div_count` 인 행입니다. A 는 파생값이",
      "필드 의미와 어긋난다고 보아 `data_error`, B 는 \"비교군이 없어 판단 불가\"",
      "(`uncertain`) 또는 \"기업수 P0 이 극단\"(`atypical_design`)으로 읽었습니다.",
      "`normal` 14건과 `atypical_design` 1건은 전부 일치했습니다 —",
      "**흔들리는 경계는 정상/비전형이 아니라 데이터 품질 쪽입니다.**", "",
      "주 평가셋(`normal`+`atypical_design`)이 바뀌는 것은 20건 중 1건이라,",
      "이 불일치가 M42 의 순위 평가를 뒤집지는 않습니다.", "",
      "> **한계: 두 라벨러가 모두 같은 모델(Claude)입니다.** 사람 2인이 아니므로",
      "> 이 수치는 inter-annotator agreement 의 **대용치**로만 읽어야 합니다.",
      "> B 는 A 의 라벨을 열람하지 않았고 규칙 문장만 받았습니다.", "",
      "## 4. Finalize 결과 (§4)", "", "| 라벨 | 건수 |", "|---|---:|"]
for k, v in fi["label_dist"].items():
    L.append("| `%s` | %d |" % (k, v))
L += ["", "```text",
      "주 평가(Clean second hold-out)  normal + atypical_design = %d건 (양성 %d건)"
      % (fi["main_eval_n"], fi["main_eval_positive"]),
      "제외                            data_error %d / uncertain %d"
      % (fi["label_dist"].get("data_error", 0), fi["label_dist"].get("uncertain", 0)),
      "```", "",
      "### 층별 라벨 — 층화가 의도대로 작동했는가", "",
      "**데이터품질 x 라벨**", ""]
ct1 = pd.crosstab(f["층_품질"], f["라벨"])
L += ["| 층 | " + " | ".join("`%s`" % c for c in ct1.columns) + " |",
      "|---|" + "---:|" * len(ct1.columns)]
for a, row in ct1.iterrows():
    L.append("| `%s` | %s |" % (a, " | ".join(str(int(v)) for v in row)))
ct2 = pd.crosstab(f["층_축수"], f["라벨"])
L += ["", "**수치축 x 라벨**", "",
      "| 층 | " + " | ".join("`%s`" % c for c in ct2.columns) + " |",
      "|---|" + "---:|" * len(ct2.columns)]
for a, row in ct2.iterrows():
    L.append("| `%s` | %s |" % (a, " | ".join(str(int(v)) for v in row)))
L += ["", "데이터품질 층화는 정확히 먹혔습니다 — `C_의심` 5건이 **전부**",
      "`data_error` 로, `A_직접기재` 49건에는 `uncertain` 이 **0건**입니다.",
      "M41 이 이 축을 1단 층화로 올린 이유가 그대로 확인됩니다.", "",
      "수치축 층화는 표본을 고르게 만들었지만 **라벨까지 고르게 만들지는",
      "못했습니다** — `atypical_design` 양성률이 축2 에서 축4 로 갈수록",
      "올라갑니다. 표본 설계로 풀 수 있는 문제가 아니라 라벨 규칙 자체의",
      "성질이고, M42 §6 이 그 결과를 잽니다.", ""]
open("reports/m41b_labeling_execution.md", "w", encoding="utf-8").write("\n".join(L))
print("[report] reports/m41b_labeling_execution.md  (%d lines)" % len(L))
