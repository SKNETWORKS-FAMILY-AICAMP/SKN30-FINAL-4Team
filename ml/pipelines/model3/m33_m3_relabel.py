r"""M33 — 교정된 값으로 hold-out 50건을 다시 라벨링한다 (계획서 Step 2).

왜 다시 라벨링하는가
    M30 에서 라벨러가 '비전형'이라고 본 15건 중 8건은 설계가 드문 게 아니라
    **값이 잘못 들어온 행**이었다. M32 가 그 값을 고쳤으므로 예전 라벨은
    이제 존재하지 않는 숫자를 보고 내린 판정이다. 그대로 쓰면 파서 버그를
    잡은 공을 이상탐지 성능으로 계산하게 된다.

라벨 4칸 (계획서 §11 Step 2 / 직전 계획서 §6.1)
    normal           비교군 안에서 특별히 드문 설계가 아니다
    atypical_design  입력값이 정상인데도 설계 조합이 드물다   <- 모델 3 의 정답
    data_error       파싱·단위·필드의미 오류가 남아 있다      <- 데이터 품질 영역
    uncertain        수치 축이 얇거나 비교군이 부족해 판단이 안 된다

    `data_error` 와 `uncertain` 은 주 평가에서 빼고 따로 보고한다. 이 둘을
    positive 로 섞으면 모델이 무엇을 잘하는지 알 수 없게 된다.

라벨 규칙 — 시트를 보기 전에 못박는다 (M23/M30 규칙 + data_error 추가)
    atypical_design  설계 축 둘 이상이 비교군 극단(P>=90 또는 P<=10)이거나,
                     축 하나가 극단인데 사업 유형으로 설명되지 않는 경우
    normal           비교군 대비 특별히 드문 조합이 아닌 경우.
                     축 하나가 치우쳐 있어도 사업 유형으로 설명되면 여기.
    data_error       교정 후에도 값이 상식 범위 밖이거나(기업당액 SANE_RANGE 밖,
                     기간 10년 초과), 필드 의미가 서로 모순되는 경우
    uncertain        수치 축이 2개 미만이거나 비교군이 전부 '비교불가'인 경우

    M30 의 '경계' 칸은 없앴다. 그 칸은 사실상 "정상인데 눈에 띈다"였고,
    positive 로 세느냐 마느냐에 따라 recall 이 0.30~0.33 사이를 오갔다.
    판단이 안 되는 것(`uncertain`)과 판단해서 정상인 것(`normal`)은 다르다.

두 단계로 나눈 이유
    --sheet     점수를 뺀 라벨링 시트를 만든다 (라벨러가 볼 것)
    --finalize  채워진 라벨을 읽어 clean hold-out 을 만든다 (모델이 볼 것)
    한 번에 하면 시트를 만들면서 점수를 본 채로 라벨을 붙이게 된다.

숨기지 않고 적는 한계
    이 라벨러(claude)는 M30 의 FN/FP 목록을 이미 읽었다. 완전한 blind 가
    아니다. 그래서 위 규칙을 시트를 열기 전에 문자로 고정했고, 라벨 근거를
    행마다 남겨 규칙과 어긋나는 판정을 나중에 찾아낼 수 있게 했다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m3_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

def _find_ml_root(_start):
    """`ml/` 를 위로 거슬러 찾는다. 파일이 몇 단계 아래로 옮겨져도 동작한다."""
    _p = _os.path.abspath(_start)
    while True:
        _p = _os.path.dirname(_p)
        if (_os.path.isdir(_os.path.join(_p, "pipelines"))
                and _os.path.isdir(_os.path.join(_p, "data"))):
            return _p
        if _p == _os.path.dirname(_p):
            raise RuntimeError("ml root not found from %s" % _start)


_ML = _find_ml_root(__file__)
for _d in ("pipelines", "evaluation", "experiments"):
    _base = _os.path.join(_ML, _d)
    if not _os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in _os.walk(_base):
        if "__pycache__" in _dp:
            continue
        if _dp not in _sys.path:
            _sys.path.insert(0, _dp)
# -------------------------------------------------------------------------

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m12_m3_cohort import compare
from m13_m3_anomaly import AXIS_LABEL, MIN_AXES, REF, SRC, prepare

HOLDOUT = os.path.join(C.DATA, "labels", "m3_anomaly_holdout_50.csv")
SHEET = C.report_path("m33_relabel_sheet.csv")
FILLED = os.path.join(C.DATA, "labels", "m33_relabel_filled.csv")
CLEAN = os.path.join(C.DATA, "labels", "m3_clean_holdout.csv")

LABELS = ["normal", "atypical_design", "data_error", "uncertain"]
PCT_EXTREME = 90
SEED = 20260826


def axis_notes(row, ref):
    """축별 비교군 percentile. 모델 점수가 아니라 관측 분포에서 나온 값이다."""
    out = []
    for axis, label in AXIS_LABEL.items():
        c = compare(ref, axis, row.get(axis), row["support_type"],
                    row["support_method"], row.get("support_unit"), row["cohort"])
        if c["status"] == "비교불가":
            continue
        out.append({"axis": axis, "label": label, "pct": c["percentile_rank"],
                    "n": c["n"], "level": c["level"]})
    return sorted(out, key=lambda x: -abs(x["pct"] - 50))


def won(v):
    if v is None or pd.isna(v):
        return ""
    for unit, mult in (("조원", 1e12), ("억원", 1e8), ("만원", 1e4)):
        if abs(v) >= mult:
            return "%.1f%s" % (v / mult, unit)
    return "%.0f원" % v


def build_sheet():
    hold = pd.read_csv(HOLDOUT, encoding="utf-8-sig")
    feat = prepare(pd.read_parquet(SRC)).drop_duplicates("row_id").set_index("row_id")
    ref = pd.read_parquet(REF)
    lo, hi = C.SANE_RANGE["per_company"]

    rows = []
    for _, h in hold.iterrows():
        rid = h["row_id"]
        if rid not in feat.index:
            continue
        r = feat.loc[rid]
        notes = axis_notes(r, ref)
        extreme = [n for n in notes if n["pct"] >= PCT_EXTREME or n["pct"] <= 100 - PCT_EXTREME]
        pr = r["per_recipient"]
        dur = r["project_duration"]
        rows.append({
            "row_id": rid,
            "사업명": str(r["title"])[:60],
            "출처": r["cohort"],
            "지원성격": r["support_type"],
            "지원방식": r["support_method"],
            "지원단위": r["support_unit"],
            "기업당지원한도": won(pr),
            "기업당액_산출": r["per_recipient_basis"],
            "지원기업수": r["support_count"],
            "지원비율": r["support_ratio"],
            "사업기간": dur,
            "기간_근거": r["duration_basis"],
            "수치축_개수": int(r["n_axes"]),
            "비교군_percentile": " / ".join(
                "%s P%.0f(n=%d,%s)" % (n["label"], n["pct"], n["n"], n["level"])
                for n in notes[:4]) or "비교군 부족",
            "극단축_개수": len(extreme),
            "상식범위밖": bool((pd.notna(pr) and not (lo <= pr <= hi))
                          or (pd.notna(dur) and dur > 10)),
            "원문발췌": str(r["evidence_text"] or "").replace("\n", " ")[:180],
            "라벨": "",
            "라벨근거": "",
        })
    df = pd.DataFrame(rows).sample(frac=1.0, random_state=SEED).reset_index(drop=True)
    df.to_csv(SHEET, index=False, encoding="utf-8-sig")
    print("[sheet] %s  %d행" % (SHEET, len(df)))
    print("  라벨 선택지: %s" % " / ".join(LABELS))
    print("  점수 없음. 비교군 percentile 은 모델 출력이 아니라 관측 분포값이다.")
    print("\n  수치축 분포: %s" % dict(df["수치축_개수"].value_counts().sort_index()))
    print("  극단축 분포: %s" % dict(df["극단축_개수"].value_counts().sort_index()))
    print("  상식범위밖: %d건" % int(df["상식범위밖"].sum()))


def finalize():
    if not os.path.exists(FILLED):
        sys.exit("채워진 라벨 파일이 없습니다: %s" % FILLED)
    f = pd.read_csv(FILLED, encoding="utf-8-sig")
    bad = set(f["라벨"].dropna()) - set(LABELS)
    if bad:
        sys.exit("허용되지 않은 라벨: %s" % bad)
    if f["라벨"].isna().any():
        sys.exit("비어 있는 라벨 %d건" % int(f["라벨"].isna().sum()))

    old = pd.read_csv(HOLDOUT, encoding="utf-8-sig").rename(
        columns={"판단(정상/비전형)": "이전라벨", "하위유형": "이전하위유형"})
    m = f.merge(old[["row_id", "이전라벨", "이전하위유형"]], on="row_id", how="left")
    m.to_csv(CLEAN, index=False, encoding="utf-8-sig")

    ct = pd.crosstab(m["이전라벨"], m["라벨"])
    print("M33 — clean hold-out 확정")
    print("  [labels] %s" % CLEAN)
    print("\n== 새 라벨 분포")
    for k, v in m["라벨"].value_counts().items():
        print("  %-16s %d" % (k, v))
    print("\n== 이전 라벨 x 새 라벨")
    print(ct.to_string())

    main_set = m[m["라벨"].isin(["normal", "atypical_design"])]
    print("\n== 주 평가셋 (normal + atypical_design): %d건 / 양성 %d건"
          % (len(main_set), int((main_set["라벨"] == "atypical_design").sum())))
    print("== 분리 보고 (data_error %d / uncertain %d)"
          % (int((m["라벨"] == "data_error").sum()),
             int((m["라벨"] == "uncertain").sum())))

    moved = m[(m["이전라벨"] == "비전형") & (m["라벨"] != "atypical_design")]
    print("\n== 이전 '비전형' 중 atypical_design 이 아니게 된 %d건" % len(moved))
    for _, r in moved.iterrows():
        print("  %-46s -> %-16s %s"
              % (str(r["사업명"])[:44], r["라벨"], str(r["라벨근거"])[:70]))

    C.save_report("m33_m3_relabel.json", {
        "labels": LABELS,
        "n": int(len(m)),
        "label_dist": {k: int(v) for k, v in m["라벨"].value_counts().items()},
        "prev_vs_new": {str(a): {str(b): int(c) for b, c in row.items()}
                        for a, row in ct.iterrows()},
        "main_eval_n": int(len(main_set)),
        "main_eval_positive": int((main_set["라벨"] == "atypical_design").sum()),
        "clean_holdout": CLEAN,
        "caveat": ("라벨러(claude)가 M30 의 FN/FP 목록을 이미 읽은 상태다. "
                   "완전한 blind 가 아니라서 규칙을 시트 열기 전에 문자로 고정하고 "
                   "행별 근거를 남겼다."),
    })
    write_md(m, ct, main_set, moved)


def write_md(m, ct, main_set, moved):
    L = ["# M33 — 교정된 값으로 다시 라벨링한 clean hold-out", "",
         "> M32 가 파서를 고쳤으므로 이전 라벨은 **이제 존재하지 않는 숫자**를 보고",
         "> 내린 판정입니다. 같은 50건을 교정된 값으로 다시 읽고 네 칸으로 나눴습니다.", "",
         "## 1. 라벨 정의", "",
         "| 라벨 | 뜻 | 평가에서의 역할 |", "|---|---|---|",
         "| `normal` | 비교군 안에서 특별히 드문 설계가 아니다 | 주 평가 음성 |",
         "| `atypical_design` | 입력값이 정상인데도 설계 조합이 드물다 | **주 평가 양성** |",
         "| `data_error` | 교정 후에도 파싱·단위·필드의미 오류가 남았다 | 분리 보고 |",
         "| `uncertain` | 수치 축이 얇거나 비교군이 부족해 판단 불가 | 분리 보고 |", "",
         "M30 의 '경계' 칸은 없앴습니다. 그 칸은 사실상 \"정상인데 눈에 띈다\"였고,",
         "positive 로 세느냐 마느냐에 따라 recall 이 0.30~0.33 사이를 오갔습니다.",
         "판단이 안 되는 것(`uncertain`)과 판단해서 정상인 것(`normal`)은 다릅니다.", "",
         "## 2. 새 라벨 분포", "", "| 라벨 | 건수 |", "|---|---:|"]
    for k, v in m["라벨"].value_counts().items():
        L.append("| `%s` | %d |" % (k, v))
    L += ["", "## 3. 이전 라벨과의 이동", "",
          "| 이전 \\ 새 | " + " | ".join("`%s`" % c for c in ct.columns) + " |",
          "|---|" + "---:|" * len(ct.columns)]
    for a, row in ct.iterrows():
        L.append("| %s | %s |" % (a, " | ".join(str(int(v)) for v in row)))
    L += ["",
          "### 이전 '비전형' %d건 중 `atypical_design` 이 아니게 된 %d건"
          % (int((m["이전라벨"] == "비전형").sum()), len(moved)), "",
          "| 사업 | 새 라벨 | 근거 |", "|---|---|---|"]
    for _, r in moved.iterrows():
        L.append("| %s | `%s` | %s |" % (str(r["사업명"])[:44], r["라벨"],
                                         str(r["라벨근거"])[:90]))
    L += ["", "## 4. 주 평가셋", "",
          "```text",
          "주 평가   normal + atypical_design = %d건 (양성 %d건)"
          % (len(main_set), int((main_set["라벨"] == "atypical_design").sum())),
          "분리 보고 data_error %d건 / uncertain %d건"
          % (int((m["라벨"] == "data_error").sum()),
             int((m["라벨"] == "uncertain").sum())),
          "```", "",
          "## 5. 한계", "",
          "- 라벨러(claude)가 M30 의 FN/FP 목록을 이미 읽은 상태입니다. **완전한",
          "  blind 가 아닙니다.** 규칙을 시트 열기 전에 문자로 고정하고 행별 근거를",
          "  남긴 것이 그 대용입니다.",
          "- 라벨러 1인. 라벨러 간 일치도를 낼 수 없습니다.",
          "- 양성 %d건뿐이라 recall 의 신뢰구간이 넓습니다."
          % int((main_set["라벨"] == "atypical_design").sum()),
          "- 이 세트는 threshold 튜닝에 쓰지 않습니다.", ""]
    p = C.report_path("m33_m3_relabel.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", action="store_true", help="라벨링 시트 생성")
    ap.add_argument("--finalize", action="store_true", help="채워진 라벨로 clean hold-out 확정")
    a = ap.parse_args()
    if a.finalize:
        finalize()
    else:
        build_sheet()
