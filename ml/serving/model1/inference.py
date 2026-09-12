"""Model 1 serving wrapper — 지원성격 분류 (KLUE-BERT, 19클래스 + 판단보류).

가중치는 `ml/pipelines/model1/dl20_m1_final_export.py` 로 학습했다. 설정은 새로
고르지 않았다 — `dl12_m1_candidates.py` 가 내부 CV 로 이미 고른 값 그대로
다(klue/bert-base · lr 5e-5 · epochs 8 · batch 16 · max_len 256 ·
class_weight · seed 42, `ml/reports/dl20_m1_final_export.json` 에 기록).

텍스트 전처리(`clean_text`)와 판단보류 임계값(0.20 / 0.35)은
`dl07_m1_apply.py` 원본 함수를 그대로 import 해서 쓴다 — 재구현하지
않는다("M09 에서 정한 값. 모델이 바뀌어도 같은 기준으로 봐야 비교가
된다").
"""
import json
import os
import sys

import torch

_SERVING_DIR = os.path.dirname(os.path.abspath(__file__))
_ML_ROOT = os.path.abspath(os.path.join(_SERVING_DIR, "..", ".."))
for _d in ("pipelines", "evaluation", "experiments"):
    _base = os.path.join(_ML_ROOT, _d)
    if not os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in os.walk(_base):          # 모델별 하위 폴더까지
        if "__pycache__" in _dp:
            continue
        if _dp not in sys.path:
            sys.path.insert(0, _dp)

from dl07_m1_apply import HOLD_THRESHOLD, TRUST_THRESHOLD, clean_text, tier  # noqa: E402

MODEL_DIR = os.path.join(_SERVING_DIR, "model")
TOKENIZER_DIR = os.path.join(_SERVING_DIR, "tokenizer")
LABEL_MAPPING_PATH = os.path.join(_SERVING_DIR, "label_mapping.json")
# dl20_m1_final_export.py FIXED["max_len"] — dl07 의 384 는 RoBERTa 용 설정이라
# 여기서는 실제 학습에 쓴 256 을 쓴다.
MAX_LEN = 256

_model = None
_tok = None
_classes = None


def _load():
    global _model, _tok, _classes
    if _model is not None:
        return
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    _tok = AutoTokenizer.from_pretrained(TOKENIZER_DIR)
    _model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR)
    _model.eval()
    if torch.cuda.is_available():
        _model = _model.cuda()
    with open(LABEL_MAPPING_PATH, encoding="utf-8") as f:
        _classes = json.load(f)["classes"]


def predict(texts, already_cleaned=False):
    """texts: 원문 문자열의 list.

    기본은 dl07 의 `clean_text()` 전처리(상투구 제거 + 900자 예산)를 그대로
    적용한다. 이미 정제된 텍스트를 넘긴다면 already_cleaned=True 로 건너뛴다.

    반환: dict 의 list (입력과 같은 순서) —
        support_type_pred  19클래스 중 하나
        confidence          softmax 최고확률
        status               tier() 그대로: 판단보류(<0.20) / 참고용(0.20~0.35) / 신뢰(>=0.35)
    """
    _load()
    clean = (list(texts) if already_cleaned
             else [clean_text(t) if t and t.strip() else "" for t in texts])

    enc = _tok(clean, truncation=True, padding=True, max_length=MAX_LEN,
              return_tensors="pt")
    if torch.cuda.is_available():
        enc = {k: v.cuda() for k, v in enc.items()}
    with torch.no_grad():
        proba = torch.softmax(_model(**enc).logits, -1).cpu().numpy()
    conf = proba.max(axis=1)
    pred = [_classes[i] for i in proba.argmax(axis=1)]
    return [
        {"support_type_pred": p, "confidence": float(c), "status": tier(float(c))}
        for p, c in zip(pred, conf)
    ]


if __name__ == "__main__":
    demo = ["2026년 중소기업 판로 지원사업 공고. 사업 개요: 국내외 판로 개척을 위한 "
            "전시회 참가비와 온라인 입점 비용을 지원한다."]
    print(predict(demo))
