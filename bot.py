import asyncio
import aiohttp
import json
import time
import os

STATE_FILE    = "bot_state.json"
TRIGGER       = 0.80    # price to trigger a buy on either side
FIRST_BET     = 50.0    # $ for first trigger
SECOND_BET    = 300.0   # $ for opposite trigger (if first reverses)
TP            = 0.99    # take profit for both sides
POLL_INTERVAL = 0.15
CLOB_BASE     = "https://clob.polymarket.com"
PRINT_EVERY   = 20

GREEN      = "\033[32m"
RED        = "\033[31m"
BOLD_GREEN = "\033[1;32m"
RESET      = "\033[0m"

def cap(v):          return f"{BOLD_GREEN}${v:.2f}{RESET}"
def up_s(s):         return f"{GREEN}{s}{RESET}"
def dn_s(s):         return f"{RED}{s}{RESET}"
def side_s(side, s): return up_s(s) if side == "up" else dn_s(s)

# ─────────────────────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.capital         = 1000.0
        self.up_token        = None
        self.down_token      = None
        self.trade_window    = None
        # phase: waiting / watching / first_active / both_active / done
        self.phase           = "waiting"

        # first trigger
        self.first_side      = None    # "up" or "down"
        self.up_shares       = 0.0
        self.up_cost         = 0.0
        self.up_done         = False   # TP hit on UP side

        # second trigger (opposite)
        self.second_triggered = False
        self.dn_shares        = 0.0
        self.dn_cost          = 0.0
        self.dn_done          = False  # TP hit on DN side

        self.completed_window = None   # last window we finished — never re-enter it
        self.poll_count       = 0

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            d = json.load(f)
        s = BotState()
        s.capital          = d.get("capital", 1000.0)
        s.up_token         = d.get("up_token")
        s.down_token       = d.get("down_token")
        s.trade_window     = d.get("trade_window")
        s.phase            = d.get("phase", "waiting")
        s.first_side       = d.get("first_side")
        s.up_shares        = d.get("up_shares", 0.0)
        s.up_cost          = d.get("up_cost", 0.0)
        s.up_done          = d.get("up_done", False)
        s.second_triggered = d.get("second_triggered", False)
        s.dn_shares        = d.get("dn_shares", 0.0)
        s.dn_cost          = d.get("dn_cost", 0.0)
        s.dn_done          = d.get("dn_done", False)
        s.completed_window = d.get("completed_window")
        return s
    return BotState()

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump({
            "capital":          round(s.capital, 4),
            "up_token":         s.up_token,
            "down_token":       s.down_token,
            "trade_window":     s.trade_window,
            "phase":            s.phase,
            "first_side":       s.first_side,
            "up_shares":        round(s.up_shares, 6),
            "up_cost":          round(s.up_cost, 4),
            "up_done":          s.up_done,
            "second_triggered": s.second_triggered,
            "dn_shares":        round(s.dn_shares, 6),
            "dn_cost":          round(s.dn_cost, 4),
            "dn_done":          s.dn_done,
            "completed_window": s.completed_window,
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

async def buy_side(state, session, side, bet, ask):
    """Buy $bet worth of shares on given side."""
    token  = state.up_token if side == "up" else state.down_token
    shares = bet / ask
    state.capital -= bet
    if side == "up":
        state.up_shares += shares
        state.up_cost   += bet
    else:
        state.dn_shares += shares
        state.dn_cost   += bet
    label = side_s(side, f"{side.upper()} @ {ask:.4f}")
    print(f"🛒 BUY {label} | ${bet:.2f} → {shares:.4f} shares | TP @ {TP} | Capital {cap(state.capital)}")
    save_state(state)

async def sell_side(state, session, side, reason="TP"):
    """Sell all shares on given side at market bid."""
    shares = state.up_shares if side == "up" else state.dn_shares
    cost   = state.up_cost   if side == "up" else state.dn_cost
    token  = state.up_token  if side == "up" else state.down_token
    if shares <= 0:
        return
    bid      = await get_best_bid(session, token)
    proceeds = shares * bid
    net      = proceeds - cost
    state.capital += net
    pnl = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
    label = side_s(side, f"{side.upper()} {shares:.4f} @ {bid:.4f}")
    icon  = "🎯" if reason == "TP" else "⏰"
    print(f"{icon} {reason} — sell {label} | cost ${cost:.2f} | net {pnl} | Capital {cap(state.capital)}")
    if side == "up":
        state.up_shares = state.up_cost = 0.0
        state.up_done = True
    else:
        state.dn_shares = state.dn_cost = 0.0
        state.dn_done = True
    save_state(state)

def rt_capital(state, up_ask, dn_ask):
    """Mark-to-market capital: cash after all trades + unrealized position value."""
    unrealized = state.up_shares * up_ask + state.dn_shares * dn_ask
    open_cost  = state.up_cost + state.dn_cost
    return state.capital - open_cost + unrealized

def both_closed(state):
    """True when all open positions are done."""
    up_closed = state.up_done or state.up_shares == 0
    dn_closed = state.dn_done or state.dn_shares == 0
    # At least one must have been triggered
    if not state.first_side:
        return False
    if not state.second_triggered:
        return up_closed if state.first_side == "up" else dn_closed
    return up_closed and dn_closed

# ─────────────────────────────────────────────────────────────────────────────

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Trigger Bot | Capital {cap(state.capital)} | Phase: {state.phase}")
    print(f"   Trigger @ {TRIGGER} | First bet ${FIRST_BET} | Opposite bet ${SECOND_BET} | TP @ {TP}")

    async with aiohttp.ClientSession() as session:
        while True:
            now            = int(time.time())
            current_window = (now // 300) * 300
            secs_elapsed   = now - current_window
            state.poll_count += 1

            # ── PHASE: waiting ────────────────────────────────────────────
            if state.phase == "waiting":
                # never re-enter a window that already completed this session
                if current_window == state.completed_window:
                    if state.poll_count % PRINT_EVERY == 0:
                        secs_to_next = 300 - secs_elapsed
                        print(f"⏳ window done — next in {secs_to_next}s | Capital {cap(state.capital)}")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                slug       = f"btc-updown-5m-{current_window}"
                event_data = await fetch_gamma(session, slug)
                if event_data:
                    market = event_data[0].get("markets", [event_data[0]])[0]
                    up_tok, dn_tok = get_tokens(market)
                    if up_tok and dn_tok:
                        state.up_token    = up_tok
                        state.down_token  = dn_tok
                        state.trade_window = current_window
                        state.first_side   = None
                        state.second_triggered = False
                        state.up_shares = state.up_cost = 0.0
                        state.dn_shares = state.dn_cost = 0.0
                        state.up_done   = state.dn_done = False
                        state.phase     = "watching"
                        save_state(state)
                        print(f"🟢 WINDOW LIVE {slug} | watching for {up_s('UP')} or {dn_s('DN')} @ {TRIGGER} | Capital {cap(state.capital)}")
                elif state.poll_count % PRINT_EVERY == 0:
                    print(f"⏳ waiting | T+{secs_elapsed}s | Capital {cap(state.capital)}")

            # ── PHASE: watching ───────────────────────────────────────────
            elif state.phase == "watching":
                if state.trade_window and now >= state.trade_window + 300:
                    print(f"⏰ EXPIRY — no trigger fired | Capital {cap(state.capital)}")
                    state.phase = "done"
                    save_state(state)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                up_ask = await get_best_ask(session, state.up_token)
                dn_ask = await get_best_ask(session, state.down_token)
                rc     = state.capital

                if state.poll_count % PRINT_EVERY == 0:
                    print(f"👀 watching | {up_s(f'UP {up_ask:.4f}')}  {dn_s(f'DN {dn_ask:.4f}')} "
                          f"| trigger @ {TRIGGER} | Capital {cap(rc)}")

                if up_ask >= TRIGGER:
                    state.first_side = "up"
                    await buy_side(state, session, "up", FIRST_BET, up_ask)
                    state.phase = "first_active"
                    print(f"   Watching for TP @ {TP} or {dn_s('DN')} reversal @ {TRIGGER} (${SECOND_BET})")

                elif dn_ask >= TRIGGER:
                    state.first_side = "down"
                    await buy_side(state, session, "down", FIRST_BET, dn_ask)
                    state.phase = "first_active"
                    print(f"   Watching for TP @ {TP} or {up_s('UP')} reversal @ {TRIGGER} (${SECOND_BET})")

            # ── PHASE: first_active ───────────────────────────────────────
            elif state.phase == "first_active":
                if state.trade_window and now >= state.trade_window + 300:
                    side = state.first_side
                    shares = state.up_shares if side == "up" else state.dn_shares
                    if shares > 0:
                        await sell_side(state, session, side, reason="EXPIRY")
                    state.phase = "done"
                    save_state(state)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                up_ask = await get_best_ask(session, state.up_token)
                dn_ask = await get_best_ask(session, state.down_token)
                rc = rt_capital(state, up_ask, dn_ask)

                first  = state.first_side
                opp    = "down" if first == "up" else "up"
                f_ask  = up_ask if first == "up" else dn_ask
                o_ask  = dn_ask if first == "up" else up_ask
                f_cost = state.up_cost if first == "up" else state.dn_cost
                f_shares = state.up_shares if first == "up" else state.dn_shares

                if state.poll_count % PRINT_EVERY == 0:
                    unreal = f_shares * f_ask - f_cost
                    u_str  = f"+${unreal:.2f}" if unreal >= 0 else f"-${abs(unreal):.2f}"
                    print(f"📊 {side_s(first, f'{first.upper()} {f_ask:.4f}')} | "
                          f"{f_shares:.4f} shares | unrealized {u_str} | "
                          f"{side_s(opp, f'{opp.upper()} {o_ask:.4f}')} (trigger @ {TRIGGER} → ${SECOND_BET}) | "
                          f"Real-time Capital {cap(rc)}")

                # ── TP on first side ──────────────────────────────────────
                if f_ask >= TP:
                    print(f"🎯 TP HIT — {side_s(first, first.upper())} {f_ask:.4f}")
                    await sell_side(state, session, first, reason="TP")
                    state.phase = "done"
                    save_state(state)

                # ── opposite side reversal trigger ────────────────────────
                elif o_ask >= TRIGGER:
                    print(f"🔁 REVERSAL — {side_s(opp, f'{opp.upper()} hit {o_ask:.4f}')} | "
                          f"first side {side_s(first, first.upper())} going against us")
                    await buy_side(state, session, opp, SECOND_BET, o_ask)
                    state.second_triggered = True
                    state.phase = "both_active"
                    save_state(state)
                    print(f"   Both sides open | {side_s(first, f'{first.upper()} TP @ {TP}')} | "
                          f"{side_s(opp, f'{opp.upper()} TP @ {TP}')}")

            # ── PHASE: both_active ────────────────────────────────────────
            elif state.phase == "both_active":
                expired = state.trade_window and now >= state.trade_window + 300

                up_ask = await get_best_ask(session, state.up_token)
                dn_ask = await get_best_ask(session, state.down_token)
                rc = rt_capital(state, up_ask, dn_ask)

                if state.poll_count % PRINT_EVERY == 0 and not expired:
                    up_u = state.up_shares * up_ask - state.up_cost
                    dn_u = state.dn_shares * dn_ask - state.dn_cost
                    print(f"📊 both open | "
                          f"{up_s(f'UP {up_ask:.4f}')} ({up_u:+.2f})  "
                          f"{dn_s(f'DN {dn_ask:.4f}')} ({dn_u:+.2f}) | "
                          f"Real-time Capital {cap(rc)}")

                # ── TP or expiry on UP ────────────────────────────────────
                if not state.up_done and state.up_shares > 0:
                    if expired or up_ask >= TP:
                        reason = "EXPIRY" if expired else "TP"
                        await sell_side(state, session, "up", reason=reason)

                # ── TP or expiry on DN ────────────────────────────────────
                if not state.dn_done and state.dn_shares > 0:
                    if expired or dn_ask >= TP:
                        reason = "EXPIRY" if expired else "TP"
                        await sell_side(state, session, "down", reason=reason)

                if both_closed(state):
                    state.phase = "done"
                    save_state(state)

            # ── PHASE: done ───────────────────────────────────────────────
            elif state.phase == "done":
                print(f"✔️  Round complete | Capital {cap(state.capital)}")
                state.completed_window = state.trade_window   # block re-entry
                state.up_token    = state.down_token  = None
                state.trade_window = None
                state.first_side   = None
                state.second_triggered = False
                state.up_shares = state.up_cost = 0.0
                state.dn_shares = state.dn_cost = 0.0
                state.up_done   = state.dn_done = False
                state.poll_count = 0
                state.phase = "waiting"
                save_state(state)

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
