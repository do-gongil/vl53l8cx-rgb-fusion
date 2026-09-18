/*
 * VL53L8CX 8x8 ToF -> PC 시리얼 (프로토콜 v2)
 *
 * test_0902/src/main.cpp 를 포크해 융합용으로 확장했다. 바뀐 것은 네 가지뿐이다.
 *
 *  1) 921600 baud
 *     status 64개가 늘어 프레임당 약 600 byte. 15 Hz 면 9 KB/s 인데 115200
 *     (11.5 KB/s)은 78% 점유라 여유가 없고 직렬화 지연만 28 ms 가 붙는다.
 *     921600 이면 3.5 ms.
 *
 *  2) esp_timer_get_time() 타임스탬프
 *     millis() 가 아니다. int64 마이크로초라 롤오버가 없다. 데이터 준비가
 *     확인된 **직후** 찍는다. 이 값이 있어야 PC 에서 카메라 프레임과 짝을
 *     지을 수 있다.
 *
 *  3) target_status 원본 전송
 *     구 펌웨어는 status != 5 를 전부 -1 로 뭉갰다. 빔스플리터를 거친
 *     매크로 거리(5~30 cm)에서는 status 6/9(50% 신뢰)가 대량 나오므로
 *     그대로 버리면 그리드가 텅 빈다. 원본을 보내고 임계값은 호스트에서
 *     실시간으로 조절한다. 펌웨어는 분기가 사라져 오히려 단순해졌다.
 *
 *  4) '?' ping 응답과 'L' LED 펄스
 *     '?' -> P,<t_us>  : 호스트가 왕복 시간으로 클럭 오프셋을 추정한다.
 *     'L' -> K,<t_us>  : LED 를 켠 시각을 알려준다. 호스트는 그 LED 가
 *                        영상에 나타난 프레임을 찾아 카메라 지연을 실측한다.
 *
 *  5) Xtalk(광학 crosstalk) 캘리브레이션
 *     빔스플리터 표면에서 VCSEL 광이 곧바로 SPAD 로 되돌아오면 거리 ~0 의
 *     강한 가짜 타깃이 생긴다. 5~30 cm 매크로 거리에서는 시료 반사광과
 *     히스토그램이 겹쳐 측정이 통째로 망가질 수 있다. 보정값은 NVS 에
 *     저장해 부팅 때마다 자동으로 다시 적용한다.
 *     'X[<반사율>,<샘플수>,<거리mm>]' -> 캘리브레이션 후 저장
 *     'C'                             -> 저장본 삭제
 *
 * 출력 (프레임당 한 줄):
 *   F,<t_us>,<seq>,<d0..d63>,<s0..s63>
 *   인덱스 = y*8 + x, 거리 단위 mm (원본), status 는 ST ULD 원본값
 *   NB_TARGET_PER_ZONE=2 로 빌드하면 뒤에 <d..>,<s..> 한 벌이 더 붙는다.
 *   호스트는 필드 개수로 자동 판별한다.
 *
 * 그 밖의 줄:
 *   P,<t_us>   ping 응답
 *   K,<t_us>   LED 점등 시각
 *   XT,ok,<반사율>,<샘플수>,<거리mm>   xtalk 캘리브레이션 성공
 *   XT,err,<코드>,<사유>               실패 (코드는 ULD status)
 *   XT,cleared / XT,none               저장본 삭제 / 저장본 없음
 *   #...       상태 메시지
 */
#include <Arduino.h>
#include <Preferences.h>
#include <Wire.h>
#include <esp_timer.h>
#include <vl53l8cx.h>

#define SDA_PIN 4
#define SCL_PIN 5

/* 카메라 지연 실측용 LED. 카메라 시야 안에 들어오게 외부 LED + 저항을 단다.
 * 보드 내장 RGB LED(GPIO48)는 addressable 이라 단순 digitalWrite 가 안 된다. */
#define LED_PIN 2
#define LED_PULSE_US 50000  // 30 fps 에서 최소 한 프레임에는 확실히 걸린다

#define SERIAL_BAUD 921600
#define ZONES 64

/* Xtalk 보정값 저장 위치 (NVS). 776 byte 라 blob 하나로 충분하다. */
#define XTALK_NS  "tof"
#define XTALK_KEY "xtalk"

/* 기본 캘리브레이션 조건.
 * ST 는 3% 반사율 타깃을 권장한다 (밝은 타깃은 포화돼 오히려 나쁘다).
 * 거리는 ULD 가 600~3000 mm 만 받는다 -- 우리 작동거리(50~300 mm)보다
 * 멀지만 이건 '보정을 측정하는 거리'일 뿐이고, 결과는 전 구간에 적용된다.
 * 타깃이 시야를 꽉 채워야 하므로 600 mm 에서 약 54 x 54 cm 가 필요하다. */
#define XTALK_DEF_REFLECTANCE 3
#define XTALK_DEF_SAMPLES     16
#define XTALK_DEF_DISTANCE_MM 600
#define XTALK_MIN_DISTANCE_MM 600
#define XTALK_MAX_DISTANCE_MM 3000

