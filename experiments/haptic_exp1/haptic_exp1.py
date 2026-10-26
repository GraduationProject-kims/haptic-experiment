"""실험 1 햅틱 인지 — 노트북에서 진동 명령을 무작위 순서로 보내고 반응을 기록한다.

영상·모델 없이 진동만 보낸다. 반응 시간의 정답은 측면 영상(자극 LED 켜짐 → 몸 반응)이고,
이 스크립트는 '무엇을 언제 보냈고 착용자가 어떻게 반응했는지'를 남긴다.

사용 예
  pip install bleak pyserial
  python haptic_exp1.py schedule --pids P1 P2 P3 P4          # 시행표 생성
  python haptic_exp1.py test --link ble --ble-name HMSoft      # 모터 3개·세기 확인
  python haptic_exp1.py run --pid P1 --link ble --ble-name HMSoft
  python haptic_exp1.py run --pid P1 --link mock               # 장치 없이 흐름만 확인

진동 패턴 값(세기·길이·간격)은 아래 '패턴 정의'만 고치면 된다. 세기는 실제 모터로 먼저 보정할 것.
"""

import argparse
import asyncio
import csv
import os
import queue
import random
import sys
import threading
import time
from datetime import datetime

# ───────────────────────── 패턴 정의 (여기만 고치면 됨) ─────────────────────────
# 진동 1회 = (펄스 수 n, 세기 pwm 0~255, 켜짐 on_ms, 쉼 gap_ms)

DIRS = ["left", "right", "front"]            # 피할 방향 = 정답 반응
RESP_CODE = {"l": "left", "r": "right", "f": "front"}   # 실험자 입력: l/r/f (+ 위험도 숫자)

# 1-A 방향 매핑: 피할 방향 → 울릴 모터
MAPPINGS = {
    "avoid":    {"left": "L", "right": "R", "front": "N"},  # 피할 방향 쪽 진동
    "obstacle": {"left": "R", "right": "L", "front": "N"},  # 장애물 쪽 진동 (왼쪽으로 피해야 = 장애물이 오른쪽)
}

# 1-B 위험도 표현: 레벨 1~3
RISK = {
    "count":     {1: (1, 255, 150, 150), 2: (2, 255, 150, 150), 3: (3, 255, 150, 150)},
    "intensity": {1: (1, 120, 300, 0),   2: (1, 180, 300, 0),   3: (1, 255, 300, 0)},
    "interval":  {1: (3, 255, 100, 400), 2: (3, 255, 100, 200), 3: (3, 255, 100, 60)},
}

PULSE_DEFAULT = (1, 255, 200, 0)   # 위험도 없이 위치만 볼 때
PULSE_SHORT = (1, 255, 100, 0)     # 1-D
PULSE_LONG = (1, 255, 400, 0)      # 1-D

# 블록 = 한 조건. state: stand(눈 가린 정지) / walk(0.8 m/s 보행)
BLOCKS = {
    "1A-avoid":     dict(case="1-A", state="stand", mapping="avoid",    risk=None,        pulse=PULSE_DEFAULT),
    "1A-obstacle":  dict(case="1-A", state="stand", mapping="obstacle", risk=None,        pulse=PULSE_DEFAULT),
    "1B-count":     dict(case="1-B", state="walk",  mapping="avoid",    risk="count",     pulse=None),
    "1B-intensity": dict(case="1-B", state="walk",  mapping="avoid",    risk="intensity", pulse=None),
    "1B-interval":  dict(case="1-B", state="walk",  mapping="avoid",    risk="interval",  pulse=None),
    "1C-stand":     dict(case="1-C", state="stand", mapping="avoid",    risk="count",     pulse=None),
    "1C-walk":      dict(case="1-C", state="walk",  mapping="avoid",    risk="count",     pulse=None),
    "1D-short":     dict(case="1-D", state="stand", mapping="avoid",    risk=None,        pulse=PULSE_SHORT),
    "1D-long":      dict(case="1-D", state="stand", mapping="avoid",    risk=None,        pulse=PULSE_LONG),
}

