import asyncio
import aiohttp
import json
import time
import os

STATE_FILE    = "bot_state.json"
INITIAL_BET   = 10.0    # first buy amount in $
BET_MULT      = 1.5     # multiply bet by this on each subsequent drop
DROP_STEP     = 0.10    # price drop that triggers next buy
TP_PCT        = 0.10    # take profit: sell all when price is 10% above avg entry
POLL_INTERVAL = 0.15
CLOB_BASE     = "https://clob.polymarket.com"
PRINT_EVERY   = 20

GREEN      = "\033[32m"
BOLD_GREEN = "\033[1;32m"
YELLOW     = "\033[33m"
RESET      = "\033[0m"

def cap(v):    return f"{BOLD_GREEN}${v:.2f}{RESET}"
def up_s(s):   return f"{GREEN}{s}{RESET}"
def warn(s):   return f"{YELLOW}{s}{RESET}"

# ─────────────────────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.capital         = 1000.0
        self.up_token        = None
        self.down_token      = None
        self.trade_window    = None
        self.phase           = "waiting"   # waiting / monitoring / done

        # martingale position
        self.up_shares       = 0.0
        self.up_cost         = 0.0        # total $ spent
        self.reference_price = None       # price from which we measure next drop
        self.current_bet     = INITIAL_BET
        self.buy_count       = 0
        self.poll_count      = 0

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            d = json.load(f)
        s = BotState()
        s.capital         = d.get("capital", 1000.0)
        s.up_token        = d.get("up_token")
        s.down_token      = d.get("down_token")
        s.trade_window    = d.get("trade_window")
        s.phase           = d.get("phase", "waiting")
        s.up_shares       = d.get("up_shares", 0.0)
        s.up_cost         = d.get("up_cost", 0.0)
        s.reference_price = d.get("reference_price")
        s.current_bet     = d.get("current_bet", INITIAL_BET)
        s.buy_count       = d.get("buy_count", 0)
        return s
    return BotState()

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump({
            "capital":         round(s.capital, 4),
            "up_token":        s.up_token,
            "down_token":      s.down_token,
            "trade_window":    s.trade_window,
            "phase":           s.phase,
            "up_shares":       round(s.up_shares, 6),
            "up_cost":         round(s.up_cost, 4),
            "reference_price": s.reference_price,
            "current_bet":     round(s.current_bet, 4),
            "buy_count":       s.buy_count,
        }, f, indent=2)

# ── API helpers ───────────────────────────────────────────────────────────────

async def fetch_gamma(session, slug):
    try:
        async with session.get(
            f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=3
        ) as r:
            if r.status == 200:
                return await r.json()
    except:
        pass
    return None

async def get_best_ask(session, token_id):
    if not token_id:
        return 0.5
    try:
        async with session.get(
            f"{CLOB_BASE}/price?token_id={token_id}&side=SELL", timeout=2
        ) as r:
            if r.status == 200:
                return float((await r.json()).get("price", 0.5))
    except:
        pass
    return 0.5

async def get_best_bid(session, token_id):
    if not token_id:
        return 0.01
    try:
        async with session.get(
            f"{CLOB_BASE}/price?token_id={token_id}&side=BUY", timeout=2
        ) as r:
            if r.status == 200:
                return float((await r.json()).get("price", 0.01))
    except:
        pass
    return 0.01

def get_tokens(market):
    ids = market.get("clobTokenIds", "[]")
    ids = json.loads(ids) if isinstance(ids, str) else ids
    return (ids[0] if ids else None, ids[1] if len(ids) > 1 else None)

# ── trade helpers ─────────────────────────────────────────────────────────────

def avg_entry(state):
    if state.up_shares <= 0:
        return 0.0
    return state.up_cost / state.up_shares

def tp_price(state):
    return avg_entry(state) * (1 + TP_PCT)

def rt_capital(state, up_ask):
    """Real-time capital: settled cash + mark-to-market of open position."""
    return state.capital - state.up_cost + state.up_shares * up_ask

async def buy_up(state, session, up_ask):
    """Buy $current_bet worth of UP shares at market ask."""
    bet     = state.current_bet
    shares  = bet / up_ask
    state.capital  -= bet
    state.up_shares += shares
    state.up_cost   += bet
    state.reference_price = up_ask
    state.buy_count += 1
    prev_bet         = state.current_bet
    state.current_bet = round(state.current_bet * BET_MULT, 4)

    avg = avg_entry(state)
    tp  = tp_price(state)
    print(f"🛒 BUY #{state.buy_count} {up_s(f'UP @ {up_ask:.4f}')} | "
          f"${prev_bet:.2f} → {shares:.4f} shares | "
          f"avg entry {avg:.4f} | TP @ {tp:.4f} | "
          f"next bet ${state.current_bet:.2f} | Capital {cap(state.capital)}")
    save_state(state)

