"""Scan universe: Hyperliquid perpetual contracts — top 50 by 24h notional volume."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

HL_API = "https://api.hyperliquid.xyz/info"
TOP_N = 50
UA = {"User-Agent": "Mozilla/5.0 (compatible; Crypto-Alerts/1.0)"}
ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(ROOT, "signals", "universe-cache.json")

FALLBACK_COINS: list[str] = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "SUI",
    "HYPE", "LTC", "NEAR", "APT", "ARB", "OP", "INJ", "TIA", "SEI", "WLD",
    "UNI", "AAVE", "ENA", "ONDO", "PEPE", "TRUMP", "FARTCOIN", "WIF", "TAO",
    "CRV", "DOT", "FIL", "ETC", "ATOM", "ICP", "RENDER", "PENGU", "VIRTUAL",
    "TRX", "XLM", "BCH", "TON", "POL", "SHIB", "MNT", "ZEC", "ALGO", "STX",
    "JUP", "PYTH",
]

GROUP_ORDER = {"perp": 0}


def _hl_post(payload: dict, timeout: int = 15) -> any:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HL_API, data=data, headers={**UA, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _jobs_from_coins(coins: list[str]) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    for coin in coins[:TOP_N]:
        jobs.append({
            "group": "perp",
            "coin": coin,
            "symbol": coin,
            "name": coin,
        })
    return jobs


def _load_cache() -> list[dict[str, str]] | None:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        rows = payload.get("contracts") or []
        jobs = [row for row in rows if row.get("coin") and row.get("symbol")]
        return jobs[:TOP_N] if jobs else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _save_cache(jobs: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"contracts": jobs}, f, ensure_ascii=False, indent=2)


def fetch_top_contracts(limit: int = TOP_N) -> list[dict[str, str]]:
    """Return top perpetuals ranked by 24h notional volume from Hyperliquid."""
    try:
        result = _hl_post({"type": "metaAndAssetCtxs"})
        universe = result[0]["universe"]
        ctxs = result[1]

        ranked: list[tuple[float, str]] = []
        for meta, ctx in zip(universe, ctxs):
            coin = str(meta.get("name") or "")
            if not coin or ":" in coin:
                continue
            try:
                vol = float(ctx.get("dayNtlVlm") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            if vol <= 0:
                continue
            ranked.append((vol, coin))

        ranked.sort(key=lambda x: x[0], reverse=True)
        jobs = _jobs_from_coins([coin for _, coin in ranked[:limit]])
        if jobs:
            _save_cache(jobs)
            return jobs
    except Exception as exc:  # noqa: BLE001
        print(f"Hyperliquid metaAndAssetCtxs unavailable: {exc}", flush=True)

    cached = _load_cache()
    if cached:
        print(f"Using cached universe ({len(cached)} contracts)", flush=True)
        return cached

    jobs = _jobs_from_coins(FALLBACK_COINS)
    print(f"Using fallback universe ({len(jobs)} contracts)", flush=True)
    return jobs


def build_scan_jobs() -> list[dict[str, str]]:
    jobs = fetch_top_contracts(TOP_N)
    if not jobs:
        raise RuntimeError("No scan jobs available")
    return jobs


def group_label(group: str) -> str:
    return {"perp": "合約 TOP50"}.get(group, group)
