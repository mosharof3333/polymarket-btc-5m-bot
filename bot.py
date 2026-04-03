import asyncio
import aiohttp
import json
import time
import os

STATE_FILE      = "bot_state.json"
TRIGGER         = 0.80    # when one side hits this, buy the OPPOSITE (cheap) side
SECOND_TRIGGER  = 0.90    # if strong side goes even higher, double down on cheap side
FIRST_BET       = 20.0    # $ for first contrarian entry
SECOND_BET      = 20.0   # $ for double-down if strong side keeps rising
TP              = 0.99    # take profit on cheap side (full reversal)
POLL_INTERVAL   = 0.15
CLOB_BASE       = "https://clob.polymarket.com"
PRINT_EVERY     = 20

GREEN      = "\033[32m"
RED        = "\033[31m"
BOLD_GREEN = "\033[1;32m"
YELLOW     = "\033[33m"
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
        # phase: waiting / watching / contrarian / done
        self.phase            = "waiting"

        # which side is STRONG (hit trigger), we bet the OPPOSITE
        self.strong_side      = None    # "up" or "down"
        self.cheap_side       = None    # "up" or "down" — the one we're buying

        # our position on the cheap side
        self.shares           = 0.0
        self.cost             = 0.0

        # second trigger
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
        s.strong_side      = d.get("strong_side")
        s.cheap_side       = d.get("cheap_side")
        s.shares           = d.get("shares", 0.0)
        s.cost             = d.get("cost", 0.0)
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
            "strong_side":      s.strong_side,
            "cheap_side":       s.cheap_side,
            "shares":           round(s.shares, 6),
            "cost":             round(s.cost, 4),
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

def cheap_token(state):
    return state.up_token if state.cheap_side == "up" else state.down_token

def strong_token(state):
    return state.up_token if state.strong_side == "up" else state.down_token

# ── trade helpers ─────────────────────────────────────────────────────────────

async def buy_cheap(state, session, bet, cheap_ask, label=""):
    shares = bet / cheap_ask
    state.capital -= bet
    state.shares  += shares
    state.cost    += bet
    avg = state.cost / state.shares
    print(f"🛒 BUY {side_s(state.cheap_side, f'{state.cheap_side.upper()} @ {cheap_ask:.4f}')} "
          f"{label}| ${bet:.2f} → {shares:.4f} shares "
          f"| total {state.shares:.4f} @ avg {avg:.4f} "
          f"| TP @ {TP} | Capital {cap(state.capital)}")
    save_state(state)

async def sell_cheap(state, session, reason="TP"):
    if state.shares <= 0:
        return
    bid      = await get_best_bid(session, cheap_token(state))
    proceeds = state.shares * bid
    net      = proceeds - state.cost
    state.capital += proceeds   # cost already deducted at buy time
    pnl = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
    icon = "🎯" if reason == "TP" else "⏰"
    print(f"{icon} {reason} — sell "
          f"{side_s(state.cheap_side, f'{state.cheap_side.upper()} {state.shares:.4f} @ {bid:.4f}')} "
          f"| proceeds ${proceeds:.2f} | cost ${state.cost:.2f} | net {pnl} | Capital {cap(state.capital)}")
    state.shares = state.cost = 0.0
    state.phase  = "done"
    save_state(state)

