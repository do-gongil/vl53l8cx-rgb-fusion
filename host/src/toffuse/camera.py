"""UVC 카메라 열기 — 백엔드 폴백과 실제 설정값 보고.

Windows 에서 ``cv2.VideoCapture`` 는 백엔드에 따라 동작이 꽤 다르다.
``CAP_DSHOW`` 가 MJPG 협상과 지연 면에서 대체로 낫고, 안 되면 ``CAP_MSMF``
로 떨어진다. 요청한 해상도가 받아들여졌는지는 **열고 나서 되읽어 확인**한다.
요청과 실제가 다른 경우가 흔하고, 그 차이가 곧 FoV 차이(계획서 R3)다.

Phase 0 은 단일 루프로 충분하므로 스레드를 두지 않는다.
``ponytail:`` 카메라 read 가 ToF 수신을 굶기면 Phase 2 에서 grab 스레드로 교체.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

# cv2.VideoWriter_fourcc 는 구형 별칭. 5.x 에서는 이쪽이 표준형이다.
MJPG = cv2.VideoWriter.fourcc(*"MJPG")

_WINDOWS_BACKENDS = ((cv2.CAP_DSHOW, "DSHOW"), (cv2.CAP_MSMF, "MSMF"))
_OTHER_BACKENDS = ((cv2.CAP_ANY, "ANY"),)


def _backends() -> tuple[tuple[int, str], ...]:
    return _WINDOWS_BACKENDS if sys.platform == "win32" else _OTHER_BACKENDS


def _fourcc_str(value: float) -> str:
    n = int(value)
    if n <= 0:
        return "?"
    return "".join(chr((n >> (8 * i)) & 0xFF) for i in range(4))


@dataclass(frozen=True)
class CameraInfo:
    """실제로 열린 카메라가 보고하는 값 — 요청값이 아니다."""

    index: int
    backend: str
    width: int
    height: int
    fps: float
    fourcc: str

    def describe(self) -> str:
        return (
            f"cam{self.index} {self.backend} {self.width}x{self.height} "
            f"@{self.fps:.0f} {self.fourcc}"
        )


def _configure(cap: cv2.VideoCapture, width: int | None, height: int | None,
               fps: float | None) -> None:
    # MJPG 를 먼저 요청해야 고해상도에서 프레임레이트가 나온다(YUY2 는 대역폭 부족).
    cap.set(cv2.CAP_PROP_FOURCC, MJPG)
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if fps:
        cap.set(cv2.CAP_PROP_FPS, fps)
    # 최신 프레임만 보고 싶다. 버퍼가 쌓이면 지연이 누적된다.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)


def _probe(index: int, backend: int, name: str, width: int | None, height: int | None,
           fps: float | None) -> tuple[cv2.VideoCapture, CameraInfo] | None:
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return None
    _configure(cap, width, height, fps)
    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        return None
    # 되읽기가 0 을 주는 백엔드가 있어 실제 프레임 형상을 우선한다.
    h, w = frame.shape[:2]
    info = CameraInfo(
        index=index,
        backend=name,
        width=w,
        height=h,
        fps=float(cap.get(cv2.CAP_PROP_FPS)),
        fourcc=_fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)),
    )
    return cap, info


def open_camera(
    index: int | None = None,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    max_scan: int = 6,
) -> tuple[cv2.VideoCapture, CameraInfo] | None:
    """카메라를 연다. ``index`` 가 None 이면 0..max_scan-1 을 훑는다.

    찾지 못하면 ``None``. 카메라가 없다고 프로그램을 죽이지 않는 것은
    의도된 동작이다 — 한쪽 장치만 꽂힌 상태로도 점검할 수 있어야 한다.
    """
    indices = [index] if index is not None else list(range(max_scan))
    for idx in indices:
        for backend, name in _backends():
            found = _probe(idx, backend, name, width, height, fps)
            if found is not None:
                return found
    return None


def read_frame(cap: cv2.VideoCapture) -> NDArray[np.uint8] | None:
    """프레임 하나. 실패하면 None (USB 가 잠깐 끊겨도 루프는 살아 있어야 한다)."""
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    return cast(NDArray[np.uint8], frame)
