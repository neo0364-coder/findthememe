"""
후보 필터 탐색 (A+B 항목) — 앞 구간(train)에서 고르고, 뒤 구간(test)에서 확인.

  python analyze.py [db경로] [train_days] [test_days]
  HTTP: /analyze?key=...&train=3&test=3

원칙
 - 후보마다 train 구간 분위수(하위/상위 1/3)로 컷을 정한다 → 컷 값은 test를 보지 않고 결정.
 - train에서 표본 MIN_N 이상인 후보 중 6h 순수익 중앙값 상위 2개만 test로 넘긴다.
 - test에서 '기준선(추가필터 없음)'보다 나아야, 그리고 표본 MIN_N 이상이어야 채택 후보.
 - 세 모집단을 따로 본다: pass(가이드 통과) / pass2(수정 규칙 통과) / all(pass+대조군, 표본 많음).
"""
import json
import os
import sqlite3
import statistics as st
import sys
import time

import report as R

MIN_N = int(os.environ.get("ANALYZE_MIN_N", "30"))
H = 6   # 평가 보유시간(시간)


def sdiv(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def features(c):
    """entries별 진입 시점 특징값 dict."""
    regime = {}
    for ts, net, lph, mv, mc in c.execute("SELECT ts,net,launches_per_hour,med_vol_h1,med_chg_h1 FROM regime ORDER BY ts"):
        regime.setdefault(net, []).append((ts, lph, mv, mc))
    out = {}
    q = ("SELECT e.net,e.pool,e.grp,k.age_min,k.liq,k.mcap,k.vol_h1,k.vol_h24,k.buys_h1,k.sells_h1,k.chg_h1,k.chg_h24,"
         "k.dossier,k.extra,e.ts FROM entries e JOIN checks k ON k.id=e.check_id")
    for net, pool, grp, age, liq, mcap, v1, v24, b1, s1, c1, c24, dos, extra, ets in c.execute(q):
        d = json.loads(dos) if dos else {}
        x = json.loads(extra) if extra else {}
        tx, vol, chg = x.get("tx") or {}, x.get("vol") or {}, x.get("chg") or {}
        h1, m5 = tx.get("h1") or {}, tx.get("m5") or {}
        f = {
            # A항목 (처음부터 저장 중)
            "A_chg_h1": c1, "A_chg_h24": c24,
            "A_liq_to_mcap": sdiv(liq, mcap),
            "A_vol24_to_liq": sdiv(v24, liq),
            "A_buy_sell_h1": sdiv(b1, (s1 or 0) + 1),
            "A_age_min": age,
            "A_dev_hold": R_float(d.get("developer_holding_percentage")),
            "A_gt_score": R_float(d.get("gt_score")),
            # B항목 (이번에 추가)
            "B_uniq_buyers_h1": h1.get("buyers"),
            "B_buyers_per_buy_h1": sdiv(h1.get("buyers"), h1.get("buys")),       # 낮으면 소수 지갑 반복매수(봇)
            "B_uniq_buyer_seller_h1": sdiv(h1.get("buyers"), (h1.get("sellers") or 0) + 1),
            "B_vol_m5_accel": sdiv(R_float(vol.get("m5")), sdiv(R_float(vol.get("h1")), 12)),  # >1 이면 최근 5분 가속
            "B_buy_sell_m5": sdiv(m5.get("buys"), (m5.get("sells") or 0) + 1),
            "B_chg_m5": R_float(chg.get("m5")),
        }
        rg = [r for r in regime.get(net, []) if r[0] <= ets]
        if rg:
            _, lph, mv, mc = rg[-1]
            f.update({"B_mkt_launch_rate": lph, "B_mkt_med_vol_h1": mv, "B_mkt_med_chg_h1": mc})
        out[(net, pool, grp)] = f
    return out


def R_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def stats(rows):
    rets = [r["ret"] - R.COST for r in rows if r["ret"] is not None]
    rugs = [r["rug"] for r in rows if r["rug"] is not None]
    if not rets:
        return {"n": 0}
    return {"n": len(rets), "med": st.median(rets), "mean": st.mean(rets),
            "win": sum(1 for x in rets if x > 0) / len(rets), "rug": (sum(rugs) / len(rugs)) if rugs else None}


def fmt(s):
    if not s or s["n"] == 0:
        return "n=0"
    rug = f"{s['rug']:.0%}" if s.get("rug") is not None else "-"
    return f"n={s['n']:4d} 중앙 {s['med']:+6.1%} 평균 {s['mean']:+6.1%} 승률 {s['win']:4.0%} 러그 {rug}"


def run(c, train_days=3.0, test_days=3.0):
    E = R.load(c)
    F = features(c)
    started = float((c.execute("SELECT v FROM meta WHERE k='started_at'").fetchone() or [time.time()])[0])
    t_split = started + train_days * 86400
    t_end = t_split + test_days * 86400
    rows = []
    for e in E:
        rows.append({"grp": e["grp"], "ts": e["ts"], "ret": R.at_horizon(e, H), "rug": R.is_rug(e),
                     "f": F.get((e["net"], e["pool"], e["grp"]), {})})
    L = [f"후보 필터 탐색 — train {train_days:g}일 / test {test_days:g}일, {H}h 순수익(비용 {R.COST:.0%} 차감), 최소표본 {MIN_N}",
         f"train: {R.kst(started)} ~ {R.kst(t_split)} / test: {R.kst(t_split)} ~ {R.kst(t_end)}"]
    if time.time() < t_end + H * 3600:
        L.append(f"※ test 구간 수익률이 아직 다 안 익음 — 최종 판정은 {R.kst(t_end + H * 3600)} 이후")
    names = sorted({k for r in rows for k in r["f"]})
    for universe in ("pass", "pass2", "all"):
        U = [r for r in rows if (r["grp"] in ("pass", "control") if universe == "all" else r["grp"] == universe)]
        tr = [r for r in U if r["ts"] < t_split]
        te = [r for r in U if t_split <= r["ts"] < t_end]
        L.append(f"\n━━ 모집단: {universe} ━━")
        L.append(f" 기준선 train {fmt(stats(tr))}")
        L.append(f" 기준선 test  {fmt(stats(te))}")
        cands = []
        for nm in names:
            vals = sorted(r["f"][nm] for r in tr if r["f"].get(nm) is not None and r["ret"] is not None)
            if len(vals) < 3 * MIN_N // 2:
                continue
            lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
            for side, cut in (("≤", lo), ("≥", hi)):
                keep = (lambda v, s=side, c=cut: v is not None and (v <= c if s == "≤" else v >= c))
                s_tr = stats([r for r in tr if keep(r["f"].get(nm))])
                if s_tr["n"] >= MIN_N:
                    cands.append((s_tr["med"], nm, side, cut, keep, s_tr))
        if not cands:
            L.append(" 후보 없음 (train 표본 부족)")
            continue
        cands.sort(key=lambda x: -x[0])
        L.append(" [train 상위 5 — 참고용, 여기서 좋아 보이는 건 대부분 우연]")
        for med, nm, side, cut, keep, s_tr in cands[:5]:
            L.append(f"  {nm} {side} {cut:.4g}: {fmt(s_tr)}")
        L.append(" [test 확인 — 상위 2개만]")
        base_te = stats(te)
        for med, nm, side, cut, keep, s_tr in cands[:2]:
            s_te = stats([r for r in te if keep(r["f"].get(nm))])
            ok = (s_te["n"] >= MIN_N and base_te["n"] and s_te["med"] > base_te["med"] and s_te["med"] > 0)
            L.append(f"  {nm} {side} {cut:.4g}: {fmt(s_te)}  → {'채택 후보' if ok else '기각'}")
    L.append("\n채택 후보가 나와도 곧바로 실전 X — 새 기간(다음 3일)에서 한 번 더 확인 후 결정.")
    return "\n".join(L)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DB_PATH", "/data/memefwd.db")
    tr = float(sys.argv[2]) if len(sys.argv) > 2 else 3
    te = float(sys.argv[3]) if len(sys.argv) > 3 else 3
    print(run(sqlite3.connect(path), tr, te))
