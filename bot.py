import asyncio
import aiohttp
import json
import time
import os

STATE_FILE    = "bot_state.json"
TRIGGER       = 0.60    # price that activates grid on a side
SWITCH        = 0.50    # if active side drops here, switch to opposite
GRID_STEP     = 0.05    # interval between buy and sell levels
GRID_SHARES   = 20      # shares per grid buy
POLL_INTERVAL = 0.15
CLOB_BASE     = "https://clob.polymarket.com"
PRINT_EVERY   = 20

GREEN      = "\033[32m"
RED        = "\033[31m"
BOLD_GREEN = "\033[1;32m"
RESET      = "\033[0m"

def cap(v):              return f"{BOLD_GREEN}${v:.2f}{RESET}"
def up_s(s):             return f"{GREEN}{s}{RESET}"
def dn_s(s):             return f"{RED}{s}{RESET}"
def side_s(side, s):     return up_s(s) if side == "up" else dn_s(s)

# ─────────────────────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.capital        = 1000.0
        self.up_token       = None
        self.down_token     = None
        self.trade_window   = None
        self.phase          = "waiting"  # waiting / watching / grid / done
        # grid state
        self.active_side    = None       # "up" or "down"
        self.last_buy_price = None       # reference level for grid steps
        self.up_shares      = 0.0
        self.up_cost        = 0.0
        self.dn_shares      = 0.0
        self.dn_cost        = 0.0
        self.poll_count     = 0

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            d = json.load(f)
        s = BotState()
        s.capital        = d.get("capital", 1000.0)
        s.up_token       = d.get("up_token")
        s.down_token     = d.get("down_token")
        s.trade_window   = d.get("trade_window")
        s.phase          = d.get("phase", "waiting")
        s.active_side    = d.get("active_side")
        s.last_buy_price = d.get("last_buy_price")
        s.up_shares      = d.get("up_shares", 0.0)
        s.up_cost        = d.get("up_cost", 0.0)
        s.dn_shares      = d.get("dn_shares", 0.0)
        s.dn_cost        = d.get("dn_cost", 0.0)
        return s
    return BotState()

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump({
            "capital":        round(s.capital, 4),
            "up_token":       s.up_token,
            "down_token":     s.down_token,
            "trade_window":   s.trade_window,
            "phase":          s.phase,
            "active_side":    s.active_side,
            "last_buy_price": s.last_buy_price,
            "up_shares":      s.up_shares,
            "up_cost":        round(s.up_cost, 4),
            "dn_shares":      s.dn_shares,
            "dn_cost":        round(s.dn_cost, 4),
        }, f, indent=2)

# ── API helpers ───────────────────────────────────────────────────────────────

async def fetch_gamma(session, slug):
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        async with session.get(url, timeout=3) as r:
            if r.status == 200:
                return await r.json()
    except:
        pass
    return None

async def get_best_ask(session, token_id):
    if not token_id:
        return 0.5
    try:
        async with session.get(f"{CLOB_BASE}/price?token_id={token_id}&side=SELL", timeout=2) as r:
            if r.status == 200:
                return float((await r.json()).get("price", 0.5))
    except:
        pass
    return 0.5

async def get_best_bid(session, token_id):
    if not token_id:
        return 0.01
    try:
        async with session.get(f"{CLOB_BASE}/price?token_id={token_id}&side=BUY", timeout=2) as r:
            if r.status == 200:
                return float((await r.json()).get("price", 0.01))
    except:
        pass
    return 0.01

def get_tokens(market):
    ids = market.get("clobTokenIds", "[]")
    ids = json.loads(ids) if isinstance(ids, str) else ids
    return (ids[0] if ids else None, ids[1] if len(ids) > 1 else None)

# ── grid helpers ──────────────────────────────────────────────────────────────

def grid_level(price):
    """Round price to nearest GRID_STEP boundary."""
    return round(round(price / GRID_STEP) * GRID_STEP, 4)

def active_shares(state):
    return state.up_shares if state.active_side == "up" else state.dn_shares

def active_cost(state):
    return state.up_cost if state.active_side == "up" else state.dn_cost

def active_token(state):
    return state.up_token if state.active_side == "up" else state.down_token

