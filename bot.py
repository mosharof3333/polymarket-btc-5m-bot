import asyncio
import aiohttp
import json
import time
import os

STATE_FILE = "bot_state.json"
FIRST_BUY_SHARES = 20
BUY_INTERVAL = 30       # buy every 30 seconds
BUY_UNTIL = 180         # stop buying at 3:00 (180 seconds into window)
TAKE_PROFIT = 0.99      # sell all when either side hits this
POLL_INTERVAL = 0.15
CLOB_BASE = "https://clob.polymarket.com"
PRINT_PRICE_EVERY = 20

class BotState:
    def __init__(self):
        self.capital = 1000.0
        self.last_window_ts = None
        self.up_shares = 0.0
        self.down_shares = 0.0
        self.up_cost = 0.0
        self.down_cost = 0.0
        self.buy_step = 0
        self.next_buy_time = None
        self.took_profit = False
        self.poll_count = 0
        self.prev_winner = None  # "up" or "down" from last settled window

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            state = BotState()
            state.capital = data.get("capital", 1000.0)
            state.last_window_ts = data.get("last_window_ts")
            state.up_shares = data.get("up_shares", 0.0)
            state.down_shares = data.get("down_shares", 0.0)
            state.up_cost = data.get("up_cost", 0.0)
            state.down_cost = data.get("down_cost", 0.0)
            state.buy_step = data.get("buy_step", 0)
            state.next_buy_time = data.get("next_buy_time")
            state.took_profit = data.get("took_profit", False)
            state.prev_winner = data.get("prev_winner")
            return state
    return BotState()

def save_state(state):
    data = {
        "capital": round(state.capital, 4),
        "last_window_ts": state.last_window_ts,
        "up_shares": state.up_shares,
        "down_shares": state.down_shares,
        "up_cost": state.up_cost,
        "down_cost": state.down_cost,
        "buy_step": state.buy_step,
        "next_buy_time": state.next_buy_time,
        "took_profit": state.took_profit,
        "prev_winner": state.prev_winner,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)

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