VL53L8CX sensor(&Wire, -1, -1);  // LPn, I2C_RST 미사용

static uint32_t g_seq = 0;
static int64_t g_led_off_us = 0;  // 0 이면 꺼져 있음

static Preferences g_nvs;
static uint8_t g_xtalk[VL53L8CX_XTALK_BUFFER_SIZE];
static bool g_xtalk_loaded = false;

/* 타깃 2개까지도 다 담을 크기. 타깃당 최대 약 770 byte. */
static char g_line[2048];

/* NVS 에 저장된 Xtalk 보정값을 센서에 다시 밀어넣는다.
 * init() 뒤, start_ranging() 앞에서 불러야 한다. */
static bool loadXtalk() {
  if (!g_nvs.begin(XTALK_NS, /*readOnly=*/true)) return false;
  const size_t n = g_nvs.getBytesLength(XTALK_KEY);
  bool ok = false;
  if (n == sizeof(g_xtalk)) {
    g_nvs.getBytes(XTALK_KEY, g_xtalk, sizeof(g_xtalk));
    ok = (sensor.set_caldata_xtalk(g_xtalk) == 0);
  }
  g_nvs.end();
  return ok;
}

static void clearXtalk() {
  if (!g_nvs.begin(XTALK_NS, /*readOnly=*/false)) {
    Serial.println("XT,err,255,NVS 열기 실패");
    return;
  }
  const bool had = g_nvs.isKey(XTALK_KEY);
  if (had) g_nvs.remove(XTALK_KEY);
  g_nvs.end();
  Serial.println(had ? "XT,cleared" : "XT,none");
  /* RAM 에 이미 적용된 보정은 그대로 남는다. 센서를 되돌리는 API 가 없어
   * 재부팅이 유일하게 확실한 방법이다. 조용히 넘어가면 안 되는 부분. */
  if (had) Serial.println("# 저장본만 지웠다. 재부팅해야 센서에서도 빠진다.");
}

/* Xtalk 캘리브레이션. 수십 초 동안 프레임이 멎으므로 호스트가 끊긴 것으로
 * 오해하지 않도록 시작·끝을 상태 메시지로 알린다. */
static void runXtalk(uint16_t reflectance, uint8_t samples, uint16_t distance) {
  /* ULD 는 인자가 틀리면 127 만 주고 무엇이 틀렸는지는 안 알려준다.
   * 물리 세팅을 다시 잡는 비용이 크므로 여기서 먼저 걸러낸다. */
  if (reflectance < 1 || reflectance > 99) {
    Serial.printf("XT,err,127,반사율 %u 는 1~99 범위 밖\n", reflectance);
    return;
  }
  if (samples < 1 || samples > 16) {
    Serial.printf("XT,err,127,샘플수 %u 는 1~16 범위 밖\n", samples);
    return;
  }
  if (distance < XTALK_MIN_DISTANCE_MM || distance > XTALK_MAX_DISTANCE_MM) {
    Serial.printf("XT,err,127,거리 %u mm 는 %u~%u 범위 밖\n",
                  distance, XTALK_MIN_DISTANCE_MM, XTALK_MAX_DISTANCE_MM);
    return;
  }

  Serial.printf("# xtalk 시작: 반사율 %u%%, 샘플 %u, 거리 %u mm\n",
                reflectance, samples, distance);
  Serial.println("# 수십 초 걸린다. 그동안 프레임이 나오지 않는다.");

  sensor.stop_ranging();
  const uint8_t st = sensor.calibrate_xtalk(reflectance, samples, distance);

  if (st != 0) {
    Serial.printf("XT,err,%u,캘리브레이션 실패 (타깃이 시야를 채우는지 확인)\n", st);
  } else if (sensor.get_caldata_xtalk(g_xtalk) != 0) {
    Serial.println("XT,err,255,보정값 읽기 실패");
  } else if (!g_nvs.begin(XTALK_NS, /*readOnly=*/false)) {
    Serial.println("XT,err,255,NVS 열기 실패");
  } else {
    const size_t written = g_nvs.putBytes(XTALK_KEY, g_xtalk, sizeof(g_xtalk));
    g_nvs.end();
    if (written != sizeof(g_xtalk)) {
      Serial.printf("XT,err,255,NVS 저장 %u/%u byte\n",
                    (unsigned)written, (unsigned)sizeof(g_xtalk));
    } else {
      g_xtalk_loaded = true;
      Serial.printf("XT,ok,%u,%u,%u\n", reflectance, samples, distance);
    }
  }

  /* 성공하든 실패하든 측정은 반드시 되살린다. */
  sensor.start_ranging();
}

/* 'X' 뒤에 붙은 인자를 읽는다. 비어 있으면 기본값. */
static void handleXtalkCommand() {
  String args = Serial.readStringUntil('\n');
  args.trim();
  unsigned r = XTALK_DEF_REFLECTANCE, n = XTALK_DEF_SAMPLES, d = XTALK_DEF_DISTANCE_MM;
  if (args.length() > 0 && sscanf(args.c_str(), "%u,%u,%u", &r, &n, &d) != 3) {
    Serial.println("XT,err,127,인자 형식은 X<반사율>,<샘플수>,<거리mm>");
    return;
  }
  runXtalk((uint16_t)r, (uint8_t)n, (uint16_t)d);
}