async def sell_all(state, session, reason="TP"):
    """Sell all UP shares at market bid."""
    if state.up_shares <= 0:
        return
    bid      = await get_best_bid(session, state.up_token)
    proceeds = state.up_shares * bid
    net      = proceeds - state.up_cost
    state.capital += net
    pnl = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
    print(f"{'🎯' if reason=='TP' else '⏰'} {reason} — "
          f"sell {up_s(f'{state.up_shares:.4f} UP @ {bid:.4f}')} | "
          f"cost ${state.up_cost:.2f} | net {pnl} | Capital {cap(state.capital)}")
    state.up_shares  = 0.0
    state.up_cost    = 0.0

def reset_martingale(state, new_reference):
    """Reset bet sizing and reference price (after TP or between rounds)."""
    state.current_bet     = INITIAL_BET
    state.buy_count       = 0
    state.reference_price = new_reference

# ─────────────────────────────────────────────────────────────────────────────

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Martingale Bot | Capital {cap(state.capital)} | Phase: {state.phase}")
    print(f"   {up_s('UP only')} | drop step {DROP_STEP} | "
          f"initial bet ${INITIAL_BET} × {BET_MULT} | TP +{int(TP_PCT*100)}%")

    async with aiohttp.ClientSession() as session:
        while True:
            now            = int(time.time())
            current_window = (now // 300) * 300
            secs_elapsed   = now - current_window
            secs_to_next   = 300 - secs_elapsed
            state.poll_count += 1

            # ── PHASE: waiting ────────────────────────────────────────────
            if state.phase == "waiting":
                slug       = f"btc-updown-5m-{current_window}"
                event_data = await fetch_gamma(session, slug)
                if event_data:
                    market = event_data[0].get("markets", [event_data[0]])[0]
                    up_tok, dn_tok = get_tokens(market)
                    if up_tok and dn_tok:
                        state.up_token    = up_tok
                        state.down_token  = dn_tok
                        state.trade_window = current_window
                        state.phase        = "monitoring"
                        up_ask = await get_best_ask(session, up_tok)
                        reset_martingale(state, up_ask)
                        state.up_shares = state.up_cost = 0.0
                        save_state(state)
                        print(f"🟢 WINDOW LIVE {slug} | {up_s(f'UP @ {up_ask:.4f}')} | "
                              f"reference set | Capital {cap(state.capital)}")
                    elif state.poll_count % PRINT_EVERY == 0:
                        print(f"⏳ fetching market data… T+{secs_elapsed}s")
                elif state.poll_count % PRINT_EVERY == 0:
                    print(f"⏳ waiting | T+{secs_elapsed}s elapsed | Capital {cap(state.capital)}")

            # ── PHASE: monitoring ─────────────────────────────────────────
            elif state.phase == "monitoring":
                # window expired
                if state.trade_window and now >= state.trade_window + 300:
                    # settle open UP position at market
                    if state.up_shares > 0:
                        await sell_all(state, session, reason="EXPIRY")
                    else:
                        print(f"⏰ EXPIRY — no open position | Capital {cap(state.capital)}")
                    state.phase = "done"
                    save_state(state)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                up_ask = await get_best_ask(session, state.up_token)
                rc     = rt_capital(state, up_ask)

                # ── status print ──────────────────────────────────────────
                if state.poll_count % PRINT_EVERY == 0:
                    if state.up_shares > 0:
                        avg = avg_entry(state)
                        tp  = tp_price(state)
                        unrealized = state.up_shares * up_ask - state.up_cost
                        u_str = f"+${unrealized:.2f}" if unrealized >= 0 else f"-${abs(unrealized):.2f}"
                        print(f"📊 {up_s(f'UP {up_ask:.4f}')} | "
                              f"{state.up_shares:.4f} shares | avg {avg:.4f} | "
                              f"TP @ {tp:.4f} | unrealized {u_str} | "
                              f"next buy < {state.reference_price - DROP_STEP:.4f} | "
                              f"Real-time Capital {cap(rc)}")
                    else:
                        print(f"👀 {up_s(f'UP {up_ask:.4f}')} | "
                              f"next buy < {state.reference_price - DROP_STEP:.4f} | "
                              f"bet ${state.current_bet:.2f} | Capital {cap(rc)}")

                # ── TAKE PROFIT ───────────────────────────────────────────
                if state.up_shares > 0 and up_ask >= tp_price(state):
                    print(f"🎯 TAKE PROFIT — {up_s(f'UP {up_ask:.4f}')} >= TP {tp_price(state):.4f}")
                    await sell_all(state, session, reason="TP")
                    # restart within same window
                    reset_martingale(state, up_ask)
                    save_state(state)
                    print(f"🔄 Restarting within window | ref {up_ask:.4f} | next buy < {up_ask - DROP_STEP:.4f}")

                # ── BUY: price dropped DROP_STEP from reference ───────────
                elif up_ask <= state.reference_price - DROP_STEP:
                    await buy_up(state, session, up_ask)

            # ── PHASE: done ───────────────────────────────────────────────
            elif state.phase == "done":
                print(f"✔️  Round complete | Capital {cap(state.capital)}")
                state.up_token    = state.down_token  = None
                state.trade_window = None
                state.up_shares    = state.up_cost = 0.0
                reset_martingale(state, None)
                state.poll_count = 0
                state.phase = "waiting"
                save_state(state)

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
