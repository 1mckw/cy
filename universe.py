"""Scan universe: Bybit USDT perpetual contracts — top 50 by 24h turnover."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BYBIT_HOSTS = (
    "https://api.bybit.com",
    "https://api.bytick.com",
)
TOP_N = 50
UA = {"User-Agent": "Mozilla/5.0 (compatible; Crypto-Alerts/1.0)"}
ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(ROOT, "signals", "universe-cache.json")

# Fallback when Bybit API is unreachable (e.g. GitHub Actions IP blocks).
FALLBACK_CONTRACTS: list[tuple[str, str]] = [
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("SOLUSDT", "SOL"),
    ("XRPUSDT", "XRP"),
    ("DOGEUSDT", "DOGE"),
    ("BNBUSDT", "BNB"),
    ("ADAUSDT", "ADA"),
    ("AVAXUSDT", "AVAX"),
    ("LINKUSDT", "LINK"),
    ("SUIUSDT", "SUI"),
    ("HYPEUSDT", "HYPE"),
    ("LTCUSDT", "LTC"),
    ("NEARUSDT", "NEAR"),
    ("APTUSDT", "APT"),
    ("ARBUSDT", "ARB"),
    ("OPUSDT", "OP"),
    ("INJUSDT", "INJ"),
    ("TIAUSDT", "TIA"),
    ("SEIUSDT", "SEI"),
    ("WLDUSDT", "WLD"),
    ("UNIUSDT", "UNI"),
    ("AAVEUSDT", "AAVE"),
    ("ENAUSDT", "ENA"),
    ("ONDOUSDT", "ONDO"),
    ("1000PEPEUSDT", "1000PEPE"),
    ("TRUMPUSDT", "TRUMP"),
    ("FARTCOINUSDT", "FARTCOIN"),
    ("WIFUSDT", "WIF"),
    ("TAOUSDT", "TAO"),
    ("CRVUSDT", "CRV"),
    ("DOTUSDT", "DOT"),
    ("FILUSDT", "FIL"),
    ("ETCUSDT", "ETC"),
    ("ATOMUSDT", "ATOM"),
    ("ICPUSDT", "ICP"),
    ("RENDERUSDT", "RENDER"),
    ("PENGUUSDT", "PENGU"),
    ("VIRTUALUSDT", "VIRTUAL"),
    ("TRXUSDT", "TRX"),
    ("XLMUSDT", "XLM"),
    ("BCHUSDT", "BCH"),
    ("TONUSDT", "TON"),
    ("POLUSDT", "POL"),
    ("SHIB1000USDT", "SHIB1000"),
    ("MNTUSDT", "MNT"),
    ("ZECUSDT", "ZEC"),
    ("ALGOUSDT", "ALGO"),
    ("STXUSDT", "STX"),
    ("JUPUSDT", "JUP"),
    ("PYTHUSDT", "PYTH"),
]

GROUP_ORDER = {"perp": 0}


def _http_get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _bybit_tickers_payload() -> dict:
    last_err: Exception | None = None
    for host in BYBIT_HOSTS:
        url = host + "/v5/market/tickers?category=linear"
        try:
            payload = _http_get_json(url)
            if int(payload.get("retCode") or 0) != 0:
                raise RuntimeError(str(payload.get("retMsg") or "Bybit tickers error"))
            return payload
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last_err = exc
    raise last_err  # type: ignore[misc]


def _jobs_from_symbols(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    for symbol, base in pairs[:TOP_N]:
        jobs.append(
            {
                "group": "perp",
                "bybit": symbol,
                "symbol": symbol,
                "name": base,
            }
        )
    return jobs


def _load_cache() -> list[dict[str, str]] | None:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        rows = payload.get("contracts") or []
        jobs = [row for row in rows if row.get("bybit") and row.get("symbol")]
        return jobs[:TOP_N] if jobs else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _save_cache(jobs: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"contracts": jobs}, f, ensure_ascii=False, indent=2)


def fetch_top_contracts(limit: int = TOP_N) -> list[dict[str, str]]:
    """Return top USDT linear perpetuals ranked by 24h turnover."""
    try:
        payload = _bybit_tickers_payload()
        rows = (payload.get("result") or {}).get("list") or []
        ranked: list[tuple[float, str, str]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if not symbol.endswith("USDT"):
                continue
            try:
                turnover = float(row.get("turnover24h") or 0)
            except (TypeError, ValueError):
                turnover = 0.0
            if turnover <= 0:
                continue
            base = symbol[:-4]
            ranked.append((turnover, symbol, base))

        ranked.sort(key=lambda x: x[0], reverse=True)
        jobs = _jobs_from_symbols([(symbol, base) for _, symbol, base in ranked[:limit]])
        if jobs:
            _save_cache(jobs)
            return jobs
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Bybit tickers unavailable: {exc}", flush=True)

    cached = _load_cache()
    if cached:
        print(f"Using cached universe ({len(cached)} contracts)", flush=True)
        return cached

    jobs = _jobs_from_symbols(FALLBACK_CONTRACTS)
    print(f"Using fallback universe ({len(jobs)} contracts)", flush=True)
    return jobs


def build_scan_jobs() -> list[dict[str, str]]:
    jobs = fetch_top_contracts(TOP_N)
    if not jobs:
        raise RuntimeError("No scan jobs available")
    return jobs


def group_label(group: str) -> str:
    return {"perp": "合約 TOP50"}.get(group, group)
