"""Model 3 serving — 가이드 구조상의 진입점 이름.

실제 구현은 같은 폴더의 `inference.py` 다. 기존 호출부를 깨지 않으려고 이름만
얹는다.

**`from inference import *` 로 하면 안 된다.** model1/model2/model3 폴더에
`inference.py` 가 각각 있어서, 여러 모델을 한 프로세스에서 쓰면
`sys.modules["inference"]` 캐시 때문에 **다른 모델의 구현**이 잡힌다(실제로
API 스모크에서 모델 1 자리에 모델 3 이 불렸다). 그래서 파일 경로로 고유
이름을 붙여 불러온다.
"""
import importlib.util as _ilu
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

_NAME = "model3_inference"
if _NAME in _sys.modules:
    _impl = _sys.modules[_NAME]
else:
    _spec = _ilu.spec_from_file_location(_NAME, _os.path.join(_HERE, "inference.py"))
    _impl = _ilu.module_from_spec(_spec)
    _sys.modules[_NAME] = _impl
    _spec.loader.exec_module(_impl)

for _k in dir(_impl):
    if not _k.startswith("_"):
        globals()[_k] = getattr(_impl, _k)
