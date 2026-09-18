"""protocol.py 파서 테스트.

시리얼 스트림은 언제든 잘리거나 깨질 수 있는 신뢰 경계다.
따라서 "정상 파싱"보다 "쓰레기를 조용히 흘려보내는가"를 더 많이 검증한다.
"""
from __future__ import annotations

import numpy as np
import pytest

from toffuse.protocol import (
    DEFAULT_STATUS_ACCEPT,
    GRID,
    Pong,
    Status,
    ToFFrame,
    parse_line,
)

# --- 테스트용 라인 생성기 -------------------------------------------------

def v1_line(dist: list[int]) -> str:
    """구 펌웨어 포맷: F,<d0>,...,<d63>"""
    assert len(dist) == GRID * GRID
    return "F," + ",".join(str(d) for d in dist)


def v2_line(dist: list[int], status: list[int], t_us: int = 1_234_567, seq: int = 42) -> str:
    """신 펌웨어 포맷: F,<t_us>,<seq>,<d0..d63>,<s0..s63>"""
    assert len(dist) == GRID * GRID and len(status) == GRID * GRID
    body = ",".join(str(v) for v in [*dist, *status])
    return f"F,{t_us},{seq},{body}"


def ramp() -> list[int]:
    """0..63 을 mm 값으로 쓴 식별 가능한 패턴."""
    return list(range(GRID * GRID))


# --- v1 (구 펌웨어) -------------------------------------------------------

def test_v1_frame_shape_and_values() -> None:
    f = parse_line(v1_line(ramp()))
    assert isinstance(f, ToFFrame)
    assert f.distance_mm.shape == (1, GRID, GRID)
    assert f.n_target == 1
    assert f.status is None
    assert f.t_us is None
    assert f.seq is None


def test_v1_preserves_row_major_index() -> None:
    """펌웨어 규약: index = y*8 + x. zone 17 은 (row 2, col 1)."""
    f = parse_line(v1_line(ramp()))
    assert isinstance(f, ToFFrame)
    assert f.depth()[2, 1] == 17.0


def test_v1_negative_is_invalid() -> None:
    dist = ramp()
    dist[0] = -1
    dist[63] = -1
    f = parse_line(v1_line(dist))
    assert isinstance(f, ToFFrame)
    d = f.masked_depth()
    assert np.isnan(d[0, 0]) and np.isnan(d[7, 7])
    assert f.valid_count() == GRID * GRID - 2


# --- v2 (신 펌웨어) -------------------------------------------------------

def test_v2_parses_timestamp_and_status() -> None:
    f = parse_line(v2_line(ramp(), [5] * 64, t_us=987_654_321, seq=7))
    assert isinstance(f, ToFFrame)
    assert f.t_us == 987_654_321
    assert f.seq == 7
    assert f.n_target == 1
    assert f.status is not None
    assert f.status.shape == (1, GRID, GRID)


def test_v2_status_raw_is_preserved_not_collapsed() -> None:
    """펌웨어가 status 를 버리지 않고 원본으로 보낸다는 계약.

    구 펌웨어는 status != 5 를 전부 -1 로 뭉갰다. 그러면 호스트에서
    임계값을 조절할 수 없다. 원본이 그대로 올라와야 한다.
    """
    status = [5] * 64
    status[10] = 6
    status[11] = 9
    status[12] = 255
    f = parse_line(v2_line(ramp(), status))
    assert isinstance(f, ToFFrame)
    assert f.status is not None
    assert f.status[0].ravel()[10] == 6
    assert f.status[0].ravel()[11] == 9
    assert f.status[0].ravel()[12] == 255


def test_v2_status_filter_accepts_5_6_9_by_default() -> None:
    status = [5, 6, 9, 255, 4, 0, 5, 5] + [5] * 56
    f = parse_line(v2_line(ramp(), status))
    assert isinstance(f, ToFFrame)
    mask = f.valid_mask().ravel()
    assert mask[0] and mask[1] and mask[2]          # 5, 6, 9 통과
    assert not mask[3] and not mask[4] and not mask[5]  # 255, 4, 0 차단
    assert DEFAULT_STATUS_ACCEPT == (5, 6, 9)


