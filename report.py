"""리포트: pass vs control 비교 + 사전 합격기준 판정. `python report.py [db경로]` 로도 실행 가능."""
import csv
import io
import os
import random
import sqlite3
import statistics as st
import sys
import time
from datetime import datetime, timedelta, timezone

COST = float(os.environ.get("ROUNDTRIP_COST", "0.03"))   # 왕복 비용 가정(수수료+슬리피지) 3%
HORIZONS = [1, 6, 24]
KST = timezone(timedelta(hours=9))

# 사전 등록 합격 기준 — 테스트 시작 전에 고정. 결과를 보고 바꾸지 않는다.
CRITERIA_TEXT = """[사전 합격 기준 — 하나라도 FAIL이면 폐기]
 C1. pass 표본 30건 이상 (6시간 수익률 측정 완료 기준)
 C2. pass 그룹 6시간 순수익률(비용 {cost:.0%} 차감) 중앙값 > 0
 C3. pass 그룹 러그 비율이 control 대비 10%p 이상 낮을 것
 C4. 가이드 청산규칙(거래량 비율<0.2 또는 24h) 시뮬레이션 평균 순수익률 > 0, 그리고 부트스트랩 95% 하한 > -5%
""".format(cost=COST)


def kst(ts):
    return datetime.fromtimestamp(ts, KST).strftime("%m-%d %H:%M KST")


def load(c):
    ents = c.execute("SELECT e.net,e.pool,e.grp,e.ts,e.price,e.liq,k.hard_reason,k.chain_reason,k.age_min,k.mcap "
                     "FROM entries e LEFT JOIN checks k ON k.id=e.check_id").fetchall()
    snaps = {}
    for net, pool, grp, ts, price, liq, v6, v24, miss in c.execute(
            "SELECT net,pool,grp,ts,price,liq,vol_h6,vol_h24,missing FROM snaps ORDER BY ts"):
        snaps.setdefault((net, pool, grp), []).append((ts, price, liq, v6, v24, miss))
    out = []
    for net, pool, grp, ts, price, liq, hr, cr, age, mcap in ents:
        out.append(dict(net=net, pool=pool, grp=grp, ts=ts, price=price, liq=liq, hard=hr, chain=cr,
                        age=age, mcap=mcap, snaps=snaps.get((net, pool, grp), [])))
    return out


def at_horizon(e, h):
    """entry 후 h시간 시점 스냅(허용오차 ±30분). 풀이 사라졌으면 -100%로 처리."""
    target = e["ts"] + h * 3600
    for s in e["snaps"]:
        if s[0] >= target - 450:
            if s[0] > target + 1800:
                return None
            if s[5] or s[1] is None:
                return -1.0
            return s[1] / e["price"] - 1
    return None


def is_rug(e):
    """1/6/24h 시점만 사용(두 그룹 동일한 해상도): 가격 -80% 이하, 유동성 80%+ 감소, 또는 풀 소멸."""
    seen = False
    for h in HORIZONS:
        target = e["ts"] + h * 3600
        s = next((s for s in e["snaps"] if target - 450 <= s[0] <= target + 1800), None)
        if not s:
            continue
        seen = True
        if s[5] or s[1] is None:
            return True
        if s[1] <= e["price"] * 0.2:
            return True
        if e["liq"] and s[2] is not None and s[2] <= e["liq"] * 0.2:
            return True
    return False if seen else None


def exit_sim(e, stop=None):
    """가이드 RISK 규칙: vol_h6 / (vol_h24/4) < 0.2 이면 청산, 아니면 24h 시간청산. 15분 해상도."""
    s_list = [s for s in e["snaps"] if s[0] <= e["ts"] + 24 * 3600 + 900]
    if not s_list or s_list[-1][0] < e["ts"] + 24 * 3600 - 900:
        return None  # 아직 24h 안 지남
    for s in s_list:
        if s[5] or s[1] is None:
            return -1.0, s[0]
        r = s[1] / e["price"] - 1
        if stop is not None and r <= -stop:
            return r, s[0]
        if s[4] and s[3] is not None and s[3] / (s[4] / 4) < 0.2:
            return r, s[0]
    last = s_list[-1]
    return last[1] / e["price"] - 1, last[0]


def boot_ci(xs, fn=st.mean, n=2000):
    if len(xs) < 5:
        return None
    rnd = random.Random(7)
    vals = sorted(fn([rnd.choice(xs) for _ in xs]) for _ in range(n))
    return vals[int(0.025 * n)], vals[int(0.975 * n)]


def summ(xs):
    if not xs:
        return "n=0"
    net = [x - COST for x in xs]
    ci = boot_ci(net)
    ci_s = f"  평균95%CI[{ci[0]:+.1%},{ci[1]:+.1%}]" if ci else ""
    return (f"n={len(xs):4d}  중앙 {st.median(xs):+7.1%}  평균 {st.mean(xs):+7.1%}  "
            f"순수익>0 비율 {sum(1 for x in net if x > 0) / len(net):5.1%}{ci_s}")


