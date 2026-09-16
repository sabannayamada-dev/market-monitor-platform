from pathlib import Path

import numpy as np
import pandas as pd

from analyze_practical_us_targets import auc_score, numeric


INPUT = Path("outputs/us_stock_research/us_bottom_events_with_sec.csv")
OUTPUT = Path("outputs/us_stock_research/practical_target_driver_validation.csv")

FEATURES = [
    "底検知時_SEC_企業価値EV",
    "底検知時_SEC_時価総額",
    "暴落開始時_SEC_営業利益_符号付きlog1p",
    "底検知時_SEC_設備投資額_直近年次_符号付きlog1p",
    "底検知時_SEC_流動資産総資産比率",
    "底検知時_SEC_インタレストカバレッジ",
    "底検知時_SEC_研究開発費売上比率_直近年次ベース",
    "ピーク時_SEC_研究開発費売上比率_直近年次ベース",
]


def main() -> None:
    frame = pd.read_csv(INPUT, encoding="utf-8-sig", low_memory=False)
    dates = pd.to_datetime(frame["底打ち候補日"], errors="coerce")
    upside = numeric(frame["1年内最大上昇率"])
    drawdown = numeric(frame["1年内最大下落率"])
    targets = {
        "1年内大化け50%": pd.Series(
            np.where(upside.notna(), (upside >= 50).astype(float), np.nan), index=frame.index
        ),
        "大化け深傷回避": pd.Series(
            np.where(
                upside.notna() & drawdown.notna(),
                ((upside >= 50) & (drawdown > -35)).astype(float),
                np.nan,
            ),
            index=frame.index,
        ),
    }
    rows = []
    for target_name, target in targets.items():
        for feature in FEATURES:
            if feature not in frame.columns:
                continue
            values = numeric(frame[feature])
            valid = (dates.dt.year >= 2023) & target.notna() & values.notna()
            if valid.sum() < 50 or values[valid].nunique() < 5:
                continue
            x = values[valid]
            y = target[valid].astype(int)
            low_threshold, high_threshold = x.quantile([0.2, 0.8])
            low = x <= low_threshold
            high = x >= high_threshold
            auc = auc_score(y.to_numpy(), x.to_numpy())
            rows.append(
                {
                    "目的指標": target_name,
                    "パラメータ": feature,
                    "有効件数": int(valid.sum()),
                    "単変量AUC": auc,
                    "値が大きい場合の方向": "押し上げ" if auc > 0.5 else "押し下げ",
                    "下位20%実現率": y[low].mean(),
                    "上位20%実現率": y[high].mean(),
                    "上位マイナス下位": y[high].mean() - y[low].mean(),
                }
            )
    result = pd.DataFrame(rows)
    result.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
