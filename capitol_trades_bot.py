#!/usr/bin/env python3
"""
Capitol Trades Bot
Finds the most recently successful politician trader on capitoltrades.com
and displays their trades as a schedule.
"""

import re
import json
import sys
from datetime import datetime, timedelta
import requests

BASE_URL = "https://www.capitoltrades.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/x-component",
    "RSC": "1",
}

# ── RSC helpers ──────────────────────────────────────────────────────────────

def fetch_rsc(path: str, params: dict | None = None) -> str:
    url = BASE_URL + path
    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_json_objects(text: str, key: str) -> list[dict]:
    """
    Pull every {...} block from the RSC wire format that contains `key`.
    The RSC payload is not valid JSON at the top level, but the embedded
    data objects are. We find the opening brace nearest to each `key`
    occurrence and walk forward to the matching closing brace.
    """
    objects = []
    for m in re.finditer(re.escape(f'"{key}"'), text):
        # Walk back to find the start of the enclosing object
        start = text.rfind('{', 0, m.start())
        if start == -1:
            continue
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        chunk = text[start:end + 1]
        try:
            obj = json.loads(chunk)
            if key in obj:
                objects.append(obj)
        except json.JSONDecodeError:
            pass
    return objects


# ── Politicians ──────────────────────────────────────────────────────────────

def fetch_politicians(page_size: int = 12) -> list[dict]:
    """Return top politicians sorted by all-time volume."""
    text = fetch_rsc("/politicians", params={"pageSize": page_size, "sortBy": "-volume"})

    # Extract politician IDs from href links
    pol_ids = re.findall(r'href\":\"(/politicians/([A-Z]\d+))\"', text)
    # Extract card bodies split by the card CSS class
    cards = re.split(r"politician-index-card-body", text)

    politicians = []
    for i, (full_path, pol_id) in enumerate(pol_ids):
        card = cards[i + 1] if i + 1 < len(cards) else ""

        name_m   = re.search(r'"children":"([A-Z][a-z]+(?: [A-Z][a-zA-Z]+)+)"', card)
        party_m  = re.search(r'party--(\w+)', card)
        state_m  = re.search(r'us-state-full--(\w+)', card)
        # Use cell class names for reliable extraction
        trades_m = re.search(r'cell--count-trades[^"]*".*?"children":"([\d,]+)"', card, re.DOTALL)
        volume_m = re.search(r'cell--volume[^"]*".*?"children":"([\d,\.]+[MBK]?)"', card, re.DOTALL)
        date_m   = re.search(r'(\d{4}-\d{2}-\d{2})', card)

        def vol_to_float(s: str) -> float:
            s = s.replace(',', '').rstrip('M').rstrip('B').rstrip('K')
            try:
                return float(s)
            except ValueError:
                return 0.0

        politicians.append({
            "id": pol_id,
            "path": full_path,
            "name": name_m.group(1) if name_m else pol_id,
            "party": party_m.group(1).title() if party_m else "?",
            "state": state_m.group(1).upper() if state_m else "?",
            "trades": trades_m.group(1) if trades_m else "0",
            "volume_raw": (volume_m.group(1) if volume_m else "0").rstrip("MBK"),
            "volume": vol_to_float(volume_m.group(1)) if volume_m else 0.0,
            "last_trade": date_m.group(1) if date_m else "N/A",
        })

    return politicians


# ── Trades ───────────────────────────────────────────────────────────────────

TRADE_FIELDS = (
    "_txId", "_politicianId", "_issuerId",
    "txDate", "pubDate", "txType", "txTypeExtended",
    "value", "price", "owner", "comment", "reportingGap",
    "chamber",
)


def _parse_trade_block(block: str) -> dict | None:
    """Parse a single trade JSON object from the RSC stream."""
    try:
        obj = json.loads(block)
    except json.JSONDecodeError:
        return None
    if "_txId" not in obj:
        return None

    issuer = obj.get("issuer") or {}
    politician = obj.get("politician") or {}

    # Capitol Trades uses issuerTicker / issuerName keys
    raw_ticker = issuer.get("issuerTicker") or issuer.get("ticker") or ""
    ticker = raw_ticker.split(":")[0] if raw_ticker else ""

    company = (
        issuer.get("issuerName")
        or issuer.get("name")
        or "Unknown"
    )

    return {
        "tx_id":         obj.get("_txId"),
        "tx_date":       obj.get("txDate", ""),
        "pub_date":      obj.get("pubDate", ""),
        "tx_type":       (obj.get("txType") or "").lower(),
        "tx_extended":   obj.get("txTypeExtended") or "",
        "value":         obj.get("value") or 0,
        "price":         obj.get("price"),
        "owner":         obj.get("owner", ""),
        "reporting_gap": obj.get("reportingGap") or 0,
        "ticker":        ticker,
        "company":       company,
        "sector":        issuer.get("sector") or "",
        "politician":    (
            politician.get("nickname")
            or f"{politician.get('firstName', '')} {politician.get('lastName', '')}".strip()
            or politician.get("name", "")
        ),
        "comment":       obj.get("comment", ""),
    }


