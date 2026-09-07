"""팀원 산출물 패키지를 설치 없이 import 가능하게 만든다.

ponytail: editable install 대신 sys.path 삽입이다. ``profile_structuring`` 은
``[tool.uv] package = false`` 라서 설치 대상이 아니고, ``packages/**`` 는
납품본 그대로(해시 고정, `packages/VENDOR.md`)여서 수정할 수 없다.
"""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]

VENDOR_PATHS = (
    REPO_ROOT / "packages" / "common_ir_pipeline" / "src",
    REPO_ROOT / "packages" / "profile_structuring",
)


def install() -> None:
    """중복 삽입 없이 vendored 경로를 ``sys.path`` 앞에 둔다."""

    for path in reversed(VENDOR_PATHS):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)


install()
