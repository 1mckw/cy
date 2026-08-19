#!/usr/bin/env python3
"""Bybit USDT perpetual TOP50 — AR/DR + trend-line scanner (1H / 4H / 1D).

Universe ranking from Bybit tickers; kline scan prefers Binance USDT-M futures
(faster, CI-friendly). Charts still open Bybit / TradingView BYBIT:*.P.
"""

from __future__ import annotations

import html
import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import ardr
import trendlines as tl
from universe import GROUP_ORDER, TOP_N, build_scan_jobs, group_label

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "signals")
STATIC_DIR = os.path.join(ROOT, "static")
CHART_PACKS_PATH = os.path.join(OUT_DIR, "chart-packs.json")

BYBIT_BASE = "https://api.bybit.com"
BYBIT_HOSTS = (
    "https://api.bybit.com",
    "https://api.bytick.com",
)
BINANCE_BASE = "https://fapi.binance.com"
BINANCE_INTERVALS = {"1h": "1h", "4h": "4h", "1d": "1d"}

# Bybit symbol -> Binance USDT-M perpetual (when names differ).
BYBIT_TO_BINANCE: dict[str, str] = {
    "SHIB1000USDT": "1000SHIBUSDT",
}

_binance_symbols: set[str] | None = None
ON_GHA = bool(os.environ.get("GITHUB_ACTIONS"))
HTTP_TIMEOUT = 12 if ON_GHA else 30

TIMEFRAMES: dict[str, dict[str, Any]] = {
    "1h": {
        "interval": "60",
        "bars": 1200,
        "chart_bars": 400,
        "touch_window": 480,
        "label": "1H",
    },
    "4h": {
        "interval": "240",
        "bars": 1200,
        "chart_bars": 400,
        "touch_window": 120,
        "label": "4H",
    },
    "1d": {
        "interval": "D",
        "bars": 800,
        "chart_bars": 320,
        "touch_window": 20,
        "label": "1D",
    },
}
TIMEFRAME_ORDER = ("1h", "4h", "1d")
TF_ORDER = {tf: i for i, tf in enumerate(TIMEFRAME_ORDER)}

LOOKBACK = ardr.LOOKBACK
VOL_LEN = ardr.VOL_LEN
DROP_PCT = ardr.DROP_PCT
MIN_STREAK = ardr.MIN_STREAK
VOL_MULT = ardr.VOL_MULT
TOUCH_WINDOW_BARS = ardr.TOUCH_WINDOW_BARS
FRESH_BARS = ardr.FRESH_BARS

detect_signals = ardr.detect_signals
collect_late_ar_dr_touches = ardr.collect_late_ar_dr_touches
collect_late_ar_dr_near_misses = ardr.collect_late_ar_dr_near_misses
fresh_range = ardr.fresh_range

TREND_EXCEED_MIN_BARS = tl.TREND_EXCEED_MIN_BARS
TREND_EXCEED_MAX_BARS = tl.TREND_EXCEED_MAX_BARS
TREND_EXCEED_BARS = tl.TREND_EXCEED_BARS
build_auto_trend_lines = tl.build_auto_trend_lines
build_best_touch_line = tl.build_best_touch_line
check_line_invalidation = tl.check_line_invalidation
find_trend_touch = tl.find_trend_touch
find_trend_exceed = tl.find_trend_exceed
line_end_at_break = tl.line_end_at_break

UA = {"User-Agent": "Mozilla/5.0 (compatible; Crypto-Alerts/1.0)"}
KIND_ORDER = {"trend_exceed": 0, "ar_dr_touch": 1, "ar_dr_near": 2, "trend_touch": 3}


def chart_key(group: str, symbol: str, timeframe: str) -> str:
    return f"{group}|{symbol}|{timeframe}"


def http_get_json(url: str, timeout: int | None = None) -> Any:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def wanted_bars(timeframe: str, bars: int | None = None) -> int:
    want = bars if bars is not None else int(TIMEFRAMES[timeframe]["bars"])
    if ON_GHA:
        # One REST page is enough on CI (Bybit max 1000; charts use 320–400).
        want = min(want, 1000)
    return want


def _bybit_get_json(path: str, params: dict[str, str | int], timeout: int | None = None) -> Any:
    query = urllib.parse.urlencode(params)
    last_err: Exception | None = None
    for host in BYBIT_HOSTS:
        url = host + path + "?" + query
        try:
            payload = http_get_json(url, timeout=timeout)
            if int(payload.get("retCode") or 0) != 0:
                raise RuntimeError(str(payload.get("retMsg") or "Bybit API error"))
            return payload
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise last_err  # type: ignore[misc]


