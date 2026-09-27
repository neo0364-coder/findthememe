"""리포트: pass vs control 비교 + 사전 합격기준 판정. `python report.py [db경로]` 로도 실행 가능."""
import csv
import io
import json
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
CRITERIA_TEXT = """[사전 합격 기준 — pass·pass2 각각 따로 판정, 하나라도 FAIL이면 그 그룹 폐기]
 C1. 그룹 표본 30건 이상 (6시간 수익률 측정 완료 기준)
 C2. 그룹 6시간 순수익률(비용 {cost:.0%} 차감) 중앙값 > 0
 C3. 그룹 러그 비율이 control 대비 10%p 이상 낮을 것
 C4. 가이드 청산규칙(거래량 비율<0.2 또는 24h) 시뮬레이션 평균 순수익률 > 0, 그리고 부트스트랩 95% 하한 > -5%
""".format(cost=COST)


def kst(ts):
    return datetime.fromtimestamp(ts, KST).strftime("%m-%d %H:%M KST")


def load(c):
    ents = c.execute("SELECT e.net,e.pool,e.grp,e.ts,e.price,e.liq,k.hard_reason,k.chain_reason,k.age_min,k.mcap,"
                     "p.token,k.dossier FROM entries e LEFT JOIN checks k ON k.id=e.check_id "
                     "LEFT JOIN pools p ON p.net=e.net AND p.pool=e.pool").fetchall()
    snaps = {}
    for net, pool, grp, ts, price, liq, v6, v24, miss in c.execute(
            "SELECT net,pool,grp,ts,price,liq,vol_h6,vol_h24,missing FROM snaps ORDER BY ts"):
        snaps.setdefault((net, pool, grp), []).append((ts, price, liq, v6, v24, miss))
    out = []
    for net, pool, grp, ts, price, liq, hr, cr, age, mcap, token, dos in ents:
        out.append(dict(net=net, pool=pool, grp=grp, ts=ts, price=price, liq=liq, hard=hr, chain=cr,
                        age=age, mcap=mcap, token=token, dos=json.loads(dos) if dos else {},
                        snaps=snaps.get((net, pool, grp), [])))
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
    t2 = c.execute("SELECT SUM(passed2) FROM checks").fetchone()[0] or 0
    p2s = c.execute("SELECT v FROM meta WHERE k='pass2_started_at'").fetchone()
    L.append(f" pass2(수정 규칙) 통과 {t2}건" + (f" — pass2 기록 시작 {kst(float(p2s[0]))}" if p2s else ""))
    nets = [r[0] for r in c.execute("SELECT DISTINCT net FROM checks ORDER BY 1")]
    for net in nets:
        n_all, n_p, n_p2, n_hard_ok = c.execute(
            "SELECT COUNT(*), SUM(passed), SUM(passed2), SUM(hard_reason IS NULL) FROM checks WHERE net=?", (net,)).fetchone()
        L.append(f" ── {net}: 평가 {n_all}건 → 하드필터 통과 {n_hard_ok or 0} → pass {n_p or 0} / pass2 {n_p2 or 0}")
        for col, lab in (("hard_reason", "하드탈락"), ("chain_reason", "체인탈락(pass)"), ("chain_reason2", "체인탈락(pass2)")):
            rows = c.execute(f"SELECT {col}, COUNT(*) FROM checks WHERE net=? AND {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
                             (net,)).fetchall()
            if rows:
                L.append(f"    {lab}: " + ", ".join(f"{r}={n}" for r, n in rows))
    for ts, net, msg in c.execute("SELECT * FROM net_errors ORDER BY ts DESC LIMIT 5"):
        L.append(f" ! {kst(ts)} {net}: {msg}")
    L.append("")

    groups = {
        "pass (가이드 필터 통과)": [e for e in E if e["grp"] == "pass"],
        "pass2 (수정 규칙: 빈값=탈락, 풀계정 제외)": [e for e in E if e["grp"] == "pass2"],
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

    sims = {}
    for gname in ("pass (가이드 필터 통과)", "pass2 (수정 규칙: 빈값=탈락, 풀계정 제외)"):
        P = groups[gname]
        L.append(f"[가이드 청산규칙 시뮬레이션 — {gname}, 순수익(비용 차감)]")
        for label, stop in (("가이드 규칙 그대로", None), ("+ 손절 -30% 추가", 0.30)):
            rs = [x for x in (exit_sim(e, stop) for e in P) if x]
            net = [r - COST for r, _ in rs]
            sims[(gname, label)] = net
            if net:
                ci = boot_ci(net)
                L.append(f" {label}: n={len(net)} 평균 {st.mean(net):+.1%} 중앙 {st.median(net):+.1%} "
                         f"승률 {sum(1 for x in net if x > 0)/len(net):.1%}" + (f" 95%CI[{ci[0]:+.1%},{ci[1]:+.1%}]" if ci else ""))
            else:
                L.append(f" {label}: 24h 완료 표본 없음")
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

    L.append("[체인별 6h gross]")
    for net in sorted({e["net"] for e in E}):
        for g, lab in (("pass", "pass "), ("pass2", "pass2"), ("control", "ctrl ")):
            xs = [r for r in (at_horizon(e, 6) for e in E if e["grp"] == g and e["net"] == net) if r is not None]
            L.append(f" {net if g == 'pass' else '':10s} {lab} {summ(xs)}")
    L.append("")

    # LP 안전성 (EVM 체인: 하드필터 통과해 dossier가 있는 진입 건만)
    L.append("[LP 상태별 러그 비율 — 하드필터 통과 토큰, EVM 체인 (pass/pass2/대조군 합산)]")
    seen_tok, lp_rows = set(), []
    for e in E:
        d = e["dos"]
        if e["net"] == "solana" or not d or (e["net"], e["pool"]) in seen_tok:
            continue
        seen_tok.add((e["net"], e["pool"]))
        lp_rows.append((e, d))

    def bucket(d):
        v = d.get("lp_safe_pct")
        if v is None:
            return "LP 상태 미확인"
        return "LP 90%+ 소각/락" if v >= 0.9 else ("LP 10~90% 소각/락" if v >= 0.1 else "LP 대부분 개인보유(<10%)")

    for b in ("LP 90%+ 소각/락", "LP 10~90% 소각/락", "LP 대부분 개인보유(<10%)", "LP 상태 미확인"):
        es = [e for e, d in lp_rows if bucket(d) == b]
        fl = [f for f in (is_rug(e) for e in es) if f is not None]
        r6 = [r for r in (at_horizon(e, 6) for e in es) if r is not None]
        L.append(f" {b:22s} 러그 " + (f"{sum(fl)/len(fl):5.1%} ({sum(fl)}/{len(fl)})" if fl else "  -  ") + f" | 6h {summ(r6)}")
    for lab, cond in (("LP 제공자 1명", lambda d: d.get("gp_lp_holder_count") == 1),
                      ("LP 제공자 2명+", lambda d: (d.get("gp_lp_holder_count") or 0) >= 2)):
        es = [e for e, d in lp_rows if cond(d)]
        fl = [f for f in (is_rug(e) for e in es) if f is not None]
        L.append(f" {lab:22s} 러그 " + (f"{sum(fl)/len(fl):5.1%} ({sum(fl)}/{len(fl)})" if fl else "  -  "))
    gp_ok = sum(1 for _, d in lp_rows if d.get("gp_supported"))
    L.append(f" (GoPlus 지원 확인 {gp_ok}/{len(lp_rows)}건, V2 LP {sum(1 for _, d in lp_rows if d.get('lp_v2'))}건)")
    L.append("")

    L.append("[최근 pass / pass2 20건 — CA는 토큰 주소, pool은 LP(풀) 주소]")
    recent = sorted([e for e in E if e["grp"] in ("pass", "pass2")], key=lambda e: -e["ts"])[:20]
    for e in recent:
        r = at_horizon(e, 6)
        lp = e["dos"].get("lp_safe_pct")
        L.append(f" {kst(e['ts'])} {e['grp']:5s} {e['net']:9s} CA={e['token']}  "
                 f"LP잠금={'-' if lp is None else f'{lp:.0%}'}  6h={'-' if r is None else f'{r:+.0%}'}  "
                 f"러그={is_rug(e)}")
    L.append("")

    # 판정
    L.append(CRITERIA_TEXT)
    rc = rug.get("control (탈락 대조군)")
    for gname in ("pass (가이드 필터 통과)", "pass2 (수정 규칙: 빈값=탈락, 풀계정 제외)"):
        p6 = res6.get(gname, [])
        c1 = len(p6) >= 30
        c2 = bool(p6) and st.median([x - COST for x in p6]) > 0
        rp = rug.get(gname)
        c3 = rp is not None and rc is not None and (rc - rp) >= 0.10
        g = sims.get((gname, "가이드 규칙 그대로")) or []
        ci = boot_ci(g) if g else None
        c4 = bool(g) and st.mean(g) > 0 and ci is not None and ci[0] > -0.05
        L.append(f" ▶ {gname}")
        L.append("   " + "  ".join(f"{k}: {'PASS' if v else ('FAIL' if c1 else '보류')}"
                                  for k, v in (("C1", c1), ("C2", c2), ("C3", c3), ("C4", c4))))
        L.append("   → 종합: " + ("전부 PASS — 다음 단계(소액 실전) 검토 가능" if all((c1, c2, c3, c4))
                               else ("표본 수집 중" if not c1 else "기준 미달 — 폐기")))
    return "\n".join(L)


def export_csv(c):
    E = load(c)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["net", "token_ca", "pool", "grp", "entry_kst", "age_min", "mcap", "hard_reason", "chain_reason",
                "ret_1h", "ret_6h", "ret_24h", "rug", "exit_rule_ret", "lp_safe_pct", "lp_holder_count", "sell_tax"])
    for e in E:
        x = exit_sim(e) if e["grp"] in ("pass", "pass2") else None
        w.writerow([e["net"], e["token"], e["pool"], e["grp"], kst(e["ts"]), round(e["age"] or 0, 1), e["mcap"], e["hard"],
                    e["chain"], *[at_horizon(e, h) for h in HORIZONS], is_rug(e), x[0] if x else None,
                    e["dos"].get("lp_safe_pct"), e["dos"].get("gp_lp_holder_count"), e["dos"].get("gp_sell_tax")])
    return buf.getvalue()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DB_PATH", "/data/memefwd.db")
    print(build_report(sqlite3.connect(path)))
