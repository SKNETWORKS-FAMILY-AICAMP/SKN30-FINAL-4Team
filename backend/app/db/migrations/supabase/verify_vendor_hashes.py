"""VENDOR.md에 기록한 팀원 마이그레이션 해시를 다시 계산해 로컬 수정 여부를 확인한다.

`packages/verify_vendor_hashes.py` 와 같은 목적·같은 모양이다. 대상이 디렉터리 트리가
아니라 파일 9개라 파일별 해시까지 함께 본다. 우리가 더한 `000_`/`100_` 은 벤더링 대상이
아니므로 검사하지 않는다.
"""

import hashlib
import pathlib

EXPECTED = {
    "01_core_schemas.sql": "fd0b0a84d6f49af9c424bba6eef26da5e6d433845d4efbc2f298a037d22ae13c",
    "02_core_ddl.sql": "c2fc3749f5dfd2854254e1a55968a25e0c2ccb6ae2374ae7ddaa4cb932ad879d",
    "03_workspace_ddl.sql": "ba3f2609947c060970d922696d9db33bb86e0ed225d403e12e0a05e9d8ffd2b8",
    "04_workspace_components.sql": "9b99fca9c37a16b13bb5643d09a92d78b7d5fae882a821a65dcfb932b9ac3d40",
    "05_workspace_projections.sql": "52257783820e7f9b9a0d2450fc25d67d4eb251e245bb0d87ad23871262eddb41",
    "06_result_ddl.sql": "1a22f4047ce1debfa8fdbcbd6d8ac7531afb188988c90d4575fd5b79815a716b",
    "07_indexes.sql": "74ff099d71b39e5bc918e9889325bd1d08691bb940e94ae6f6dbc190268de749",
    "08_rls_policies.sql": "a5aa5a91a2c676041371a8cde482e42549ec45248852e3a6d11ed8ed96825a74",
    "09_kb_notice_metadata.sql": "e6aecce4e9802da81c1f262affc1b01515e1ae3a4383636f88a564b3c792c087",
}

EXPECTED_TREE = "ffd5947d1c29cb1e0d702db0c10a9236887fed75bad7a7fd739ed48cd01726d8"


def main() -> int:
    base = pathlib.Path(__file__).parent
    tree = hashlib.sha256()
    failed = False
    for name, expected in EXPECTED.items():
        path = base / name
        if not path.is_file():
            print(f"MISSING  {name}")
            failed = True
            continue
        payload = path.read_bytes()
        tree.update(name.encode())
        tree.update(payload)
        actual = hashlib.sha256(payload).hexdigest()
        failed |= actual != expected
        status = "OK" if actual == expected else "CHANGED"
        print(f"{status:8} {name} bytes={len(payload)} sha256={actual}")

    actual_tree = tree.hexdigest()
    failed |= actual_tree != EXPECTED_TREE
    print(f"{'OK' if actual_tree == EXPECTED_TREE else 'CHANGED':8} tree sha256={actual_tree}")

    if failed:
        print("\n로컬 수정이 있다. VENDOR.md의 '로컬 수정 금지' 절과 해시를 갱신한다.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
