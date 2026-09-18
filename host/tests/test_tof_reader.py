"""drain-to-latest 수신 알고리즘 테스트.

시리얼 포트를 흉내내는 스텁을 쓰지만 목으로 커버리지를 채우는 것이 아니다.
검증 대상은 알고리즘 자체의 계약이다:

  * 버퍼에 프레임이 여러 개 쌓여 있으면 **마지막 것만** 남긴다 (지연 누적 방지)
  * 밀려난 프레임 수를 센다 (표시가 수신을 못 따라가는지 HUD 로 보여야 함)
  * 새 데이터를 기다리며 막히지 않는다
  * 중간부터 읽기 시작해 앞이 잘린 줄이 와도 다음 줄부터 복구한다
"""
from __future__ import annotations

import pytest

from toffuse.protocol import GRID, Pong, Status, ToFFrame
from toffuse.tof_reader import drain_latest


class FakeSerial:
    """미리 채워둔 줄을 순서대로 내주는 최소 시리얼 스텁.

    실제 ``serial.Serial`` 과 계약이 같은 부분만 흉내낸다 -- ``in_waiting``
    은 아직 안 읽은 바이트 수, ``readline`` 은 개행까지의 bytes.
    """

    def __init__(self, lines: list[str]) -> None:
        self._queue = [ln.encode("utf-8") + b"\n" for ln in lines]
        self.read_calls = 0

    @property
    def in_waiting(self) -> int:
        return sum(len(b) for b in self._queue)

    def readline(self) -> bytes:
        self.read_calls += 1
        return self._queue.pop(0) if self._queue else b""


def frame_line(value: int, seq: int | None = None) -> str:
    """모든 zone 이 같은 거리인 식별 가능한 프레임."""
    dist = ",".join([str(value)] * GRID * GRID)
    if seq is None:
        return f"F,{dist}"
    status = ",".join(["5"] * GRID * GRID)
    return f"F,{seq * 1000},{seq},{dist},{status}"


def test_empty_buffer_returns_nothing() -> None:
    d = drain_latest(FakeSerial([]))
    assert d.frame is None and d.pong is None
    assert d.statuses == () and d.dropped == 0


def test_single_frame_passes_through() -> None:
    d = drain_latest(FakeSerial([frame_line(250)]))
    assert isinstance(d.frame, ToFFrame)
    assert d.frame.depth()[0, 0] == 250.0
    assert d.dropped == 0


def test_keeps_only_latest_frame_and_counts_dropped() -> None:
    """지연이 누적되지 않아야 한다 -- 구 뷰어에서 검증된 핵심 동작."""
    d = drain_latest(FakeSerial([frame_line(100), frame_line(200), frame_line(300)]))
    assert isinstance(d.frame, ToFFrame)
    assert d.frame.depth()[0, 0] == 300.0
    assert d.dropped == 2


def test_drains_entire_buffer_in_one_call() -> None:
    ser = FakeSerial([frame_line(i) for i in range(1, 6)])
    drain_latest(ser)
    assert ser.in_waiting == 0


def test_does_not_block_waiting_for_new_data() -> None:
    """in_waiting 이 0 이면 readline 을 아예 부르지 않는다."""
    ser = FakeSerial([])
    drain_latest(ser)
    assert ser.read_calls == 0


def test_status_lines_collected_in_order() -> None:
    d = drain_latest(FakeSerial(["# VL53L8CX start", "# init... OK", frame_line(120)]))
    assert [s.text for s in d.statuses] == ["# VL53L8CX start", "# init... OK"]
    assert all(isinstance(s, Status) for s in d.statuses)
    assert isinstance(d.frame, ToFFrame)


def test_pong_captured_alongside_frame() -> None:
    d = drain_latest(FakeSerial([frame_line(100), "P,555000", frame_line(200)]))
    assert isinstance(d.pong, Pong)
    assert d.pong.t_us == 555_000
    assert isinstance(d.frame, ToFFrame)
    assert d.frame.depth()[0, 0] == 200.0


def test_latest_pong_wins() -> None:
    d = drain_latest(FakeSerial(["P,100", "P,200", "P,300"]))
    assert isinstance(d.pong, Pong)
    assert d.pong.t_us == 300


def test_recovers_after_truncated_first_line() -> None:
    """포트를 중간부터 읽으면 첫 줄은 앞이 잘려 온다. 다음 줄부터 살아나야 한다."""
    truncated = frame_line(999)[40:]
    d = drain_latest(FakeSerial([truncated, frame_line(150)]))
    assert isinstance(d.frame, ToFFrame)
    assert d.frame.depth()[0, 0] == 150.0
    assert d.dropped == 0  # 깨진 줄은 프레임으로 세지 않는다


def test_garbage_between_frames_is_ignored() -> None:
    d = drain_latest(FakeSerial([frame_line(100), "!!!rubbish", "", frame_line(200)]))
    assert isinstance(d.frame, ToFFrame)
    assert d.frame.depth()[0, 0] == 200.0
    assert d.dropped == 1


def test_invalid_utf8_bytes_do_not_raise() -> None:
    """UART 노이즈로 깨진 바이트가 섞여도 예외가 나면 안 된다."""
    ser = FakeSerial([])
    ser._queue = [b"\xff\xfe\x00 garbage\n", frame_line(180).encode() + b"\n"]
    d = drain_latest(ser)
    assert isinstance(d.frame, ToFFrame)
    assert d.frame.depth()[0, 0] == 180.0


def test_mixed_v1_and_v2_frames() -> None:
    """펌웨어를 갈아끼운 직후 두 포맷이 섞여 들어올 수 있다."""
    d = drain_latest(FakeSerial([frame_line(100), frame_line(200, seq=7)]))
    assert isinstance(d.frame, ToFFrame)
    assert d.frame.t_us == 7000
    assert d.frame.seq == 7
    assert d.dropped == 1


@pytest.mark.parametrize("count", [1, 15, 60])
def test_dropped_count_matches_backlog(count: int) -> None:
    d = drain_latest(FakeSerial([frame_line(i) for i in range(count)]))
    assert d.dropped == count - 1
