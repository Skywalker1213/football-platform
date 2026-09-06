#!/usr/bin/env python3
"""Daily pipeline: collect → merge_dupes → scorecard → learn/fine-tune QS → predict → report.

Usage:
  PYTHONPATH=. python scripts/daily_update.py
  PYTHONPATH=. FOOTBALL_SKIP_WEATHER=1 python scripts/daily_update.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db
from app.collector_core import collect, today_hkt
from app.scorecard import run_scorecard_and_finetune
from app.predict import predict_all

HKT = timezone(timedelta(hours=8))
REPORT_PATH = ROOT / "data" / "daily_report.md"


def _fmt_pct(x) -> str:
    if x is None:
        return "—"
    return f"{float(x) * 100:.1f}%"


def _param_diff_lines(before: dict, after: dict) -> list[str]:
    lines = []
    keys = sorted(set(before) | set(after))
    for k in keys:
        b, a = before.get(k), after.get(k)
        if b is None or a is None:
            continue
        try:
            if abs(float(a) - float(b)) > 1e-9:
                lines.append(f"- `{k}`: {b} → {a}")
        except (TypeError, ValueError):
            if a != b:
                lines.append(f"- `{k}`: {b} → {a}")
    if not lines:
        lines.append("- （今次無參數變更）")
    return lines


def write_report(summary: dict) -> None:
    learn = summary.get("learning") or {}
    before = learn.get("params_before") or {}
    after = learn.get("params_after") or {}
    sc = summary.get("scorecard") or {}
    qs = summary.get("qs_finetune") or {}
    now = datetime.now(HKT).strftime("%Y-%m-%d %H:%M 香港時間")
    body = f"""# 每日更新報告

- **產生時間**：{now}
- **收集視窗**：{summary.get('date_from')} → {summary.get('date_to')}（香港時間今日 ± 視窗）

## 資料

| 項目 | 數值 |
|------|------|
| 原始賽事（來源） | {summary.get('raw_matches')} |
| 允許聯賽寫入 | {summary.get('upserted')} |
| Elo 完場場次 | {summary.get('elo_finished')} |
| 預測場次 | {summary.get('predictions')} |
| 資料庫比賽總數 | {summary.get('db_matches')} |

## 計分卡（預測對照）

| 項目 | 數值 |
|------|------|
| 完場樣本 n | {sc.get('n')} |
| 1X2 命中率 | {_fmt_pct(sc.get('hit_1x2_rate'))} |
| 正確比分命中率 | {_fmt_pct(sc.get('hit_score_rate'))} |
| 平均 Brier | {sc.get('mean_brier')}（基線 {sc.get('baseline_brier')}） |
| 平均 log loss | {sc.get('mean_logloss')} |

## 學習結果

| 項目 | 數值 |
|------|------|
| 完場樣本 n | {learn.get('finished_n')} |
| 勝負命中率 | {_fmt_pct(learn.get('result_acc'))} |
| 正確比分命中率 | {_fmt_pct(learn.get('exact_acc'))} |
| Brier（已存預測） | {learn.get('brier')} |
| 評估 Brier（學習前→後） | {learn.get('eval_brier_before')} → {learn.get('eval_brier_after')} |
| 備註 | {learn.get('notes') or '—'} |

### 參數變更

{chr(10).join(_param_diff_lines(before, after))}

## Quant-Striker 微調

| 項目 | 數值 |
|------|------|
| Brier 前→後 | {qs.get('brier_before')} → {qs.get('brier_after')} |
| 有否更新權重 | {qs.get('changed')} |
| 備註 | {qs.get('notes') or '—'} |

---
僅供統計估計，並非博彩建議。
"""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(body, encoding="utf-8")


def _merge_dupes() -> dict:
    try:
        from scripts.merge_duplicates import main as merge_main
        # merge_duplicates may expose main; fall back to runpy
    except Exception:
        merge_main = None
    try:
        import runpy
        ns = runpy.run_path(str(ROOT / "scripts" / "merge_duplicates.py"))
        if callable(ns.get("main")):
            try:
                ns["main"]()
            except TypeError:
                pass
        return {"ok": True}
    except Exception as e:
        print("merge_duplicates skipped:", e)
        return {"ok": False, "error": str(e)}


def main() -> int:
    os.environ.setdefault("FOOTBALL_SKIP_WEATHER", "1")
    db.init_db()

    t = today_hkt()
    start, end = t - timedelta(days=4), t + timedelta(days=4)
    matches, meta = collect(start, end)
    upserted = db.upsert_matches(matches)

    merge_meta = _merge_dupes()

    # scorecard → learn (Elo/Poisson grid) → QS fine-tune
    pipeline = run_scorecard_and_finetune(recent_n=200)
    learning = pipeline.get("learning") or {}
    scorecard = pipeline.get("scorecard") or {}
    qs_finetune = pipeline.get("qs_finetune") or {}

    rss_meta = None
    try:
        from app.consensus import fetch_sports_rss
        rss_meta = fetch_sports_rss(force=False)
    except Exception as e:
        rss_meta = {"ok": False, "error": str(e)}

    # predict_all after learning / QS tune so new params apply
    predicted = predict_all(limit=500)
    counts = db.counts()
    accuracy = db.accuracy_stats()

    summary = {
        "ok": True,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "raw_matches": meta.get("match_count"),
        "upserted": upserted,
        "merge": merge_meta,
        "elo_finished": learning.get("elo_finished"),
        "predictions": predicted,
        "db_matches": counts.get("matches"),
        "accuracy": accuracy,
        "scorecard": scorecard,
        "qs_finetune": {
            "brier_before": qs_finetune.get("brier_before"),
            "brier_after": qs_finetune.get("brier_after"),
            "changed": qs_finetune.get("changed"),
            "notes": qs_finetune.get("notes"),
            "weights_before": qs_finetune.get("weights_before"),
            "weights_after": qs_finetune.get("weights_after"),
            "ran_at": qs_finetune.get("ran_at"),
        },
        "learning": {
            "run_id": learning.get("run_id"),
            "finished_n": learning.get("finished_n"),
            "result_acc": learning.get("result_acc"),
            "exact_acc": learning.get("exact_acc"),
            "brier": learning.get("brier"),
            "eval_brier_before": learning.get("eval_brier_before"),
            "eval_brier_after": learning.get("eval_brier_after"),
            "params_before": learning.get("params_before"),
            "params_after": learning.get("params_after"),
            "notes": learning.get("notes"),
            "ran_at": learning.get("ran_at"),
        },
        "report_path": str(REPORT_PATH),
        "sources_notes": meta.get("sources_notes"),
        "rss": rss_meta,
    }
    write_report(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