TRIALS_PER_BLOCK = 30
UNEXPLAINED_TRIALS = 3        # 1-A: 블록 첫 3회(세 방향 1번씩)는 매핑 설명 없이 진행
DELAY = {"stand": (1.0, 4.0), "walk": (1.5, 3.5)}   # Enter 후 진동까지 무작위 대기(s) — 예측 방지

BLE_CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"   # HM-10 기본 특성

LOG_FIELDS = [
    "pid", "seq", "block", "case", "state", "trial", "explained",
    "avoid_dir", "motor", "level", "n", "pwm", "on_ms", "gap_ms",
    "delay_s", "sent_at", "ack_ms", "rtt_ms",
    "resp_dir", "resp_level", "dir_ok", "level_ok", "status", "note",
    "rt_video_ms",   # 측면 영상에서 나중에 채움: 몸 반응 프레임 - 자극 LED 켜진 프레임
]


# ───────────────────────── 연결 ─────────────────────────
class _LineLink:
    def __init__(self):
        self.q = queue.Queue()
        self._buf = ""

    def _feed(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                self.q.put((line, time.perf_counter()))

    def wait_for(self, prefix, timeout=1.0):
        end = time.perf_counter() + timeout
        while True:
            left = end - time.perf_counter()
            if left <= 0:
                return None, None
            try:
                line, t = self.q.get(timeout=left)
            except queue.Empty:
                return None, None
            if line.startswith(prefix):
                return line, t
            if line.startswith("E"):
                print(f"  [장치 오류] {line}")

    def drain(self):
        while not self.q.empty():
            self.q.get_nowait()


class SerialLink(_LineLink):
    def __init__(self, port):
        super().__init__()
        import serial
        self.ser = serial.Serial(port, 9600, timeout=0.05)
        time.sleep(2.0)   # 아두이노 리셋 대기
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            data = self.ser.read(64)
            if data:
                self._feed(data.decode(errors="ignore"))

    def send(self, line):
        self.ser.write((line + "\n").encode())

    def close(self):
        self.ser.close()


class BleLink(_LineLink):
    def __init__(self, name=None, address=None):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self._run(self._connect(name, address), 25)

    def _run(self, coro, timeout=5):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _connect(self, name, address):
        from bleak import BleakClient, BleakScanner
        if address:
            dev = await BleakScanner.find_device_by_address(address, timeout=10)
        else:
            dev = await BleakScanner.find_device_by_name(name, timeout=10)
        if dev is None:
            raise RuntimeError(f"BLE 장치를 못 찾음: {address or name}")
        self.client = BleakClient(dev)
        await self.client.connect()
        await self.client.start_notify(BLE_CHAR, lambda _, d: self._feed(d.decode(errors="ignore")))
        print(f"BLE 연결: {dev.name} ({dev.address})")

    async def _write(self, data):
        for i in range(0, len(data), 20):   # HM-10 패킷 20바이트
            await self.client.write_gatt_char(BLE_CHAR, data[i:i + 20], response=False)

    def send(self, line):
        self._run(self._write((line + "\n").encode()))

    def close(self):
        try:
            self._run(self.client.disconnect())
        except Exception:
            pass


class MockLink(_LineLink):
    """장치 없이 흐름 확인용. 30 ms 뒤 응답."""
    def send(self, line):
        def reply():
            time.sleep(0.03)
            ms = int(time.perf_counter() * 1000)
            if line[0] == "V":
                self._feed(f"A{line[1:].split(',')[0]},{ms}\n")
            elif line[0] in "PF":
                self._feed(f"{line[0]}{ms}\n")
            elif line[0] == "S":
                self._feed("K\n")
        threading.Thread(target=reply, daemon=True).start()

    def close(self):
        pass


def open_link(args):
    if args.link == "ble":
        return BleLink(name=args.ble_name, address=args.ble_address)
    if args.link == "serial":
        return SerialLink(args.port)
    return MockLink()


def vibrate(link, seq, motor, n, pwm, on, gap):
    """진동 명령 송신. (보낸 시각, 아두이노 millis, 왕복 ms) 반환. 응답 없으면 ack=None."""
    link.drain()
    t0 = time.perf_counter()
    sent_at = datetime.now().isoformat(timespec="milliseconds")
    link.send(f"V{seq},{motor},{n},{pwm},{on},{gap}")
    line, t1 = link.wait_for(f"A{seq},", timeout=1.0)
    if line is None:
        return sent_at, None, None
    return sent_at, int(line.split(",")[1]), round((t1 - t0) * 1000, 1)


# ───────────────────────── 시행표 ─────────────────────────
def stimulus(block, avoid_dir, level):
    b = BLOCKS[block]
    motor = MAPPINGS[b["mapping"]][avoid_dir]
    n, pwm, on, gap = RISK[b["risk"]][level] if b["risk"] else b["pulse"]
    return motor, n, pwm, on, gap


def make_trials(block, rng):
    b = BLOCKS[block]
    combos = [(d, lv) for d in DIRS for lv in ([1, 2, 3] if b["risk"] else [""])]
    trials = []
    while len(trials) < TRIALS_PER_BLOCK:         # 조합을 최대한 고르게
        c = combos[:]
        rng.shuffle(c)
        trials += c
    trials = trials[:TRIALS_PER_BLOCK]
    rng.shuffle(trials)
    if b["case"] == "1-A":                         # 설명 없는 첫 3회에 세 방향이 한 번씩 오도록
        first = [d for d in DIRS]
        rng.shuffle(first)
        rest = trials[:]
        for d in first:
            rest.remove((d, ""))
        trials = [(d, "") for d in first] + rest
    return trials


def block_order(pid_index, rng):
    """1-A를 맨 앞에 두고(첫 시행 직관성), 매핑 순서는 착용자마다 번갈아. 나머지 사례·조건 순서는 무작위."""
    a = ["1A-avoid", "1A-obstacle"]
    if pid_index % 2 == 1:
        a.reverse()
    cases = {}
    for name, b in BLOCKS.items():
        if b["case"] != "1-A":
            cases.setdefault(b["case"], []).append(name)
    order_cases = list(cases)
    rng.shuffle(order_cases)
    rest = []
    for c in order_cases:
        v = cases[c][:]
        rng.shuffle(v)
        rest += v
    return a + rest


def write_schedule(pid, pid_index, seed):
    rng = random.Random(f"{seed}-{pid}")
    rows, seq = [], 0
    for block in block_order(pid_index, rng):
        b = BLOCKS[block]
        for i, (d, lv) in enumerate(make_trials(block, rng), 1):
            seq += 1
            motor, n, pwm, on, gap = stimulus(block, d, lv)
            explained = 0 if (b["case"] == "1-A" and i <= UNEXPLAINED_TRIALS) else 1
            rows.append(dict(pid=pid, seq=seq, block=block, case=b["case"], state=b["state"],
                             trial=i, explained=explained, avoid_dir=d, motor=motor, level=lv,
                             n=n, pwm=pwm, on_ms=on, gap_ms=gap))
    path = f"exp1_{pid}_schedule.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path, rows


def read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ───────────────────────── 진행 ─────────────────────────
BLOCK_GUIDE = {
    "1-A": "착용자에게 '진동이 오면 바로 몸으로 피하세요'만 말하고 매핑은 설명하지 않는다 (첫 3회).",
    "1-B": "블록 시작 전 d로 레벨 1→2→3 예시를 들려준다. 반응: 방향 + 말로 레벨 (예: r2).",
    "1-C": "정지/보행만 다르고 진동은 같다. 반응: 방향 + 레벨.",
    "1-D": "진동 길이만 다르다. 반응: 방향. 블록 끝에 불쾌감 점수.",
}


def ask(prompt):
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def parse_resp(s, need_level):
    """'l', 'r2', 'f3', 'x'(못 느낌/반응 없음) → (dir, level) 또는 None."""
    if s == "x":
        return "none", ""
    if not s or s[0] not in RESP_CODE:
        return None
    d = RESP_CODE[s[0]]
    lv = s[1:]
    if need_level:
        if lv not in ("1", "2", "3"):
            return None
        return d, lv
    return (d, "") if lv == "" else None


def demo_levels(link, block):
    risk = BLOCKS[block]["risk"]
    if not risk:
        return
    for lv in (1, 2, 3):
        print(f"  예시: 레벨 {lv}")
        n, pwm, on, gap = RISK[risk][lv]
        vibrate(link, 0, "N", n, pwm, on, gap)
        time.sleep(2.0)


def append_row(path, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def run(args):
    sched_path = f"exp1_{args.pid}_schedule.csv"
    if not os.path.exists(sched_path):
        print(f"{sched_path} 없음 → 먼저 schedule 실행")
        return
    sched = read_csv(sched_path)
    log_path = f"exp1_{args.pid}_log.csv"
    rating_path = f"exp1_{args.pid}_ratings.csv"
    done = {r["seq"] for r in read_csv(log_path) if r["status"] == "ok"} if os.path.exists(log_path) else set()
    todo = [r for r in sched if r["seq"] not in done]
    if done:
        print(f"이어서 진행: 완료 {len(done)}회, 남은 {len(todo)}회")

    link = open_link(args)
    rng = random.Random()
    try:
        link.send("F800")   # 동기화: LED 전부 0.8초 켜짐 — 이때 박수
        line, _ = link.wait_for("F", 1.0)
        print(f"[동기화] LED 깜빡 + 박수 ({datetime.now().isoformat(timespec='milliseconds')}) → 측면 영상 기준점")

        current = None
        i = 0
        while i < len(todo):
            t = todo[i]
            b = t["block"]
            need_level = BLOCKS[b]["risk"] is not None
            if b != current:
                if current is not None:
                    rate_block(rating_path, args.pid, current)
                current = b
                left = sum(1 for x in todo if x["block"] == b)
                print(f"\n══ 블록 {b}  ({BLOCKS[b]['state']}, 남은 {left}회) ══")
                print("  " + BLOCK_GUIDE[t["case"]])
                if ask("  d=레벨 예시, Enter=시작, q=종료: ") == "d":
                    demo_levels(link, b)

            if t["case"] == "1-A" and t["trial"] == str(UNEXPLAINED_TRIALS + 1):
                m = BLOCKS[b]["mapping"]
                print(f"  ▶ 지금 매핑 설명: {'진동 온 쪽으로 피한다' if m == 'avoid' else '진동 온 반대쪽으로 피한다'}, 목 뒤 = 정면 대응")

            where = "출발선 통과 시" if t["state"] == "walk" else "준비되면"
            cmd = ask(f"  [{t['trial']:>2}/{TRIALS_PER_BLOCK}] {where} Enter (p=핑, q=종료): ")
            if cmd == "q":
                break
            if cmd == "p":
                link.drain(); t0 = time.perf_counter(); link.send("P")
                line, t1 = link.wait_for("P", 1.0)
                print(f"    핑 {'응답 없음' if line is None else f'{(t1 - t0) * 1000:.0f} ms'}")
                continue

            delay = round(rng.uniform(*DELAY[t["state"]]), 2)
            time.sleep(delay)
            sent_at, ack, rtt = vibrate(link, t["seq"], t["motor"], t["n"], t["pwm"], t["on_ms"], t["gap_ms"])
            if ack is None:
                print("    ⚠ 장치 응답 없음 (진동이 나갔는지 확인)")

            while True:
                hint = "방향+레벨 (l1 r2 f3)" if need_level else "방향 (l r f)"
                s = ask(f"    반응 {hint}, x=무반응, redo=다시, 메모는 '#내용': ")
                note = ""
                if "#" in s:
                    s, note = [p.strip() for p in s.split("#", 1)]
                if s == "redo":
                    row = dict(t, delay_s=delay, sent_at=sent_at, ack_ms=ack or "", rtt_ms=rtt or "",
                               resp_dir="", resp_level="", dir_ok="", level_ok="", status="redo",
                               note=note, rt_video_ms="")
                    append_row(log_path, {k: row.get(k, "") for k in LOG_FIELDS})
                    last = max(j for j, x in enumerate(todo) if x["block"] == b)
                    todo.insert(last + 1, t)   # 같은 블록 맨 끝에서 다시
                    print("    → 블록 끝에서 다시 진행")
                    break
                r = parse_resp(s, need_level)
                if r is None:
                    print("    입력 형식 확인")
                    continue
                rd, rl = r
                row = dict(t, delay_s=delay, sent_at=sent_at, ack_ms=ack if ack is not None else "",
                           rtt_ms=rtt if rtt is not None else "", resp_dir=rd, resp_level=rl,
                           dir_ok=int(rd == t["avoid_dir"]),
                           level_ok=(int(rl == t["level"]) if need_level and rd != "none" else ""),
                           status="ok" if ack is not None else "noack", note=note, rt_video_ms="")
                append_row(log_path, {k: row.get(k, "") for k in LOG_FIELDS})
                break
            i += 1
        else:
            if current:
                rate_block(rating_path, args.pid, current)
            print("\n모든 시행 완료")
    finally:
        link.send("S")
        link.close()
    print(f"기록: {log_path}  /  주관 평가: {rating_path}")


def rate_block(path, pid, block):
    print(f"  블록 {block} 끝 — 착용자 주관 평가 (1=전혀 아님 ~ 5=매우)")
    vals = {}
    for key, label in (("confusion", "헷갈림"), ("discomfort", "불쾌감")):
        while True:
            v = ask(f"    {label} 1~5 (건너뛰기 Enter): ")
            if v == "" or v in "12345" and len(v) == 1:
                vals[key] = v
                break
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["pid", "block", "confusion", "discomfort"])
        if new:
            w.writeheader()
        w.writerow(dict(pid=pid, block=block, **vals))


def test(args):
    """모터 3개를 세기별로 한 번씩 — 착용 전 보정용."""
    link = open_link(args)
    try:
        for m, label in (("L", "왼쪽"), ("N", "목 뒤"), ("R", "오른쪽")):
            for pwm in (120, 180, 255):
                print(f"{label} pwm={pwm}")
                _, ack, rtt = vibrate(link, 0, m, 1, pwm, 400, 0)
                print(f"   응답 {'없음' if ack is None else f'{rtt} ms'}")
                time.sleep(1.2)
    finally:
        link.send("S")
        link.close()


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("schedule")
    s.add_argument("--pids", nargs="+", required=True)
    s.add_argument("--seed", default="exp1")

    for name in ("run", "test"):
        r = sub.add_parser(name)
        if name == "run":
            r.add_argument("--pid", required=True)
        r.add_argument("--link", choices=["ble", "serial", "mock"], default="ble")
        r.add_argument("--ble-name", default="HMSoft")
        r.add_argument("--ble-address")
        r.add_argument("--port", default="/dev/tty.usbmodem1101")

    args = p.parse_args()
    if args.cmd == "schedule":
        for idx, pid in enumerate(args.pids):
            path, rows = write_schedule(pid, idx, args.seed)
            order = list(dict.fromkeys(r["block"] for r in rows))
            print(f"{path}: {len(rows)}회  순서 {' → '.join(order)}")
    elif args.cmd == "run":
        run(args)
    else:
        test(args)


if __name__ == "__main__":
    main()
