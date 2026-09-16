from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable


WINDOWS = (1, 5, 20, 60, 120, 250)
MODEL_NAME = "patent_60d_excess_upside_v1"
FEATURES = (
    "technology_score", "metadata_score", "novelty_score", "materiality_score", "final_score",
    "gpt_importance", "gpt_short_term", "gpt_long_term", "gpt_novelty",
    "gemini_importance", "gemini_short_term", "gemini_long_term", "gemini_novelty",
    "log_market_cap",
)


def normalize_publication_date(value: str) -> str:
    text = str(value or "").strip()
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ""


def window_is_mature(publication_date: str, window_days: int, today: date | None = None) -> bool:
    normalized = normalize_publication_date(publication_date)
    if not normalized:
        return False
    try:
        publication_day = date.fromisoformat(normalized)
    except ValueError:
        return False
    current = today or date.today()
    age_days = max(0, (current - publication_day).days)
    return age_days >= math.ceil(window_days * 7 / 5) + 3


def yahoo_ticker(raw: str) -> str:
    value = str(raw or "").strip().upper()
    if not value:
        return ""
    if value.isdigit() and len(value) in {4, 5}:
        return value[:4] + ".T"
    return value


def _json(value: str) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def _score(data: dict[str, Any], key: str) -> float:
    try:
        return float(data.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def feature_vector(row: sqlite3.Row, market_cap: float = 0.0) -> list[float]:
    gpt = _json(row["gpt_json"])
    gemini = _json(row["gemini_json"])
    return [
        float(row["technology_score"] or 0), float(row["metadata_score"] or 0),
        float(row["novelty_score"] or 0), float(row["materiality_score"] or 0),
        float(row["final_score"] or 0),
        _score(gpt, "importance_score"), _score(gpt, "short_term_market_impact_score"),
        _score(gpt, "long_term_business_value_score"), _score(gpt, "novelty_score"),
        _score(gemini, "importance_score"), _score(gemini, "short_term_market_impact_score"),
        _score(gemini, "long_term_business_value_score"), _score(gemini, "novelty_score"),
        math.log10(max(1.0, market_cap)),
    ]


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def predict(model: dict[str, Any], values: list[float]) -> float:
    means = model.get("means", [])
    scales = model.get("scales", [])
    weights = model.get("weights", [])
    if not (len(values) == len(means) == len(scales) == len(weights)):
        return 0.0
    standardized = [(x - m) / s for x, m, s in zip(values, means, scales)]
    linear = float(model.get("intercept", 0)) + sum(w * x for w, x in zip(weights, standardized))
    return _sigmoid(linear)


def train_logistic(samples: list[list[float]], labels: list[int]) -> dict[str, Any]:
    count = len(samples)
    width = len(FEATURES)
    means = [sum(row[j] for row in samples) / count for j in range(width)]
    scales = []
    for j in range(width):
        variance = sum((row[j] - means[j]) ** 2 for row in samples) / count
        scales.append(max(math.sqrt(variance), 1.0))
    x = [[(row[j] - means[j]) / scales[j] for j in range(width)] for row in samples]
    positive_rate = min(0.99, max(0.01, sum(labels) / count))
    intercept = math.log(positive_rate / (1.0 - positive_rate))
    weights = [0.0] * width
    learning_rate = 0.08
    regularization = 0.04
    for _ in range(700):
        grad_b = 0.0
        grad_w = [0.0] * width
        for row, label in zip(x, labels):
            error = _sigmoid(intercept + sum(a * b for a, b in zip(weights, row))) - label
            grad_b += error
            for j, value in enumerate(row):
                grad_w[j] += error * value
        intercept -= learning_rate * grad_b / count
        for j in range(width):
            weights[j] -= learning_rate * (grad_w[j] / count + regularization * weights[j])
    probabilities = [_sigmoid(intercept + sum(a * b for a, b in zip(weights, row))) for row in x]
    accuracy = sum((p >= 0.5) == bool(y) for p, y in zip(probabilities, labels)) / count
    return {
        "model_name": MODEL_NAME, "features": list(FEATURES), "means": means, "scales": scales,
        "weights": weights, "intercept": intercept, "sample_count": count,
        "positive_count": sum(labels), "positive_rate": positive_rate,
        "training_accuracy": accuracy, "created_at": datetime.now().isoformat(timespec="seconds"),
        "target": "60取引日後のTOPIX連動ETF超過収益が5%以上",
    }


@dataclass
class FeedbackResult:
    event_count: int
    outcome_count: int
    fetched_ticker_count: int
    model_sample_count: int
    model_created: bool
    output_csv: str
    model_json: str


class MarketFeedbackService:
    def __init__(self, database_path: str | Path, companies: list[Any], output_dir: str | Path):
        self.database_path = Path(database_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.companies = {company.company_id: company for company in companies}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_outcomes (
                patent_id TEXT, company_id TEXT, ticker TEXT, publication_date TEXT, window_days INTEGER,
                base_date TEXT, target_date TEXT, base_price REAL, target_price REAL,
                stock_return REAL, benchmark_return REAL, excess_return REAL, updated_at TEXT,
                PRIMARY KEY(patent_id, window_days)
            );
            CREATE TABLE IF NOT EXISTS learning_models (
                model_name TEXT PRIMARY KEY, created_at TEXT, sample_count INTEGER, model_json TEXT
            );
            """
        )
        connection.commit()
        return connection

    @staticmethod
    def _prices(frame: Any, ticker: str) -> list[tuple[str, float]]:
        if frame is None or getattr(frame, "empty", True):
            return []
        series = None
        try:
            if getattr(frame.columns, "nlevels", 1) > 1:
                series = frame["Close"][ticker]
            else:
                series = frame["Close"]
        except (KeyError, TypeError):
            return []
        result = []
        for index, value in series.dropna().items():
            try:
                result.append((index.date().isoformat(), float(value)))
            except (AttributeError, TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _on_or_after(prices: list[tuple[str, float]], target: str) -> int | None:
        for index, (day, _price) in enumerate(prices):
            if day >= target:
                return index
        return None

    def run(self, benchmark: str = "1306.T", progress: Callable[[str, float], None] | None = None) -> FeedbackResult:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("yfinanceが必要です。pip install -U yfinance を一度実行してください") from exc
        notify = progress or (lambda _message, _ratio: None)
        connection = self._connect()
        events = connection.execute(
            """SELECT p.patent_id,p.company_id,p.publication_date,e.* FROM patents p
               JOIN evaluations e ON e.patent_id=p.patent_id
               WHERE p.company_id<>'' AND p.publication_date<>''
               AND e.created_at=(SELECT MAX(e2.created_at) FROM evaluations e2 WHERE e2.patent_id=e.patent_id)
               ORDER BY p.publication_date"""
        ).fetchall()
        prepared = []
        existing = {
            (row[0], int(row[1]))
            for row in connection.execute("SELECT patent_id,window_days FROM market_outcomes")
        }
        for row in events:
            company = self.companies.get(row["company_id"])
            ticker = yahoo_ticker(company.ticker if company else "")
            publication_date = normalize_publication_date(row["publication_date"])
            if not publication_date:
                continue
            # Only download when at least one unresolved trading-day window can have matured.
            eligible = any(
                (row["patent_id"], window) not in existing
                and window_is_mature(publication_date, window)
                for window in WINDOWS
            )
            if ticker and eligible:
                prepared.append((row, company, ticker, publication_date))
        if not prepared:
            rows = connection.execute("SELECT * FROM market_outcomes ORDER BY publication_date,patent_id,window_days").fetchall()
            # All currently observable windows are cached or not mature yet; refresh exports without network access.
        tickers: list[str] = []
        price_map: dict[str, list[tuple[str, float]]] = {}
        benchmark_prices: list[tuple[str, float]] = []
        if prepared:
            start = min(publication_date for _row, _company, _ticker, publication_date in prepared)
            end = (date.today() + timedelta(days=2)).isoformat()
            tickers = sorted({ticker for _row, _company, ticker, _publication_date in prepared} | {benchmark})
            notify(f"差分株価取得中: {len(tickers)}銘柄 {start}～{end}", 0.08)
            frame = yf.download(
                tickers, start=start, end=end, auto_adjust=True, progress=False,
                group_by="column", threads=True, timeout=30,
            )
            price_map = {ticker: self._prices(frame, ticker) for ticker in tickers}
            benchmark_prices = price_map.get(benchmark, [])
            missing_tickers = [ticker for ticker in tickers if not price_map.get(ticker)]
            try:
                if not benchmark_prices:
                    raise RuntimeError(f"株価取得失敗: ベンチマーク{benchmark}の価格が0件です")
                if not any(price_map.get(ticker) for ticker in tickers if ticker != benchmark):
                    raise RuntimeError("株価取得失敗: 対象銘柄の価格がすべて0件です")
            except RuntimeError:
                connection.close()
                raise
            if missing_tickers:
                notify(
                    f"株価取得警告: {len(missing_tickers)}/{len(tickers)}銘柄が0件 "
                    + ",".join(missing_tickers[:10]),
                    0.09,
                )
        now = datetime.now().isoformat(timespec="seconds")
        written = 0
        for event_index, (row, _company, ticker, publication_date) in enumerate(prepared, 1):
            prices = price_map.get(ticker, [])
            base_index = self._on_or_after(prices, publication_date)
            benchmark_base = self._on_or_after(benchmark_prices, publication_date)
            if base_index is None or benchmark_base is None:
                continue
            base_date, base_price = prices[base_index]
            for window in WINDOWS:
                if (row["patent_id"], window) in existing:
                    continue
                target_index = base_index + window
                benchmark_target = benchmark_base + window
                if target_index >= len(prices) or benchmark_target >= len(benchmark_prices):
                    continue
                target_date, target_price = prices[target_index]
                _benchmark_base_date, benchmark_base_price = benchmark_prices[benchmark_base]
                _benchmark_target_date, benchmark_target_price = benchmark_prices[benchmark_target]
                stock_return = target_price / base_price - 1.0
                benchmark_return = benchmark_target_price / benchmark_base_price - 1.0
                connection.execute(
                    """INSERT OR REPLACE INTO market_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["patent_id"], row["company_id"], ticker, publication_date, window,
                     base_date, target_date, base_price, target_price, stock_return, benchmark_return,
                     stock_return - benchmark_return, now),
                )
                written += 1
            notify(f"答え合わせ {event_index}/{len(prepared)}: {ticker}", 0.1 + 0.65 * event_index / len(prepared))
        connection.commit()
        training_rows = connection.execute(
            """SELECT e.*,o.excess_return,p.publication_date FROM evaluations e
               JOIN market_outcomes o ON o.patent_id=e.patent_id AND o.window_days=60
               JOIN patents p ON p.patent_id=e.patent_id
               WHERE e.created_at=(SELECT MAX(e2.created_at) FROM evaluations e2 WHERE e2.patent_id=e.patent_id)
               ORDER BY p.publication_date"""
        ).fetchall()
        samples, labels = [], []
        for row in training_rows:
            company = self.companies.get(row["company_id"])
            samples.append(feature_vector(row, company.market_cap_jpy if company else 0.0))
            labels.append(int(float(row["excess_return"]) >= 0.05))
        model = None
        if len(samples) >= 30 and 2 <= sum(labels) <= len(labels) - 2:
            validation_count = max(6, len(samples) // 5)
            split = len(samples) - validation_count
            validation_model = train_logistic(samples[:split], labels[:split])
            validation_probabilities = [predict(validation_model, row) for row in samples[split:]]
            validation_labels = labels[split:]
            validation_accuracy = sum(
                (probability >= 0.5) == bool(label)
                for probability, label in zip(validation_probabilities, validation_labels)
            ) / validation_count
            baseline_class = int(sum(labels[:split]) >= split / 2)
            baseline_accuracy = sum(label == baseline_class for label in validation_labels) / validation_count
            validation_brier = sum(
                (probability - label) ** 2
                for probability, label in zip(validation_probabilities, validation_labels)
            ) / validation_count
            model = train_logistic(samples, labels)
            model.update({
                "validation_method": "publication_date_ordered_last_20_percent",
                "validation_count": validation_count,
                "validation_accuracy": validation_accuracy,
                "validation_baseline_accuracy": baseline_accuracy,
                "validation_brier_score": validation_brier,
            })
            connection.execute(
                "INSERT OR REPLACE INTO learning_models VALUES(?,?,?,?)",
                (MODEL_NAME, model["created_at"], model["sample_count"], json.dumps(model, ensure_ascii=False)),
            )
            connection.commit()
        rows = connection.execute(
            "SELECT * FROM market_outcomes ORDER BY publication_date,patent_id,window_days"
        ).fetchall()
        output_csv = self.output_dir / "patent_market_outcomes.csv"
        with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(rows[0].keys() if rows else ["patent_id"])
            writer.writerows(tuple(row) for row in rows)
        model_path = self.output_dir / "patent_learning_model.json"
        model_path.write_text(json.dumps(model or {
            "status": "insufficient_data", "sample_count": len(samples), "minimum_required": 30,
            "target": "60取引日後のTOPIX連動ETF超過収益が5%以上",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        connection.close()
        notify("株価答え合わせ・学習が完了しました", 1.0)
        return FeedbackResult(
            event_count=len(prepared), outcome_count=written, fetched_ticker_count=len(tickers),
            model_sample_count=len(samples), model_created=model is not None,
            output_csv=str(output_csv), model_json=str(model_path),
        )


def load_latest_model(database_path: str | Path) -> dict[str, Any] | None:
    path = Path(database_path)
    if not path.exists():
        return None
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT model_json FROM learning_models WHERE model_name=?", (MODEL_NAME,)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        connection.close()
    return _json(row[0]) if row else None
