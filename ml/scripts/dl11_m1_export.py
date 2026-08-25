"""DL11 — 모델 1 ML/DL 공정비교용 입력 묶음 내보내기 (로컬 실행).

계획서 5절: "같은 train/validation split · 같은 target · 같은 feature source ·
같은 외부 hold-out · 같은 평가 지표". DL 은 GPU 박스에서 돌아가므로, 그 조건을
로컬에서 한 번 고정해 파일로 만들고 그 파일만 올린다. 파드에서 텍스트를 다시
만들면 ML 과 미세하게 달라질 수 있고, 그러면 비교가 아니라 두 실험이 된다.

내보내는 것
    train.parquet     학습 1,404건 — text / label / group(program_stem)
                      m01_support_type.coarsen() + MIN_SUPPORT=10 을 그대로 통과시킨 것.
                      text 는 text_for_model = title+purpose+content+target_text 로
                      ML(TF-IDF)이 쓰는 문자열과 완전히 같다.
    external.parquet  외부 hold-out 131건 — text / gold / has_doc
                      M29 의 model_inputs(운영조건 B: e01_documents_api.jsonl +
                      clean_text, 없으면 요약문 폴백)과 같은 함수를 호출해 만든다.

주의
    외부 hold-out 은 하이퍼파라미터 선택에 쓰지 않는다. 파드에서도 학습 루프는
    train.parquet 만 본다.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m01_support_type import MIN_SUPPORT, coarsen
from m29_m1_external_eval import DOCS_PROD, build_labelset, model_inputs

OUT = os.path.join(C.PROC, "m1_dl_bundle")
TAX = os.path.join(C.PROC, "business_taxonomy.parquet")


def export_train():
    t = pd.read_parquet(TAX)
    t["label"] = t["middle_category"].map(coarsen)
    sub = t.dropna(subset=["label"])
    vc = sub["label"].value_counts()
    sub = sub[sub["label"].isin(vc[vc >= MIN_SUPPORT].index)].reset_index(drop=True)
    out = pd.DataFrame({
        "text": sub["text_for_model"].fillna("").astype(str),
        "label": sub["label"].astype(str),
        "group": sub["program_stem"].fillna("").astype(str),
        "row_id": sub["row_id"].astype(str),
    })
    return out


def export_external():
    comb, _ = build_labelset()
    ev = comb[(comb["exclude_reason"] == "") & (comb["label_19class"] != "")].copy()
    texts, has_doc = model_inputs(ev["announcement_id"].tolist(), DOCS_PROD, source="api")
    return pd.DataFrame({
        "announcement_id": ev["announcement_id"].values,
        "text": texts,
        "gold": ev["label_19class"].values,
        "has_doc": has_doc,
        "confidence": ev["confidence"].values,
        "batch": ev["batch"].values,
        "title": ev["title"].values,
    })


def main():
    os.makedirs(OUT, exist_ok=True)
    tr = export_train()
    ex = export_external()
    tr.to_parquet(os.path.join(OUT, "train.parquet"), index=False)
    ex.to_parquet(os.path.join(OUT, "external.parquet"), index=False)
    print("train    %d행 / %d클래스 / 그룹 %d"
          % (len(tr), tr["label"].nunique(),
             tr.loc[tr["group"] != "", "group"].nunique() + (tr["group"] == "").sum()))
    print("external %d행 / 원문보유 %d / 클래스 %d"
          % (len(ex), int(ex["has_doc"].sum()), ex["gold"].nunique()))
    print("빈 텍스트 — train %d / external %d"
          % (int((tr["text"].str.len() == 0).sum()), int((ex["text"].str.len() == 0).sum())))
    print("->", OUT)


if __name__ == "__main__":
    main()
