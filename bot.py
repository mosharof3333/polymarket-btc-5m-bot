import asyncio
import aiohttp
import json
import time
import os

STATE_FILE           = "bot_state.json"
TRIGGER_CHEAP        = 0.20   # buy whichever side hits this first
SECOND_TRIGGER_CHEAP = 0.10   # if cheap side drops here, also buy the strong side
FIRST_BET            = 10.0   # $ on cheap side at 0.20
SECOND_BET           = 150.0  # $ on strong side at ~0.90
TP                   = 0.99   # take profit for both positions
POLL_INTERVAL        = 0.15
CLOB_BASE            = "https://clob.polymarket.com"
PRINT_EVERY          = 20

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
        self.capital          = 1000.0
        self.up_token         = None
        self.down_token       = None
        self.trade_window     = None
        self.phase            = "waiting"  # waiting / watching / first_active / both_active / done

        # first position: the cheap side that hit 0.20
        self.cheap_side       = None       # "up" or "down"
        self.cheap_shares     = 0.0
        self.cheap_cost       = 0.0
        self.cheap_done       = False

        # second position: the strong (opposite) side bought at ~0.90
        self.strong_side      = None       # "up" or "down"
        self.strong_shares    = 0.0
        self.strong_cost      = 0.0
        self.strong_done      = False
        self.second_triggered = False

        self.completed_window = None
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
        s.cheap_side       = d.get("cheap_side")
        s.cheap_shares     = d.get("cheap_shares", 0.0)
        s.cheap_cost       = d.get("cheap_cost", 0.0)
        s.cheap_done       = d.get("cheap_done", False)
        s.strong_side      = d.get("strong_side")
        s.strong_shares    = d.get("strong_shares", 0.0)
        s.strong_cost      = d.get("strong_cost", 0.0)
        s.strong_done      = d.get("strong_done", False)
        s.second_triggered = d.get("second_triggered", False)
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
            "cheap_side":       s.cheap_side,
            "cheap_shares":     round(s.cheap_shares, 6),
            "cheap_cost":       round(s.cheap_cost, 4),
            "cheap_done":       s.cheap_done,
            "strong_side":      s.strong_side,
            "strong_shares":    round(s.strong_shares, 6),
            "strong_cost":      round(s.strong_cost, 4),
            "strong_done":      s.strong_done,
            "second_triggered": s.second_triggered,
            "completed_window": s.completed_window,
        }, f, indent=2)

# ── API helpers ───────────────────────────────────────────────────────────────

async def fetch_gamma(session, slug):
    try:
        async with session.get(
            f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=3
        ) as r:
            if r.status == 200:
                data = await r.json()
                if isinstance(data, list) and len(data) > 0:
                    return data
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

def token_for(state, side):
    return state.up_token if side == "up" else state.down_token

# ── trade helpers ─────────────────────────────────────────────────────────────

def settle_side_at_dollar(state, side, winner):
    """Settle one side at $1/share (winner) or $0/share (loser). Cost already deducted."""
    if side == state.cheap_side:
        shares = state.cheap_shares
        cost   = state.cheap_cost
    else:
        shares = state.strong_shares
        cost   = state.strong_cost
    if shares <= 0:
        return
    payout = shares * (1.0 if side == winner else 0.0)
    net    = payout - cost
    state.capital += payout
    pnl  = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
    icon = "🏆" if side == winner else "💀"
    print(f"{icon} FINAL SETTLE — {side_s(side, f'{side.upper()} {shares:.4f} × $1.00' if side==winner else f'{side.upper()} {shares:.4f} × $0')} "
          f"| cost ${cost:.2f} | net {pnl} | Capital {cap(state.capital)}")
    if side == state.cheap_side:
        state.cheap_shares = state.cheap_cost = 0.0
        state.cheap_done   = True
    else:
        state.strong_shares = state.strong_cost = 0.0
        state.strong_done   = True