def load_binance_symbols() -> set[str]:
    global _binance_symbols
    if _binance_symbols is not None:
        return _binance_symbols
    payload = http_get_json(BINANCE_BASE + "/fapi/v1/exchangeInfo")
    symbols: set[str] = set()
    for row in payload.get("symbols") or []:
        if (
            row.get("status") == "TRADING"
            and row.get("contractType") == "PERPETUAL"
            and row.get("quoteAsset") == "USDT"
        ):
            symbols.add(str(row.get("symbol") or ""))
    _binance_symbols = {s for s in symbols if s}
    return _binance_symbols


def resolve_binance_symbol(bybit_symbol: str) -> str | None:
    symbols = load_binance_symbols()
    if bybit_symbol in symbols:
        return bybit_symbol
    alias = BYBIT_TO_BINANCE.get(bybit_symbol)
    if alias and alias in symbols:
        return alias
    return None


def fetch_binance(symbol: str, timeframe: str = "1d", bars: int | None = None) -> list[dict]:
    interval = BINANCE_INTERVALS[timeframe]
    want = wanted_bars(timeframe, bars)
    limit = min(1500, want)
    query = urllib.parse.urlencode(
        {"symbol": symbol, "interval": interval, "limit": limit}
    )
    rows = http_get_json(BINANCE_BASE + "/fapi/v1/klines?" + query)
    if not isinstance(rows, list):
        raise RuntimeError(f"Binance kline error for {symbol} ({timeframe})")
    out: list[dict] = []
    for row in rows:
        ts_ms = int(row[0])
        o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        v = float(row[5] or 0)
        out.append(
            {
                "time": ts_ms // 1000,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            }
        )
    if len(out) > want:
        out = out[-want:]
    if not out:
        raise RuntimeError(f"No Binance kline data for {symbol} ({timeframe})")
    return out


def fetch_candles(bybit_symbol: str, timeframe: str) -> tuple[list[dict], str]:
    """Return OHLCV bars and data source label (binance | bybit)."""
    binance_symbol = resolve_binance_symbol(bybit_symbol)
    if binance_symbol:
        try:
            return fetch_binance(binance_symbol, timeframe), "binance"
        except Exception:  # noqa: BLE001
            pass
    return fetch_bybit(bybit_symbol, timeframe), "bybit"


def fetch_bybit(symbol: str, timeframe: str = "1d", bars: int | None = None) -> list[dict]:
    cfg = TIMEFRAMES[timeframe]
    interval = cfg["interval"]
    want = wanted_bars(timeframe, bars)
    out: list[dict] = []
    end_ms: int | None = None

    while len(out) < want:
        limit = min(1000, want - len(out))
        params: dict[str, str | int] = {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if end_ms is not None:
            params["end"] = end_ms
        payload = _bybit_get_json("/v5/market/kline", params)
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            break
        batch: list[dict] = []
        for row in rows:
            ts_ms = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            v = float(row[5] or 0)
            batch.append(
                {
                    "time": ts_ms // 1000,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                }
            )
        batch.reverse()
        if out:
            oldest_existing = out[0]["time"]
            batch = [b for b in batch if b["time"] < oldest_existing]
            if not batch:
                break
        out = batch + out
        if len(rows) < limit:
            break
        end_ms = int(rows[-1][0]) - 1

    out.sort(key=lambda x: x["time"])
    if len(out) > want:
        out = out[-want:]
    if not out:
        raise RuntimeError(f"No Bybit kline data for {symbol} ({timeframe})")
    return out


def with_retries(fn, retries: int | None = None, pause: float | None = None):
    if retries is None:
        retries = 3
    if pause is None:
        pause = 0.5 if ON_GHA else 0.8
    last_err = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(pause * (attempt + 1))
    raise last_err  # type: ignore[misc]


def collect_trend_touches(candles: list[dict], lines: list[dict]) -> list[dict]:
    if not candles:
        return []
    lo, last = fresh_range(len(candles))
    hits = []
    for line in lines:
        if check_line_invalidation(candles, line):
            continue
        touch = find_trend_touch(candles, line)
        if not touch or not (lo <= touch["index"] <= last):
            continue
        label = "阻力趨勢線觸碰" if line["type"] == "resistance" else "支撐趨勢線觸碰"
        hits.append(
            {
                "kind": "trend_touch",
                "label": label,
                "type": line["type"],
                "time": touch["time"],
                "index": touch["index"],
                "level": touch["price"],
                "close": touch["close"],
            }
        )
    return hits


def collect_trend_exceeds(candles: list[dict], lines: list[dict]) -> list[dict]:
    hits = []
    for line in lines:
        exc = find_trend_exceed(candles, line)
        if not exc:
            continue
        label = "阻力趨勢線超出" if line["type"] == "resistance" else "支撐趨勢線超出"
        hits.append(
            {
                "kind": "trend_exceed",
                "label": label,
                "type": line["type"],
                "time": exc["time"],
                "index": exc["index"],
                "level": exc["price"],
                "close": exc["close"],
                "exceed_bars": exc["bars"],
            }
        )
    return hits


def chart_pack_start_index(
    candles: list[dict],
    lines: list[dict],
    chart_bars: int,
    best_touch_line: dict | None = None,
) -> int:
    tail = max(0, len(candles) - chart_bars)
    if not candles:
        return tail
    starts = [int(line["p1"]["index"]) for line in lines] if lines else []
    if best_touch_line is not None:
        starts.append(int(best_touch_line["p1"]["index"]))
    if not starts:
        return tail
    return min(tail, min(starts))


def build_chart_pack(
    candles: list[dict],
    signals: list[dict],
    lines: list[dict],
    chart_bars: int = 800,
) -> dict:
    best_touch = build_best_touch_line(candles)
    start_idx = chart_pack_start_index(candles, lines, chart_bars, best_touch)
    trimmed = candles[start_idx:]
    if not trimmed:
        return {"candles": [], "rays": [], "trend_lines": [], "best_touch_line": None}

    t_min = int(trimmed[0]["time"])
    t_max = int(trimmed[-1]["time"])
    last_time = t_max

    visible_signals = [s for s in signals if t_min <= int(s["time"]) <= t_max]
    rays = []
    for sig in visible_signals:
        ray = ardr.signal_to_chart_ray(sig, candles, last_time)
        segs = []
        for seg in ray.get("segments") or []:
            t0, t1 = int(seg["t0"]), int(seg["t1"])
            clip0, clip1 = max(t0, t_min), min(t1, t_max)
            if clip1 > clip0:
                segs.append({**seg, "t0": clip0, "t1": clip1})
        if segs:
            ray["segments"] = segs
            rays.append(ray)

    trend = []
    for line in lines:
        invalidated = check_line_invalidation(candles, line)
        end_time, end_price = line_end_at_break(candles, line)
        trend.append(
            {
                "type": line["type"],
                "p1": {"time": int(line["p1"]["time"]), "price": float(line["p1"]["price"])},
                "p2": {"time": int(line["p2"]["time"]), "price": float(line["p2"]["price"])},
                "endTime": int(end_time),
                "endPrice": float(end_price),
                "invalidated": invalidated,
                "pivot_count": int(line.get("pivot_count") or 0),
            }
        )

    best_touch_line = None
    if best_touch is not None:
        end_time, end_price = line_end_at_break(candles, best_touch)
        best_touch_line = {
            "type": best_touch["type"],
            "p1": {"time": int(best_touch["p1"]["time"]), "price": float(best_touch["p1"]["price"])},
            "p2": {"time": int(best_touch["p2"]["time"]), "price": float(best_touch["p2"]["price"])},
            "endTime": int(end_time),
            "endPrice": float(end_price),
            "invalidated": check_line_invalidation(candles, best_touch),
            "pivot_count": int(best_touch.get("pivot_count") or 0),
        }

    return {
        "candles": [
            {
                "time": int(c["time"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            }
            for c in trimmed
        ],
        "rays": rays,
        "trend_lines": trend,
        "best_touch_line": best_touch_line,
    }


def scan_job(job: dict[str, str]) -> dict:
    group = job["group"]
    bybit = job["bybit"]
    symbol = job["symbol"]
    name = job["name"]
    timeframe = job["timeframe"]
    cfg = TIMEFRAMES[timeframe]
    touch_window = int(cfg["touch_window"])
    try:
        candles, data_source = with_retries(lambda: fetch_candles(bybit, timeframe))
        signals = detect_signals(candles)
        late = collect_late_ar_dr_touches(candles, signals, touch_window)
        near = collect_late_ar_dr_near_misses(candles, signals, touch_window)
        lines = build_auto_trend_lines(candles)
        trend = collect_trend_touches(candles, lines)
        exceed = collect_trend_exceeds(candles, lines)
        events = late + near + trend + exceed
        for ev in events:
            ev["timeframe"] = timeframe
        return {
            "group": group,
            "symbol": symbol,
            "bybit_symbol": bybit,
            "name": name,
            "source": data_source,
            "timeframe": timeframe,
            "bars": len(candles),
            "events": events,
            "error": None,
            "chart": build_chart_pack(candles, signals, lines, int(cfg["chart_bars"])),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "group": group,
            "symbol": symbol,
            "bybit_symbol": bybit,
            "name": name,
            "source": "unknown",
            "timeframe": timeframe,
            "bars": 0,
            "events": [],
            "error": str(exc),
            "chart": None,
        }


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_num(v: float) -> str:
    return f"{v:.6g}"


def fmt_tf(tf: str) -> str:
    return TIMEFRAMES.get(tf, {}).get("label", (tf or "?").upper())


def read_static(name: str) -> str:
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
        return f.read()


def build_symbol_catalog(results: list[dict], charts: dict) -> list[dict]:
    catalog = []
    for r in results:
        if r.get("error"):
            continue
        g, sym, tf = r["group"], r["symbol"], r.get("timeframe") or "1d"
        ck = chart_key(g, sym, tf)
        catalog.append(
            {
                "group": g,
                "symbol": sym,
                "name": r.get("name") or sym,
                "timeframe": tf,
                "hasHit": bool(r.get("events")),
                "hasChart": ck in charts,
            }
        )
    catalog.sort(
        key=lambda x: (
            not x["hasHit"],
            TF_ORDER.get(x.get("timeframe", ""), 99),
            GROUP_ORDER.get(x["group"], 99),
            x["symbol"],
        )
    )
    return catalog


def render_html(payload: dict) -> str:
    hits = payload["hits"]
    ar_dr = [h for h in hits if h["kind"] == "ar_dr_touch"]
    ar_near = [h for h in hits if h["kind"] == "ar_dr_near"]
    trend = [h for h in hits if h["kind"] == "trend_touch"]
    exceed = [h for h in hits if h["kind"] == "trend_exceed"]
    c = payload["counts"]
    u = payload["universe"]
    gen = html.escape(payload["generated_at"])

    def sym_btn(h: dict) -> str:
        sym = str(h.get("symbol", ""))
        grp = str(h.get("group", ""))
        name = str(h.get("name", sym))
        tf = str(h.get("timeframe", "1d"))
        attrs = (
            f'data-symbol="{html.escape(sym, quote=True)}" '
            f'data-group="{html.escape(grp, quote=True)}" '
            f'data-name="{html.escape(name, quote=True)}" '
            f'data-tf="{html.escape(tf, quote=True)}" '
            f'data-source="bybit" '
            f'data-tvSymbol="{html.escape("BYBIT:" + sym + ".P", quote=True)}" '
            f'data-level="{html.escape(str(h.get("level", "")), quote=True)}" '
            f'data-type="{html.escape(str(h.get("type", "")), quote=True)}" '
            f'data-kind="{html.escape(str(h.get("kind", "")), quote=True)}" '
            f'data-time="{html.escape(str(h.get("time", "")), quote=True)}"'
        )
        return (
            f'<button type="button" class="sym-btn" {attrs} title="開啟蠟燭圖">'
            f"<code>{html.escape(sym)}</code></button>"
        )

    def tf_cell(h: dict) -> str:
        return html.escape(fmt_tf(str(h.get("timeframe", "1d"))))

    def pool_cell(h: dict) -> str:
        g = h.get("group", "")
        return html.escape(group_label(str(g)))

    def rows(items: list[dict], empty: str, cols: int, builder) -> str:
        if not items:
            return f'<tr><td colspan="{cols}" class="empty">{empty}</td></tr>'
        return "\n".join(builder(h) for h in items)

    def row_ar_dr(h: dict) -> str:
        cls = "ar" if h.get("type") == "AR" else "dr"
        tf = str(h.get("timeframe", "1d"))
        return (
            f'<tr data-symbol="{html.escape(str(h.get("symbol","")), quote=True)}" '
            f'data-group="{html.escape(str(h.get("group","")), quote=True)}" '
            f'data-timeframe="{html.escape(tf, quote=True)}">'
            f'<td><span class="tag {cls}">{html.escape(str(h.get("type", "")))}</span></td>'
            f"<td>{tf_cell(h)}</td>"
            f"<td>{pool_cell(h)}</td>"
            f"<td>{sym_btn(h)}</td>"
            f"<td>{html.escape(h.get('name', ''))}</td>"
            f'<td class="num">{fmt_num(float(h["level"]))}</td>'
            f'<td class="num">{int(h.get("bars_after_signal", 0))}</td>'
            f"<td>{html.escape(fmt_ts(int(h['time'])))}</td>"
            "</tr>"
        )

    def row_ar_near(h: dict) -> str:
        cls = "ar" if h.get("type") == "AR" else "dr"
        tf = str(h.get("timeframe", "1d"))
        return (
            f'<tr data-symbol="{html.escape(str(h.get("symbol","")), quote=True)}" '
            f'data-group="{html.escape(str(h.get("group","")), quote=True)}" '
            f'data-timeframe="{html.escape(tf, quote=True)}">'
            f'<td><span class="tag {cls}">{html.escape(str(h.get("type", "")))}</span></td>'
            f"<td>{tf_cell(h)}</td>"
            f"<td>{pool_cell(h)}</td>"
            f"<td>{sym_btn(h)}</td>"
            f"<td>{html.escape(h.get('name', ''))}</td>"
            f'<td class="num">{fmt_num(float(h["level"]))}</td>'
            f'<td class="num">{float(h.get("gap_pct", 0)):.3g}%</td>'
            f'<td class="num">{int(h.get("bars_after_signal", 0))}</td>'
            f"<td>{html.escape(fmt_ts(int(h['time'])))}</td>"
            "</tr>"
        )

    def row_trend(h: dict) -> str:
        cls = "resist" if h.get("type") == "resistance" else "support"
        tf = str(h.get("timeframe", "1d"))
        return (
            f'<tr data-symbol="{html.escape(str(h.get("symbol","")), quote=True)}" '
            f'data-group="{html.escape(str(h.get("group","")), quote=True)}" '
            f'data-timeframe="{html.escape(tf, quote=True)}">'
            f'<td><span class="tag {cls}">{html.escape(str(h.get("type", "")))}</span></td>'
            f"<td>{tf_cell(h)}</td>"
            f"<td>{pool_cell(h)}</td>"
            f"<td>{sym_btn(h)}</td>"
            f"<td>{html.escape(h.get('name', ''))}</td>"
            f'<td class="num">{fmt_num(float(h["level"]))}</td>'
            f"<td>{html.escape(fmt_ts(int(h['time'])))}</td>"
            "</tr>"
        )

    def row_exceed(h: dict) -> str:
        cls = "resist" if h.get("type") == "resistance" else "support"
        tf = str(h.get("timeframe", "1d"))
        return (
            f'<tr data-symbol="{html.escape(str(h.get("symbol","")), quote=True)}" '
            f'data-group="{html.escape(str(h.get("group","")), quote=True)}" '
            f'data-timeframe="{html.escape(tf, quote=True)}">'
            f'<td><span class="tag {cls}">{html.escape(str(h.get("type", "")))}</span></td>'
            f"<td>{tf_cell(h)}</td>"
            f"<td>{pool_cell(h)}</td>"
            f"<td>{sym_btn(h)}</td>"
            f"<td>{html.escape(h.get('name', ''))}</td>"
            f'<td class="num">{fmt_num(float(h["level"]))}</td>'
            f'<td class="num">{int(h.get("exceed_bars", TREND_EXCEED_BARS))}</td>'
            f"<td>{html.escape(fmt_ts(int(h['time'])))}</td>"
            "</tr>"
        )

    catalog = build_symbol_catalog(payload.get("results") or [], payload.get("charts") or {})
    embed_js = (
        "<script>window.CHART_PACKS = {};"
        + "window.SYMBOL_CATALOG = "
        + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        + ";window.WATCHLISTS = {};</script>\n"
    )

    tf_labels = " · ".join(fmt_tf(tf) for tf in TIMEFRAME_ORDER)
    tf_buttons = "".join(
        f'<button type="button" data-tf="{tf}">{fmt_tf(tf)}</button>' for tf in TIMEFRAME_ORDER
    )
    filter_script = read_static("report-pool-filter.html")

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="3600" />
  <title>Crypto Touch Alerts</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --bg: #000; --panel: rgba(8,12,20,.58); --border: rgba(0,255,213,.18);
      --text: #eefdfb; --muted: #7a93a8; --primary: #00f0c8;
      --ar: #00e896; --dr: #ff4d6d;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "Space Grotesk", system-ui, sans-serif;
      background: #000; color: var(--text); min-height: 100vh; padding: 28px 18px 48px;
    }}
    .wrap {{ max-width: 1100px; margin: 0 auto; }}
    h1 {{ font-size: 1.5rem; color: var(--primary); }}
    .meta {{ color: var(--muted); font-size: .9rem; margin: 8px 0 18px; line-height: 1.5; }}
    .cards {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 16px; }}
    @media (max-width: 900px) {{ .cards {{ grid-template-columns: repeat(2, 1fr); }} }}
    .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; }}
    .card .lbl {{ font-size: .65rem; color: var(--muted); text-transform: uppercase; }}
    .card .val {{ font-family: "JetBrains Mono", monospace; font-size: 1.15rem; font-weight: 700; margin-top: 4px; }}
    .pool-filters, .tf-filters {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; }}
    .pool-filters button, .tf-filters button {{
      font: inherit; cursor: pointer; height: 30px; padding: 0 12px; border-radius: 999px;
      border: 1px solid var(--border); background: rgba(6,10,18,.55); color: var(--muted); font-size: .78rem;
    }}
    .pool-filters button.active, .tf-filters button.active {{
      color: #04110e; border-color: transparent;
      background: linear-gradient(135deg, #00f0c8, #00b894);
    }}
    h2 {{ font-size: 1.05rem; margin: 22px 0 10px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .84rem; min-width: 640px; }}
    th, td {{ padding: 9px 12px; text-align: left; border-bottom: 1px solid rgba(0,240,200,.08); }}
    th {{ color: var(--muted); font-size: .68rem; text-transform: uppercase; }}
    td.num, th.num {{ text-align: right; font-family: "JetBrains Mono", monospace; }}
    td.empty {{ text-align: center; color: var(--muted); padding: 22px; }}
    code {{ font-family: "JetBrains Mono", monospace; color: var(--primary); }}
    .tag {{ display: inline-block; font-size: .72rem; padding: 2px 7px; border-radius: 5px; font-weight: 700; }}
    .tag.ar {{ background: rgba(0,232,150,.14); color: var(--ar); }}
    .tag.dr {{ background: rgba(255,77,109,.14); color: var(--dr); }}
    .tag.resist {{ background: rgba(255,77,109,.14); color: var(--dr); }}
    .tag.support {{ background: rgba(0,232,150,.14); color: var(--ar); }}
    .sym-btn {{ background: none; border: 0; padding: 0; cursor: pointer; color: inherit; }}
    .sym-btn:hover code {{ text-decoration: underline; }}
    footer {{ margin-top: 28px; color: var(--muted); font-size: .75rem; }}
    a {{ color: var(--primary); }}
    .search-fab {{
      position: fixed; right: 18px; bottom: 22px; z-index: 70;
      display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-radius: 14px;
      border: 1px solid var(--border); background: rgba(6,10,18,.85); color: var(--text); cursor: pointer; font: inherit;
    }}
    .search-overlay {{
      position: fixed; inset: 0; z-index: 85; background: rgba(0,0,0,.62);
      display: flex; align-items: flex-start; justify-content: center; padding: 10vh 16px;
    }}
    .search-overlay[hidden] {{ display: none !important; }}
    .search-modal {{
      width: min(520px, 100%); max-height: 70vh; background: rgba(8,12,20,.95);
      border: 1px solid var(--border); border-radius: 16px; display: flex; flex-direction: column; overflow: hidden;
    }}
    .search-modal-head {{ display: flex; gap: 8px; padding: 14px; border-bottom: 1px solid var(--border); }}
    #symbolSearch {{ flex: 1; height: 42px; padding: 8px 14px; border-radius: 10px; border: 1px solid var(--border); background: #0a0e14; color: var(--text); font-family: "JetBrains Mono", monospace; }}
    #symbolList {{ list-style: none; margin: 0; padding: 8px; overflow-y: auto; flex: 1; }}
    #symbolList li {{ padding: 10px 12px; border-radius: 10px; cursor: pointer; }}
    #symbolList li:hover {{ background: rgba(0,240,200,.06); }}
    .modal {{
      position: fixed; inset: 0; z-index: 80; display: flex; align-items: center; justify-content: center;
      padding: 16px; background: rgba(0,0,0,.62); opacity: 0; pointer-events: none; transition: opacity .2s;
    }}
    .modal.open {{ opacity: 1; pointer-events: auto; }}
    .modal-panel {{
      width: min(1100px, 100%); height: min(720px, 92vh);
      background: rgba(8,12,20,.9); border: 1px solid var(--border); border-radius: 16px;
      display: flex; flex-direction: column; overflow: hidden;
    }}
    .modal-head {{ display: flex; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--border); }}
    .modal-close {{ width: 40px; height: 40px; border-radius: 10px; border: 1px solid var(--border); background: transparent; color: var(--text); cursor: pointer; }}
    .modal-chart {{ flex: 1; min-height: 0; position: relative; background: #000; }}
    .modal-chart #lwc {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
    .modal-status {{ position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Crypto · AR/DR &amp; 趨勢線 Alerts</h1>
    <p class="meta">
      商品池 <strong>Bybit 合約 24h 交易額 TOP{TOP_N}</strong> · 週期 <strong>{tf_labels}</strong> ·
      蠟燭圖優先 <strong>Bybit</strong> · 掃描 {u['total']} 檔 × {u['timeframes']} 週期 = {u['jobs']} jobs · 更新 {gen}
    </p>
    <div class="cards">
      <div class="card"><div class="lbl">掃描 OK</div><div class="val">{c['ok']}/{c['jobs']}</div></div>
      <div class="card"><div class="lbl">AR/DR 觸碰</div><div class="val">{c['ar_dr_touch']}</div></div>
      <div class="card"><div class="lbl">AR/DR 接近</div><div class="val">{c['ar_dr_near']}</div></div>
      <div class="card"><div class="lbl">趨勢線觸碰</div><div class="val">{c['trend_touch']}</div></div>
      <div class="card"><div class="lbl">趨勢線超出</div><div class="val">{c['trend_exceed']}</div></div>
    </div>
    <div class="pool-filters" id="poolFilters">
      <button type="button" data-pool="all" class="active">全部</button>
      <button type="button" data-pool="perp">合約 TOP50</button>
    </div>
    <div class="tf-filters" id="tfFilters">
      <button type="button" data-tf="all" class="active">全部週期</button>
      {tf_buttons}
    </div>

    <h2>趨勢線超出（最新 {TREND_EXCEED_MIN_BARS}–{TREND_EXCEED_MAX_BARS} 根）</h2>
    <div class="panel"><table><thead><tr>
      <th>類型</th><th>週期</th><th>池</th><th>代碼</th><th>名稱</th><th class="num">價位</th><th class="num">根數</th><th>時間</th>
    </tr></thead><tbody data-section="exceed">{rows(exceed, "目前無超出信號", 8, row_exceed)}</tbody></table></div>

    <h2>AR / DR 觸碰（超過各週期門檻根數後）</h2>
    <div class="panel"><table><thead><tr>
      <th>類型</th><th>週期</th><th>池</th><th>代碼</th><th>名稱</th><th class="num">價位</th><th class="num">根數</th><th>時間</th>
    </tr></thead><tbody data-section="ar_dr">{rows(ar_dr, "目前無 AR/DR 觸碰", 8, row_ar_dr)}</tbody></table></div>

    <h2>AR / DR 接近未觸</h2>
    <div class="panel"><table><thead><tr>
      <th>類型</th><th>週期</th><th>池</th><th>代碼</th><th>名稱</th><th class="num">價位</th><th class="num">差距</th><th class="num">根數</th><th>時間</th>
    </tr></thead><tbody data-section="ar_near">{rows(ar_near, "目前無接近未觸", 9, row_ar_near)}</tbody></table></div>

    <h2>趨勢線觸碰</h2>
    <div class="panel"><table><thead><tr>
      <th>類型</th><th>週期</th><th>池</th><th>代碼</th><th>名稱</th><th class="num">價位</th><th>時間</th>
    </tr></thead><tbody data-section="trend">{rows(trend, "目前無趨勢線觸碰", 7, row_trend)}</tbody></table></div>

    <footer>每小時自動更新 · 資料來源 Bybit · <a href="latest.json">latest.json</a></footer>
  </div>

  <button type="button" class="search-fab" id="searchFab" aria-label="搜尋商品">
    <span>🔍</span><span>搜尋商品</span>
  </button>
  <div class="search-overlay" id="symbolOverlay" hidden>
    <div class="search-modal">
      <div class="search-modal-head">
        <input id="symbolSearch" type="search" placeholder="代碼或名稱…" autocomplete="off" />
        <button type="button" id="symbolSearchClose">關閉</button>
      </div>
      <ul id="symbolList"></ul>
    </div>
  </div>

  <div id="chart-modal" class="modal" hidden aria-hidden="true">
    <div class="modal-panel" role="dialog">
      <div class="modal-head">
        <div>
          <div id="chart-title" class="modal-title">Chart</div>
          <div id="chart-sub" class="modal-sub"></div>
        </div>
        <button type="button" class="modal-close" id="chart-close" aria-label="關閉">×</button>
      </div>
      <div class="modal-chart" id="chart-body">
        <div class="modal-status" id="chart-status">載入中…</div>
        <div id="lwc" hidden></div>
        <iframe id="tv-frame" title="TradingView chart" hidden></iframe>
      </div>
    </div>
  </div>
{embed_js}{filter_script}{read_static("report-chart-modal.html")}
</body>
</html>
"""


def main() -> int:
    try:
        return _main_impl()
    except Exception as exc:  # noqa: BLE001
        print(f"Fatal scan error: {exc}", flush=True)
        return 1


def _main_impl() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    global _binance_symbols
    try:
        binance_n = len(load_binance_symbols())
        print(f"Binance USDT-M symbols loaded: {binance_n}", flush=True)
    except Exception as exc:  # noqa: BLE001
        _binance_symbols = set()
        print(f"Binance exchangeInfo unavailable ({exc}); Bybit fallback only", flush=True)
    base_jobs = build_scan_jobs()
    jobs: list[dict[str, str]] = []
    for job in base_jobs:
        for tf in TIMEFRAME_ORDER:
            jobs.append({**job, "timeframe": tf})
    perp_n = sum(1 for j in base_jobs if j["group"] == "perp")
    print(
        f"Scanning {len(jobs)} jobs "
        f"({len(base_jobs)} symbols × {len(TIMEFRAME_ORDER)} TF: {', '.join(fmt_tf(t) for t in TIMEFRAME_ORDER)})…",
        flush=True,
    )

    binance_jobs: list[dict[str, str]] = []
    bybit_jobs: list[dict[str, str]] = []
    for j in jobs:
        if resolve_binance_symbol(j["bybit"]):
            binance_jobs.append(j)
        else:
            bybit_jobs.append(j)
    print(f"  routes: Binance {len(binance_jobs)}, Bybit {len(bybit_jobs)}", flush=True)

    bn_workers = 16 if ON_GHA else 8
    bb_workers = 4 if ON_GHA else 6
    if not binance_jobs:
        bb_workers = 3 if ON_GHA else 6

    results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=bn_workers) as bn_pool, ThreadPoolExecutor(
        max_workers=max(1, bb_workers)
    ) as bb_pool:
        futs = [bn_pool.submit(scan_job, j) for j in binance_jobs]
        futs += [bb_pool.submit(scan_job, j) for j in bybit_jobs]
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 20 == 0:
                print(f"  progress {done}/{len(jobs)}", flush=True)

    hits: list[dict] = []
    charts: dict[str, dict] = {}
    slim_results: list[dict] = []

    for r in results:
        pack = r.pop("chart", None)
        g, sym = r["group"], r["symbol"]
        tf = r.get("timeframe") or "1d"
        key = chart_key(g, sym, tf)
        if pack and not r.get("error"):
            charts[key] = pack
        slim_results.append({k: v for k, v in r.items() if k != "chart"})
        for ev in r.get("events") or []:
            hits.append({**ev, "group": g, "symbol": sym, "name": r.get("name"), "timeframe": tf})

    hits.sort(
        key=lambda x: (
            KIND_ORDER.get(x["kind"], 99),
            TF_ORDER.get(x.get("timeframe", ""), 99),
            GROUP_ORDER.get(x.get("group", ""), 99),
            x["symbol"],
        )
    )

    ok = sum(1 for r in slim_results if not r.get("error"))
    min_ok = len(jobs) if not ON_GHA else max(45, len(jobs) * 2 // 3)
    if ok < min_ok:
        print(f"Too few OK jobs: {ok}/{len(jobs)} (need {min_ok})", flush=True)
        return 1

    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    payload = {
        "generated_at": generated_at,
        "timeframes": list(TIMEFRAME_ORDER),
        "timeframe": "+".join(TIMEFRAME_ORDER),
        "universe": {
            "total": len(base_jobs),
            "perp": perp_n,
            "timeframes": len(TIMEFRAME_ORDER),
            "jobs": len(jobs),
        },
        "params": {
            "timeframes": {tf: TIMEFRAMES[tf] for tf in TIMEFRAME_ORDER},
            "drop_pct": DROP_PCT,
            "min_streak": MIN_STREAK,
            "vol_mult": VOL_MULT,
        },
        "counts": {
            "jobs": len(jobs),
            "ok": ok,
            "errors": len(jobs) - ok,
            "ar_dr_touch": sum(1 for h in hits if h["kind"] == "ar_dr_touch"),
            "ar_dr_near": sum(1 for h in hits if h["kind"] == "ar_dr_near"),
            "trend_touch": sum(1 for h in hits if h["kind"] == "trend_touch"),
            "trend_exceed": sum(1 for h in hits if h["kind"] == "trend_exceed"),
            "hits": len(hits),
        },
        "hits": hits,
        "results": slim_results,
        "charts": charts,
    }

    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in payload.items() if k != "charts"}, f, ensure_ascii=False, indent=2)

    with open(CHART_PACKS_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": generated_at, "charts": charts},
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    page = render_html(payload)
    for name in ("latest.html", "index.html"):
        with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
            f.write(page)

    errs = [r for r in slim_results if r.get("error")]
    if errs:
        print(f"Errors ({len(errs)}):", flush=True)
        for e in errs[:8]:
            print(f"  {e['symbol']} ({e['bybit_symbol']}): {e['error']}", flush=True)

    src_binance = sum(1 for r in slim_results if r.get("source") == "binance" and not r.get("error"))
    src_bybit = sum(1 for r in slim_results if r.get("source") == "bybit" and not r.get("error"))
    print(
        f"Hits: {len(hits)} · OK: {ok}/{len(jobs)} · "
        f"data: Binance {src_binance}, Bybit {src_bybit}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
