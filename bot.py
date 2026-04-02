import asyncio
import aiohttp
import json
import time
import os

STATE_FILE    = "bot_state.json"
BUY_SHARES    = 100
PRE_BUY_SECS  = 60    # buy this many seconds before next window
TP1           = 0.70  # initial take profit — first side to hit this gets sold
TP2           = 0.99  # secondary take profit — remaining side after TP1 hit
POLL_INTERVAL = 0.15
CLOB_BASE     = "https://clob.polymarket.com"
PRINT_EVERY   = 20

class BotState:
    def __init__(self):
        self.capital        = 1000.0
        # ── current window being traded ──
        self.up_shares      = 0.0
        self.down_shares    = 0.0
        self.up_cost        = 0.0
        self.down_cost      = 0.0
        self.up_token       = None
        self.down_token     = None
        self.trade_window   = None
        self.phase          = "waiting"   # waiting/pre_bought/tp_initial/tp_secondary/done
        self.first_tp_side  = None
        # ── next window pre-bought (populated while current window is active) ──
        self.next_window      = None
        self.next_up_shares   = 0.0
        self.next_down_shares = 0.0
        self.next_up_cost     = 0.0
        self.next_down_cost   = 0.0
        self.next_up_token    = None
        self.next_down_token  = None
        self.poll_count       = 0

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            s = BotState()
            s.capital           = data.get("capital", 1000.0)
            s.up_shares         = data.get("up_shares", 0.0)
            s.down_shares       = data.get("down_shares", 0.0)
            s.up_cost           = data.get("up_cost", 0.0)
            s.down_cost         = data.get("down_cost", 0.0)
            s.up_token          = data.get("up_token")
            s.down_token        = data.get("down_token")
            s.trade_window      = data.get("trade_window")
            s.phase             = data.get("phase", "waiting")
            s.first_tp_side     = data.get("first_tp_side")
            s.next_window       = data.get("next_window")
            s.next_up_shares    = data.get("next_up_shares", 0.0)
            s.next_down_shares  = data.get("next_down_shares", 0.0)
            s.next_up_cost      = data.get("next_up_cost", 0.0)
            s.next_down_cost    = data.get("next_down_cost", 0.0)
            s.next_up_token     = data.get("next_up_token")
            s.next_down_token   = data.get("next_down_token")
            return s
    return BotState()

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump({
            "capital":           round(s.capital, 4),
            "up_shares":         s.up_shares,
            "down_shares":       s.down_shares,
            "up_cost":           s.up_cost,
            "down_cost":         s.down_cost,
            "up_token":          s.up_token,
            "down_token":        s.down_token,
            "trade_window":      s.trade_window,
            "phase":             s.phase,
            "first_tp_side":     s.first_tp_side,
            "next_window":       s.next_window,
            "next_up_shares":    s.next_up_shares,
            "next_down_shares":  s.next_down_shares,
            "next_up_cost":      s.next_up_cost,
            "next_down_cost":    s.next_down_cost,
            "next_up_token":     s.next_up_token,
            "next_down_token":   s.next_down_token,
        }, f, indent=2)

async def fetch_gamma(session, slug):
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        async with session.get(url, timeout=3) as resp:
            if resp.status == 200:
                return await resp.json()
    except:
        pass
    return None

async def get_best_ask(session, token_id):
    if not token_id:
        return 0.5
    url = f"{CLOB_BASE}/price?token_id={token_id}&side=SELL"
    try:
        async with session.get(url, timeout=2) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data.get("price", 0.5))
    except:
        pass
    return 0.5

async def get_best_bid(session, token_id):
    if not token_id:
        return 0.01
    url = f"{CLOB_BASE}/price?token_id={token_id}&side=BUY"
    try:
        async with session.get(url, timeout=2) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data.get("price", 0.01))
    except:
        pass
    return 0.01

def get_tokens(market):
    clob_str = market.get("clobTokenIds", "[]")
    clob_ids = json.loads(clob_str) if isinstance(clob_str, str) else clob_str
    return (clob_ids[0] if clob_ids else None,
            clob_ids[1] if len(clob_ids) > 1 else None)

