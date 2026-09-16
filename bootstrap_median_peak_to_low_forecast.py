from pathlib import Path

import numpy as np
import pandas as pd

from analyze_practical_us_targets import correlation, rankdata


INPUT = Path("outputs/us_stock_research/median_peak_to_low_forecast_predictions.csv")
OUTPUT = Path("outputs/us_stock_research/median_peak_to_low_forecast_bootstrap.csv")
N_BOOTSTRAP = 3000


def metric(frame: pd.DataFrame) -> dict:
    actual = pd.to_numeric(frame["判定前最高値→候補後最安値下落率"], errors="coerce").to_numpy()
    predicted = pd.to_numeric(frame["予測_50pct下落率"], errors="coerce").to_numpy()
    baseline = pd.to_numeric(frame["単純中央値予測下落率"], errors="coerce").to_numpy()
    model_mae = np.mean(np.abs(actual - predicted))
    baseline_mae = np.mean(np.abs(actual - baseline))
    low = frame["予測危険度五分位"].to_numpy() == 1
    high = frame["予測危険度五分位"].to_numpy() == 5
    return {
        "予測より深く下落した割合": np.mean(actual < predicted),
        "Spearman相関": correlation(rankdata(actual), rankdata(predicted)),
        "MAE改善率_pct": (1 - model_mae / baseline_mae) * 100,
        "最危険分位マイナス低危険分位_実際平均下落率差": actual[high].mean() - actual[low].mean(),
    }


def main() -> None:
    frame = pd.read_csv(INPUT, encoding="utf-8-sig")
    observed = metric(frame)
    groups = [group.index.to_numpy() for _, group in frame.groupby("銘柄", sort=False)]
    rng = np.random.default_rng(20260713)
    values = {key: [] for key in observed}
    for _ in range(N_BOOTSTRAP):
        sampled = rng.integers(0, len(groups), len(groups))
        indices = np.concatenate([groups[index] for index in sampled])
        result = metric(frame.loc[indices])
        for key, value in result.items():
            values[key].append(value)
    rows = []
    for key, point in observed.items():
        low, high = np.quantile(values[key], [0.025, 0.975])
        rows.append({"指標": key, "推定値": point, "銘柄クラスタ95%下限": low, "銘柄クラスタ95%上限": high})
    output = pd.DataFrame(rows)
    output.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print(output.to_string(index=False))


if __name__ == "__main__":
    main()
