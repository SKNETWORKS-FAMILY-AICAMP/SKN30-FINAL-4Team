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
        후보  -> 사업 유형으로 설명되면 `normal` 로 내린다
        비후보 -> 설명되지 않는 실질적 이유가 있으면 `atypical_design` 으로 올린다
        양방향 모두 근거를 남기고 몇 건이 움직였는지 보고한다

바꾸지 않은 것
    `data_error` / `uncertain` 정의는 v1 그대로다. 축 개수 편향과 무관하고,
    고정해 두면 clean set 53건이 그대로 유지돼 **바뀐 것이 라벨 규칙뿐**이라는
    비교가 성립한다. 모델도 건드리지 않는다(M42 와 같은 후보·같은 프로토콜).

숨기지 않고 적는 한계
    이 라벨러는 v1 라벨과 M42 결과를 이미 읽었다. **blind 가 아니다.**
    그래서 기계적 후보 판정을 먼저 코드로 확정하고(사람 개입 0),
    사람이 움직인 행만 따로 세어 보고한다.
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m12_m3_cohort import compare
from m13_m4_anomaly import AXIS_LABEL, MIN_AXES, REF, SRC, prepare
from m41_m3_labelset2 import OUT as HOLDOUT2

AXES = list(AXIS_LABEL)
TOP_FRAC = 0.20          # 후보 상위 비율. 프로젝트 공통 경고 예산과 같은 값
SHEET = os.path.join(C.REPORTS, "m43_rule_v2_sheet.csv")
OUT = os.path.join(C.DATA, "labels", "m3_holdout2_v2.csv")
POOL_CACHE = os.path.join(C.PROC, "m43_extremity_pool.parquet")


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


def build_sheet():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    ref = pd.read_parquet(REF)
    hold = pd.read_csv(HOLDOUT2, encoding="utf-8-sig")

    print("M43 — 라벨 기준 v2 (축 개수 중립)")
    print("  pool %d행에서 이상도 기준 분포를 만든다..." % len(train))
    pool = build_pool(train, ref)
    print("  n_comp 별 pool 분포: %s"
          % {int(k): int(v) for k, v in pool["n_comp"].value_counts().sort_index().items()})

    feat = train.drop_duplicates("row_id").set_index("row_id")
    rows = []
    for _, h in hold.iterrows():
        rid = h["row_id"]
        r = feat.loc[rid]
        d = axis_devs(r, ref)
        devs = [v[0] for v in d.values()]
        E = float(np.mean(devs)) if devs else np.nan
        mx = float(np.max(devs)) if devs else np.nan
        p = within_group_pct(pool, len(devs), E)
        rows.append({
            "row_id": rid, "사업명": h["사업명"],
            "지원성격": h["지원성격"], "지원방식": h["지원방식"], "지원단위": h["지원단위"],
            "수치축_개수": int(r["n_axes"]), "비교가능축": len(devs),
            "이상도_E": round(E, 4) if devs else None,
            "최대이탈": round(mx, 4) if devs else None,
            "축보정_백분위": round(p, 1) if devs else None,
            "축별_이탈": " / ".join(
                "%s P%.0f(dev %.2f)" % (AXIS_LABEL[a], v[1], v[0])
                for a, v in sorted(d.items(), key=lambda kv: -kv[1][0])),
            "v1_라벨": h["라벨"],
            "기계후보_v2": "", "v2_라벨": "", "v2_근거": "",
            "층_품질": h["층_품질"], "층_축수": h["층_축수"],
            "원문발췌": h["원문발췌"],
        })
    s = pd.DataFrame(rows)

    # 기계 후보 — 사람 개입 0. clean 대상(normal/atypical_design)만 판정한다.
    clean = s["v1_라벨"].isin(["normal", "atypical_design"])
    cand = clean & (s["축보정_백분위"] >= (1 - TOP_FRAC) * 100)
    s["기계후보_v2"] = np.where(clean, np.where(cand, "candidate", "-"), "n/a")
    s.to_csv(SHEET, index=False, encoding="utf-8-sig")

    print("\n  clean 대상 %d건 / 기계 후보 %d건 (상위 %.0f%%)"
          % (int(clean.sum()), int(cand.sum()), TOP_FRAC * 100))
    print("\n== 축 개수별 기계 후보율 — 중립성 1차 확인")
    for k, g in s[clean].groupby("수치축_개수"):
        c = int((g["기계후보_v2"] == "candidate").sum())
        print("  축%d  n=%-3d 후보 %-2d (%.1f%%)" % (k, len(g), c, c / len(g) * 100))
    print("\n[sheet] %s" % SHEET)
    print("  사람 판단 단계: 후보를 사업 유형으로 설명할 수 있으면 normal 로 내리고,")
    print("  비후보라도 설명되지 않는 이유가 있으면 atypical_design 으로 올린다.")


