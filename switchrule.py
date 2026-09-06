import time
import requests
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple, List

# ============================================================
# CONFIG
# ============================================================
BASE_URL = "http://localhost:10002/v1"
API_KEY  = "G3PE3PRN"
HEADERS  = {"X-API-Key": API_KEY}

TICKERS = ["RITC", "COMP"]

# Orders
DRY_RUN = False
MAX_CHILD_ORDER = 10_000
LOOP_SLEEP = 0.10

# --- Costs / buffers (tune per sub-heat security table) ---
COMMISSION_PER_SHARE = 0.02
SAFETY_BUFFER_PER_SHARE = 0.01
COST_PER_SHARE = COMMISSION_PER_SHARE + SAFETY_BUFFER_PER_SHARE

# --- Phase switching ---
PROFITABLE_WINDOW_SECS = 10.0   # Phase 1 max length (auto-switch after this)
EARLY_CHECK_SECS = 5.0          # if no progress by this time, switch early
MIN_PROGRESS = 0.30             # need 30% position reduction by EARLY_CHECK_SECS or switch

# --- Liquidity thresholds using /securities (top-of-book) ---
LIQ_THRESH = {
    "HIGH":   {"max_spread": 0.02, "min_depth": 20000},
    "MEDIUM": {"max_spread": 0.05, "min_depth": 8000},
}

# Liquidity -> execution multipliers
LIQ_EXEC = {
    "THIN":   {"slice_mult": 0.50, "poll_mult": 1.50, "slip_add": 1},
    "MEDIUM": {"slice_mult": 1.00, "poll_mult": 1.00, "slip_add": 0},
    "HIGH":   {"slice_mult": 1.50, "poll_mult": 0.80, "slip_add": 0},
}

# Base per-ticker settings (starting point)
EXEC_PARAMS = {
    "RITC": {"base_slice": 2000, "base_poll": 0.80, "base_slip_cents": 1},
    "COMP": {"base_slice": 800,  "base_poll": 0.40, "base_slip_cents": 2},
}

# ============================================================
# HTTP helpers
# ============================================================
def http_get(path: str, params: Optional[dict] = None) -> Any:
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=5)
    r.raise_for_status()
    return r.json()

def http_post_orders(payload: dict) -> requests.Response:
    """
    Tries a few common submission styles for picky gateways.
    """
    url = f"{BASE_URL}/orders"
    attempts = [
        ("json",        lambda: requests.post(url, headers=HEADERS, json=payload, timeout=5)),
        ("data",        lambda: requests.post(url, headers=HEADERS, data=payload, timeout=5)),
        ("params",      lambda: requests.post(url, headers=HEADERS, params=payload, timeout=5)),
        ("data+params", lambda: requests.post(url, headers=HEADERS, data=payload, params=payload, timeout=5)),
    ]
    last = None
    for _name, fn in attempts:
        resp = fn()
        last = resp
        if resp.status_code < 400:
            return resp
    return last

# ============================================================
# Tender reading: GET /v1/tenders
# ============================================================
@dataclass
class TenderInfo:
    tender_id: str
    ticker: str
    tender_side: str   # "buy" means tender buys from you (you become short); "sell" means tender sells to you (you become long)
    price: float
    qty: int
    seen_time: float

def get_tenders() -> List[Dict[str, Any]]:
    data = http_get("/tenders")
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected /tenders response type: {type(data)}")
    return data

def parse_tender(t: Dict[str, Any]) -> TenderInfo:
    tid = str(t.get("tender_id", t.get("id")))
    ticker = str(t.get("ticker", t.get("symbol", ""))).upper()
    price = float(t.get("price"))
    qty = int(t.get("quantity", t.get("qty", t.get("volume"))))

    raw = (t.get("action") or t.get("side") or t.get("direction") or "").upper()
    if raw in ("BUY", "BID"):
        tender_side = "buy"
    elif raw in ("SELL", "ASK", "OFFER"):
        tender_side = "sell"
    else:
        raise ValueError(f"Unknown tender side/action value: {raw} | tender={t}")

    return TenderInfo(
        tender_id=tid,
        ticker=ticker,
        tender_side=tender_side,
        price=price,
        qty=qty,
        seen_time=time.time(),
    )