def test_v2_status_filter_is_tunable() -> None:
    """엄격 모드(5만 허용)로 좁힐 수 있어야 한다."""
    status = [5, 6, 9] + [5] * 61
    f = parse_line(v2_line(ramp(), status))
    assert isinstance(f, ToFFrame)
    strict = f.valid_mask(accept=(5,)).ravel()
    assert strict[0]
    assert not strict[1] and not strict[2]


def test_v2_negative_distance_invalid_even_if_status_ok() -> None:
    """xtalk 가 심하면 status 가 유효해도 음수 거리가 올라온다."""
    dist = ramp()
    dist[3] = -12
    f = parse_line(v2_line(dist, [5] * 64))
    assert isinstance(f, ToFFrame)
    assert np.isnan(f.masked_depth().ravel()[3])


# --- n_target 자동 판별 ---------------------------------------------------

def test_v2_two_targets_autodetected() -> None:
    """NB_TARGET_PER_ZONE=2 빌드: d(t0),s(t0),d(t1),s(t1) 순서."""
    d0, d1 = ramp(), [v + 1000 for v in ramp()]
    s0, s1 = [5] * 64, [6] * 64
    body = ",".join(str(v) for v in [*d0, *s0, *d1, *s1])
    f = parse_line(f"F,111,2,{body}")
    assert isinstance(f, ToFFrame)
    assert f.n_target == 2
    assert f.depth(target=0)[0, 0] == 0.0
    assert f.depth(target=1)[0, 0] == 1000.0
    assert f.status is not None
    assert f.status[1][0, 0] == 6


def test_depth_rejects_out_of_range_target() -> None:
    f = parse_line(v1_line(ramp()))
    assert isinstance(f, ToFFrame)
    with pytest.raises(IndexError):
        f.depth(target=1)


# --- 그 밖의 줄 ------------------------------------------------------------

def test_pong_line() -> None:
    p = parse_line("P,1234567890")
    assert isinstance(p, Pong)
    assert p.t_us == 1234567890


def test_status_line() -> None:
    s = parse_line("# VL53L8CX start")
    assert isinstance(s, Status)
    assert s.text == "# VL53L8CX start"


# --- 쓰레기 입력 (신뢰 경계) ----------------------------------------------

@pytest.mark.parametrize(
    "line",
    [
        "",                                   # 빈 줄
        "   ",                                # 공백
        "F",                                  # 접두사만
        "F,",                                 # 필드 없음
        "F,1,2,3",                            # 필드 수 부족
        "F," + ",".join(["1"] * 63),          # v1 에서 하나 모자람
        "F," + ",".join(["1"] * 65),          # v1 에서 하나 많음
        "F," + ",".join(["1"] * 129),         # v2 필드 수 불일치
        "F,100,1," + ",".join(["x"] * 128),   # 정수가 아님
        "F,abc,1," + ",".join(["1"] * 128),   # 타임스탬프가 정수가 아님
        "P",                                  # pong 인자 없음
        "P,notanint",
        "Z,1,2,3",                            # 알 수 없는 접두사
        "\x00\xff garbage",                   # 바이너리 쓰레기
    ],
)
def test_garbage_returns_none(line: str) -> None:
    assert parse_line(line) is None


def test_truncated_line_mid_resync_returns_none() -> None:
    """포트를 중간부터 읽기 시작하면 앞이 잘린 줄이 먼저 온다."""
    full = v2_line(ramp(), [5] * 64)
    assert parse_line(full[37:]) is None


def test_trailing_whitespace_and_crlf_tolerated() -> None:
    f = parse_line(v1_line(ramp()) + "\r\n")
    assert isinstance(f, ToFFrame)


def test_frame_is_immutable() -> None:
    """읽기 전용 값 객체 — 소비자가 실수로 프레임을 바꾸면 안 된다."""
    f = parse_line(v1_line(ramp()))
    assert isinstance(f, ToFFrame)
    with pytest.raises(AttributeError):
        f.t_us = 5  # type: ignore[misc]
    assert not f.distance_mm.flags.writeable


