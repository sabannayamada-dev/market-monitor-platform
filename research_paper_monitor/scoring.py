from __future__ import annotations

import re
from typing import Any

from .models import ScoredPaper


def _matches(text: str, terms: list[str]) -> list[str]:
    lowered = text.casefold()
    matched: list[str] = []
    for term in terms:
        needle = str(term).strip().casefold()
        has_cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", needle))
        found = needle in lowered if has_cjk else bool(re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", lowered))
        if needle and found:
            matched.append(str(term))
    return matched


def score_paper(paper: dict[str, Any], profiles: list[dict[str, Any]]) -> ScoredPaper:
    best: ScoredPaper | None = None
    title = str(paper.get("title") or "")
    abstract = str(paper.get("abstract") or "")
    categories = " ".join(str(value) for value in paper.get("categories") or [])
    for profile in profiles:
        required_title = [str(x) for x in profile.get("required_title_keywords") or []]
        required_title_groups = [
            [str(x) for x in group] for group in profile.get("required_title_keyword_groups") or []
        ]
        configured_groups = profile.get("required_keyword_groups") or []
        if not configured_groups and profile.get("required_any_keywords"):
            configured_groups = [profile["required_any_keywords"]]
        required_groups = [[str(x) for x in group] for group in configured_groups]
        strong = [str(x) for x in profile.get("strong_keywords") or []]
        related = [str(x) for x in profile.get("related_keywords") or []]
        industry = [str(x) for x in profile.get("industry_keywords") or []]
        excluded = [str(x) for x in profile.get("exclude_keywords") or []]
        category_terms = [str(x) for x in profile.get("category_keywords") or []]
        title_requirement_met = not required_title or bool(_matches(title, required_title))
        title_groups_met = all(_matches(title, group) for group in required_title_groups)
        group_requirements_met = all(_matches(title + " " + abstract, group) for group in required_groups)
        if not title_requirement_met or not title_groups_met or not group_requirements_met:
            scored = ScoredPaper(
                paper_id=str(paper["paper_id"]), profile_id=str(profile["id"]),
                profile_label=str(profile.get("label") or profile["id"]),
                score=0.0, reasons=("必須の中心語なし",),
            )
            if best is None:
                best = scored
            continue
        strong_title = _matches(title, strong)
        strong_abstract = _matches(abstract, strong)
        related_title = _matches(title, related)
        related_abstract = _matches(abstract, related)
        industry_matches = _matches(title + " " + abstract, industry)
        category_matches = _matches(categories, category_terms)
        excluded_matches = _matches(title + " " + abstract, excluded)
        score = 15.0 if required_title or required_title_groups or required_groups else 0.0
        reasons: list[str] = ["研究室の中心テーマに一致"] if score else []
        if category_matches:
            score += 25
            reasons.append("分野一致: " + ", ".join(category_matches[:3]))
        if strong_title:
            score += min(25, 15 + 5 * len(strong_title))
            reasons.append("題名の強一致: " + ", ".join(strong_title[:4]))
        if paper.get("source") == "jstage" and not abstract and strong_title:
            score += 15
            reasons.append("J-STAGE題名一致（要約なし）")
        if strong_abstract:
            score += min(20, 8 + 3 * len(strong_abstract))
            reasons.append("要約の強一致: " + ", ".join(strong_abstract[:4]))
        if related_title:
            score += min(10, 5 + 2 * len(related_title))
            reasons.append("題名の関連語: " + ", ".join(related_title[:3]))
        if related_abstract:
            score += min(10, 3 + len(related_abstract))
        total_distinct = len(set(strong_title + strong_abstract + related_title + related_abstract))
        if total_distinct >= 3:
            score += 10
            reasons.append("複数の関連語が共起")
        if industry_matches:
            score += min(10, 4 + 2 * len(industry_matches))
            reasons.append("応用性: " + ", ".join(industry_matches[:3]))
        if abstract:
            score += 5
        if excluded_matches:
            score -= min(40, 20 + 5 * len(excluded_matches))
            reasons.append("除外語: " + ", ".join(excluded_matches[:3]))
        scored = ScoredPaper(
            paper_id=str(paper["paper_id"]), profile_id=str(profile["id"]),
            profile_label=str(profile.get("label") or profile["id"]),
            score=max(0.0, min(100.0, score)), reasons=tuple(reasons),
        )
        if best is None or scored.score > best.score:
            best = scored
    assert best is not None
    return best
