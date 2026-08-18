#!/usr/bin/env bash
# 원격 GPU 박스(RunPod) 재프로비저닝 스크립트.
#
# RunPod 파드를 Stop/Terminate하면 / 아래 설치분(apt, pip)은 사라진다.
# (/workspace 볼륨만 유지) 파드를 다시 켠 뒤 이 파일 한 번만 실행하면 복구된다.
#
#   ssh My-Remote-Server 'bash -s' < ml/scripts/setup_remote.sh
#
# 소요: 약 8~12분
set -e

echo "=== 1/3 LibreOffice + JRE (HWP 변환용) ==="
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  libreoffice-writer libreoffice-calc libreoffice-java-common default-jre-headless \
  unzip curl fonts-nanum > /dev/null
soffice --version | head -1

echo "=== 2/3 H2Orestart (한글 HWP/HWPX 필터) ==="
mkdir -p /workspace/tools && cd /workspace/tools
if [ ! -f H2Orestart.oxt ]; then
  URL=$(curl -sL https://api.github.com/repos/ebandal/H2Orestart/releases/latest \
        | grep -o 'https://[^"]*\.oxt' | head -1)
  curl -sL -o H2Orestart.oxt "$URL"
fi
unopkg add --shared --force H2Orestart.oxt 2>&1 | tail -2 || true
unopkg list --shared 2>/dev/null | grep -i h2orestart | head -1 || true

echo "=== 3/3 Python 패키지 ==="
# RunPod 이미지의 시스템 파이썬에 torch가 이미 있어 venv를 새로 만들면 torch를 다시 받아야 한다.
# 컨테이너는 일회용이므로 시스템에 얹는다(PEP 668 우회).
pip install --break-system-packages --no-input -q \
  PyMuPDF pandas pyarrow scikit-learn lightgbm xgboost catboost \
  statsmodels prophet transformers sentence-transformers datasets accelerate \
  seqeval matplotlib
python3 -c "import torch,fitz;print('torch',torch.__version__,'| CUDA',torch.cuda.is_available(),'| PyMuPDF',fitz.__doc__ or 'ok')"
echo "=== 완료 ==="