async def try_pre_buy_next(state, session, next_window, secs_to_next):
    """Pre-buy both sides of the upcoming window if not already done."""
    if state.next_window == next_window:
        return  # already pre-bought this window
    upcoming_slug = f"btc-updown-5m-{next_window}"
    event_data = await fetch_gamma(session, upcoming_slug)
    if not event_data:
        return
    market = event_data[0].get("markets", [event_data[0]])[0]
    up_token, down_token = get_tokens(market)
    if not up_token or not down_token:
        return
    up_ask   = await get_best_ask(session, up_token)
    down_ask = await get_best_ask(session, down_token)
    cost_up   = BUY_SHARES * up_ask
    cost_down = BUY_SHARES * down_ask
    state.capital          -= (cost_up + cost_down)
    state.next_window       = next_window
    state.next_up_shares    = BUY_SHARES
    state.next_down_shares  = BUY_SHARES
    state.next_up_cost      = cost_up
    state.next_down_cost    = cost_down
    state.next_up_token     = up_token
    state.next_down_token   = down_token
    save_state(state)
    print(f"🛒 PRE-BUY NEXT WINDOW | T-{secs_to_next}s to {upcoming_slug}")
    print(f"   UP  100 @ {up_ask:.4f}  cost ${cost_up:.2f}")
    print(f"   DN  100 @ {down_ask:.4f}  cost ${cost_down:.2f}")
    print(f"   Total ${cost_up+cost_down:.2f} | Capital ${state.capital:.2f}")

