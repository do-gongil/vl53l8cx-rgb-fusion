# ToF–RGB 동축 융합 (VL53L8CX × 5MP USB 카메라)

빔스플리터로 광축을 겹친 ToF 센서와 RGB 카메라를 PC 에서 동기화·정합하여,
**영상의 특정 지점을 클릭하면 그 지점의 depth 를 읽는** 시스템.

```
                      ┌──────────┐
   Sample ────────────┤  50:50   ├──────── RGB Cam (5MP 1/2.5", f=3.6mm)
                      │    BS    │
                      └────┬─────┘
                           │ 90° 반사
                      VL53L8CX (8×8, 15Hz, 940nm)
                           │ I2C
                       ESP32-S3 ──USB 시리얼──▶ PC
```

광축이 겹치므로 **시차가 0** 이고, 8×8 zone ↔ 픽셀 매핑이 거리에 무관한
**호모그래피 하나**로 끝난다. 전체 설계와 광학 리스크는
`~/.claude/plans/imx291-usb-usb-shimmying-honey.md` 참조.

## 배선

| 연결 | ESP32-S3 | 비고 |
|---|---|---|
| VL53L8CX `SDA` | `GPIO4` | I2C 400 kHz |
| VL53L8CX `SCL` | `GPIO5` | |
| VL53L8CX `VDD` / `GND` | 3V3 / GND | LPn · I2C_RST 미사용 |
| 동기용 LED (+저항) | `GPIO2` | 카메라 지연 실측용. **카메라 시야 안**에 놓을 것 |

보드 내장 RGB LED(`GPIO48`)는 addressable 이라 `digitalWrite` 로 못 쓴다.
핀은 `firmware/src/main.cpp` 상단의 `SDA_PIN` / `SCL_PIN` / `LED_PIN` 에서 바꾼다.

## 빠른 시작

```bash
# 1) 호스트 의존성
cd host && pip install -r requirements.txt

# 2) 두 장치가 살아 있는지 확인 (가장 먼저 할 일)
python scripts/live_check.py

# 3) 펌웨어 빌드/업로드
cd ../firmware && pio run -t upload
```

`live_check.py` 는 **구 펌웨어(`F,d0..d63`)와 신 펌웨어 양쪽을 자동 판별**하므로
펌웨어를 올리기 전에도 그대로 돌아간다. 한쪽 장치만 꽂혀 있어도 실행된다.

| 키 | 동작 |
|---|---|
| `Q` / `ESC` | 종료 |
| `S` | 색상 원본 + 히트맵 + 원시 거리(JSON) 저장 |
| `[` `]` | 컬러맵 범위 |
| `N` | 무효 zone 표시 |
| `T` | zone 숫자 |

## Xtalk 캘리브레이션 (빔스플리터 장착 후 필수)

BS 표면에서 940 nm VCSEL 광이 곧바로 SPAD 로 되돌아오면 **거리 ~0 의 강한 가짜 타깃**이
생긴다. ST 는 커버글라스 crosstalk 내성을 **60 cm 이상**에서만 보장하는데 이 장비의
작동거리는 **5~30 cm** 다. 보정 없이는 시료 반사광과 고스트의 히스토그램이 겹쳐
측정이 통째로 망가질 수 있다.

```bash
cd host
python scripts/calib_xtalk.py              # 기본값 3% / 16샘플 / 600mm
python scripts/calib_xtalk.py --distance 800
python scripts/calib_xtalk.py --clear      # 저장본 삭제
```

| 인자 | 범위 | 비고 |
|---|---|---|
| `--reflectance` | 1~99 | ST 권장 **3** (밝은 타깃은 포화돼 오히려 나쁘다) |
| `--samples` | 1~16 | 많을수록 정확, 그만큼 오래 걸림 |
| `--distance` | 600~3000 mm | ULD 제약. 작동거리보다 멀지만 결과는 전 구간에 적용 |

