#!/usr/bin/env python3
"""
Capitol Trades Bot
Finds the most recently successful politician trader on capitoltrades.com,
displays their trades as a schedule, and ranks all actionable buy signals
by conviction score (size, options, repetition, committee relevance, recency).
"""

import math
import re
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date
import requests

BASE_URL = "https://www.capitoltrades.com"

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_RSC_HEADERS = {
    **_BASE_HEADERS,
    "Accept": "text/x-component",
    "Next-Router-State-Tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%5D%7D%2Cnull%2Cnull%2Ctrue%5D",
    "Next-Router-Prefetch": "1",
    "RSC": "1",
    "Referer": BASE_URL + "/",
}

# Shared session — seeded with real browser cookies on first use
_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    s = requests.Session()
    s.headers.update(_BASE_HEADERS)
    # Warm up: visit the home page so the CDN/WAF issues real cookies
    try:
        warm = s.get(BASE_URL + "/", timeout=30)
        warm.raise_for_status()
    except Exception:
        pass
    _SESSION = s
    return s

# ── Committee data ────────────────────────────────────────────────────────────
# Maps politician bioguide ID → sectors their committees oversee.
# Used to give a bonus when a politician buys in their own wheelhouse
# (they see regulatory/contract flow before the public does).

COMMITTEE_SECTORS = {
    "M001157": ["industrials", "technology", "energy"],           # McCaul: Foreign Affairs, Homeland Security
    "K000389": ["industrials", "technology", "energy"],           # Khanna: Armed Services, Oversight
    "S001229": ["technology", "industrials", "health care"],      # Shreve: Science & Tech, Veterans
    "G000583": ["financials", "technology"],                      # Gottheimer: Financial Services
    "I000056": ["technology", "financials"],                      # Issa: Judiciary, Foreign Affairs
    "B001277": ["industrials", "technology", "communication"],    # Blumenthal: Judiciary, Armed Services, Commerce
    "P000197": [],                                                # Pelosi: Leadership (no committee — broader power)
    "S001217": ["industrials", "financials", "energy"],           # Rick Scott: Armed Services, Banking, Commerce
    "T000483": ["financials"],                                    # Trone: Financial Services (former)
    "M001243": ["financials", "industrials", "energy"],           # McCormick: Banking, Armed Services, Foreign Relations
    "P000608": ["energy", "technology", "health care"],           # Peters: Energy & Commerce, Science & Tech
    "D000617": ["financials", "energy"],                          # DelBene: Ways & Means, Joint Economic
}

# Politician IDs known to have strong historical trading track records
ALPHA_POLITICIANS = {"P000197"}  # Pelosi — consistently cited for market outperformance

# ── RSC helpers ──────────────────────────────────────────────────────────────

