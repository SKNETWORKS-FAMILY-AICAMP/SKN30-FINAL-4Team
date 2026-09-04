r"""M41 — 모델 3 두 번째 라벨 세트: 설계축 층화로 뽑은 독립 검증셋 (M39 남은것 1, M40 §3).

왜 기존 50건을 늘리지 않는가
    M30 의 50건은 **파서 수정 전(M32) 모델 점수**로 층화한 표본이다. 그 점수를
    만든 값 자체가 틀려 있었고(M31), 고치자 라벨 8건이 다른 칸으로 옮겨갔다(M33).
    같은 방식으로 20건을 덧붙이면 같은 편향을 20건만큼 더 사는 것이다.
    그래서 새 표본을 처음부터 다시 뽑는다.

무엇을 층화 기준으로 쓰는가 — 모델이 아니라 **설계와 데이터 품질**
    n_axes         M40 §3 에서 `n_axes` 단독 ROC-AUC 가 0.704 로 나왔다. 수치 축이
                   몇 개 채워졌는지만으로 라벨이 어느 정도 맞는다는 뜻이고,
                   층화하지 않으면 '설계가 드문 것'과 '원문에 항목을 많이 적는
                   유형'을 영영 못 가른다. **1순위 층화축이다.**
    데이터 품질     M30 의 '비전형' 15건 중 8건이 설계가 아니라 값 오류였다.
                   품질 계층을 미리 갈라 두면 atypical_design 과 data_error 가
                   표본 단계에서 섞이지 않는다. **1순위 층화축이다.**
    지원성격/방식/단위  비교군을 정의하는 세 축이다(M12/M38). 비교군마다 퍼진
                   정도가 달라서, 특정 비교군에 표본이 몰리면 거리 기반 점수의
                   평가가 그 비교군 성질에 끌려간다. **2순위 층화축이다.**

M38 점수는 어디에 쓰는가 — **선택이 아니라 확인**
    뽑은 뒤에 비교군거리 percentile 의 분포를 pool 과 맞춰 본다(십분위 커버리지,
    KS). 한쪽으로 쏠렸으면 그 사실을 리포트에 적는다. **점수를 보고 표본을
    바꾸지 않는다.** 바꾸는 순간 이 세트는 다시 모델 의존 표본이 되고, M30 이
    빠졌던 자리로 되돌아간다.

독립성 보장
    · 기존 50건의 row_id 를 pool 에서 뺀다.
    · 기존 50건과 같은 program_stem(재공고 계열)도 뺀다. 같은 사업의 다른 회차가
      들어오면 두 세트가 독립이 아니게 된다 (M19 가 연도 hold-out 에서 걸린 자리).
    · 새 표본 안에서도 program_stem 중복을 허용하지 않는다.

두 단계로 나눈 이유는 M33 과 같다 — 시트를 만들면서 점수를 본 채로 라벨을
붙이는 일을 막는다
    (인자 없음)  점수를 뺀 라벨링 시트를 만든다 (라벨러가 볼 것)
    --label      LABEL_A 를 붙여 검증셋을 확정하고, 2인 일치도와 실행 기록을 낸다

라벨 규칙 — M33 과 **글자 그대로 같게** 둔다
    두 세트를 합쳐 보려면 라벨 정의가 같아야 한다. 규칙을 여기서 손보면 세트 간
    차이가 모델 성능인지 라벨 기준 변경인지 못 가린다.

    atypical_design  설계 축 둘 이상이 비교군 극단(P>=90 또는 P<=10)이거나,
                     축 하나가 극단인데 사업 유형으로 설명되지 않는 경우
    normal           비교군 대비 특별히 드문 조합이 아닌 경우.
                     축 하나가 치우쳐 있어도 사업 유형으로 설명되면 여기.
    data_error       교정 후에도 값이 상식 범위 밖이거나(기업당액 SANE_RANGE 밖,
                     기간 10년 초과), 필드 의미가 서로 모순되는 경우
    uncertain        수치 축이 2개 미만이거나 비교군이 전부 '비교불가'인 경우
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
from m33_m3_relabel import LABELS, PCT_EXTREME, won

CLEAN1 = os.path.join(C.DATA, "labels", "m3_clean_holdout.csv")
SHEET = C.report_path("m41_labelset2_sheet.csv")
OUT = os.path.join(C.DATA, "labels", "m3_holdout2.csv")

N_TARGET = 70
SEED = 20260827          # M33 시트 셔플 시드(20260826)와 다르게 둔다
STYPE_TOP = ["사업화", "연구개발", "융자", "판로", "컨설팅", "설비", "고용보조"]
MIN_CELL = 20            # 2단 셀이 이보다 얇으면 '잔여조합' 으로 합친다
POWER2 = 0.5             # 2단 배분 지수. 1=비례, 0=균등, 0.5=제곱근(절충)
REST = "잔여조합"
DUAL_SEED = 777          # 2인 라벨링 대상 20건을 뽑은 시드


# ------------------------------------------------------------------ 층화 기준
# ---------------------------------------------------------------- 라벨 (A / B)
# 라벨러 A 가 70건에 붙인 판정과 근거. 시트 CSV 를 주고받지 않으므로
# 이 딕셔너리가 곧 라벨링 기록이다. 키는 시트의 행 순서(0~69)다.
LABEL_A = {
0:("normal","기간 P90은 융자 대출기간 10년. 융자 유형으로 설명된다. 한도 10억 P58"),
1:("normal","두 축 모두 극단 아님(한도 P75 / 비율 P45)"),
2:("normal","두 축 모두 중앙 근처(기업수 P57 / 한도 P55)"),
3:("normal","팀당 1억·30팀. 두 축 극단 아님. 다만 team 단위 비교군이 n=9로 얇다"),
4:("data_error","원문 '9개, 200백만원'인데 파서가 설명문의 '5억원 이상 투자유치'를 금액으로 잡아 amount_outlier 처리, 금액축이 통째로 비었다"),
5:("data_error","총예산 3000억/6 = 기업당 500억. SANE_RANGE 밖"),
6:("normal","두 축 P76/P75, 극단 아님"),
7:("atypical_design","기업수 P10 + 지원비율 P90 동시 극단. 4개사·자부담 10%는 사업화 비교군에서 드문 조합"),
8:("atypical_design","한도 P100 + 비율 P90 동시 극단. 고용보조에서 기업당 5000만원·90% 지원"),
9:("normal","비율 P90 한 축만 극단. 지자체 소규모 시설개선 80% 보조로 설명된다"),
10:("normal","한도 P20 / 기업수 P71, 극단 아님"),
11:("normal","세 축 모두 극단 아님"),
12:("data_error","총예산 5000만원/350 = 기업당 14.3만원. 산불피해복구 지원으로 성립하지 않고 성격도 컨설팅으로 잘못 붙었다"),
13:("normal","한도 P26 / 비율 P38, 극단 아님"),
14:("data_error","1억은 지원금이 아니라 '보험한도'(부보금액). 지원한도 축과 의미가 다르다"),
15:("normal","한도 P90 한 축만 극단. 지자체 육성자금 시설자금 융자로 설명된다"),
16:("atypical_design","비율 P90 + 기간 P10 동시 극단. 1년 단기 R&D에 80% 지원"),
17:("normal","세 축 모두 극단 아님"),
18:("data_error","사업기간 7.0은 '창업 7년 이내' 신청자격을 기간으로 오파싱"),
19:("normal","기간 P10 한 축만 극단. 1년 R&D 트랙으로 설명된다. 다만 금액 8억(중기지원 2년)과 기간 1년(융합촉진)이 서로 다른 트랙에서 왔다"),
20:("normal","세 축 모두 중앙 근처"),
21:("atypical_design","기업수 P10 + 비율 P10 동시 극단. 2개사·자부담 50% 해외실증. 다만 비교군이 전역 레벨로 물러나 percentile 신뢰도는 낮다"),
22:("normal","한도 P59 / 기업수 P25, 극단 아님"),
23:("data_error","원문 '5억원'인데 총기금 80억/4로 기업당 20억이 산출됐다. 기업수 4도 '일반기업 4%'의 4로 보인다"),
24:("normal","기업수 P100 한 축만 극단. 전국 단위 연구인력 인건비 지원 규모(775개사)로 설명된다"),
25:("normal","기업수 P34 / 한도 P54, 극단 아님"),
26:("normal","비교 가능 축이 사업기간(전체 레벨) 하나뿐이고 P25로 극단 아님"),
27:("data_error","총예산 1500만원을 기업수 1로 나눈 값. 기업당 한도 축에 사업 총예산이 들어갔다"),
28:("data_error","총예산 2550억/12 = 기업당 212.5억. SANE_RANGE 밖"),
29:("normal","기업수 P10 한 축만 극단. 로봇 분야 한정 소규모 사업으로 설명된다"),
30:("atypical_design","과제당 45억 P90 + 비율 P90 동시 극단"),
31:("normal","한도 P90 한 축만 극단. 지자체 육성자금 융자로 설명된다. 방식 grant는 이차보전 구조로 설명 가능"),
32:("normal","기업수 P10 한 축만 극단. 3차 모집 소규모 기획지원으로 설명된다"),
33:("normal","한도 P80 / 기업수 P78, 극단 아님"),
34:("normal","한도 P40 / 기업수 P76, 극단 아님"),
35:("normal","한도 P20 / 기업수 P42, 극단 아님"),
36:("atypical_design","과제당 7억 P100 + 기간 P10 동시 극단. 사업화 비교군에서 자릿수가 다르다"),
37:("normal","네 축 모두 극단 아님"),
38:("atypical_design","한도 P100 + 비율 P10 동시 극단. '수출통관' 성격에 홍보관 상설운영이 붙은 성격 오분류 소지도 있다"),
39:("atypical_design","기업수 P0 + 한도 P90 + 비율 P90 세 축 동시 극단"),
40:("normal","세 축 모두 극단 아님"),
41:("uncertain","비교군이 전부 비교불가. 총예산/6 값 자체는 상식범위 안"),
42:("normal","한도 P100 한 축만 극단. 글로벌 강소기업 패키지 지원으로 설명된다"),
43:("data_error","총예산 5억을 기업수 1로 나눈 값. 기업당 한도 축에 사업 총예산이 들어갔다"),
44:("uncertain","비교군이 전부 비교불가. 총예산 800만원/4 = 200만원은 원문과 일관"),
45:("normal","한도 P75 / 비율 P25, 극단 아님"),
46:("atypical_design","과제당 4.6억 P100 + 비율 P90 동시 극단"),
47:("data_error","지원비율 3.5는 이차보전 이율(연 3.5%)이지 지원비율이 아니다"),
48:("data_error","사업기간 22.0년. duration_basis=bare로 원문 근거가 없다"),
49:("normal","한도 P50 / 기업수 P25, 극단 아님"),
50:("normal","기간 P10 한 축만 극단. 프리팁스 1년 과제로 설명된다"),
51:("normal","한도 P37 / 기업수 P36, 극단 아님"),
52:("normal","기업수 P10 한 축만 극단. 장비국산화 소수 대형과제로 설명된다"),
53:("normal","비교 가능 축이 지원비율 하나뿐이고 P90이나 사회적기업가 육성 90% 지원으로 설명된다"),
54:("data_error","총예산 5000만원을 기업수 1로 나눈 값. 참여기업 모집 공고인데 기업수 1은 성립하지 않는다"),
55:("uncertain","비교군이 전부 비교불가. 주관기관 8곳 운영비로 값 자체는 합리적"),
56:("normal","비율 P90 한 축만 극단. 컨설팅 자부담 10% 구조로 설명된다"),
57:("normal","한도 P77 / 기업수 P36, 극단 아님"),
58:("normal","한도 P10 한 축만 극단. 연구소 설립 컨설팅 소액 지원으로 설명된다"),
59:("data_error","총예산 2000만원을 기업수 1로 나눈 값. 기업당 한도 축에 사업 총예산이 들어갔다"),
60:("data_error","과제당 100억은 인재양성 과제 규모로 성립하지 않는다. 총사업비를 과제당 한도로 읽었고 기업수 1도 그 흔적이다. 원문 근거는 제목뿐"),
61:("normal","한도 P26 / 기업수 P63, 극단 아님. 창업보육 비교군이 얇아 단위 레벨로 물러났다"),
62:("normal","한도 P90 한 축만 극단. SaaS 글로벌 육성 대형과제로 설명된다"),
63:("normal","한도 P16 / 비율 P75, 극단 아님"),
64:("normal","비율 P10 한 축만 극단. 타당성조사 50% 매칭으로 설명된다"),
65:("normal","한도 P77 / 기업수 P68, 극단 아님"),
66:("normal","비율 P90 한 축만 극단(기업수 P89는 문턱 아래). R&D 80% 지원은 흔하다"),
67:("normal","한도 P35 / 기업수 P64, 극단 아님"),
68:("normal","한도 P56 / 비율 P25, 극단 아님"),
69:("normal","기업수 P100 한 축만 극단. 환경부 대표 사업화 지원의 실제 규모(115개사)로 설명된다"),
}

# 라벨러 B — 무작위 20건을 A 의 라벨을 보지 못한 채 다시 붙였다.
# 두 라벨러가 모두 같은 모델(Claude)이라 사람 2인 일치도의 **대용치**다.
LABEL_B = {
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

def axes_tier(n):
    return "축2" if n == 2 else ("축3" if n == 3 else "축4")


def stype_tier(s):
    """지원성격 23종을 그대로 쓰면 70건에 셀이 더 많아진다. 상위 7종만 남긴다."""
    return s if s in STYPE_TOP else "기타성격"


def method_tier(s):
    return s if s in ("grant", "loan", "voucher") else "기타방식"


def unit_tier(s):
    if pd.isna(s):
        return "단위미상"
    return s if s in ("company", "project") else "기타단위"


def dq_tier(df):
    """데이터 품질 3계층. 위쪽이 이긴다 (의심 > 파생 > 직접기재).

    M30 이 '비전형'으로 잡았다가 M33 에서 data_error 로 내려간 행들은 전부
    `budget_div_count`(총액/기업수) 아니면 상식범위 밖이었다. 그 두 갈래를
    표본 단계에서 분리한다.
    """
    lo, hi = C.SANE_RANGE["per_company"]
    pr, dur = df["per_recipient"], df["project_duration"]
    suspect = (df["amount_outlier"].fillna(False).astype(bool)
               | (pr.notna() & ~pr.between(lo, hi))
               | (dur.notna() & (dur > 10)))
    derived = (df["per_recipient_basis"].eq("budget_div_count")
               | df["amount_type"].isin(["total_budget", "periodic", "unknown"])
               | df["amount_type"].isna())
    return np.where(suspect, "C_의심", np.where(derived, "B_파생", "A_직접기재"))


def add_strata(d):
    d = d.copy()
    d["st_axes"] = [axes_tier(n) for n in d["n_axes"]]
    d["st_dq"] = dq_tier(d)
    d["st_type"] = [stype_tier(s) for s in d["support_type"]]
    d["st_method"] = [method_tier(s) for s in d["support_method"]]
    d["st_unit"] = [unit_tier(s) for s in d["support_unit"]]
    # 구분자로 '|' 를 쓰면 markdown 표에서 셀이 쪼개진다. 가운뎃점을 쓴다.
    d["st_stage1"] = d["st_dq"] + " · " + d["st_axes"]
    d["st_stage2"] = d["st_type"] + " · " + d["st_method"] + " · " + d["st_unit"]
    return d


# ------------------------------------------------------------------ 배분
def allocate(counts, n_total, floor=1, power=1.0):
    """층별 배분. 결정적(난수 없음)이다.

    `power` 는 비례(1.0)와 균등(0.0) 사이를 잇는다 — n_h ∝ N_h^power.
    1단은 비례(1.0)+floor 로 pool 구성을 따라가고, 2단은 제곱근(0.5)을 쓴다.
    2단을 비례로만 두면 `사업화|grant|company` 한 칸이 표본의 3분의 1을 먹고,
    균등으로 두면 pool 에 3건뿐인 조합이 큰 칸과 같은 대접을 받는다.
    """
    keys = [k for k, v in counts.items() if v > 0]
    if not keys:
        return {}
    cap = {k: int(counts[k]) for k in keys}
    if n_total >= sum(cap.values()):
        return dict(cap)
    wt = {k: cap[k] ** power for k in keys}

    alloc = {k: 0 for k in keys}
    for k in sorted(keys, key=lambda k: (-cap[k], k)):        # 큰 셀부터 floor
        if sum(alloc.values()) >= n_total:
            break
        alloc[k] = min(floor, cap[k], n_total - sum(alloc.values()))

    while sum(alloc.values()) < n_total:
        rem = n_total - sum(alloc.values())
        open_k = [k for k in keys if alloc[k] < cap[k]]
        if not open_k:
            break
        w = sum(wt[k] for k in open_k)
        share = {k: rem * wt[k] / w for k in open_k}
        add = {k: min(int(np.floor(share[k])), cap[k] - alloc[k]) for k in open_k}
        left = rem - sum(add.values())
        for k in sorted(open_k, key=lambda k: (-(share[k] % 1.0), -cap[k], k)):
            if left <= 0:
                break
            if alloc[k] + add[k] < cap[k]:
                add[k] += 1
                left -= 1
        if sum(add.values()) == 0:                            # 교착 방지
            add[max(open_k, key=lambda k: (cap[k], k))] = 1
        for k in open_k:
            alloc[k] += add[k]
    return alloc


def draw(pool, n_total, seed=SEED):
    """2단 층화 추출. 1단 = 데이터품질 x 수치축, 2단 = 성격 x 방식 x 단위.

    1단을 먼저 확정하는 이유: 이 두 축은 M40/M30 이 지목한 교란요인이라
    비교군 축보다 우선해서 통제해야 한다.
    """
    rng = np.random.default_rng(seed)
    a1 = allocate(pool["st_stage1"].value_counts().to_dict(), n_total, floor=1)

    used_stem, picked, plan = set(), [], []
    for k1 in sorted(a1, key=lambda k: (-a1[k], k)):
        cell = pool[pool["st_stage1"] == k1]
        a2 = allocate(cell["st_cell2"].value_counts().to_dict(), a1[k1],
                      floor=0, power=POWER2)
        for k2 in sorted(a2, key=lambda k: (-a2[k], k)):
            sub = cell[cell["st_cell2"] == k2]
            sub = sub.sample(frac=1.0, random_state=int(rng.integers(1e9)))
            got = 0
            for _, r in sub.iterrows():
                if got >= a2[k2]:
                    break
                if r["program_stem"] in used_stem:            # 재공고 계열 중복 금지
                    continue
                used_stem.add(r["program_stem"])
                picked.append(r["row_id"])
                got += 1
            plan.append({"stage1": k1, "stage2": k2, "pool": int(len(sub)),
                         "quota": int(a2[k2]), "drawn": got})

    short = n_total - len(picked)
    if short > 0:            # stem 중복으로 못 채운 몫은 pool 비례로 다시 채운다
        rest = pool[~pool["row_id"].isin(picked)
                    & ~pool["program_stem"].isin(used_stem)]
        rest = rest.sample(frac=1.0, random_state=int(rng.integers(1e9)))
        for _, r in rest.head(short).iterrows():
            picked.append(r["row_id"])
            plan.append({"stage1": r["st_stage1"], "stage2": r["st_cell2"],
                         "pool": 0, "quota": 0, "drawn": 1})
    return picked, pd.DataFrame(plan), a1


def coarsen_stage2(pool):
    """pool 에서 MIN_CELL 건 미만인 2단 조합은 REST 한 칸으로 합친다.

    합치지 않으면 70건에 2단 셀이 70개가 되어, 층화가 아니라 '희귀 조합을
    하나씩 훑기'가 된다. 실제로 그렇게 뽑아 보니 voucher 비중이 pool 의
    4.4배로 부풀었다.
    """
    vc = pool["st_stage2"].value_counts()
    keep = set(vc[vc >= MIN_CELL].index)
    return pool["st_stage2"].where(pool["st_stage2"].isin(keep), REST)


# ------------------------------------------------------------------ 보조 확인
def m38_balance(train, picked):
    """M38 비교군거리 percentile 의 표본/pool 분포 비교. **선택에 쓰지 않는다.**"""
    from scipy.stats import ks_2samp
    from m38_m3_vector_direction import build_vectors, score_components

    Xtr, Xap, _, n_num = build_vectors(train, train)
    comp = score_components(train, train, Xtr, Xap, n_num)
    s = pd.Series(comp["dist_pct"].to_numpy(float), index=train["row_id"].to_numpy())
    pool_s = s.to_numpy()
    samp_s = s.loc[[r for r in picked if r in s.index]].to_numpy()

    edges = np.arange(0, 101, 10)
    ph = np.histogram(pool_s, bins=edges)[0] / len(pool_s)
    sh = np.histogram(samp_s, bins=edges)[0] / len(samp_s)
    ks = ks_2samp(pool_s, samp_s)
    cut = float(np.quantile(pool_s, 0.98))
    return {
        "note": "균형 확인용 보조 변수. 이 값으로 표본을 다시 뽑지 않았다.",
        "pool_n": int(len(pool_s)), "sample_n": int(len(samp_s)),
        "decile_edges": edges.tolist(),
        "pool_share": [round(float(v), 4) for v in ph],
        "sample_share": [round(float(v), 4) for v in sh],
        "empty_deciles": [int(i) for i, v in enumerate(sh) if v == 0],
        "ks_stat": round(float(ks.statistic), 4), "ks_p": round(float(ks.pvalue), 4),
        "sample_median_pct": round(float(np.median(samp_s)), 2),
        "pool_median_pct": round(float(np.median(pool_s)), 2),
        "operating_cut_p98": round(cut, 2),
        "sample_above_operating_cut": int((samp_s >= cut).sum()),
    }


def axis_notes(row, ref):
    out = []
    for axis, label in AXIS_LABEL.items():
        c = compare(ref, axis, row.get(axis), row["support_type"],
                    row["support_method"], row.get("support_unit"), row["cohort"])
        if c["status"] == "비교불가":
            continue
        out.append({"label": label, "pct": c["percentile_rank"],
                    "n": c["n"], "level": c["level"]})
    return sorted(out, key=lambda x: -abs(x["pct"] - 50))


# ------------------------------------------------------------------ 시트
def build_sheet():
    df = prepare(pd.read_parquet(SRC))
    train = add_strata(df[df["n_axes"] >= MIN_AXES].reset_index(drop=True))
    ref = pd.read_parquet(REF)
    old = pd.read_csv(CLEAN1, encoding="utf-8-sig")

    old_ids = set(old["row_id"])
    old_stems = set(train[train["row_id"].isin(old_ids)]["program_stem"].dropna())
    pool = train[~train["row_id"].isin(old_ids)
                 & ~train["program_stem"].isin(old_stems)].reset_index(drop=True)
    pool["st_cell2"] = coarsen_stage2(pool)

    print("M41 — 두 번째 라벨 세트 (독립 검증셋)")
    print("  학습 pool %d행 -> 기존 50건 제외 -> 재공고 계열 제외 -> %d행"
          % (len(train), len(pool)))
    print("  제외: row_id %d / 같은 program_stem %d"
          % (len(old_ids & set(train["row_id"])),
             int(train["program_stem"].isin(old_stems).sum()) - len(old_ids & set(train["row_id"]))))

    picked, plan, a1 = draw(pool, N_TARGET)
    samp = pool[pool["row_id"].isin(picked)].copy()
    print("  추출 %d건 / 1단 셀 %d개 / 2단 셀 %d개"
          % (len(samp), len(a1), int(plan["quota"].gt(0).sum())))

    lo, hi = C.SANE_RANGE["per_company"]
    rows = []
    for _, r in samp.iterrows():
        notes = axis_notes(r, ref)
        ext = [n for n in notes if n["pct"] >= PCT_EXTREME or n["pct"] <= 100 - PCT_EXTREME]
        pr, dur = r["per_recipient"], r["project_duration"]
        rows.append({
            "row_id": r["row_id"],
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
            "극단축_개수": len(ext),
            "상식범위밖": bool((pd.notna(pr) and not (lo <= pr <= hi))
                          or (pd.notna(dur) and dur > 10)),
            "층_품질": r["st_dq"], "층_축수": r["st_axes"],
            "층_비교군": r["st_stage2"],
            "원문발췌": str(r["evidence_text"] or "").replace("\n", " ")[:180],
            "라벨": "", "라벨근거": "", "라벨러": "",
        })
    sheet = pd.DataFrame(rows).sample(frac=1.0, random_state=SEED).reset_index(drop=True)
    sheet.to_csv(SHEET, index=False, encoding="utf-8-sig")
    print("\n[sheet] %s  %d행" % (SHEET, len(sheet)))
    print("  라벨 선택지: %s" % " / ".join(LABELS))
    print("  점수 없음. 비교군 percentile 은 모델 출력이 아니라 관측 분포값이다.")

    marg = {}
    for col, name in (("st_dq", "데이터품질"), ("st_axes", "수치축"),
                      ("st_type", "지원성격"), ("st_method", "지원방식"),
                      ("st_unit", "지원단위")):
        p = pool[col].value_counts(normalize=True)
        s = samp[col].value_counts(normalize=True)
        marg[name] = {str(k): {"pool": round(float(p.get(k, 0)) * 100, 1),
                               "sample": round(float(s.get(k, 0)) * 100, 1),
                               "sample_n": int((samp[col] == k).sum())}
                      for k in sorted(set(p.index) | set(s.index))}
        print("\n== %s (pool%% / 표본%% / 표본n)" % name)
        for k, v in marg[name].items():
            print("  %-12s %5.1f / %5.1f / %d" % (k, v["pool"], v["sample"], v["sample_n"]))

    bal = m38_balance(train, picked)
    print("\n== M38 비교군거리 percentile — 균형 확인 (보조)")
    print("  표본 중앙값 P%.1f / pool 중앙값 P%.1f / KS %.3f (p=%.3f)"
          % (bal["sample_median_pct"], bal["pool_median_pct"], bal["ks_stat"], bal["ks_p"]))
    print("  비어 있는 십분위: %s" % (bal["empty_deciles"] or "없음"))
    print("  운영 경고선(P98) 이상: %d건" % bal["sample_above_operating_cut"])

    under = ["%s/%s (pool %.1f%% -> 표본 %.1f%%)" % (n, k, v["pool"], v["sample"])
             for n, m in marg.items() for k, v in m.items()
             if v["pool"] >= 3.0 and v["sample"] < v["pool"] * 0.5]
    if under:
        print("\n== 표본에서 얇아진 계층 (미리 적어 둔다)")
        for u in under:
            print("  %s" % u)

    rep = {
        "목적": "M39 남은것 1 — 모델 점수에 의존하지 않는 두 번째 라벨 세트",
        "n_target": N_TARGET, "n_drawn": int(len(samp)), "seed": SEED,
        "pool": {"train_rows": int(len(train)), "pool_rows": int(len(pool)),
                 "excluded_row_id": int(len(old_ids & set(train["row_id"]))),
                 "excluded_same_stem": int(train["program_stem"].isin(old_stems).sum())
                 - int(len(old_ids & set(train["row_id"])))},
        "stratification": {
            "stage1": "데이터품질(A_직접기재/B_파생/C_의심) x 수치축(2/3/4)",
            "stage2": "지원성격(상위7+기타) x 지원방식(grant/loan/voucher+기타) x 지원단위",
            "rule": ("1단 비례배분(power=1.0)+셀당 최소 1건, "
                     "2단 제곱근배분(power=%.1f). pool %d건 미만 2단 조합은 '%s' 로 합침"
                     % (POWER2, MIN_CELL, REST)),
            "min_cell": MIN_CELL, "power_stage2": POWER2,
            "why_stage1_first": ("n_axes 단독 ROC-AUC 0.704(M40 §3), "
                                 "M30 '비전형' 15건 중 8건이 값 오류(M33)"),
        },
        "stage1_allocation": {k: int(v) for k, v in a1.items()},
        "stage2_allocation": [{k: (int(v) if k in ("pool", "quota", "drawn") else v)
                               for k, v in row.items()}
                              for row in plan.to_dict("records") if row["drawn"] > 0],
        "marginals": marg,
        "under_covered": under,
        "m38_balance_check": bal,
        "independence": ("기존 50건의 row_id 와 program_stem 을 pool 에서 뺐다. "
                         "새 표본 안에서도 program_stem 중복을 허용하지 않았다."),
        "label_rule": "M33 과 동일 (normal / atypical_design / data_error / uncertain)",
        "sheet": SHEET,
    }
    C.save_report("m41_m3_labelset2.json", rep)
    write_md(rep, plan)


# ------------------------------------------------------------------ 라벨링
def apply_labels():
    """LABEL_A 를 시트에 붙여 검증셋을 확정한다.

    예전에는 시트 CSV 를 내보내고 사람이 채워 다시 읽는 2단계였다. 판정과
    근거가 LABEL_A 에 코드로 들어와 있으므로 중간 파일이 필요 없다.
    """
    if not os.path.exists(SHEET):
        sys.exit("시트가 없습니다. 먼저 인자 없이 실행해 추출하십시오: %s" % SHEET)
    f = pd.read_csv(SHEET, encoding="utf-8-sig")
    if len(f) != len(LABEL_A):
        sys.exit("시트 %d행 vs 라벨 %d건 — 어긋납니다" % (len(f), len(LABEL_A)))
    bad = {v[0] for v in LABEL_A.values()} - set(LABELS)
    if bad:
        sys.exit("허용되지 않은 라벨: %s" % bad)

    f["라벨"] = [LABEL_A[i][0] for i in range(len(f))]
    f["라벨근거"] = [LABEL_A[i][1] for i in range(len(f))]
    f["라벨러"] = "claude-A"
    f.to_csv(OUT, index=False, encoding="utf-8-sig")

    main = f[f["라벨"].isin(["normal", "atypical_design"])]
    print("M41 — 두 번째 검증셋 확정")
    print("  [labels] %s" % OUT)
    print("\n== 라벨 분포")
    for k, v in f["라벨"].value_counts().items():
        print("  %-16s %d" % (k, v))
    print("\n== 주 평가셋 %d건 / 양성 %d건"
          % (len(main), int((main["라벨"] == "atypical_design").sum())))
    ct_dq = pd.crosstab(f["층_품질"], f["라벨"])
    ct_ax = pd.crosstab(f["층_축수"], f["라벨"])
    print("\n== 층별 라벨 (데이터품질 x 라벨)")
    print(ct_dq.to_string())
    print("\n== 층별 라벨 (수치축 x 라벨)")
    print(ct_ax.to_string())

    old = pd.read_csv(CLEAN1, encoding="utf-8-sig")
    om = old[old["라벨"].isin(["normal", "atypical_design"])]
    print("\n== 합산 (M33 + M41)")
    print("  주 평가 %d건 / 양성 %d건"
          % (len(om) + len(main),
             int((om["라벨"] == "atypical_design").sum())
             + int((main["라벨"] == "atypical_design").sum())))

    dual = dual_agreement(f)
    fin = {
        "n": int(len(f)),
        "label_dist": {k: int(v) for k, v in f["라벨"].value_counts().items()},
        "main_eval_n": int(len(main)),
        "main_eval_positive": int((main["라벨"] == "atypical_design").sum()),
        "by_dq": {str(a): {str(b): int(c) for b, c in r.items()} for a, r in ct_dq.iterrows()},
        "by_axes": {str(a): {str(b): int(c) for b, c in r.items()} for a, r in ct_ax.iterrows()},
        "combined_with_m33": {
            "main_eval_n": int(len(om) + len(main)),
            "main_eval_positive": int((om["라벨"] == "atypical_design").sum())
            + int((main["라벨"] == "atypical_design").sum())},
        "dual_labeling": dual,
        "holdout": OUT,
    }
    C.save_report("m41_m3_labelset2_final.json", fin)
    execution_md(f, fin, dual)


def dual_agreement(f):
    """A 와 B 의 일치도. B 는 A 의 라벨을 보지 못한 채 무작위 20건을 다시 붙였다."""
    from sklearn.metrics import cohen_kappa_score, confusion_matrix
    a = f.set_index("row_id")["라벨"]
    ids = [i for i in LABEL_B if i in a.index]
    ya = [a.loc[i] for i in ids]
    yb = [LABEL_B[i][0] for i in ids]
    agree = sum(x == y for x, y in zip(ya, yb))
    kappa = float(cohen_kappa_score(ya, yb))
    cm = confusion_matrix(ya, yb, labels=LABELS)

    print("\n== 2인 라벨링 일치도 (%d건)" % len(ids))
    print("  단순 agreement %d/%d = %.1f%%" % (agree, len(ids), agree / len(ids) * 100))
    print("  Cohen's kappa  %.4f" % kappa)

    per = {}
    for L in LABELS:
        n = sum(1 for x in ya if x == L)
        if n:
            ok = sum(1 for x, y in zip(ya, yb) if x == L and y == L)
            per[L] = {"n_A": n, "agree": ok, "rate": round(ok / n, 4)}

    dis = [{"row_id": i, "A": a.loc[i], "B": LABEL_B[i][0],
            "A_reason": str(f.set_index("row_id").loc[i, "라벨근거"]),
            "B_reason": LABEL_B[i][1],
            "affects_clean_set": bool((a.loc[i] in ("normal", "atypical_design"))
                                      != (LABEL_B[i][0] in ("normal", "atypical_design")))}
           for i in ids if a.loc[i] != LABEL_B[i][0]]
    print("  불일치 %d건 (주 평가셋에 영향 %d건)"
          % (len(dis), sum(d["affects_clean_set"] for d in dis)))
    for d in dis:
        print("    %s  A=%s / B=%s" % (d["row_id"], d["A"], d["B"]))

    return {
        "n_dual": len(ids), "subset_seed": DUAL_SEED,
        "selection": "70건에서 무작위 %d건 (1차 라벨을 보지 않고 시드로 선정)" % len(ids),
        "labeler_A": "claude-A (전체 70건)",
        "labeler_B": "claude-B (서브에이전트, %d건, A의 라벨 미열람)" % len(ids),
        "caveat": ("두 라벨러가 모두 같은 모델(Claude)이다. 사람 2인이 아니므로 "
                   "inter-annotator agreement 의 대용치로만 읽어야 한다."),
        "agreement": round(agree / len(ids), 4), "n_agree": agree,
        "cohen_kappa": round(kappa, 4),
        "confusion_A_rows_B_cols": {LABELS[i]: {LABELS[j]: int(cm[i][j])
                                                for j in range(len(LABELS))}
                                    for i in range(len(LABELS))},
        "per_class_A": per, "disagreements": dis,
    }


def execution_md(f, fin, dual):
    """§1~§4 실행 기록 — freeze / blind 조건 / 2인 일치도 / finalize 결과."""
    import json
    fr = json.load(open(os.path.join(C.DATA, "labels", "m41_frozen_70.json"),
                        encoding="utf-8"))
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
         "대상   %s" % dual["selection"],
         "A      %s" % dual["labeler_A"],
         "B      %s" % dual["labeler_B"],
         "```", "",
         "| 지표 | 값 |", "|---|---:|",
         "| 단순 agreement | %d/%d = **%.0f%%** |" % (dual["n_agree"], dual["n_dual"], dual["agreement"] * 100),
         "| Cohen's kappa | **%.4f** |" % dual["cohen_kappa"], "",
         "클래스별 (A 기준)", "", "| 라벨 | 일치 / A의 건수 |", "|---|---:|"]
    for k, v in dual["per_class_A"].items():
        L.append("| `%s` | %d / %d |" % (k, v["agree"], v["n_A"]))
    L += ["", "### 불일치 %d건 — 전부 `data_error` 경계입니다" % len(dual["disagreements"]), "",
          "| row_id | A | B | 주 평가셋 영향 |", "|---|---|---|---|"]
    for d in dual["disagreements"]:
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
    for k, v in fin["label_dist"].items():
        L.append("| `%s` | %d |" % (k, v))
    L += ["", "```text",
          "주 평가(Clean second hold-out)  normal + atypical_design = %d건 (양성 %d건)"
          % (fin["main_eval_n"], fin["main_eval_positive"]),
          "제외                            data_error %d / uncertain %d"
          % (fin["label_dist"].get("data_error", 0), fin["label_dist"].get("uncertain", 0)),
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
    p = C.report_path("m41b_labeling_execution.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(chr(10).join(L))
    print("[report] %s" % p)


def write_md(r, plan):
    b = r["m38_balance_check"]
    L = ["# M41 — 두 번째 라벨 세트: 설계축 층화 독립 검증셋", "",
         "> M39 남은것 1. 기존 50건을 늘리지 않고 **새로 뽑았습니다.** 그 50건은",
         "> 파서 수정 전 모델 점수로 층화된 표본이라(M31/M32), 덧붙이면 같은 편향을",
         "> 그만큼 더 사게 됩니다.", "",
         "```text",
         "학습 pool %d행 -> 기존 50건 제외 -> 재공고 계열 제외 -> %d행 -> %d건 추출"
         % (r["pool"]["train_rows"], r["pool"]["pool_rows"], r["n_drawn"]),
         "seed %d" % r["seed"],
         "```", "",
         "## 1. 층화 기준 — 모델 점수를 쓰지 않습니다", "",
         "| 단계 | 축 | 왜 이 축인가 |", "|---|---|---|",
         "| 1단 | `데이터품질` x `수치축` | %s |" % r["stratification"]["why_stage1_first"],
         "| 2단 | `지원성격` x `지원방식` x `지원단위` | 비교군을 정의하는 세 축(M12/M38). 특정 비교군에 표본이 몰리면 평가가 그 비교군 성질에 끌려간다 |", "",
         "배분 규칙: **%s**" % r["stratification"]["rule"], "",
         "### 데이터품질 계층 정의", "",
         "| 계층 | 정의 |", "|---|---|",
         "| `A_직접기재` | 원문에 기업당 한도가 직접 적혀 있고 상식범위 안 |",
         "| `B_파생` | 총액/기업수 산출(`budget_div_count`) 또는 금액유형이 total_budget·periodic·unknown |",
         "| `C_의심` | 상식범위(SANE_RANGE) 밖, 사업기간 10년 초과, amount_outlier |", "",
         "> M30 이 '비전형'으로 잡았다가 M33 에서 `data_error` 로 내려간 행은 전부",
         "> `B_파생` 아니면 `C_의심` 이었습니다. 표본 단계에서 갈라 두면",
         "> `atypical_design` 과 `data_error` 가 섞이지 않습니다.", "",
         "## 2. 1단 배분", "", "| 1단 셀 | 배정 |", "|---|---:|"]
    for k, v in sorted(r["stage1_allocation"].items(), key=lambda kv: -kv[1]):
        L.append("| `%s` | %d |" % (k, v))
    L += ["", "## 3. 주변분포 — pool 과 표본", ""]
    for name, m in r["marginals"].items():
        L += ["### %s" % name, "", "| 계층 | pool % | 표본 % | 표본 n |",
              "|---|---:|---:|---:|"]
        for k, v in m.items():
            L.append("| `%s` | %.1f | %.1f | %d |" % (k, v["pool"], v["sample"], v["sample_n"]))
        L.append("")
    L += ["## 4. M38 점수 — 균형 확인용 보조 변수", "",
          "> **이 값으로 표본을 다시 뽑지 않았습니다.** 뽑은 뒤 비교군거리",
          "> percentile 이 한쪽으로 쏠렸는지만 확인합니다. 점수를 보고 표본을",
          "> 고치면 이 세트는 다시 모델 의존 표본이 되고, M30 이 빠졌던 자리로",
          "> 되돌아갑니다.", "",
          "| 항목 | 값 |", "|---|---:|",
          "| 표본 중앙값 percentile | P%.1f |" % b["sample_median_pct"],
          "| pool 중앙값 percentile | P%.1f |" % b["pool_median_pct"],
          "| KS 통계량 | %.4f |" % b["ks_stat"],
          "| KS p | %.4f |" % b["ks_p"],
          "| 비어 있는 십분위 | %s |" % (b["empty_deciles"] or "없음"),
          "| 운영 경고선(pool P98) 이상 | %d건 |" % b["sample_above_operating_cut"], "",
          "십분위별 비중 (pool / 표본)", "", "| 구간 | pool | 표본 |", "|---|---:|---:|"]
    for i in range(10):
        L.append("| P%d~P%d | %.3f | %.3f |"
                 % (i * 10, i * 10 + 10, b["pool_share"][i], b["sample_share"][i]))
    L += ["", "## 5. 독립성", "", "- %s" % r["independence"], "",
          "## 6. 라벨 규칙", "",
          "M33 과 **글자 그대로 같습니다**. 두 세트를 합쳐 보려면 정의가 같아야",
          "합니다 — 규칙을 손보면 세트 간 차이가 모델 성능인지 기준 변경인지",
          "가릴 수 없게 됩니다.", "",
          "| 라벨 | 정의 |", "|---|---|",
          "| `normal` | 비교군 대비 특별히 드문 조합이 아니다. 축 하나가 치우쳐도 사업 유형으로 설명되면 여기 |",
          "| `atypical_design` | 설계 축 둘 이상이 비교군 극단(P>=90 / P<=10), 또는 축 하나가 극단인데 사업 유형으로 설명되지 않는다 |",
          "| `data_error` | 교정 후에도 값이 상식범위 밖이거나 필드 의미가 모순된다 |",
          "| `uncertain` | 수치 축 2개 미만이거나 비교군이 전부 비교불가 |", "",
          "## 7. 한계 — 미리 적어 둡니다", "",
          "- 운영 경고선(pool P98) 이상이 표본에 **%d건**뿐입니다. 이 세트로도"
          % b["sample_above_operating_cut"],
          "  운영 임계선을 다시 잡기는 어렵습니다(M39 남은것 2). 점수 기반으로 뽑으면",
          "  해결되지만 그건 이 세트의 존재 이유를 지우는 선택입니다.",
          "- 라벨러 1인. 라벨러 간 일치도를 낼 수 없습니다.",
          "- 이 세트는 threshold 튜닝·feature 선택에 쓰지 않습니다."]
    if r.get("under_covered"):
        L.append("- 70건에 다 담기지 않아 pool 대비 절반 미만으로 얇아진 계층이 "
                 "있습니다: %s. 해당 계층의 결과는 개별로 읽지 않습니다."
                 % ", ".join("`%s`" % u for u in r["under_covered"]))
    L.append("")
    p = C.report_path("m41_m3_labelset2.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", action="store_true",
                    help="LABEL_A 적용 + 2인 일치도 + 라벨링 실행 기록")
    a = ap.parse_args()
    apply_labels() if a.label else build_sheet()
