#!/usr/bin/env bash
# 딥러닝 학습용 원격 GPU 박스(RunPod) 프로비저닝.
#
# HWP 처리가 rhwp(로컬)로 해결되면서 LibreOffice는 더 이상 설치하지 않는다.
# 이전 setup_remote.sh 대비 설치 시간이 12분 → 2분으로 줄었다.
#
#   ssh My-Remote-Server 'bash -s' < ml/scripts/setup_remote_dl.sh
#
# 파드를 Stop/Terminate 하면 /usr 아래 pip 설치분이 사라진다(/workspace 볼륨만 유지).
set -e

pip install --break-system-packages --no-input -q \
  transformers datasets accelerate scikit-learn pandas pyarrow seqeval

python3 - <<'PY'
import importlib
for m in ["torch", "transformers", "datasets", "accelerate",
          "sklearn", "pandas", "pyarrow"]:
    try:
        x = importlib.import_module(m)
        print("  OK   %-16s %s" % (m, getattr(x, "__version__", "?")))
    except Exception as e:
        print("  FAIL %-16s %s" % (m, type(e).__name__))
import torch
print("  CUDA:", torch.cuda.is_available(),
      torch.cuda.get_device_name(0) if torch.cuda.is_available() else "")
PY

mkdir -p /workspace/dl/{data,scripts,reports,models}
echo "완료 — 학습 데이터는 scp 로 /workspace/dl/data 에 넣는다"
