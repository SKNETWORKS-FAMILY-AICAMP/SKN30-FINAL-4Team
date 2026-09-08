r"""M43 — 라벨 기준 v2: `atypical_design` 을 축 개수에 중립적으로 다시 정의한다.

무엇이 문제였나
    v1(M33/M41)의 정의는 "비교군 극단(P>=90 또는 P<=10) 축이 **둘 이상**"이다.
    **세는** 규칙이라 축이 많을수록 걸리기 쉽다. 축 2개짜리는 둘 다 극단이어야
    하고 4개짜리는 아무 둘이면 된다.

    M42 가 그 결과를 쟀다.
        양성률   축2 3.3% / 축3 29.4% / 축4 50.0%
        n_axes 단독 ROC-AUC 0.8005 > 모델 0.7399

    `n_axes` 는 '설계가 얼마나 드문가'가 아니라 '원문에 항목을 몇 개 적었나'다
    (M40 §3 이 지목한 혼입). 그 둘이 라벨 안에서 섞이면 모델이 무엇을 맞히는지
    알 수 없다. 그래서 **모델이 아니라 라벨 정의를 고친다.**

v2 정의 — 축 개수에 중립적인 이상도
    1. 축별 이탈량   dev_i = |pct_i - 50| / 50        0=비교군 중앙값, 1=P0/P100
    2. 이상도        E = mean(dev_i)                  비교 가능한 축들의 평균
       세지 않고 평균을 낸다. 축이 늘어도 기대값이 변하지 않는다.
    3. 축 개수 보정  E 를 **같은 비교가능축 개수(n_comp)를 가진 pool 행들 안에서의
       백분위**로 바꾼다. 평균만 쓰면 축이 많을수록 분산이 줄어 고정 임계
       통과율이 오히려 낮아진다 — 반대 방향 편향이다. 같은 n_comp 안에서
       순위를 매기면 그것까지 빠진다.
    4. 후보          그 백분위가 **상위 20%** 면 `atypical_design` 후보

    상위 20% 는 이 프로젝트가 계속 써 온 경고 예산 비율이다(M30 20/50,
    M34 7/35, M42 20%). **라벨 결과를 보고 고른 값이 아니다.**

    사람 판단은 그대로 남는다 — v1 과 같은 구조다
        후보  -> 사업 유형으로 설명되면 `normal` 로 내린다   (DOWN)
        후보  -> 설명되지 않으면 그대로 둔다                 (KEEP)

        **하향만 쓴다.** 상향(비후보 -> atypical)은 규칙상 열려 있으나 쓰지
        않았다. 허용하면 경계 아래 행마다 사후 논리를 만들어 올릴 수 있어
        규칙이 무력해진다. v1(M33/M41)도 하향 조항만 있었다.

        판단과 근거는 아래 DOWN/KEEP 에 코드로 들어 있다. 중간 CSV 를
        주고받지 않으므로 **이 스크립트가 곧 라벨링 기록이다.**

바꾸지 않은 것
    `data_error` / `uncertain` 정의는 v1 그대로다. 축 개수 편향과 무관하고,
    고정해 두면 clean set 53건이 그대로 유지돼 **바뀐 것이 라벨 규칙뿐**이라는
    비교가 성립한다. 모델도 건드리지 않는다(M42 와 같은 후보·같은 프로토콜).

숨기지 않고 적는 한계
    이 라벨러는 v1 라벨과 M42 결과를 이미 읽었다. **blind 가 아니다.**
    그래서 기계적 후보 판정을 먼저 코드로 확정하고(사람 개입 0),
    사람이 움직인 행만 따로 세어 보고한다.
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

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m12_m3_cohort import compare
from m13_m3_anomaly import AXIS_LABEL, MIN_AXES, REF, SRC, prepare
from m41_m3_labelset2 import OUT as HOLDOUT2_V1

AXES = list(AXIS_LABEL)
TOP_FRAC = 0.20          # 후보 상위 비율. 프로젝트 공통 경고 예산과 같은 값
OUT = os.path.join(C.DATA, "labels", "m3_holdout2_v2.csv")
POOL_CACHE = os.path.join(C.PROC, "m43_extremity_pool.parquet")

# 기계 후보에 대한 사람 판단. 후보 목록과 어긋나면 build_labels 가 멈춘다.
DOWN = {   # 후보였으나 사업 유형으로 설명돼 normal 로 내린 행
    "EXCEL2022_0105": "후속연계 개발은 1년 단기가 설계 전제이고(기간 P10), 자부담 20%(비율 P90)는 중소기업 R&D 통상 출연비율이다. 두 축 모두 사업 유형으로 설명된다",
    "PBLN_000000000104669": "비교 가능 축이 지원비율 1개뿐이라 설계 '조합'을 말할 수 없다. 지자체 시설개선 80% 보조는 통상 보조율이다",
    "PBLN_000000000103925": "비교 가능 축이 지원비율 1개뿐이라 설계 '조합'을 말할 수 없다. 사회적기업가 육성 90% 지원은 통상 구조다",
    "PBLN_000000000118209": "기업수 P100(775개사)은 전국 단위 연구인력 인건비 지원의 실제 규모이고, 나머지 축(한도 P72)은 중앙 근처다",
}
KEEP = {   # 후보를 그대로 atypical_design 으로 둔 행
    "PBLN_000000000106847": "과제당 4.6억 P100 + 지원비율 P90. 사업화 project 비교군 최상단 조합이고 사업 유형으로 설명되지 않는다",
    "EXCEL2022_0570": "기업수 P10 + 비율 P90 + 한도 P18. 소수·소액·고지원율이 한 방향으로 겹친 드문 조합",
    "PBLN_000000000048393": "과제당 45억 P90 + 비율 P90. 인재양성 R&D 로도 단가와 지원율이 함께 최상단이다",
    "EXCEL2023_0653": "기업수 P0 + 한도 P90 + 비율 P90. 세 축 동시 극단",
    "EXCEL2023_0873": "115개사 P100 규모에서 기업당 3억 P75 를 동시에 준다. 규모와 단가가 함께 상단인 드문 조합 — '원래 큰 사업'은 하향 근거가 되지 못한다",
}


def axis_devs(row, ref):
    """비교 가능한 축의 이탈량 dev = |pct-50|/50 을 돌려준다."""
    out = {}
    for axis in AXES:
        c = compare(ref, axis, row.get(axis), row["support_type"],
                    row["support_method"], row.get("support_unit"), row["cohort"])
        if c["status"] == "비교불가":
            continue
        out[axis] = (abs(float(c["percentile_rank"]) - 50.0) / 50.0,
                     float(c["percentile_rank"]), int(c["n"]), c["level"])
    return out


def build_pool(train, ref, use_cache=True):
    """pool 전체의 이상도 E 와 비교가능축 개수 n_comp. 축 개수 보정의 기준이다."""
    if use_cache and os.path.exists(POOL_CACHE):
        return pd.read_parquet(POOL_CACHE)
    rows = []
    for _, r in train.iterrows():
        d = axis_devs(r, ref)
        devs = [v[0] for v in d.values()]
        rows.append({"row_id": r["row_id"], "n_comp": len(devs),
                     "extremity": float(np.mean(devs)) if devs else np.nan,
                     "max_dev": float(np.max(devs)) if devs else np.nan})
    out = pd.DataFrame(rows)
    out.to_parquet(POOL_CACHE, index=False)
    return out


def within_group_pct(pool, n_comp, value):
    """같은 n_comp 를 가진 pool 행들 안에서 value 의 백분위(0~100)."""
    g = pool[(pool["n_comp"] == n_comp) & pool["extremity"].notna()]["extremity"]
    if len(g) == 0 or pd.isna(value):
        return np.nan
    return float((g <= value).mean()) * 100


def build_labels():
    """기계 후보 판정 -> 사람 판단 적용 -> v2 라벨 확정. 한 번에 돈다.

    예전에는 시트 CSV 를 내보내고 사람이 채워 다시 읽는 2단계였다. 사람 판단이
    DOWN/KEEP 에 근거와 함께 코드로 들어와 있으므로 중간 파일이 필요 없다.
    스크립트가 곧 라벨링 기록이다.
    """
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    ref = pd.read_parquet(REF)
    hold = pd.read_csv(HOLDOUT2_V1, encoding="utf-8-sig")

    print("M43 — 라벨 기준 v2 (축 개수 중립)")
    print("  pool %d행에서 이상도 기준 분포를 만든다..." % len(train))
    pool = build_pool(train, ref)
    print("  n_comp 별 pool 분포: %s"
          % {int(k): int(v) for k, v in pool["n_comp"].value_counts().sort_index().items()})
    print("  n_comp 별 상위 %.0f%% 임계 E: %s"
          % (TOP_FRAC * 100,
             {int(k): round(float(np.quantile(g["extremity"], 1 - TOP_FRAC)), 3)
              for k, g in pool[pool["extremity"].notna()].groupby("n_comp")}))

    feat = train.drop_duplicates("row_id").set_index("row_id")
    rows = []
    for _, h in hold.iterrows():
        r = feat.loc[h["row_id"]]
        d = axis_devs(r, ref)
        devs = [v[0] for v in d.values()]
        E = float(np.mean(devs)) if devs else np.nan
        rows.append({
            "row_id": h["row_id"], "사업명": h["사업명"],
            "지원성격": h["지원성격"], "지원방식": h["지원방식"], "지원단위": h["지원단위"],
            "수치축_개수": int(r["n_axes"]), "비교가능축": len(devs),
            "이상도_E": round(E, 4) if devs else None,
            "최대이탈": round(float(np.max(devs)), 4) if devs else None,
            "축보정_백분위": round(within_group_pct(pool, len(devs), E), 1) if devs else None,
            "축별_이탈": " / ".join("%s P%.0f(dev %.2f)" % (AXIS_LABEL[a], v[1], v[0])
                                 for a, v in sorted(d.items(), key=lambda kv: -kv[1][0])),
            "v1_라벨": h["라벨"], "층_품질": h["층_품질"], "층_축수": h["층_축수"],
            "원문발췌": h["원문발췌"],
        })
    s = pd.DataFrame(rows)

    clean = s["v1_라벨"].isin(["normal", "atypical_design"])
    cand = clean & (s["축보정_백분위"] >= (1 - TOP_FRAC) * 100)
    s["기계후보_v2"] = np.where(clean, np.where(cand, "candidate", "-"), "n/a")
    if set(s.loc[cand, "row_id"]) != set(DOWN) | set(KEEP):
        sys.exit("기계 후보 목록이 기록된 사람 판단과 어긋납니다:\n  후보 %s\n  기록 %s"
                 % (sorted(set(s.loc[cand, "row_id"])), sorted(set(DOWN) | set(KEEP))))

    s["v2_라벨"] = s["v1_라벨"]                      # data_error / uncertain 은 그대로
    s.loc[clean, "v2_라벨"] = "normal"
    s.loc[s["row_id"].isin(KEEP), "v2_라벨"] = "atypical_design"
    s["v2_근거"] = np.where(clean, "기계 비후보 (축보정 백분위 상위 %.0f%% 밖)" % (TOP_FRAC * 100),
                          "v1 유지 (축 개수 편향과 무관)")
    for rid, why in {**DOWN, **KEEP}.items():
        s.loc[s["row_id"] == rid, "v2_근거"] = why
    s.to_csv(OUT, index=False, encoding="utf-8-sig")

    cl = s[clean].copy()
    print("\n  clean 대상 %d건 / 기계 후보 %d건 / 사람 하향 %d건 / 사람 상향 0건"
          % (len(cl), int(cand.sum()), len(DOWN)))
    print("  [labels] %s" % OUT)
    print("\n== v2 라벨 분포")
    for k, v in s["v2_라벨"].value_counts().items():
        print("  %-16s %d" % (k, v))

    ct = pd.crosstab(s["v1_라벨"], s["v2_라벨"])
    print("\n== v1 x v2")
    print(ct.to_string())

    print("\n== 사람이 기계 후보에서 움직인 행 (하향 %d / 상향 0)" % len(DOWN))
    for rid, why in DOWN.items():
        nm = str(s.loc[s["row_id"] == rid, "사업명"].iloc[0])[:44]
        print("    %-46s %s" % (nm, why[:62]))

    print("\n== 축 개수별 양성률 — v1 vs v2 (핵심)")
    bias = {}
    for k, g in cl.groupby("수치축_개수"):
        r1 = float((g["v1_라벨"] == "atypical_design").mean())
        r2 = float((g["v2_라벨"] == "atypical_design").mean())
        bias[int(k)] = {"n": int(len(g)),
                        "v1_positive": int((g["v1_라벨"] == "atypical_design").sum()),
                        "v2_positive": int((g["v2_라벨"] == "atypical_design").sum()),
                        "v1_rate": round(r1, 4), "v2_rate": round(r2, 4)}
        print("  축%d  n=%-3d  v1 %d (%.1f%%)  ->  v2 %d (%.1f%%)"
              % (k, len(g), bias[int(k)]["v1_positive"], r1 * 100,
                 bias[int(k)]["v2_positive"], r2 * 100))

    from sklearn.metrics import roc_auc_score
    ax = cl["수치축_개수"].to_numpy(float)
    a1 = float(roc_auc_score((cl["v1_라벨"] == "atypical_design").astype(int), ax))
    a2 = float(roc_auc_score((cl["v2_라벨"] == "atypical_design").astype(int), ax))
    sp1 = max(v["v1_rate"] for v in bias.values()) - min(v["v1_rate"] for v in bias.values())
    sp2 = max(v["v2_rate"] for v in bias.values()) - min(v["v2_rate"] for v in bias.values())
    print("\n  n_axes 로 라벨을 맞히는 정도 (낮을수록 중립)")
    print("    v1 ROC-AUC %.4f  양성률 폭 %.3f" % (a1, sp1))
    print("    v2 ROC-AUC %.4f  양성률 폭 %.3f" % (a2, sp2))

    rep = {
        "정의": {
            "dev": "축별 이탈량 |pct-50|/50",
            "extremity": "비교 가능한 축들의 dev 평균 (세지 않고 평균)",
            "correction": "같은 비교가능축 개수(n_comp) pool 안에서의 백분위",
            "cut": "상위 %.0f%% (프로젝트 공통 경고 예산 비율)" % (TOP_FRAC * 100),
            "human": "후보->normal 하향만 사용. 상향 조항은 열어 뒀으나 쓰지 않았다",
            "unchanged": "data_error / uncertain 은 v1 그대로. 모델도 그대로",
        },
        "n": int(len(s)), "n_clean": int(len(cl)),
        "n_machine_candidate": int(cand.sum()),
        "label_dist_v2": {k: int(v) for k, v in s["v2_라벨"].value_counts().items()},
        "v1_vs_v2": {str(a): {str(b): int(c) for b, c in row.items()}
                     for a, row in ct.iterrows()},
        "human_moves": {
            "candidate_to_normal": [
                {"row_id": rid, "사업명": str(s.loc[s["row_id"] == rid, "사업명"].iloc[0]),
                 "근거": why} for rid, why in DOWN.items()],
            "noncandidate_to_atypical": [],
        },
        "machine_kept": [{"row_id": rid, "사업명": str(s.loc[s["row_id"] == rid, "사업명"].iloc[0]),
                          "근거": why} for rid, why in KEEP.items()],
        "naxes_bias": {
            "by_n_axes": bias,
            "label_roc_auc_from_n_axes": {"v1": round(a1, 4), "v2": round(a2, 4)},
            "positive_rate_spread": {"v1": round(sp1, 4), "v2": round(sp2, 4)},
        },
    }
    C.save_report("m43_m3_label_rule_v2.json", rep)
    write_md(rep)


def write_md(r):
    b = r["naxes_bias"]
    L = ["# M43 — 라벨 기준 v2: `atypical_design` 을 축 개수에 중립적으로", "",
         "> M42 에서 `n_axes` 단독 ROC-AUC 0.8005 가 모델(0.7399)을 앞질렀습니다.",
         "> 원인은 모델이 아니라 라벨 정의였습니다. **모델은 건드리지 않고 정의만**",
         "> **고칩니다.**", "",
         "## 1. 무엇이 문제였나", "",
         "v1 의 `atypical_design` 은 \"비교군 극단(P>=90 또는 P<=10) 축이 **둘 이상**\"",
         "입니다. **세는** 규칙이라 축이 2개면 둘 다 극단이어야 하고 4개면 아무",
         "둘이면 됩니다. 축 개수는 '설계가 드문 정도'가 아니라 '원문에 항목을",
         "몇 개 적었나'인데(M40 §3), 그것이 라벨 안으로 들어와 있었습니다.", "",
         "## 2. v2 정의", "", "```text",
         "1. 축별 이탈량   dev_i = |pct_i - 50| / 50      0=비교군 중앙값, 1=P0/P100",
         "2. 이상도        E = mean(dev_i)                비교 가능한 축들의 평균",
         "3. 축 개수 보정  E -> 같은 n_comp pool 안에서의 백분위",
         "4. 후보          그 백분위 %s" % r["정의"]["cut"],
         "```", "",
         "왜 두 단계인가", "",
         "| 단계 | 없으면 생기는 편향 |", "|---|---|",
         "| 세지 않고 평균 | 축이 많을수록 걸리기 쉽다 (v1 의 편향) |",
         "| 같은 n_comp 안 백분위 | 평균만 쓰면 축이 많을수록 분산이 줄어 고정 임계 통과율이 **낮아진다** — 반대 방향 편향 |", "",
         "상위 20% 는 이 프로젝트가 계속 써 온 경고 예산 비율입니다",
         "(M30 20/50, M34 7/35, M42 20%). **라벨 결과를 보고 고른 값이 아닙니다.**", "",
         "### 사람 판단은 그대로 남습니다", "",
         "v1 과 같은 구조입니다 — 기계가 후보를 내고 사람이 사업 유형으로",
         "설명되는지 봅니다. 양방향 모두 허용하고 움직인 행을 셉니다.", "",
         "### 바꾸지 않은 것", "",
         "`data_error`(14) / `uncertain`(3) 은 v1 그대로 두었습니다. 축 개수 편향과",
         "무관하고, 고정하면 clean set 53건이 유지돼 **바뀐 것이 라벨 규칙뿐**이라는",
         "비교가 성립합니다. 모델도 M42 와 같은 후보·같은 프로토콜입니다.", "",
         "## 3. v1 x v2", "",
         "| v1 \\ v2 | " + " | ".join("`%s`" % c for c in
                                     sorted({k for v in r["v1_vs_v2"].values() for k in v})) + " |"]
    cols = sorted({k for v in r["v1_vs_v2"].values() for k in v})
    L.append("|---|" + "---:|" * len(cols))
    for a, row in r["v1_vs_v2"].items():
        L.append("| `%s` | %s |" % (a, " | ".join(str(row.get(c, 0)) for c in cols)))
    hm = r["human_moves"]
    L += ["", "## 4. 사람이 기계 후보에서 움직인 행", "",
          "| 방향 | 건수 |", "|---|---:|",
          "| 후보 -> `normal` | %d |" % len(hm["candidate_to_normal"]),
          "| 비후보 -> `atypical_design` | %d |" % len(hm["noncandidate_to_atypical"]), ""]
    for key, title in (("candidate_to_normal", "후보였으나 사업 유형으로 설명돼 `normal`"),
                       ("noncandidate_to_atypical", "비후보였으나 `atypical_design` 으로 올림")):
        if hm[key]:
            L += ["**%s**" % title, "", "| 사업 | 근거 |", "|---|---|"]
            for x in hm[key]:
                L.append("| %s | %s |" % (str(x["사업명"])[:44], x["근거"]))
            L.append("")
    L += ["## 5. 축 개수 편향이 줄었는가 (핵심)", "",
          "| 축 개수 | n | v1 양성 | v1 양성률 | v2 양성 | v2 양성률 |",
          "|---|---:|---:|---:|---:|---:|"]
    for k, v in sorted(b["by_n_axes"].items(), key=lambda kv: int(kv[0])):
        L.append("| 축%s | %d | %d | %.1f%% | %d | %.1f%% |"
                 % (k, v["n"], v["v1_positive"], v["v1_rate"] * 100,
                    v["v2_positive"], v["v2_rate"] * 100))
    ra = b["label_roc_auc_from_n_axes"]
    sp = b["positive_rate_spread"]
    L += ["", "`n_axes` 만으로 라벨을 맞히는 정도 (낮을수록 중립)", "",
          "| | v1 | v2 |", "|---|---:|---:|",
          "| 라벨 ROC-AUC (n_axes 단독) | %.4f | **%.4f** |" % (ra["v1"], ra["v2"]),
          "| 축별 양성률 폭 | %.3f | **%.3f** |" % (sp["v1"], sp["v2"]), "",
          "> 이 표는 **라벨 자체가** 축 개수와 얼마나 엮여 있는지입니다. 모델 성능이",
          "> 아닙니다. 모델 쪽 비교는 M44 에서 같은 프로토콜로 잽니다.", ""]
    p = C.report_path("m43_m3_label_rule_v2.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    build_labels()
