"""OpenCV 기반 렌더링 — 8x8 히트맵, HUD, 화면 합성.

matplotlib 을 쓰지 않는다. 구 뷰어(``tof_viewer.py``)는 matplotlib 창을 썼지만,
카메라 영상과 나란히 놓으려면 두 GUI 툴킷이 한 프로세스에서 이벤트 루프를
다투게 된다. 전부 OpenCV 로 그리면 창 하나로 끝나고, Phase 3 의
``fusion_view.py`` 가 이 모듈을 그대로 물려받는다.

**색 규약: 가까울수록 따뜻한 색(빨강), 멀수록 차가운 색(파랑).**

글자는 ``cv2.putText`` 가 아니라 Pillow 로 그린다. OpenCV 의 Hershey 폰트는
ASCII 만 있어서 한글이 두부(□)로 깨진다. Pillow 는 이미 설치되어 있고
Windows 의 맑은 고딕을 쓰면 새 의존성이 없다. 폰트를 못 찾으면 Hershey 로
떨어지며, 그때는 한글이 깨지므로 ``has_unicode_font()`` 로 확인할 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFont

# cv2 의 타입 스텁은 반환 ndarray 의 dtype 파라미터를 보존하지 못한다.
# 런타임 dtype 은 항상 uint8 이므로 경계에서만 cast 로 좁힌다.
BGR = NDArray[np.uint8]
Color = tuple[int, int, int]

INVALID_BGR: Color = (58, 58, 58)   # 무효 zone 회색 — 구 뷰어의 '#444444' 와 같은 톤
_HUD_FONT = cv2.FONT_HERSHEY_SIMPLEX

# 한글이 들어 있는 시스템 폰트. 앞에서부터 먼저 찾아지는 것을 쓴다.
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\malgun.ttf",          # 맑은 고딕 (Windows 기본)
    r"C:\Windows\Fonts\gulim.ttc",           # 굴림 (구형 Windows)
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

# 글자색을 배경 밝기로 고른다. 이 값보다 밝으면 검은 글씨.
_LUMA_SWITCH = 140.0


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return None


def has_unicode_font() -> bool:
    """한글을 그릴 수 있는 폰트를 찾았는가."""
    return _font(14) is not None


@dataclass(frozen=True)
class _Label:
    """한 번의 Pillow 패스로 모아서 그릴 글자 하나."""

    text: str
    xy: tuple[int, int]
    size: int
    fill: Color
    stroke: Color
    center: bool = False


def _to_rgb(bgr: Color) -> Color:
    return (bgr[2], bgr[1], bgr[0])


def _draw_labels(img: BGR, labels: list[_Label]) -> BGR:
    """글자를 전부 한 번에 그린다.

    BGR<->PIL 변환이 글자마다 일어나면 64개 zone 에서 낭비가 크다.
    한 프레임에 한 번만 변환한다.
    """
    if not labels:
        return img
    if not has_unicode_font():
        return _draw_labels_cv2(img, labels)

    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    for lb in labels:
        draw.text(
            lb.xy,
            lb.text,
            font=_font(lb.size),
            fill=_to_rgb(lb.fill),
            stroke_width=2,
            stroke_fill=_to_rgb(lb.stroke),
            anchor="mm" if lb.center else "la",
        )
    return cast(BGR, cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR))


def _draw_labels_cv2(img: BGR, labels: list[_Label]) -> BGR:
    """폰트를 못 찾았을 때의 폴백. 한글은 깨지지만 숫자는 읽힌다."""
    out: BGR = img.copy()
    for lb in labels:
        scale = lb.size / 30.0
        (tw, th), _ = cv2.getTextSize(lb.text, _HUD_FONT, scale, 1)
        x, y = lb.xy
        org = (int(x - tw / 2), int(y + th / 2)) if lb.center else (int(x), int(y + th))
        cv2.putText(out, lb.text, org, _HUD_FONT, scale, lb.stroke, 3, cv2.LINE_AA)
        cv2.putText(out, lb.text, org, _HUD_FONT, scale, lb.fill, 1, cv2.LINE_AA)
    return out


def _contrast_pair(background: Color) -> tuple[Color, Color]:
    """배경 위에서 확실히 읽히는 (글자색, 테두리색).

    히트맵 중간 톤(노랑·연두)에서 흰 글씨는 묻힌다. 밝기로 갈라야 한다.
    """
    b, g, r = (float(v) for v in background)
    luma = 0.114 * b + 0.587 * g + 0.299 * r
    return ((0, 0, 0), (255, 255, 255)) if luma > _LUMA_SWITCH else ((255, 255, 255), (0, 0, 0))


def heatmap(
    depth: NDArray[np.float64],
    size: int = 480,
    vmin: float = 0.0,
    vmax: float = 1500.0,
    colormap: int = cv2.COLORMAP_TURBO,
    show_invalid: bool = True,
) -> BGR:
    """NaN 을 포함한 (8, 8) 거리 배열을 ``size x size`` BGR 히트맵으로.

    ``INTER_NEAREST`` 로 확대해 zone 경계를 또렷하게 남긴다. 보간하면 실제보다
    해상도가 높아 보여서 정합 오차를 눈으로 잡아내기 어려워진다.
    """
    if vmax <= vmin:
        raise ValueError(f"vmax({vmax}) 가 vmin({vmin}) 보다 커야 한다")

    invalid = ~np.isfinite(depth)
    # 가까울수록 255(빨강)가 되도록 뒤집는다.
    norm = 1.0 - (np.nan_to_num(depth, nan=vmax) - vmin) / (vmax - vmin)
    u8 = np.clip(norm * 255.0, 0, 255).astype(np.uint8)

    img = cv2.applyColorMap(u8, colormap)
    if show_invalid:
        img[invalid] = INVALID_BGR

    return cast(BGR, cv2.resize(img, (size, size), interpolation=cv2.INTER_NEAREST))


def annotate_zones(img: BGR, depth: NDArray[np.float64], grid: int = 8) -> BGR:
    """히트맵 위에 zone 별 거리(mm)를 적고 격자선을 긋는다. 원본은 건드리지 않는다.

    글자색은 그 칸의 배경 밝기로 정한다 — 컬러맵 전 구간에서 읽혀야 한다.
    """
    out: BGR = img.copy()
    h, w = out.shape[:2]
    cell_w, cell_h = w / grid, h / grid
    size = max(9, int(min(cell_w, cell_h) * 0.30))

    for i in range(1, grid):
        x, y = int(i * cell_w), int(i * cell_h)
        cv2.line(out, (x, 0), (x, h), (0, 0, 0), 1, cv2.LINE_AA)
        cv2.line(out, (0, y), (w, y), (0, 0, 0), 1, cv2.LINE_AA)

    labels: list[_Label] = []
    for r in range(grid):
        for c in range(grid):
            cx, cy = int((c + 0.5) * cell_w), int((r + 0.5) * cell_h)
            v = depth[r, c]
            bg: Color = (int(out[cy, cx][0]), int(out[cy, cx][1]), int(out[cy, cx][2]))
            fill, stroke = _contrast_pair(bg)
            labels.append(
                _Label(
                    text="-" if not np.isfinite(v) else f"{int(round(v))}",
                    xy=(cx, cy),
                    size=size,
                    fill=fill,
                    stroke=stroke,
                    center=True,
                )
            )
    return _draw_labels(out, labels)


def hud(img: BGR, lines: list[str], origin: tuple[int, int] = (10, 10)) -> BGR:
    """좌상단에 반투명 패널을 깔고 텍스트를 쌓는다. 원본은 건드리지 않는다."""
    out: BGR = img.copy()
    if not lines:
        return out

    size, pad, step = 15, 9, 21
    font = _font(size)
    if font is not None:
        widths = [int(font.getlength(s)) for s in lines]
    else:
        widths = [cv2.getTextSize(s, _HUD_FONT, size / 30.0, 1)[0][0] for s in lines]

    x0, y0 = origin
    x1 = min(out.shape[1], x0 + max(widths) + 2 * pad)
    y1 = min(out.shape[0], y0 + step * len(lines) + 2 * pad)

    panel = out[y0:y1, x0:x1]
    if panel.size:
        panel[:] = (panel.astype(np.float32) * 0.35).astype(np.uint8)

    labels = [
        _Label(
            text=t,
            xy=(x0 + pad, y0 + pad + step * i),
            size=size,
            fill=(235, 235, 235),
            stroke=(0, 0, 0),
        )
        for i, t in enumerate(lines)
    ]
    return _draw_labels(out, labels)


def fit_height(img: BGR, height: int) -> BGR:
    """가로세로비를 유지한 채 높이를 맞춘다."""
    h, w = img.shape[:2]
    if h == height:
        return img
    return cast(
        BGR,
        cv2.resize(img, (max(1, round(w * height / h)), height),
                   interpolation=cv2.INTER_AREA),
    )


def side_by_side(left: BGR, right: BGR, gap: int = 8) -> BGR:
    """높이를 맞춰 좌우로 붙인다."""
    h = max(left.shape[0], right.shape[0])
    lo, ro = fit_height(left, h), fit_height(right, h)
    spacer = np.zeros((h, gap, 3), dtype=np.uint8)
    out: BGR = np.hstack([lo, spacer, ro])
    return out


def placeholder(size: tuple[int, int], message: str) -> BGR:
    """장치가 없을 때 자리를 채우는 안내 패널. size 는 (width, height)."""
    w, h = size
    img: BGR = np.full((h, w, 3), 28, dtype=np.uint8)
    label = _Label(
        text=message,
        xy=(w // 2, h // 2),
        size=18,
        fill=(90, 90, 200),
        stroke=(0, 0, 0),
        center=True,
    )
    return _draw_labels(img, [label])
