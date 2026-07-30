#!/usr/bin/env python3
"""
Market Monitor — data fetcher.

Pulls free public data and writes JSON files into ./data/ for the static
dashboard (index.html) to read. Designed to run in GitHub Actions on a
schedule. Every fetcher fails gracefully: if a source is down, the previous
JSON for that block is kept and marked stale rather than crashing the run.

Sources:
  - FRED (St. Louis Fed)        — needs FRED_API_KEY env var (free key)
  - Bank of England IADB        — no key
  - BIS policy-rate API         — no key
  - iShares MBB holdings CSV    — no key
"""

import csv
import io
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (personal market dashboard; github actions)"}

def log(msg):
    print(f"[fetch] {msg}", flush=True)

def http_get(url, timeout=60, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def load_previous(name):
    try:
        with open(os.path.join(DATA_DIR, name), "r") as f:
            return json.load(f)
    except Exception:
        return None

def _clean(obj):
    """Recursively replace NaN/Infinity (invalid JSON) with None."""
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj

def save(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, name), "w") as f:
        json.dump(_clean(obj), f, indent=1, allow_nan=False)
    log(f"wrote {name}")

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ──────────────────────────────────────────────────────────────────────
# FRED
# ──────────────────────────────────────────────────────────────────────
def fred_series(series_id, limit=130):
    """Return list of [date, value] (oldest→newest), skipping missing obs."""
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY not set")
    params = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": FRED_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    })
    url = f"https://api.stlouisfed.org/fred/series/observations?{params}"
    data = json.loads(http_get(url))
    out = []
    for obs in data.get("observations", []):
        v = obs.get("value", ".")
        if v not in (".", "", None):
            out.append([obs["date"], float(v)])
    out.reverse()
    return out

FRED_SERIES = {
    # id, label, group, units
    "DFEDTARU":        ("Fed Funds (upper target)", "policy", "%"),
    "ECBDFR":          ("ECB Deposit Facility",     "policy", "%"),
    "SOFR":            ("SOFR",                     "money",  "%"),
    "DGS2":            ("US 2Y Treasury",           "curve",  "%"),
    "DGS10":           ("US 10Y Treasury",          "curve",  "%"),
    "DGS30":           ("US 30Y Treasury",          "curve",  "%"),
    "IRLTLT01GBM156N": ("UK 10Y Gilt (monthly)",    "curve",  "%"),
    "BAMLH0A0HYM2":    ("US High Yield OAS",        "credit", "bp"),
    "BAMLC0A0CM":      ("US Investment Grade OAS",  "credit", "bp"),
    "BAMLH0A3HYC":     ("US CCC & Lower OAS",       "credit", "bp"),
    "BAMLHE00EHYIOAS": ("Euro High Yield OAS",      "credit", "bp"),
    "MORTGAGE30US":    ("US 30Y Mortgage Rate",     "mbs",    "%"),
}

def fetch_fred_block():
    out = {}
    for sid, (label, group, units) in FRED_SERIES.items():
        try:
            hist = fred_series(sid)
            if not hist:
                raise RuntimeError("empty series")
            scale = 100.0 if units == "bp" else 1.0
            hist = [[d, round(v * scale, 2)] for d, v in hist]
            out[sid] = {
                "label": label, "group": group, "units": units,
                "value": hist[-1][1], "asOf": hist[-1][0],
                "history": hist[-90:], "status": "live",
            }
            log(f"FRED {sid}: {hist[-1][1]}{units} as of {hist[-1][0]}")
        except Exception as e:
            log(f"FRED {sid} FAILED: {e}")
            out[sid] = None
    return out

# ──────────────────────────────────────────────────────────────────────
# Bank of England IADB (Bank Rate + SONIA)
# ──────────────────────────────────────────────────────────────────────
BOE_CODES = {"IUDBEDR": "BoE Bank Rate", "IUDSOIA": "SONIA (overnight)"}