# ============================================================
# Market snapshot: GET /v1/securities (top-of-book)
# ============================================================
def securities_list() -> List[Dict[str, Any]]:
    data = http_get("/securities")
    if isinstance(data, dict) and "securities" in data:
        data = data["securities"]
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected /securities response type: {type(data)}")
    return data

def securities_map() -> Dict[str, Dict[str, Any]]:
    return {row["ticker"]: row for row in securities_list() if "ticker" in row}

def get_position(row: Dict[str, Any]) -> int:
    return int(float(row.get("position", 0)))

def top_liquidity_metrics(row: Dict[str, Any]) -> Dict[str, Optional[float]]:
    bid = row.get("bid")
    ask = row.get("ask")
    bid_sz = row.get("bid_size")
    ask_sz = row.get("ask_size")
    if bid is None or ask is None or bid_sz is None or ask_sz is None:
        return {"bid": None, "ask": None, "spread": None, "top_depth": None}
    bid = float(bid); ask = float(ask)
    bid_sz = float(bid_sz); ask_sz = float(ask_sz)
    return {
        "bid": bid,
        "ask": ask,
        "spread": ask - bid,
        "top_depth": bid_sz + ask_sz,
    }

def classify_liquidity(spread: Optional[float], top_depth: Optional[float]) -> str:
    if spread is None or top_depth is None:
        return "UNKNOWN"
    if spread <= LIQ_THRESH["HIGH"]["max_spread"] and top_depth >= LIQ_THRESH["HIGH"]["min_depth"]:
        return "HIGH"
    if spread <= LIQ_THRESH["MEDIUM"]["max_spread"] and top_depth >= LIQ_THRESH["MEDIUM"]["min_depth"]:
        return "MEDIUM"
    return "THIN"

# ============================================================
# Orders
# ============================================================
def place_marketable_limit(ticker: str, side: str, qty: int, price: float) -> Dict[str, Any]:
    side = side.upper()
    qty = int(max(1, min(qty, MAX_CHILD_ORDER)))
    price = round(float(price), 2)

    payloads = [
        {"ticker": ticker, "action": side, "type": "LIMIT", "quantity": qty, "price": price},
        {"ticker": ticker, "side":   side, "type": "LIMIT", "quantity": qty, "price": price},
        {"symbol": ticker, "action": side, "type": "LIMIT", "quantity": qty, "price": price},
        {"symbol": ticker, "side":   side, "type": "LIMIT", "quantity": qty, "price": price},
    ]

    if DRY_RUN:
        print(f"  [DRY_RUN ORDER] {payloads[0]}")
        return {"dry_run": True, **payloads[0]}

    last_resp = None
    last_payload = None
    for p in payloads:
        resp = http_post_orders(p)
        last_resp = resp
        last_payload = p
        if resp.status_code < 400:
            try:
                return resp.json()
            except Exception:
                return {"status_code": resp.status_code, "text": resp.text}

    print("\n[ORDER ERROR]")
    print("Payload:", last_payload)
    print("Status :", last_resp.status_code if last_resp else None)
    print("Body   :", last_resp.text if last_resp else None)
    raise requests.HTTPError(f"Order failed status={last_resp.status_code if last_resp else None}")

# ============================================================
# Phase logic
# ============================================================
@dataclass
class TenderCtx:
    ticker: str
    tender_price: float
    tender_side: str     # "buy" or "sell" (tender perspective)
    start_time: float
    start_pos: int
    tender_id: str

