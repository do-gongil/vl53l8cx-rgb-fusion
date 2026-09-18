"""calib_xtalk.py 의 하드웨어 없는 부분 테스트.

이 스크립트에서 조용히 틀릴 수 있는 것은 두 가지다:

  * **범위 검증** -- 물리 세팅을 다 잡아놓고 나서 펌웨어에 거부당하면
    타깃을 다시 세워야 한다. 호스트가 먼저 걸러야 의미가 있다.
  * **타깃 크기 계산** -- 시야를 못 채우면 ULD 는 에러가 아니라 '그럴듯한
    틀린 보정값'을 준다. 이 숫자가 틀리면 잘못된 보정을 눈치채지 못한다.

시리얼 왕복은 실물 없이 검증할 수 없으므로 목으로 채우지 않는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from toffuse.protocol import XtalkResult

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "calib_xtalk.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("calib_xtalk", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["calib_xtalk"] = mod
    spec.loader.exec_module(mod)
    return mod


cx = _load()


def _args(reflectance: int = 3, samples: int = 16, distance: int = 600) -> argparse.Namespace:
    return argparse.Namespace(reflectance=reflectance, samples=samples, distance=distance)


# --- 범위 검증 --------------------------------------------------------------

def test_defaults_pass() -> None:
    assert cx.validate(_args()) is None


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        ({"reflectance": 0}, "반사율"),
        ({"reflectance": 100}, "반사율"),
        ({"samples": 0}, "샘플"),
        ({"samples": 17}, "샘플"),
        ({"distance": 599}, "거리"),
        ({"distance": 3001}, "거리"),
    ],
)
def test_out_of_range_rejected_with_reason(kwargs: dict[str, int], needle: str) -> None:
    problem = cx.validate(_args(**kwargs))
    assert problem is not None
    assert needle in problem


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reflectance": 1},
        {"reflectance": 99},
        {"samples": 1},
        {"samples": 16},
        {"distance": 600},
        {"distance": 3000},
    ],
)
def test_boundaries_are_inclusive(kwargs: dict[str, int]) -> None:
    """ULD 문서가 1~99 / 1~16 / 600~3000 을 '포함'으로 쓴다."""
    assert cx.validate(_args(**kwargs)) is None


def test_ranges_match_uld_documentation() -> None:
    assert cx.REFLECTANCE_RANGE == (1, 99)
    assert cx.SAMPLES_RANGE == (1, 16)
    assert cx.DISTANCE_RANGE_MM == (600, 3000)


# --- 타깃 크기 --------------------------------------------------------------

def test_target_size_at_minimum_distance() -> None:
    """600 mm 에서 48.5도 정사각 시야 -> 약 54 cm.

    2 * 600 * tan(24.25도) = 2 * 600 * 0.4505 = 541 mm
    """
    assert cx.target_size_mm(600) == pytest.approx(541, abs=2)


def test_target_size_scales_linearly() -> None:
    assert cx.target_size_mm(1200) == pytest.approx(2 * cx.target_size_mm(600), abs=2)


def test_checklist_states_required_target_size() -> None:
    """점검표가 실제 필요한 크기를 알려줘야 사용자가 판단할 수 있다."""
    text = "\n".join(cx.checklist(_args(distance=600)))
    assert "541 x 541" in text
    assert "빔스플리터" in text
    assert "940" in text


def test_checklist_size_follows_distance() -> None:
    far = "\n".join(cx.checklist(_args(distance=1200)))
    assert "1081 x 1081" in far


# --- 결과 해석 --------------------------------------------------------------

def test_report_success_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code = cx.report(
        XtalkResult(ok=True, reflectance_percent=3, nb_samples=16, distance_mm=600)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "완료" in out and "600" in out


def test_report_failure_returns_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    code = cx.report(XtalkResult(ok=False, code=255, message="타깃이 시야를 못 채움"))
    out = capsys.readouterr().out
    assert code == 1
    assert "실패" in out
    assert "타깃이 시야를 못 채움" in out


def test_report_argument_error_points_at_help(capsys: pytest.CaptureFixture[str]) -> None:
    """127 은 인자 문제다. 물리 세팅을 다시 잡으라고 하면 안 된다."""
    cx.report(XtalkResult(ok=False, code=127, message="거리 100 mm 는 범위 밖"))
    out = capsys.readouterr().out
    assert "--help" in out
    assert "타깃이 시야를 꽉" not in out


def test_report_cleared_warns_about_reboot(capsys: pytest.CaptureFixture[str]) -> None:
    """NVS 만 지워지고 RAM 의 보정은 남는다. 조용히 넘어가면 오해한다."""
    code = cx.report(XtalkResult(ok=True, message="cleared"))
    out = capsys.readouterr().out
    assert code == 0
    assert "재부팅" in out


def test_report_none_is_not_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = cx.report(XtalkResult(ok=True, message="none"))
    assert code == 0
    assert "없습니다" in capsys.readouterr().out
