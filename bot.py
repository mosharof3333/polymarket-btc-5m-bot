import asyncio
import aiohttp
import json
import time
import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL

STATE_FILE = "bot_state.json"

TRIGGER_CHEAP = 0.30
SECOND_TRIGGER_STRONG = 0.90
SL_STRONG = 0.40
FIRST_BET = 2.0
SECOND_BET = 20.0
TP = 0.99
POLL_INTERVAL = 0.05
FINAL_10S_THRESHOLD = 0.55
CLOB_BASE = "https://clob.polymarket.com"
PRINT_EVERY = 20

GREEN = "\033[32m"
RED = "\033[31m"
BOLD_GREEN = "\033[1;32m"
RESET = "\033[0m"

def cap(v): return f"{BOLD_GREEN}${v:.2f}{RESET}"
def up_s(s): return f"{GREEN}{s}{RESET}"
def dn_s(s): return f"{RED}{s}{RESET}"
def side_s(side, s): return up_s(s) if side == "up" else dn_s(s)

class BotState:
    def __init__(self):
        self.capital = 1000.0
        self.up_token = None
        self.down_token = None
        self.trade_window = None
        self.phase = "waiting"
        self.cheap_side = None
        self.cheap_shares = 0.0
        self.cheap_cost = 0.0
        self.cheap_done = False
        self.strong_side = None
        self.strong_shares = 0.0
        self.strong_cost = 0.0
        self.strong_done = False
        self.second_triggered = False
        self.completed_window = None
        self.poll_count = 0
        self.stat_first_win = 0
        self.stat_first_loss = 0
        self.stat_second_triggered = 0
        self.stat_second_win = 0
        self.stat_second_loss = 0
        self.cheap_exited_tp = False
        self.strong_exited_tp = False

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
            s = BotState()
            for k, v in d.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            return s
        except Exception:
            pass
    return BotState()

def save_state(s):
    try:
        data = {k: round(getattr(s, k), 6) if isinstance(getattr(s, k), float) else getattr(s, k) for k in vars(s)}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

# API helpers
async def fetch_gamma(session, slug):
    try:
        async with session.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=5) as r:
            if r.status == 200:
                data = await r.json()
                if isinstance(data, list) and data:
                    return data
    except Exception as e:
        print(f"Gamma error: {e}")
    return None

async def get_best_ask(session, token_id):
    if not token_id: return 0.5
    try:
        async with session.get(f"{CLOB_BASE}/price?token_id={token_id}&side=SELL", timeout=3) as r:
            if r.status == 200:
                return float((await r.json()).get("price", 0.5))
    except Exception:
        pass
    return 0.5

async def get_best_bid(session, token_id):
    if not token_id: return 0.01
    try:
        async with session.get(f"{CLOB_BASE}/price?token_id={token_id}&side=BUY", timeout=3) as r:
            if r.status == 200:
                return float((await r.json()).get("price", 0.01))
    except Exception:
        pass
    return 0.01

def get_tokens(market):
    ids = market.get("clobTokenIds", "[]")
    ids = json.loads(ids) if isinstance(ids, str) else ids
    return (ids[0] if ids else None, ids[1] if len(ids) > 1 else None)

def token_for(state, side):
    return state.up_token if side == "up" else state.down_token

# Client
CLIENT = None

async def init_client():
    global CLIENT
    if CLIENT is not None: return True
    private_key = os.getenv("PRIVATE_KEY")
    funder = os.getenv("FUNDER") or os.getenv("FUNDER_ADDRESS")
    if not private_key or not funder:
        print("❌ Missing PRIVATE_KEY or FUNDER!")
        return False
    try:
        CLIENT = ClobClient(host="https://clob.polymarket.com", key=private_key, chain_id=137, signature_type=2, funder=funder)
        CLIENT.set_api_creds(CLIENT.create_or_derive_api_creds())
        print("✅ Real client ready")
        return True
    except Exception as e:
        print(f"❌ Client init failed: {e}")
        return False

async def get_real_usdc_balance():
    if not CLIENT: return None
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = CLIENT.get_balance_allowance(params)
        return int(result.get("balance", 0)) / 1_000_000
    except Exception:
        return None

