# 每日更新報告

- **產生時間**：2026-09-06 09:20 香港時間
- **收集視窗**：2026-09-02 → 2026-09-10（香港時間今日 ± 視窗）

## 資料

| 項目 | 數值 |
|------|------|
| 原始賽事（來源） | 102 |
| 允許聯賽寫入 | 75 |
| Elo 完場場次 | 34 |
| 預測場次 | 89 |
| 資料庫比賽總數 | 89 |

## 計分卡（預測對照）

| 項目 | 數值 |
|------|------|
| 完場樣本 n | 34 |
| 1X2 命中率 | 44.1% |
| 正確比分命中率 | 2.9% |
| 平均 Brier | 0.619（基線 0.6667） |
| 平均 log loss | 1.0333 |

## 學習結果

| 項目 | 數值 |
|------|------|
| 完場樣本 n | 34 |
| 勝負命中率 | 44.1% |
| 正確比分命中率 | 2.9% |
| Brier（已存預測） | 0.619 |
| 評估 Brier（學習前→後） | 0.6385 → 0.6324 |
| 備註 | updated: home_adv_elo, home_adv_goals, avg_goals, calibration_home, calibration_draw, calibration_away; eval_brier 0.6385→0.6324 on n=34; rebuilt Elo after HA/K change |

### 參數變更

- `avg_goals`: 1.32 → 1.22
- `calibration_away`: 1.12 → 1.0944
- `calibration_draw`: 1.0264 → 1.0674
- `calibration_home`: 0.9171 → 0.8739
- `home_adv_elo`: 44.0 → 40.0
- `home_adv_goals`: 0.07 → 0.05

## Quant-Striker 微調

| 項目 | 數值 |
|------|------|
| Brier 前→後 | 0.6248 → 0.6225 |
| 有否更新權重 | True |
| 備註 | QS fine-tune improved: form30→0.85 (brier 0.6248), form30→0.75 (brier 0.6248), form30→0.70 (brier 0.6248), commercial→1.35 (brier 0.6248), commercial→1.45 (brier 0.6248), commercial→1.60 (brier 0.6247), stakes→1.05 (brier 0.6247), stakes→1.15 (brier 0.6247), stakes→1.30 (brier 0.6246), marketBlendWeight→0.85 (brier 0.6240), marketBlendWeight→0.95 (brier 0.6230), marketBlendWeight→1.00 (brier 0.6225); brier 0.6248→0.6225 on n=34 evals=36 |

---
僅供統計估計，並非博彩建議。
