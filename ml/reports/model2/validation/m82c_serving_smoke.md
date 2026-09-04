# M82-C — P3 End-to-End Serving Smoke

> 질문: **신규 공고문 하나가 masking → proximity → SVD → routing 을 거쳐 금액까지 정상적으로 나오는가?**

## 경로

```text
신규 문서
↓ 본문 추출 (기존 F06, 이 실험 범위 밖)
↓ masking ([AMOUNT]/#)
↓ proximity regex
↓ explicit proximity feature
↓ masked proximity TF-IDF/SVD
↓ structured + title/body feature
↓ M73 ordinal soft routing
최종 금액
```

## 합성 신규 문서 결과

```text
proximity   {'prox_support_rate': 60.0, 'prox_self_burden_rate': 40.0, 'prox_selected_count': 25.0, 'prox_duration_months': 10.0}
예측        7.1169 log10  (약 13,088,319원)
구간확률    Low 0.429 / Mid 0.562 / High 0.009
구간경계    ['20,000,000', '120,000,000']원
```

## 실제 행 대조

```text
행          PBLN_000000000039623
예측        8.8921  (780,029,688원)
실제        9.0000  (1,000,000,000원)
|오차|      0.1079
```

## 점검표

- [x] 1. 신규 문서가 크래시 없이 예측까지 도달
- [x] 2. 합성 문서 proximity 가 실제로 추출됨
- [x] 3. masking 후 문맥에 숫자 잔존 없음
- [x] 4. 합성 예측이 학습 타깃 1~99 분위 안
- [x] 5. 실제 행 예측 오차가 OOF MAE 의 3배 이내(0.3518×3)
- [x] 6. feature 차원 일치
- [x] 7. feature 순서 일치
- [x] 8. 구간확률이 정상 분포(합 1, 음수 없음)

## 판정

```text
PASS — serving 경로 정상
```
