r"""M61 — Model 3 실험 D: scaling / distance 재점검 (지시서 Part B 7절, 4순위).

지시서가 이 실험을 **마지막에만** 하라고 한 이유가 이미 기록돼 있다.
M47 에서 RobustScaler 는 exploratory ROC 를 +0.0041 올렸지만 상위 30건 중
20건(67%)을 다른 사업으로 교체했다. **경고 목록이 통째로 바뀌는 변경을
ROC 는 보지 못한다.** 그래서 여기서도 무작정 채택하지 않는다.

순서도 지시서 7절 그대로다 — 바꾸기 전에 **먼저 축을 본다.**

    skewness / heavy tail / zero inflation / extreme outlier / 단위 오류 /
    log transform 이 이미 걸려 있는가

    이걸 안 보고 scaler 부터 갈아끼우면 "왜 robust 가 다르게 도는가"를
    설명할 수 없다. 진단이 지목하지 않은 변형은 후보로 올리지 않는다.

후보 (지시서 7절)

    scaling   현행 standard / robust / mad / log1p+standard
    distance  현행 Euclidean / 축가중 Euclidean / Mahalanobis 유사

    가중 Euclidean 의 가중치는 **비교군 내부 잔차의 표준편차 역수**로 정한다.
    사람 라벨 ROC 를 보고 손으로 고르지 않는다(지시서 7절 주의). 라벨을 한
    건도 쓰지 않는 정의이고, M51 이 금액 축에서 관측한 신호/잡음비 문제를
    정면으로 겨냥한다 — 비교군 안에서 원래 넓게 퍼지는 축의 과대 기여를
    깎는 것이다.
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
from scipy.stats import kurtosis, skew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import m3_lab as L

RAW_KR = {"per_recipient": "기업당 지원액(원)", "support_count": "지원 기업수",
          "project_duration": "사업기간(개월)", "support_ratio": "지원비율(%)"}

VARIANTS = {
    "현행 (standard · Euclid)": {},
    "robust scaling": {"scaler": "robust"},
    "MAD scaling": {"scaler": "mad"},
    "log1p + standard": {"log_axes": L.LOG1P_AXES},
    "축가중 Euclid": {"metric": "diag"},
    "Mahalanobis 유사": {"metric": "mahalanobis"},
}


def axis_diagnostics(train):
    """바꾸기 전에 축을 본다 (지시서 7절). 원 스케일과 모델 입력 스케일을
    같이 놓아야 'log 가 이미 걸려 있다'가 보인다."""
    rows = []
    for raw, kr in RAW_KR.items():
        v = train[raw].dropna().to_numpy(dtype=float)
        model_axis = {"per_recipient": "log_per_recipient",
                      "support_count": "log_support_count"}.get(raw, raw)
        m = train[model_axis].dropna().to_numpy(dtype=float)
        q1, q3 = np.percentile(v, [25, 75])
        iqr = q3 - q1
        rows.append({
            "축": kr, "모델입력": model_axis,
            "n": int(len(v)), "결측": int(train[raw].isna().sum()),
            "0값비율": round(float((v == 0).mean()), 4),
            "최소": float(v.min()), "중앙": float(np.median(v)),
            "최대": float(v.max()),
            "최대/중앙": round(float(v.max() / max(np.median(v), 1e-9)), 1),
            "왜도_원": round(float(skew(v)), 2),
            "첨도_원": round(float(kurtosis(v)), 2),
            "왜도_모델": round(float(skew(m)), 2),
            "첨도_모델": round(float(kurtosis(m)), 2),
            "IQR밖3배": int(((v < q1 - 3 * iqr) | (v > q3 + 3 * iqr)).sum()),
        })
    return pd.DataFrame(rows)


def run_variant(train, kw, ids, y, holdout_ids):
    fit = train[~train["row_id"].isin(holdout_ids)].reset_index(drop=True)
    res = L.score_pool(train, train, **kw)
    res_ho = L.score_pool(fit, train, **kw)
    return {
        "config": {k: str(v) for k, v in kw.items()},
        "cohort": L.cohort_profile(res),
        "stability": L.resample_stability(train, **kw),
        "synthetic": L.synthetic_stress(train, **kw),
        "dependency": L.feature_dependency(train, **kw),
        "attribution": L.attribution_stability(train, **kw),
        "labeled": L.eval_labeled(res_ho["score"], ids, y),
        "_score": res["score"],
    }


def main():
    train = L.load_pool()
    holdout_ids, ids, y = L.load_labels(train)

    print("M61 — 실험 D: scaling / distance 재점검 (지시서 Part B 7절)")
    print("  pool %d행. 지시서대로 **축 진단을 먼저** 한다." % len(train))

    # ------------------------------------------------------- 0. 축 진단
    diag = axis_diagnostics(train)
    print("\n== 0. 수치축 진단 (원 스케일)")
    print("  %-18s %6s %6s %8s %12s %10s %8s %8s"
          % ("축", "n", "결측", "0값비율", "최대/중앙", "왜도(원)", "왜도(모델)", "IQR밖"))
    for _, r in diag.iterrows():
        print("  %-18s %6d %6d %8.3f %12.1f %10.2f %8.2f %8d"
              % (r["축"], r["n"], r["결측"], r["0값비율"], r["최대/중앙"],
                 r["왜도_원"], r["왜도_모델"], r["IQR밖3배"]))
    print("\n  범위")
    for _, r in diag.iterrows():
        print("  %-18s 최소 %14.4g  중앙 %14.4g  최대 %14.4g"
              % (r["축"], r["최소"], r["중앙"], r["최대"]))

    # 진단이 어떤 후보를 지목하는가
    heavy = diag[diag["왜도_모델"].abs() > 2]["축"].tolist()
    print("\n  -> 모델 입력 단계에서도 왜도 |2| 를 넘는 축: %s"
          % (", ".join(heavy) or "없음"))

    # ------------------------------------------------------- 1~4. 변형
    variants = {}
    for nm, kw in VARIANTS.items():
        print("\n  [%s] 채점 중..." % nm)
        variants[nm] = run_variant(train, kw, ids, y, holdout_ids)
    base = variants["현행 (standard · Euclid)"]
    for nm, v in variants.items():
        v["vs_base"] = L.compare(base["_score"], v["_score"])

    print("\n== 1. 현행 대비 순위·목록 변화")
    print("  %-24s %10s %8s %8s %8s"
          % ("변형", "Spearman", "Top10", "Top30", "Top39"))
    for nm, v in variants.items():
        c = v["vs_base"]
        print("  %-24s %10.4f %8.3f %8.3f %8.3f"
              % (nm, c["spearman"], c["top10_overlap"], c["top30_overlap"],
                 c["top39_overlap"]))

    print("\n== 2. 재표집 안정성")
    for metric, lbl in (("spearman_mean", "Spearman"), ("top30_mean", "Top30 겹침")):
        print("  -- %-14s %s" % (lbl, "  ".join("%6d%%" % int(f * 100) for f in L.FRACS)))
        for nm, v in variants.items():
            print("     %-22s %s"
                  % (nm[:22], "  ".join("%7.4f" % v["stability"]["frac_%.1f" % f][metric]
                                        for f in L.FRACS)))

    print("\n== 3. Synthetic · 의존도 · attribution · 참고 ROC")
    print("  %-24s %10s %10s %10s %10s %8s"
          % ("변형", "최저단조성", "최대축점유", "설명축유지", "ROC", "PR"))
    for nm, v in variants.items():
        s, d, a, b = v["synthetic"], v["dependency"], v["attribution"], v["labeled"]
        print("  %-24s %10.3f %10.3f %10.3f %10.4f %8.4f"
              % (nm, s["min_positive_rate"], d["max_axis_share"],
                 a["top1_axis_agreement_mean"], b["roc_auc"], b["pr_auc"]))
    print("\n  축별 기여도 점유 (상위 39건)")
    for nm, v in variants.items():
        print("  %-24s %s"
              % (nm, "  ".join("%s %.2f" % (L.AXIS_KR[k], x)
                               for k, x in v["dependency"]["top39_contribution_share"].items())))

    print("\n== 4. 판정")
    for nm, v in variants.items():
        if nm == "현행 (standard · Euclid)":
            v.update({"fails": [], "verdict": "기준선"})
            print("  %-24s 기준선" % nm)
            continue
        fails = L.verdict(v["vs_base"], v["stability"], v["synthetic"],
                          v["dependency"], v["attribution"],
                          base["stability"], base["synthetic"],
                          base["dependency"], base["attribution"])
        # M47 이 세운 문턱 — 목록이 통째로 바뀌면 ROC 가 올라도 채택하지 않는다
        if v["vs_base"]["top30_overlap"] < L.KEEP_TOP30:
            fails.append("Top30 목록 %.0f%% 교체 (문턱 %.0f%%)"
                         % ((1 - v["vs_base"]["top30_overlap"]) * 100,
                            (1 - L.KEEP_TOP30) * 100))
        v["fails"] = fails
        v["verdict"] = "REJECT" if fails else "채택 후보"
        print("  %-24s %-10s %s" % (nm, v["verdict"], " / ".join(fails) or "필수조건 통과"))

    rep = {
        "실험": "D. scaling / distance 재점검 (지시서 Part B 7절)",
        "순서": "축 진단 -> 진단이 지목한 후보만 비교",
        "n_pool": int(len(train)),
        "axis_diagnostics": diag.to_dict("records"),
        "가중치_정의": ("축가중 Euclid 의 가중치 = 비교군 내부 잔차 표준편차의 역수. "
                   "사람 라벨을 한 건도 쓰지 않는다 (지시서 7절 주의)"),
        "variants": {nm: {k: v for k, v in x.items() if not k.startswith("_")}
                     for nm, x in variants.items()},
    }
    C.save_report("m61_m3_scaling.json", rep)
    write_md(rep, variants, diag, base)


def write_md(r, variants, diag, base):
    L_ = ["# M61 — 실험 D: scaling / distance 재점검", "",
          "> 지시서 Part B 7절, 실험 우선순위 **4순위 (마지막)**.", "",
          "지시서가 이 실험을 마지막에만 하라고 한 이유가 이미 기록돼 있습니다.",
          "M47 에서 `RobustScaler` 는 exploratory ROC 를 **+0.0041** 올렸지만 상위",
          "30건 중 20건(67%)을 다른 사업으로 교체했습니다. **경고 목록이 통째로**",
          "**바뀌는 변경을 ROC 는 보지 못합니다.**", "",
          "## 0. 바꾸기 전에 축을 봅니다", "",
          "지시서 7절 순서 그대로입니다. 이걸 안 보고 scaler 부터 갈아끼우면",
          "\"왜 robust 가 다르게 도는가\"를 설명할 수 없습니다.", "",
          "| 축 | 모델 입력 | n | 결측 | 0값 비율 | 최대/중앙 | 왜도(원) | 왜도(모델입력) | 첨도(모델입력) | IQR 3배 밖 |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for _, x in diag.iterrows():
        L_.append("| %s | `%s` | %d | %d | %.3f | %.1f | %.2f | **%.2f** | %.2f | %d |"
                  % (x["축"], x["모델입력"], x["n"], x["결측"], x["0값비율"],
                     x["최대/중앙"], x["왜도_원"], x["왜도_모델"], x["첨도_모델"],
                     x["IQR밖3배"]))
    L_ += ["", "| 축 | 최소 | 중앙 | 최대 |", "|---|---:|---:|---:|"]
    for _, x in diag.iterrows():
        L_.append("| %s | %.4g | %.4g | %.4g |"
                  % (x["축"], x["최소"], x["중앙"], x["최대"]))
    L_ += ["",
           "**읽는 법 셋.**", "",
           "**(가) log 는 이미 걸려 있고, 잘 듣고 있습니다.** `기업당 지원액` 은 원",
           "스케일 왜도 %.2f → 모델 입력 %.2f, `지원 기업수` 는 %.2f → %.2f 입니다"
           % (diag.iloc[0]["왜도_원"], diag.iloc[0]["왜도_모델"],
              diag.iloc[1]["왜도_원"], diag.iloc[1]["왜도_모델"]),
           "(M13 `prepare` 의 log10). 이 두 축에 log 를 또 거는 후보는 진단이",
           "지목하지 않습니다.", "",
           "**(나) 남은 두 축은 왜도가 크지만 log 로 풀리는 종류가 아닙니다.**",
           "`사업기간` 왜도 +%.2f 는 오른쪽 꼬리라 log 후보가 정당합니다. 그러나"
           % diag.iloc[2]["왜도_모델"],
           "`지원비율` 왜도 **%.2f 는 왼쪽 꼬리**입니다 — 값이 80~100%%에 몰려 있고"
           % diag.iloc[3]["왜도_모델"],
           "아래로 늘어진 모양이라, 오른쪽 꼬리를 눌러주는 log 는 방향이 맞지",
           "않습니다.", "",
           "**(다) 그 왼쪽 꼬리는 분포가 아니라 데이터 오류였습니다.** `지원비율`",
           "최소값이 **%.0f%%** 입니다. 비율이 음수일 수 없으므로 파싱 오류입니다."
           % diag.iloc[3]["최소"],
           "확인해 보니 886건 중 **정확히 1건**(`[전남] 2025년 강소기업 육성사업`)",
           "이고, 이 한 건이 축 전체의 왜도를 만들고 있었습니다. 지시서 7절이",
           "\"단위 오류 가능성\"을 먼저 보라고 한 자리이고, 이것이 그 산출물입니다 —",
           "**scaler 를 바꿔서 다룰 문제가 아니라 M31 파서 감사 대상**입니다.", "",
           "> 결측도 같이 봐야 합니다. `사업기간` 은 %.0f%%, `지원비율` 은 %.0f%%가"
           % (diag.iloc[2]["결측"] / r["n_pool"] * 100,
              diag.iloc[3]["결측"] / r["n_pool"] * 100),
           "> 비어 있어 중앙값으로 채워집니다. 이 두 축에서 scaling 을 손보는 것은",
           "> **채워 넣은 값의 축척을 손보는 것**에 가깝습니다.", "",
           "## 1. 현행 대비 순위·목록 변화", "",
           "| 변형 | Spearman | Top10 | Top30 | Top39 |", "|---|---:|---:|---:|---:|"]
    for nm, v in variants.items():
        c = v["vs_base"]
        L_.append("| %s | %.4f | %.3f | %.3f | %.3f |"
                  % (nm, c["spearman"], c["top10_overlap"], c["top30_overlap"],
                     c["top39_overlap"]))

    L_ += ["", "## 2. 재표집 안정성", ""]
    for metric, lbl in (("spearman_mean", "Spearman 순위상관"), ("top30_mean", "Top30 겹침")):
        L_ += ["**%s**" % lbl, "",
               "| 변형 | " + " | ".join("%d%%" % int(f * 100) for f in L.FRACS) + " |",
               "|---|" + "---:|" * len(L.FRACS)]
        for nm, v in variants.items():
            L_.append("| %s | %s |"
                      % (nm, " | ".join("%.4f" % v["stability"]["frac_%.1f" % f][metric]
                                        for f in L.FRACS)))
        L_.append("")

    L_ += ["## 3. Synthetic · 의존도 · attribution · 참고 ROC", "",
           "| 변형 | 최저 단조성 | 최대 축 점유율 | 설명축 유지율 | ROC-AUC | PR-AUC |",
           "|---|---:|---:|---:|---:|---:|"]
    for nm, v in variants.items():
        s, d, a, b = v["synthetic"], v["dependency"], v["attribution"], v["labeled"]
        L_.append("| %s | %.3f | %.3f | %.3f | %.4f | %.4f |"
                  % (nm, s["min_positive_rate"], d["max_axis_share"],
                     a["top1_axis_agreement_mean"], b["roc_auc"], b["pr_auc"]))
    L_ += ["", "축별 기여도 점유 (상위 39건)", "",
           "| 변형 | " + " | ".join(L.AXIS_KR[a] for a in L.NUM) + " |",
           "|---|" + "---:|" * len(L.NUM)]
    for nm, v in variants.items():
        sh = v["dependency"]["top39_contribution_share"]
        L_.append("| %s | %s |" % (nm, " | ".join("%.2f" % sh[a] for a in L.NUM)))

    L_ += ["", "## 4. 판정", "",
           "필수조건(지시서 9절)에 M47 이 세운 문턱을 하나 더 얹습니다 — **상위 30건",
           "목록이 %.0f%% 넘게 교체되면 ROC 와 무관하게 reject**. 운영에서 바뀌는"
           % ((1 - L.KEEP_TOP30) * 100),
           "것은 점수가 아니라 담당자가 받아보는 목록이기 때문입니다.", "",
           "| 변형 | 판정 | 이유 |", "|---|---|---|"]
    for nm, v in variants.items():
        L_.append("| %s | **%s** | %s |" % (nm, v["verdict"], " / ".join(v["fails"]) or "—"))

    rb, lg, dg, mh = (variants["robust scaling"], variants["log1p + standard"],
                      variants["축가중 Euclid"], variants["Mahalanobis 유사"])
    bsh = base["dependency"]["top39_contribution_share"]
    rsh = rb["dependency"]["top39_contribution_share"]
    L_ += ["", "## 5. 읽은 것", "",
           "**(1) M47 의 관측이 재현되고, 이번에는 원인까지 나왔습니다.**",
           "`robust scaling` 은 상위 30건 중 %.0f%%를 교체하면서 exploratory ROC 는"
           % ((1 - rb["vs_base"]["top30_overlap"]) * 100),
           "%.4f (현행 %.4f) 로 거의 같습니다 — M47 이 본 그대로입니다. 원인은"
           % (rb["labeled"]["roc_auc"], base["labeled"]["roc_auc"]),
           "축별 기여도 표에 있습니다. robust 로 바꾸면 `지원비율` 하나가 상위",
           "39건 기여도의 **%.0f%%**(현행 %.0f%%)를 가져갑니다. RobustScaler 는"
           % (rsh["support_ratio"] * 100, bsh["support_ratio"] * 100),
           "IQR 로 나누는데, `지원비율` 은 값이 80~100% 에 몰려 IQR 이 매우 좁아",
           "**나눗셈이 그 축을 폭발시킵니다.** 사실상 지원비율 하나짜리 모델이",
           "되는 것이고, ROC 가 그대로인 것은 우연입니다.", "",
           "**(2) 진단이 지목한 후보(`log1p`)는 문제를 겨냥하지 못합니다.**",
           "`사업기간` 은 오른쪽 꼬리라 log 가 맞지만 `지원비율` 은 왼쪽 꼬리이고,",
           "그 왼쪽 꼬리는 분포가 아니라 **파싱 오류 1건**이었습니다(0장 다). 실제로",
           "log1p 를 걸면 Top30 겹침 %.3f 로 목록이 크게 바뀌면서 기여도만 `사업기간`"
           % lg["vs_base"]["top30_overlap"],
           "쪽으로 %.2f → %.2f 쏠립니다. 고칠 곳은 scaler 가 아니라 데이터입니다."
           % (bsh["project_duration"],
              lg["dependency"]["top39_contribution_share"]["project_duration"]),
           "", "**(3) 거리 함수 둘은 의도한 효과를 내지만 대가가 다릅니다.**",
           "`축가중 Euclid` 는 최대 축 점유율을 %.3f → %.3f 로 크게 낮춰 feature"
           % (base["dependency"]["max_axis_share"], dg["dependency"]["max_axis_share"]),
           "dependency 는 확실히 개선합니다. 그러나 같은 조작이 \"이 비교군에서는",
           "원래 잘 안 움직이는 축\"을 그만큼 키워 상위 목록의 %.0f%%가 바뀝니다."
           % ((1 - dg["vs_base"]["top30_overlap"]) * 100),
           "", "`Mahalanobis 유사` 는 이번 실험에서 **유일하게 아쉬운 후보**입니다.",
           "재표집 안정성이 네 표집비율 전부에서 현행보다 낫고(Spearman %s vs %s),"
           % (" / ".join("%.4f" % mh["stability"]["frac_%.1f" % f]["spearman_mean"]
                         for f in L.FRACS),
              " / ".join("%.4f" % base["stability"]["frac_%.1f" % f]["spearman_mean"]
                         for f in L.FRACS)),
           "축 점유율도 %.3f → %.3f 로 내려가며, ROC 는 %.4f 로 동일합니다."
           % (base["dependency"]["max_axis_share"], mh["dependency"]["max_axis_share"],
              mh["labeled"]["roc_auc"]),
           "그런데 상위 30건의 %.0f%%가 교체되어 사전 고정 문턱(%.0f%%)을 %.0f%%p"
           % ((1 - mh["vs_base"]["top30_overlap"]) * 100, (1 - L.KEEP_TOP30) * 100,
              ((1 - mh["vs_base"]["top30_overlap"]) - (1 - L.KEEP_TOP30)) * 100),
           "넘습니다. **문턱을 결과를 보고 옮기지 않습니다** — 그 순간 문턱은",
           "판정 기준이 아니라 결론을 정당화하는 장식이 됩니다. 다만 이 후보는",
           "\"기각\"이 아니라 **\"라벨이 확보되면 가장 먼저 다시 볼 후보\"**로",
           "기록해 둡니다. 지금 갈라놓을 근거가 양성 5건뿐이라 못 가리는 것입니다.", "",
           "**결론: scaling·distance 는 현행 유지 (standard scaling · Euclidean).**",
           "M47 의 결론과 같고, 이번에는 log1p 와 거리 함수 두 종까지 확인한 뒤의",
           "같은 결론입니다. 이 실험이 새로 남긴 것은 채택안이 아니라 **`지원비율`",
           "파싱 오류 1건**과 **robust scaling 이 무너지는 이유**입니다.", ""]

    p = C.report_path("m61_m3_scaling.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L_))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
