# Syntax is intentionally simple so this image can be built by either Docker
# BuildKit or the classic builder.
FROM python:3.12-slim

# Keep tiktoken's verified encoding cache inside the image. Runtime
# parsing/retrieval must not need openaipublic.blob.core.windows.net.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend/vendor/portable_existing_request_profiles_20260831 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

# Keep the repository's ``backend/`` segment in the image path.  Worker
# vendor discovery intentionally resolves its immutable packages relative to
# the repository root, so flattening the build context to /app would make
# /app/backend/vendor unavailable at runtime.
WORKDIR /app/backend

# rhwp-python's native module needs the host FreeType symbols when parsing
# HWP/HWPX.  The worker injects this library into the parser subprocess only;
# keeping it out of global LD_PRELOAD avoids affecting the FastAPI process.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libexpat1 \
        libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

COPY vendor ./vendor
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && mkdir -p "$TIKTOKEN_CACHE_DIR" \
    && python -c "from pathlib import Path; import tiktoken; assert tiktoken.get_encoding('cl100k_base').name == 'cl100k_base'; assert any(Path('/opt/tiktoken-cache').iterdir())"

COPY . ./

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