def set_active(state, shares_delta, cost_delta):
    if state.active_side == "up":
        state.up_shares += shares_delta
        state.up_cost   += cost_delta
    else:
        state.dn_shares += shares_delta
        state.dn_cost   += cost_delta

def clear_active(state):
    if state.active_side == "up":
        state.up_shares = state.up_cost = 0.0
    else:
        state.dn_shares = state.dn_cost = 0.0

async def sell_active(state, session, reason=""):
    """Sell all shares on active side at market bid. Returns net P&L."""
    shares = active_shares(state)
    cost   = active_cost(state)
    if shares <= 0:
        return 0.0
    bid      = await get_best_bid(session, active_token(state))
    proceeds = shares * bid
    net      = proceeds - cost
    state.capital += net
    clear_active(state)
    pnl = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
    label = side_s(state.active_side, f"{state.active_side.upper()} {shares:.0f} @ {bid:.4f}")
    print(f"   {'🔄' if reason=='switch' else '💰'} SELL ALL {label} | cost ${cost:.2f} | net {pnl}{' — ' + reason if reason else ''}")
    return net

async def settle_expiry(state, session):
    """Settle all open positions at window expiry."""
    settled = False
    for side in ("up", "down"):
        shares = state.up_shares if side == "up" else state.dn_shares
        cost   = state.up_cost   if side == "up" else state.dn_cost
        if shares > 0:
            token  = state.up_token if side == "up" else state.down_token
            up_ask = await get_best_ask(session, state.up_token)
            dn_ask = await get_best_ask(session, state.down_token)
            winner = "up" if up_ask >= dn_ask else "down"
            payout = shares * (1.0 if side == winner else 0.0)
            net    = payout - cost
            state.capital += net
            if side == "up":
                state.up_shares = state.up_cost = 0.0
            else:
                state.dn_shares = state.dn_cost = 0.0
            pnl = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
            label = side_s(side, side.upper())
            print(f"⏰ EXPIRY — {label} {shares:.0f} shares | {'WIN' if net>0 else 'LOSS'} {pnl} | Capital {cap(state.capital)}")
            settled = True
    if not settled:
        print(f"⏰ EXPIRY — no open positions | Capital {cap(state.capital)}")
    state.phase = "done"
    save_state(state)

