"""RGB 카메라와 VL53L8CX ToF 를 한 창에 동시에 띄우는 스모크 테스트.

캘리브레이션도 시간 동기화도 하지 않는다. 확인하는 것은 단 하나 --
**두 장치가 지금 살아서 데이터를 내보내고 있는가.**

구 펌웨어(``F,d0..d63``)와 신 펌웨어(``F,t_us,seq,d..,s..``) 양쪽을 자동으로
알아보므로 펌웨어를 고치기 전에 그대로 돌릴 수 있다. 한쪽 장치만 꽂혀 있어도
실행되며, 없는 쪽은 안내 패널이 대신 뜬다.

첫 실행에서 확인해야 할 것:
  * COM 포트와 카메라 인덱스
  * **카메라가 실제로 내보내는 해상도** (계획서 R3 -- 5MP 모듈이 1080p 를
    크롭으로 내는지 다운스케일로 내는지에 따라 실효 FoV 가 달라진다)
  * 손을 흔들었을 때 좌우 두 화면이 동시에 반응하는가

사용법::

    python scripts/live_check.py                        # 자동 탐지
    python scripts/live_check.py --cam 1 --port COM7
    python scripts/live_check.py --baud 921600          # v2 펌웨어
    python scripts/live_check.py --width 2592 --height 1944
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toffuse import camera, render, tof_reader  # noqa: E402
from toffuse.console import setup_stdout  # noqa: E402
from toffuse.protocol import GRID, ToFFrame  # noqa: E402

WINDOW = "live_check  |  RAW - not aligned"
_VMAX_STEP = 100.0
_VMAX_MIN = 200.0
_VMAX_MAX = 4000.0


class Fps:
    """최근 N 개 시점으로 낸 순간 프레임레이트."""

    def __init__(self, window: int = 30) -> None:
        self._t: deque[float] = deque(maxlen=window)

    def tick(self) -> None:
        self._t.append(time.perf_counter())

    @property
    def value(self) -> float:
        if len(self._t) < 2:
            return 0.0
        span = self._t[-1] - self._t[0]
        return (len(self._t) - 1) / span if span > 0 else 0.0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cam", type=int, default=None, help="카메라 인덱스 (기본: 자동 탐지)")
    ap.add_argument("--port", default=None, help="시리얼 포트 예: COM7 (기본: 자동 탐지)")
    ap.add_argument("--baud", type=int, default=tof_reader.DEFAULT_BAUD, help="시리얼 속도")
    ap.add_argument("--width", type=int, default=None, help="카메라 요청 가로")
    ap.add_argument("--height", type=int, default=None, help="카메라 요청 세로")
    ap.add_argument("--disp-height", type=int, default=540, help="표시 높이 (저장은 원본)")
    ap.add_argument("--max", dest="vmax", type=float, default=1500.0, help="컬러맵 상한 mm")
    ap.add_argument("--out", type=Path, default=Path("snapshots"), help="스냅샷 저장 폴더")
    ap.add_argument("--no-camera", action="store_true", help="카메라를 열지 않는다")
    ap.add_argument("--no-tof", action="store_true", help="ToF 를 열지 않는다")
    return ap.parse_args()


def open_devices(
    args: argparse.Namespace,
) -> tuple[cv2.VideoCapture | None, camera.CameraInfo | None, serial.Serial | None, str | None]:
    """두 장치를 연다. 한쪽이 없어도 죽지 않는다 -- 한쪽만으로도 점검할 수 있어야 한다."""
    cap: cv2.VideoCapture | None = None
    info: camera.CameraInfo | None = None
    if not args.no_camera:
        found = camera.open_camera(args.cam, args.width, args.height)
        if found is None:
            print("[경고] 카메라를 찾지 못했습니다. --cam 으로 인덱스를 지정해 보세요.")
        else:
            cap, info = found
            print(f"[카메라] {info.describe()}")

    ser: serial.Serial | None = None
    port = args.port
    if not args.no_tof:
        port = port or tof_reader.find_port()
        if port is None:
            print("[경고] USB 시리얼 포트를 찾지 못했습니다. ESP32 연결을 확인하세요.")
            for line in tof_reader.list_usb_ports():
                print(f"        후보: {line}")
        else:
            try:
                ser = tof_reader.open_serial(port, args.baud)
                print(f"[ToF] {port} @ {args.baud}")
            except serial.SerialException as exc:
                print(f"[경고] {port} 열기 실패: {exc}")
                ser = None
    return cap, info, ser, port


def save_snapshot(
    out_dir: Path,
    frame: np.ndarray | None,
    tof: ToFFrame | None,
    heat: np.ndarray,
) -> Path:
    """색상 원본 + 히트맵 + 원시 거리값을 한 묶음으로 남긴다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    if frame is not None:
        cv2.imwrite(str(out_dir / f"{stamp}_color.png"), frame)
    cv2.imwrite(str(out_dir / f"{stamp}_tof.png"), heat)
    if tof is not None:
        payload = {
            "t_us": tof.t_us,
            "seq": tof.seq,
            "n_target": tof.n_target,
            "distance_mm": tof.depth().tolist(),
            "status": None if tof.status is None else tof.status[0].tolist(),
        }
        (out_dir / f"{stamp}_tof.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
    return out_dir / stamp


def build_hud(
    info: camera.CameraInfo | None,
    port: str | None,
    baud: int,
    cam_fps: Fps,
    tof_fps: Fps,
    tof: ToFFrame | None,
    vmax: float,
    dropped: int,
) -> list[str]:
    lines = [f"CAM  {info.describe() if info else '없음'}  {cam_fps.value:4.1f} fps"]
    if tof is None:
        lines.append(f"ToF  {port or '포트 없음'}  대기 중...")
    else:
        proto = "v2" if tof.t_us is not None else "v1"
        depth = tof.masked_depth()
        finite = depth[np.isfinite(depth)]
        rng = f"{finite.min():.0f}~{finite.max():.0f}mm" if finite.size else "측정 없음"
        lines += [
            f"ToF  {port} @{baud} [{proto}] {tof_fps.value:4.1f} fps  drop {dropped}",
            f"     유효 {tof.valid_count():2d}/{GRID * GRID} zone   {rng}",
        ]
        if tof.t_us is not None:
            lines.append(f"     t={tof.t_us / 1e6:10.3f}s  seq={tof.seq}")
    lines.append(f"범위 0~{vmax:.0f}mm   [ ] 범위  N 무효  T 숫자  S 저장  Q 종료")
    return lines


def main() -> int:
    setup_stdout()
    args = parse_args()
    cap, info, ser, port = open_devices(args)
    if cap is None and ser is None:
        print("[오류] 카메라와 ToF 둘 다 열지 못했습니다.")
        return 1

    cam_fps, tof_fps = Fps(), Fps()
    tof: ToFFrame | None = None
    last_frame: np.ndarray | None = None
    vmax = args.vmax
    show_invalid, show_text = True, True
    dropped_total = 0

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    print("\n창이 뜨면 손을 카메라 앞에서 움직여 보세요. 좌우가 함께 반응해야 합니다.\n")

    try:
        while True:
            if cap is not None:
                got = camera.read_frame(cap)
                if got is not None:
                    last_frame = got
                    cam_fps.tick()

            if ser is not None:
                drained = tof_reader.drain_latest(ser)
                for status in drained.statuses:
                    print(status.text)
                if drained.frame is not None:
                    tof = drained.frame
                    dropped_total += drained.dropped
                    tof_fps.tick()

            depth = tof.masked_depth() if tof is not None else np.full((GRID, GRID), np.nan)
            heat = render.heatmap(
                depth, size=args.disp_height, vmax=vmax, show_invalid=show_invalid
            )
            if show_text:
                heat = render.annotate_zones(heat, depth)

            if last_frame is not None:
                left = render.fit_height(last_frame, args.disp_height)
            else:
                left = render.placeholder(
                    (int(args.disp_height * 4 / 3), args.disp_height), "카메라 없음"
                )

            canvas = render.side_by_side(left, heat)
            canvas = render.hud(
                canvas,
                build_hud(info, port, args.baud, cam_fps, tof_fps, tof, vmax, dropped_total),
            )
            cv2.imshow(WINDOW, canvas)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("s"):
                print(f"저장: {save_snapshot(args.out, last_frame, tof, heat)}*")
            elif key == ord("["):
                vmax = max(_VMAX_MIN, vmax - _VMAX_STEP)
            elif key == ord("]"):
                vmax = min(_VMAX_MAX, vmax + _VMAX_STEP)
            elif key == ord("n"):
                show_invalid = not show_invalid
            elif key == ord("t"):
                show_text = not show_text

            if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        if cap is not None:
            cap.release()
        if ser is not None:
            ser.close()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
