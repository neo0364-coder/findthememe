"""
밈코인 신규상장 필터 포워드 테스트 (페이퍼, 실매수 없음)

@savipww "Grok Bot + Jev desk" 가이드의 하드필터/체인필터를 그대로 재현해서
  - 필터 통과 토큰(pass 그룹)과
  - 같은 시점에 평가됐지만 탈락한 토큰(control 그룹)의
이후 수익률/러그 비율을 비교한다. LLM(Jev) 소프트필터는 제외 — 하드필터 자체에
엣지가 없으면 그 위에 LLM을 얹을 근거도 없기 때문.

데이터 소스: GeckoTerminal 무료 API (+ Solana 공개 RPC). FOMO 앱은 공개 API가 없어 사용하지 않음.
모든 평가 원자료를 DB에 남기므로, 나중에 임계값을 바꿔 오프라인 재분석이 가능하다.
"""
import json
import logging
import os
import random
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# ───────────────────────── 설정 ─────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/data/memefwd.db")
NETWORKS = [n.strip() for n in os.environ.get("NETWORKS", "solana,bsc,robinhood").split(",") if n.strip()]
DISCOVERY_PAGES = int(os.environ.get("DISCOVERY_PAGES", "2"))
DISCOVERY_EVERY_SEC = int(os.environ.get("DISCOVERY_EVERY_SEC", "900"))   # 가이드와 동일: 15분
GT_RPM = float(os.environ.get("GT_RPM", "9"))                             # GeckoTerminal 무료 10/분 → 여유 두고 9
CONTROL_SAMPLE = float(os.environ.get("CONTROL_SAMPLE", "0.35"))          # 대조군 추적 비율 (API 예산용)
PASS_SNAP_HOURS = 24
CONTROL_SNAP_HOURS = [1, 6, 24]
CHECK_AGES_MIN = [20, 45, 90, 180, 360, 720, 1440, 2880, 4320]            # 재평가 시점(상장 후 분)
PORT = int(os.environ.get("PORT", "8080"))
REPORT_TOKEN = os.environ.get("REPORT_TOKEN", "")                          # 설정 시 ?key=값 필요

# 가이드 thresholds.py HARD 그대로
HARD = {
    "min_age_minutes": 15,
    "max_age_hours": 72,
    "min_liquidity_usd": 12_000,
    "min_volume_h24": 40_000,
    "min_mcap_usd": 60_000,
    "max_mcap_usd": 8_000_000,
    "min_trades_h24": 150,
    "max_top_wallet": 0.05,   # solana only
    "max_top_10": 0.60,
    "min_holders": 80,
}

GT = "https://api.geckoterminal.com/api/v2"
HDR = {"accept": "application/json;version=20230302", "user-agent": "memefwd/1.0"}
SOL_RPC = os.environ.get("SOL_RPC", "https://api.mainnet-beta.solana.com")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("memefwd")


# ───────────────────────── 유틸 ─────────────────────────
def fnum(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def parse_ts(s):
    if not s:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class RateLimiter:
    def __init__(self, per_min):
        self.gap = 60.0 / per_min
        self.last = 0.0

    def wait(self):
        d = self.last + self.gap - time.time()
        if d > 0:
            time.sleep(d)
        self.last = time.time()


GT_LIMIT = RateLimiter(GT_RPM)
BAD_NETS = set()


def gt_get(path, params=None):
    for attempt in range(3):
        GT_LIMIT.wait()
        try:
            r = requests.get(GT + path, params=params, headers=HDR, timeout=25)
        except requests.RequestException as e:
            log.warning("GT 네트워크 오류 %s: %s", path, e)
            time.sleep(5)
            continue
        if r.status_code == 429:
            log.warning("GT 429 — 60초 대기")
            time.sleep(60)
            continue
        if r.status_code == 404:
            return None
        if r.status_code >= 500:
            time.sleep(10)
            continue
        r.raise_for_status()
        return r.json()
    return None


# ───────────────────────── DB ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS pools(
  pool TEXT, net TEXT, token TEXT, name TEXT, created_at REAL, discovered_at REAL,
  next_idx INTEGER DEFAULT 0, done INTEGER DEFAULT 0, has_control INTEGER DEFAULT 0,
  PRIMARY KEY(net, pool));