def fetch_rsc(path: str, params: dict | None = None) -> str:
    s = _get_session()
    url = BASE_URL + path
    resp = s.get(url, headers=_RSC_HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


# ── Politicians ──────────────────────────────────────────────────────────────

def fetch_politicians(page_size: int = 12) -> list[dict]:
    """Return top politicians sorted by all-time volume."""
    text = fetch_rsc("/politicians", params={"pageSize": page_size, "sortBy": "-volume"})

    pol_ids = re.findall(r'href\":\"(/politicians/([A-Z]\d+))\"', text)
    cards   = re.split(r"politician-index-card-body", text)

    politicians = []
    for i, (full_path, pol_id) in enumerate(pol_ids):
        card = cards[i + 1] if i + 1 < len(cards) else ""

        name_m   = re.search(r'"children":"([A-Z][a-z]+(?: [A-Z][a-zA-Z]+)+)"', card)
        party_m  = re.search(r'party--(\w+)', card)
        state_m  = re.search(r'us-state-full--(\w+)', card)
        trades_m = re.search(r'cell--count-trades[^"]*".*?"children":"([\d,]+)"', card, re.DOTALL)
        volume_m = re.search(r'cell--volume[^"]*".*?"children":"([\d,\.]+[MBK]?)"', card, re.DOTALL)
        date_m   = re.search(r'(\d{4}-\d{2}-\d{2})', card)

        def vol_to_float(s: str) -> float:
            s = s.replace(',', '').rstrip('MBK')
            try:
                return float(s)
            except ValueError:
                return 0.0

        politicians.append({
            "id":         pol_id,
            "path":       full_path,
            "name":       name_m.group(1) if name_m else pol_id,
            "party":      party_m.group(1).title() if party_m else "?",
            "state":      state_m.group(1).upper() if state_m else "?",
            "trades":     trades_m.group(1) if trades_m else "0",
            "volume_raw": (volume_m.group(1) if volume_m else "0").rstrip("MBK"),
            "volume":     vol_to_float(volume_m.group(1)) if volume_m else 0.0,
            "last_trade": date_m.group(1) if date_m else "N/A",
        })

    return politicians


# ── Trades ───────────────────────────────────────────────────────────────────

def _parse_trade_block(block: str) -> dict | None:
    try:
        obj = json.loads(block)
    except json.JSONDecodeError:
        return None
    if "_txId" not in obj:
        return None

    issuer    = obj.get("issuer") or {}
    politician = obj.get("politician") or {}

    raw_ticker = issuer.get("issuerTicker") or issuer.get("ticker") or ""
    ticker = raw_ticker.split(":")[0] if raw_ticker else ""

    company = issuer.get("issuerName") or issuer.get("name") or "Unknown"

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


def _extract_trades_from_rsc(text: str) -> list[dict]:
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


def fetch_trades(pol_path: str, page_size: int = 100) -> list[dict]:
    all_trades: list[dict] = []
    page = 1

    while True:
        text = fetch_rsc(pol_path, params={"pageSize": page_size, "page": page})
        trades_found = _extract_trades_from_rsc(text)
        if not trades_found:
            break
        all_trades.extend(trades_found)

        total_pages_m = re.search(r'"totalPages":(\d+)', text)
        total_pages = int(total_pages_m.group(1)) if total_pages_m else 1
        if page >= total_pages:
            break
        page += 1

    return all_trades


# ── Politician-level scoring (who to follow) ─────────────────────────────────

def score_politician(pol: dict, trades: list[dict]) -> float:
    """Composite score: recency + buy-side dominance + volume."""
    today = datetime.utcnow().date()
    ninety_days_ago = today - timedelta(days=90)

    try:
        last_dt = datetime.strptime(pol["last_trade"], "%Y-%m-%d").date()
        recency = max(0, 40 - (today - last_dt).days * 0.5)
    except ValueError:
        recency = 0

    recent = [t for t in trades if t["tx_date"] >= str(ninety_days_ago)]
    buys   = sum(1 for t in recent if t["tx_type"] == "buy")
    sells  = sum(1 for t in recent if t["tx_type"] == "sell")
    total  = buys + sells
    buy_score = (buys / total * 30) if total else 15

    vol_score = min(30, math.log10(pol["volume"] + 1) * 6) if pol["volume"] > 0 else 0

    return recency + buy_score + vol_score


# ── Per-trade signal scoring ──────────────────────────────────────────────────

def _is_real_stock(trade: dict) -> bool:
    """Return True if the trade looks like a single stock (not a bond/fund/LLC)."""
    ticker  = trade.get("ticker", "")
    company = (trade.get("company") or "").lower()
    comment = (trade.get("comment") or "").lower()

    if not ticker:
        return False
    if "coupon" in comment or "matures" in comment:
        return False
    noise_words = [
        "llc", " lp", "fund", "authority", "district",
        "city of", "state of", "department", "treasury bill",
        "finance corp", "finance ltd",
    ]
    return not any(w in company for w in noise_words)


def signal_score(trade: dict, pol: dict, pol_trades: list[dict]) -> tuple[int, list[str]]:
    """
    Score a single buy trade on five dimensions:
      1. Asset quality  — single stock beats ETF beats bond
      2. Position size  — larger = more conviction
      3. Options flag   — exercised calls = pre-planned, highest conviction
      4. Repetition     — same ticker bought multiple times by same politician
      5. Committee fit  — buying in the sector their committee oversees
      6. Recency        — fresher filings are more actionable
      7. Alpha track    — extra weight for politicians with proven records
    """
    score   = 0
    reasons = []

    ticker  = trade.get("ticker", "")
    company = (trade.get("company") or "").lower()
    comment = (trade.get("comment") or "").lower()
    sector  = (trade.get("sector") or "").lower()
    pol_id  = pol.get("id", "")

    # 1. Asset quality
    if _is_real_stock(trade):
        score += 15
        reasons.append("Single stock")
    elif any(x in company for x in ["etf", "ishares", "vanguard", "spdr"]):
        score += 3
        reasons.append("ETF")
    else:
        score -= 15
        reasons.append("Bond / structured note / private fund")

    # 2. Position size
    value = trade.get("value", 0) or 0
    if value >= 5_000_000:
        score += 40; reasons.append(">$5M position")
    elif value >= 1_000_000:
        score += 30; reasons.append("$1M–$5M position")
    elif value >= 500_000:
        score += 20; reasons.append("$500K–$1M position")
    elif value >= 250_000:
        score += 15; reasons.append("$250K–$500K position")
    elif value >= 100_000:
        score += 8;  reasons.append("$100K–$250K position")
    elif value >= 15_000:
        score += 3;  reasons.append("$15K–$50K position")
    else:
        score += 1;  reasons.append("<$15K (low conviction)")

    # 3. Options exercise
    tx_ext = (trade.get("tx_extended") or "").lower()
    if any(w in tx_ext + comment for w in ["option", "exercised", " call ", "call option"]):
        score += 25
        reasons.append("Options exercise — pre-planned conviction")

    # 4. Repetition
    if ticker:
        n_buys = sum(
            1 for t in pol_trades
            if t.get("ticker") == ticker and t.get("tx_type") == "buy"
        )
        if n_buys >= 5:
            score += 30; reasons.append(f"Bought {n_buys}× total (very high repetition)")
        elif n_buys >= 3:
            score += 20; reasons.append(f"Bought {n_buys}× total (high repetition)")
        elif n_buys == 2:
            score += 10; reasons.append("Bought 2× (repeated)")

    # 5. Committee fit
    relevant = COMMITTEE_SECTORS.get(pol_id, [])
    if sector and relevant and any(s in sector for s in relevant):
        score += 20
        reasons.append(f"Sector ({sector}) aligns with committee oversight")

    # 6. Recency (effective age = days since trade minus mandatory 45-day lag)
    tx_date = trade.get("tx_date", "")
    if tx_date:
        try:
            trade_dt     = datetime.strptime(tx_date, "%Y-%m-%d").date()
            days_old     = (datetime.utcnow().date() - trade_dt).days
            effective    = max(0, days_old - 45)
            recency_pts  = max(0.0, 15 - effective * 0.05)
            score       += recency_pts
            if recency_pts > 10:
                reasons.append("Fresh filing")
        except ValueError:
            pass

    # 7. Alpha-politician premium
    if pol_id in ALPHA_POLITICIANS:
        score += 15
        reasons.append("Proven alpha trader")

    return max(0, int(score)), reasons


# ── Signal aggregation across all politicians ────────────────────────────────

def generate_signals(results: list[tuple[dict, list[dict]]]) -> list[dict]:
    """
    Score every buy trade, then aggregate per ticker.
    Multiple politicians independently buying the same stock
    is a strong consensus signal — we boost the score accordingly.
    """
    trade_signals = []
    for pol, trades in results:
        for trade in trades:
            if trade.get("tx_type") != "buy":
                continue
            sc, reasons = signal_score(trade, pol, trades)
            trade_signals.append({
                "trade":   trade,
                "pol":     pol,
                "score":   sc,
                "reasons": reasons,
            })

    # Group by ticker (fall back to company name for untickered assets)
    groups: dict[str, list] = defaultdict(list)
    for s in trade_signals:
        key = s["trade"].get("ticker") or (s["trade"].get("company") or "")[:20]
        groups[key].append(s)

    aggregated = []
    for ticker, signals in groups.items():
        if not ticker:
            continue

        total_score   = sum(s["score"] for s in signals)
        pols_buying   = list(dict.fromkeys(s["pol"]["name"] for s in signals))  # ordered unique
        consensus_boost = 30 * (len(pols_buying) - 1)
        total_score  += consensus_boost

        most_recent = max(signals, key=lambda s: s["trade"].get("tx_date", ""))
        all_reasons  = list(dict.fromkeys(r for s in signals for r in s["reasons"]))

        aggregated.append({
            "ticker":          ticker,
            "company":         most_recent["trade"].get("company", ""),
            "sector":          most_recent["trade"].get("sector", ""),
            "total_score":     total_score,
            "signals":         signals,
            "pols_buying":     pols_buying,
            "last_trade_date": most_recent["trade"].get("tx_date", ""),
            "last_value":      most_recent["trade"].get("value", 0),
            "all_reasons":     all_reasons,
            "consensus_boost": consensus_boost,
        })

    aggregated.sort(key=lambda x: x["total_score"], reverse=True)
    return aggregated


# ── Display helpers ───────────────────────────────────────────────────────────

VALUE_RANGES = {
    (0,          1_000):      "<$1K",
    (1_000,     15_000):      "$1K–$15K",
    (15_000,    50_000):      "$15K–$50K",
    (50_000,   100_000):      "$50K–$100K",
    (100_000,  250_000):      "$100K–$250K",
    (250_000,  500_000):      "$250K–$500K",
    (500_000, 1_000_000):     "$500K–$1M",
    (1_000_000, 5_000_000):   "$1M–$5M",
    (5_000_000, float("inf")): ">$5M",
}


def fmt_value(v: int | float) -> str:
    for (lo, hi), label in VALUE_RANGES.items():
        if lo <= v < hi:
            return label
    return f"${v:,.0f}"


STRENGTH_LABELS = [
    (120, "VERY HIGH", "\033[32m"),
    (80,  "HIGH",      "\033[32m"),
    (50,  "MEDIUM",    "\033[33m"),
    (20,  "LOW",       "\033[31m"),
    (0,   "NOISE",     "\033[90m"),
]

def strength_label(score: int) -> tuple[str, str]:
    for threshold, label, color in STRENGTH_LABELS:
        if score >= threshold:
            return label, color
    return "NOISE", "\033[90m"


def print_signals(signals: list[dict], top_n: int = 15) -> None:
    print("\n" + "═" * 72)
    print("  ACTIONABLE SIGNALS  —  ranked by master-trader conviction score")
    print("  Scoring: size + options + repetition + committee fit + recency + track record")
    print("═" * 72)

    displayed = 0
    for agg in signals:
        ticker  = agg["ticker"]
        company = agg["company"]

        # Skip non-stock signals in the main list
        if not ticker or len(ticker) > 6:
            continue
        # Skip obvious bond/fund names even if they have a ticker
        company_l = company.lower()
        if any(w in company_l for w in ["finance corp", "finance ltd", "treasury", "municipal"]):
            continue

        if displayed >= top_n:
            break
        displayed += 1

        score    = agg["total_score"]
        label, color = strength_label(score)
        reset    = "\033[0m"
        pols     = ", ".join(agg["pols_buying"])
        last_dt  = agg["last_trade_date"]
        size     = fmt_value(agg["last_value"])
        sector   = agg["sector"].title() if agg["sector"] else "?"
        n_trades = len(agg["signals"])

        bar_filled = min(24, score // 6)
        bar = "█" * bar_filled + "░" * (24 - bar_filled)

        print(f"\n  #{displayed:<3} {color}{ticker:<8}{reset}  {company[:38]:<38}  {color}{label}{reset}")
        print(f"       Score: {score:>4}  [{bar}]")
        print(f"       Sector: {sector:<20}  Trades tracked: {n_trades}")
        print(f"       Buyer(s): {pols}")
        if len(agg["pols_buying"]) > 1:
            print(f"       ⚡ CONSENSUS — {len(agg['pols_buying'])} politicians buying independently")
        print(f"       Last buy: {last_dt}  Size: {size}")

        # Top 4 reasons
        for r in agg["all_reasons"][:4]:
            print(f"       → {r}")

    print("\n" + "─" * 72 + "\n")


def print_schedule(pol: dict, trades: list[dict], max_trades: int = 40) -> None:
    party_symbol = {"democrat": "D", "republican": "R"}.get(pol["party"].lower(), "?")

    print("\n" + "═" * 72)
    print(f"  MOST SUCCESSFUL RECENT TRADER: {pol['name']}")
    print(f"  ({party_symbol}) {pol['party']} · {pol['state']} · Path: {pol['path']}")
    print(f"  All-time volume: ${pol['volume_raw']}M  |  Total trades: {pol['trades']}")
    print(f"  Last trade: {pol['last_trade']}")
    print("═" * 72)

    sorted_trades = sorted(trades, key=lambda t: t["tx_date"], reverse=True)[:max_trades]
    if not sorted_trades:
        print("  No trade data available.")
        return

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

        color = "\033[32m" if "buy" in t["tx_type"] else "\033[31m" if "sell" in t["tx_type"] else "\033[0m"
        reset = "\033[0m"
        ticker  = t["ticker"][:7] if t["ticker"] else "—"
        company = (t["company"] or "Unknown")[:27]
        size    = fmt_value(t["value"]) if t["value"] else "N/A"

        print(f"  {t['tx_date']:<12} {color}{tx_type:<8}{reset} {ticker:<8} {company:<28} {size}")

        if t.get("comment"):
            desc = t["comment"].replace("Description: ", "").strip()
            if len(desc) > 70:
                desc = desc[:67] + "..."
            print(f"  {'':12} {'':8} {'':8}  → {desc}")

    print("\n" + "─" * 72)
    buys  = [t for t in sorted_trades if t["tx_type"] == "buy"]
    sells = [t for t in sorted_trades if t["tx_type"] == "sell"]
    print(f"  Showing {len(sorted_trades)} most recent trades")
    print(f"  Buys : {len(buys):>3}  (~{fmt_value(sum(t['value'] for t in buys))} total value)")
    print(f"  Sells: {len(sells):>3}  (~{fmt_value(sum(t['value'] for t in sells))} total value)")
    print("─" * 72 + "\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Capitol Trades Bot  —  master trader signal scanner\n")

    print("Fetching politicians list …")
    politicians = fetch_politicians(page_size=12)
    if not politicians:
        print("ERROR: could not fetch politicians list.", file=sys.stderr)
        sys.exit(1)

    print(f"  Found {len(politicians)} politicians. Fetching all trades …\n")

    results: list[tuple[dict, list[dict]]] = []
    scored:  list[tuple[float, dict, list[dict]]] = []

    for pol in politicians:
        sys.stdout.write(f"  {pol['name']:<28} … ")
        sys.stdout.flush()
        try:
            trades = fetch_trades(pol["path"])
            sc     = score_politician(pol, trades)
            results.append((pol, trades))
            scored.append((sc, pol, trades))
            buys  = sum(1 for t in trades if t["tx_type"] == "buy")
            sells = sum(1 for t in trades if t["tx_type"] == "sell")
            print(f"pol_score={sc:5.1f}  trades={len(trades):>5}  buys={buys:>5}  sells={sells:>5}")
        except Exception as e:
            print(f"ERROR: {e}")

    if not scored:
        print("ERROR: no data retrieved.", file=sys.stderr)
        sys.exit(1)

    scored.sort(key=lambda x: x[0], reverse=True)

    # ── 1. Actionable signal table (the money output) ──
    signals = generate_signals(results)
    print_signals(signals, top_n=15)

    # ── 2. Schedule for the top-scoring politician ──
    best_score, best_pol, best_trades = scored[0]
    print(f"Top-scored politician: {best_pol['name']} (score={best_score:.1f})\n")
    print_schedule(best_pol, best_trades, max_trades=30)

    # ── 3. Leaderboard ──
    print("Politician leaderboard:")
    print(f"  {'Rank':<5} {'Name':<28} {'Score':>6}  {'Last Trade':<12} {'Volume':<10}  Buys/Total")
    print(f"  {'────':<5} {'────────────────────────────':<28} {'─────':>6}  {'──────────':<12} {'──────':<10}  ──────────")
    for rank, (sc, p, trades) in enumerate(scored, 1):
        buys = sum(1 for t in trades if t["tx_type"] == "buy")
        vol = p['volume_raw'].strip()
        print(f"  {rank:<5} {p['name']:<28} {sc:>6.1f}  {p['last_trade']:<12} ${vol}M{'':>{max(0,9-len(vol))}}  {buys}/{len(trades)}")
    print()


if __name__ == "__main__":
    main()