/* '?' 와 'L' 은 ranging 읽기보다 먼저 처리한다. ping 응답이 프레임 전송
 * 뒤로 밀리면 왕복 시간에 그만큼 오차가 섞여 클럭 추정이 망가진다. */
static void handleCommands() {
  while (Serial.available() > 0) {
    const int c = Serial.read();
    if (c == '?') {
      Serial.printf("P,%lld\n", (long long)esp_timer_get_time());
    } else if (c == 'L') {
      const int64_t now = esp_timer_get_time();
      digitalWrite(LED_PIN, HIGH);
      g_led_off_us = now + LED_PULSE_US;
      Serial.printf("K,%lld\n", (long long)now);
    } else if (c == 'X') {
      handleXtalkCommand();
    } else if (c == 'C') {
      clearXtalk();
    }
  }
}

/* LED 를 delay 로 끄면 그동안 프레임을 통째로 놓친다. 마감 시각만 보고 넘어간다. */
static void serviceLed() {
  if (g_led_off_us != 0 && esp_timer_get_time() >= g_led_off_us) {
    digitalWrite(LED_PIN, LOW);
    g_led_off_us = 0;
  }
}

static void emitFrame(const VL53L8CX_ResultsData &r, int64_t t_us) {
  int n = snprintf(g_line, sizeof(g_line), "F,%lld,%lu",
                   (long long)t_us, (unsigned long)g_seq++);

  for (int t = 0; t < VL53L8CX_NB_TARGET_PER_ZONE; t++) {
    for (int zone = 0; zone < ZONES; zone++) {
      const int i = zone * VL53L8CX_NB_TARGET_PER_ZONE + t;
      n += snprintf(g_line + n, sizeof(g_line) - n, ",%d", (int)r.distance_mm[i]);
    }
    for (int zone = 0; zone < ZONES; zone++) {
      const int i = zone * VL53L8CX_NB_TARGET_PER_ZONE + t;
      n += snprintf(g_line + n, sizeof(g_line) - n, ",%u", (unsigned)r.target_status[i]);
    }
    /* 버퍼를 넘기면 잘린 줄이 나간다. 호스트는 그걸 버리지만, 조용히
     * 프레임을 잃는 것보다 눈에 보이게 실패하는 편이 낫다. */
    if (n >= (int)sizeof(g_line) - 16) {
      Serial.println("# ERROR: 라인 버퍼 초과 - g_line 을 키우세요");
      return;
    }
  }

  Serial.write((const uint8_t *)g_line, n);
  Serial.write('\n');
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  Serial.setTimeout(50);  // handleXtalkCommand 의 인자 읽기 상한
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  delay(1000);
  Serial.println("# VL53L8CX start");

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(400000);

  sensor.begin();
  Serial.print("# init... ");
  if (sensor.init() != 0) {
    Serial.println("FAILED - check wiring");
    while (1) delay(1000);
  }
  Serial.println("OK");

  sensor.set_resolution(VL53L8CX_RESOLUTION_8X8);
  sensor.set_ranging_frequency_hz(15);  // 8x8 최대치

  /* 저장된 Xtalk 보정값이 있으면 되살린다. start_ranging() 앞이어야 한다. */
  g_xtalk_loaded = loadXtalk();
  Serial.println(g_xtalk_loaded ? "# xtalk: 저장된 보정값 적용"
                                : "# xtalk: 보정값 없음 (X 명령으로 캘리브레이션)");

  sensor.start_ranging();

  Serial.printf("# proto=2 res=8x8 rate=15 ntarget=%d baud=%d xtalk=%s\n",
                VL53L8CX_NB_TARGET_PER_ZONE, SERIAL_BAUD,
                g_xtalk_loaded ? "on" : "off");
}

void loop() {
  handleCommands();
  serviceLed();

  uint8_t ready = 0;
  sensor.check_data_ready(&ready);
  if (!ready) {
    /* ponytail: 폴링 주기가 곧 타임스탬프 양자화 오차다. 1 ms 면 카메라
     * 프레임 간격(33 ms)의 3% 라 짝짓기에 영향이 없다. 더 필요하면
     * VL53L8CX 의 INT 핀을 붙이고 ISR 에서 시각을 찍는다. */
    delay(1);
    return;
  }

  /* 데이터 준비 확인 직후 = 적분 구간의 끝. 적분 시간의 절반만큼 앞선
   * 시각이 진짜 관측 시점이지만, 그건 상수라 호스트의 tof_latency_ms
   * 노브가 흡수한다. 펌웨어에서 추정하지 않는다. */
  const int64_t t_us = esp_timer_get_time();

  VL53L8CX_ResultsData r;
  sensor.get_ranging_data(&r);
  emitFrame(r, t_us);
}