def profitable_bounds(tender_side: str, tender_price: float, cost_per_share: float) -> Tuple[Optional[float], Optional[float]]:
    # returns (max_buy_price, min_sell_price)
    if tender_side == "buy":      # tender buys from you => you're short => BUY to cover
        return tender_price - cost_per_share, None
    else:                         # tender sells to you => you're long => SELL to exit
        return None, tender_price + cost_per_share

def progress_fraction(start_pos: int, current_pos: int) -> float:
    start_abs = max(1, abs(start_pos))
    return (start_abs - abs(current_pos)) / start_abs  # 0..1

def pick_phase(ctx: TenderCtx, current_pos: int) -> str:
    elapsed = time.time() - ctx.start_time
    prog = progress_fraction(ctx.start_pos, current_pos)

    # Automatic switch after timeframe:
    if elapsed >= PROFITABLE_WINDOW_SECS:
        return "PHASE2"

    # Early switch if you're not reducing position enough:
    if elapsed >= EARLY_CHECK_SECS and prog < MIN_PROGRESS:
        return "PHASE2"

    return "PHASE1"

# ============================================================
# Unwinder
# ============================================================
@dataclass
class UnwindState:
    last_order_time: float

class TwoPhaseUnwinder:
    def __init__(self, tickers: List[str]):
        self.tickers = tickers
        self.unwind_state: Dict[str, Optional[UnwindState]] = {t: None for t in tickers}
        self.ctx: Dict[str, Optional[TenderCtx]] = {t: None for t in tickers}

        # Track most recent tender per ticker (seen from /tenders)
        self.latest_tender: Dict[str, Optional[TenderInfo]] = {t: None for t in tickers}
        self.seen_tender_ids = set()

    def update_latest_tenders(self):
        try:
            tenders = get_tenders()
        except Exception as e:
            print("[TENDER READ ERROR]", e)
            return

        for raw in tenders:
            try:
                ti = parse_tender(raw)
            except Exception:
                continue

            if ti.tender_id in self.seen_tender_ids:
                continue
            self.seen_tender_ids.add(ti.tender_id)

            if ti.ticker in self.latest_tender:
                self.latest_tender[ti.ticker] = ti
                print(f"[TENDER SEEN] {ti.ticker} id={ti.tender_id} side={ti.tender_side} px={ti.price} qty={ti.qty}")

    def compute_exec_params(self, ticker: str, liq_level: str) -> Tuple[int, float, int]:
        base = EXEC_PARAMS[ticker]
        adj = LIQ_EXEC.get(liq_level, LIQ_EXEC["MEDIUM"])

        slice_qty = int(base["base_slice"] * adj["slice_mult"])
        slice_qty = max(1, min(slice_qty, MAX_CHILD_ORDER))

        poll_secs = float(base["base_poll"] * adj["poll_mult"])
        poll_secs = max(0.05, poll_secs)

        slip_cents = int(base["base_slip_cents"] + adj["slip_add"])
        slip_cents = max(0, slip_cents)

        return slice_qty, poll_secs, slip_cents

    def maybe_attach_ctx(self, ticker: str, pos: int):
        """
        If position just became non-zero and we don't have a ctx, attach the latest seen tender.
        (This assumes you accepted manually; position change is your "accept" event.)
        """
        if self.ctx[ticker] is not None:
            return

        lt = self.latest_tender.get(ticker)
        if lt is None:
            return

        # attach tender context
        self.ctx[ticker] = TenderCtx(
            ticker=ticker,
            tender_price=lt.price,
            tender_side=lt.tender_side,
            start_time=time.time(),
            start_pos=pos,
            tender_id=lt.tender_id
        )
        print(f"[CTX ATTACHED] {ticker} from tender {lt.tender_id} (side={lt.tender_side} px={lt.price}) start_pos={pos}")

    def step(self, ticker: str, row: Dict[str, Any]):
        pos = get_position(row)
        m = top_liquidity_metrics(row)
        liq = classify_liquidity(m["spread"], m["top_depth"])

        # flat -> clear state
        if pos == 0:
            if self.unwind_state[ticker] is not None:
                print(f"[FLAT] {ticker} unwind complete.")
            self.unwind_state[ticker] = None
            self.ctx[ticker] = None
            return

        # attach ctx if possible (manual accept detection)
        self.maybe_attach_ctx(ticker, pos)

        # start unwind state
        if self.unwind_state[ticker] is None:
            self.unwind_state[ticker] = UnwindState(last_order_time=0.0)
            print(f"[UNWIND START] {ticker} pos={pos}")

        st = self.unwind_state[ticker]

        # need bid/ask
        bid = m["bid"]; ask = m["ask"]
        if bid is None or ask is None:
            return

        # compute execution knobs
        target_slice, poll_secs, slip_cents = self.compute_exec_params(ticker, liq)

        # pacing
        if time.time() - st.last_order_time < poll_secs:
            return

        side = "SELL" if pos > 0 else "BUY"
        qty = min(abs(pos), target_slice, MAX_CHILD_ORDER)
        qty = max(1, int(qty))

        # base aggressive price (phase2 default)
        slip = slip_cents / 100.0
        aggressive_px = (ask + slip) if side == "BUY" else (bid - slip)

        # phase-based "profitable-first" cap/floor
        ctx = self.ctx.get(ticker)
        phase = "NOCTX"
        price = aggressive_px

        if ctx is not None:
            phase = pick_phase(ctx, pos)
            max_buy, min_sell = profitable_bounds(ctx.tender_side, ctx.tender_price, COST_PER_SHARE)

            if phase == "PHASE1":
                # trade immediately, but only at profitable prices
                if side == "BUY" and max_buy is not None:
                    price = min(aggressive_px, round(max_buy, 2))
                elif side == "SELL" and min_sell is not None:
                    price = max(aggressive_px, round(min_sell, 2))
                else:
                    price = aggressive_px
            else:
                # phase2: flatten (allow give-up)
                price = aggressive_px

        place_marketable_limit(ticker, side, qty, price)
        st.last_order_time = time.time()

        prog = progress_fraction(ctx.start_pos, pos) if ctx else float("nan")
        print(
            f"[{phase}] {ticker} {side} {qty} @ {price:.2f} | pos={pos} prog={prog:.2f} | "
            f"liq={liq} spr={m['spread'] if m['spread'] is not None else float('nan'):.2f} "
            f"depth={int(m['top_depth']) if m['top_depth'] is not None else 0} "
            f"(slice={target_slice}, poll={poll_secs:.2f}s, slip={slip_cents}c) "
            + (f"| tender_px={ctx.tender_price:.2f} tender_side={ctx.tender_side} id={ctx.tender_id}" if ctx else "")
        )

    def run(self):
        print("Two-phase (profitable-first -> risk-first) unwinder running.")
        print("DRY_RUN:", DRY_RUN)
        print("Tickers:", ", ".join(self.tickers))
        print("Switch rule:")
        print(f"  - Phase1 until elapsed>={PROFITABLE_WINDOW_SECS}s OR (elapsed>={EARLY_CHECK_SECS}s and progress<{MIN_PROGRESS})\n")

        while True:
            try:
                # update tender feed (so ctx can be attached when position appears)
                self.update_latest_tenders()

                smap = securities_map()
                for t in self.tickers:
                    if t not in smap:
                        continue
                    self.step(t, smap[t])

                time.sleep(LOOP_SLEEP)

            except KeyboardInterrupt:
                print("\nStopping.")
                break
            except Exception as e:
                print("[LOOP ERROR]", e)
                time.sleep(0.25)

def main():
    smap = securities_map()
    missing = [t for t in TICKERS if t not in smap]
    if missing:
        print("[ERROR] Missing tickers:", missing)
        print("Available:", ", ".join(sorted(smap.keys())))
        return

    bot = TwoPhaseUnwinder(TICKERS)
    bot.run()

if __name__ == "__main__":
    main()