def activate_next_window(state):
    """Promote next window pre-buy into the active trading slot."""
    state.up_shares     = state.next_up_shares
    state.down_shares   = state.next_down_shares
    state.up_cost       = state.next_up_cost
    state.down_cost     = state.next_down_cost
    state.up_token      = state.next_up_token
    state.down_token    = state.next_down_token
    state.trade_window  = state.next_window
    state.phase         = "pre_bought"
    state.first_tp_side = None
    state.next_window       = None
    state.next_up_shares    = state.next_down_shares   = 0.0
    state.next_up_cost      = state.next_down_cost     = 0.0
    state.next_up_token     = state.next_down_token    = None

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Straddle Bot | Capital ${state.capital:.2f} | Phase: {state.phase}")

    async with aiohttp.ClientSession() as session:
        while True:
            now            = int(time.time())
            current_window = (now // 300) * 300
            next_window    = current_window + 300
            secs_to_next   = next_window - now
            state.poll_count += 1

            # ── PRE-BUY CHECK (runs every loop regardless of phase) ─────────
            if secs_to_next <= PRE_BUY_SECS and state.next_window != next_window:
                await try_pre_buy_next(state, session, next_window, secs_to_next)

            # ── PHASE: waiting ──────────────────────────────────────────────
            if state.phase == "waiting":
                if state.next_window and now >= state.next_window:
                    activate_next_window(state)
                    save_state(state)
                    print(f"🟢 WINDOW LIVE — watching UP & DOWN for TP1 @ {TP1}")
                elif state.poll_count % PRINT_EVERY == 0:
                    print(f"⏳ waiting | T-{secs_to_next}s to next window | Capital ${state.capital:.2f}")

            # ── PHASE: pre_bought ───────────────────────────────────────────
            elif state.phase == "pre_bought":
                if now >= state.trade_window:
                    state.phase = "tp_initial"
                    save_state(state)
                    print(f"🟢 WINDOW LIVE — watching UP & DOWN for TP1 @ {TP1}")
                elif state.poll_count % PRINT_EVERY == 0:
                    print(f"⏳ pre_bought | T-{state.trade_window - now}s to live | Capital ${state.capital:.2f}")

            # ── PHASE: tp_initial ───────────────────────────────────────────
            elif state.phase == "tp_initial":
                up_ask   = await get_best_ask(session, state.up_token)
                down_ask = await get_best_ask(session, state.down_token)
                if state.poll_count % PRINT_EVERY == 0:
                    print(f"👀 TP1 | UP {up_ask:.4f}  DN {down_ask:.4f} | Capital ${state.capital:.2f}")

                if up_ask >= TP1:
                    up_bid   = await get_best_bid(session, state.up_token)
                    proceeds = state.up_shares * min(up_bid, TP1)
                    state.capital      += proceeds
                    state.up_shares     = 0.0
                    state.first_tp_side = "up"
                    state.phase         = "tp_secondary"
                    save_state(state)
                    print(f"✅ TP1 HIT — UP {up_ask:.4f} | sold 100 @ {min(up_bid,TP1):.4f} | +${proceeds:.2f} | Capital ${state.capital:.2f}")
                    print(f"   DOWN still open — TP2 @ {TP2}")

                elif down_ask >= TP1:
                    down_bid  = await get_best_bid(session, state.down_token)
                    proceeds  = state.down_shares * min(down_bid, TP1)
                    state.capital       += proceeds
                    state.down_shares    = 0.0
                    state.first_tp_side  = "down"
                    state.phase          = "tp_secondary"
                    save_state(state)
                    print(f"✅ TP1 HIT — DOWN {down_ask:.4f} | sold 100 @ {min(down_bid,TP1):.4f} | +${proceeds:.2f} | Capital ${state.capital:.2f}")
                    print(f"   UP still open — TP2 @ {TP2}")

                elif now >= state.trade_window + 300:
                    await settle_remaining(state, session)

            # ── PHASE: tp_secondary ─────────────────────────────────────────
            elif state.phase == "tp_secondary":
                remaining = "down" if state.first_tp_side == "up" else "up"
                token     = state.down_token if remaining == "down" else state.up_token
                shares    = state.down_shares if remaining == "down" else state.up_shares
                ask       = await get_best_ask(session, token)
                if state.poll_count % PRINT_EVERY == 0:
                    print(f"👀 TP2 | {remaining.upper()} {ask:.4f} (target {TP2}) | Capital ${state.capital:.2f}")

                if ask >= TP2:
                    bid      = await get_best_bid(session, token)
                    proceeds = shares * min(bid, TP2)
                    state.capital += proceeds
                    if remaining == "down":
                        state.down_shares = 0.0
                    else:
                        state.up_shares = 0.0
                    state.phase = "done"
                    save_state(state)
                    total_cost = state.up_cost + state.down_cost
                    print(f"🎯 TP2 HIT — {remaining.upper()} {ask:.4f} | sold {shares:.0f} @ {min(bid,TP2):.4f} | +${proceeds:.2f}")
                    print(f"   Total cost ${total_cost:.2f} | Capital ${state.capital:.2f}")

                elif now >= state.trade_window + 300:
                    await settle_remaining(state, session)

            # ── PHASE: done ─────────────────────────────────────────────────
            elif state.phase == "done":
                print(f"✔️  Round complete | Capital ${state.capital:.2f}")
                state.up_shares = state.down_shares = 0.0
                state.up_cost   = state.down_cost   = 0.0
                state.up_token  = state.down_token  = None
                state.trade_window  = None
                state.first_tp_side = None
                state.poll_count    = 0
                # if next window already pre-bought, activate it; else wait
                if state.next_window and now >= state.next_window:
                    activate_next_window(state)
                    print(f"🟢 NEXT WINDOW LIVE — watching UP & DOWN for TP1 @ {TP1}")
                elif state.next_window:
                    state.phase = "pre_bought"
                    print(f"⏳ next window pre-bought, T-{state.next_window - now}s to live")
                else:
                    state.phase = "waiting"
                save_state(state)

            await asyncio.sleep(POLL_INTERVAL)

async def settle_remaining(state, session):
    up_price   = await get_best_ask(session, state.up_token)
    down_price = await get_best_ask(session, state.down_token)
    if up_price >= down_price:
        payout = state.up_shares * 1.0
        winner = "UP"
    else:
        payout = state.down_shares * 1.0
        winner = "DOWN"
    total_cost = state.up_cost + state.down_cost
    net_pnl    = payout - total_cost
    state.capital += payout
    pnl_str = f"+${net_pnl:.2f}" if net_pnl >= 0 else f"-${abs(net_pnl):.2f}"
    print(f"⏰ WINDOW EXPIRED — {winner} wins | payout ${payout:.2f} | cost ${total_cost:.2f} | {'WIN' if net_pnl>0 else 'LOSS'} {pnl_str} | Capital ${state.capital:.2f}")
    state.phase = "done"
    save_state(state)

if __name__ == "__main__":
    asyncio.run(main())
