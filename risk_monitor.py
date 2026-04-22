#!/usr/bin/env python3
"""
Risk Alerts: Monitors Binance leverage, combined equity, and cross-exchange delta.
Fires Slack alerts when thresholds are breached.
Designed for Jenkins: credentials via environment variables, failures notify Slack.
"""

import os
import sys
import time
import hmac
import hashlib
import traceback
import requests
from typing import Any, Dict, List, Optional

from binance.client import Client


BINANCE_API_KEY    = os.environ["BINANCE_API_KEY"]
BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
GATE_API_KEY       = os.environ["GATE_API_KEY"]
GATE_API_SECRET    = os.environ["GATE_API_SECRET"]
SLACK_WEBHOOK      = os.environ["SLACK_WEBHOOK"]

LEVERAGE_THRESHOLD = 2.1
EQUITY_THRESHOLD   = 1020000.0
DELTA_THRESHOLD    = 2000.0

GATE_HOST   = "https://api.gateio.ws"
GATE_PREFIX = "/api/v4"


def send_slack_alert(message: str):
    try:
        resp = requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"Slack alert failed: {e}", file=sys.stderr)


def send_slack_error(component: str, error: Exception):
    tb = traceback.format_exception(type(error), error, error.__traceback__)
    short_tb = "".join(tb[-3:])
    message = (
        f":x: *Soleil Risk Monitor Failure*\n"
        f"Component: `{component}`\n"
        f"Error: `{type(error).__name__}: {error}`\n"
        f"```{short_tb}```"
    )
    send_slack_alert(message)


def build_binance_client() -> Client:
    return Client(
        api_key=BINANCE_API_KEY,
        api_secret=BINANCE_API_SECRET,
        tld="com",
        testnet=False,
        verbose=False,
    )


def fetch_binance_equity(client: Client) -> float:
    params = {"recvWindow": 5000, "timestamp": int(time.time() * 1000)}
    balances = client.papi_get_balance(**params)
    usdt_row = next((b for b in balances if b["asset"] == "USDT"), None)
    if usdt_row is None:
        raise ValueError("No USDT row in Binance balances")
    wallet = float(usdt_row["totalWalletBalance"])
    upnl = float(usdt_row["umUnrealizedPNL"])
    return wallet + upnl


def fetch_binance_positions(client: Client) -> List[Dict[str, Any]]:
    resp = client.papi_get_um_account(recvWindow=5000)
    positions = []
    for p in resp.get("positions", []):
        amt = float(p.get("positionAmt", 0) or 0)
        if amt == 0:
            continue
        entry = float(p.get("entryPrice", 0) or 0)
        upnl = float(p.get("unrealizedProfit", 0) or 0)
        side = "SHORT" if amt < 0 else "LONG"
        notional_entry = abs(amt) * entry
        if side == "LONG":
            notional_mtm = notional_entry + upnl
        else:
            notional_mtm = notional_entry - upnl
        positions.append({
            "symbol": p.get("symbol"),
            "positionAmt": amt,
            "side": side,
            "notional_mtm": notional_mtm,
        })
    return positions


def compute_gross_leverage(positions: List[Dict[str, Any]], equity: float) -> float:
    if not positions or equity == 0:
        return 0.0
    gross_notional = sum(p["notional_mtm"] for p in positions)
    return gross_notional / equity