# --- 값 객체 자체의 검증 (직접 생성하는 경로) -----------------------------

def test_frame_rejects_bad_distance_shape() -> None:
    with pytest.raises(ValueError, match="형상"):
        ToFFrame(distance_mm=np.zeros((8, 8)), status=None, t_us=None, seq=None)


def test_frame_rejects_mismatched_status_shape() -> None:
    with pytest.raises(ValueError, match="불일치"):
        ToFFrame(
            distance_mm=np.zeros((1, 8, 8)),
            status=np.zeros((2, 8, 8), dtype=np.int16),
            t_us=None,
            seq=None,
        )


def test_v1_line_with_noninteger_field_returns_none() -> None:
    """v1 경로에서도 정수가 아닌 필드는 조용히 버린다."""
    assert parse_line("F," + ",".join(["1"] * 63 + ["oops"])) is None


# --- 좌우 반전 (센서 장착 방향 보정) --------------------------------------

def test_fliplr_mirrors_columns() -> None:
    f = parse_line(v1_line(ramp()))
    assert isinstance(f, ToFFrame)
    flipped = f.fliplr()
    # zone (r, c) 가 (r, 7-c) 로 간다.
    assert flipped.depth()[0, 0] == f.depth()[0, 7]
    assert flipped.depth()[3, 2] == f.depth()[3, 5]
    assert np.array_equal(flipped.fliplr().depth(), f.depth())  # 두 번이면 제자리


def test_fliplr_does_not_touch_rows() -> None:
    """좌우만 뒤집는다. 상하까지 뒤집히면 위아래가 통째로 어긋난다."""
    f = parse_line(v1_line(ramp()))
    assert isinstance(f, ToFFrame)
    # row 0 은 0..7, row 7 은 56..63. 뒤집어도 row 0 은 여전히 0..7 범위.
    assert set(f.fliplr().depth()[0].tolist()) == set(range(8))
    assert set(f.fliplr().depth()[7].tolist()) == set(range(56, 64))


def test_fliplr_moves_distance_and_status_together() -> None:
    """거리만 뒤집고 status 를 두면 마스크가 어긋나 멀쩡한 zone 이 무효가 된다."""
    status = [5] * 64
    status[0] = 255          # (0,0) 만 무효
    f = parse_line(v2_line(ramp(), status))
    assert isinstance(f, ToFFrame)

    flipped = f.fliplr()
    mask = flipped.valid_mask()
    assert not mask[0, 7]    # 무효 표시가 거리와 같이 따라왔다
    assert mask[0, 0]
    assert np.isnan(flipped.masked_depth()[0, 7])
    assert flipped.masked_depth()[0, 0] == f.depth()[0, 7]


def test_fliplr_preserves_timestamp_and_seq() -> None:
    f = parse_line(v2_line(ramp(), [5] * 64, t_us=555, seq=9))
    assert isinstance(f, ToFFrame)
    flipped = f.fliplr()
    assert flipped.t_us == 555
    assert flipped.seq == 9
    assert flipped.n_target == 1


def test_fliplr_handles_all_targets() -> None:
    d0, d1 = ramp(), [v + 1000 for v in ramp()]
    body = ",".join(str(v) for v in [*d0, *[5] * 64, *d1, *[6] * 64])
    f = parse_line(f"F,1,1,{body}")
    assert isinstance(f, ToFFrame)
    flipped = f.fliplr()
    assert flipped.depth(target=0)[0, 0] == 7.0
    assert flipped.depth(target=1)[0, 0] == 1007.0


def test_fliplr_returns_new_frozen_frame() -> None:
    """불변 값 객체 계약 — 원본은 그대로 남고 결과도 잠겨 있어야 한다."""
    f = parse_line(v1_line(ramp()))
    assert isinstance(f, ToFFrame)
    before = f.depth().copy()
    flipped = f.fliplr()
    assert flipped is not f
    assert np.array_equal(f.depth(), before)
    assert not flipped.distance_mm.flags.writeable
