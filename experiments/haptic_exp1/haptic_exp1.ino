// 실험 1 햅틱 인지 — 아두이노 진동 장치 펌웨어
// 보드: Arduino Uno/Nano + HM-10 계열 BLE 모듈 (9600 baud)
// 이 명령 형식은 9주차 앱(폰 → BLE) 연결에도 그대로 쓴다.
//
// 배선
//   HM-10 TX -> D2,  HM-10 RX <- D4 (5V→3.3V 분압 저항 권장)
//   모터 모듈 IN: 왼쪽 D5, 목 뒤 D6, 오른쪽 D9 (PWM 핀이어야 세기 조절 가능)
//   모터 표시 LED: 왼쪽 D7, 목 뒤 D8, 오른쪽 D12
//   자극 LED: D13 — 진동이 나가는 동안 켜짐. 측면 카메라에 보이게 둬서
//             반응 시간 = (몸 반응 프레임) - (이 LED 켜진 프레임) 으로 잰다.
//
// 명령 (한 줄, '\n' 끝)
//   V<id>,<m>,<n>,<pwm>,<on>,<gap>   진동. m=L/N/R, n=펄스 수(1~9), pwm=0~255, on/gap=ms
//                                     예) V12,R,2,255,150,150  → 응답 A12,<millis>
//   S                                 모두 정지 → K
//   P                                 핑 → P<millis>
//   F<ms>                             LED 전부 깜빡 (영상 동기화용) → F<millis>
//   새 V 명령이 오면 진행 중이던 진동은 끊고 새 것을 바로 시작한다.

#include <SoftwareSerial.h>

const uint8_t BT_RX = 2, BT_TX = 4;
SoftwareSerial bt(BT_RX, BT_TX);

const uint8_t MOTOR_PIN[3] = {5, 6, 9};   // L, N, R
const uint8_t LED_PIN[3]   = {7, 8, 12};
const uint8_t STIM_LED = 13;

struct Job {
  bool active;
  uint8_t motor, pwm, left;
  uint16_t onMs, gapMs;
  bool phaseOn;
  unsigned long t0;
} job = {false};

unsigned long flashEnd = 0;
bool flashing = false;

char bufUsb[40], bufBt[40];
uint8_t lenUsb = 0, lenBt = 0;

int motorIndex(char c) {
  if (c == 'L') return 0;
  if (c == 'N') return 1;
  if (c == 'R') return 2;
  return -1;
}

void allOff() {
  for (uint8_t i = 0; i < 3; i++) {
    analogWrite(MOTOR_PIN[i], 0);
    digitalWrite(LED_PIN[i], LOW);
  }
  digitalWrite(STIM_LED, LOW);
}

void pulseOn() {
  analogWrite(MOTOR_PIN[job.motor], job.pwm);
  digitalWrite(LED_PIN[job.motor], HIGH);
  digitalWrite(STIM_LED, HIGH);
}

void pulseOff() {
  analogWrite(MOTOR_PIN[job.motor], 0);
  digitalWrite(LED_PIN[job.motor], LOW);
  digitalWrite(STIM_LED, LOW);
}

void handleLine(char *line, Stream &out) {
  char cmd = line[0];
  if (cmd == 'V') {
    long id; char m; int n, pwm; unsigned int on, gap;
    if (sscanf(line + 1, "%ld,%c,%d,%d,%u,%u", &id, &m, &n, &pwm, &on, &gap) != 6) {
      out.println(F("E,parse")); return;
    }
    int mi = motorIndex(m);
    if (mi < 0 || n < 1 || n > 9 || pwm < 0 || pwm > 255 || on < 10 || on > 3000 || gap > 3000) {
      out.println(F("E,range")); return;
    }
    flashing = false;
    allOff();
    job.active = true; job.motor = mi; job.pwm = pwm; job.left = n;
    job.onMs = on; job.gapMs = gap; job.phaseOn = true; job.t0 = millis();
    pulseOn();
    out.print('A'); out.print(id); out.print(','); out.println(job.t0);
  } else if (cmd == 'S') {
    job.active = false; flashing = false; allOff();
    out.println('K');
  } else if (cmd == 'P') {
    out.print('P'); out.println(millis());
  } else if (cmd == 'F') {
    unsigned int ms = 500;
    if (line[1]) ms = atoi(line + 1);
    job.active = false; allOff();
    for (uint8_t i = 0; i < 3; i++) digitalWrite(LED_PIN[i], HIGH);
    digitalWrite(STIM_LED, HIGH);
    flashing = true; flashEnd = millis() + ms;
    out.print('F'); out.println(millis());
  } else {
    out.println(F("E,cmd"));
  }
}

void readStream(Stream &s, char *buf, uint8_t &len) {
  while (s.available()) {
    char c = s.read();
    if (c == '\r') continue;
    if (c == '\n') {
      buf[len] = 0;
      if (len > 0) handleLine(buf, s);
      len = 0;
    } else if (len < 39) {
      buf[len++] = c;
    } else {
      len = 0;  // 너무 긴 줄은 버림
    }
  }
}

void tick() {
  unsigned long now = millis();
  if (flashing && (long)(now - flashEnd) >= 0) { flashing = false; allOff(); }
  if (!job.active) return;
  if (job.phaseOn) {
    if (now - job.t0 >= job.onMs) {
      pulseOff();
      job.left--;
      if (job.left == 0) { job.active = false; return; }
      job.phaseOn = false; job.t0 = now;
    }
  } else if (now - job.t0 >= job.gapMs) {
    pulseOn();
    job.phaseOn = true; job.t0 = now;
  }
}

void setup() {
  for (uint8_t i = 0; i < 3; i++) {
    pinMode(MOTOR_PIN[i], OUTPUT);
    pinMode(LED_PIN[i], OUTPUT);
  }
  pinMode(STIM_LED, OUTPUT);
  allOff();
  Serial.begin(9600);   // USB 연결로도 같은 명령 사용 가능 (정지 상태 테스트용)
  bt.begin(9600);
}

void loop() {
  readStream(Serial, bufUsb, lenUsb);
  readStream(bt, bufBt, lenBt);
  tick();
}