def fetch_trades(pol_path: str, page_size: int = 100) -> list[dict]:
    """Fetch all trades for a politician, paginating if needed."""
    all_trades: list[dict] = []
    page = 1

    while True:
        text = fetch_rsc(pol_path, params={"pageSize": page_size, "page": page})
        trades_found = _extract_trades_from_rsc(text)

        if not trades_found:
            break
        all_trades.extend(trades_found)

        # Check pagination: look for totalPages in the RSC text
        total_pages_m = re.search(r'"totalPages":(\d+)', text)
        total_pages = int(total_pages_m.group(1)) if total_pages_m else 1
        if page >= total_pages:
            break
        page += 1

    return all_trades


def _extract_trades_from_rsc(text: str) -> list[dict]:
    """
    Pull all trade objects out of the RSC wire-format payload.
    We look for every occurrence of `"_txId"` and extract the
    enclosing JSON object.
    """
    trades = []
    seen_ids: set = set()

    for m in re.finditer(r'"_txId"', text):
        start = text.rfind('{', 0, m.start())
        if start == -1:
            continue
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        block = text[start:end + 1]
        trade = _parse_trade_block(block)
        if trade and trade["tx_id"] not in seen_ids:
            seen_ids.add(trade["tx_id"])
            trades.append(trade)

    return trades


# ── Scoring ──────────────────────────────────────────────────────────────────

def score_politician(pol: dict, trades: list[dict]) -> float:
    """
    Success score based on:
      - Recent activity (recency of last trade, max 40 pts)
      - Buy-side dominance in the last 90 days (max 30 pts)
      - Raw trade volume (max 30 pts)
    """
    today = datetime.utcnow().date()
    ninety_days_ago = today - timedelta(days=90)

    # Recency score
    try:
        last_dt = datetime.strptime(pol["last_trade"], "%Y-%m-%d").date()
        days_ago = (today - last_dt).days
        recency = max(0, 40 - days_ago * 0.5)
    except ValueError:
        recency = 0

    # Buy ratio in last 90 days
    recent = [t for t in trades if t["tx_date"] >= str(ninety_days_ago)]
    buys  = sum(1 for t in recent if t["tx_type"] == "buy")
    sells = sum(1 for t in recent if t["tx_type"] == "sell")
    total_recent = buys + sells
    buy_ratio = (buys / total_recent) if total_recent else 0.5
    buy_score = buy_ratio * 30

    # Volume score (log-scaled, max 30 pts)
    import math
    vol = pol["volume"]
    vol_score = min(30, math.log10(vol + 1) * 6) if vol > 0 else 0

    return recency + buy_score + vol_score


# ── Display ──────────────────────────────────────────────────────────────────

VALUE_RANGES = {
    (0,        1_000):      "<$1K",
    (1_000,    15_000):     "$1K–$15K",
    (15_000,   50_000):     "$15K–$50K",
    (50_000,   100_000):    "$50K–$100K",
    (100_000,  250_000):    "$100K–$250K",
    (250_000,  500_000):    "$250K–$500K",
    (500_000,  1_000_000):  "$500K–$1M",
    (1_000_000, 5_000_000): "$1M–$5M",
    (5_000_000, float("inf")): ">$5M",
}


def fmt_value(v: int | float) -> str:
    for (lo, hi), label in VALUE_RANGES.items():
        if lo <= v < hi:
            return label
    return f"${v:,.0f}"