def build_report(c):
    E = load(c)
    started = float((c.execute("SELECT v FROM meta WHERE k='started_at'").fetchone() or [time.time()])[0])
    L = []
    L.append(f"밈코인 필터 포워드 테스트 리포트 — {kst(time.time())} (시작 {kst(started)}, 경과 {(time.time()-started)/86400:.1f}일)")
    L.append(f"왕복비용 가정 {COST:.0%} / 수익률은 비용 차감 전 gross, '순수익' 항목만 차감\n")

    # 퍼널
    L.append("[퍼널: 평가 건수와 탈락 사유]")
    tot = c.execute("SELECT COUNT(*), SUM(passed) FROM checks").fetchone()
    L.append(f" 평가 {tot[0] or 0}건, 통과 {tot[1] or 0}건, 등록 풀 {c.execute('SELECT COUNT(*) FROM pools').fetchone()[0]}개")
    for col in ("hard_reason", "chain_reason"):
        rows = c.execute(f"SELECT {col}, COUNT(*) FROM checks WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()
        L.append(f" {col}: " + ", ".join(f"{r}={n}" for r, n in rows))
    for ts, net, msg in c.execute("SELECT * FROM net_errors ORDER BY ts DESC LIMIT 5"):
        L.append(f" ! {kst(ts)} {net}: {msg}")
    L.append("")

    groups = {
        "pass (가이드 필터 통과)": [e for e in E if e["grp"] == "pass"],
        "control (탈락 대조군)": [e for e in E if e["grp"] == "control"],
        "  └ 하드통과·top_wallet만 탈락": [e for e in E if e["grp"] == "control" and e["chain"] == "top_wallet"],
    }
    L.append("[보유기간별 gross 수익률]")
    res6 = {}
    for name, es in groups.items():
        L.append(f" {name}")
        for h in HORIZONS:
            xs = [r for r in (at_horizon(e, h) for e in es) if r is not None]
            if h == 6:
                res6[name] = xs
            L.append(f"   {h:2d}h: {summ(xs)}")
    L.append("")

    L.append("[러그 비율 (1/6/24h 시점 기준: -80%, 유동성 -80%, 풀 소멸)]")
    rug = {}
    for name, es in groups.items():
        flags = [f for f in (is_rug(e) for e in es) if f is not None]
        rug[name] = (sum(flags) / len(flags)) if flags else None
        L.append(f" {name}: " + (f"{rug[name]:.1%} ({sum(flags)}/{len(flags)})" if flags else "데이터 없음"))
    L.append("")

    L.append("[가이드 청산규칙 시뮬레이션 — pass 그룹, 순수익(비용 차감)]")
    P = groups["pass (가이드 필터 통과)"]
    sims = {}
    for label, stop in (("가이드 규칙 그대로", None), ("+ 손절 -30% 추가", 0.30)):
        rs = [x for x in (exit_sim(e, stop) for e in P) if x]
        net = [r - COST for r, _ in rs]
        sims[label] = net
        if net:
            ci = boot_ci(net)
            L.append(f" {label}: n={len(net)} 평균 {st.mean(net):+.1%} 중앙 {st.median(net):+.1%} "
                     f"승률 {sum(1 for x in net if x > 0)/len(net):.1%}" + (f" 95%CI[{ci[0]:+.1%},{ci[1]:+.1%}]" if ci else ""))
        else:
            L.append(f" {label}: 24h 완료 표본 없음")
    # 한 번에 한 포지션 (가이드 book.py 방식)
    seq, busy_until = [], 0
    for e in sorted(P, key=lambda e: e["ts"]):
        if e["ts"] < busy_until:
            continue
        x = exit_sim(e)
        if not x:
            continue
        seq.append(x[0] - COST)
        busy_until = x[1]
    if seq:
        eq = 1.0
        for r in seq:
            eq *= 1 + 0.06 * r   # 가이드 SIZE: 자금의 6%
        L.append(f" 1포지션 순차운영(자금 6%/회): {len(seq)}회, 누적 자산 {eq - 1:+.2%}")
    L.append("")

    L.append("[체인별 pass 6h gross]")
    for net in sorted({e["net"] for e in E}):
        xs = [r for r in (at_horizon(e, 6) for e in P if e["net"] == net) if r is not None]
        cx = [r for r in (at_horizon(e, 6) for e in E if e["grp"] == "control" and e["net"] == net) if r is not None]
        L.append(f" {net:10s} pass {summ(xs)}\n {'':10s} ctrl {summ(cx)}")
    L.append("")

    # 판정
    L.append(CRITERIA_TEXT)
    p6 = res6.get("pass (가이드 필터 통과)", [])
    c1 = len(p6) >= 30
    c2 = bool(p6) and st.median([x - COST for x in p6]) > 0
    rp, rc = rug.get("pass (가이드 필터 통과)"), rug.get("control (탈락 대조군)")
    c3 = rp is not None and rc is not None and (rc - rp) >= 0.10
    g = sims.get("가이드 규칙 그대로") or []
    ci = boot_ci(g) if g else None
    c4 = bool(g) and st.mean(g) > 0 and ci is not None and ci[0] > -0.05
    for k, v in (("C1", c1), ("C2", c2), ("C3", c3), ("C4", c4)):
        L.append(f" {k}: {'PASS' if v else ('FAIL' if c1 else '판정보류(표본부족)')}")
    L.append(" → 종합: " + ("전부 PASS — 다음 단계(소액 실전) 검토 가능" if all((c1, c2, c3, c4))
                          else ("표본 수집 중" if not c1 else "기준 미달 — 폐기")))
    return "\n".join(L)


def export_csv(c):
    E = load(c)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["net", "pool", "grp", "entry_kst", "age_min", "mcap", "hard_reason", "chain_reason",
                "ret_1h", "ret_6h", "ret_24h", "rug", "exit_rule_ret"])
    for e in E:
        x = exit_sim(e) if e["grp"] == "pass" else None
        w.writerow([e["net"], e["pool"], e["grp"], kst(e["ts"]), round(e["age"] or 0, 1), e["mcap"], e["hard"], e["chain"],
                    *[at_horizon(e, h) for h in HORIZONS], is_rug(e), x[0] if x else None])
    return buf.getvalue()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DB_PATH", "/data/memefwd.db")
    print(build_report(sqlite3.connect(path)))