async def real_buy(state, side, bet, ask):
    token = token_for(state, side)
    if not token or not CLIENT: return False
    try:
        mo = MarketOrderArgs(token_id=token, amount=bet, side=BUY, order_type=OrderType.FOK)
        signed = CLIENT.create_market_order(mo)
        resp = CLIENT.post_order(signed, OrderType.FOK)
        print(f"🛒 REAL BUY SENT → {resp}")
        shares = bet / ask
        state.capital -= bet
        if side == state.cheap_side:
            state.cheap_shares += shares
            state.cheap_cost += bet
        else:
            state.strong_shares += shares
            state.strong_cost += bet
        save_state(state)
        return True
    except Exception as e:
        print(f"❌ BUY FAILED: {e}")
        return False

async def real_sell(state, side, reason="TP"):
    if side == state.cheap_side:
        shares = state.cheap_shares
        cost = state.cheap_cost
        token = state.up_token if state.cheap_side == "up" else state.down_token
    else:
        shares = state.strong_shares
        cost = state.strong_cost
        token = state.up_token if state.strong_side == "up" else state.down_token
    if shares <= 0 or not token or not CLIENT:
        return
    try:
        bid = await get_best_bid(None, token)
        mo = MarketOrderArgs(token_id=token, amount=shares, side=SELL, order_type=OrderType.FOK)
        signed = CLIENT.create_market_order(mo)
        resp = CLIENT.post_order(signed, OrderType.FOK)
        print(f"📤 REAL SELL SENT → {resp}")
        proceeds = shares * bid
        net = proceeds - cost
        state.capital += proceeds
        pnl = f"+\( {net:.2f}" if net >= 0 else f"- \){abs(net):.2f}"
        icon = "🎯" if reason == "TP" else "⏰"
        print(f"{icon} {reason} — REAL SELL {side_s(side, f'{side.upper()} {shares:.4f} @ {bid:.4f}')} | net {pnl} | Capital {cap(state.capital)}")
        if side == state.cheap_side:
            state.cheap_shares = state.cheap_cost = 0.0
            state.cheap_done = True
        else:
            state.strong_shares = state.strong_cost = 0.0
            state.strong_done = True
        save_state(state)
    except Exception as e:
        print(f"❌ SELL FAILED: {e}")

# Trade helpers
def settle_side_at_dollar(state, side, winner):
    if side == state.cheap_side:
        shares = state.cheap_shares
        cost = state.cheap_cost
    else:
        shares = state.strong_shares
        cost = state.strong_cost
    if shares <= 0: return
    payout = shares * (1.0 if side == winner else 0.0)
    net = payout - cost
    state.capital += payout
    pnl = f"+\( {net:.2f}" if net >= 0 else f"- \){abs(net):.2f}"
    icon = "🏆" if side == winner else "💀"
    print(f"{icon} FINAL SETTLE — {side_s(side, f'{side.upper()} {shares:.4f} × $1.00' if side==winner else f'{side.upper()} {shares:.4f} × $0')} | cost ${cost:.2f} | net {pnl} | Capital {cap(state.capital)}")
    if side == state.cheap_side:
        state.cheap_shares = state.cheap_cost = 0.0
        state.cheap_done = True
    else:
        state.strong_shares = state.strong_cost = 0.0
        state.strong_done = True

def determine_winner(up_ask, dn_ask, threshold=None):
    if threshold:
        if up_ask >= threshold: return "up"
        if dn_ask >= threshold: return "down"
        return None
    return "up" if up_ask >= dn_ask else "down"

async def check_final_10s(state, session, up_ask, dn_ask):
    now_expired = state.trade_window and int(time.time()) >= state.trade_window + 300
    winner = determine_winner(up_ask, dn_ask) if now_expired else determine_winner(up_ask, dn_ask, FINAL_10S_THRESHOLD)
    if winner is None: return False
    winner_ask = up_ask if winner == "up" else dn_ask
    label = "EXPIRY" if now_expired else f"LAST 10s ({winner_ask:.4f} >= {FINAL_10S_THRESHOLD})"
    print(f"⏱️  {label} — {side_s(winner, winner.upper())} wins")
    if state.cheap_shares > 0 and not state.cheap_done:
        settle_side_at_dollar(state, state.cheap_side, winner)
        if state.cheap_side == winner: state.cheap_exited_tp = True
    if state.second_triggered and state.strong_shares > 0 and not state.strong_done:
        settle_side_at_dollar(state, state.strong_side, winner)
        if state.strong_side == winner: state.strong_exited_tp = True
    state.phase = "done"
    save_state(state)
    return True

