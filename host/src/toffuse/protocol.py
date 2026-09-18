"""ESP32 ToF 시리얼 프로토콜 파서.

시리얼은 신뢰 경계다. 줄이 잘리거나 깨져 들어오는 것이 정상이므로,
파싱 실패는 예외가 아니라 ``None`` 으로 조용히 흘려보내고 다음 줄을 기다린다.

두 가지 프레임 포맷을 **필드 개수로 자동 판별**한다. 덕분에 펌웨어를
고치기 전에도 같은 호스트 코드가 그대로 돌아간다.

구 포맷(v1, ``test_0902`` 원본 펌웨어)::

    F,<d0>,...,<d63>                          # 64 필드, 무효 셀은 -1

신 포맷(v2, 본 프로젝트)::

    F,<t_us>,<seq>,<d0..d63>,<s0..s63>        # 2 + 128 필드
    F,<t_us>,<seq>,<d0..>,<s0..>,<d1..>,<s1..>  # 2 + 256 (NB_TARGET_PER_ZONE=2)

``t_us`` 는 ``esp_timer_get_time()`` (부팅 후 µs, int64 — 롤오버 없음),
``s`` 는 ``target_status`` **원본**이다. 구 펌웨어처럼 호스트에 도착하기
전에 뭉개지 않으므로 임계값을 런타임에 조절할 수 있다.

그 밖의 줄::

    P,<t_us>    클럭 동기화 pong
    #...        상태 메시지
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

GRID = 8
ZONES = GRID * GRID

#: ST ULD 의 target_status 중 거리값을 신뢰할 수 있는 값.
#: 5 = 100% 유효, 6·9 = 50% 신뢰(거리는 맞지만 신호가 약함).
#: 빔스플리터를 거친 매크로 거리에서는 6·9 가 대량 발생하므로 기본값에 포함한다.
#: 엄격하게 보려면 ``accept=(5,)`` 로 좁힌다.
DEFAULT_STATUS_ACCEPT: tuple[int, ...] = (5, 6, 9)

_V1_FIELDS = ZONES            # F 를 뗀 나머지
_V2_HEADER = 2                # t_us, seq
_V2_PER_TARGET = ZONES * 2    # distance + status


def _freeze(a: NDArray[np.generic]) -> None:
    """값 객체가 밖에서 수정되지 않도록 잠근다."""
    a.flags.writeable = False


@dataclass(frozen=True)
class ToFFrame:
    """ToF 한 프레임. 거리·상태는 모두 ``(n_target, 8, 8)`` 로 통일한다.

    형상을 타깃 수와 무관하게 고정해 두면 소비자 쪽에 분기가 생기지 않는다.
    zone 인덱스 규약은 펌웨어와 동일한 ``index = y*8 + x`` (row-major).
    """

    distance_mm: NDArray[np.float64]      # (n_target, 8, 8) 원본 mm
    status: NDArray[np.int16] | None      # (n_target, 8, 8), v1 이면 None
    t_us: int | None                      # 장치 클럭 µs, v1 이면 None
    seq: int | None

    def __post_init__(self) -> None:
        if self.distance_mm.ndim != 3 or self.distance_mm.shape[1:] != (GRID, GRID):
            raise ValueError(f"distance_mm 형상이 (n,8,8) 이 아님: {self.distance_mm.shape}")
        if self.status is not None and self.status.shape != self.distance_mm.shape:
            raise ValueError(
                f"status 형상 불일치: {self.status.shape} != {self.distance_mm.shape}"
            )

    @property
    def n_target(self) -> int:
        return int(self.distance_mm.shape[0])

    def depth(self, target: int = 0) -> NDArray[np.float64]:
        """필터링하지 않은 원본 거리 (8, 8)."""
        if not 0 <= target < self.n_target:
            raise IndexError(f"target {target} 없음 (n_target={self.n_target})")
        out: NDArray[np.float64] = self.distance_mm[target]
        return out

    def fliplr(self) -> ToFFrame:
        """좌우를 뒤집은 새 프레임. 센서 장착 방향 보정용.

        거리와 status 를 **함께** 뒤집는다. 하나만 뒤집으면 유효성 마스크가
        어긋나 멀쩡한 zone 이 무효로 표시된다.

        ponytail: 이 보정은 광학 구성에 딸린 값이다. 빔스플리터 반사가
        손대칭을 한 번 더 뒤집으므로 BS 장착 후 반드시 다시 확인할 것.
        Phase 3 의 호모그래피가 붙으면 이 플립까지 흡수하므로, 그때는
        기본값을 끄고 H 하나로 통일하는 편이 낫다.
        """
        dist = np.ascontiguousarray(self.distance_mm[:, :, ::-1])
        _freeze(dist)
        stat: NDArray[np.int16] | None = None
        if self.status is not None:
            stat = np.ascontiguousarray(self.status[:, :, ::-1])
            _freeze(stat)
        return ToFFrame(distance_mm=dist, status=stat, t_us=self.t_us, seq=self.seq)

    def valid_mask(
        self, target: int = 0, accept: Sequence[int] = DEFAULT_STATUS_ACCEPT
    ) -> NDArray[np.bool_]:
        """신뢰할 수 있는 zone 마스크 (8, 8).

        음수 거리는 status 와 무관하게 무효로 본다. xtalk 가 심하면 ST 가
        오프셋 보정 결과로 작은 음수를 내보내는데, 물리적으로 쓸 수 없다.
        """
        d = self.depth(target)
        ok: NDArray[np.bool_] = d >= 0
        if self.status is not None:
            ok &= np.isin(self.status[target], np.asarray(accept, dtype=np.int16))
        return ok

    def masked_depth(
        self, target: int = 0, accept: Sequence[int] = DEFAULT_STATUS_ACCEPT
    ) -> NDArray[np.float64]:
        """무효 zone 을 NaN 으로 바꾼 거리 (8, 8)."""
        return np.where(self.valid_mask(target, accept), self.depth(target), np.nan)

    def valid_count(
        self, target: int = 0, accept: Sequence[int] = DEFAULT_STATUS_ACCEPT
    ) -> int:
        return int(np.count_nonzero(self.valid_mask(target, accept)))


@dataclass(frozen=True)
class Pong:
    """``?`` 에 대한 응답. 장치 클럭 µs."""

    t_us: int


@dataclass(frozen=True)
class Status:
    """``#`` 으로 시작하는 상태 메시지. 원문 그대로 보존한다."""

    text: str


