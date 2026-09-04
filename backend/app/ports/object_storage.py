from dataclasses import dataclass
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int


class ObjectStorage(Protocol):
    async def put(self, key: str, content: BinaryIO) -> StoredObject: ...

    async def open(self, key: str) -> BinaryIO: ...

    async def delete(self, key: str) -> None: ...
