import asyncio
import aiohttp
import json
import time
import os
from datetime import datetime

STATE_FILE = "bot_state.json"
BASE_SHARES = 10
POLL_INTERVAL = 0.2                # 200ms = millisecond reaction speed

class BotState:
    def __init__(self):
        self.capital = 1000.0
        self.last_window_ts = None
        self.current_side = None      # 'up' or 'down' or None
        self.up_shares = 0.0
        self.down_shares = 0.0
        self.up_cost = 0.0
        self.down_cost = 0.0
        self.last_buy_size = 0.0      # for doubling on flips

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

async def fetch_event(session, slug):
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status == 200:
                return await resp.json()
    except:
        pass
    return None

def get_prices_and_status(event_data):
    if not event_data or not isinstance(event_data, list) or len(event_data) == 0:
        return None, None, None, True
    event = event_data[0]
    market = event.get("markets", [event])[0]
    closed = market.get("closed", event.get("closed", False))
    
    # outcomePrices for live price (exactly as Polymarket shows)
    if "outcomePrices" in market:
        prices_str = json.loads(market["outcomePrices"])
        up_price = float(prices_str[0])
        down_price = float(prices_str[1])
    else:
        up_price = down_price = 0.5
    
    return up_price, down_price, closed

async def main():
    state = load_state()
    print(f"🚀 BTC 5m Doubling Bot started | Capital: ${state.capital:.2f} | Polling every 200ms")
    
    async with aiohttp.ClientSession() as session:
        while True:
            now_ts = (int(time.time()) // 300) * 300
            slug = f"btc-updown-5m-{now_ts}"
            
            event_data = await fetch_event(session, slug)
            if not event_data:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            
            up_price, down_price, closed = get_prices_and_status(event_data)
            
            # New window → reset positions but keep capital
            if now_ts != state.last_window_ts:
                if state.last_window_ts is not None:
                    print(f"✅ Window {state.last_window_ts} ended. Capital now: ${state.capital:.2f}")
                state.last_window_ts = now_ts
                state.current_side = None
                state.up_shares = state.down_shares = 0.0
                state.up_cost = state.down_cost = 0.0
                state.last_buy_size = 0.0
                save_state(state)
                print(f"🌟 NEW 5m WINDOW: {slug} | Up {up_price:.3f} / Down {down_price:.3f}")
            
            if closed:
                # Resolve P&L - one side must be \~1.0
                up_wins = up_price >= 0.999
                winning_shares = state.up_shares if up_wins else state.down_shares
                payout = winning_shares * 1.0
                state.capital += payout
                save_state(state)
                print(f"🏁 WINDOW RESOLVED → {'UP' if up_wins else 'DOWN'} wins | Payout ${payout:.2f} | New Capital ${state.capital:.2f}")
                await asyncio.sleep(2)
                continue
            
            # === YOUR DOUBLING STRATEGY ===
            if state.current_side is None:
                # First entry: 10 shares on whichever hits 60¢ first
                if up_price >= 0.60:
                    shares = BASE_SHARES
                    cost = shares * up_price
                    if state.capital >= cost:
                        state.up_shares += shares
                        state.up_cost += cost
                        state.capital -= cost
                        state.current_side = "up"
                        state.last_buy_size = shares
                        save_state(state)
                        print(f"📈 FIRST BUY → UP {shares} shares @ {up_price:.3f} | Cost ${cost:.2f} | Capital ${state.capital:.2f}")
                elif down_price >= 0.60:
                    shares = BASE_SHARES
                    cost = shares * down_price
                    if state.capital >= cost:
                        state.down_shares += shares
                        state.down_cost += cost
                        state.capital -= cost
                        state.current_side = "down"
                        state.last_buy_size = shares
                        save_state(state)
                        print(f"📉 FIRST BUY → DOWN {shares} shares @ {down_price:.3f} | Cost ${cost:.2f} | Capital ${state.capital:.2f}")
            
            elif state.current_side == "up":
                # Reversal: Down now ≥60¢ → double up on Down
                if down_price >= 0.60:
                    new_shares = int(state.last_buy_size * 2)
                    cost = new_shares * down_price
                    if state.capital >= cost:
                        state.down_shares += new_shares
                        state.down_cost += cost
                        state.capital -= cost
                        state.current_side = "down"
                        state.last_buy_size = new_shares
                        save_state(state)
                        print(f"🔄 FLIP → DOWN {new_shares} shares @ {down_price:.3f} | Cost ${cost:.2f} | Capital ${state.capital:.2f} | (next double = {new_shares*2})")
            
            elif state.current_side == "down":
                # Reversal: Up now ≥60¢ → double up on Up
                if up_price >= 0.60:
                    new_shares = int(state.last_buy_size * 2)
                    cost = new_shares * up_price
                    if state.capital >= cost:
                        state.up_shares += new_shares
                        state.up_cost += cost
                        state.capital -= cost
                        state.current_side = "up"
                        state.last_buy_size = new_shares
                        save_state(state)
                        print(f"🔄 FLIP → UP {new_shares} shares @ {up_price:.3f} | Cost ${cost:.2f} | Capital ${state.capital:.2f} | (next double = {new_shares*2})")
            
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