def print_stats(state):
    f_total = state.stat_first_win + state.stat_first_loss
    f_rate = f"{state.stat_first_win/f_total*100:.0f}%" if f_total else "n/a"
    s_total = state.stat_second_win + state.stat_second_loss
    s_rate = f"{state.stat_second_win/s_total*100:.0f}%" if s_total else "n/a"
    print(f"📈 STATS ─────────────────────────────────────────")
    print(f"   First buy (${FIRST_BET} @ {TRIGGER_CHEAP}): Win {state.stat_first_win}x | Loss {state.stat_first_loss}x | Rate {f_rate}")
    print(f"   Second buy (${SECOND_BET} @ {SECOND_TRIGGER_STRONG}): triggered {state.stat_second_triggered}x | Win {state.stat_second_win}x | Loss {state.stat_second_loss}x | Rate {s_rate}")
    print(f"──────────────────────────────────────────────────")

def all_done(state):
    cheap_closed = state.cheap_done or state.cheap_shares == 0
    strong_closed = not state.second_triggered or state.strong_done or state.strong_shares == 0
    return cheap_closed and strong_closed

# ── Main ────────────────────────────────────────────────────────────────────
async def main():
    await init_client()
    state = load_state()
    real_balance = await get_real_usdc_balance()
    if real_balance is not None:
        print(f"🚀 REAL BTC 5m Bot | Real USDC: {cap(real_balance)} | Phase: {state.phase}")
        if real_balance > 0:
            state.capital = real_balance
    else:
        print(f"🚀 REAL BTC 5m Bot | Virtual Capital {cap(state.capital)} | Phase: {state.phase}")

    print(f"   First trigger ≤ {TRIGGER_CHEAP} → ${FIRST_BET}")
    print(f"   Second trigger ≥ {SECOND_TRIGGER_STRONG} → ${SECOND_BET}")
    print(f"   TP @ {TP} | SL on second @ {SL_STRONG}")
    print_stats(state)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now = int(time.time())
                current_window = (now // 300) * 300
                secs_elapsed = now - current_window
                state.poll_count += 1

                if state.phase == "waiting":
                    if current_window == state.completed_window:
                        if state.poll_count % PRINT_EVERY == 0:
                            print(f"⏳ waiting for next window")
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    slug = f"btc-updown-5m-{current_window}"
                    event_data = await fetch_gamma(session, slug)
                    if event_data:
                        market = event_data[0].get("markets", [event_data[0]])[0]
                        up_tok, dn_tok = get_tokens(market)
                        if up_tok and dn_tok:
                            state.up_token = up_tok
                            state.down_token = dn_tok
                            state.trade_window = current_window
                            state.cheap_side = state.strong_side = None
                            state.cheap_shares = state.cheap_cost = 0.0
                            state.strong_shares = state.strong_cost = 0.0
                            state.cheap_done = state.strong_done = False
                            state.second_triggered = False
                            state.phase = "watching"
                            save_state(state)
                            print(f"🟢 WINDOW LIVE {slug}")

                elif state.phase == "watching":
                    if state.trade_window and now >= state.trade_window + 300:
                        state.phase = "done"
                        save_state(state)
                        continue

                    up_ask = await get_best_ask(session, state.up_token)
                    dn_ask = await get_best_ask(session, state.down_token)

                    if state.poll_count % PRINT_EVERY == 0:
                        print(f"👀 watching | {up_s(f'UP {up_ask:.4f}')}  {dn_s(f'DN {dn_ask:.4f}')} | trigger @ ≤ {TRIGGER_CHEAP}")

                    # FIRST BUY - only once
                    if state.cheap_side is None:   # ← This prevents double buy
                        if up_ask <= TRIGGER_CHEAP:
                            state.cheap_side = "up"
                            state.strong_side = "down"
                            print(f"🎯 FIRST TRIGGER — buying ${FIRST_BET} on UP @ {up_ask:.4f}")
                            await real_buy(state, "up", FIRST_BET, up_ask)
                            state.phase = "first_active"
                            save_state(state)

                        elif dn_ask <= TRIGGER_CHEAP:
                            state.cheap_side = "down"
                            state.strong_side = "up"
                            print(f"🎯 FIRST TRIGGER — buying ${FIRST_BET} on DN @ {dn_ask:.4f}")
                            await real_buy(state, "down", FIRST_BET, dn_ask)
                            state.phase = "first_active"
                            save_state(state)

                elif state.phase == "first_active":
                    up_ask = await get_best_ask(session, state.up_token)
                    dn_ask = await get_best_ask(session, state.down_token)

                    if state.trade_window and now >= state.trade_window + 290:
                        if await check_final_10s(state, session, up_ask, dn_ask):
                            await asyncio.sleep(POLL_INTERVAL)
                            continue
                    if state.trade_window and now >= state.trade_window + 300:
                        await check_final_10s(state, session, up_ask, dn_ask)
                        continue

                    cheap_ask = up_ask if state.cheap_side == "up" else dn_ask
                    strong_ask = up_ask if state.strong_side == "up" else dn_ask

                    if state.poll_count % PRINT_EVERY == 0:
                        unreal = state.cheap_shares * cheap_ask - state.cheap_cost
                        u_str = f"+\( {unreal:.2f}" if unreal >= 0 else f"- \){abs(unreal):.2f}"
                        print(f"📊 {side_s(state.cheap_side, state.cheap_side.upper())} {cheap_ask:.4f} | unreal {u_str} | 2nd if opposite ≥ {SECOND_TRIGGER_STRONG}")

                    if cheap_ask >= TP:
                        print(f"🎯 TP on cheap side")
                        await real_sell(state, state.cheap_side, "TP")
                        state.cheap_exited_tp = True
                        state.phase = "done"
                        save_state(state)

                    elif strong_ask >= SECOND_TRIGGER_STRONG and not state.second_triggered:
                        print(f"📉 SECOND TRIGGER — buying ${SECOND_BET} on {state.strong_side.upper()}")
                        await real_buy(state, state.strong_side, SECOND_BET, strong_ask)
                        state.second_triggered = True
                        state.stat_second_triggered += 1
                        state.phase = "both_active"
                        save_state(state)

                elif state.phase == "both_active":
                    up_ask = await get_best_ask(session, state.up_token)
                    dn_ask = await get_best_ask(session, state.down_token)

                    if state.trade_window and now >= state.trade_window + 290:
                        if await check_final_10s(state, session, up_ask, dn_ask):
                            await asyncio.sleep(POLL_INTERVAL)
                            continue

                    expired = state.trade_window and now >= state.trade_window + 300
                    cheap_ask = up_ask if state.cheap_side == "up" else dn_ask
                    strong_ask = up_ask if state.strong_side == "up" else dn_ask

                    if state.poll_count % PRINT_EVERY == 0 and not expired:
                        cu = state.cheap_shares * cheap_ask - state.cheap_cost
                        su = state.strong_shares * strong_ask - state.strong_cost
                        print(f"📊 both | {side_s(state.cheap_side, f'{state.cheap_side.upper()} {cheap_ask:.4f}')} ({cu:+.2f}) | {side_s(state.strong_side, f'{state.strong_side.upper()} {strong_ask:.4f}')} ({su:+.2f})")

                    if not state.cheap_done and cheap_ask >= TP:
                        await real_sell(state, state.cheap_side, "TP")
                        state.cheap_exited_tp = True

                    if not state.strong_done and strong_ask >= TP:
                        await real_sell(state, state.strong_side, "TP")
                        state.strong_exited_tp = True
                    elif not state.strong_done and strong_ask <= SL_STRONG:
                        await real_sell(state, state.strong_side, "SL")

                    if expired:
                        await check_final_10s(state, session, up_ask, dn_ask)

                    if all_done(state):
                        state.phase = "done"
                        save_state(state)

                elif state.phase == "done":
                    if state.cheap_side is not None:
                        if state.cheap_exited_tp:
                            state.stat_first_win += 1
                        else:
                            state.stat_first_loss += 1
                    if state.second_triggered:
                        if state.strong_exited_tp:
                            state.stat_second_win += 1
                        else:
                            state.stat_second_loss += 1

                    print(f"✔️ Round complete | Capital {cap(state.capital)}")
                    print_stats(state)

                    state.completed_window = state.trade_window
                    state.up_token = state.down_token = None
                    state.trade_window = None
                    state.cheap_side = state.strong_side = None
                    state.cheap_shares = state.cheap_cost = 0.0
                    state.strong_shares = state.strong_cost = 0.0
                    state.cheap_done = state.strong_done = False
                    state.cheap_exited_tp = state.strong_exited_tp = False
                    state.second_triggered = False
                    state.poll_count = 0
                    state.phase = "waiting"
                    save_state(state)

                await asyncio.sleep(POLL_INTERVAL)

            except Exception as e:
                print(f"⚠️ ERROR: {e}")
                await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
