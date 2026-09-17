#!/usr/bin/env python3
"""Extract only verified Model 1 artifact files into the fixed RunPod path."""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile


EXPECTED_ARCHIVE_SHA256 = "0fca416dfe6910f2fc00764c94d8418dc67dc42c79569feadd036e0cdc0ede41"
EXPECTED_WEIGHT_SHA256 = "8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779"
DESTINATION = Path("/workspace/project/prereview-model1/model1")
MEMBERS = (
    "model1/inference.py",
    "model1/label_mapping.json",
    "model1/model/config.json",
    "model1/model/model.safetensors",
    "model1/tokenizer/tokenizer.json",
    "model1/tokenizer/tokenizer_config.json",
)


def _digest(path: Path) -> str:
    result = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def prepare(archive: Path) -> None:
    archive = archive.expanduser().resolve(strict=True)
    if _digest(archive) != EXPECTED_ARCHIVE_SHA256:
        raise RuntimeError("serving archive SHA-256 mismatch")
    if DESTINATION.exists() or DESTINATION.is_symlink():
        raise RuntimeError("refusing to overwrite Model 1 runtime")
    DESTINATION.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage_parent = Path(tempfile.mkdtemp(prefix=".model1-stage-", dir=DESTINATION.parent))
    stage = stage_parent / "model1"
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in MEMBERS:
                info = bundle.getinfo(member)
                # Reject encrypted, directory, and symlink-ish entries.
                if info.is_dir() or info.flag_bits & 0x1 or stat.S_ISLNK(info.external_attr >> 16):
                    raise RuntimeError("serving archive member is invalid")
                target = stage.joinpath(*Path(member).parts[1:])
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        weight = stage / "model" / "model.safetensors"
        if _digest(weight) != EXPECTED_WEIGHT_SHA256:
            raise RuntimeError("Model 1 weight SHA-256 mismatch")
        for path in [stage, *stage.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        stage.rename(DESTINATION)
    except Exception:
        shutil.rmtree(stage_parent, ignore_errors=True)
        raise
    else:
        stage_parent.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.archive)
    print(f"prepared {DESTINATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