def _ints(parts: list[str]) -> NDArray[np.int64] | None:
    """정수 리스트로 변환. 하나라도 정수가 아니면 None."""
    try:
        return np.fromiter((int(p) for p in parts), dtype=np.int64, count=len(parts))
    except (ValueError, TypeError):
        return None


def _build(values: NDArray[np.int64], n_target: int, t_us: int | None, seq: int | None) -> ToFFrame:
    """v2 페이로드(d,s 가 타깃별로 묶인 순서)를 프레임으로 조립."""
    blocks = values.reshape(n_target, 2, ZONES)
    dist = blocks[:, 0, :].astype(np.float64).reshape(n_target, GRID, GRID)
    stat = blocks[:, 1, :].astype(np.int16).reshape(n_target, GRID, GRID)
    _freeze(dist)
    _freeze(stat)
    return ToFFrame(distance_mm=dist, status=stat, t_us=t_us, seq=seq)


def _parse_tof(rest: str) -> ToFFrame | None:
    parts = rest.split(",")
    n = len(parts)

    # v1: 거리 64개뿐. 상태도 타임스탬프도 없다.
    if n == _V1_FIELDS:
        values = _ints(parts)
        if values is None:
            return None
        dist = values.astype(np.float64).reshape(1, GRID, GRID)
        _freeze(dist)
        return ToFFrame(distance_mm=dist, status=None, t_us=None, seq=None)

    # v2: 헤더 2개 + 타깃당 128개.
    payload = n - _V2_HEADER
    if payload <= 0 or payload % _V2_PER_TARGET != 0:
        return None
    try:
        t_us = int(parts[0])
        seq = int(parts[1])
    except ValueError:
        return None
    values = _ints(parts[_V2_HEADER:])
    if values is None:
        return None
    return _build(values, payload // _V2_PER_TARGET, t_us, seq)


def parse_line(line: str) -> ToFFrame | Pong | Status | None:
    """시리얼 한 줄을 해석한다. 해석할 수 없으면 ``None``.

    호출자는 ``isinstance`` 로 분기한다. 예외를 던지지 않으므로 수신 루프에
    try/except 를 두를 필요가 없다.
    """
    line = line.strip()
    if not line:
        return None
    if line.startswith("#"):
        return Status(text=line)
    if line.startswith("F,"):
        return _parse_tof(line[2:])
    if line.startswith("P,"):
        try:
            return Pong(t_us=int(line[2:]))
        except ValueError:
            return None
    return None
