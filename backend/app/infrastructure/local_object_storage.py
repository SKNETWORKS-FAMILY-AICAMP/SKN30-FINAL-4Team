import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

from app.ports.object_storage import StoredObject


class LocalObjectStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path_for(self, key: str) -> Path:
        if not key.strip():
            raise ValueError("Storage key must not be blank")

        path = (self._root / key).resolve()
        if path == self._root or not path.is_relative_to(self._root):
            raise ValueError("Storage key escapes storage root")
        return path

    async def put(self, key: str, content: BinaryIO) -> StoredObject:
        write_task = asyncio.create_task(asyncio.to_thread(self._put, key, content))
        try:
            return await asyncio.shield(write_task)
        except asyncio.CancelledError:
            try:
                await write_task
            finally:
                raise

    def _put(self, key: str, content: BinaryIO) -> StoredObject:
        target = self._path_for(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                dir=target.parent,
                prefix=".upload-",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                content.seek(0)
                shutil.copyfileobj(content, temporary, length=1024 * 1024)
                temporary.flush()
                os.fsync(temporary.fileno())
                size_bytes = temporary.tell()

            os.link(temporary_path, target)
            temporary_path.unlink()
            temporary_path = None
            return StoredObject(key=key, size_bytes=size_bytes)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def open(self, key: str) -> BinaryIO:
        return await asyncio.to_thread(self._path_for(key).open, "rb")

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path_for(key).unlink, missing_ok=True)
