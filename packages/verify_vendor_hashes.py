"""VENDOR.md에 기록한 tree sha256을 다시 계산해 로컬 수정 여부를 확인한다."""

import hashlib
import pathlib
import sys

EXPECTED = {
    "common_ir_pipeline": "2c9b06aaf7dde39d017eaef76d1e2343d4f6793bc5841734dbe9c10ade2a311e",
    "profile_structuring": "37ac8d0e7b548ac2d5ac68f55bc781020a6ca6e252de2e5de27e7b31cec8f2fc",
}


def tree_digest(root: pathlib.Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    files = 0
    for path in sorted(root.rglob("*")):
        # __pycache__ 는 편입에서 제외했으므로 재계산에서도 뺀다.
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
        files += 1
    return digest.hexdigest(), files


def main() -> int:
    base = pathlib.Path(__file__).parent
    failed = False
    for name, expected in EXPECTED.items():
        actual, files = tree_digest(base / name)
        status = "OK" if actual == expected else "CHANGED"
        failed |= actual != expected
        print(f"{status:8} {name} files={files} sha256={actual}")
    if failed:
        print("\n로컬 수정이 있다. VENDOR.md의 '로컬 수정 이력'과 해시를 갱신한다.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