async def check_final_10s(state, session, up_ask, dn_ask):
    """
    In the last 10 seconds, if either side >= 0.80 declare it the winner
    and settle all open positions at $1 (winner) or $0 (loser).
    Returns True if settlement happened.
    """
    if up_ask >= 0.80:
        winner = "up"
    elif dn_ask >= 0.80:
        winner = "down"
    else:
        return False   # neither side qualifies yet

    winner_ask = up_ask if winner == "up" else dn_ask
    print(f"⏱️  LAST 10s — {side_s(winner, f'{winner.upper()} @ {winner_ask:.4f}')} >= 0.80 → settling all at $1/$0")
    if state.cheap_shares > 0 and not state.cheap_done:
        settle_side_at_dollar(state, state.cheap_side, winner)
    if state.second_triggered and state.strong_shares > 0 and not state.strong_done:
        settle_side_at_dollar(state, state.strong_side, winner)
    state.phase = "done"
    save_state(state)
    return True

async def buy_position(state, session, side, bet, ask, label=""):
    shares = bet / ask
    state.capital -= bet
    if side == state.cheap_side:
        state.cheap_shares += shares
        state.cheap_cost   += bet
    else:
        state.strong_shares += shares
        state.strong_cost   += bet
    print(f"🛒 BUY {side_s(side, f'{side.upper()} @ {ask:.4f}')} {label}"
          f"| ${bet:.2f} → {shares:.4f} shares | TP @ {TP} | Capital {cap(state.capital)}")
    save_state(state)

async def sell_position(state, session, side, reason="TP"):
    if side == state.cheap_side:
        shares = state.cheap_shares
        cost   = state.cheap_cost
    else:
        shares = state.strong_shares
        cost   = state.strong_cost
    if shares <= 0:
        return
    bid      = await get_best_bid(session, token_for(state, side))
    proceeds = shares * bid
    net      = proceeds - cost
    state.capital += proceeds   # cost already deducted at buy time
    pnl  = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
    icon = "🎯" if reason == "TP" else "⏰"
    print(f"{icon} {reason} — sell {side_s(side, f'{side.upper()} {shares:.4f} @ {bid:.4f}')} "
          f"| proceeds ${proceeds:.2f} | cost ${cost:.2f} | net {pnl} | Capital {cap(state.capital)}")
    if side == state.cheap_side:
        state.cheap_shares = state.cheap_cost = 0.0
        state.cheap_done   = True
    else:
        state.strong_shares = state.strong_cost = 0.0
        state.strong_done   = True
    save_state(state)

def all_done(state):
    cheap_closed  = state.cheap_done  or state.cheap_shares  == 0
    strong_closed = not state.second_triggered or state.strong_done or state.strong_shares == 0
    return cheap_closed and strong_closed

