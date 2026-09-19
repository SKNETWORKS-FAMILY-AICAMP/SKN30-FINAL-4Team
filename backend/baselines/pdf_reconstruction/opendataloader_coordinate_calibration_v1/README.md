# OpenDataLoader 좌표 캘리브레이션 baseline v1

이 디렉터리는 실제 공고나 개인정보가 아닌 합성 PDF 7건의 machine-bound 평가 기록이다.
Common IR·Profile·selector 입력으로 사용하지 않는다.

## 포함 파일

- `fixture_suite.json`: 합성 case, source/render/coordinate hash, anchor 정답
- `cases/*`: 합성 PDF, canonical PDFium render, render manifest
- `odl-run-1/*`, `odl-run-2/*`: OpenDataLoader 독립 반복 실행 원본 JSON과
  source/command/JAR/JRE/output hash를 묶은 `run_manifest.json`
- `calibration_proof.json`: 두 run의 결정성 및 좌표 가설별 오차와 최종 판정

`calibration_proof.json`의 디스크 byte SHA-256은
`b7b7a7e2556d7f907d655ac37d5b50d70e9fbfdd519d8516b6e9e2df86de1eca`다. 좌표 변환
API가 고정하는 값은 JSON 공백과 key 순서에 영향받지 않는 canonical proof SHA-256
`f8c041e14cad1637150165050dc12ff87ef9a752d8192e46e73bda12edf003be`다. 두 값을 서로
대체해서 사용하지 않는다. canonical 값은 `ensure_ascii=False`, `sort_keys=True`,
`separators=(",", ":")`, `allow_nan=False` 직렬화 byte를 해시한 값이다. 디스크 byte SHA는
파일 전송·보관 무결성 확인용일 뿐 좌표 projection 승인값이 아니다.

## 실행 identity

- OpenDataLoader: `2.5.7`
- JAR SHA-256: `74f0d797bea8088bd4a58137e372eb38a5fa24639e06b633cef7f78eba14cd62`
- mode: local Java, `--hybrid off --format json` (OCR 미사용)
- Java: OpenJDK `17.0.20`
- 정확한 JAR/package metadata/Java executable/config hash는 `calibration_proof.json`과
  각 run manifest에 기록한다. JRE `.deb`는 executable과의 package ownership을 주장하지
  않고, 재현 시 제공된 package 파일 hash로만 기록한다.

## 결론

유효한 leaf `/Page`의 `/UserUnit`을 사용하는 7개 case, 35개 anchor와 두 번의
byte-identical replay가 모두 gate를 통과했다. 중심점뿐 아니라 bbox 네 변, IoU,
물리 point 오차와 anchor 중복 상한을 함께 검사했다. 단일로 남은 좌표 convention은 다음과
같다.

```text
rotated_crop_relative_bottom_left_raw_units
```

ODL bbox는 CropBox 원점 기준이고 `/Rotate`가 적용됐으며 y축은 bottom-left다. bbox 숫자에는
`/UserUnit`이 곱해지지 않는다. 후속 alignment는 canonical coordinate manifest를 사용해 이
좌표를 raw PDF user space로 역변환해야 한다.

이 baseline은 좌표 convention을 증명할 뿐 ODL 문자열을 원문 evidence로 승격하지 않는다.
또한 동일 길이 Courier paragraph anchor를 대상으로 한 calibration이므로 table/cell/image 등
모든 ODL node type의 좌표까지 검증했다고 확대 해석하지 않는다.
기존 `opendataloader_artifact/v1`의 좌표 상태도 계속
`odl_pdf_points_unverified`다. 검토된 후속 계약 전에는 production alignment key로 사용할 수
없다.
