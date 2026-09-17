from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from monitor_core.mail import SMTPMailer, SMTPSettings

from .database import PaperDatabase


def build_digest(candidates: list[dict[str, Any]], stats: dict[str, int], window: str, errors: list[str]) -> tuple[str, str]:
    source_ok = stats.get("sources_succeeded", 0)
    source_total = stats.get("sources_attempted", 0)
    health = "✅正常" if source_total and source_ok == source_total and not errors else "⚠️一部失敗" if source_ok else "❌収集不成立"
    priority = [item for item in candidates if float(item["score"]) >= 70]
    normal = [item for item in candidates if float(item["score"]) < 70]
    subject = f"[最新論文] {datetime.now().date().isoformat()} 重点{len(priority)}件・通常{len(normal)}件 {health}"
    lines = ["最新論文モニター", "", f"状態: {health}", f"対象期間: {window}", ""]
    if errors:
        lines.extend(["【要確認】", *[f"- {error}" for error in errors[:20]], ""])
    for heading, items in (("重点論文", priority), ("通常候補", normal)):
        lines.append(f"【{heading}: {len(items)}件】")
        if not items:
            lines.append("該当なし")
        for index, item in enumerate(items, 1):
            title = item.get("japanese_title") or item["title"]
            authors = "、".join((item.get("authors") or [])[:3]) or "不明"
            if len(item.get("authors") or []) > 3:
                authors += " ほか"
            lines.extend([
                "", f"{index}. {title}", f"   原題: {item['title']}",
                f"   分野: {item['profile_label']}  ルール点: {float(item['score']):.0f}/100",
                f"   著者: {authors}", f"   公開日: {item.get('published_at') or '不明'}",
                f"   掲載先: {item.get('journal') or 'プレプリント／不明'}",
            ])
            if item.get("summary"):
                lines.append(f"   要約: {item['summary']}")
                lines.append(f"   新規性: {item.get('novelty') or '不明'}")
                lines.append(f"   応用先: {item.get('applications') or '不明'}")
                if item.get("caution"):
                    lines.append(f"   注意: {item['caution']}")
            else:
                lines.append("   AI要約: 未評価（ルール判定で掲載）")
            lines.append("   選出理由: " + "／".join((item.get("reasons") or [])[:4]))
            lines.append(f"   URL: {item.get('landing_url') or 'なし'}")
            if item.get("pdf_url"):
                lines.append(f"   PDF: {item['pdf_url']}")
    if not candidates:
        lines.extend(["", "正常に探索した範囲では、掲載基準を満たす新着論文はありませんでした。"])
    lines.extend([
        "", "=" * 60, "【運用・収集状況】",
        f"対象API: {source_total}", f"成功API: {source_ok}",
        f"取得: {stats.get('fetched', 0)}件", f"新規保存: {stats.get('inserted', 0)}件",
        f"既存・重複: {stats.get('duplicates', 0)}件", f"別APIとの統合: {stats.get('updated', 0)}件",
        f"採点: {stats.get('scored', 0)}件", f"掲載基準通過: {stats.get('candidates', 0)}件",
        f"AI送信: {stats.get('ai_sent', 0)}件", f"AI成功: {stats.get('ai_completed', 0)}件",
        f"AI失敗: {stats.get('ai_failed', 0)}件", f"AI上限による保留: {stats.get('ai_budget_skipped', 0)}件",
        f"HTTP 403: {stats.get('http_403', 0)}件", f"HTTP 406: {stats.get('http_406', 0)}件",
        f"HTTP 429: {stats.get('http_429', 0)}件",
        f"レート制限休止によるスキップ: {stats.get('cooldown_skipped', 0)}件",
        "情報提供元: J-STAGE（国立研究開発法人科学技術振興機構）",
        "J-STAGE: https://www.jstage.jst.go.jp/browse/-char/ja",
        "PDF本文は保存していません。リンク先の原文で内容を確認してください。",
    ])
    return subject, "\n".join(lines)


def queue_digest(database: PaperDatabase, key: str, subject: str, body: str, candidates: list[dict[str, Any]], now: str) -> None:
    database.queue_notification(key, subject, body, [item["paper_id"] for item in candidates], now)


def flush_outbox(database: PaperDatabase) -> tuple[int, list[str]]:
    sent = 0
    errors: list[str] = []
    mailer = SMTPMailer(SMTPSettings.from_env())
    for item in database.pending_notifications():
        paper_ids = json.loads(item["paper_ids_json"])
        try:
            mailer.send_text(item["subject"], item["body"])
            database.mark_notification_sent(item["notification_key"], paper_ids, datetime.now().astimezone().isoformat(timespec="seconds"))
            sent += 1
        except Exception as exc:
            message = f"mail {item['notification_key']}: {type(exc).__name__}: {exc}"
            database.mark_notification_failed(item["notification_key"], message)
            errors.append(message)
    return sent, errors
