from dataclasses import dataclass
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int


def storage_key(bucket: str, object_key: str) -> str:
    """버킷을 키 앞에 붙인다.

    이 포트는 버킷을 모른다 (Supabase Storage 는 알고, 로컬 파일은 모른다).
    버킷별 격리는 키 네임스페이스로 흉내내고, 규칙은 한 곳에만 둔다 —
    업로드가 쓴 키를 보고서 쪽이 다르게 읽으면 파일을 못 찾는다.
    """
    return f"{bucket}/{object_key}"


class ObjectStorage(Protocol):
    async def put(self, key: str, content: BinaryIO) -> StoredObject: ...

    async def open(self, key: str) -> BinaryIO: ...

    async def delete(self, key: str) -> None: ...