def fetch_boe():
    date_from = (datetime.now() - timedelta(days=400)).strftime("%d/%b/%Y")
    date_to = "now"
    params = (
        f"csv.x=yes&Datefrom={date_from}&Dateto={date_to}"
        f"&SeriesCodes={','.join(BOE_CODES)}&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"
    )
    url = f"https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?{params}"
    text = http_get(url)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise RuntimeError("empty CSV")
    header = [h.strip().upper() for h in rows[0]]
    series = {code: [] for code in BOE_CODES}
    col_for = {}
    for code in BOE_CODES:
        for i, h in enumerate(header):
            if code in h:
                col_for[code] = i
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        date_raw = row[0].strip()
        try:
            d = datetime.strptime(date_raw, "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            try:
                d = datetime.strptime(date_raw, "%d %b %y").strftime("%Y-%m-%d")
            except ValueError:
                continue
        for code, i in col_for.items():
            if i < len(row) and row[i].strip():
                try:
                    series[code].append([d, float(row[i])])
                except ValueError:
                    pass
    out = {}
    for code, label in BOE_CODES.items():
        hist = sorted(series.get(code, []))
        if hist:
            out[code] = {
                "label": label, "units": "%",
                "value": hist[-1][1], "asOf": hist[-1][0],
                "history": hist[-90:], "status": "live",
            }
            log(f"BoE {code}: {hist[-1][1]}% as of {hist[-1][0]}")
        else:
            out[code] = None
            log(f"BoE {code}: no data parsed")
    return out

# ──────────────────────────────────────────────────────────────────────
# BIS central bank policy rates (global)
# ──────────────────────────────────────────────────────────────────────
BIS_AREAS = {
    "JP": "Bank of Japan", "CH": "Swiss National Bank", "CA": "Bank of Canada",
    "AU": "Reserve Bank of Australia", "SE": "Riksbank", "NO": "Norges Bank",
    "BR": "Central Bank of Brazil", "IN": "Reserve Bank of India",
    "KR": "Bank of Korea", "MX": "Banco de México", "NZ": "RBNZ",
    "ZA": "South African Reserve Bank",
}

def bis_parse_csv(text):
    """Pull (TIME_PERIOD, OBS_VALUE) pairs out of a BIS SDMX CSV."""
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return []
    header = [h.strip().upper() for h in rows[0]]
    try:
        ti = header.index("TIME_PERIOD")
        vi = header.index("OBS_VALUE")
    except ValueError:
        return []
    out = []
    for row in rows[1:]:
        if len(row) > max(ti, vi) and row[ti] and row[vi]:
            try:
                val = float(row[vi])
            except ValueError:
                continue
            if not math.isfinite(val):  # skip NaN / inf
                continue
            out.append([row[ti][:10], val])
    return sorted(out)

def fetch_bis():
    out = {}
    start = (datetime.now() - timedelta(days=800)).strftime("%Y-%m-%d")
    for area, name in BIS_AREAS.items():
        hist = []
        # Try the current data.bis.org API first, then the legacy host.
        candidates = [
            f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.{area}"
            f"?startPeriod={start}&format=csv",
            f"https://stats.bis.org/api/v1/data/WS_CBPOL/D.{area}/all"
            f"?startPeriod={start}&format=csv",
        ]
        for url in candidates:
            try:
                text = http_get(url, headers={"Accept": "text/csv"})
                hist = bis_parse_csv(text)
                if hist:
                    break
            except Exception as e:
                log(f"BIS {area} via {url.split('/api/')[1][:6]}…: {e}")
        if hist:
            # thin the daily history to ~90 points
            step = max(1, len(hist) // 90)
            thin = hist[::step]
            if thin[-1] != hist[-1]:
                thin.append(hist[-1])
            out[area] = {
                "label": name, "units": "%",
                "value": hist[-1][1], "asOf": hist[-1][0],
                "history": thin, "status": "live",
            }
            log(f"BIS {area}: {hist[-1][1]}% as of {hist[-1][0]}")
        else:
            out[area] = None
            log(f"BIS {area}: FAILED (all endpoints)")
    return out

# ──────────────────────────────────────────────────────────────────────
# Yahoo Finance chart API — equity indices, FX, commodities, crypto
# ──────────────────────────────────────────────────────────────────────
YF_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

MARKET_SYMBOLS = {
    # key: (yahoo symbol, label, group, decimals)
    "SPX":    ("^GSPC",     "S&P 500",            "indices", 0),
    "DJI":    ("^DJI",      "Dow Jones Ind. Avg.", "indices", 0),
    "NDX":    ("^IXIC",     "Nasdaq Composite",    "indices", 0),
    "FTSE":   ("^FTSE",     "FTSE 100",            "indices", 0),
    "DAX":    ("^GDAXI",    "DAX",                 "indices", 0),
    "N225":   ("^N225",     "Nikkei 225",          "indices", 0),
    "VIX":    ("^VIX",      "CBOE Volatility (VIX)", "indices", 2),
    "EURUSD": ("EURUSD=X",  "EUR/USD",             "fx", 4),
    "GBPUSD": ("GBPUSD=X",  "GBP/USD",             "fx", 4),
    "USDJPY": ("USDJPY=X",  "USD/JPY",             "fx", 2),
    "DXY":    ("DX-Y.NYB",  "US Dollar Index",     "fx", 2),
    "GOLD":   ("GC=F",      "Gold (front-month)",  "commodities", 2),
    "SILVER": ("SI=F",      "Silver (front-month)", "commodities", 3),
    "WTI":    ("CL=F",      "WTI Crude",           "commodities", 2),
    "BRENT":  ("BZ=F",      "Brent Crude",         "commodities", 2),
    "COPPER": ("HG=F",      "Copper (front-month)", "commodities", 3),
    "BTC":    ("BTC-USD",   "Bitcoin",             "crypto", 0),
    "ETH":    ("ETH-USD",   "Ethereum",            "crypto", 2),
    # cross-sector single-name watchlist
    "AAPL":   ("AAPL",  "Apple",           "watchlist", 2),
    "MSFT":   ("MSFT",  "Microsoft",       "watchlist", 2),
    "NVDA":   ("NVDA",  "Nvidia",          "watchlist", 2),
    "GOOGL":  ("GOOGL", "Alphabet",        "watchlist", 2),
    "AMZN":   ("AMZN",  "Amazon",          "watchlist", 2),
    "META":   ("META",  "Meta Platforms",  "watchlist", 2),
    "TSLA":   ("TSLA",  "Tesla",           "watchlist", 2),
    "JPM":    ("JPM",   "JPMorgan Chase",  "watchlist", 2),
    "XOM":    ("XOM",   "Exxon Mobil",     "watchlist", 2),
    "UNH":    ("UNH",   "UnitedHealth",    "watchlist", 2),
    "JNJ":    ("JNJ",   "Johnson & Johnson", "watchlist", 2),
    "PG":     ("PG",    "Procter & Gamble", "watchlist", 2),
    "HD":     ("HD",    "Home Depot",      "watchlist", 2),
    "V":      ("V",     "Visa",            "watchlist", 2),
    "WMT":    ("WMT",   "Walmart",         "watchlist", 2),
    "DIS":    ("DIS",   "Walt Disney",     "watchlist", 2),
    # S&P sector SPDRs
    "XLK": ("XLK", "Technology",             "sectors", 2),
    "XLF": ("XLF", "Financials",             "sectors", 2),
    "XLE": ("XLE", "Energy",                 "sectors", 2),
    "XLV": ("XLV", "Health Care",            "sectors", 2),
    "XLY": ("XLY", "Consumer Discretionary", "sectors", 2),
    "XLP": ("XLP", "Consumer Staples",       "sectors", 2),
    "XLI": ("XLI", "Industrials",            "sectors", 2),
    "XLB": ("XLB", "Materials",              "sectors", 2),
    "XLU": ("XLU", "Utilities",              "sectors", 2),
    "XLRE": ("XLRE", "Real Estate",          "sectors", 2),
    "XLC": ("XLC", "Communication Services", "sectors", 2),
}

def yahoo_series(symbol, rng="1y"):
    """Return list of [date, close] (oldest→newest) from Yahoo's chart API."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval=1d")
    data = json.loads(http_get(url, headers=YF_UA))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError("empty chart result")
    res = result[0]
    ts = res.get("timestamp") or []
    closes = (res.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
        out.append([d, float(c)])
    return out

def fetch_markets_block():
    out = {}
    for key, (symbol, label, group, decimals) in MARKET_SYMBOLS.items():
        try:
            hist = yahoo_series(symbol)
            if not hist:
                raise RuntimeError("empty series")
            hist = [[d, round(v, decimals)] for d, v in hist]
            out[key] = {
                "label": label, "group": group, "decimals": decimals,
                "value": hist[-1][1], "asOf": hist[-1][0],
                "history": hist[-130:], "status": "live",
            }
            log(f"YF {key}: {hist[-1][1]} as of {hist[-1][0]}")
        except Exception as e:
            log(f"YF {key} FAILED: {e}")
            out[key] = None
    return out

def pct_chg(entry):
    """1-day % change from an instrument's history, or None."""
    if not entry or not entry.get("history"):
        return None
    hist = [p for p in entry["history"] if p[1] is not None]
    if len(hist) < 2:
        return None
    prev, last = hist[-2][1], hist[-1][1]
    if not prev:
        return None
    return (last - prev) / prev * 100

def build_pulse(markets, rates):
    """Deterministic, data-driven narrative — no LLM, just real numbers."""
    movers = []
    for grp in ("indices", "watchlist", "sectors", "commodities", "crypto", "fx"):
        for key, entry in (markets.get(grp) or {}).items():
            if not entry:
                continue
            p = pct_chg(entry)
            if p is not None:
                movers.append((abs(p), p, key, entry.get("label", key), grp))
    bullets = []

    spx = pct_chg((markets.get("indices") or {}).get("SPX"))
    vix = (markets.get("indices") or {}).get("VIX")
    vix_val = vix.get("value") if vix else None
    if spx is not None:
        tone = "advancing" if spx > 0.15 else "retreating" if spx < -0.15 else "roughly flat"
        headline = f"US equities {tone}, S&P 500 {spx:+.2f}%"
        if vix_val is not None:
            regime = ("calm" if vix_val < 15 else "normal" if vix_val < 20
                      else "elevated" if vix_val < 28 else "stressed")
            headline += f" · VIX {vix_val:.1f} ({regime})"
        bullets.append(headline + ".")
    else:
        headline = "Waiting on the first successful equities pull."

    sectors = [(pct_chg(e), e.get("label", k)) for k, e in (markets.get("sectors") or {}).items() if e]
    sectors = [(p, l) for p, l in sectors if p is not None]
    if sectors:
        sectors.sort()
        worst, best = sectors[0], sectors[-1]
        bullets.append(f"Sector leader: {best[1]} {best[0]:+.2f}% · Laggard: {worst[1]} {worst[0]:+.2f}%.")

    non_vol_movers = [m for m in movers if m[2] != "VIX"]
    if non_vol_movers:
        non_vol_movers.sort(reverse=True)
        _, p, key, label, grp = non_vol_movers[0]
        name = label if f"({key})" in label else f"{label} ({key})"
        bullets.append(f"Biggest mover: {name} {p:+.2f}% on the day.")

    t2 = ((rates.get("us") or {}).get("t2y") or {}).get("value")
    t10 = ((rates.get("us") or {}).get("t10y") or {}).get("value")
    if t2 is not None and t10 is not None:
        spread = (t10 - t2) * 100
        state = "inverted — classic late-cycle recession signal" if spread < 0 else "positively sloped"
        bullets.append(f"US 2s10s curve is {state} at {spread:+.0f}bp.")

    return {"headline": headline, "bullets": bullets}

# ──────────────────────────────────────────────────────────────────────
# News headlines — CNBC markets RSS, Yahoo Finance RSS fallback
# ──────────────────────────────────────────────────────────────────────
import html as _html_mod

def parse_rss(xml_text, source, limit=12):
    items = re.findall(r"<item>(.*?)</item>", xml_text, re.S)
    out = []
    for it in items[:limit]:
        t = re.search(r"<title>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</title>", it, re.S)
        l = re.search(r"<link>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</link>", it, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        if not t:
            continue
        title = _html_mod.unescape((t.group(1) or t.group(2) or "").strip())
        link = _html_mod.unescape((l.group(1) or l.group(2) or "").strip()) if l else ""
        pub_date = (p.group(1).strip() if p else "")
        iso = None
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                iso = datetime.strptime(pub_date, fmt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                break
            except ValueError:
                continue
        if title:
            out.append({"title": title, "link": link, "published": iso, "source": source})
    return out

NEWS_FEEDS = [
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC Markets"),
    ("https://finance.yahoo.com/news/rssindex", "Yahoo Finance"),
]

def fetch_headlines():
    for url, source in NEWS_FEEDS:
        try:
            text = http_get(url, headers=YF_UA)
            items = parse_rss(text, source)
            if items:
                log(f"headlines: {len(items)} from {source}")
                return items
        except Exception as e:
            log(f"headlines {source} FAILED: {e}")
    return []

# ──────────────────────────────────────────────────────────────────────
# iShares MBB holdings → coupon stack
# ──────────────────────────────────────────────────────────────────────
MBB_URL = ("https://www.ishares.com/us/products/239465/ishares-mbs-etf/"
           "1467271812596.ajax?fileType=csv&fileName=MBB_holdings&dataType=fund")

def fetch_mbb():
    text = http_get(MBB_URL)
    text = text.lstrip("\ufeff")  # strip BOM if present
    if "<html" in text[:500].lower() or "<!doctype" in text[:500].lower():
        raise RuntimeError("iShares returned an HTML page, not CSV (rate-limited or URL changed)")
    lines = text.splitlines()
    # The file has metadata lines first; find the real header row.
    # Match on Name + (Weight | Market Value); scan generously in case the
    # preamble grows.
    header_idx = None
    for i, line in enumerate(lines[:80]):
        u = line.upper()
        if "NAME" in u and ("WEIGHT" in u or "MARKET VALUE" in u):
            header_idx = i
            break
    if header_idx is None:
        preview = " | ".join(l[:60] for l in lines[:8])
        raise RuntimeError(f"could not locate header row; first lines: {preview}")
    as_of = ""
    for line in lines[:header_idx]:
        m = re.search(r"as of[,\s]+\"?([A-Za-z]+ \d{1,2},? \d{4})", line, re.I)
        if m:
            as_of = m.group(1).strip().strip('"')
            break
    rows = list(csv.reader(io.StringIO("\n".join(lines[header_idx:]))))
    header = [h.strip().lower() for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(header):
                if n in h:
                    return i
        return None

    c_name = col("name")
    c_weight = col("weight")
    c_price = col("price")
    c_coupon = col("coupon")
    c_class = col("asset class")
    c_sector = col("sector")
    if c_name is None or c_weight is None:
        raise RuntimeError(f"unexpected holdings columns: {header}")

    def fnum(s):
        s = (s or "").replace(",", "").replace("%", "").strip()
        try:
            return float(s)
        except ValueError:
            return None

    buckets = {}  # (agency, coupon) -> {weight, px_weighted, n}
    total_rows = 0
    for row in rows[1:]:
        if len(row) <= c_weight or not row[c_name].strip():
            continue
        name = row[c_name].strip()
        un = name.upper()
        # Skip cash / collateral rows two ways: asset-class column and name.
        if c_class is not None and len(row) > c_class and "cash" in row[c_class].lower():
            continue
        if "CASH" in un or "BLACKROCK" in un:
            continue
        w = fnum(row[c_weight])
        if w is None or w <= 0:
            continue
        # Coupon: prefer a real coupon column; otherwise pull it out of the
        # security name, e.g. "FNMA 30YR UMBS - 6.0 2054-12-01" -> 6.0,
        # or a trailing "5.5%".
        coupon = fnum(row[c_coupon]) if c_coupon is not None and len(row) > c_coupon else None
        if coupon is None:
            m = re.search(r"[-\s](\d+(?:\.\d+)?)\s+\d{4}-\d{2}-\d{2}", name)
            if not m:
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", name)
            if m:
                coupon = float(m.group(1))
        if coupon is None:
            continue
        if "GNMA" in un or "GINNIE" in un or un.startswith("G2"):
            agency = "GNMA"
        elif "FHLMC" in un or "FGLMC" in un or "FREDDIE" in un:
            agency = "FHLMC"
        elif "FNMA" in un or "FANNIE" in un or "UMBS" in un:
            agency = "FNMA"
        else:
            agency = "OTHER"
        cpn = round(coupon * 2) / 2  # bucket to nearest 0.5
        key = (agency, cpn)
        b = buckets.setdefault(key, {"weight": 0.0, "pxw": 0.0, "pw": 0.0, "n": 0})
        b["weight"] += w
        b["n"] += 1
        px = fnum(row[c_price]) if c_price is not None and len(row) > c_price else None
        if px:
            b["pxw"] += px * w
            b["pw"] += w
        total_rows += 1

    stack = []
    for (agency, cpn), b in buckets.items():
        stack.append({
            "agency": agency,
            "coupon": cpn,
            "weight": round(b["weight"], 2),
            "avgPrice": round(b["pxw"] / b["pw"], 2) if b["pw"] else None,
            "holdings": b["n"],
        })
    stack.sort(key=lambda x: -x["weight"])
    log(f"MBB: {total_rows} holdings rows → {len(stack)} coupon buckets")
    return {
        "fund": "MBB — iShares MBS ETF",
        "asOf": as_of,
        "holdingsCount": total_rows,
        "stack": stack[:18],
        "status": "live",
        "source": "iShares daily holdings CSV",
    }

# ──────────────────────────────────────────────────────────────────────
# Assemble output files
# ──────────────────────────────────────────────────────────────────────
def keep_or(new, old):
    """Use new value; if fetch failed, keep old one flagged stale."""
    if new is not None:
        return new
    if old is not None:
        old = dict(old)
        old["status"] = "stale"
        return old
    return None

def main():
    updated = now_iso()
    prev_rates = load_previous("rates.json") or {}
    prev_credit = load_previous("credit.json") or {}
    prev_mbs = load_previous("mbs.json") or {}
    prev_markets = load_previous("markets.json") or {}

    fred = fetch_fred_block() if FRED_KEY else {}
    if not FRED_KEY:
        log("WARNING: FRED_API_KEY not set — skipping all FRED series")

    try:
        boe = fetch_boe()
    except Exception as e:
        log(f"BoE block FAILED: {e}")
        boe = {k: None for k in BOE_CODES}

    bis = fetch_bis()

    prev_banks = (prev_rates.get("banks") or {})
    banks = {}
    banks["US"] = keep_or(fred.get("DFEDTARU"), prev_banks.get("US"))
    if banks["US"]:
        banks["US"]["label"] = "Federal Reserve (upper target)"
    banks["GB"] = keep_or(boe.get("IUDBEDR"), prev_banks.get("GB"))
    if banks["GB"]:
        banks["GB"]["label"] = "Bank of England"
    banks["EU"] = keep_or(fred.get("ECBDFR"), prev_banks.get("EU"))
    if banks["EU"]:
        banks["EU"]["label"] = "ECB (deposit facility)"
    for area in BIS_AREAS:
        banks[area] = keep_or(bis.get(area), prev_banks.get(area))

    prev_named = lambda blk, k: (prev_rates.get(blk) or {}).get(k)
    rates = {
        "updated": updated,
        "banks": banks,
        "uk": {
            "sonia": keep_or(boe.get("IUDSOIA"), prev_named("uk", "sonia")),
            "gilt10y": keep_or(fred.get("IRLTLT01GBM156N"), prev_named("uk", "gilt10y")),
        },
        "us": {
            "sofr": keep_or(fred.get("SOFR"), prev_named("us", "sofr")),
            "t2y": keep_or(fred.get("DGS2"), prev_named("us", "t2y")),
            "t10y": keep_or(fred.get("DGS10"), prev_named("us", "t10y")),
            "t30y": keep_or(fred.get("DGS30"), prev_named("us", "t30y")),
        },
    }
    save("rates.json", rates)

    pc = prev_credit.get("spreads") or {}
    credit = {
        "updated": updated,
        "spreads": {
            "hy": keep_or(fred.get("BAMLH0A0HYM2"), pc.get("hy")),
            "ig": keep_or(fred.get("BAMLC0A0CM"), pc.get("ig")),
            "ccc": keep_or(fred.get("BAMLH0A3HYC"), pc.get("ccc")),
            "eurhy": keep_or(fred.get("BAMLHE00EHYIOAS"), pc.get("eurhy")),
        },
    }
    save("credit.json", credit)

    try:
        mbb = fetch_mbb()
    except Exception as e:
        log(f"MBB FAILED: {e}")
        mbb = None
    mbs = {
        "updated": updated,
        "fund": keep_or(mbb, prev_mbs.get("fund")),
        "mortgage30": keep_or(fred.get("MORTGAGE30US"),
                              prev_mbs.get("mortgage30")),
    }
    save("mbs.json", mbs)

    yf = fetch_markets_block()
    MARKET_GROUPS = ("indices", "fx", "commodities", "crypto", "watchlist", "sectors")
    prev_flat = {}
    for grp in MARKET_GROUPS:
        for k, v in (prev_markets.get(grp) or {}).items():
            prev_flat[k] = v
    markets = {"updated": updated, **{g: {} for g in MARKET_GROUPS}}
    for key, (_, _, group, _) in MARKET_SYMBOLS.items():
        markets[group][key] = keep_or(yf.get(key), prev_flat.get(key))
    markets["pulse"] = build_pulse(markets, rates)
    save("markets.json", markets)

    try:
        headlines = fetch_headlines()
    except Exception as e:
        log(f"headlines FAILED: {e}")
        headlines = []
    prev_headlines = load_previous("headlines.json") or {}
    save("headlines.json", {
        "updated": updated,
        "items": headlines or prev_headlines.get("items", []),
        "status": "live" if headlines else ("stale" if prev_headlines.get("items") else "empty"),
    })

    save("meta.json", {"updated": updated, "generator": "fetch_data.py"})

    # Fail the workflow loudly only if literally everything failed.
    have_any = any(v for v in banks.values()) or any(
        v for v in credit["spreads"].values()) or mbs["fund"] or any(
        v for grp in markets.values() if isinstance(grp, dict) for v in grp.values())
    if not have_any:
        log("FATAL: every source failed")
        sys.exit(1)
    log("done")

if __name__ == "__main__":
    main()
