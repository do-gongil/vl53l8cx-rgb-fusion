"""Windows 콘솔에서 한글 출력이 깨지지 않게 한다.

Python 은 stdout 이 콘솔이 아니면(파이프·리다이렉트) 로케일 인코딩(ko_KR 은
cp949)을 쓴다. 메시지가 전부 한글이라 이대로면 로그를 남기거나 파이프로
넘길 때 통째로 깨진다. 콘솔일 때는 Python 이 이미 UTF-16 으로 올바르게
쓰므로 건드리지 않는다.
"""
from __future__ import annotations

import sys
from typing import TextIO


def _fix(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    isatty = getattr(stream, "isatty", None)
    if reconfigure is None or (isatty is not None and isatty()):
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass  # 재설정할 수 없는 스트림이면 그냥 둔다.


def setup_stdout() -> None:
    """스크립트 main() 첫 줄에서 호출한다. 부작용을 import 시점에 두지 않기 위해."""
    _fix(sys.stdout)
    _fix(sys.stderr)