# ─────────────────────────────────────────────────────────────────────────────

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Grid Bot | Capital {cap(state.capital)} | Phase: {state.phase}")
    print(f"   Trigger {TRIGGER} | Switch {SWITCH} | Grid ±{GRID_STEP} × {GRID_SHARES} shares")

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
                        state.up_token      = up_tok
                        state.down_token    = dn_tok
                        state.trade_window  = current_window
                        state.active_side   = None
                        state.last_buy_price = None
                        state.up_shares = state.up_cost = 0.0
                        state.dn_shares = state.dn_cost = 0.0
                        state.phase = "watching"
                        save_state(state)
                        print(f"🟢 WINDOW LIVE {slug} | {up_s('UP')} & {dn_s('DN')} | trigger @ {TRIGGER}")
                    elif state.poll_count % PRINT_EVERY == 0:
                        print(f"⏳ waiting for market data | T+{secs_elapsed}s | Capital {cap(state.capital)}")
                elif state.poll_count % PRINT_EVERY == 0:
                    print(f"⏳ waiting | T+{secs_elapsed}s elapsed | Capital {cap(state.capital)}")

            # ── PHASE: watching ───────────────────────────────────────────
            elif state.phase == "watching":
                # window expired with no grid triggered
                if state.trade_window and now >= state.trade_window + 300:
                    await settle_expiry(state, session)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                up_ask = await get_best_ask(session, state.up_token)
                dn_ask = await get_best_ask(session, state.down_token)
                rc = state.capital  # no open positions in watching

                if state.poll_count % PRINT_EVERY == 0:
                    print(f"👀 watching | {up_s(f'UP {up_ask:.4f}')}  {dn_s(f'DN {dn_ask:.4f}')} "
                          f"| trigger @ {TRIGGER} | Capital {cap(rc)}")

                triggered = None
                trigger_ask = 0.0
                if up_ask >= TRIGGER:
                    triggered, trigger_ask = "up", up_ask
                elif dn_ask >= TRIGGER:
                    triggered, trigger_ask = "down", dn_ask

                if triggered:
                    state.active_side    = triggered
                    state.last_buy_price = grid_level(trigger_ask)
                    cost = GRID_SHARES * trigger_ask
                    state.capital -= cost
                    set_active(state, GRID_SHARES, cost)
                    state.phase = "grid"
                    save_state(state)
                    label = side_s(triggered, f"{triggered.upper()} {trigger_ask:.4f}")
                    total = active_shares(state)
                    print(f"🎯 TRIGGER {label} | bought {GRID_SHARES} @ {trigger_ask:.4f} "
                          f"| ref level {state.last_buy_price:.2f} | total {total:.0f} shares | Capital {cap(state.capital)}")

            # ── PHASE: grid ───────────────────────────────────────────────
            elif state.phase == "grid":
                # window expired
                if state.trade_window and now >= state.trade_window + 300:
                    await settle_expiry(state, session)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                side   = state.active_side
                ask    = await get_best_ask(session, active_token(state))
                shares = active_shares(state)
                cost   = active_cost(state)
                # real-time capital: capital already has cost deducted at buy time
                rc = state.capital + shares * ask

                if state.poll_count % PRINT_EVERY == 0:
                    label = side_s(side, f"{side.upper()} {ask:.4f}")
                    print(f"📊 grid {label} | {shares:.0f} shares | "
                          f"ref {state.last_buy_price:.2f} | "
                          f"buy < {state.last_buy_price - GRID_STEP:.2f} | "
                          f"sell > {state.last_buy_price + GRID_STEP:.2f} | "
                          f"Real-time Capital {cap(rc)}")

                # ── SWITCH: active side dropped to 0.50 ───────────────────
                if ask <= SWITCH:
                    print(f"🔄 SWITCH — {side_s(side, side.upper())} dropped to {ask:.4f}")
                    await sell_active(state, session, reason="switch")
                    opp = "down" if side == "up" else "up"
                    state.active_side    = None
                    state.last_buy_price = None
                    state.phase          = "watching"
                    save_state(state)
                    opp_label = up_s(opp.upper()) if opp == "up" else dn_s(opp.upper())
                    print(f"   Watching {opp_label} & both sides for trigger @ {TRIGGER} | Capital {cap(state.capital)}")

                # ── SELL ALL: price rose by GRID_STEP from last buy ────────
                elif ask >= state.last_buy_price + GRID_STEP and shares > 0:
                    label = side_s(side, f"{side.upper()} {ask:.4f}")
                    print(f"💰 SELL ALL — {label} rose to {ask:.4f} (ref {state.last_buy_price:.2f} +{GRID_STEP})")
                    await sell_active(state, session)
                    state.last_buy_price = grid_level(ask)  # reset reference to current level
                    save_state(state)
                    print(f"   New ref level {state.last_buy_price:.2f} | Capital {cap(state.capital)}")

                # ── BUY: price dropped by GRID_STEP from last buy ──────────
                elif ask <= state.last_buy_price - GRID_STEP:
                    cost = GRID_SHARES * ask
                    state.capital -= cost
                    set_active(state, GRID_SHARES, cost)
                    state.last_buy_price -= GRID_STEP   # step grid level down
                    state.last_buy_price  = round(state.last_buy_price, 4)
                    total = active_shares(state)
                    label = side_s(side, f"{side.upper()} {ask:.4f}")
                    print(f"🛒 GRID BUY {label} +{GRID_SHARES} | total {total:.0f} shares "
                          f"| new ref {state.last_buy_price:.2f} | Capital {cap(state.capital)}")
                    save_state(state)

            # ── PHASE: done ───────────────────────────────────────────────
            elif state.phase == "done":
                print(f"✔️  Round complete | Capital {cap(state.capital)}")
                state.up_token    = state.down_token  = None
                state.trade_window = None
                state.active_side  = None
                state.last_buy_price = None
                state.up_shares = state.up_cost = 0.0
                state.dn_shares = state.dn_cost = 0.0
                state.poll_count = 0
                state.phase = "waiting"
                save_state(state)

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
