# Crypto AR/DR Touch Alerts

GitHub Pages 每小時掃描 **Hyperliquid 永續合約 24h 交易額 TOP50**，輸出 AR/DR 與趨勢線觸碰報告。

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

| 週期 | 歷史 K | 圖表顯示 | 晚觸碰門檻 |
|------|--------|----------|-----------|
| 1H | 2000 | 400 | 480 根（約 20 天） |
| 4H | 1200 | 400 | 120 根（約 20 天） |
| 1D | 800 | 320 | 20 根（約 20 天） |

蠟燭圖資料來自 Hyperliquid；彈窗優先本地 pack，備援 **TradingView BYBIT 合約**。

## AR/DR 規則

|   | AR | DR |
|---|----|----|
| 觸發 | 急跌後反轉陽線 | 急漲後反轉陰線 |
| 射線 | 信號 K 上下引線各向右延伸，碰到即停 |
| 晚觸碰 | AR→上引線、DR→下引線，超過各週期門檻根數後 |

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