def finalize(filled):
    f = pd.read_csv(filled, encoding="utf-8-sig")
    bad = set(f["v2_라벨"].dropna()) - {"normal", "atypical_design", "data_error", "uncertain"}
    if bad:
        sys.exit("허용되지 않은 라벨: %s" % bad)
    if f["v2_라벨"].isna().any():
        sys.exit("비어 있는 v2_라벨 %d건" % int(f["v2_라벨"].isna().sum()))

    # data_error / uncertain 은 v1 그대로여야 한다 — 바꾼 것이 규칙뿐임을 보장
    fixed = f["v1_라벨"].isin(["data_error", "uncertain"])
    if not (f.loc[fixed, "v2_라벨"] == f.loc[fixed, "v1_라벨"]).all():
        sys.exit("data_error/uncertain 은 v1 을 그대로 유지해야 합니다")

    f.to_csv(OUT, index=False, encoding="utf-8-sig")
    clean = f[f["v1_라벨"].isin(["normal", "atypical_design"])].copy()

    print("M43 — v2 라벨 확정")
    print("  [labels] %s" % OUT)
    print("\n== v2 라벨 분포")
    for k, v in f["v2_라벨"].value_counts().items():
        print("  %-16s %d" % (k, v))

    ct = pd.crosstab(f["v1_라벨"], f["v2_라벨"])
    print("\n== v1 x v2")
    print(ct.to_string())

    moved_dn = clean[(clean["기계후보_v2"] == "candidate")
                     & (clean["v2_라벨"] == "normal")]
    moved_up = clean[(clean["기계후보_v2"] == "-")
                     & (clean["v2_라벨"] == "atypical_design")]
    print("\n== 사람이 기계 후보에서 움직인 행")
    print("  후보 -> normal        %d건" % len(moved_dn))
    for _, r in moved_dn.iterrows():
        print("    %-44s %s" % (str(r["사업명"])[:42], str(r["v2_근거"])[:64]))
    print("  비후보 -> atypical    %d건" % len(moved_up))
    for _, r in moved_up.iterrows():
        print("    %-44s %s" % (str(r["사업명"])[:42], str(r["v2_근거"])[:64]))

    print("\n== 축 개수별 양성률 — v1 vs v2 (핵심)")
    bias = {}
    for k, g in clean.groupby("수치축_개수"):
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
    ax = clean["수치축_개수"].to_numpy(float)
    a1 = float(roc_auc_score((clean["v1_라벨"] == "atypical_design").astype(int), ax))
    a2 = float(roc_auc_score((clean["v2_라벨"] == "atypical_design").astype(int), ax))
    spread1 = max(v["v1_rate"] for v in bias.values()) - min(v["v1_rate"] for v in bias.values())
    spread2 = max(v["v2_rate"] for v in bias.values()) - min(v["v2_rate"] for v in bias.values())
    print("\n  n_axes 로 라벨을 맞히는 정도 (낮을수록 중립)")
    print("    v1 ROC-AUC %.4f  양성률 폭 %.3f" % (a1, spread1))
    print("    v2 ROC-AUC %.4f  양성률 폭 %.3f" % (a2, spread2))

    rep = {
        "정의": {
            "dev": "축별 이탈량 |pct-50|/50",
            "extremity": "비교 가능한 축들의 dev 평균 (세지 않고 평균)",
            "correction": "같은 비교가능축 개수(n_comp) pool 안에서의 백분위",
            "cut": "상위 %.0f%% (프로젝트 공통 경고 예산 비율)" % (TOP_FRAC * 100),
            "human": "후보->normal 하향 / 비후보->atypical 상향 모두 허용, 근거 기록",
            "unchanged": "data_error / uncertain 은 v1 그대로. 모델도 그대로",
        },
        "n": int(len(f)), "n_clean": int(len(clean)),
        "label_dist_v2": {k: int(v) for k, v in f["v2_라벨"].value_counts().items()},
        "v1_vs_v2": {str(a): {str(b): int(c) for b, c in row.items()}
                     for a, row in ct.iterrows()},
        "human_moves": {
            "candidate_to_normal": [{"row_id": r["row_id"], "사업명": r["사업명"],
                                     "근거": r["v2_근거"]} for _, r in moved_dn.iterrows()],
            "noncandidate_to_atypical": [{"row_id": r["row_id"], "사업명": r["사업명"],
                                          "근거": r["v2_근거"]} for _, r in moved_up.iterrows()],
        },
        "naxes_bias": {
            "by_n_axes": bias,
            "label_roc_auc_from_n_axes": {"v1": round(a1, 4), "v2": round(a2, 4)},
            "positive_rate_spread": {"v1": round(spread1, 4), "v2": round(spread2, 4)},
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
    p = os.path.join(C.REPORTS, "m43_m3_label_rule_v2.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", action="store_true", help="기계 후보 + 검토 시트 생성")
    ap.add_argument("--finalize", metavar="FILLED", help="채워진 시트로 v2 라벨 확정")
    a = ap.parse_args()
    finalize(a.finalize) if a.finalize else build_sheet()
