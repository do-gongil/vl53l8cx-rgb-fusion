"""live_check.py 의 하드웨어 없는 부분 테스트.

카메라·시리얼은 실물이 없으면 의미 있는 검증이 안 되므로 목으로 채우지
않는다. 대신 실물 없이도 틀릴 수 있는 것 -- HUD 문자열 조립, 스냅샷 저장
포맷, 렌더링 파이프라인 -- 만 잡는다. 장치 I/O 는 `--no-camera/--no-tof`
스모크 실행으로 대신한다.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from toffuse import render
from toffuse.camera import CameraInfo
from toffuse.protocol import GRID, ToFFrame, parse_line

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_check.py"


def _load_live_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_check", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_check"] = mod
    spec.loader.exec_module(mod)
    return mod


lc = _load_live_check()


def _info() -> CameraInfo:
    return CameraInfo(index=0, backend="DSHOW", width=2592, height=1944, fps=15.0, fourcc="MJPG")


def _v1_frame(dist: list[int] | None = None) -> ToFFrame:
    values = dist if dist is not None else [100 + i for i in range(GRID * GRID)]
    frame = parse_line("F," + ",".join(str(v) for v in values))
    assert isinstance(frame, ToFFrame)
    return frame


def _v2_frame() -> ToFFrame:
    dist = [100 + i for i in range(GRID * GRID)]
    status = [5] * (GRID * GRID)
    body = ",".join(str(v) for v in [*dist, *status])
    frame = parse_line(f"F,7000000,3,{body}")
    assert isinstance(frame, ToFFrame)
    return frame


# --- Fps -------------------------------------------------------------------

def test_fps_is_zero_before_two_ticks() -> None:
    f = lc.Fps()
    assert f.value == 0.0
    f.tick()
    assert f.value == 0.0


def test_fps_measures_rate() -> None:
    f = lc.Fps()
    # perf_counter 를 흉내내지 않고 내부 큐에 알려진 간격을 넣는다.
    f._t.extend([0.0, 0.1, 0.2, 0.3])
    assert f.value == pytest.approx(10.0)


# --- HUD 조립 --------------------------------------------------------------

def test_hud_without_tof_says_waiting() -> None:
    lines = lc.build_hud(_info(), "COM7", 115200, lc.Fps(), lc.Fps(), None, 1500.0, 0)
    assert any("대기" in ln for ln in lines)
    assert any("2592x1944" in ln for ln in lines)


def test_hud_labels_v1_and_v2_protocol() -> None:
    v1 = lc.build_hud(_info(), "COM7", 115200, lc.Fps(), lc.Fps(), _v1_frame(), 1500.0, 0)
    v2 = lc.build_hud(_info(), "COM7", 921600, lc.Fps(), lc.Fps(), _v2_frame(), 1500.0, 0)
    assert any("[v1]" in ln for ln in v1)
    assert any("[v2]" in ln for ln in v2)
    # v1 에는 타임스탬프 줄이 없어야 한다.
    assert not any("seq=" in ln for ln in v1)
    assert any("seq=3" in ln for ln in v2)


def test_hud_reports_valid_zone_count_and_range() -> None:
    dist = [-1] * 60 + [200, 300, 400, 500]
    lines = lc.build_hud(_info(), "COM7", 115200, lc.Fps(), lc.Fps(), _v1_frame(dist), 1500.0, 0)
    assert any("유효  4/64" in ln for ln in lines)
    assert any("200~500mm" in ln for ln in lines)


def test_hud_handles_all_invalid_frame() -> None:
    """전부 무효일 때 min/max 를 빈 배열에 부르면 터진다 -- 그 경로를 막는다."""
    lines = lc.build_hud(
        _info(), "COM7", 115200, lc.Fps(), lc.Fps(), _v1_frame([-1] * 64), 1500.0, 0
    )
    assert any("측정 없음" in ln for ln in lines)
    assert any("유효  0/64" in ln for ln in lines)


def test_hud_without_camera() -> None:
    lines = lc.build_hud(None, None, 115200, lc.Fps(), lc.Fps(), None, 1500.0, 0)
    assert any("없음" in ln for ln in lines)
    assert any("포트 없음" in ln for ln in lines)


# --- 스냅샷 저장 -----------------------------------------------------------

def test_snapshot_writes_all_three_files(tmp_path: Path) -> None:
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    heat = render.heatmap(np.full((GRID, GRID), 300.0))
    lc.save_snapshot(tmp_path, frame, _v2_frame(), heat)

    names = sorted(p.name.split("_", 3)[-1] for p in tmp_path.iterdir())
    assert names == ["color.png", "tof.json", "tof.png"]


def test_snapshot_json_schema_and_values(tmp_path: Path) -> None:
    heat = render.heatmap(np.full((GRID, GRID), 300.0))
    lc.save_snapshot(tmp_path, None, _v2_frame(), heat)

    payload = json.loads(next(tmp_path.glob("*_tof.json")).read_text(encoding="utf-8"))
    assert set(payload) == {"t_us", "seq", "n_target", "distance_mm", "status"}
    assert payload["t_us"] == 7_000_000
    assert payload["seq"] == 3
    assert payload["n_target"] == 1
    assert len(payload["distance_mm"]) == GRID
    assert payload["distance_mm"][0][0] == 100.0
    assert payload["status"][0][0] == 5


def test_snapshot_without_camera_skips_color(tmp_path: Path) -> None:
    heat = render.heatmap(np.full((GRID, GRID), 300.0))
    lc.save_snapshot(tmp_path, None, None, heat)
    assert not list(tmp_path.glob("*_color.png"))
    assert not list(tmp_path.glob("*_tof.json"))
    assert list(tmp_path.glob("*_tof.png"))


def test_snapshot_timestamp_format(tmp_path: Path) -> None:
    """파일명은 기존 프로젝트 관례인 %Y%m%d_%H%M%S + 밀리초."""
    heat = render.heatmap(np.full((GRID, GRID), 300.0))
    lc.save_snapshot(tmp_path, None, None, heat)
    stamp = next(tmp_path.glob("*_tof.png")).name.removesuffix("_tof.png")
    date, clock, msec = stamp.split("_")
    assert len(date) == 8 and date.isdigit()
    assert len(clock) == 6 and clock.isdigit()
    assert len(msec) == 3 and msec.isdigit()


# --- 렌더링 파이프라인 -----------------------------------------------------

def test_render_pipeline_survives_all_nan() -> None:
    """ToF 가 아직 안 붙었을 때 첫 프레임이 전부 NaN 이다. 여기서 죽으면 안 된다."""
    depth = np.full((GRID, GRID), np.nan)
    heat = render.annotate_zones(render.heatmap(depth, size=240), depth)
    canvas = render.side_by_side(render.placeholder((320, 240), "카메라 없음"), heat)
    out = render.hud(canvas, ["a", "bb", "ccc"])
    assert out.shape[0] == 240
    assert out.dtype == np.uint8


def test_heatmap_colors_near_warm_and_far_cool() -> None:
    """가까울수록 빨강, 멀수록 파랑 -- 이 규약이 뒤집히면 오독한다."""
    near = render.heatmap(np.full((GRID, GRID), 100.0), size=8, vmin=0, vmax=1000)
    far = render.heatmap(np.full((GRID, GRID), 900.0), size=8, vmin=0, vmax=1000)
    b_near, r_near = int(near[0, 0, 0]), int(near[0, 0, 2])
    b_far, r_far = int(far[0, 0, 0]), int(far[0, 0, 2])
    assert r_near > b_near
    assert b_far > r_far


def test_heatmap_marks_invalid_zones_gray() -> None:
    depth = np.full((GRID, GRID), 500.0)
    depth[3, 4] = np.nan
    img = render.heatmap(depth, size=GRID, vmax=1000)
    assert tuple(int(v) for v in img[3, 4]) == render.INVALID_BGR


def test_heatmap_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="vmax"):
        render.heatmap(np.zeros((GRID, GRID)), vmin=1000, vmax=100)


def test_annotate_does_not_mutate_input() -> None:
    img = render.heatmap(np.full((GRID, GRID), 300.0), size=80)
    before = img.copy()
    render.annotate_zones(img, np.full((GRID, GRID), 300.0))
    render.hud(img, ["x"])
    assert np.array_equal(img, before)