async def settle_pnl(state, payout, label):
    total_cost = state.up_cost + state.down_cost
    net_pnl = payout - total_cost
    old_capital = state.capital
    state.capital += payout
    result = "WIN" if net_pnl > 0 else "LOSS"
    pnl_str = f"+${net_pnl:.2f}" if net_pnl >= 0 else f"-${abs(net_pnl):.2f}"
    print(f"{label}")
    print(f"   UP shares: {state.up_shares:.0f} (cost ${state.up_cost:.2f}) | DOWN shares: {state.down_shares:.0f} (cost ${state.down_cost:.2f})")
    print(f"   Payout: ${payout:.2f} | Total cost: ${total_cost:.2f} | {result} {pnl_str}")
    print(f"   Capital: ${old_capital:.2f} → ${state.capital:.2f}")

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Accumulation Bot started | Capital ${state.capital:.2f}")

    async with aiohttp.ClientSession() as session:
        while True:
            now_ts = (int(time.time()) // 300) * 300
            slug = f"btc-updown-5m-{now_ts}"

            # New window
            if state.last_window_ts != now_ts:
                if state.last_window_ts is not None and (state.up_shares > 0 or state.down_shares > 0):
                    # Settle previous window
                    prev_slug = f"btc-updown-5m-{state.last_window_ts}"
                    prev_data = await fetch_gamma(session, prev_slug)
                    settled = False
                    if prev_data:
                        try:
                            prev_market = prev_data[0].get("markets", [prev_data[0]])[0]
                            prices = json.loads(prev_market.get("outcomePrices", "[0.5,0.5]"))
                            up_final = float(prices[0])
                            down_final = float(prices[1])
                            if up_final >= 0.80:
                                payout = state.up_shares * 1.0
                                await settle_pnl(state, payout, f"📊 PREV WINDOW SETTLED (UP wins)")
                                state.prev_winner = "up"
                                settled = True
                            elif down_final >= 0.80:
                                payout = state.down_shares * 1.0
                                await settle_pnl(state, payout, f"📊 PREV WINDOW SETTLED (DOWN wins)")
                                state.prev_winner = "down"
                                settled = True
                        except:
                            pass
                    if not settled:
                        print(f"⚠️  Could not settle prev window — positions dropped, capital unchanged")

                state.last_window_ts = now_ts
                state.up_shares = state.down_shares = 0.0
                state.up_cost = state.down_cost = 0.0
                state.buy_step = 0
                state.next_buy_time = None
                state.took_profit = False
                state.poll_count = 0
                save_state(state)
                print(f"🌟 NEW WINDOW: {slug} | Capital ${state.capital:.2f}")

            event_data = await fetch_gamma(session, slug)
            if not event_data:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            market = event_data[0].get("markets", [event_data[0]])[0]
            clob_str = market.get("clobTokenIds", "[]")
            if isinstance(clob_str, str):
                clob_ids = json.loads(clob_str)
            else:
                clob_ids = clob_str
            up_token = clob_ids[0] if clob_ids else None
            down_token = clob_ids[1] if len(clob_ids) > 1 else None

            up_ask = await get_best_ask(session, up_token)
            down_ask = await get_best_ask(session, down_token)

            state.poll_count += 1
            if state.poll_count % PRINT_PRICE_EVERY == 0:
                elapsed = int(time.time()) - state.last_window_ts
                print(f"LIVE T+{elapsed}s | Up {up_ask:.4f} | Down {down_ask:.4f} | UP {state.up_shares:.0f} shares | DOWN {state.down_shares:.0f} shares | Capital ${state.capital:.2f}")

            # Take profit when either side hits 0.99
            if not state.took_profit and (state.up_shares > 0 or state.down_shares > 0):
                if up_ask >= TAKE_PROFIT or down_ask >= TAKE_PROFIT:
                    up_bid = await get_best_bid(session, up_token)
                    down_bid = await get_best_bid(session, down_token)
                    payout = (state.up_shares * min(up_bid, 0.99)) + (state.down_shares * min(down_bid, 0.99))
                    await settle_pnl(state, payout, f"🎯 TAKE PROFIT — {'UP' if up_ask >= TAKE_PROFIT else 'DOWN'} hit {TAKE_PROFIT}")
                    state.up_shares = state.down_shares = 0.0
                    state.up_cost = state.down_cost = 0.0
                    state.took_profit = True
                    save_state(state)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

            # Buy every 30s until 3:00 — follow prev winner if known, else buy cheaper side
            elapsed = int(time.time()) - state.last_window_ts
            now = int(time.time())
            if not state.took_profit and elapsed <= BUY_UNTIL:
                if state.next_buy_time is None or now >= state.next_buy_time:
                    shares = FIRST_BUY_SHARES
                    if state.prev_winner == "up":
                        side = "up"
                        price = up_ask
                    elif state.prev_winner == "down":
                        side = "down"
                        price = down_ask
                    elif up_ask <= down_ask:
                        side = "up"
                        price = up_ask
                    else:
                        side = "down"
                        price = down_ask
                    cost = shares * price
                    if state.capital >= cost:
                        if side == "up":
                            state.up_shares += shares
                            state.up_cost += cost
                        else:
                            state.down_shares += shares
                            state.down_cost += cost
                        state.capital -= cost
                        state.buy_step += 1
                        state.next_buy_time = now + BUY_INTERVAL
                        save_state(state)
                        reason = f"prev winner" if state.prev_winner else "cheaper side"
                        print(f"🛒 BUY {side.upper()} {shares} @ {price:.4f} | Cost ${cost:.2f} | Capital ${state.capital:.2f} | {reason} | Next in 30s")
                    else:
                        print(f"⚠️  Insufficient capital for {shares} shares @ ${price:.4f} (need ${cost:.2f})")

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