def gate_signed_request(method: str, url: str, query_param: str = "", body_param: str = ""):
    timestamp = str(int(time.time()))
    body_hash = hashlib.sha512(body_param.encode("utf-8")).hexdigest()
    sign_string = "\n".join([
        method,
        f"{GATE_PREFIX}{url}",
        query_param,
        body_hash,
        timestamp,
    ])
    sign = hmac.new(
        GATE_API_SECRET.encode("utf-8"),
        sign_string.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    headers = {
        "Timestamp": timestamp,
        "KEY": GATE_API_KEY,
        "SIGN": sign,
        "Content-Type": "application/json",
    }
    r = requests.request(method, f"{GATE_HOST}{GATE_PREFIX}{url}", headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_gate_spot_holdings() -> Dict[str, float]:
    data = gate_signed_request("GET", "/spot/accounts")
    holdings: Dict[str, float] = {}
    for row in data:
        available = float(row.get("available", 0) or 0)
        locked = float(row.get("locked", 0) or 0)
        total = available + locked
        if total != 0:
            holdings[row["currency"]] = total
    return holdings


def fetch_gate_spot_prices(currencies: List[str]) -> Dict[str, float]:
    prices: Dict[str, float] = {}
    stablecoins = {"USDT", "USDC"}
    for c in currencies:
        if c in stablecoins:
            prices[c] = 1.0
            continue
        try:
            url = f"{GATE_HOST}{GATE_PREFIX}/spot/tickers"
            r = requests.get(url, params={"currency_pair": f"{c}_USDT"}, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data and len(data) > 0:
                prices[c] = float(data[0].get("last", 0))
            else:
                prices[c] = 0.0
        except Exception:
            prices[c] = 0.0
    return prices


def compute_gate_equity(holdings: Dict[str, float], prices: Dict[str, float]) -> float:
    return sum(qty * prices.get(token, 0.0) for token, qty in holdings.items())


def compute_net_delta(
    binance_positions: List[Dict[str, Any]],
    gate_holdings: Dict[str, float],
    prices: Dict[str, float],
) -> List[Dict[str, Any]]:
    bn_exposure: Dict[str, float] = {}
    for p in binance_positions:
        token = p["symbol"].replace("USDT", "")
        bn_exposure[token] = bn_exposure.get(token, 0) + p["positionAmt"]

    all_tokens = sorted(set(list(bn_exposure.keys()) + list(gate_holdings.keys())))
    deltas = []
    for token in all_tokens:
        if token == "USDT":
            print("skipping USDT delta")
            continue
        perp_qty = bn_exposure.get(token, 0.0)
        spot_qty = gate_holdings.get(token, 0.0)
        net_qty = spot_qty + perp_qty
        price = prices.get(token, 0.0)
        net_usd = net_qty * price
        if abs(net_usd) < 0.01:
            continue
        deltas.append({
            "token": token,
            "net_qty": net_qty,
            "net_usd": net_usd,
        })
    return deltas


def main():
    bn_equity: Optional[float] = None
    bn_positions: Optional[List[Dict[str, Any]]] = None
    gate_holdings: Optional[Dict[str, float]] = None
    gate_prices: Optional[Dict[str, float]] = None
    binance_ok = True
    gate_ok = True

    try:
        bn_client = build_binance_client()
    except Exception as e:
        send_slack_error("Binance client init", e)
        print(f"FATAL: Binance client init failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        bn_equity = fetch_binance_equity(bn_client)
        bn_positions = fetch_binance_positions(bn_client)
    except Exception as e:
        send_slack_error("Binance API", e)
        print(f"ERROR: Binance API failed: {e}", file=sys.stderr)
        binance_ok = False

    try:
        gate_holdings = fetch_gate_spot_holdings()
        gate_prices = fetch_gate_spot_prices(list(gate_holdings.keys()))
    except Exception as e:
        send_slack_error("Gate.io API", e)
        print(f"ERROR: Gate.io API failed: {e}", file=sys.stderr)
        gate_ok = False

    if not binance_ok and not gate_ok:
        send_slack_alert(
            ":fire: *Soleil Risk Monitor*: Both Binance and Gate.io API calls failed. "
            "No risk data available."
        )
        sys.exit(1)

    gross_leverage = 0.0
    gate_equity = 0.0

    if binance_ok:
        gross_leverage = compute_gross_leverage(bn_positions, bn_equity)

    if gate_ok:
        gate_equity = compute_gate_equity(gate_holdings, gate_prices)

    total_equity = (bn_equity or 0.0) + gate_equity

    print(f"Binance Equity:   ${bn_equity or 0:,.2f}" + ("" if binance_ok else "  [STALE]"))
    print(f"Gate.io Equity:   ${gate_equity:,.2f}" + ("" if gate_ok else "  [STALE]"))
    print(f"Total Equity:     ${total_equity:,.2f}")
    print(f"Gross Leverage:   {gross_leverage:.2f}x")
    print()

    deltas: List[Dict[str, Any]] = []
    if binance_ok and gate_ok:
        deltas = compute_net_delta(bn_positions, gate_holdings, gate_prices)

    if deltas:
        print("Net Delta:")
        for d in sorted(deltas, key=lambda x: abs(x["net_usd"]), reverse=True):
            print(f"  {d['token']:>8s}  {d['net_qty']:>14.4f}  ${d['net_usd']:>12,.2f}")

    alerts = []

    if binance_ok and gross_leverage > LEVERAGE_THRESHOLD:
        alerts.append(
            f":warning: *Soleil Leverage Alert*: Binance gross leverage is "
            f"{gross_leverage:.2f}x, check positions! "
            f"(threshold: {LEVERAGE_THRESHOLD:.1f}x)"
        )

    if total_equity < EQUITY_THRESHOLD:
        partial = ""
        if not binance_ok or not gate_ok:
            partial = " (partial data - one exchange failed)"
        alerts.append(
            f":rotating_light: *Soleil Equity Alert*: Total portfolio equity is "
            f"${total_equity:,.2f}, reach out to Soleil team immediately! "
            f"(threshold: ${EQUITY_THRESHOLD:,.2f}){partial}"
        )

    for d in deltas:
        if abs(d["net_usd"]) > DELTA_THRESHOLD:
            direction = "long" if d["net_usd"] > 0 else "short"
            alerts.append(
                f":scales: *Soleil Delta Alert*: Net {direction} {d['token']}, "
                f"check positions! Net deltas = {d['net_qty']:.4f} "
                f"(${d['net_usd']:,.2f})"
            )

    if alerts:
        message = "\n".join(alerts)
        print(f"\nFiring {len(alerts)} alert(s)...")
        send_slack_alert(message)
    else:
        print("\nNo alerts triggered.")


if __name__ == "__main__":
    main()