**타깃이 시야를 꽉 채워야 한다.** 600 mm 에서 약 **54 x 54 cm** 가 필요하다
(ToF 시야 48.5° 정사각). 모자라면 에러가 아니라 *그럴듯한 틀린 값*이 나오므로
스크립트가 실행 전에 점검표를 띄운다.

보정값(776 byte)은 **ESP32 의 NVS 에 저장**되어 부팅 때마다 자동 적용된다.
적용 여부는 부팅 로그의 `xtalk=on` 또는 `# xtalk: 저장된 보정값 적용` 로 확인한다.
광학계를 바꾸면 다시 실행해야 한다.

> `--clear` 는 NVS 저장본만 지운다. 센서 RAM 에 이미 올라간 보정을 되돌리는 API 가
> 없으므로 **재부팅해야 완전히 빠진다.**

## 시리얼 프로토콜 v2

```
F,<t_us>,<seq>,<d0..d63>,<s0..s63>   프레임 (인덱스 = y*8+x, mm, status 원본)
P,<t_us>                             '?' 에 대한 ping 응답
K,<t_us>                             'L' LED 점등 시각 (카메라 지연 실측용)
XT,ok,<반사율>,<샘플수>,<거리mm>  xtalk 캘리브레이션 성공 ('X' 응답)
XT,err,<코드>,<사유>              실패 (코드는 ULD status)
XT,cleared / XT,none             저장본 삭제 / 저장본 없음 ('C' 응답)
#...                                 상태 메시지
```

`t_us` = `esp_timer_get_time()` (int64 µs, 롤오버 없음). 921600 baud.
`NB_TARGET_PER_ZONE=2` 로 빌드하면 `<d..>,<s..>` 한 벌이 더 붙고 호스트가 자동 판별한다.

**status 를 원본 그대로 보내는 이유:** 5=100% 유효, 6·9=50% 신뢰.
빔스플리터를 거친 매크로 거리에서는 6·9 가 대량 나오므로 펌웨어에서 버리면
그리드가 텅 빈다. 필터링은 호스트에서 조절한다 (`DEFAULT_STATUS_ACCEPT=(5,6,9)`).

## 개발

```bash
cd host && python -m pytest        # 80% 커버리지 기준
```

순수 로직(`protocol`, `render`, `tof_reader`)은 테스트로 고정한다.
카메라 I/O 는 실물 없이 목으로 채우지 않고 `--no-camera/--no-tof` 스모크로 대신한다.

## 구조

```
firmware/     PlatformIO (ESP32-S3 + VL53L8CX, 프로토콜 v2)
host/src/toffuse/
  protocol.py   시리얼 라인 파서 (v1/v2 자동 판별)
  render.py     히트맵·HUD (Pillow 로 한글 렌더링)
  camera.py     UVC 열기 (DSHOW→MSMF 폴백)
  tof_reader.py drain-to-latest 수신
  console.py    Windows 한글 출력 보정
host/scripts/
  live_check.py 두 장치 동시 표시 (Phase 0)
  calib_xtalk.py  빔스플리터 crosstalk 보정 (Phase 0.5)
```

## 관련 저장소

센서 단독 최소 예제 — ESP32-S3 + VL53L8CX 를 53줄로 돌리고 matplotlib 히트맵으로 보는 것만
필요하다면 이쪽이 더 간단합니다: **[esp32-vl53l8cx-tof](https://github.com/do-gongil/esp32-vl53l8cx-tof)**

이 저장소의 펌웨어는 그 프로젝트의 `src/main.cpp` 에서 출발해 동기화용으로 확장한 것입니다
(타임스탬프 · status 원본 · ping · LED 펄스). 두 펌웨어는 목적이 달라 각자 유지됩니다 —
저쪽은 최소 예제(프로토콜 v1), 이쪽은 카메라와 짝을 맞추기 위한 계측용(v2).
