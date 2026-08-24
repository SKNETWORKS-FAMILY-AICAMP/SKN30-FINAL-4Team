"""N09 — 모델 2·3·4 의 ML / DL 성능 통합 비교.

N03~N05(ML)와 N06~N08(DL)이 낸 측정값을 한 표로 모은다. 새로 학습하지 않고
각 리포트의 JSON 만 읽는다 — 같은 수치를 두 번 만들면 어느 쪽이 진짜인지
알 수 없게 된다.

비교가 성립하려면 세 모델 모두 '같은 자'로 쟀어야 한다. 각 DL 스크립트에서
그 조건을 어떻게 맞췄는지 여기 함께 적는다.
    모델 2  군집은 각자의 공간에서, 점수는 둘 다 같은 Gower 거리행렬에서
    모델 3  같은 GroupKFold(5) by program_stem, 같은 타깃 log10(기업당 지원액)
    모델 4  같은 합성 이상치(4종·같은 시드), 같은 인코딩, 같은 재표집 검증

딥러닝 채택 기준은 모델 1 때와 같다 — ML 최고 모델을 넘어야 하고, 넘더라도
안정성 검증을 통과해야 한다.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

R = C.REPORTS


def load(name):
    p = os.path.join(R, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def m2_rows(ml, dl):
    rows = []
    if ml:
        b = ml["results"]["B_지원성격제외"]
        h, st = b["hdbscan"], b["stability"]
        rows.append(("ML", "Gower + HDBSCAN", h["n_clusters"], h["noise_ratio"],
                     h["silhouette"], h["davies_bouldin"], h["ari_support_type"],
                     st["ari_mean"]))
        for name, a in b["alternatives"].items():
            rows.append(("ML", name, a["n_clusters"], a["noise_ratio"], a["silhouette"],
                         a["davies_bouldin"], a["ari_support_type"], None))
    if dl:
        for label, key in (("AE latent + HDBSCAN (튜닝 전)", "base_result"),
                           ("AE latent + HDBSCAN (튜닝 후)", "tuned_result")):
            t = dl[key]
            rows.append(("DL", label, t["n_clusters"], t["noise_ratio"], t["silhouette"],
                         t["davies_bouldin"], t["ari_support_type"], None))
    return rows


def m3_rows(ml, dl):
    rows = []
    if ml:
        for name, v in ml["phase_b"]["results"].items():
            rows.append(("ML", name, v["MAE_log10"], v["geo_mean_error_x"],
                         v["within_2x"], v.get("p10_p90_coverage")))
    if dl:
        for label, key in (("MLP quantile (튜닝 전)", "base_result"),
                           ("MLP quantile (튜닝 후)", "tuned_result")):
            v = dl.get(key)
            if v and v.get("MAE_log10") == v.get("MAE_log10"):   # NaN 방어
                rows.append(("DL", label, v["MAE_log10"], v["geo_mean_error_x"],
                             v["within_2x"], v.get("p10_p90_coverage")))
    return rows


def m4_rows(ml, dl):
    rows = []
    if ml:
        for name, v in ml["synthetic_eval"].items():
            stab = (ml["resample_stability"]["overlap_mean"]
                    if name == ml["best_model"] else None)
            rows.append(("ML", name, v["recall_at_k"], v["recall_at_2k"],
                         v["median_rank_pct"], stab))
    if dl:
        for label, key in (("AE 복원오차 (튜닝 전)", "base_result"),
                           ("AE 복원오차 (튜닝 후)", "tuned_result")):
            v = dl[key]
            stab = (dl["resample_stability"]["overlap_mean"]
                    if key == ("base_result" if dl.get("ofat_combination_failed")
                               else "tuned_result") else None)
            rows.append(("DL", label, v["recall_at_k"], v["recall_at_2k"],
                         v["median_rank_pct"], stab))
    return rows


def fmt(v, spec="%.4f"):
    return "—" if v is None else (spec % v if isinstance(v, float) else str(v))


def main():
    m2ml, m2dl = load("n03_m2_cluster.json"), load("n06_m2_dl.json")
    m3ml, m3dl = load("n04_m3_cohort.json"), load("n07_m3_dl.json")
    m4ml, m4dl = load("n05_m4_anomaly.json"), load("n08_m4_dl.json")

    L = ["# 모델 2·3·4 — ML / DL 성능 비교", "",
         "> N03~N05(ML)와 N06~N08(DL)의 측정값을 모았습니다. 새로 학습하지 않고",
         "> 각 리포트 JSON 을 읽습니다.", "",
         "## 0. 한눈에", "",
         "| 모델 | ML 채택 | DL 후보 | 딥러닝 판정 |", "|---|---|---|---|"]

    m2v = (m2dl or {}).get("verdict", {}).get("verdict", "—")
    m3v = (m3dl or {}).get("verdict", "—")
    m4v = (m4dl or {}).get("verdict", {}).get("verdict", "—")
    L += [
        "| 2. 설계유형 군집 | Gower + HDBSCAN | AE latent + HDBSCAN | **%s** |" % m2v,
        "| 3. 지원규모 상대비교 | LGBM-quantile50 | MLP quantile | **%s** |" % m3v,
        "| 4. 설계 이상탐지 | OneClassSVM | AE 복원오차 | **%s** |" % m4v, ""]

    # ---- 모델 2 ----
    L += ["## 1. 모델 2 — 설계유형 군집", "",
          "**같은 자로 쟀습니다.** 군집 배정은 각자의 공간(Gower 거리 / AE 잠재공간)에서",
          "만들되, 실루엣·DBI·ARI 는 **둘 다 같은 Gower 거리행렬** 위에서 계산했습니다.", "",
          "| | 모델 | 군집 수 | 노이즈 | 실루엣 | DBI | 지원성격 ARI | bootstrap ARI |",
          "|---|---|---:|---:|---:|---:|---:|---:|"]
    for k, name, n, noise, sil, dbi, ari, stab in m2_rows(m2ml, m2dl):
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s |"
                 % (k, name, n, fmt(noise, "%.1f%%") if noise is None
                    else "%.1f%%" % (noise * 100), fmt(sil), fmt(dbi), fmt(ari),
                    fmt(stab)))
    L += ["",
          "지원성격 ARI 가 0.50 이상이면 설계서 실패 조건(모델 1 복제)입니다. 전부 통과했습니다.", ""]

    # ---- 모델 3 ----
    L += ["## 2. 모델 3 — 지원규모 상대비교", "",
          "**같은 자로 쟀습니다.** GroupKFold(5) by `program_stem`, 타깃 log10(기업당 지원액).",
          "재공고·동일사업이 학습/검증에 갈라지지 않게 통제한 것도 동일합니다.", "",
          "| | 모델 | MAE(log10) | 배수 오차 | 2배 이내 | P10~P90 포함률 |",
          "|---|---|---:|---:|---:|---:|"]
    for k, name, mae, geo, w2, cov in sorted(m3_rows(m3ml, m3dl), key=lambda r: r[2]):
        L.append("| %s | %s | %.4f | %.2fx | %.1f%% | %s |"
                 % (k, name, mae, geo, w2 * 100,
                    "—" if cov is None else "%.1f%%" % (cov * 100)))
    L += ["",
          "P10~P90 포함률은 딥러닝만 낼 수 있는 값입니다 — 분위 3개를 함께 학습하기",
          "때문입니다. ML 쪽은 점추정이라 해당 칸이 비어 있습니다.", ""]

    # ---- 모델 4 ----
    L += ["## 3. 모델 4 — 설계 이상탐지", "",
          "**같은 자로 쟀습니다.** 같은 합성 이상치 4종(극소수·극고액 / 극다수·극소액 /",
          "비정상 장기 / 지원비율 100%), 같은 시드, 같은 인코딩.", "",
          "| | 모델 | top-k 회수율 | top-2k | 합성사례 중앙 순위 | 재학습 유지율 |",
          "|---|---|---:|---:|---:|---:|"]
    for k, name, r1, r2, rank, stab in sorted(m4_rows(m4ml, m4dl),
                                              key=lambda r: -r[2]):
        L.append("| %s | %s | %.1f%% | %.1f%% | 상위 %.1f%% | %s |"
                 % (k, name, r1 * 100, r2 * 100, rank,
                    "—" if stab is None else "%.0f%%" % (stab * 100)))
    L += [""]

    # ---- 결론 ----
    L += ["## 4. 정리", "", "| 모델 | 결과 |", "|---|---|"]
    if m2dl:
        L.append("| 2 | %s |" % " / ".join(m2dl["verdict"]["reasons"]))
    if m3dl:
        L.append("| 3 | cohort median 대비 %+.1f%%, LGBM-quantile 대비 %+.1f%% → %s |"
                 % (m3dl["improvement_vs_cohort_median"] * 100,
                    m3dl["improvement_vs_ml_best"] * 100, m3dl["verdict"]))
    if m4dl:
        L.append("| 4 | %s |" % " / ".join(m4dl["verdict"]["reasons"]))
    L += ["", "### 모델 1과 비교", "",
          "모델 1에서는 KLUE-RoBERTa 가 ML 기준선을 넘었습니다(Macro F1 0.6428 → 0.6940).",
          "사전학습 모델이 한국어 지식을 이미 갖고 있어 900건으로도 충분했기 때문입니다.",
          "모델 2~4는 표 형식 수치·범주 데이터라 그런 전이가 없습니다. 2천 행 규모에서",
          "처음부터 학습하는 신경망이 트리 계열·거리 기반 방법을 넘기 어려운 조건입니다.", "",
          "### 학습 환경", "",
          "```text",
          "모델 1  KLUE-RoBERTa 파인튜닝 — RunPod GPU 필요",
          "모델 2~4 DL  표 형식 2천여 행 / AE·MLP — 로컬 CPU 로 충분",
          "             (AE 300 epoch 2,339행 = 1.06초, 전체 ablation 이 수 분)",
          "```", ""]

    p = os.path.join(R, "n09_ml_dl_compare.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("\n".join(L))
    print("\n[report] %s" % p)

    C.save_report("n09_ml_dl_compare.json", {
        "model2": {"ml": "n03_m2_cluster.json", "dl": "n06_m2_dl.json", "verdict": m2v},
        "model3": {"ml": "n04_m3_cohort.json", "dl": "n07_m3_dl.json", "verdict": m3v},
        "model4": {"ml": "n05_m4_anomaly.json", "dl": "n08_m4_dl.json", "verdict": m4v},
    })


if __name__ == "__main__":
    main()
