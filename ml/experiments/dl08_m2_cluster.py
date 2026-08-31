"""DL08 — 모델 2 딥러닝: Autoencoder latent embedding + 군집 + Ablation.

설계서의 모델 2 우선순위 4위가 "Autoencoder latent embedding + clustering" 이다.
1~3위(HDBSCAN / GMM / K-Means)는 M11 에서 이미 쟀다.

기준선 (M11, 지원성격 제외 조건 · 같은 1,447행)
    Gower + HDBSCAN   군집 20 / 노이즈 41.1% / 실루엣 0.6012
                      지원성격 ARI 0.2245 / bootstrap ARI 0.9545

공정한 비교를 위해 지킨 것
    AE 는 유클리드 잠재공간에서 군집하고 M11 은 Gower 거리에서 군집한다.
    각자의 공간에서 실루엣을 재면 숫자가 서로 다른 자를 쓰는 셈이다.
    그래서 **군집 배정만 AE 에서 가져오고, 품질 점수는 M11 과 똑같은 Gower
    거리행렬 위에서 잰다.** 이렇게 해야 "어느 쪽 군집이 더 나은가"를 말할 수 있다.

설계서가 못박은 실패 조건은 여기서도 같다.
    > 모델 1의 19클래스를 다시 복제하는 군집이면 실패  (ARI >= 0.50)

Ablation 축 (기존 DL06 방식과 동일 — one-factor-at-a-time)
    latent        2 / 4 / 8 / 16
    hidden        32 / 64 / 128
    epochs        200 / 500 / 1000
    lr            3e-4 / 1e-3 / 3e-3
    dropout       0.0 / 0.1 / 0.3
    denoising     0.0 / 0.1 / 0.2   (입력 잡음 비율)
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m4_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

_ML = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = _os.path.join(_ML, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# -------------------------------------------------------------------------

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.cluster import HDBSCAN
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m11_m2_cluster import (ARI_FAIL, CAT_FEATS, NUM_FEATS, SRC, gower,
                            name_clusters, prepare, profile, score)

OUT = os.path.join(C.PROC, "design_clusters_ae.parquet")
SEED = 42

BASE = {"latent": 8, "hidden": 64, "epochs": 500, "lr": 1e-3,
        "dropout": 0.1, "denoising": 0.1}
GRID = {
    "latent": [2, 4, 8, 16],
    "hidden": [32, 64, 128],
    "epochs": [200, 500, 1000],
    "lr": [3e-4, 1e-3, 3e-3],
    "dropout": [0.0, 0.1, 0.3],
    "denoising": [0.0, 0.1, 0.2],
}
# M11 이 고른 HDBSCAN 설정과 같게 둔다. 군집기까지 바뀌면 무엇이 성능을 바꿨는지
# 알 수 없다 — 여기서 바뀌는 것은 '거리 공간'뿐이어야 한다.
HDB = {"min_cluster_size": 15, "min_samples": None, "cluster_selection_method": "eom"}


def encode_matrix(t):
    """AE 입력. 범주는 one-hot, 수치는 표준화, 결측은 지시자로 분리한다.

    Gower 와 달리 신경망은 결측을 분모에서 뺄 수 없다. 중앙값으로 채우되
    채웠다는 사실을 별도 축으로 남긴다 — 안 그러면 미기재 행이 '평범한 사업'이 된다.
    """
    parts, names = [], []
    for f in NUM_FEATS:
        v = t[f].astype(float)
        parts.append(v.fillna(v.median()).to_numpy())
        names.append(f)
        parts.append(v.isna().astype(float).to_numpy())
        names.append(f + "__missing")
    X = np.column_stack(parts)
    X = StandardScaler().fit_transform(X)

    oh = pd.get_dummies(t[CAT_FEATS].astype(str), columns=CAT_FEATS, dtype=float)
    names += list(oh.columns)
    return np.column_stack([X, oh.to_numpy()]), names


class AE(nn.Module):
    def __init__(self, n_in, hidden, latent, dropout):
        super().__init__()
        self.enc = nn.Sequential(
            nn.Linear(n_in, hidden), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(hidden, hidden // 2), nn.ReLU(),
            nn.Linear(hidden // 2, latent))
        self.dec = nn.Sequential(
            nn.Linear(latent, hidden // 2), nn.ReLU(),
            nn.Linear(hidden // 2, hidden), nn.ReLU(),
            nn.Linear(hidden, n_in))

    def forward(self, x):
        z = self.enc(x)
        return z, self.dec(z)


def latent_of(X, cfg, seed=SEED):
    torch.manual_seed(seed)
    x = torch.tensor(X, dtype=torch.float32)
    m = AE(x.shape[1], cfg["hidden"], cfg["latent"], cfg["dropout"])
    opt = torch.optim.Adam(m.parameters(), lr=cfg["lr"])
    m.train()
    for _ in range(cfg["epochs"]):
        opt.zero_grad()
        inp = x
        if cfg["denoising"] > 0:
            # 잡음을 넣어 복원시키면 잠재공간이 개별 행을 외우는 대신 구조를 잡는다.
            inp = x + torch.randn_like(x) * cfg["denoising"]
        _, rec = m(inp)
        ((rec - x) ** 2).mean().backward()
        opt.step()
    m.eval()
    with torch.no_grad():
        z, rec = m(x)
    return z.numpy(), float(((rec - x) ** 2).mean())


def run(X, D, y_type, cfg):
    """AE 잠재공간에서 군집하고, 점수는 M11 과 같은 Gower 거리에서 잰다."""
    z, recon = latent_of(X, cfg)
    labels = HDBSCAN(metric="euclidean", **HDB).fit_predict(z)
    r = score(D, z, labels, y_type)
    r["recon_mse"] = round(recon, 5)
    return r, labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()

    t = prepare(pd.read_parquet(SRC))
    y_type = t["support_type"].to_numpy()
    X, names = encode_matrix(t)
    # 점수용 자 — M11 의 B 조건(지원성격 제외)과 정확히 같은 거리행렬
    D = gower(t, CAT_FEATS, NUM_FEATS)
    print("모델 2 DL 대상: %d행 / AE 입력 %d축" % (len(t), X.shape[1]))
    print("기준선(M11 Gower+HDBSCAN): 군집 20 / 노이즈 41.1% / 실루엣 0.6012 / ARI 0.2245")

    t0 = time.time()
    base, base_lab = run(X, D, y_type, BASE)
    print("\n== 기준 설정 (튜닝 전)")
    print("  %s" % BASE)
    print("  군집 %d / 노이즈 %.1f%% / 실루엣 %s / ARI %s / 복원MSE %s"
          % (base["n_clusters"], base["noise_ratio"] * 100, base["silhouette"],
             base["ari_support_type"], base["recon_mse"]))

    grid = GRID if not a.smoke else {"latent": [4, 8], "epochs": [200, 500]}
    print("\n== Ablation (one-factor-at-a-time)")
    abl, best_cfg = {}, dict(BASE)
    for axis, values in grid.items():
        abl[axis] = {}
        for v in values:
            abl[axis][str(v)] = (dict(base, note="기준 설정") if v == BASE[axis]
                                 else run(X, D, y_type, dict(BASE, **{axis: v}))[0])
        # 선택 기준: 설계서 실패조건을 통과한 것 중 실루엣 최대.
        # 실루엣만 보면 ARI 가 높은(= 지원성격 복제) 설정을 고를 수 있다.
        ok = {k: v for k, v in abl[axis].items()
              if v["silhouette"] is not None and v["n_ge30"] >= 2
              and (v["ari_support_type"] or 0) < ARI_FAIL and v["noise_ratio"] < 0.6}
        pool = ok or {k: v for k, v in abl[axis].items() if v["silhouette"] is not None}
        best_v = max(pool, key=lambda k: pool[k]["silhouette"])
        best_cfg[axis] = float(best_v) if "." in best_v else int(best_v)
        print("  %-10s %s  -> 최적 %s"
              % (axis, "  ".join("%s:(실루엣 %s, ARI %s)"
                                 % (k, v["silhouette"], v["ari_support_type"])
                                 for k, v in abl[axis].items()), best_v))

    print("\n== 축별 최적값 조합")
    print("  %s" % best_cfg)
    tuned, labels = run(X, D, y_type, best_cfg)
    print("  군집 %d / 노이즈 %.1f%% / ≥30군집 %d / 실루엣 %s / DBI %s / ARI %s"
          % (tuned["n_clusters"], tuned["noise_ratio"] * 100, tuned["n_ge30"],
             tuned["silhouette"], tuned["davies_bouldin"], tuned["ari_support_type"]))

    prof = profile(t, labels, NUM_FEATS, CAT_FEATS)
    tags, _ = name_clusters(prof)
    for c, p in prof.items():
        p["tag"] = tags[c]

    ml = {"n_clusters": 20, "noise_ratio": 0.4105, "silhouette": 0.6012,
          "ari_support_type": 0.2245, "n_ge30": 12}
    verdict = judge(tuned, ml)
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    t = t.assign(cluster_ae=labels,
                 cluster_ae_tag=[tags.get(int(l)) if l >= 0 else None for l in labels])
    t[["row_id", "title", "support_type", "support_method", "cluster_ae",
       "cluster_ae_tag"]].to_parquet(OUT, index=False)
    print("[data] %s" % OUT)

    C.save_report("dl08_m2_cluster.json", {
        "n_rows": int(len(t)), "n_input_axes": int(X.shape[1]),
        "hdbscan": HDB, "scoring": "군집은 AE 잠재공간, 점수는 M11 과 같은 Gower 거리",
        "base_config": BASE, "base_result": base, "ablation": abl,
        "tuned_config": best_cfg, "tuned_result": tuned,
        "ml_reference": ml, "profile": prof, "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2),
    })


def judge(dl, ml):
    reasons, v = [], "미채택"
    ari = dl["ari_support_type"] or 0
    if ari >= ARI_FAIL:
        reasons.append("군집이 지원성격을 복제한다 (ARI %.3f) — 설계서 실패 조건" % ari)
        return {"verdict": "No-Go", "reasons": reasons}
    reasons.append("지원성격 복제 아님 (ARI %.3f)" % ari)

    ds, ms = dl["silhouette"], ml["silhouette"]
    if ds is not None and ds > ms:
        reasons.append("실루엣 %.4f > ML %.4f — 같은 Gower 자로 재도 AE 잠재공간이 낫다"
                       % (ds, ms))
        v = "채택"
    else:
        reasons.append("실루엣 %s <= ML %.4f — AE 잠재공간이 Gower 거리보다 낫지 않다"
                       % (ds, ms))
    reasons.append("노이즈 %.1f%% (ML %.1f%%) / ≥30군집 %d개 (ML %d개)"
                   % (dl["noise_ratio"] * 100, ml["noise_ratio"] * 100,
                      dl["n_ge30"], ml["n_ge30"]))
    if dl["noise_ratio"] > 0.6 or dl["n_ge30"] < 2:
        v = "미채택"
        reasons.append("노이즈 과다 또는 독립 유형 부족 — 설계유형으로 쓸 수 없다")
    return {"verdict": v, "reasons": reasons}


if __name__ == "__main__":
    main()
