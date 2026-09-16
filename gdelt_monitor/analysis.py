from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .collector import QueryTask, stable_id
from .database import NewsDatabase


@dataclass(frozen=True)
class CompanyAlias:
    company_id: str
    company_name: str
    ticker: str
    alias: str
    normalized_alias: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"株式会社|有限会社|ホールディングス|holdings?|corporation|corp\.?|inc\.?|ltd\.?", "", value)
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", value)


def _ticker_key(value: str) -> str:
    value = str(value or "").strip().upper()
    if value.endswith(".T"):
        value = value[:-2]
    return value[:4] if len(value) >= 4 else value


def _market_cap_names(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    result: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = _ticker_key(row.get("銘柄") or row.get("入力値") or "")
            name = str(row.get("名称") or "").strip()
            if ticker and name and ticker not in result:
                result[ticker] = name
    return result


def load_company_aliases(path: Path, market_cap_snapshot: Path | None = None) -> list[CompanyAlias]:
    aliases: list[CompanyAlias] = []
    english_names = _market_cap_names(market_cap_snapshot)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("target", "1")).strip() not in {"1", "true", "True"}:
                continue
            company_id = str(row.get("company_id") or "").strip()
            company_name = str(row.get("company_name") or "").strip()
            ticker = str(row.get("ticker") or "").strip()
            values = [company_name, english_names.get(_ticker_key(ticker), "")]
            for field in ("aliases", "subsidiaries"):
                values.extend(part.strip() for part in str(row.get(field) or "").split("|") if part.strip())
            seen: set[str] = set()
            for alias in values:
                normalized = normalize_text(alias)
                if len(normalized) < 4 or normalized in seen:
                    continue
                seen.add(normalized)
                aliases.append(CompanyAlias(company_id, company_name, ticker, alias, normalized))
    return aliases


def canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    query = urlencode(
        [(key, val) for key, val in parse_qsl(parsed.query) if not key.lower().startswith("utm_")]
    )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, ""))


