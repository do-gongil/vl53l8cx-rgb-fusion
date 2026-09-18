"""렌더러 테스트 — 눈으로 잡은 결함이 되돌아가지 않게 고정한다.

여기 있는 두 가지는 실제로 한 번씩 틀렸던 것들이다:

  * zone 숫자를 항상 흰 글씨로 그렸더니 히트맵 중간 톤(노랑·연두)에서
    전혀 읽히지 않았다 -> 배경 밝기로 글자색을 고르게 고쳤다.
  * ``cv2.putText`` 로 그렸더니 한글이 전부 두부(□)로 깨졌다
    -> Pillow + 시스템 폰트로 바꿨다.

둘 다 "안 죽지만 쓸모없어지는" 종류라 테스트가 없으면 조용히 되돌아간다.
"""
from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest

from toffuse import render


@pytest.fixture
def no_font(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """폰트를 못 찾는 환경(폰트 없는 Linux/CI)을 흉내낸다."""
    monkeypatch.setattr(render, "_FONT_CANDIDATES", ())
    render._font.cache_clear()
    yield
    render._font.cache_clear()


# --- 글자색 대비 -----------------------------------------------------------

def test_contrast_picks_black_on_bright_background() -> None:
    fill, stroke = render._contrast_pair((0, 255, 255))  # 노랑(BGR)
    assert fill == (0, 0, 0)
    assert stroke == (255, 255, 255)


def test_contrast_picks_white_on_dark_background() -> None:
    fill, stroke = render._contrast_pair((128, 0, 0))  # 진한 파랑(BGR)
    assert fill == (255, 255, 255)
    assert stroke == (0, 0, 0)


def test_contrast_uses_luma_not_raw_sum() -> None:
    """순수 파랑과 순수 초록은 채널 합이 같아도 체감 밝기가 전혀 다르다."""
    blue_fill, _ = render._contrast_pair((255, 0, 0))
    green_fill, _ = render._contrast_pair((0, 255, 0))
    assert blue_fill == (255, 255, 255)   # 파랑은 어둡게 보인다 -> 흰 글씨
    assert green_fill == (0, 0, 0)        # 초록은 밝게 보인다 -> 검은 글씨


def test_zone_numbers_readable_across_whole_colormap() -> None:
    """컬러맵 전 구간에서 글자가 배경과 구분되어야 한다.

    칸마다 중앙 부근에 배경과 다른 픽셀이 실제로 생겼는지 확인한다.
    글자색이 배경과 같으면 이 검사가 무너진다.
    """
    depth = np.linspace(0, 1500, 64).reshape(8, 8)
    plain = render.heatmap(depth, size=320, vmax=1500)
    marked = render.annotate_zones(plain, depth)

    for r in range(8):
        for c in range(8):
            y, x = int((r + 0.5) * 40), int((c + 0.5) * 40)
            patch = marked[y - 12 : y + 12, x - 12 : x + 12]
            base = plain[y, x]
            assert np.any(
                np.abs(patch.astype(int) - base.astype(int)).sum(axis=2) > 60
            ), f"zone ({r},{c}) 에서 숫자가 배경에 묻힘"


# --- 한글 렌더링 -----------------------------------------------------------

def test_korean_text_actually_draws_pixels() -> None:
    """한글이 두부로 깨지거나 아예 안 그려지면 잡는다."""
    if not render.has_unicode_font():
        pytest.skip("이 환경에 한글 폰트가 없다")
    blank = np.zeros((60, 400, 3), dtype=np.uint8)
    drawn = render.hud(blank, ["유효 62/64 zone   측정 범위"])
    assert np.count_nonzero(drawn) > 200


def test_falls_back_to_cv2_without_font(no_font: None) -> None:
    """폰트가 없어도 죽지 않는다. 한글은 깨지지만 숫자는 읽힌다."""
    assert render.has_unicode_font() is False
    depth = np.full((8, 8), 250.0)
    out = render.annotate_zones(render.heatmap(depth, size=160), depth)
    assert out.shape == (160, 160, 3)
    assert render.placeholder((200, 80), "no camera").shape == (80, 200, 3)
    assert render.hud(np.zeros((60, 300, 3), dtype=np.uint8), ["ToF 15.0 fps"]).any()


# --- HUD 경계 --------------------------------------------------------------

def test_hud_with_no_lines_returns_copy() -> None:
    img = render.heatmap(np.full((8, 8), 300.0), size=64)
    out = render.hud(img, [])
    assert np.array_equal(out, img)
    assert out is not img


def test_hud_panel_clipped_to_image_bounds() -> None:
    """이미지보다 긴 줄이 와도 슬라이싱이 밖으로 나가면 안 된다."""
    img = np.zeros((40, 80, 3), dtype=np.uint8)
    out = render.hud(img, ["아주 긴 상태 문자열 " * 10, "두 번째 줄", "세 번째 줄"])
    assert out.shape == (40, 80, 3)


def test_hud_darkens_panel_background() -> None:
    img = np.full((80, 400, 3), 200, dtype=np.uint8)
    out = render.hud(img, ["CAM 2592x1944"])
    assert int(out[15, 15].mean()) < 120       # 패널 안쪽은 어두워짐
    assert int(out[70, 380].mean()) == 200     # 패널 밖은 그대로


# --- 합성 ------------------------------------------------------------------

def test_side_by_side_matches_heights_and_preserves_aspect() -> None:
    left = np.zeros((100, 200, 3), dtype=np.uint8)
    right = np.zeros((50, 50, 3), dtype=np.uint8)
    out = render.side_by_side(left, right, gap=8)
    assert out.shape[0] == 100
    assert out.shape[1] == 200 + 8 + 100      # 오른쪽이 50x50 -> 100x100


def test_fit_height_returns_same_object_when_already_matching() -> None:
    img = np.zeros((64, 64, 3), dtype=np.uint8)
    assert render.fit_height(img, 64) is img


def test_heatmap_uses_nearest_neighbour_not_interpolation() -> None:
    """보간하면 8x8 보다 해상도가 높아 보여 정합 오차를 눈으로 못 잡는다."""
    depth = np.zeros((8, 8))
    depth[0, 0] = 1500.0
    img = render.heatmap(depth, size=80, vmax=1500)
    # 첫 칸(10x10 px) 안은 전부 같은 색이어야 한다.
    cell = img[0:10, 0:10].reshape(-1, 3)
    assert len(np.unique(cell, axis=0)) == 1