CREATE UNIQUE INDEX IF NOT EXISTS ux_token ON pools(net, token);
CREATE TABLE IF NOT EXISTS checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, net TEXT, pool TEXT, ts REAL, age_min REAL,
  price REAL, liq REAL, mcap REAL, vol_h1 REAL, vol_h6 REAL, vol_h24 REAL,
  buys_h1 INTEGER, sells_h1 INTEGER, trades_h24 INTEGER, chg_h1 REAL, chg_h24 REAL,
  hard_reason TEXT, chain_reason TEXT, dossier TEXT, passed INTEGER);
CREATE TABLE IF NOT EXISTS entries(
  net TEXT, pool TEXT, grp TEXT, ts REAL, price REAL, liq REAL, check_id INTEGER,
  next_snap REAL, snap_idx INTEGER DEFAULT 0, closed INTEGER DEFAULT 0,
  PRIMARY KEY(net, pool, grp));
CREATE TABLE IF NOT EXISTS snaps(
  net TEXT, pool TEXT, grp TEXT, ts REAL, price REAL, liq REAL, vol_h6 REAL, vol_h24 REAL, missing INTEGER);
CREATE INDEX IF NOT EXISTS ix_snaps ON snaps(net, pool, grp, ts);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS net_errors(ts REAL, net TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS regime(ts REAL, net TEXT, n_pools INTEGER, span_min REAL, launches_per_hour REAL,
  med_vol_h1 REAL, med_chg_h1 REAL);
"""


def db_connect():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(checks)")}
    if "extra" not in cols:   # B항목 추가 전 DB 호환: 기존 데이터 유지한 채 컬럼만 추가
        c.execute("ALTER TABLE checks ADD COLUMN extra TEXT")
    c.execute("INSERT OR IGNORE INTO meta VALUES('started_at', ?)", (str(time.time()),))
    c.commit()
    return c


# ───────────────────────── 파싱 ─────────────────────────
def pool_metrics(p):
    """GeckoTerminal pool 객체 → 평가에 쓰는 평평한 dict."""
    a = p.get("attributes") or {}
    tx = a.get("transactions") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    h1 = tx.get("h1") or {}
    h24 = tx.get("h24") or {}
    mcap = fnum(a.get("market_cap_usd")) or fnum(a.get("fdv_usd"))
    b24, s24 = h24.get("buys"), h24.get("sells")
    return {
        "pool": a.get("address"),
        "price": fnum(a.get("base_token_price_usd")),
        "liq": fnum(a.get("reserve_in_usd")),
        "mcap": mcap,
        "vol_h1": fnum(vol.get("h1")),
        "vol_h6": fnum(vol.get("h6")),
        "vol_h24": fnum(vol.get("h24")),
        "buys_h1": h1.get("buys"),
        "sells_h1": h1.get("sells"),
        "trades_h24": (b24 or 0) + (s24 or 0) if (b24 is not None or s24 is not None) else None,
        "chg_h1": fnum(chg.get("h1")),
        "chg_h24": fnum(chg.get("h24")),
        "created_at": parse_ts(a.get("pool_created_at")),
        "name": a.get("name"),
        # B항목: 필터 판정에는 안 쓰고 기록만 (고유 매수/매도자, 5분 흐름 등 전 구간 원자료)
        "extra": {"tx": tx, "vol": vol, "chg": chg},
    }


def base_token_addr(p):
    rel = (p.get("relationships") or {}).get("base_token") or {}
    tid = (rel.get("data") or {}).get("id") or ""
    return tid.split("_", 1)[1] if "_" in tid else None


# ───────────────────────── 필터 (가이드 filter.py 재현) ─────────────────────────
def free_kill(m, age_min):
    if not (HARD["min_age_minutes"] <= age_min <= HARD["max_age_hours"] * 60):
        return "age"
    if (m["liq"] or 0) < HARD["min_liquidity_usd"]:
        return "liquidity"
    if (m["vol_h24"] or 0) < HARD["min_volume_h24"]:
        return "volume"
    if m["mcap"] is None or not (HARD["min_mcap_usd"] <= m["mcap"] <= HARD["max_mcap_usd"]):
        return "mcap"
    return None


def trade_kill(m):
    if m["trades_h24"] is None:
        return "no_pair"
    if m["trades_h24"] < HARD["min_trades_h24"]:
        return "trades"
    if (m["sells_h1"] or 0) == 0 and (m["buys_h1"] or 0) > 20:
        return "no_sells"
    return None


def sol_top_wallet(mint):
    def q(method, params):
        r = requests.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        return r.json()["result"]
    try:
        supply = float(q("getTokenSupply", [mint])["value"]["amount"])
        top = q("getTokenLargestAccounts", [mint])["value"]
        return float(top[0]["amount"]) / supply if supply and top else None
    except Exception as e:
        log.info("sol RPC 실패 %s: %s", mint, e)
        return None


def dossier(net, token):
    j = gt_get(f"/networks/{net}/tokens/{token}/info")
    if not j:
        return None
    a = (j.get("data") or {}).get("attributes") or {}
    holders = a.get("holders") or {}
    dist = holders.get("distribution_percentage") or {}
    d = {
        "holder_count": holders.get("count"),
        "top_10_percent": fnum(dist.get("top_10")),
        "developer_holding_percentage": fnum(a.get("developer_holding_percentage")),
        "is_honeypot": a.get("is_honeypot"),
        "mint_authority": a.get("mint_authority"),
        "freeze_authority": a.get("freeze_authority"),
        "gt_score": fnum(a.get("gt_score")),
        "twitter_handle": a.get("twitter_handle"),
    }
    if net == "solana":
        d["top_wallet_percent"] = sol_top_wallet(token)
    return d


def _authority_open(v):
    # GT는 "renounced" 같은 문자열을 줄 때가 있음 → 실제 주소일 때만 open으로 간주
    return bool(v) and str(v).lower() not in ("renounced", "none", "null", "no", "false")


def chain_kill(net, d):
    if d.get("top_wallet_percent") is not None and d["top_wallet_percent"] > HARD["max_top_wallet"]:
        return "top_wallet"
    if d.get("top_10_percent") is not None and d["top_10_percent"] / 100 > HARD["max_top_10"]:
        return "top_10"
    if d.get("holder_count") is not None and d["holder_count"] < HARD["min_holders"]:
        return "holders"
    if net == "solana" and (_authority_open(d.get("mint_authority")) or _authority_open(d.get("freeze_authority"))):
        return "authority_open"
    if net in ("bsc", "base") and d.get("is_honeypot") in (True, "true", "yes"):
        return "honeypot"
    return None


# ───────────────────────── 루프 단계 ─────────────────────────
def discover(db):
    now = time.time()
    added = 0
    for net in NETWORKS:
        if net in BAD_NETS:
            continue
        seen = []
        for page in range(1, DISCOVERY_PAGES + 1):
            j = gt_get(f"/networks/{net}/new_pools", {"page": page, "include": "base_token"})
            if j is None:
                if page == 1:
                    BAD_NETS.add(net)
                    db.execute("INSERT INTO net_errors VALUES(?,?,?)", (now, net, "new_pools 404/실패 → 네트워크 제외"))
                    log.warning("네트워크 %s new_pools 실패 → 이번 실행에서 제외", net)
                break
            for p in j.get("data", []):
                m = pool_metrics(p)
                seen.append(m)
                tok = base_token_addr(p)
                if not m["pool"] or not tok or not m["created_at"]:
                    continue
                if now - m["created_at"] > HARD["max_age_hours"] * 3600:
                    continue
                cur = db.execute("INSERT OR IGNORE INTO pools(pool,net,token,name,created_at,discovered_at) VALUES(?,?,?,?,?,?)",
                                 (m["pool"], net, tok, m["name"], m["created_at"], now))
                added += cur.rowcount
        # B항목: 시장 분위기 — 최신 풀 N개가 몇 분에 걸쳐 생겼나(=상장 속도), 신규 풀 거래량/등락 중앙값
        ts_list = [m["created_at"] for m in seen if m["created_at"]]
        if len(ts_list) >= 5:
            import statistics as st
            span = (max(ts_list) - min(ts_list)) / 60
            vols = [m["vol_h1"] for m in seen if m["vol_h1"] is not None]
            chgs = [m["chg_h1"] for m in seen if m["chg_h1"] is not None]
            db.execute("INSERT INTO regime VALUES(?,?,?,?,?,?,?)",
                       (now, net, len(ts_list), span, len(ts_list) / (span / 60) if span > 0 else None,
                        st.median(vols) if vols else None, st.median(chgs) if chgs else None))
    db.commit()
    log.info("discover: 신규 풀 %d개 등록", added)


def fetch_multi(net, pools):
    """풀 주소 최대 30개씩 묶어서 조회 → {pool: (metrics)} ; 응답에 없으면 누락."""
    out = {}
    for i in range(0, len(pools), 30):
        chunk = pools[i:i + 30]
        j = gt_get(f"/networks/{net}/pools/multi/{','.join(chunk)}")
        if not j:
            continue
        for p in j.get("data", []):
            m = pool_metrics(p)
            if m["pool"]:
                out[m["pool"]] = m
    return out


def next_idx_after(age_min):
    for i, a in enumerate(CHECK_AGES_MIN):
        if a > age_min:
            return i
    return len(CHECK_AGES_MIN)


def evaluate_due(db, max_pools=300):
    now = time.time()
    rows = db.execute("SELECT net,pool,token,created_at,next_idx,has_control FROM pools WHERE done=0").fetchall()
    due = [r for r in rows if r[4] < len(CHECK_AGES_MIN) and (now - r[3]) / 60 >= CHECK_AGES_MIN[r[4]]]
    for r in rows:
        if r[4] >= len(CHECK_AGES_MIN) or (now - r[3]) > HARD["max_age_hours"] * 3600 + 3600:
            db.execute("UPDATE pools SET done=1 WHERE net=? AND pool=?", (r[0], r[1]))
    due = due[:max_pools]
    by_net = {}
    for r in due:
        by_net.setdefault(r[0], []).append(r)
    n_pass = n_eval = 0
    for net, lst in by_net.items():
        metrics = fetch_multi(net, [r[1] for r in lst])
        for (_, pool, token, created, idx, has_control) in lst:
            age_min = (now - created) / 60
            m = metrics.get(pool)
            db.execute("UPDATE pools SET next_idx=? WHERE net=? AND pool=?", (next_idx_after(age_min), net, pool))
            if not m or m["price"] is None:
                continue
            n_eval += 1
            hard = free_kill(m, age_min) or trade_kill(m)
            chain_r, dos = None, None
            if hard is None:
                dos = dossier(net, token)
                chain_r = chain_kill(net, dos) if dos else "dossier_failed"
            passed = int(hard is None and chain_r is None)
            if chain_r in ("honeypot", "authority_open", "top_wallet"):
                # 가이드 bench 규칙: 바뀌지 않는 사실로 탈락 → 재평가 안 함 (API 예산 절약)
                db.execute("UPDATE pools SET done=1 WHERE net=? AND pool=?", (net, pool))
            cur = db.execute(
                "INSERT INTO checks(net,pool,ts,age_min,price,liq,mcap,vol_h1,vol_h6,vol_h24,buys_h1,sells_h1,trades_h24,"
                "chg_h1,chg_h24,hard_reason,chain_reason,dossier,passed,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (net, pool, now, age_min, m["price"], m["liq"], m["mcap"], m["vol_h1"], m["vol_h6"], m["vol_h24"],
                 m["buys_h1"], m["sells_h1"], m["trades_h24"], m["chg_h1"], m["chg_h24"], hard, chain_r,
                 json.dumps(dos) if dos else None, passed, json.dumps(m["extra"])))
            cid = cur.lastrowid
            if passed:
                n_pass += 1
                db.execute("INSERT OR IGNORE INTO entries(net,pool,grp,ts,price,liq,check_id,next_snap) VALUES(?,?,?,?,?,?,?,?)",
                           (net, pool, "pass", now, m["price"], m["liq"], cid, now + 900))
                db.execute("UPDATE pools SET done=1 WHERE net=? AND pool=?", (net, pool))
                log.info("PASS %s %s age=%.0fm liq=%.0f mcap=%.0f", net, pool, age_min, m["liq"] or 0, m["mcap"] or 0)
            elif not has_control and age_min >= HARD["min_age_minutes"]:
                # 대조군: 처음으로 '평가 가능 나이'에 도달했을 때 탈락한 토큰, 예산 위해 샘플링
                db.execute("UPDATE pools SET has_control=1 WHERE net=? AND pool=?", (net, pool))
                if random.random() < CONTROL_SAMPLE:
                    db.execute("INSERT OR IGNORE INTO entries(net,pool,grp,ts,price,liq,check_id,next_snap) VALUES(?,?,?,?,?,?,?,?)",
                               (net, pool, "control", now, m["price"], m["liq"], cid, now + CONTROL_SNAP_HOURS[0] * 3600))
    db.commit()
    if due:
        log.info("evaluate: %d개 평가, pass %d", n_eval, n_pass)


def snapshot_due(db):
    now = time.time()
    rows = db.execute("SELECT net,pool,grp,ts,snap_idx FROM entries WHERE closed=0 AND next_snap<=?", (now,)).fetchall()
    by_net = {}
    for r in rows:
        by_net.setdefault(r[0], []).append(r)
    for net, lst in by_net.items():
        metrics = fetch_multi(net, sorted({r[1] for r in lst}))
        for (_, pool, grp, ets, sidx) in lst:
            m = metrics.get(pool)
            db.execute("INSERT INTO snaps VALUES(?,?,?,?,?,?,?,?,?)",
                       (net, pool, grp, now, m["price"] if m else None, m["liq"] if m else None,
                        m["vol_h6"] if m else None, m["vol_h24"] if m else None, 0 if m else 1))
            if grp == "pass":
                nxt = now + 900
                closed = int(nxt > ets + PASS_SNAP_HOURS * 3600 + 600)
                db.execute("UPDATE entries SET next_snap=?, snap_idx=?, closed=? WHERE net=? AND pool=? AND grp=?",
                           (nxt, sidx + 1, closed, net, pool, grp))
            else:
                k = sidx + 1
                if k >= len(CONTROL_SNAP_HOURS):
                    db.execute("UPDATE entries SET snap_idx=?, closed=1 WHERE net=? AND pool=? AND grp=?", (k, net, pool, grp))
                else:
                    db.execute("UPDATE entries SET next_snap=?, snap_idx=? WHERE net=? AND pool=? AND grp=?",
                               (ets + CONTROL_SNAP_HOURS[k] * 3600, k, net, pool, grp))
    db.commit()
    if rows:
        log.info("snapshot: %d건", len(rows))


# ───────────────────────── HTTP (리포트 보기) ─────────────────────────
def serve(db_path):
    import report

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if REPORT_TOKEN and f"key={REPORT_TOKEN}" not in self.path:
                self.send_response(403); self.end_headers(); return
            try:
                c = sqlite3.connect(db_path, timeout=30)
                if self.path.startswith("/export.csv"):
                    body, ctype = report.export_csv(c), "text/csv; charset=utf-8"
                elif self.path.startswith("/analyze"):
                    import analyze
                    from urllib.parse import parse_qs, urlparse
                    qs = parse_qs(urlparse(self.path).query)
                    body = analyze.run(c, float(qs.get("train", ["3"])[0]), float(qs.get("test", ["3"])[0]))
                    ctype = "text/plain; charset=utf-8"
                else:
                    body, ctype = report.build_report(c), "text/plain; charset=utf-8"
                c.close()
                data = body.encode("utf-8")
                self.send_response(200)
            except Exception as e:  # noqa
                data, ctype = f"error: {e}".encode(), "text/plain"
                self.send_response(500)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


def main():
    db = db_connect()
    threading.Thread(target=serve, args=(DB_PATH,), daemon=True).start()
    log.info("시작: networks=%s, GT_RPM=%s, DB=%s, 리포트 포트 %d", NETWORKS, GT_RPM, DB_PATH, PORT)
    last_disc = 0.0
    while True:
        try:
            if time.time() - last_disc >= DISCOVERY_EVERY_SEC:
                discover(db)
                last_disc = time.time()
            snapshot_due(db)
            evaluate_due(db)
        except Exception as e:
            log.exception("사이클 오류: %s", e)
        time.sleep(30)


if __name__ == "__main__":
    main()