def print_schedule(pol: dict, trades: list[dict], max_trades: int = 40) -> None:
    party_symbol = {"democrat": "D", "republican": "R"}.get(pol["party"].lower(), "?")

    print("\n" + "═" * 72)
    print(f"  MOST SUCCESSFUL RECENT TRADER: {pol['name']}")
    print(f"  ({party_symbol}) {pol['party']} · {pol['state']} · Path: {pol['path']}")
    print(f"  All-time volume: ${pol['volume_raw']}M  |  Total trades: {pol['trades']}")
    print(f"  Last trade: {pol['last_trade']}")
    print("═" * 72)

    # Sort trades newest → oldest, limit
    sorted_trades = sorted(trades, key=lambda t: t["tx_date"], reverse=True)[:max_trades]

    if not sorted_trades:
        print("  No trade data available.")
        return

    # Group by month
    current_month = ""
    for t in sorted_trades:
        month = t["tx_date"][:7] if t["tx_date"] else "Unknown"
        if month != current_month:
            current_month = month
            try:
                label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
            except ValueError:
                label = month
            print(f"\n  ── {label} {'─' * (55 - len(label))}")
            print(f"  {'Date':<12} {'Type':<8} {'Ticker':<8} {'Company':<28} {'Size'}")
            print(f"  {'────':<12} {'────':<8} {'──────':<8} {'───────────────────────────':<28} {'────────────────'}")

        tx_type = t["tx_type"].upper()
        if t["tx_extended"]:
            tx_type = t["tx_extended"].upper()[:7]

        # Colorise (ANSI) buy=green, sell=red
        color = "\033[32m" if "buy" in t["tx_type"] else "\033[31m" if "sell" in t["tx_type"] else "\033[0m"
        reset = "\033[0m"

        ticker  = t["ticker"][:7] if t["ticker"] else "—"
        company = (t["company"] or "Unknown")[:27]
        size    = fmt_value(t["value"]) if t["value"] else "N/A"

        print(f"  {t['tx_date']:<12} {color}{tx_type:<8}{reset} {ticker:<8} {company:<28} {size}")

        if t.get("comment"):
            # Show a one-line description
            desc = t["comment"].replace("Description: ", "").strip()
            if len(desc) > 70:
                desc = desc[:67] + "..."
            print(f"  {'':12} {'':8} {'':8}  → {desc}")

    print("\n" + "─" * 72)
    # Summary
    buys  = [t for t in sorted_trades if t["tx_type"] == "buy"]
    sells = [t for t in sorted_trades if t["tx_type"] == "sell"]
    buy_vol  = sum(t["value"] for t in buys)
    sell_vol = sum(t["value"] for t in sells)
    print(f"  Showing {len(sorted_trades)} most recent trades")
    print(f"  Buys : {len(buys):>3}  (~{fmt_value(buy_vol)} total value)")
    print(f"  Sells: {len(sells):>3}  (~{fmt_value(sell_vol)} total value)")
    print("─" * 72 + "\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Capitol Trades Bot  —  finding the most successful recent trader …\n")

    # 1. Get top politicians by all-time volume
    print("Fetching politicians list …")
    politicians = fetch_politicians(page_size=12)
    if not politicians:
        print("ERROR: could not fetch politicians list.", file=sys.stderr)
        sys.exit(1)

    print(f"  Found {len(politicians)} politicians. Analysing recent activity …\n")

    # 2. Score each by fetching their trades
    scored: list[tuple[float, dict, list[dict]]] = []
    for pol in politicians:
        sys.stdout.write(f"  Checking {pol['name']} ({pol['id']}) … ")
        sys.stdout.flush()
        try:
            trades = fetch_trades(pol["path"])
            score  = score_politician(pol, trades)
            scored.append((score, pol, trades))
            print(f"score={score:.1f}  trades_fetched={len(trades)}")
        except Exception as e:
            print(f"ERROR: {e}")

    if not scored:
        print("ERROR: no data retrieved.", file=sys.stderr)
        sys.exit(1)

    # 3. Pick the winner
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_pol, best_trades = scored[0]

    print(f"\nWinner: {best_pol['name']}  (score={best_score:.1f})\n")

    # 4. Print their trade schedule
    print_schedule(best_pol, best_trades, max_trades=40)

    # 5. Also print a leaderboard
    print("\nLeaderboard (all evaluated politicians):")
    print(f"  {'Rank':<5} {'Name':<28} {'Score':>6}  {'Last Trade':<12} {'Volume'}")
    print(f"  {'────':<5} {'────────────────────────────':<28} {'─────':>6}  {'──────────':<12} {'──────'}")
    for rank, (sc, p, _) in enumerate(scored, 1):
        print(f"  {rank:<5} {p['name']:<28} {sc:>6.1f}  {p['last_trade']:<12} ${p['volume_raw']}M")
    print()


if __name__ == "__main__":
    main()
