"""실험 1 결과 집계 — 슬라이드 13 기록 양식 칸을 채우고 판정 규칙대로 채택안을 고른다.

  python analyze_exp1.py exp1_P*_log.csv            # 주관 평가는 exp1_P*_ratings.csv 자동 탐색

rt_video_ms(측면 영상 반응 시간)는 로그 CSV에 직접 채운 뒤 돌린다. 비어 있으면 반응 시간은 '—'.
판정 규칙: 위치 구분율 ≥ 95%(보행 중 유지), 위험도 구분율 ≥ 90% 충족 조건 중 반응 시간 중앙값이 짧은 것,
동률(차이 50 ms 이내)이면 주관 평가(헷갈림·불쾌감) 평균이 낮은 것.
"""

import csv
import glob
import math
import os
import statistics
import sys

LOC_MIN, LEVEL_MIN, RT_TIE_MS = 0.95, 0.90, 50


def wilson(k, n, z=1.96):
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def rate(rows, key):
    vals = [int(r[key]) for r in rows if r[key] != ""]
    k, n = sum(vals), len(vals)
    return k, n


def fmt_rate(k, n):
    if n == 0:
        return "—"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.0f}% ({k}/{n}, CI {100 * lo:.0f}–{100 * hi:.0f})"


def med_rt(rows):
    v = [float(r["rt_video_ms"]) for r in rows if r.get("rt_video_ms", "").strip()]
    return statistics.median(v) if v else None


def load(paths):
    rows, ratings = [], []
    for p in paths:
        with open(p, encoding="utf-8-sig") as f:
            rows += [r for r in csv.DictReader(f) if r["status"] == "ok"]
        rp = p.replace("_log.csv", "_ratings.csv")
        if os.path.exists(rp):
            with open(rp, encoding="utf-8-sig") as f:
                ratings += list(csv.DictReader(f))
    return rows, ratings


def summarize(rows, ratings):
    out = {}
    for block in dict.fromkeys(r["block"] for r in rows):
        br = [r for r in rows if r["block"] == block]
        loc = rate(br, "dir_ok")
        lvl = rate(br, "level_ok")
        first = [r for r in br if r["trial"] == "1"]
        unexpl = [r for r in br if r["explained"] == "0"]
        rts = [r for r in br if r["explained"] == "1"]
        rb = [x for x in ratings if x["block"] == block]
        conf = [int(x["confusion"]) for x in rb if x["confusion"]]
        disc = [int(x["discomfort"]) for x in rb if x["discomfort"]]
        out[block] = dict(
            case=br[0]["case"], state=br[0]["state"], n=len(br),
            loc=loc, lvl=lvl, first=rate(first, "dir_ok"), unexpl=rate(unexpl, "dir_ok"),
            rt=med_rt(rts),
            conf=statistics.mean(conf) if conf else None,
            disc=statistics.mean(disc) if disc else None,
            per_dir={d: rate([r for r in br if r["avoid_dir"] == d], "dir_ok") for d in ("left", "right", "front")},
        )
    return out


def pick(cands, s, need_level):
    ok = []
    for b in cands:
        x = s[b]
        k, n = x["loc"]
        if n == 0 or k / n < LOC_MIN:
            continue
        if need_level:
            k2, n2 = x["lvl"]
            if n2 == 0 or k2 / n2 < LEVEL_MIN:
                continue
        ok.append(b)
    if not ok:
        return "기준 충족 조건 없음"
    with_rt = [b for b in ok if s[b]["rt"] is not None]
    if not with_rt:
        return "충족: " + ", ".join(ok) + " (반응 시간 미입력)"
    with_rt.sort(key=lambda b: s[b]["rt"])
    best = with_rt[0]
    tied = [b for b in with_rt if s[b]["rt"] - s[best]["rt"] <= RT_TIE_MS]
    if len(tied) > 1:
        def subj(b):
            v = [s[b][k] for k in ("conf", "disc") if s[b][k] is not None]
            return statistics.mean(v) if v else 99
        tied.sort(key=subj)
        return f"{tied[0]} (반응 시간 동률 → 주관 평가)"
    return best


def main(paths):
    if not paths:
        paths = sorted(glob.glob("exp1_*_log.csv"))
    rows, ratings = load(paths)
    if not rows:
        print("집계할 시행 없음")
        return
    pids = sorted({r["pid"] for r in rows})
    s = summarize(rows, ratings)
    ms = lambda v: "—" if v is None else f"{v:.0f} ms"
    sc = lambda v: "—" if v is None else f"{v:.1f} / 5"

    lines = [f"# 실험 1 결과 — 착용자 {len(pids)}명 ({', '.join(pids)})", ""]
    lines += ["| 블록 | 상태 | 시행 | 위치 구분율 (95% CI) | 위험도 구분율 (95% CI) | 반응 시간 중앙값 | 헷갈림 | 불쾌감 |",
              "|---|---|---|---|---|---|---|---|"]
    for b, x in s.items():
        lines.append(f"| {b} | {x['state']} | {x['n']} | {fmt_rate(*x['loc'])} | {fmt_rate(*x['lvl'])} | "
                     f"{ms(x['rt'])} | {sc(x['conf'])} | {sc(x['disc'])} |")

    lines += ["", "## 1-A 방향 매핑"]
    for b in ("1A-avoid", "1A-obstacle"):
        if b in s:
            x = s[b]
            lines.append(f"- {b}: 첫 시행 정답률 {fmt_rate(*x['first'])}, 설명 전 3회 {fmt_rate(*x['unexpl'])}, "
                         f"전체 {fmt_rate(*x['loc'])}, 방향별 " +
                         ", ".join(f"{d} {fmt_rate(*v)}" for d, v in x["per_dir"].items()))
    lines.append(f"- 채택: {pick([b for b in ('1A-avoid', '1A-obstacle') if b in s], s, False)}")

    lines += ["", "## 1-B 위험도 표현 (보행 중)"]
    lines.append(f"- 채택: {pick([b for b in s if s[b]['case'] == '1-B'], s, True)}")

    if "1C-stand" in s and "1C-walk" in s:
        a, w = s["1C-stand"], s["1C-walk"]
        pct = lambda kn: 100 * kn[0] / kn[1] if kn[1] else float("nan")
        lines += ["", "## 1-C 보행 상태",
                  f"- 보행 중 감소: 위치 {pct(a['loc']) - pct(w['loc']):.1f}%p, "
                  f"위험도 {pct(a['lvl']) - pct(w['lvl']):.1f}%p, "
                  f"반응 시간 {ms(a['rt'])} → {ms(w['rt'])}",
                  f"- 보행 중 위치 구분율 ≥ {LOC_MIN:.0%} 유지: {'예' if pct(w['loc']) >= 100 * LOC_MIN else '아니오'}"]

    lines += ["", "## 1-D 진동 길이",
              f"- 채택: {pick([b for b in ('1D-short', '1D-long') if b in s], s, False)}"]

    lines += ["", "주의: 착용자 4명 시행은 서로 독립이 아니므로 CI는 참고용. 착용자별 값도 같이 볼 것."]
    lines += ["", "## 착용자별 위치 구분율"]
    for pid in pids:
        ps = summarize([r for r in rows if r["pid"] == pid], [])
        lines.append(f"- {pid}: " + ", ".join(f"{b} {100 * x['loc'][0] / x['loc'][1]:.0f}%" for b, x in ps.items() if x["loc"][1]))

    text = "\n".join(lines)
    print(text)
    with open("exp1_summary.md", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n→ exp1_summary.md 저장")


if __name__ == "__main__":
    main(sys.argv[1:])
