"""VL53L8CX Xtalk(광학 crosstalk) 캘리브레이션 실행기.

**빔스플리터를 끼운 상태에서 반드시 한 번 돌려야 한다.**

BS 표면에서 940 nm VCSEL 광이 곧바로 SPAD 로 되돌아오면 거리 ~0 의 강한
가짜 타깃이 생긴다. ST 는 커버글라스 crosstalk 내성을 60 cm 이상에서만
보장하는데, 이 장비의 작동거리는 5~30 cm 다. 보정 없이는 시료 반사광과
고스트의 히스토그램이 겹쳐 측정이 통째로 망가질 수 있다.

보정값(776 byte)은 ESP32 의 NVS 에 저장되어 부팅 때마다 자동 적용된다.
따라서 이 스크립트는 **광학계를 바꿀 때만** 다시 돌리면 된다.

물리 세팅이 조금만 틀려도 ULD 는 그럴듯한 쓰레기 값을 내놓는다. 그래서
실행 전에 점검표를 띄우고 사용자 확인을 받는다.

사용법::

    python scripts/calib_xtalk.py                     # 기본값 (3%, 16샘플, 600mm)
    python scripts/calib_xtalk.py --distance 800
    python scripts/calib_xtalk.py --reflectance 5 --samples 8
    python scripts/calib_xtalk.py --clear             # 저장된 보정값 삭제
    python scripts/calib_xtalk.py --yes               # 점검표 확인 생략
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toffuse import tof_reader  # noqa: E402
from toffuse.console import setup_stdout  # noqa: E402
from toffuse.protocol import XtalkResult  # noqa: E402

# ULD 가 받아주는 범위. 어차피 펌웨어가 다시 검사하지만, 물리 세팅을 다
# 잡아놓고 나서 거부당하면 낭비라 여기서 먼저 걸러낸다.
REFLECTANCE_RANGE = (1, 99)
SAMPLES_RANGE = (1, 16)
DISTANCE_RANGE_MM = (600, 3000)

# 캘리브레이션은 수십 초 걸린다. 넉넉히 잡되 무한정 기다리지는 않는다.
_TIMEOUT_S = 180.0
_POLL_S = 0.05

# ToF 정사각 시야 한 변은 48.5도. tan(24.25도) = 0.4505
_FOV_TAN_HALF = 0.4505


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", default=None, help="시리얼 포트 예: COM7 (기본: 자동 탐지)")
    ap.add_argument("--baud", type=int, default=921600, help="시리얼 속도 (v2 펌웨어 기본)")
    ap.add_argument("--reflectance", type=int, default=3, help="타깃 반사율 (1~99, ST 권장 3)")
    ap.add_argument("--samples", type=int, default=16, help="샘플 수 (1~16, 많을수록 정확)")
    ap.add_argument("--distance", type=int, default=600, help="타깃 거리 mm (600~3000)")
    ap.add_argument("--clear", action="store_true", help="저장된 보정값을 삭제한다")
    ap.add_argument("--yes", action="store_true", help="점검표 확인을 생략한다")
    return ap.parse_args()


def validate(args: argparse.Namespace) -> str | None:
    """범위를 벗어나면 사유를 돌려준다. 통과하면 None."""
    checks = (
        ("반사율", args.reflectance, REFLECTANCE_RANGE, "%"),
        ("샘플 수", args.samples, SAMPLES_RANGE, ""),
        ("거리", args.distance, DISTANCE_RANGE_MM, " mm"),
    )
    for name, value, (lo, hi), unit in checks:
        if not lo <= value <= hi:
            return f"{name} {value}{unit} 는 {lo}~{hi}{unit} 범위 밖입니다."
    return None


def target_size_mm(distance_mm: int) -> int:
    """그 거리에서 시야를 꽉 채우려면 타깃 한 변이 몇 mm 여야 하는가.

    ST 문서: "The target must stay in Full FOV". 이걸 못 채우면 보정값이
    조용히 틀린다 -- 에러가 아니라 그럴듯한 쓰레기가 나온다.
    """
    return round(2 * distance_mm * _FOV_TAN_HALF)


def checklist(args: argparse.Namespace) -> list[str]:
    side = target_size_mm(args.distance)
    return [
        "",
        "─" * 64,
        " Xtalk 캘리브레이션 준비 점검",
        "─" * 64,
        "  1. 빔스플리터가 최종 위치에 장착되어 있는가?",
        "     (광학계를 나중에 바꾸면 이 보정은 무효가 된다)",
        f"  2. 반사율 {args.reflectance}% 타깃을 센서에서 {args.distance} mm 에 두었는가?",
        f"  3. 타깃이 시야를 꽉 채우는가? 최소 {side} x {side} mm 필요.",
        "     (모자라면 에러가 아니라 '그럴듯한 틀린 값'이 나온다)",
        "  4. 타깃이 센서 광축에 수직인가?",
        "  5. 940 nm 외부 광원(햇빛·백열등)이 들어오지 않는가?",
        "─" * 64,
    ]


def send_and_wait(ser: serial.Serial, command: str, label: str) -> XtalkResult | None:
    """명령을 보내고 XT 응답이 올 때까지 상태 메시지를 중계한다."""
    ser.reset_input_buffer()
    ser.write(command.encode("ascii"))
    ser.flush()
    print(f"[{label}] 전송: {command.strip()!r}\n")

    deadline = time.monotonic() + _TIMEOUT_S
    while time.monotonic() < deadline:
        drained = tof_reader.drain_latest(ser)
        for status in drained.statuses:
            print("   ", status.text)
        if drained.xtalk is not None:
            return drained.xtalk
        time.sleep(_POLL_S)

    print(f"[오류] {_TIMEOUT_S:.0f}초 안에 응답이 없습니다.")
    return None


def report(result: XtalkResult) -> int:
    """사람이 다음에 뭘 해야 할지 알 수 있게 결과를 푼다."""
    if not result.ok:
        print(f"\n[실패] ULD status {result.code}: {result.message}")
        if result.code == 127:
            print("       인자가 범위를 벗어났습니다. --help 로 범위를 확인하세요.")
        else:
            print("       타깃이 시야를 꽉 채우는지, 반사율이 맞는지 다시 확인하세요.")
        return 1

    if result.reflectance_percent is None:
        if result.message == "cleared":
            print("\n[완료] 저장된 보정값을 삭제했습니다.")
            print("       ESP32 를 재부팅해야 센서에서도 완전히 빠집니다.")
        else:
            print("\n[정보] 저장된 보정값이 없습니다.")
        return 0

    print(
        f"\n[완료] 보정값을 NVS 에 저장했습니다 "
        f"(반사율 {result.reflectance_percent}%, "
        f"샘플 {result.nb_samples}, 거리 {result.distance_mm} mm)"
    )
    print("       부팅할 때마다 자동 적용됩니다. 광학계를 바꾸면 다시 실행하세요.")
    print("       확인: live_check.py 를 띄우고 부팅 로그의 'xtalk=on' 을 보세요.")
    return 0


def main() -> int:
    setup_stdout()
    args = parse_args()

    if not args.clear:
        problem = validate(args)
        if problem is not None:
            print(f"[오류] {problem}")
            return 2

    port = args.port or tof_reader.find_port()
    if port is None:
        print("[오류] USB 시리얼 포트를 찾지 못했습니다. --port 로 지정하세요.")
        for line in tof_reader.list_usb_ports():
            print(f"       후보: {line}")
        return 1

    if args.clear:
        command, label = "C\n", "삭제"
    else:
        if not args.yes:
            print("\n".join(checklist(args)))
            if input("\n위 항목을 모두 확인했습니까? [y/N] ").strip().lower() != "y":
                print("취소했습니다.")
                return 130
        command = f"X{args.reflectance},{args.samples},{args.distance}\n"
        label = "캘리브레이션"

    try:
        with tof_reader.open_serial(port, args.baud) as ser:
            print(f"[포트] {port} @ {args.baud}")
            result = send_and_wait(ser, command, label)
    except serial.SerialException as exc:
        print(f"[오류] {port} 열기 실패: {exc}")
        return 1

    return report(result) if result is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
