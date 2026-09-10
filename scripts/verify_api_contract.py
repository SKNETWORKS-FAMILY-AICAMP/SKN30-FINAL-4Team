"""API 명세 JSON 이 실제 서버 계약과 어긋나지 않는지 검사한다.

문서를 자동 생성하지는 않는다. 설명과 예시는 사람이 쓰고, 기계로 확인할 수
있는 사실만 여기서 막는다. 비밀번호 최소 길이가 코드에서 바뀌었는데 문서가
옛 값을 들고 있던 사고를 다시 내지 않기 위한 장치다.

    python scripts/verify_api_contract.py

어긋나면 무엇이 어떻게 다른지 출력하고 종료 코드 1 로 끝난다.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

SPEC_PATH = ROOT / "docs" / "Pre-review_API_명세_현행_v1.0_260831.json"

from annotated_types import MaxLen, MinLen  # noqa: E402

from main import create_app  # noqa: E402

# 명세의 request.body 필드가 어느 요청 모델에서 오는지.
# 모델은 import 경로로 적어 두고 필요할 때만 불러온다.
REQUEST_MODELS = {
    "/api/v1/auth/login": ("app.api.v1.auth", "LoginRequest"),
    "/api/v1/auth/change-password": ("app.api.v1.auth", "ChangePasswordRequest"),
    "/api/v1/auth/password-reset/request":
        ("app.api.v1.password_reset", "PasswordResetRequest"),
    "/api/v1/cases/{case_id}/chat/messages": ("app.schemas.chat", "ChatMessageRequest"),
}

# "이름: 8~128자" 형태의 제약만 기계로 확인한다. 나머지 문장은 사람 몫이다.
LENGTH_RULE = re.compile(r"^(?P<field>[a-z_]+): (?P<lo>\d+)~(?P<hi>\d+)자$")


def load_model(path: str):
    module_name, class_name = REQUEST_MODELS[path]
    module = __import__(module_name, fromlist=[class_name])
    return getattr(module, class_name)


def field_length(model, field: str):
    meta = model.model_fields[field].metadata
    lo = hi = None
    for item in meta:
        if isinstance(item, MinLen):
            lo = item.min_length
        if isinstance(item, MaxLen):
            hi = item.max_length
    return lo, hi


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    live = create_app().openapi()["paths"]
    problems: list[str] = []

    # 1. 명세와 서버의 (메서드, 경로) 집합이 같아야 한다.
    spec_ops = {(a["method"].lower(), a["path"]) for a in spec["apis"]}
    live_ops = {(method, path) for path, ops in live.items() for method in ops}
    for method, path in sorted(spec_ops - live_ops):
        problems.append(f"명세에만 있는 라우트: {method.upper()} {path}")
    for method, path in sorted(live_ops - spec_ops):
        problems.append(f"명세에 빠진 라우트: {method.upper()} {path}")

    for api in spec["apis"]:
        label = f"{api['method']} {api['path']}"
        operation = live.get(api["path"], {}).get(api["method"].lower())
        if operation is None:
            continue
        declared = set(operation["responses"])

        # 2. 성공 상태코드가 실제 라우트의 선언과 같아야 한다.
        success = api["success"]["http_status"]
        declared_success = {int(c) for c in declared if c.startswith("2")}
        if success not in declared_success:
            problems.append(
                f"{label}: 명세 성공 코드 {success}, 서버 선언 {sorted(declared_success)}"
            )

        # 3. 명세가 적은 실패 코드는 서버가 낼 수 있어야 한다.
        for error in api["errors"]:
            code = error["http_status"]
            if str(code) not in declared and api["path"] not in ("/health/ready",):
                problems.append(f"{label}: 실패 코드 {code} 가 서버에 선언되어 있지 않다")

        # 4. "필드: N~M자" 제약이 요청 모델과 같아야 한다.
        if api["path"] not in REQUEST_MODELS:
            continue
        model = load_model(api["path"])
        for line in api["request"]["constraints"]:
            matched = LENGTH_RULE.match(line)
            if matched is None:
                continue
            field = matched["field"]
            if field not in model.model_fields:
                problems.append(f"{label}: 제약에 적힌 {field} 필드가 모델에 없다")
                continue
            spec_lo, spec_hi = int(matched["lo"]), int(matched["hi"])
            real_lo, real_hi = field_length(model, field)
            if (spec_lo, spec_hi) != (real_lo, real_hi):
                problems.append(
                    f"{label}: {field} 길이 제약이 명세 {spec_lo}~{spec_hi}, "
                    f"코드 {real_lo}~{real_hi}"
                )

    if problems:
        print("API 명세가 서버 계약과 어긋난다.\n")
        for problem in problems:
            print(f"  - {problem}")
        print(f"\n{len(problems)}건. {SPEC_PATH.name} 을 고치거나 코드를 확인한다.")
        return 1

    checked = sum(
        1
        for api in spec["apis"]
        for line in api["request"]["constraints"]
        if LENGTH_RULE.match(line)
    )
    print(
        f"OK - 라우트 {len(spec['apis'])}개, 길이 제약 {checked}개가 코드와 일치한다."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
