# 실험 1 햅틱 인지 코드

| 파일 | 역할 |
|---|---|
| `haptic_exp1.ino` | 아두이노 펌웨어. BLE(HM-10)·USB로 진동 명령 수신 → 모터 3개 구동. 9주차 앱 연결 때도 같은 명령 형식 사용 |
| `haptic_exp1.py` | 노트북 실험 진행. 시행표 생성, 무작위 순서로 진동 송신, 실험자가 반응 입력 → CSV 기록 |
| `analyze_exp1.py` | 슬라이드 13 기록 양식 칸 채우기 + 판정 규칙대로 채택안 선택 |
| `exp1_P1~P4_schedule.csv` | 착용자별 시행표 (각 270회 = 9블록 × 30회) |

## 순서
1. 배선 후 `.ino` 업로드 (배선은 파일 상단 주석)
2. `pip install bleak pyserial`
3. `python haptic_exp1.py test --link ble --ble-name HMSoft` → 세기 120/180/255가 실제로 구분되는지 확인하고, 안 되면 `haptic_exp1.py` 상단 `RISK` 값 수정 후 **schedule 다시 생성**
4. `python haptic_exp1.py run --pid P1 --link ble` — 측면 카메라 녹화 시작 후 실행. 시작할 때 LED가 다 켜지면 박수(영상 동기화)
5. 측면 영상에서 `rt_video_ms` = 몸 반응 프레임 − D13 자극 LED 켜진 프레임 을 로그 CSV에 채움
6. `python analyze_exp1.py exp1_P*_log.csv` → `exp1_summary.md`

## 진행 중 입력
- Enter: 진동 (Enter 후 1~4초 무작위 뒤 나감 — 착용자가 타이밍 예측 못 하게. 보행은 출발선 통과 때 Enter)
- 반응: `l` `r` `f` (+ 위험도 블록은 `l2`처럼 숫자), `x` 무반응, `redo` 다시(블록 끝으로), `#메모`
- `p` 핑, `q` 중단 — 다시 run 하면 이어서 진행

## 블록 구성 (기본값, `BLOCKS`에서 수정)
- 1-A 피할 방향 쪽 / 장애물 쪽: 정지. 항상 맨 앞, 착용자마다 순서 번갈아. 첫 3회(세 방향 1번씩)는 매핑 설명 없이
- 1-B 횟수 / 세기 / 간격: 보행 중
- 1-C 정지 / 보행: 같은 진동(횟수 방식)
- 1-D 짧게 100 ms / 길게 400 ms: 정지
- 1-B~1-D 사례 순서·조건 순서는 착용자별 무작위
