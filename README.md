# Crypto AR/AD Touch Alerts

GitHub Pages 每小時掃描 **Hyperliquid 永續合約 24h 交易額 TOP50**，輸出 AR/AD 與趨勢線觸碰報告。

- **商品池排名**：Hyperliquid `metaAndAssetCtxs`（24h notional volume）
- **K 線掃描**：Hyperliquid `candleSnapshot`（單次最多 5000 根）
- **圖表展示**：本地 pack 優先，備援 TradingView `BYBIT:*.P`

## 線上報告

- HTML：https://1mckw.github.io/cy/
- JSON：`/latest.json`

## 商品池

| 池 | 數量 | 說明 |
|----|------|------|
| **合約 TOP50** | 50 | Hyperliquid 永續，依 24h 交易額排序 |

共 **50** 檔 × **1H / 4H / 1D** = **150** 掃描 jobs。

週期：**1H · 4H · 1D** · 更新：每小時（UTC 整點）

| 週期 | 歷史 K | 圖表顯示 | 觸碰（>10日） | 晚觸碰新鮮窗 | 接近區間 |
|------|--------|----------|--------------|--------------|----------|
| 1H | 4800 | 4800 | >240 根 · 最近 2 根 | ≥1440 根 · 最近 240 根 | 1440–4800 根 · 誤差 0～1% |
| 4H | 1200 | 1200 | >60 根 · 最近 2 根 | ≥360 根 · 最近 60 根 | 360–1200 根 · 誤差 0～1% |
| 1D | 200 | 200 | >10 根 · 最近 2 根 | ≥60 根 · 最近 10 根 | 60–200 根 · 誤差 0～1% |

蠟燭圖資料來自 Hyperliquid；彈窗優先本地 pack，備援 **TradingView BYBIT 合約**。

## AR/AD 規則

| | AR | AD |
|---|----|----|
| 觸發 | 急跌後反轉陽線 | 急漲後反轉陰線 |
| 射線 | 信號 K 上下引線各向右延伸，碰到即停 |
| **觸碰** | 主射線首次觸碰在 **超過 10 根日 K** 之後，且發生在 **最近 2 根** |
| **晚觸碰** | 主射線首次觸碰根數 **≥ 60**（且 >20），且發生在 **最近 10 根日 K** 內 |
| **接近未觸** | 主射線仍有效；根數 **60～200** 日 K 內；引線距射線 **0～1%** 未碰到 |

主射線：AR→上引線（high），AD→下引線（low）。日 K 門檻依週期換算（1H×24、4H×6）。

**趨勢線：** 至少 3 觸點；最多 2 條上升支撐 + 2 條下降阻力；觸點較少者圖上 50% 透明；急漲/跌貫穿 grace 2 根 K。

## 首次部署 GitHub Pages

Repo **Settings → Pages → Build and deployment → Source** 選 **GitHub Actions**。
未啟用前 workflow 的 deploy 步驟會失敗，網站 `https://1mckw.github.io/cy/` 會 404。

## 手動觸發

Repo → **Actions** → **Hourly Crypto Alerts (Hyperliquid TOP50)** → **Run workflow**

## 本機

```bash
python scan_signals.py
```

商品池：`universe.py`（執行時從 Hyperliquid API 動態取 TOP50）
