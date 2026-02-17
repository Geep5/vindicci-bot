#!/usr/bin/env python3
"""Vindicci prediction bot — continuous 5-minute BTC predictions via Hyperliquid data + Claude."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = os.environ.get("VINDICCI_SERVER", "https://vindicci-board.fly.dev")
API_KEY = os.environ.get("VINDICCI_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("VINDICCI_MODEL", "claude-sonnet-4-20250514")
HL = "https://api.hyperliquid.xyz/info"

LOOP_SECONDS = 310  # 5 min + 10s buffer


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def hl_post(payload):
    body = json.dumps(payload).encode()
    req = Request(HL, data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_mid_price():
    mids = hl_post({"type": "allMids"})
    return float(mids["BTC"])


def get_candles(interval, count):
    now_ms = int(time.time() * 1000)
    durations = {"1m": 60_000, "15m": 900_000, "1h": 3_600_000}
    start = now_ms - (count + 1) * durations[interval]
    candles = hl_post({
        "type": "candleSnapshot",
        "req": {"coin": "BTC", "interval": interval, "startTime": start, "endTime": now_ms},
    })
    return candles[-count:] if len(candles) > count else candles


def get_orderbook():
    return hl_post({"type": "l2Book", "coin": "BTC"})


def get_recent_trades():
    return hl_post({"type": "recentTrades", "coin": "BTC"})


def fmt_candles(candles, label):
    lines = [f"=== {label} ==="]
    lines.append(f"{'Time(UTC)':<10} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Vol(BTC)':>10} {'Trades':>7}")
    for c in candles:
        t = datetime.fromtimestamp(c["t"] / 1000, timezone.utc).strftime("%H:%M")
        lines.append(
            f"{t:<10} {float(c['o']):>10,.1f} {float(c['h']):>10,.1f} "
            f"{float(c['l']):>10,.1f} {float(c['c']):>10,.1f} {float(c['v']):>10.2f} {c['n']:>7}"
        )
    return "\n".join(lines)


def fmt_orderbook(book):
    bids = book["levels"][0][:5]
    asks = book["levels"][1][:5]
    lines = ["=== ORDERBOOK (top 5 levels) ==="]
    lines.append(f"{'BIDS':>30}    {'ASKS':<30}")
    for i in range(min(5, len(bids), len(asks))):
        b, a = bids[i], asks[i]
        lines.append(
            f"${float(b['px']):>10,.1f}  {float(b['sz']):>6.3f} BTC ({b['n']:>2})    "
            f"${float(a['px']):>10,.1f}  {float(a['sz']):>6.3f} BTC ({a['n']:>2})"
        )
    bid_total = sum(float(b["sz"]) for b in bids)
    ask_total = sum(float(a["sz"]) for a in asks) or 0.001
    spread = float(asks[0]["px"]) - float(bids[0]["px"])
    mid = (float(asks[0]["px"]) + float(bids[0]["px"])) / 2
    ratio = bid_total / ask_total
    lines.append(f"Bid depth (5 lvl): {bid_total:.3f} BTC    Ask depth (5 lvl): {ask_total:.3f} BTC")
    lines.append(f"Spread: ${spread:.1f} ({spread / mid * 100:.4f}%)")
    bias = "bid-heavy" if ratio > 1.2 else "ask-heavy" if ratio < 0.8 else "balanced"
    lines.append(f"Bid/Ask ratio: {ratio:.2f} ({bias})")
    return "\n".join(lines)


def fmt_trades(trades):
    large = [t for t in trades if float(t["sz"]) > 0.1]
    lines = ["=== RECENT LARGE TRADES (> 0.1 BTC) ==="]
    if not large:
        lines.append("No large trades in recent window. Low activity.")
    else:
        for t in large[-8:]:
            ts = datetime.fromtimestamp(t["time"] / 1000, timezone.utc).strftime("%H:%M:%S")
            side = "BUY" if t["side"] == "B" else "SELL"
            sz, px = float(t["sz"]), float(t["px"])
            lines.append(f"{ts}  {side:<4}  {sz:.2f} BTC @ ${px:,.1f}  (${sz * px:,.0f})")
    buy_vol = sum(float(t["sz"]) * float(t["px"]) for t in large if t["side"] == "B")
    sell_vol = sum(float(t["sz"]) * float(t["px"]) for t in large if t["side"] == "A")
    net = buy_vol - sell_vol
    lines.append(f"Net large-trade flow: {'+'if net>=0 else ''}{net:,.0f}")
    return "\n".join(lines)


def build_prompt():
    mid = get_mid_price()
    c1m = get_candles("1m", 5)
    c15m = get_candles("15m", 5)
    c1h = get_candles("1h", 5)
    book = get_orderbook()
    trades = get_recent_trades()

    parts = [
        f"BTC/USD 5-Minute Prediction — Market Data (Hyperliquid)\n\nCurrent mid price: ${mid:,.2f}\n",
        fmt_candles(c1m, "1-MINUTE CANDLES (last 5)"),
        fmt_candles(c15m, "15-MINUTE CANDLES (last 5)"),
        fmt_candles(c1h, "1-HOUR CANDLES (last 5)"),
        fmt_orderbook(book),
        fmt_trades(trades),
        f"---\nWill BTC be above or below ${mid:,.2f} in exactly 5 minutes?\n"
        "Analyze the candles (all three timeframes), volume trends, orderbook imbalance, "
        "and recent trade flow. Then state your direction: ABOVE or BELOW.",
    ]
    return "\n\n".join(parts)


def generate_report(prompt):
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": (
                    "You are a quantitative BTC trader. Analyze this market data and predict "
                    "whether BTC will be ABOVE or BELOW the current price in 5 minutes.\n\n"
                    "Cover: 1-min momentum, 15-min/1-hour context, volume, orderbook imbalance, "
                    "trade flow. End with a clear 'Direction: ABOVE' or 'Direction: BELOW'.\n\n"
                    f"{prompt}"
                ),
            }
        ],
    }).encode()
    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    return resp["content"][0]["text"]


def extract_direction(report):
    text = report.lower()
    # Find the last occurrence of "direction:" pattern
    idx = text.rfind("direction:")
    if idx >= 0:
        after = text[idx + 10 :].strip()[:20]
        if "above" in after:
            return "above"
        if "below" in after:
            return "below"
    # Fallback: count occurrences
    above_count = text.count("above")
    below_count = text.count("below")
    if above_count > below_count:
        return "above"
    if below_count > above_count:
        return "below"
    return "above"  # default


def submit(direction, prompt, report):
    body = json.dumps({
        "direction": direction,
        "prompt": prompt,
        "report": report,
        "model": MODEL,
    }).encode()
    req = Request(
        f"{BASE}/api/predictions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def run_once():
    log("Fetching market data...")
    prompt = build_prompt()
    log(f"Prompt built ({len(prompt)} chars). Calling Claude...")
    report = generate_report(prompt)
    direction = extract_direction(report)
    log(f"Analysis complete. Direction: {direction.upper()}")
    status, resp = submit(direction, prompt, report)
    if status == 201:
        log(f"Prediction #{resp['id']} submitted: {direction.upper()} from ${resp['entry_price']:,.2f}")
        log(f"Window closes: {resp['window_end']}")
        return True
    elif status == 409:
        log("Open prediction still pending — will retry next cycle.")
        return True
    else:
        log(f"Error {status}: {resp}")
        return False


def main():
    if not API_KEY:
        print("Set VINDICCI_API_KEY env var", file=sys.stderr)
        sys.exit(1)
    if not ANTHROPIC_KEY:
        print("Set ANTHROPIC_API_KEY env var", file=sys.stderr)
        sys.exit(1)

    log(f"Vindicci prediction bot starting")
    log(f"Server: {BASE}")
    log(f"Model: {MODEL}")
    log(f"Loop interval: {LOOP_SECONDS}s")
    log("---")

    while True:
        try:
            run_once()
        except Exception as e:
            log(f"Error: {e}")
        log(f"Sleeping {LOOP_SECONDS}s until next prediction...")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