# ─────────────────────────────────────────────────────────────────────────────

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Bot | Capital {cap(state.capital)} | Phase: {state.phase}")
    print(f"   First: whichever side hits {TRIGGER_CHEAP} → buy ${FIRST_BET}")
    print(f"   Second: if that side drops to {SECOND_TRIGGER_CHEAP} → buy opposite ${SECOND_BET}")
    print(f"   TP @ {TP} for both | no stop loss")

    async with aiohttp.ClientSession() as session:
        while True:
          try:
            now            = int(time.time())
            current_window = (now // 300) * 300
            secs_elapsed   = now - current_window
            state.poll_count += 1

            # ── PHASE: waiting ────────────────────────────────────────────
            if state.phase == "waiting":
                if current_window == state.completed_window:
                    if state.poll_count % PRINT_EVERY == 0:
                        print(f"⏳ window done — next in {300 - secs_elapsed}s | Capital {cap(state.capital)}")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                slug       = f"btc-updown-5m-{current_window}"
                event_data = await fetch_gamma(session, slug)
                if event_data:
                    market = event_data[0].get("markets", [event_data[0]])[0]
                    up_tok, dn_tok = get_tokens(market)
                    if up_tok and dn_tok:
                        state.up_token        = up_tok
                        state.down_token      = dn_tok
                        state.trade_window    = current_window
                        state.cheap_side      = state.strong_side = None
                        state.cheap_shares    = state.cheap_cost  = 0.0
                        state.strong_shares   = state.strong_cost = 0.0
                        state.cheap_done      = state.strong_done = False
                        state.second_triggered = False
                        state.phase           = "watching"
                        save_state(state)
                        print(f"🟢 WINDOW LIVE {slug} | watching for first side to hit {TRIGGER_CHEAP} | Capital {cap(state.capital)}")
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

                if state.poll_count % PRINT_EVERY == 0:
                    print(f"👀 watching | {up_s(f'UP {up_ask:.4f}')}  {dn_s(f'DN {dn_ask:.4f}')} "
                          f"| buy trigger @ {TRIGGER_CHEAP} | Capital {cap(state.capital)}")

                if up_ask <= TRIGGER_CHEAP:
                    state.cheap_side  = "up"
                    state.strong_side = "down"
                    print(f"🎯 TRIGGER — {up_s(f'UP hit {up_ask:.4f}')} (cheap) | buying ${FIRST_BET}")
                    await buy_position(state, session, "up", FIRST_BET, up_ask)
                    state.phase = "first_active"
                    save_state(state)
                    print(f"   If {up_s('UP')} drops to {SECOND_TRIGGER_CHEAP}, will buy {dn_s('DN')} ${SECOND_BET} | TP @ {TP}")

                elif dn_ask <= TRIGGER_CHEAP:
                    state.cheap_side  = "down"
                    state.strong_side = "up"
                    print(f"🎯 TRIGGER — {dn_s(f'DN hit {dn_ask:.4f}')} (cheap) | buying ${FIRST_BET}")
                    await buy_position(state, session, "down", FIRST_BET, dn_ask)
                    state.phase = "first_active"
                    save_state(state)
                    print(f"   If {dn_s('DN')} drops to {SECOND_TRIGGER_CHEAP}, will buy {up_s('UP')} ${SECOND_BET} | TP @ {TP}")

            # ── PHASE: first_active ───────────────────────────────────────
            elif state.phase == "first_active":
                up_ask    = await get_best_ask(session, state.up_token)
                dn_ask    = await get_best_ask(session, state.down_token)

                # last 10 seconds: settle at $1/$0 if a side is >= 0.80
                if state.trade_window and now >= state.trade_window + 290:
                    if await check_final_10s(state, session, up_ask, dn_ask):
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                if state.trade_window and now >= state.trade_window + 300:
                    await sell_position(state, session, state.cheap_side, reason="EXPIRY")
                    state.phase = "done"
                    save_state(state)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                cheap_ask = up_ask if state.cheap_side == "up" else dn_ask
                rc        = state.capital + state.cheap_shares * cheap_ask

                if state.poll_count % PRINT_EVERY == 0:
                    unreal = state.cheap_shares * cheap_ask - state.cheap_cost
                    u_str  = f"+${unreal:.2f}" if unreal >= 0 else f"-${abs(unreal):.2f}"
                    print(f"📊 {side_s(state.cheap_side, f'{state.cheap_side.upper()} {cheap_ask:.4f}')} "
                          f"| {state.cheap_shares:.4f} shares | unrealized {u_str} "
                          f"| TP @ {TP} | 2nd trigger @ {SECOND_TRIGGER_CHEAP} | Real-time Capital {cap(rc)}")

                # TP on cheap side
                if cheap_ask >= TP:
                    print(f"🎯 TP — {side_s(state.cheap_side, state.cheap_side.upper())} hit {cheap_ask:.4f}!")
                    await sell_position(state, session, state.cheap_side, reason="TP")
                    state.phase = "done"
                    save_state(state)

                # Second trigger: cheap side dropped to 0.10 → buy strong side
                elif cheap_ask <= SECOND_TRIGGER_CHEAP and not state.second_triggered:
                    strong_ask = dn_ask if state.strong_side == "down" else up_ask
                    print(f"📉 2ND TRIGGER — {side_s(state.cheap_side, f'{state.cheap_side.upper()} dropped to {cheap_ask:.4f}')} "
                          f"| buying strong side {side_s(state.strong_side, f'{state.strong_side.upper()} @ {strong_ask:.4f}')} ${SECOND_BET}")
                    await buy_position(state, session, state.strong_side, SECOND_BET, strong_ask)
                    state.second_triggered = True
                    state.phase = "both_active"
                    save_state(state)

            # ── PHASE: both_active ────────────────────────────────────────
            elif state.phase == "both_active":
                up_ask     = await get_best_ask(session, state.up_token)
                dn_ask     = await get_best_ask(session, state.down_token)

                # last 10 seconds: settle at $1/$0 if a side is >= 0.80
                if state.trade_window and now >= state.trade_window + 290:
                    if await check_final_10s(state, session, up_ask, dn_ask):
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                expired = state.trade_window and now >= state.trade_window + 300
                cheap_ask  = up_ask if state.cheap_side  == "up" else dn_ask
                strong_ask = up_ask if state.strong_side == "up" else dn_ask
                rc = (state.capital
                      + state.cheap_shares  * cheap_ask
                      + state.strong_shares * strong_ask)

                if state.poll_count % PRINT_EVERY == 0 and not expired:
                    cu = state.cheap_shares  * cheap_ask  - state.cheap_cost
                    su = state.strong_shares * strong_ask - state.strong_cost
                    print(f"📊 both open | "
                          f"{side_s(state.cheap_side,  f'{state.cheap_side.upper()}  {cheap_ask:.4f}')} ({cu:+.2f})  "
                          f"{side_s(state.strong_side, f'{state.strong_side.upper()} {strong_ask:.4f}')} ({su:+.2f}) "
                          f"| TP @ {TP} | Real-time Capital {cap(rc)}")

                # cheap side TP or expiry
                if not state.cheap_done and state.cheap_shares > 0:
                    if cheap_ask >= TP:
                        print(f"🎯 TP — {side_s(state.cheap_side, state.cheap_side.upper())} hit {cheap_ask:.4f}!")
                        await sell_position(state, session, state.cheap_side, reason="TP")
                    elif expired:
                        await sell_position(state, session, state.cheap_side, reason="EXPIRY")

                # strong side TP or expiry
                if not state.strong_done and state.strong_shares > 0:
                    if strong_ask >= TP:
                        print(f"🎯 TP — {side_s(state.strong_side, state.strong_side.upper())} hit {strong_ask:.4f}!")
                        await sell_position(state, session, state.strong_side, reason="TP")
                    elif expired:
                        await sell_position(state, session, state.strong_side, reason="EXPIRY")

                if all_done(state):
                    state.phase = "done"
                    save_state(state)

            # ── PHASE: done ───────────────────────────────────────────────
            elif state.phase == "done":
                print(f"✔️  Round complete | Capital {cap(state.capital)}")
                state.completed_window = state.trade_window
                state.up_token         = state.down_token  = None
                state.trade_window     = None
                state.cheap_side       = state.strong_side = None
                state.cheap_shares     = state.cheap_cost  = 0.0
                state.strong_shares    = state.strong_cost = 0.0
                state.cheap_done       = state.strong_done = False
                state.second_triggered = False
                state.poll_count       = 0
                state.phase            = "waiting"
                save_state(state)

            await asyncio.sleep(POLL_INTERVAL)

          except Exception as e:
            print(f"⚠️  ERROR (phase={state.phase}): {e} — continuing")
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