# ─────────────────────────────────────────────────────────────────────────────

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Contrarian Bot | Capital {cap(state.capital)} | Phase: {state.phase}")
    print(f"   Strategy: when one side hits {TRIGGER}, BUY THE OPPOSITE")
    print(f"   First bet ${FIRST_BET} | Double-down ${SECOND_BET} at {SECOND_TRIGGER} | TP @ {TP}")

    async with aiohttp.ClientSession() as session:
        while True:
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
                        state.strong_side     = None
                        state.cheap_side      = None
                        state.shares          = 0.0
                        state.cost            = 0.0
                        state.second_triggered = False
                        state.phase           = "watching"
                        save_state(state)
                        print(f"🟢 WINDOW LIVE {slug} | watching for {up_s('UP')} or {dn_s('DN')} @ {TRIGGER} to fade | Capital {cap(state.capital)}")
                elif state.poll_count % PRINT_EVERY == 0:
                    print(f"⏳ waiting | T+{secs_elapsed}s | Capital {cap(state.capital)}")

            # ── PHASE: watching ───────────────────────────────────────────
            elif state.phase == "watching":
                if state.trade_window and now >= state.trade_window + 300:
                    print(f"⏰ EXPIRY — no trigger fired | Capital {cap(state.capital)}")
                    state.phase           = "done"
                    state.completed_window = state.trade_window
                    save_state(state)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                up_ask = await get_best_ask(session, state.up_token)
                dn_ask = await get_best_ask(session, state.down_token)

                if state.poll_count % PRINT_EVERY == 0:
                    print(f"👀 watching | {up_s(f'UP {up_ask:.4f}')}  {dn_s(f'DN {dn_ask:.4f}')} "
                          f"| fade trigger @ {TRIGGER} | Capital {cap(state.capital)}")

                # UP is strong → buy DOWN (cheap)
                if up_ask >= TRIGGER:
                    state.strong_side = "up"
                    state.cheap_side  = "down"
                    cheap_ask = dn_ask
                    print(f"📉 FADE — {up_s(f'UP hit {up_ask:.4f}')} → buying cheap {dn_s(f'DN @ {cheap_ask:.4f}')}")
                    await buy_cheap(state, session, FIRST_BET, cheap_ask)
                    state.phase = "contrarian"
                    save_state(state)
                    print(f"   If {up_s('UP')} hits {SECOND_TRIGGER}, double-down ${SECOND_BET} on {dn_s('DN')}")

                # DOWN is strong → buy UP (cheap)
                elif dn_ask >= TRIGGER:
                    state.strong_side = "down"
                    state.cheap_side  = "up"
                    cheap_ask = up_ask
                    print(f"📉 FADE — {dn_s(f'DN hit {dn_ask:.4f}')} → buying cheap {up_s(f'UP @ {cheap_ask:.4f}')}")
                    await buy_cheap(state, session, FIRST_BET, cheap_ask)
                    state.phase = "contrarian"
                    save_state(state)
                    print(f"   If {dn_s('DN')} hits {SECOND_TRIGGER}, double-down ${SECOND_BET} on {up_s('UP')}")

            # ── PHASE: contrarian ─────────────────────────────────────────
            elif state.phase == "contrarian":
                expired = state.trade_window and now >= state.trade_window + 300

                up_ask     = await get_best_ask(session, state.up_token)
                dn_ask     = await get_best_ask(session, state.down_token)
                cheap_ask  = up_ask if state.cheap_side  == "up" else dn_ask
                strong_ask = dn_ask if state.strong_side == "down" else up_ask

                # real-time capital: cost already deducted, add current position value
                rc = state.capital + state.shares * cheap_ask

                if state.poll_count % PRINT_EVERY == 0 and not expired:
                    avg      = state.cost / state.shares if state.shares > 0 else 0
                    unreal   = state.shares * cheap_ask - state.cost
                    u_str    = f"+${unreal:.2f}" if unreal >= 0 else f"-${abs(unreal):.2f}"
                    print(f"📊 contrarian | "
                          f"{side_s(state.cheap_side,  f'{state.cheap_side.upper()}  {cheap_ask:.4f}')} "
                          f"[{state.shares:.4f} shares, avg {avg:.4f}, {u_str}] | "
                          f"{side_s(state.strong_side, f'{state.strong_side.upper()} {strong_ask:.4f}')} "
                          f"{'(2nd trigger fired)' if state.second_triggered else f'(2nd trigger @ {SECOND_TRIGGER})'} | "
                          f"Real-time Capital {cap(rc)}")

                # ── TAKE PROFIT ───────────────────────────────────────────
                if cheap_ask >= TP:
                    print(f"🎯 TP — cheap side {side_s(state.cheap_side, state.cheap_side.upper())} reversed to {cheap_ask:.4f}!")
                    await sell_cheap(state, session, reason="TP")

                # ── EXPIRY ────────────────────────────────────────────────
                elif expired:
                    print(f"⏰ EXPIRY — settling position")
                    await sell_cheap(state, session, reason="EXPIRY")

                # ── DOUBLE DOWN: strong side goes even higher ──────────────
                elif not state.second_triggered and strong_ask >= SECOND_TRIGGER:
                    print(f"📉 DOUBLE DOWN — {side_s(state.strong_side, f'{state.strong_side.upper()} hit {strong_ask:.4f}')} "
                          f"| cheap side {side_s(state.cheap_side, state.cheap_side.upper())} now even cheaper @ {cheap_ask:.4f}")
                    await buy_cheap(state, session, SECOND_BET, cheap_ask,
                                    label=f"(double-down ${SECOND_BET}) ")
                    state.second_triggered = True
                    save_state(state)

            # ── PHASE: done ───────────────────────────────────────────────
            elif state.phase == "done":
                print(f"✔️  Round complete | Capital {cap(state.capital)}")
                state.completed_window = state.trade_window
                state.up_token         = state.down_token = None
                state.trade_window     = None
                state.strong_side      = state.cheap_side = None
                state.shares           = state.cost       = 0.0
                state.second_triggered = False
                state.poll_count       = 0
                state.phase            = "waiting"
                save_state(state)

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