def save_articles(
    database: NewsDatabase,
    run_id: str,
    task: QueryTask,
    articles: list[dict[str, Any]],
) -> tuple[int, int, list[str]]:
    inserted = 0
    duplicates = 0
    article_ids: list[str] = []
    timestamp = now_iso()
    with database.connection() as connection:
        for article in articles:
            original_url = str(article.get("url") or "").strip()
            title = str(article.get("title") or "").strip()
            if not original_url and not title:
                continue
            clean_url = canonical_url(original_url) if original_url else ""
            if article.get("_dedupe_by_title") and title:
                article_id = stable_id("ngram-title", normalize_text(title))
            else:
                article_id = stable_id(clean_url or title, str(article.get("seendate") or ""))
            existed = connection.execute(
                "SELECT 1 FROM articles WHERE article_id=?", (article_id,)
            ).fetchone() is not None
            connection.execute(
                """INSERT INTO articles(
                   article_id,canonical_url,original_url,title,seen_date,domain,source_country,
                   source_language,tone,raw_json,first_seen_at,last_seen_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(article_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (
                    article_id, clean_url, original_url, title,
                    str(article.get("seendate") or ""), str(article.get("domain") or ""),
                    str(article.get("sourcecountry") or ""), str(article.get("language") or ""),
                    _float_or_none(article.get("tone")),
                    json.dumps(article, ensure_ascii=False, separators=(",", ":")),
                    timestamp, timestamp,
                ),
            )
            connection.execute(
                """INSERT OR IGNORE INTO article_discoveries(
                   article_id,query_id,query_name,profile,severity,discovered_at
                   ) VALUES(?,?,?,?,?,?)""",
                (article_id, task.query_id, task.name, task.profile, task.severity, timestamp),
            )
            if existed:
                duplicates += 1
            else:
                inserted += 1
                article_ids.append(article_id)
    return inserted, duplicates, article_ids


def match_and_score(
    database: NewsDatabase,
    article_ids: list[str],
    aliases: list[CompanyAlias],
    config: dict[str, Any],
) -> tuple[int, int, list[dict[str, Any]]]:
    matched_count = 0
    timestamp = now_iso()
    candidates: list[dict[str, Any]] = []
    positive = [normalize_text(x) for x in config.get("positive_keywords", [])]
    negative = [normalize_text(x) for x in config.get("negative_keywords", [])]
    with database.connection() as connection:
        for article_id in dict.fromkeys(article_ids):
            article = connection.execute("SELECT * FROM articles WHERE article_id=?", (article_id,)).fetchone()
            if article is None:
                continue
            normalized_title = normalize_text(article["title"])
            company_rows: list[CompanyAlias] = []
            used_companies: set[str] = set()
            try:
                embedded_matches = json.loads(article["raw_json"] or "{}").get("_company_matches", [])
            except (AttributeError, json.JSONDecodeError):
                embedded_matches = []
            for item in embedded_matches:
                if not isinstance(item, dict):
                    continue
                company_id = str(item.get("company_id") or "").strip()
                if not company_id or company_id in used_companies:
                    continue
                alias = CompanyAlias(
                    company_id=company_id,
                    company_name=str(item.get("company_name") or "").strip(),
                    ticker=str(item.get("ticker") or "").strip(),
                    alias=str(item.get("alias") or "").strip(),
                    normalized_alias=normalize_text(str(item.get("alias") or "")),
                )
                used_companies.add(company_id)
                company_rows.append(alias)
            for alias in aliases:
                if alias.company_id in used_companies or alias.normalized_alias not in normalized_title:
                    continue
                used_companies.add(alias.company_id)
                company_rows.append(alias)
            for alias in company_rows:
                connection.execute(
                    """INSERT OR IGNORE INTO article_company_matches(
                       article_id,company_id,company_name,ticker,alias,confidence,matched_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (article_id, alias.company_id, alias.company_name, alias.ticker, alias.alias, 1.0, timestamp),
                )
            matched_count += len(company_rows)
            discovery = connection.execute(
                "SELECT MAX(severity) severity, COUNT(*) discoveries FROM article_discoveries WHERE article_id=?",
                (article_id,),
            ).fetchone()
            score = float(discovery["severity"] or 0)
            reasons = [f"query_severity={int(score)}"]
            if company_rows:
                score += 25
                reasons.append("company_name_match=25")
            if int(discovery["discoveries"] or 0) > 1:
                score += 5
                reasons.append("multiple_query_match=5")
            positive_hit = any(word and word in normalized_title for word in positive)
            negative_hit = any(word and word in normalized_title for word in negative)
            if positive_hit or negative_hit:
                score += 10
                reasons.append("material_keyword=10")
            direction = "mixed" if positive_hit and negative_hit else "positive" if positive_hit else "negative" if negative_hit else "unknown"
            score = min(100.0, score)
            connection.execute(
                """INSERT INTO article_scores(article_id,score,direction,reasons_json,scored_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(article_id) DO UPDATE SET
                   score=excluded.score,direction=excluded.direction,reasons_json=excluded.reasons_json,
                   scored_at=excluded.scored_at""",
                (article_id, score, direction, json.dumps(reasons, ensure_ascii=False), timestamp),
            )
            if score >= float(config.get("digest_score_threshold", 40)):
                candidates.append(
                    {
                        "article_id": article_id,
                        "title": article["title"], "url": article["original_url"],
                        "domain": article["domain"], "seen_date": article["seen_date"],
                        "score": score, "direction": direction,
                        "companies": [row.company_name for row in company_rows],
                        "reasons": reasons,
                    }
                )
    candidates.sort(key=lambda item: (-item["score"], item["seen_date"]), reverse=False)
    return matched_count, len(candidates), candidates


def load_candidates_since(
    database: NewsDatabase,
    since: str,
    threshold: float,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with database.connection() as connection:
        rows = connection.execute(
            """SELECT a.article_id,a.title,a.original_url,a.domain,a.source_language,a.seen_date,
                      s.score,s.direction,s.reasons_json,
                      g.importance AS gpt_importance,g.summary AS gpt_summary,g.risk AS gpt_risk,
                      r.importance AS gemini_importance,r.summary AS gemini_summary,r.risk AS gemini_risk,
                      GROUP_CONCAT(DISTINCT m.company_name) AS company_names
               FROM articles a
               JOIN article_scores s ON s.article_id=a.article_id
               LEFT JOIN ai_reviews g ON g.article_id=a.article_id AND g.provider='openai'
               LEFT JOIN ai_reviews r ON r.article_id=a.article_id AND r.provider='gemini'
               LEFT JOIN article_company_matches m ON m.article_id=a.article_id
               WHERE a.first_seen_at>=? AND s.score>=?
               GROUP BY a.article_id
               ORDER BY s.score DESC,a.seen_date DESC
               LIMIT ?""",
            (since, threshold, limit),
        ).fetchall()
    return [
        {
            "article_id": row["article_id"], "title": row["title"],
            "url": row["original_url"], "domain": row["domain"],
            "source_language": row["source_language"],
            "seen_date": row["seen_date"], "score": float(row["score"]),
            "direction": row["direction"],
            "gpt_importance": row["gpt_importance"],
            "gpt_summary": row["gpt_summary"] or "",
            "gpt_risk": row["gpt_risk"] or "",
            "gemini_importance": row["gemini_importance"],
            "gemini_summary": row["gemini_summary"] or "",
            "gemini_risk": row["gemini_risk"] or "",
            "companies": [name for name in str(row["company_names"] or "").split(",") if name],
            "reasons": json.loads(row["reasons_json"] or "[]"),
        }
        for row in rows
    ]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
