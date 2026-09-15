#!/usr/bin/env bash
# E2E용 컨테이너를 지운다. 데이터도 함께 사라진다.
set -euo pipefail
docker rm -f sims-e2e-pg 2>/dev/null && echo "제거됨" || echo "실행 중인 컨테이너 없음"
