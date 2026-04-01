import asyncio
import aiohttp
import json
import time
import os

STATE_FILE = "bot_state.json"
BASE_SHARES = 10
POLL_INTERVAL = 0.15
CLOB_BASE = "https://clob.polymarket.com"
PRINT_PRICE_EVERY = 8

class BotState:
    def __init__(self):
        self.capital = 1000.0
        self.last_window_ts = None
        self.current_side = None
        self.up_shares = 0.0
        self.down_shares = 0.0
        self.up_cost = 0.0
        self.down_cost = 0.0
        self.last_buy_size = 0.0
        self.poll_count = 0

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            state = BotState()
            state.capital = data.get("capital", 1000.0)
            state.last_window_ts = data.get("last_window_ts")
            state.current_side = data.get("current_side")
            state.up_shares = data.get("up_shares", 0.0)
            state.down_shares = data.get("down_shares", 0.0)
            state.up_cost = data.get("up_cost", 0.0)
            state.down_cost = data.get("down_cost", 0.0)
            state.last_buy_size = data.get("last_buy_size", 0.0)
            return state
    return BotState()

def save_state(state):
    data = {
        "capital": round(state.capital, 4),
        "last_window_ts": state.last_window_ts,
        "current_side": state.current_side,
        "up_shares": state.up_shares,
        "down_shares": state.down_shares,
        "up_cost": state.up_cost,
        "down_cost": state.down_cost,
        "last_buy_size": state.last_buy_size,
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

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Doubling Bot — Math & Payout finally fixed")

    async with aiohttp.ClientSession() as session:
        while True:
            now_ts = (int(time.time()) // 300) * 300
            slug = f"btc-updown-5m-{now_ts}"

            # New window reset
            if state.last_window_ts != now_ts:
                if state.last_window_ts is not None:
                    print(f"✅ New 5m window started — positions reset")
                state.last_window_ts = now_ts
                state.current_side = None
                state.up_shares = state.down_shares = 0.0
                state.up_cost = state.down_cost = 0.0
                state.last_buy_size = 0.0
                state.poll_count = 0
                save_state(state)
                print(f"🌟 NEW WINDOW: {slug}")

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

            # Immediate resolution when any side hits 0.99+
            resolved = False
            up_final = 0.5
            down_final = 0.5
            if "outcomePrices" in market:
                try:
                    prices = json.loads(market["outcomePrices"])
                    up_final = float(prices[0])
                    down_final = float(prices[1])
                    if up_final >= 0.99 or down_final >= 0.99:
                        resolved = True
                except:
                    pass

            if resolved:
                # Calculate payout BEFORE resetting shares
                if up_final >= 0.99:
                    winner = "UP"
                    payout = state.up_shares * 1.0
                else:
                    winner = "DOWN"
                    payout = state.down_shares * 1.0

                old_capital = state.capital
                state.capital += payout
                
                # Save the new capital FIRST
                save_state(state)

                result = "WIN" if payout > 0 else "LOSS"
                pnl_str = f"+\( {payout:.2f}" if payout > 0 else f"- \){(old_capital - state.capital):.2f}"

                print(f"🏁 WINDOW RESOLVED (side hit 0.99+)!")
                print(f"   UP shares: {state.up_shares:.0f} | DOWN shares: {state.down_shares:.0f}")
                print(f"   Outcome → UP: ${up_final:.3f} | DOWN: ${down_final:.3f}")
                print(f"   RESULT: **{result}** ({winner} wins) | P&L: {pnl_str}")
                print(f"   Capital: ${old_capital:.2f} → ${state.capital:.2f}")

                # Now safe to reset shares
                state.up_shares = state.down_shares = 0.0
                state.up_cost = state.down_cost = 0.0
                state.current_side = None
                state.last_buy_size = 0.0
                state.last_window_ts = None  # Force fresh next window
                save_state(state)

                await asyncio.sleep(3)
                continue

            # Live prices
            up_ask = await get_best_ask(session, up_token)
            down_ask = await get_best_ask(session, down_token)

            state.poll_count += 1

            if state.poll_count % PRINT_PRICE_EVERY == 0 or state.current_side is None:
                side_status = f" | Holding {state.current_side.upper()}" if state.current_side else ""
                print(f"LIVE: Up {up_ask:.4f} | Down {down_ask:.4f}{side_status} | Capital ${state.capital:.2f}")

            # Your strategy (10 → double on flips)
            if state.current_side is None:
                if up_ask >= 0.60:
                    shares = BASE_SHARES
                    cost = shares * up_ask
                    if state.capital >= cost:
                        state.up_shares += shares
                        state.up_cost += cost
                        state.capital -= cost
                        state.current_side = "up"
                        state.last_buy_size = shares
                        save_state(state)
                        print(f"📈 FIRST BUY UP {shares} @ {up_ask:.4f} | Cost ${cost:.2f} | Capital ${state.capital:.2f}")
                elif down_ask >= 0.60:
                    shares = BASE_SHARES
                    cost = shares * down_ask
                    if state.capital >= cost:
                        state.down_shares += shares
                        state.down_cost += cost
                        state.capital -= cost
                        state.current_side = "down"
                        state.last_buy_size = shares
                        save_state(state)
                        print(f"📉 FIRST BUY DOWN {shares} @ {down_ask:.4f} | Cost ${cost:.2f} | Capital ${state.capital:.2f}")

            elif state.current_side == "up" and down_ask >= 0.60:
                new_shares = int(state.last_buy_size * 2)
                cost = new_shares * down_ask
                if state.capital >= cost:
                    state.down_shares += new_shares
                    state.down_cost += cost
                    state.capital -= cost
                    state.current_side = "down"
                    state.last_buy_size = new_shares
                    save_state(state)
                    print(f"🔄 FLIP DOWN {new_shares} @ {down_ask:.4f} | Cost ${cost:.2f} | Capital ${state.capital:.2f} (next: {new_shares*2})")

            elif state.current_side == "down" and up_ask >= 0.60:
                new_shares = int(state.last_buy_size * 2)
                cost = new_shares * up_ask
                if state.capital >= cost:
                    state.up_shares += new_shares
                    state.up_cost += cost
                    state.capital -= cost
                    state.current_side = "up"
                    state.last_buy_size = new_shares
                    save_state(state)
                    print(f"🔄 FLIP UP {new_shares} @ {up_ask:.4f} | Cost ${cost:.2f} | Capital ${state.capital:.2f} (next: {new_shares*2})")

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
