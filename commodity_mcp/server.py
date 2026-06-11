"""
Commodity MCP Server
Provides real-time commodity prices scraped from Trading Economics + Yahoo Finance (BTC).

Commodities: Gold, BTC, Oil (WTI), Palm Oil, Sugar, Rubber, Coal
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Optional

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="Commodity Prices",
    instructions=(
        "Get real-time commodity prices: Gold, BTC, Oil (WTI), Palm Oil, Sugar, Rubber, Coal. "
        "Data sourced from Trading Economics (scraping) and Yahoo Finance (BTC). "
        "Tools: get_commodity_prices (all), get_commodity_price (single by key)."
    ),
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COMMODITY_ORDER = ["gold", "btc", "oil", "palmoil", "sugar", "rubber", "coal"]

TE_COMMODITIES = {
    "gold":    {"keyword": "Gold",      "exact_start": True,  "name": "ทอง (Gold)"},
    "oil":     {"keyword": "Crude Oil", "exact_start": True,  "name": "น้ำมัน WTI (Crude Oil)"},
    "sugar":   {"keyword": "Sugar",     "exact_start": True,  "name": "น้ำตาล (Sugar)"},
    "coal":    {"keyword": "Coal",      "exact_start": True,  "name": "ถ่านหิน (Coal)"},
    "palmoil": {"keyword": "Palm Oil",  "exact_start": False, "name": "น้ำมันปาล์ม (Palm Oil)"},
    "rubber":  {"keyword": "Rubber",    "exact_start": True,  "name": "ยางพารา (Rubber)"},
}

TE_URL = "https://tradingeconomics.com/commodities"
TE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
CACHE_TTL = 300  # 5 minutes

_cache: dict = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_fresh(key: str) -> bool:
    entry = _cache.get(key)
    return entry is not None and time.time() - entry["ts"] < CACHE_TTL


def _parse_unit(text: str) -> str:
    m = re.search(r"(USD\s+Cents\s*/\s*\S+|[A-Z]{2,3}[a-z]?/\S+)", text)
    return m.group(0).strip() if m else ""


def _pct(cells: list[str], idx: int) -> Optional[float]:
    try:
        return float(cells[idx].replace("%", "").replace("+", "").replace(",", ""))
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _fetch_te() -> dict[str, dict]:
    if _is_fresh("te"):
        return _cache["te"]["data"]

    result: dict[str, dict] = {}
    try:
        r = requests.get(TE_URL, headers=TE_HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all(["th", "td"])]
                if len(cells) < 6:
                    continue
                name_cell = cells[0]
                name_lower = name_cell.lower()

                for key, cfg in TE_COMMODITIES.items():
                    if key in result:
                        continue
                    kw = cfg["keyword"].lower()
                    hit = name_lower.startswith(kw) if cfg["exact_start"] else kw in name_lower
                    if not hit:
                        continue

                    try:
                        price = float(cells[1].replace(",", ""))
                    except (ValueError, IndexError):
                        price = None

                    result[key] = {
                        "key": key,
                        "name": cfg["name"],
                        "price": price,
                        "change_day_pct": _pct(cells, 3),
                        "change_week_pct": _pct(cells, 4),
                        "change_month_pct": _pct(cells, 5),
                        "unit": _parse_unit(name_cell),
                        "date": cells[-1],
                        "source": "Trading Economics",
                        "updated_at": datetime.utcnow().isoformat() + "Z",
                    }

    except Exception as e:
        for key, cfg in TE_COMMODITIES.items():
            if key not in result:
                result[key] = {
                    "key": key, "name": cfg["name"],
                    "error": str(e), "source": "Trading Economics",
                }

    _cache["te"] = {"ts": time.time(), "data": result}
    return result


def _fetch_btc() -> dict:
    if _is_fresh("btc"):
        return _cache["btc"]["data"]

    try:
        t = yf.Ticker("BTC-USD")
        hist = t.history(period="1mo")
        if hist.empty:
            raise ValueError("No price data")

        price = round(float(hist["Close"].iloc[-1]), 2)

        def _chg(idx_back: int) -> Optional[float]:
            if len(hist) > idx_back:
                p = float(hist["Close"].iloc[-idx_back - 1])
                return round((price - p) / p * 100, 2) if p else None
            return None

        data = {
            "key": "btc",
            "name": "Bitcoin (BTC)",
            "price": price,
            "change_day_pct": _chg(1),
            "change_week_pct": _chg(5),
            "change_month_pct": round(
                (price - float(hist["Close"].iloc[0])) / float(hist["Close"].iloc[0]) * 100, 2
            ),
            "unit": "USD",
            "date": datetime.utcnow().strftime("%b/%d"),
            "source": "Yahoo Finance",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    except Exception as e:
        data = {"key": "btc", "name": "Bitcoin (BTC)", "error": str(e), "source": "Yahoo Finance"}

    _cache["btc"] = {"ts": time.time(), "data": data}
    return data


def _get_all() -> list[dict]:
    te = _fetch_te()
    result = []
    for key in COMMODITY_ORDER:
        result.append(_fetch_btc() if key == "btc" else te.get(key, {"key": key, "error": "not found"}))
    return result


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_commodity_prices() -> str:
    """Get real-time prices for all 7 commodities: Gold, BTC, Oil (WTI), Palm Oil, Sugar, Rubber, Coal.

    Returns price, day%, week%, and month% change for each commodity.
    Data sourced from Trading Economics (scrape) and Yahoo Finance (BTC).
    Cache TTL: 5 minutes.
    """
    items = _get_all()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"🌍 Commodity Prices  [{now}]", "─" * 55]

    for d in items:
        if "error" in d:
            lines.append(f"❌ {d['name']}: {d['error']}")
            continue

        def fmt(v):
            if v is None: return "  N/A "
            sign = "+" if v >= 0 else ""
            return f"{sign}{v:.1f}%"

        lines.append(
            f"{d['name']}\n"
            f"  ราคา: {d['price']:,} {d.get('unit','')}  ({d.get('date','')})\n"
            f"  Day:{fmt(d.get('change_day_pct'))}  "
            f"Week:{fmt(d.get('change_week_pct'))}  "
            f"Month:{fmt(d.get('change_month_pct'))}"
        )
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def get_commodity_price(key: str) -> str:
    """Get real-time price for a single commodity.

    Args:
        key: Commodity key — one of: gold, btc, oil, palmoil, sugar, rubber, coal

    Returns price, day%, week%, month% change with source and timestamp.
    """
    key = key.lower().strip()
    valid = COMMODITY_ORDER

    if key == "btc":
        d = _fetch_btc()
    elif key in TE_COMMODITIES:
        d = _fetch_te().get(key, {"key": key, "error": "not found"})
    else:
        return f"❌ ไม่รู้จัก key '{key}'\nใช้ได้: {', '.join(valid)}"

    if "error" in d:
        return f"❌ {d['name']}: {d['error']}"

    def fmt(v):
        if v is None: return "N/A"
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2f}%"

    return (
        f"📊 {d['name']}\n"
        f"ราคา: {d['price']:,} {d.get('unit','')}\n"
        f"วันที่: {d.get('date','')}\n"
        f"Day:   {fmt(d.get('change_day_pct'))}\n"
        f"Week:  {fmt(d.get('change_week_pct'))}\n"
        f"Month: {fmt(d.get('change_month_pct'))}\n"
        f"Source: {d.get('source','')} | {d.get('updated_at','')}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
