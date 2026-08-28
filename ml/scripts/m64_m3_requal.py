r"""M64 — 모델 3(현행 구조)을 수정 데이터로 재평가.

지시서 Model 3 우선순위 1~5 를 한 스크립트에서 답한다. **구조는 건드리지
않는다** — 거리 기반 이례성 점수, 비교군 사다리(성격x방식), mean 대표벡터,
standard scaling, Euclidean, MIN_COHORT=20 전부 M44 Freeze 그대로다.
바뀌는 것은 입력 데이터셋 하나뿐이다.

    1 thin cohort 표본 확대 가능성   pool 이 얼마나 늘었고 얇은 비교군이
                                    몇 개 남는가 (M48/M50/M60 이 같은 벽을 지목)
    2 지원단위 결측 해소             M62 D2 이후 결측률
    3 파싱 오류 제거                 M62 D3 (지원비율 -320% 포함)
    4 cohort feature 결측/정규화     M62 D4/D5
    5 현행 Model 3 재평가            아래 지표들

평가 기준은 지시서가 정한 다섯을 그대로 쓴다. ROC 만 보지 않는다.

    resampling Spearman / Top-K overlap      m3_lab.resample_stability
    synthetic perturbation 방향 일관성        m3_lab.synthetic_stress
    attribution 안정성                        m3_lab.attribution_stability
    fallback 비율                             m3_lab.cohort_profile
    (보조) 탐색적 ROC                          m3_lab.eval_labeled

세 가지를 나눠 잰다. pool 이 1,948 -> 2,377 로 늘었기 때문이다(M62 D4 가
목록 표본의 사업기간을 복원해 수치축 2개 조건을 넘은 행이 생겼다). 크기가
다른 두 집합의 지표를 그냥 나란히 놓으면 '데이터가 좋아졌다'와 '대상이
달라졌다'가 섞인다.

    A v1 pool (1,948)            현행
    B 교집합 pool (v1∩v2)        같은 행에서 입력 품질만 바꾼 순수 대조
    C v2 pool (2,377)            수정 후 실제 운영 상태

**M58 A3(+지원단위) 재검토**도 여기서 한다. 성능결과서 6장이 "지원단위 결측
15% 해소되면 A3 이 다시 열린다"고 적어 둔 항목이라 새 구조 탐색이 아니다.
판정 문턱은 M58 이 결과를 보기 전에 고정한 값(Top30 겹침 0.918 / Spearman
0.969 · m3_lab.KEEP_*)과 m3_lab.verdict 의 필수조건 5종을 그대로 쓴다.

**Mahalanobis 유사 거리는 돌리지 않는다.** 지시서가 "향후 재검토 후보로만
남긴다"고 못박았다. 양성 5건짜리 라벨셋으로는 지금도 가릴 수 없다.
"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warnings

warnings.filterwarnings("ignore")
import common as C
import m3_lab as L
import f06_design_features as F6

V1 = F6.OUT
V2 = F6.OUT_V2
BASE_KW = {}                      # 현행 구조 = m3_lab 의 기본값


def evaluate(train, tag, **kw):
    """지시서 평가 기준 5종 + 탐색적 ROC. 하나라도 빼지 않는다."""
    t0 = time.time()
    res = L.score_pool(train, train, **kw)
    out = {
        "n_rows": int(len(train)),
        "cohort_profile": L.cohort_profile(res),
        "resample_stability": L.resample_stability(train, **kw),
        "rank_volatility": L.rank_volatility(train, **kw),
        "synthetic_stress": L.synthetic_stress(train, **kw),
        "attribution_stability": L.attribution_stability(train, **kw),
        "feature_dependency": L.feature_dependency(train, **kw),
    }
    try:
        _, ids, y = L.load_labels(train)
        out["exploratory_roc"] = (L.eval_labeled(res["score"], ids, y)
                                  if len(set(y)) > 1 else None)
        out["labeled_n"] = {"n": len(ids), "positives": int(y.sum())}
    except Exception as e:                      # 라벨셋이 없어도 나머지는 낸다
        out["exploratory_roc"] = None
        out["labeled_error"] = str(e)
    out["elapsed_sec"] = round(time.time() - t0, 1)
    print("   [%s] %ds  fallback %d  얇은비교군 %d  Spearman(0.8) %.3f  Top30 %.3f  "
          "단조성 %.2f  attribution %.3f  ROC %s"
          % (tag, out["elapsed_sec"], out["cohort_profile"]["n_global_fallback"],
             out["cohort_profile"]["n_thin"],
             out["resample_stability"]["frac_0.8"]["spearman_mean"],
             out["resample_stability"]["frac_0.8"]["top30_mean"],
             out["synthetic_stress"]["min_positive_rate"],
             out["attribution_stability"]["top1_axis_agreement_mean"],
             "%.4f" % out["exploratory_roc"]["roc_auc"] if out["exploratory_roc"] else "—"))
    return out


def thin_cohort_view(train, **kw):
    """얇은 비교군이 몇 개이고 몇 행을 담는가 (지시서 1번)."""
    res = L.score_pool(train, train, **kw)
    n, lvl = res["cohort_n"], res["level"]
    thin = (n <= L.THIN_MAX) & (lvl != "L0 전체")
    per = (pd.DataFrame({"key": res["cohort_key"], "level": lvl, "n": n})
           .groupby(["level", "key"]).agg(n=("n", "first")))
    thin_keys = per[per["n"] <= L.THIN_MAX]
    return {
        "n_rows_in_thin": int(thin.sum()),
        "n_thin_cohorts": int(len(thin_keys)),
        "thin_cohorts": [{"cohort": str(k[1]), "level": str(k[0]), "n": int(r["n"])}
                         for k, r in thin_keys.sort_values("n").iterrows()],
        "cohort_size_median": int(n.median()),
        "n_distinct_cohorts": int(res["cohort_key"].nunique()),
    }


def expansion_headroom(path, thin):
    """지시서 1번의 '가능성' 쪽 — 얇은 비교군에 더 넣을 행이 남아 있는가.

    pool 은 `n_axes >= 2` 로 걸러진 것이라, 같은 비교군에 속하지만 수치축이
    부족해 빠진 행이 데이터에 이미 있을 수 있다. 그 수를 세면 '표본 확대'가
    **새 수집**을 뜻하는지 **이미 가진 문서의 추출 개선**을 뜻하는지 갈린다.
    """
    from m13_m4_anomaly import MIN_AXES, prepare

    d = prepare(pd.read_parquet(path))
    out = []
    for c in thin["thin_cohorts"]:
        parts = c["cohort"].split("|")
        m = d["support_type"].astype(str) == parts[0]
        if len(parts) > 1:
            m &= d["support_method"].astype(str) == parts[1]
        total = int(m.sum())
        out.append({"cohort": c["cohort"], "level": c["level"], "in_pool": c["n"],
                    "all_rows": total,
                    "excluded_for_axes": int((m & (d["n_axes"] < MIN_AXES)).sum())})
    return out


MD = os.path.join(C.REPORTS, "m64_m3_requal.md")


def _row(tag, b):
    cp, rs = b["cohort_profile"], b["resample_stability"]["frac_0.8"]
    roc = b.get("exploratory_roc") or {}
    return ("| %s | %d | %d | %d | %.3f | %.3f | %.2f | %.3f | %s |"
            % (tag, b["n_rows"], cp["n_global_fallback"], cp["n_thin"],
               rs["spearman_mean"], rs["top30_mean"],
               b["synthetic_stress"]["min_positive_rate"],
               b["attribution_stability"]["top1_axis_agreement_mean"],
               ("%.4f" % roc["roc_auc"]) if roc else "—"))


def write_md(r):
    E = r["evaluation"]
    b1, b2 = E["B 교집합 v1"], E["B 교집합 v2"]
    v1, v2 = E["A v1 pool"], E["C v2 pool"]
    a3 = r["m58_a3_recheck"]
    L_ = []
    A = L_.append
    A("# M64 — 모델 3 을 수정 데이터로 재평가 (구조는 그대로)\n")
    A("> 지시서 Model 3: `thin cohort · 지원단위 결측 · 파싱 오류 · cohort feature")
    A("> 품질`을 먼저 고친 뒤 **현행 구조를 재평가**한다. 추가 구조 탐색은 하지 않는다.\n")
    A("바뀐 것은 입력 데이터셋 하나(M62)뿐입니다. 거리 기반 이례성 점수 · 비교군")
    A("사다리(성격x방식) · mean 대표벡터 · standard scaling · Euclidean ·")
    A("`MIN_COHORT=20` 전부 M44 Freeze 그대로입니다.\n")
    A("pool 이 %d → %d 로 늘었기 때문에(M62 D4 가 목록 표본의 사업기간을 복원해"
      % (v1["n_rows"], v2["n_rows"]))
    A("수치축 2개 조건을 넘긴 행이 생겼습니다) **세 갈래로 나눠 잽니다.** 크기가 다른")
    A("두 집합을 그냥 나란히 놓으면 '데이터가 좋아졌다'와 '대상이 달라졌다'가 섞입니다.\n")
    A("| 대상 | n | 전역 fallback | 얇은 비교군 행 | 재표집 ρ(0.8) | Top30(0.8) | 단조성 | attribution | 탐색적 ROC |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    A(_row("A v1 pool (현행)", v1))
    A(_row("**B 교집합 · v1**", b1))
    A(_row("**B 교집합 · v2**", b2))
    A(_row("C v2 pool (수정 후 운영상태)", v2))
    A("")
    A("## 0. 입력이 얼마나 달라졌는가 (M62)\n")
    q = r["data_quality"]["지원단위_결측률"]
    A("| | A v1 | B 교집합 v1 | B 교집합 v2 | C v2 |")
    A("|---|---:|---:|---:|---:|")
    A("| `지원단위` 결측률 | %.1f%% | %.1f%% | **%.1f%%** | %.1f%% |"
      % (100 * q["A v1"], 100 * q["B 교집합 v1"],
         100 * q["B 교집합 v2"], 100 * q["C v2"]))
    A("")
    A("결측률은 **같은 행에서만** 읽습니다. C 열이 더 높아 보이는 것은 pool 이 커져")
    A("분모가 달라졌기 때문이지 결측이 늘어서가 아닙니다.\n")
    A("| 수치축 결측률 | A v1 | C v2 |")
    A("|---|---:|---:|")
    for ax in L.NUM:
        A("| `%s` | %.1f%% | %.1f%% |"
          % (ax, 100 * r["data_quality"]["수치축_결측률"]["A v1"][ax],
             100 * r["data_quality"]["수치축_결측률"]["C v2"][ax]))
    A("")
    A("`project_duration` 결측이 77.7% → 40.2% 로 반토막 납니다(M62 D4). 대신")
    A("`log_per_recipient` 결측이 올라가는데, 이것도 분모 문제입니다 — 사업기간이")
    A("복원되면서 **금액이 없고 기간·기업수만 있는 행**이 새로 pool 에 들어옵니다.")
    A("빠진 것이 아니라 없던 행이 들어온 것입니다 — 새로 들어온 %d행은 전부"
      % (r["pool_rows"]["v2"] - r["pool_rows"]["intersection"]))
    A("사업기간을 갖고 있고 금액이 있는 것은 일부입니다. 반대로 v1 pool 에서 빠진")
    A("%d행은 D3 가 연도 오파싱 사업기간을 지우면서 수치축 2개 조건을 못 채운 행입니다.\n"
      % (r["pool_rows"]["v1"] - r["pool_rows"]["intersection"]))
    A("| 지원방식 | A v1 | C v2 |")
    A("|---|---:|---:|")
    dist = r["data_quality"]["지원방식_분포"]
    for k in sorted(set(dist["A v1"]) | set(dist["C v2"])):
        A("| %s | %d | %d |" % (k, dist["A v1"].get(k, 0), dist["C v2"].get(k, 0)))
    A("")
    A("`지원비율` 최소값 %s%% → %s%% — M61 이 scaling 진단에서 지목하고 \"M31 파서 감사"
      % (r["data_quality"]["지원비율_최소"]["A v1"],
         r["data_quality"]["지원비율_최소"]["C v2"]))
    A("대상\"으로 넘긴 1건이 M62 D3 에서 처리됐습니다.\n")
    A("## 1. 같은 행에서 무엇이 좋아지고 무엇이 나빠졌는가 (B)\n")
    A("데이터 수정의 순수 효과는 **B 두 줄**에만 있습니다. 같은 1,942행, 같은 구조,")
    A("입력 품질만 다릅니다.\n")
    A("| 지표 | v1 | v2 | |")
    A("|---|---:|---:|---|")
    for label, key, better in [
            ("얇은 비교군 행수", ("cohort_profile", "n_thin"), "low"),
            ("비교군 표본수 P10", ("cohort_profile", "cohort_size_p10"), "high"),
            ("성격만으로 후퇴한 행(L2)", None, "low"),
            ("행 단위 순위 변동(전체)", ("rank_volatility", "overall_mean"), "low"),
            ("행 단위 순위 변동(얇은 비교군)", ("rank_volatility", "thin_mean"), "low"),
            ("행 단위 순위 변동(두꺼운 비교군)", ("rank_volatility", "nonthin_mean"), "low"),
            ("행 단위 순위 변동(전역 fallback)", ("rank_volatility", "global_mean"), "low"),
            ("재표집 순위상관 ρ(0.8)", ("resample_stability", "frac_0.8", "spearman_mean"), "high"),
            ("재표집 Top30 겹침(0.8)", ("resample_stability", "frac_0.8", "top30_mean"), "high"),
            ("attribution top1 일치", ("attribution_stability", "top1_axis_agreement_mean"), "high"),
            ("탐색적 ROC-AUC", ("exploratory_roc", "roc_auc"), "high"),
            ("탐색적 PR-AUC", ("exploratory_roc", "pr_auc"), "high")]:
        if key is None:
            x = b1["cohort_profile"]["level_dist"].get("L2 support_type", 0)
            y = b2["cohort_profile"]["level_dist"].get("L2 support_type", 0)
        else:
            x, y = b1, b2
            for k in key:
                x, y = x[k], y[k]
        mark = "개선" if ((y > x) == (better == "high")) else ("동일" if x == y else "악화")
        fmt = "%.3f" if isinstance(x, float) else "%d"
        A("| %s | %s | %s | %s |" % (label, fmt % x, fmt % y, mark))
    A("")
    A("### 좋아진 것 — 비교군의 구성\n")
    A("`support_method` 가 제자리를 찾자(M62 D5) 비교군이 실제 사업 형태를 따라")
    A("갈라집니다. 얇은 비교군에 속한 행이 %d → %d 로 줄고, 비교군 표본수의 하위 10%%가"
      % (b1["cohort_profile"]["n_thin"], b2["cohort_profile"]["n_thin"]))
    A("%d → %d 로 굵어졌습니다. 전역 fallback 으로 떨어진 행들의 순위 변동도"
      % (b1["cohort_profile"]["cohort_size_p10"], b2["cohort_profile"]["cohort_size_p10"]))
    A("%.2f → %.2f 로 절반이 됐습니다."
      % (b1["rank_volatility"]["global_mean"], b2["rank_volatility"]["global_mean"]))
    A("두꺼운 비교군의 변동도 %.2f → %.2f 입니다.\n"
      % (b1["rank_volatility"]["nonthin_mean"], b2["rank_volatility"]["nonthin_mean"]))
    A("### 나빠진 것 — 상위 목록의 재현성\n")
    A("**재표집 Top30 겹침이 %.3f → %.3f 로 떨어집니다.** 순위상관은 %.3f → %.3f 로"
      % (b1["resample_stability"]["frac_0.8"]["top30_mean"],
         b2["resample_stability"]["frac_0.8"]["top30_mean"],
         b1["resample_stability"]["frac_0.8"]["spearman_mean"],
         b2["resample_stability"]["frac_0.8"]["spearman_mean"]))
    A("거의 그대로인데 **상위 목록만** 흔들립니다. 원인은 같은 표에 있습니다 —")
    A("얇은 비교군 행의 변동이 %.2f → %.2f 로 **올라갔습니다.**\n"
      % (b1["rank_volatility"]["thin_mean"], b2["rank_volatility"]["thin_mean"]))
    A("M60 이 이름 붙인 **단계 이탈**입니다. `support_method` 를 바로잡으면 L1")
    A("(성격x방식) 칸이 잘게 나뉘고, 그중 일부가 `MIN_COHORT=20` **문턱 바로 위**에")
    A("놓입니다. 80% 재표집에서 그 칸은 문턱 아래로 떨어져 L2 로 후퇴하고, 그 순간")
    A("비교 대상이 통째로 바뀝니다. 실제로 성격만으로 후퇴한 행이 %d → %d 로 늘었습니다.\n"
      % (b1["cohort_profile"]["level_dist"].get("L2 support_type", 0),
         b2["cohort_profile"]["level_dist"].get("L2 support_type", 0)))
    A("> **이것은 데이터 수정을 되돌릴 근거가 아닙니다.** v1 의 Top30 %.3f 는"
      % b1["resample_stability"]["frac_0.8"]["top30_mean"])
    A("> *`grant` 한 덩어리로 뭉쳐 있어서* 안정적이었던 숫자입니다 — 융자·보증·")
    A("> 바우처 사업이 보조금 분포에서 percentile 을 받고 있었으니 목록이 흔들릴")
    A("> 일이 없었던 것이지, 맞는 목록이라 안정적이었던 것이 아닙니다. **안정성과")
    A("> 정합성이 반대로 움직이는 자리**이고, M58 이 `A1 성격만` 에서 이미")
    A("> 같은 것을 관측했습니다(굵게 묶으면 전 구간 최고 안정성).\n")

    A("## 2. 문턱 근처 칸 진단 (채택 후보 아님)\n")
    A("위 진단이 맞다면 문턱 근처 칸이 실제로 늘어야 합니다. M60 의 sweep 을 v1·v2")
    A("에서 같은 자로 다시 셌습니다.\n")
    A("| MIN_COHORT | pool | 전역 fallback | 얇은 비교군 행 | 문턱 근처 행 | ρ(0.8) | Top30(0.8) |")
    A("|---|---|---:|---:|---:|---:|---:|")
    for tag, blk in r["min_cohort_diagnosis"].items():
        for k, v in blk.items():
            A("| %s | %s | %d | %d | %d | %.3f | %.3f |"
              % (k.replace("MIN_COHORT=", ""), tag, v["n_global_fallback"],
                 v["n_thin_rows"], v["n_rows_near_threshold"],
                 v["spearman_0.8"], v["top30_0.8"]))
    A("")
    A("**문턱은 옮기지 않습니다.** 결과를 보고 문턱을 움직이면 그 순간 문턱은 판정")
    A("기준이 아니라 결론을 정당화하는 장식이 됩니다(M61 이 Mahalanobis 에서 지킨")
    A("규율). 그리고 M60 이 이미 잰 대로 문턱을 올리면 전역 fallback 이 급증해")
    A("**안정성이 좋아진 것이 아니라 재는 대상을 바꿔 흔들릴 일을 없앤 것**이 됩니다.")
    A("해법은 여전히 모델링이 아니라 그 비교군의 표본 확대입니다.\n")

    A("## 3. 얇은 비교군은 몇 개인가 (지시서 1번)\n")
    A("| | 얇은 비교군 개수 | 그 안의 행수 | 전체 비교군 | 비교군 표본수 중앙값 |")
    A("|---|---:|---:|---:|---:|")
    for k, v in r["thin_cohorts"].items():
        A("| %s | %d | %d | %d | %d |"
          % (k, v["n_thin_cohorts"], v["n_rows_in_thin"],
             v["n_distinct_cohorts"], v["cohort_size_median"]))
    A("")
    A("수정 후 pool 에서 표본 확대가 필요한 칸입니다 (n ≤ %d). `pool 밖`은 같은"
      % L.THIN_MAX)
    A("비교군에 속하지만 **수치축이 2개가 안 돼 빠진** 행 수입니다 — 이 칸이")
    A("크면 표본 확대가 새 수집이 아니라 **이미 가진 문서의 추출 개선**으로")
    A("해결된다는 뜻입니다.\n")
    A("| 비교군 | 단계 | pool 안 | pool 밖(축 부족) | 데이터 전체 |")
    A("|---|---|---:|---:|---:|")
    for c in r.get("expansion_headroom", []):
        A("| `%s` | %s | %d | %d | %d |"
          % (c["cohort"].replace("|", " x "), c["level"], c["in_pool"],
             c["excluded_for_axes"], c["all_rows"]))
    A("")
    eh = r.get("expansion_headroom", [])
    if eh:
        tot_in = sum(c["in_pool"] for c in eh)
        tot_out = sum(c["excluded_for_axes"] for c in eh)
        cleared = sum(1 for c in eh if c["all_rows"] > L.THIN_MAX)
        A("합계 pool 안 %d행 · pool 밖 %d행입니다. 전부 끌어오면 **%d칸 중 %d칸이**"
          % (tot_in, tot_out, len(eh), cleared))
        A("**얇은 비교군 기준(n > %d)을 넘습니다** — 교육훈련 26→88, 고용보조x mixed"
          % L.THIN_MAX)
        A("26→59 처럼 크게 벌어지는 칸이 있습니다. 나머지 %d칸은 데이터에 아예"
          % (len(eh) - cleared))
        A("행이 없어 새 수집이 필요합니다.\n")
        A("**표본 확대의 절반은 새 수집이 아니라 추출 개선입니다.** M62 D4 가")
        A("사업기간에서 한 일(목록 표본 1,400행 복원)을 나머지 축 — 특히")
        A("`지원비율`(pool 결측 60.9%)과 `지원 기업수` — 에서도 하면 됩니다.\n")

    A("## 4. M58 A3(+지원단위) 재검토\n")
    A("성능결과서 6장이 \"`지원단위` 결측 15% 해소되면 A3 이 다시 열린다\"고 적어 둔")
    A("항목입니다. 새 구조 탐색이 아니라 **예약돼 있던 재검토**라 여기서 처리합니다.")
    A("판정 문턱은 M58 이 결과를 보기 전에 고정한 값 그대로입니다")
    A("(Top30 겹침 %.3f / 순위상관 %.3f).\n" % (a3["thresholds"]["KEEP_TOP30"],
                                            a3["thresholds"]["KEEP_SPEARMAN"]))
    A("| | 현행 (C v2) | A3 +지원단위 |")
    A("|---|---:|---:|")
    A("| 재표집 ρ(0.8) | %.3f | %.3f |"
      % (v2["resample_stability"]["frac_0.8"]["spearman_mean"],
         a3["metrics"]["resample_stability"]["frac_0.8"]["spearman_mean"]))
    A("| 재표집 Top30(0.8) | %.3f | %.3f |"
      % (v2["resample_stability"]["frac_0.8"]["top30_mean"],
         a3["metrics"]["resample_stability"]["frac_0.8"]["top30_mean"]))
    A("| 얇은 비교군 행 | %d | %d |"
      % (v2["cohort_profile"]["n_thin"], a3["metrics"]["cohort_profile"]["n_thin"]))
    A("| 현행 대비 순위상관 | 1.000 | **%.3f** |" % a3["vs_current"]["spearman"])
    A("| 현행 대비 Top30 겹침 | 1.000 | **%.3f** |" % a3["vs_current"]["top30_overlap"])
    A("| 탐색적 ROC-AUC | %.4f | **%.4f** |"
      % (v2["exploratory_roc"]["roc_auc"], a3["metrics"]["exploratory_roc"]["roc_auc"]))
    A("")
    A("**판정: %s**\n" % a3["verdict"])
    for f in a3["fails"]:
        A("- %s" % f)
    A("")
    A("M58 의 `+기관계열` 과 **같은 모양**입니다 — 탐색적 ROC 는 넷 중 가장 높은데")
    A("(%.4f vs 현행 %.4f) 목록이 통째로 바뀌어 reject 됩니다. `지원단위` 결측을"
      % (a3["metrics"]["exploratory_roc"]["roc_auc"], v2["exploratory_roc"]["roc_auc"]))
    A("같은 행 기준 %.1f%% → **%.1f%%** 로 줄였는데도 열리지 않았습니다. 이유는"
      % (100 * r["data_quality"]["지원단위_결측률"]["B 교집합 v1"],
         100 * r["data_quality"]["지원단위_결측률"]["B 교집합 v2"]))
    A("결측률이 아니라 **비교군을 한 단계 더 자르면 문턱 근처 칸이 또 늘기 때문**")
    A("입니다(2절과 같은 원인). 결측을 더 줄여서 열릴 문이 아니라 **표본이 늘어야**")
    A("**열리는 문**입니다.\n")
    A("> 양성 5건짜리 라벨셋의 ROC 0.83 을 근거로 목록 교체를 승인하지 않습니다.")
    A("> 그 라벨셋의 95% 신뢰구간은 0.575~0.908 입니다(M44).\n")

    A("## 5. 결론\n")
    A("```text")
    A("구조            현행 유지. M44 Freeze 그대로 (거리·사다리·mean·standard·Euclid·20)")
    A("데이터 수정      채택. 비교군 구성·fallback·두꺼운 비교군 안정성이 모두 개선")
    A("남은 약점        얇은 비교군의 상위 목록 재현성 — 표본 확대 외의 해법이 또 없음")
    A("A3 +지원단위     재검토 결과 reject (사전 문턱 초과). 표본이 늘면 다시 연다")
    A("Mahalanobis      돌리지 않음 — 지시서가 '후속 후보로만 남긴다'고 지정")
    A("```\n")
    A("지시서의 종료 조건에 답하면 이렇습니다. **모델 3 쪽에서 데이터 수정의 실질")
    A("개선은 '점수가 좋아졌다'가 아니라 '비교군이 제대로 갈렸다'입니다.** 그리고")
    A("그 대가로 상위 목록이 흔들리는 것까지 같이 보이므로, 추가 모델 실험이 아니라")
    A("**얇은 비교군 표본 확대**가 다음 작업이라는 결론은 M48/M50/M60 과 같습니다 —")
    A("이번에는 원인이 한 단계 더 선명합니다.")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L_) + "\n")
    print("[report] %s" % MD)


def min_cohort_diagnosis(train, values=(15, 20, 25, 30)):
    """진단 전용 — 상위 목록이 흔들리는 원인이 '문턱 근처 칸'인가.

    채택 후보가 아니다. M60 이 v1 에서 이미 이 sweep 을 돌려 `MIN_COHORT=20`
    을 확정했고, 그때 관측한 것이 **단계 이탈**이다: 원본에서 n=25 인 비교군은
    80% 재표집에서 n≈20 이 되어 문턱 아래로 떨어지고, 그 순간 비교 대상이
    통째로 바뀐다. D5 가 `support_method` 를 바로잡아 L1 칸이 잘게 나뉘었으니
    **문턱 근처 칸이 몇 개 늘었는지**를 같은 자로 다시 세어 본다.

    결과를 보고 문턱을 옮기지 않는다 — 그 순간 문턱은 판정 기준이 아니라
    결론을 정당화하는 장식이 된다(M61 이 Mahalanobis 에서 지킨 규율).
    """
    out = {}
    for mc in values:
        res = L.score_pool(train, train, min_cohort=mc)
        prof = L.cohort_profile(res)
        stab = L.resample_stability(train, min_cohort=mc, fracs=[0.8], n_iter=L.N_ITER)
        n = res["cohort_n"]
        out["MIN_COHORT=%d" % mc] = {
            "n_global_fallback": prof["n_global_fallback"],
            "n_thin_rows": prof["n_thin"],
            "n_distinct_cohorts": prof["n_distinct_cohorts"],
            "level_dist": prof["level_dist"],
            # 문턱 근처(문턱 ~ 문턱x1.5) 칸에 속한 행수 — 단계 이탈 후보
            "n_rows_near_threshold": int(((n >= mc) & (n < mc * 1.5)).sum()),
            "spearman_0.8": stab["frac_0.8"]["spearman_mean"],
            "top30_0.8": stab["frac_0.8"]["top30_mean"],
        }
        print("   MIN_COHORT=%-2d fallback %3d · 얇은비교군 %3d · 문턱근처 %3d · "
              "Spearman %.3f · Top30 %.3f"
              % (mc, prof["n_global_fallback"], prof["n_thin"],
                 out["MIN_COHORT=%d" % mc]["n_rows_near_threshold"],
                 stab["frac_0.8"]["spearman_mean"], stab["frac_0.8"]["top30_mean"]))
    return out


def main():
    t0 = time.time()
    p1 = L.load_pool(V1)
    p2 = L.load_pool(V2)
    ids = sorted(set(p1["row_id"]) & set(p2["row_id"]))
    b1 = p1[p1["row_id"].isin(ids)].reset_index(drop=True)
    b2 = p2[p2["row_id"].isin(ids)].reset_index(drop=True)
    print("== pool  v1 %d / v2 %d / 교집합 %d" % (len(p1), len(p2), len(ids)))

    quality = {
        "지원단위_결측률": {"A v1": round(float(p1["support_unit"].isna().mean()), 4),
                      "B 교집합 v1": round(float(b1["support_unit"].isna().mean()), 4),
                      "B 교집합 v2": round(float(b2["support_unit"].isna().mean()), 4),
                      "C v2": round(float(p2["support_unit"].isna().mean()), 4)},
        "지원방식_분포": {"A v1": {str(k): int(v) for k, v in
                            p1["support_method"].value_counts().items()},
                    "C v2": {str(k): int(v) for k, v in
                             p2["support_method"].value_counts().items()}},
        "수치축_결측률": {
            tag: {a: round(float(d[a].isna().mean()), 4) for a in L.NUM}
            for tag, d in (("A v1", p1), ("C v2", p2))},
        "지원비율_최소": {"A v1": float(p1["support_ratio"].min()),
                    "C v2": float(p2["support_ratio"].min())},
    }
    print("   지원단위 결측률 %s" % quality["지원단위_결측률"])
    print("   지원비율 최소   %s" % quality["지원비율_최소"])

    print("\n== 현행 구조 재평가 (지시서 평가기준 5종)")
    blocks = {"A v1 pool": evaluate(p1, "A v1"),
              "B 교집합 v1": evaluate(b1, "B∩v1"),
              "B 교집합 v2": evaluate(b2, "B∩v2"),
              "C v2 pool": evaluate(p2, "C v2")}

    print("\n== 얇은 비교군 (지시서 1번)")
    thin = {"A v1": thin_cohort_view(p1), "C v2": thin_cohort_view(p2)}
    for k, v in thin.items():
        print("   %-5s 얇은 비교군 %d개 · %d행 (전체 비교군 %d개, 중앙 표본수 %d)"
              % (k, v["n_thin_cohorts"], v["n_rows_in_thin"],
                 v["n_distinct_cohorts"], v["cohort_size_median"]))

    print("\n== 문턱 근처 칸 진단 (채택 후보 아님 · M60 sweep 을 v1/v2 에서 다시)")
    mc_diag = {"A v1": min_cohort_diagnosis(p1), "C v2": min_cohort_diagnosis(p2)}

    print("\n== M58 A3(+지원단위) 재검토 — 문턱은 M58 이 미리 고정한 값 그대로")
    a3_kw = {"ladder": L.LADDERS["A3 +지원단위"]}
    a3 = evaluate(p2, "A3 on v2", **a3_kw)
    base = blocks["C v2 pool"]
    fails = L.verdict(None, a3["resample_stability"], a3["synthetic_stress"],
                      a3["feature_dependency"], a3["attribution_stability"],
                      base["resample_stability"], base["synthetic_stress"],
                      base["feature_dependency"], base["attribution_stability"])
    cmp_score = L.compare(L.score_pool(p2, p2)["score"],
                          L.score_pool(p2, p2, **a3_kw)["score"])
    if cmp_score["top30_overlap"] < L.KEEP_TOP30:
        fails.append("Top30 교체율이 사전 문턱(%.3f) 초과 — 실제 %.3f"
                     % (L.KEEP_TOP30, cmp_score["top30_overlap"]))
    if cmp_score["spearman"] < L.KEEP_SPEARMAN:
        fails.append("순위상관이 사전 문턱(%.3f) 미달 — 실제 %.3f"
                     % (L.KEEP_SPEARMAN, cmp_score["spearman"]))
    thin_up = (a3["cohort_profile"]["n_thin"] > base["cohort_profile"]["n_thin"] * 1.2)
    if thin_up:
        fails.append("얇은 비교군 급증 %d -> %d"
                     % (base["cohort_profile"]["n_thin"], a3["cohort_profile"]["n_thin"]))
    a3_verdict = "현행 유지 (reject)" if fails else "채택 후보 — 승격 절차 필요"
    print("   판정: %s  %s" % (a3_verdict, fails if fails else ""))
    print("   현행 대비 %s" % cmp_score)

    rep = {
        "datasets": {"v1": os.path.relpath(V1, C.ROOT), "v2": os.path.relpath(V2, C.ROOT)},
        "pool_rows": {"v1": int(len(p1)), "v2": int(len(p2)), "intersection": len(ids)},
        "structure": {"ladder": L.BASE_LADDER, "min_cohort": L.MIN_COHORT,
                      "scaler": "standard", "metric": "euclidean", "n_proto": 1,
                      "note": "M44 Freeze 그대로 — 이 스크립트는 구조를 바꾸지 않는다"},
        "data_quality": quality,
        "evaluation": blocks,
        "thin_cohorts": thin,
        "expansion_headroom": expansion_headroom(V2, thin["C v2"]),
        "min_cohort_diagnosis": mc_diag,
        "m58_a3_recheck": {"ladder": "A3 +지원단위", "on": "C v2 pool",
                           "thresholds": {"KEEP_TOP30": L.KEEP_TOP30,
                                          "KEEP_SPEARMAN": L.KEEP_SPEARMAN},
                           "vs_current": cmp_score, "fails": fails,
                           "verdict": a3_verdict, "metrics": a3},
        "not_run": {"Mahalanobis 유사 거리":
                    "지시서가 '향후 재검토 후보로만 남긴다'고 지정 — 돌리지 않는다"},
        "elapsed_sec": round(time.time() - t0, 1),
    }
    C.save_report("m64_m3_requal.json", rep)
    write_md(rep)
    print("\n[%.0fs] 완료" % rep["elapsed_sec"])


if __name__ == "__main__":
    main()
