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

두 단계로 나눈 이유는 M33 과 같다
    --sheet     점수를 뺀 라벨링 시트를 만든다 (라벨러가 볼 것)
    --finalize  채워진 라벨을 읽어 검증셋을 확정한다 (모델이 볼 것)

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
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m12_m3_cohort import compare
from m13_m4_anomaly import AXIS_LABEL, MIN_AXES, REF, SRC, prepare
from m33_m3_relabel import LABELS, PCT_EXTREME, won

CLEAN1 = os.path.join(C.DATA, "labels", "m3_clean_holdout.csv")
SHEET = os.path.join(C.REPORTS, "m41_labelset2_sheet.csv")
FILLED = os.path.join(C.DATA, "labels", "m41_labelset2_filled.csv")
OUT = os.path.join(C.DATA, "labels", "m3_holdout2.csv")
PLAN = os.path.join(C.REPORTS, "m41_labelset2_plan.csv")

N_TARGET = 70
SEED = 20260827          # M33 시트 셔플 시드(20260826)와 다르게 둔다
STYPE_TOP = ["사업화", "연구개발", "융자", "판로", "컨설팅", "설비", "고용보조"]
MIN_CELL = 20            # 2단 셀이 이보다 얇으면 '잔여조합' 으로 합친다
POWER2 = 0.5             # 2단 배분 지수. 1=비례, 0=균등, 0.5=제곱근(절충)
REST = "잔여조합"


# ------------------------------------------------------------------ 층화 기준
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
    plan.to_csv(PLAN, index=False, encoding="utf-8-sig")
    print("\n[sheet] %s  %d행" % (SHEET, len(sheet)))
    print("[plan ] %s" % PLAN)
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
        "marginals": marg,
        "under_covered": under,
        "m38_balance_check": bal,
        "independence": ("기존 50건의 row_id 와 program_stem 을 pool 에서 뺐다. "
                         "새 표본 안에서도 program_stem 중복을 허용하지 않았다."),
        "label_rule": "M33 과 동일 (normal / atypical_design / data_error / uncertain)",
        "sheet": SHEET, "plan": PLAN,
    }
    C.save_report("m41_m3_labelset2.json", rep)
    write_md(rep, plan)


# ------------------------------------------------------------------ 확정
def finalize():
    if not os.path.exists(FILLED):
        sys.exit("채워진 라벨 파일이 없습니다: %s" % FILLED)
    f = pd.read_csv(FILLED, encoding="utf-8-sig")
    bad = set(f["라벨"].dropna()) - set(LABELS)
    if bad:
        sys.exit("허용되지 않은 라벨: %s" % bad)
    if f["라벨"].isna().any():
        sys.exit("비어 있는 라벨 %d건" % int(f["라벨"].isna().sum()))
    f.to_csv(OUT, index=False, encoding="utf-8-sig")

    main = f[f["라벨"].isin(["normal", "atypical_design"])]
    print("M41 — 두 번째 검증셋 확정")
    print("  [labels] %s" % OUT)
    print("\n== 라벨 분포")
    for k, v in f["라벨"].value_counts().items():
        print("  %-16s %d" % (k, v))
    print("\n== 주 평가셋 %d건 / 양성 %d건"
          % (len(main), int((main["라벨"] == "atypical_design").sum())))
    print("\n== 층별 라벨 (데이터품질 x 라벨)")
    print(pd.crosstab(f["층_품질"], f["라벨"]).to_string())
    print("\n== 층별 라벨 (수치축 x 라벨)")
    print(pd.crosstab(f["층_축수"], f["라벨"]).to_string())

    old = pd.read_csv(CLEAN1, encoding="utf-8-sig")
    om = old[old["라벨"].isin(["normal", "atypical_design"])]
    print("\n== 합산 (M33 + M41)")
    print("  주 평가 %d건 / 양성 %d건"
          % (len(om) + len(main),
             int((om["라벨"] == "atypical_design").sum())
             + int((main["라벨"] == "atypical_design").sum())))

    C.save_report("m41_m3_labelset2_final.json", {
        "n": int(len(f)),
        "label_dist": {k: int(v) for k, v in f["라벨"].value_counts().items()},
        "main_eval_n": int(len(main)),
        "main_eval_positive": int((main["라벨"] == "atypical_design").sum()),
        "by_dq": {str(a): {str(b): int(c) for b, c in r.items()}
                  for a, r in pd.crosstab(f["층_품질"], f["라벨"]).iterrows()},
        "by_axes": {str(a): {str(b): int(c) for b, c in r.items()}
                    for a, r in pd.crosstab(f["층_축수"], f["라벨"]).iterrows()},
        "combined_with_m33": {
            "main_eval_n": int(len(om) + len(main)),
            "main_eval_positive": int((om["라벨"] == "atypical_design").sum())
            + int((main["라벨"] == "atypical_design").sum())},
        "holdout": OUT,
    })


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
    p = os.path.join(C.REPORTS, "m41_m3_labelset2.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", action="store_true", help="층화 추출 + 라벨링 시트 생성")
    ap.add_argument("--finalize", action="store_true", help="채워진 라벨로 검증셋 확정")
    a = ap.parse_args()
    finalize() if a.finalize else build_sheet()
