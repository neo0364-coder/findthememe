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

VERSION = "v9.4-flow-at-entry (2026-09-28)"   # 배포 확인용: 시작 로그·리포트 첫 줄에 표시

# ───────────────────────── 설정 ─────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/data/memefwd.db")
NETWORKS = [n.strip() for n in os.environ.get("NETWORKS", "solana,robinhood").split(",") if n.strip()]   # v8: BSC 신규 수집 중단(6h 중앙 -100%)
DISCOVERY_PAGES = int(os.environ.get("DISCOVERY_PAGES", "2"))           # v8: BSC 뺀 여유분으로 다시 2 (체인당 최신 40개)
DISCOVERY_EVERY_SEC = int(os.environ.get("DISCOVERY_EVERY_SEC", "900"))   # 가이드와 동일: 15분
GT_RPM = float(os.environ.get("GT_RPM", "6"))                             # GeckoTerminal 무료 한도 — 9에서 429가 잦아 6으로 낮춤
CONTROL_SAMPLE = float(os.environ.get("CONTROL_SAMPLE", "0.35"))          # 대조군 추적 비율 (API 예산용)
PASS_SNAP_HOURS = 30                                                      # 12h 대기+12h 보유 분석용으로 24→30
CONTROL_SNAP_HOURS = [1, 6, 24]
SURV_SNAP_HOURS = [3, 6, 12, 18, 24]   # v9 생존자 확장: 하드필터 통과 토큰 전체를 가이드 체인필터와 무관하게 추적
SNAP_SCHEDULE = {"control": CONTROL_SNAP_HOURS, "surv": SURV_SNAP_HOURS}
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
SOL_RPC = os.environ.get("SOL_RPC", "https://api.mainnet-beta.solana.com").strip()
if SOL_RPC and not SOL_RPC.startswith("http"):
    # 주소 없이 API 키만 넣은 경우 → Helius 키로 간주해 전체 주소로 보정
    SOL_RPC = f"https://mainnet.helius-rpc.com/?api-key={SOL_RPC}"
EVM_RPC = {
    "bsc": os.environ.get("BSC_RPC", "https://bsc-dataseed.bnbchain.org"),
    "base": os.environ.get("BASE_RPC", "https://mainnet.base.org"),
    "robinhood": os.environ.get("ROBINHOOD_RPC", "https://rpc.mainnet.chain.robinhood.com"),
}
GOPLUS_CHAIN = {"bsc": "56", "base": "8453", "robinhood": "4663"}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
DEAD = ("0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000")

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
    """적응형: 429를 받으면 속도를 낮추고(×0.7), 성공이 이어지면 천천히 원래 속도로 복귀."""
    def __init__(self, per_min, floor_per_min=2.0):
        self.base_gap = 60.0 / per_min
        self.gap = self.base_gap
        self.max_gap = 60.0 / floor_per_min
        self.last = 0.0
        self.ok_streak = 0
        self.n429 = 0

    def wait(self):
        d = self.last + self.gap - time.time()
        if d > 0:
            time.sleep(d)
        self.last = time.time()

    def success(self):
        self.ok_streak += 1
        if self.ok_streak >= 30 and self.gap > self.base_gap:
            self.gap = max(self.base_gap, self.gap * 0.9)
            self.ok_streak = 0

    def throttled(self):
        self.n429 += 1
        self.ok_streak = 0
        self.gap = min(self.max_gap, self.gap / 0.7)

    def rpm(self):
        return 60.0 / self.gap


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
            GT_LIMIT.throttled()
            ra = r.headers.get("retry-after")
            wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else 20.0
            log.warning("GT 429 — %.0f초 대기, 속도 분당 %.1f회로 조정 (누적 %d회)", wait, GT_LIMIT.rpm(), GT_LIMIT.n429)
            time.sleep(wait)
            continue
        if r.status_code == 404:
            return None
        if r.status_code >= 500:
            time.sleep(10)
            continue
        r.raise_for_status()
        GT_LIMIT.success()
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
CREATE TABLE IF NOT EXISTS flow_snaps(net TEXT, pool TEXT, mark INTEGER, ts REAL, flow TEXT, PRIMARY KEY(net, pool, mark));
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
    if "chain_reason2" not in cols:   # pass2 추가
        c.execute("ALTER TABLE checks ADD COLUMN chain_reason2 TEXT")
        c.execute("ALTER TABLE checks ADD COLUMN passed2 INTEGER")
    pcols = {r[1] for r in c.execute("PRAGMA table_info(pools)")}
    if "guide_done" not in pcols:
        c.execute("ALTER TABLE pools ADD COLUMN guide_done INTEGER DEFAULT 0")
        c.execute("ALTER TABLE pools ADD COLUMN v2_done INTEGER DEFAULT 0")
        c.execute("UPDATE pools SET guide_done=1, v2_done=1 WHERE done=1")
    c.execute("INSERT OR IGNORE INTO meta VALUES('pass2_started_at', ?)", (str(time.time()),))
    c.execute("INSERT OR IGNORE INTO meta VALUES('survivor_criteria_at', ?)", (str(time.time()),))
    c.execute("INSERT OR IGNORE INTO meta VALUES('surv2_criteria_at', ?)", (str(time.time()),))   # v9 확장 생존자 기준 고정   # 생존자 기준 고정 시각
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


