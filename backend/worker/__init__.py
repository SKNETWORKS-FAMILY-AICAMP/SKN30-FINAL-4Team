"""분석 워커. import 만으로 vendored 패키지 경로가 준비된다."""

from . import vendor as vendor  # noqa: F401  # sys.path 부작용이 목적이다
