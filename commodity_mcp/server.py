"""
Commodity MCP Server
Provides real-time commodity prices scraped from Trading Economics + Yahoo Finance (BTC).

Commodities: Gold, BTC, Oil (WTI), Palm Oil, Sugar, Rubber, Coal,
             BDI (Baltic Dry Index), World Container Index, Containerized Freight Index
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
        "Get real-time commodity prices and shipping rates. "
        "Commodities: Gold, BTC, Oil (WTI), Palm Oil, Sugar, Rubber, Coal. "
        "Shipping: BDI (Baltic Dry Index), World Container Index (WCI), Containerized Freight Index (CCFI). "
        "Data sourced from Trading Economics (scraping) and Yahoo Finance (BTC). "
        "Tools: get_commodity_prices (all), get_commodity_price (single by key)."
    ),
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COMMODITY_ORDER = [
    "gold", "btc", "oil", "palmoil", "sugar", "rubber", "coal",
    "bdi", "wci", "ccfi",
]

# Commodities scraped from tradingeconomics.com/commodities (one request)
TE_COMMODITIES = {
    "gold":    {"keyword": "Gold",                    "exact_start": True,  "name": "ทอง (Gold)"},
    "oil":     {"keyword": "Crude Oil",               "exact_start": True,  "name": "น้ำมัน WTI (Crude Oil)"},
    "sugar":   {"keyword": "Sugar",                   "exact_start": True,  "name": "น้ำตาล (Sugar)"},
    "coal":    {"keyword": "Coal",                    "exact_start": True,  "name": "ถ่านหิน (Coal)"},
    "palmoil": {"keyword": "Palm Oil",                "exact_start": False, "name": "น้ำมันปาล์ม (Palm Oil)"},
    "rubber":  {"keyword": "Rubber",                  "exact_start": True,  "name": "ยางพารา (Rubber)"},
    "wci":     {"keyword": "World Container Index",   "exact_start": True,  "name": "ค่าระวาง Container WCI",  "unit": "USD"},
    "ccfi":    {"keyword": "Containerized Freight",   "exact_start": True,  "name": "ดัชนีระวางเรือ CCFI",    "unit": "Points"},
}

TE_URL = "https://tradingeconomics.com/commodities"
TE_BDI_URL = "https://tradingeconomics.com/commodity/baltic"
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


def _parse_pct_str(s: str) -> Optional[float]:
    try:
        return float(s.replace("%", "").replace("+", "").replace(",", ""))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _fetch_te() -> dict[str, dict]:
    """Scrape tradingeconomics.com/commodities for all TE_COMMODITIES in one request."""
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
                        "unit": cfg.get("unit") or _parse_unit(name_cell),
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


def _fetch_bdi() -> dict:
    """Scrape BDI (Baltic Dry Index) from tradingeconomics.com/commodity/baltic."""
    if _is_fresh("bdi"):
        return _cache["bdi"]["data"]

    try:
        r = requests.get(TE_BDI_URL, headers=TE_HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # ราคาจาก JSON ใน script tag
        price = None
        for script in soup.find_all("script"):
            text = script.string or ""
            m = re.search(r'"last"\s*:\s*([0-9.]+)', text)
            if m and "Baltic" in text:
                price = float(m.group(1))
                break

        # day% / month% จาก stats summary table ใน page text
        page_text = soup.get_text()
        lines = [l.strip() for l in page_text.splitlines() if l.strip()]

        day_pct = month_pct = date_str = None
        for i, line in enumerate(lines):
            # หาบรรทัดที่มีตัวเลขราคา BDI แล้วดู context
            if price and str(int(price)) in line.replace(",", "") and len(line) < 20:
                # รูปแบบ: price / day_abs / day% / month% / year% / date
                candidates = lines[i:i+6]
                pct_vals = []
                for c in candidates:
                    if "%" in c:
                        pct_vals.append(_parse_pct_str(c))
                if len(pct_vals) >= 1:
                    day_pct = pct_vals[0]
                if len(pct_vals) >= 2:
                    month_pct = pct_vals[1]
                break

        # fallback: ดึงจาก description text เช่น "down 1.63% from the previous day"
        if day_pct is None:
            m = re.search(r'(up|down)\s+([\d.]+)%\s+from the previous day', page_text, re.I)
            if m:
                sign = -1 if m.group(1).lower() == "down" else 1
                day_pct = sign * float(m.group(2))

        # หา date
        m_date = re.search(r'on\s+([A-Z][a-z]+ \d+, \d{4})', page_text)
        if m_date:
            try:
                dt = datetime.strptime(m_date.group(1), "%B %d, %Y")
                date_str = dt.strftime("%b/%d")
            except ValueError:
                date_str = m_date.group(1)

        data = {
            "key": "bdi",
            "name": "ค่าระวางเรือ BDI (Baltic Dry Index)",
            "price": price,
            "change_day_pct": day_pct,
            "change_week_pct": None,  # TE individual page ไม่มี week%
            "change_month_pct": month_pct,
            "unit": "Index Points",
            "date": date_str,
            "source": "Trading Economics",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    except Exception as e:
        data = {
            "key": "bdi",
            "name": "ค่าระวางเรือ BDI (Baltic Dry Index)",
            "error": str(e),
            "source": "Trading Economics",
        }

    _cache["bdi"] = {"ts": time.time(), "data": data}
    return data


def _fetch_btc() -> dict:
    """Get BTC price from Yahoo Finance (not available on TE commodities page)."""
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
        if key == "btc":
            result.append(_fetch_btc())
        elif key == "bdi":
            result.append(_fetch_bdi())
        else:
            result.append(te.get(key, {"key": key, "error": "not found"}))
    return result


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_commodity_prices() -> str:
    """Get real-time prices for all commodities and shipping rates.

    Commodities: Gold, BTC, Oil (WTI), Palm Oil, Sugar, Rubber, Coal
    Shipping: BDI (Baltic Dry Index), WCI (World Container Index), CCFI (Containerized Freight Index)

    Returns price, day%, week%, month% change for each item.
    Cache TTL: 5 minutes.
    """
    items = _get_all()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Commodity & Shipping Prices  [{now}]", "=" * 55]

    sections = {
        "Commodities": ["gold", "btc", "oil", "palmoil", "sugar", "rubber", "coal"],
        "Shipping Rates": ["bdi", "wci", "ccfi"],
    }

    item_map = {d["key"]: d for d in items if "key" in d}

    def fmt(v):
        if v is None:
            return "  N/A "
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.1f}%"

    for section, keys in sections.items():
        lines.append(f"\n[{section}]")
        for key in keys:
            d = item_map.get(key, {"key": key, "error": "not found", "name": key})
            if "error" in d:
                lines.append(f"  {d.get('name', key)}: ERROR")
                continue
            lines.append(
                f"  {d['name']}\n"
                f"    {d['price']:,} {d.get('unit','')}  ({d.get('date','')})\n"
                f"    Day:{fmt(d.get('change_day_pct'))}  "
                f"Week:{fmt(d.get('change_week_pct'))}  "
                f"Month:{fmt(d.get('change_month_pct'))}"
            )

    return "\n".join(lines)


@mcp.tool()
def get_commodity_price(key: str) -> str:
    """Get real-time price for a single commodity or shipping index.

    Args:
        key: One of: gold, btc, oil, palmoil, sugar, rubber, coal, bdi, wci, ccfi

    Returns price, day%, week%, month% change with source and timestamp.
    """
    key = key.lower().strip()

    if key == "btc":
        d = _fetch_btc()
    elif key == "bdi":
        d = _fetch_bdi()
    elif key in TE_COMMODITIES:
        d = _fetch_te().get(key, {"key": key, "error": "not found"})
    else:
        return f"Unknown key '{key}'. Valid: {', '.join(COMMODITY_ORDER)}"

    if "error" in d:
        return f"ERROR {d.get('name', key)}: {d['error']}"

    def fmt(v):
        if v is None:
            return "N/A"
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2f}%"

    return (
        f"{d['name']}\n"
        f"Price:  {d['price']:,} {d.get('unit','')}\n"
        f"Date:   {d.get('date','')}\n"
        f"Day:    {fmt(d.get('change_day_pct'))}\n"
        f"Week:   {fmt(d.get('change_week_pct'))}\n"
        f"Month:  {fmt(d.get('change_month_pct'))}\n"
        f"Source: {d.get('source','')}  |  {d.get('updated_at','')}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
