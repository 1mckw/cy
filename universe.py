"""Scan universe: Bybit USDT perpetual contracts — top 50 by 24h turnover."""

from __future__ import annotations

import json
import urllib.request

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers?category=linear"
TOP_N = 50
UA = {"User-Agent": "Mozilla/5.0 (compatible; Crypto-Alerts/1.0)"}

GROUP_ORDER = {"perp": 0}


def _http_get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_top_contracts(limit: int = TOP_N) -> list[dict[str, str]]:
    """Return top USDT linear perpetuals ranked by 24h turnover."""
    payload = _http_get_json(BYBIT_TICKERS_URL)
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
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        ranked.append((turnover, symbol, base))

    ranked.sort(key=lambda x: x[0], reverse=True)
    jobs: list[dict[str, str]] = []
    for _, symbol, base in ranked[:limit]:
        jobs.append(
            {
                "group": "perp",
                "bybit": symbol,
                "symbol": symbol,
                "name": base,
            }
        )
    return jobs


def build_scan_jobs() -> list[dict[str, str]]:
    return fetch_top_contracts(TOP_N)


def group_label(group: str) -> str:
    return {"perp": "合約 TOP50"}.get(group, group)