# ── pass2용: 풀/프로그램 계정(PDA)을 제외한 '진짜 지갑' 최대 보유율 ──
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    b = n.to_bytes(32, "big") if n else b""
    return b.rjust(32, b"\0")[-32:]


def is_on_curve(addr):
    """일반 지갑 주소는 ed25519 곡선 위, PDA(풀 금고·락업·프로그램 소유)는 곡선 밖."""
    try:
        b = _b58decode(addr)
    except ValueError:
        return True
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    sign = b[31] >> 7
    if y >= _P:
        return False
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x = (u * pow(v, 3, _P) * pow(u * pow(v, 7, _P), (_P - 5) // 8, _P)) % _P
    vx2 = (v * x * x) % _P
    if vx2 == u:
        pass
    elif vx2 == (-u) % _P:
        x = (x * pow(2, (_P - 1) // 4, _P)) % _P
    else:
        return False
    return not (x == 0 and sign == 1)


def sol_top_wallet_ex_pda(mint):
    """상위 20개 토큰계정의 소유자를 조회해 PDA 소유(풀 금고 등)를 빼고, 소유자별 합산 최대 비율."""
    def q(method, params):
        r = requests.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        return r.json()["result"]
    try:
        supply = float(q("getTokenSupply", [mint])["value"]["amount"])
        top = q("getTokenLargestAccounts", [mint])["value"]
        if not supply or not top:
            return None
        infos = q("getMultipleAccounts", [[t["address"] for t in top], {"encoding": "jsonParsed"}])["value"]
        per_owner = {}
        for t, info in zip(top, infos):
            owner = (((info or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
            if not owner or not is_on_curve(owner):
                continue
            per_owner[owner] = per_owner.get(owner, 0.0) + float(t["amount"])
        return max(per_owner.values()) / supply if per_owner else 0.0
    except Exception as e:
        log.info("sol RPC(ex_pda) 실패 %s: %s", mint, e)
        return None


# ── LP(유동성) 안전성: 기록 전용, 필터 판정에는 아직 안 씀 ──
def _eth_call(net, to, data):
    r = requests.post(EVM_RPC[net], json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                          "params": [{"to": to, "data": data}, "latest"]},
                      headers={"user-agent": BROWSER_UA, "content-type": "application/json"}, timeout=15)
    j = r.json()
    if "error" in j or j.get("result") in (None, "0x"):
        return None
    return int(j["result"], 16)


def evm_lp_burn(net, pool):
    """V2 풀(LP가 ERC20)이면 LP 총량 중 소각주소(0xdead/0x0)에 있는 비율. V3 등은 lp_v2=False."""
    if net not in EVM_RPC:
        return {}
    try:
        supply = _eth_call(net, pool, "0x18160ddd")               # totalSupply()
        if not supply:
            return {"lp_v2": False}
        burned = 0
        for a in DEAD:
            b = _eth_call(net, pool, "0x70a08231" + a[2:].rjust(64, "0"))   # balanceOf(dead)
            burned += b or 0
        return {"lp_v2": True, "lp_burned_pct": burned / supply}
    except Exception as e:
        log.info("LP RPC 실패 %s %s: %s", net, pool, e)
        return {"lp_rpc_error": True}


GP_LIMIT = RateLimiter(float(os.environ.get("GOPLUS_RPM", "20")))


def goplus(net, token):
    """GoPlus 토큰 보안: LP 보유자·락 여부, 제작자 보유율, 세금, 권한 위험. 미지원 체인이면 빈 dict."""
    cid = GOPLUS_CHAIN.get(net)
    if not cid:
        return {}
    GP_LIMIT.wait()
    try:
        j = requests.get(f"https://api.gopluslabs.io/api/v1/token_security/{cid}",
                         params={"contract_addresses": token}, timeout=20).json()
        r = (j.get("result") or {}).get(token.lower())
        if not r:
            return {"gp_supported": False}
    except Exception as e:
        log.info("GoPlus 실패 %s %s: %s", net, token, e)
        return {"gp_error": True}
    lps = r.get("lp_holders") or []
    locked = sum(fnum(h.get("percent")) or 0 for h in lps
                 if str(h.get("is_locked")) == "1" or (h.get("address") or "").lower() in DEAD)
    top = max(lps, key=lambda h: fnum(h.get("percent")) or 0) if lps else {}
    return {
        "gp_supported": True,
        "gp_lp_holder_count": int(fnum(r.get("lp_holder_count")) or 0) if r.get("lp_holder_count") is not None else None,
        "gp_lp_locked_pct": locked if lps else None,
        "gp_top_lp_pct": fnum(top.get("percent")),
        "gp_top_lp_is_contract": top.get("is_contract"),
        "gp_creator_pct": fnum(r.get("creator_percent")),
        "gp_owner_pct": fnum(r.get("owner_percent")),
        "gp_buy_tax": fnum(r.get("buy_tax")), "gp_sell_tax": fnum(r.get("sell_tax")),
        "gp_is_honeypot": r.get("is_honeypot"), "gp_cannot_sell_all": r.get("cannot_sell_all"),
        "gp_is_mintable": r.get("is_mintable"), "gp_hidden_owner": r.get("hidden_owner"),
        "gp_owner_change_balance": r.get("owner_change_balance"), "gp_transfer_pausable": r.get("transfer_pausable"),
        "gp_is_open_source": r.get("is_open_source"),
    }


def lp_safe_pct(d):
    """LP 중 '뺄 수 없는' 비율(소각 또는 락). RPC와 GoPlus 중 큰 값. 모르면 None."""
    vals = [v for v in (d.get("lp_burned_pct"), d.get("gp_lp_locked_pct")) if v is not None]
    return max(vals) if vals else None


# ── 거래 흐름: '진짜 사람들의 거래'인지 '연출된 거래'인지 (기록 전용) ──
def trade_flow(net, pool):
    """최근 체결(최대 300건)로 지갑 분산도·자전거래·봇 흔적을 계산. 모든 체인 공통."""
    j = gt_get(f"/networks/{net}/pools/{pool}/trades")
    if not j:
        return {}
    rows = []
    for t in j.get("data", []):
        a = t.get("attributes") or {}
        w = (a.get("tx_from_address") or "").lower()
        v = fnum(a.get("volume_in_usd"))
        if not w or v is None:
            continue
        rows.append((w, v, a.get("kind"), a.get("block_number"), parse_ts(a.get("block_timestamp"))))
    if len(rows) < 10:
        return {"tr_n": len(rows)}
    vol_by_w, kinds_by_w, block_buys = {}, {}, {}
    for w, v, k, b, _ in rows:
        vol_by_w[w] = vol_by_w.get(w, 0) + v
        kinds_by_w.setdefault(w, set()).add(k)
        if k == "buy":
            block_buys[b] = block_buys.get(b, 0) + 1
    tot = sum(vol_by_w.values()) or 1
    top5 = sum(sorted(vol_by_w.values(), reverse=True)[:5])
    buys = [r for r in rows if r[2] == "buy"]
    sells = [r for r in rows if r[2] == "sell"]
    ts = [r[4] for r in rows if r[4]]
    span_min = (max(ts) - min(ts)) / 60 if len(ts) > 1 else None
    vols = sorted(r[1] for r in rows)
    return {
        "tr_n": len(rows),
        "tr_span_min": span_min,                                   # 300건이 몇 분 동안 쌓였나 (짧을수록 과열/봇)
        "tr_uniq_wallets": len(vol_by_w),
        "tr_trades_per_wallet": len(rows) / len(vol_by_w),        # 높으면 소수 지갑이 반복 거래(자전거래 의심)
        "tr_top5_vol_share": top5 / tot,                           # 상위 5지갑 거래량 비중
        "tr_uniq_buyers": len({r[0] for r in buys}),
        "tr_uniq_sellers": len({r[0] for r in sells}),
        "tr_roundtrip_wallet_share": sum(1 for k in kinds_by_w.values() if len(k) > 1) / len(kinds_by_w),
        "tr_dust_share": sum(1 for v in vols if v < 2) / len(vols),   # $2 미만 먼지 거래 비율(봇 스팸)
        "tr_bundle_buy_share": (sum(n for n in block_buys.values() if n >= 3) / len(buys)) if buys else None,
        "tr_median_usd": vols[len(vols) // 2],
        "tr_net_buy_flow": (sum(r[1] for r in buys) - sum(r[1] for r in sells)) / tot,
    }


def _sol_rpc(method, params, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
            j = r.json()
            if "result" in j:
                return j["result"]
            last = f"HTTP {r.status_code} {str(j.get('error'))[:160]}"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:160]}"
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{method} 실패: {last}")


def sol_holders(mint):
    """Solana 보유 구조를 한 번에: RPC 3회(공급량·상위20계정·소유자) + 실패 시 재시도.
    top_wallet_percent: 가이드 원본(가장 큰 토큰계정 그대로)
    top_wallet_ex_pda / top10_ex_pda: 풀 금고·프로그램 소유(PDA) 계정을 빼고 소유자별 합산"""
    try:
        supply = float(_sol_rpc("getTokenSupply", [mint])["value"]["amount"])
        top = _sol_rpc("getTokenLargestAccounts", [mint])["value"]
        if not supply or not top:
            return {"sol_rpc_error": "empty"}
        out = {"top_wallet_percent": float(top[0]["amount"]) / supply}
        infos = _sol_rpc("getMultipleAccounts", [[t["address"] for t in top], {"encoding": "jsonParsed"}])["value"]
        per_owner, pda_amt = {}, 0.0
        for t, info in zip(top, infos):
            owner = (((info or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
            amt = float(t["amount"])
            if not owner or not is_on_curve(owner):
                pda_amt += amt
                continue
            per_owner[owner] = per_owner.get(owner, 0.0) + amt
        vals = sorted(per_owner.values(), reverse=True)
        out.update({
            "top_wallet_ex_pda": (vals[0] / supply) if vals else 0.0,
            "top10_ex_pda": sum(vals[:10]) / supply,
            "pda_share_top20": pda_amt / supply,          # 상위20 중 풀·프로그램이 가진 비율 (참고)
        })
        return out
    except Exception as e:
        log.warning("sol RPC 실패 %s: %s", mint, e)
        return {"sol_rpc_error": str(e)[:200]}


def dossier(net, token, pool=None):
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
        d.update(sol_holders(token))
    elif pool:
        d.update(evm_lp_burn(net, pool))
        d.update(goplus(net, token))
        d["lp_safe_pct"] = lp_safe_pct(d)
    if pool:
        d.update(trade_flow(net, pool))
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


def chain_kill_v2(net, d):
    """pass2 규칙: (1) 값이 비면 탈락 (가이드 본문 'NEVER let null mean fine' 원칙대로)
                    (2) Solana 최대지갑은 풀/프로그램(PDA) 계정 제외 후 계산"""
    if net == "solana":
        tw = d.get("top_wallet_ex_pda")
        if tw is None:
            return "missing_top_wallet"
        if tw > HARD["max_top_wallet"]:
            return "top_wallet_ex_pda"
        t10 = d.get("top10_ex_pda")        # GT top10은 풀 금고를 포함해 신규 토큰이 거의 다 걸림 → RPC로 직접 계산
        if t10 is None:
            return "missing_top_10"
        if t10 > HARD["max_top_10"]:
            return "top_10_ex_pda"
    elif d.get("top_10_percent") is None:
        return "missing_top_10"
    if net != "solana" and d["top_10_percent"] / 100 > HARD["max_top_10"]:
        return "top_10"
    if d.get("holder_count") is None:
        return "missing_holders"
    if d["holder_count"] < HARD["min_holders"]:
        return "holders"
    if net == "solana" and (_authority_open(d.get("mint_authority")) or _authority_open(d.get("freeze_authority"))):
        return "authority_open"
    if net in ("bsc", "base"):
        hp = d.get("is_honeypot")
        if hp is None or str(hp).lower() == "unknown":
            return "missing_honeypot"
        if hp in (True, "true", "yes"):
            return "honeypot"
    return None


PERMANENT_GUIDE = ("honeypot", "authority_open", "top_wallet")
PERMANENT_V2 = ("honeypot", "authority_open")


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
    """pass(가이드 원본 규칙)와 pass2(수정 규칙)를 같은 평가 시점·같은 데이터로 동시에 판정."""
    now = time.time()
    rows = [r for r in db.execute("SELECT net,pool,token,created_at,next_idx,has_control,guide_done,v2_done FROM pools WHERE done=0")
            if r[0] in NETWORKS]          # 수집 중단한 체인은 신규 평가 안 함 (이미 진입한 건의 가격 추적은 계속)
    for r in rows:
        if r[4] >= len(CHECK_AGES_MIN) or (now - r[3]) > HARD["max_age_hours"] * 3600 + 3600:
            db.execute("UPDATE pools SET done=1 WHERE net=? AND pool=?", (r[0], r[1]))
    due = [r for r in rows if r[4] < len(CHECK_AGES_MIN) and (now - r[3]) / 60 >= CHECK_AGES_MIN[r[4]]][:max_pools]
    by_net = {}
    for r in due:
        by_net.setdefault(r[0], []).append(r)
    n_eval = n_pass = n_pass2 = 0
    for net, lst in by_net.items():
        metrics = fetch_multi(net, [r[1] for r in lst])
        for (_, pool, token, created, idx, has_control, g_done, v_done) in lst:
            age_min = (now - created) / 60
            m = metrics.get(pool)
            db.execute("UPDATE pools SET next_idx=? WHERE net=? AND pool=?", (next_idx_after(age_min), net, pool))
            if not m or m["price"] is None:
                continue
            n_eval += 1
            hard = free_kill(m, age_min) or trade_kill(m)
            chain_r = chain_r2 = dos = None
            if hard is None:
                dos = dossier(net, token, pool)
                chain_r = chain_kill(net, dos) if dos else "dossier_failed"
                chain_r2 = chain_kill_v2(net, dos) if dos else "dossier_failed"
            passed = int(not g_done and hard is None and chain_r is None)
            passed2 = int(not v_done and hard is None and chain_r2 is None)
            if chain_r in PERMANENT_GUIDE:
                g_done = 1
            if chain_r2 in PERMANENT_V2:
                v_done = 1
            cur = db.execute(
                "INSERT INTO checks(net,pool,ts,age_min,price,liq,mcap,vol_h1,vol_h6,vol_h24,buys_h1,sells_h1,trades_h24,"
                "chg_h1,chg_h24,hard_reason,chain_reason,dossier,passed,extra,chain_reason2,passed2) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (net, pool, now, age_min, m["price"], m["liq"], m["mcap"], m["vol_h1"], m["vol_h6"], m["vol_h24"],
                 m["buys_h1"], m["sells_h1"], m["trades_h24"], m["chg_h1"], m["chg_h24"], hard, chain_r,
                 json.dumps(dos) if dos else None, passed, json.dumps(m["extra"]), chain_r2, passed2))
            cid = cur.lastrowid
            for grp, ok in (("pass", passed), ("pass2", passed2)):
                if ok:
                    db.execute("INSERT OR IGNORE INTO entries(net,pool,grp,ts,price,liq,check_id,next_snap) VALUES(?,?,?,?,?,?,?,?)",
                               (net, pool, grp, now, m["price"], m["liq"], cid, now + 900))
                    log.info("%s %s CA=%s (pool=%s) age=%.0fm liq=%.0f mcap=%.0f lp_safe=%s",
                             grp.upper(), net, token, pool, age_min, m["liq"] or 0, m["mcap"] or 0,
                             (dos or {}).get("lp_safe_pct"))
            n_pass += passed
            n_pass2 += passed2
            g_done = g_done or passed
            v_done = v_done or passed2
            if hard is None:
                # v9: 하드필터(유동성·거래량·시총·거래수)를 처음 통과한 시점을 기준으로 생존 추적 (체인필터 결과와 무관)
                db.execute("INSERT OR IGNORE INTO entries(net,pool,grp,ts,price,liq,check_id,next_snap) VALUES(?,?,?,?,?,?,?,?)",
                           (net, pool, "surv", now, m["price"], m["liq"], cid, now + SURV_SNAP_HOURS[0] * 3600))
            if not passed and not has_control and age_min >= HARD["min_age_minutes"]:
                # 대조군: 처음 '평가 가능 나이'에 가이드 규칙으로 탈락한 토큰, 예산 위해 샘플링
                has_control = 1
                if random.random() < CONTROL_SAMPLE:
                    db.execute("INSERT OR IGNORE INTO entries(net,pool,grp,ts,price,liq,check_id,next_snap) VALUES(?,?,?,?,?,?,?,?)",
                               (net, pool, "control", now, m["price"], m["liq"], cid, now + CONTROL_SNAP_HOURS[0] * 3600))
            db.execute("UPDATE pools SET guide_done=?, v2_done=?, has_control=?, done=? WHERE net=? AND pool=?",
                       (g_done, v_done, has_control, int(bool(g_done and v_done)), net, pool))
    db.commit()
    if due:
        log.info("evaluate: %d개 평가, pass %d, pass2 %d", n_eval, n_pass, n_pass2)


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
            # v9.4: 12h(=생존자 진입 시점)에 체결 내역으로 '진짜 사람 거래'인지 기록 (토큰당 1회)
            if m and now - ets >= 12 * 3600 - 900 and grp in ("pass", "pass2", "surv"):
                if not db.execute("SELECT 1 FROM flow_snaps WHERE net=? AND pool=? AND mark=12", (net, pool)).fetchone():
                    fl = trade_flow(net, pool)
                    db.execute("INSERT OR IGNORE INTO flow_snaps VALUES(?,?,?,?,?)", (net, pool, 12, now, json.dumps(fl)))
            if grp in ("pass", "pass2"):
                nxt = now + 900
                closed = int(nxt > ets + PASS_SNAP_HOURS * 3600 + 600)
                db.execute("UPDATE entries SET next_snap=?, snap_idx=?, closed=? WHERE net=? AND pool=? AND grp=?",
                           (nxt, sidx + 1, closed, net, pool, grp))
            else:
                sched = SNAP_SCHEDULE.get(grp, CONTROL_SNAP_HOURS)
                k = sidx + 1
                if k >= len(sched):
                    db.execute("UPDATE entries SET snap_idx=?, closed=1 WHERE net=? AND pool=? AND grp=?", (k, net, pool, grp))
                else:
                    db.execute("UPDATE entries SET next_snap=?, snap_idx=? WHERE net=? AND pool=? AND grp=?",
                               (ets + sched[k] * 3600, k, net, pool, grp))
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
    log.info("버전 %s", VERSION)
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
