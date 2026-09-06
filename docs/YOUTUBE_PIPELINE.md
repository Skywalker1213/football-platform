# YouTube short-form pipeline (Quant-Striker)

每日選出 **6 場**最高信心的即將開賽預測，產生約 **3 分鐘**橫向 MP4（1920×1080）與 YouTube 標題／說明／標籤。

**免責：** 僅供統計估計與教育用途，**並非博彩建議**。本腳本不會自動上傳 YouTube。

## 選場規則

見 `app/youtube_picks.py`：

- 非 FINISHED／LIVE，且有預測與 Quant-Striker system card
- 信心排序：`STRONG` > `LEAN` > `TOSS-UP`；同層再比 `max(p_home,p_draw,p_away)`
- 僅 allowlist `competition_key`（`app/competitions.py`）
- 優先香港時間 today−2…today+3；不足 6 場則擴至 today+7
- 去重同一 fixture；結果寫入 `data/youtube_picks_YYYYMMDD.json`

## 執行

```bash
cd /workspace/football-platform
source .venv/bin/activate
pip install pillow edge-tts   # 首次；TTS 失敗仍會產出靜音片＋字幕卡

PYTHONPATH=. python scripts/make_youtube_video.py
# 略過語音：
PYTHONPATH=. python scripts/make_youtube_video.py --no-tts
```

## 輸出（`data/youtube/`）

| 檔案 | 說明 |
|------|------|
| `picks.json` | 六場選擇與機率／信心 |
| `script_zh.md` | 繁中旁白腳本 |
| `title.txt` / `description.txt` / `tags.txt` | YouTube 發佈文案 |
| `quant_striker_picks.mp4` | ≈180s 影片 |

亦可單獨選場：

```bash
PYTHONPATH=. python -m app.youtube_picks
```

## 排程建議

在既有 `scripts/daily_update.py`（collect → predict）之後呼叫本腳本，例如 cron／Task Scheduler：

```bash
PYTHONPATH=. FOOTBALL_SKIP_WEATHER=1 python scripts/daily_update.py
PYTHONPATH=. python scripts/make_youtube_video.py
```
