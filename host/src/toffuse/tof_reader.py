"""ESP32 시리얼 수신.

핵심은 **drain-to-latest**: 수신 버퍼에 쌓인 줄을 전부 읽고 마지막 유효
프레임만 돌려준다. 그리기가 수신(15 Hz)보다 느려도 지연이 누적되지 않는다.
구 뷰어(``tof_viewer.py:43``)에서 검증된 방식을 그대로 가져왔다.

Phase 0 은 단일 루프이므로 스레드가 없다.
``ponytail:`` Phase 2 에서 이 함수를 스레드로 감싸고 ping 루프를 붙인다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import serial
from serial.tools import list_ports

from .protocol import Pong, Status, ToFFrame, parse_line

DEFAULT_BAUD = 115200          # 구 펌웨어. v2 펌웨어는 921600.
_READ_TIMEOUT_S = 0.05         # 잘린 줄에 걸려도 루프가 멎지 않을 만큼 짧게


class SerialLike(Protocol):
    """``drain_latest`` 가 실제로 쓰는 것만.

    ``serial.Serial`` 전체를 요구할 이유가 없다. 이 두 개로 좁혀 두면
    테스트에서 가짜 스트림을 넣을 수 있고, 나중에 소켓이나 파일 재생으로
    바꿔 끼울 때도 이 함수는 그대로다.
    """

    @property
    def in_waiting(self) -> int: ...

    def readline(self) -> bytes: ...


@dataclass(frozen=True)
class Drained:
    """한 번의 배수 결과."""

    frame: ToFFrame | None          # 마지막 유효 프레임
    pong: Pong | None               # 마지막 pong
    statuses: tuple[Status, ...]    # 순서대로 모인 '#' 줄
    dropped: int                    # 최신 것에 밀려 버려진 프레임 수


def find_port() -> str | None:
    """USB 시리얼 포트를 추측한다.

    ``vid`` 가 있는 포트만 본다. 메인보드 내장 COM1/COM2 나 블루투스 가상
    포트는 vid 가 없어 자연히 걸러진다.
    """
    usb = [p.device for p in list_ports.comports() if p.vid is not None]
    return usb[0] if usb else None


def list_usb_ports() -> list[str]:
    """사용자에게 보여줄 후보 목록."""
    return [
        f"{p.device} (vid={p.vid:04x} pid={p.pid:04x}) {p.description}"
        for p in list_ports.comports()
        if p.vid is not None
    ]


def open_serial(port: str, baud: int = DEFAULT_BAUD) -> serial.Serial:
    return serial.Serial(port, baud, timeout=_READ_TIMEOUT_S)


def drain_latest(ser: SerialLike) -> Drained:
    """버퍼에 있는 줄을 전부 소비하고 가장 최신 프레임만 남긴다.

    ``in_waiting`` 이 0 이 될 때까지만 읽으므로 새 데이터를 기다리며 막히지
    않는다. 깨진 줄은 ``parse_line`` 이 None 을 주므로 그냥 넘어간다.
    """
    frame: ToFFrame | None = None
    pong: Pong | None = None
    statuses: list[Status] = []
    dropped = 0

    while ser.in_waiting:
        raw = ser.readline()
        if not raw:
            break
        item = parse_line(raw.decode("utf-8", errors="ignore"))
        if isinstance(item, ToFFrame):
            if frame is not None:
                dropped += 1
            frame = item
        elif isinstance(item, Pong):
            pong = item
        elif isinstance(item, Status):
            statuses.append(item)

    return Drained(frame=frame, pong=pong, statuses=tuple(statuses), dropped=dropped)
