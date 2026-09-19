from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from monitor_core.mail import SMTPMailer, SMTPSettings
from monitor_core.deepl_translation import translate_to_japanese

from .database import NewsDatabase


def _jst_time(value: Any) -> str:
    if not value:
        return "未定"
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.astimezone(ZoneInfo("Asia/Tokyo")).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return str(value)


def _source_language_label(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = raw.casefold()
    if normalized in {"ja", "jpn", "japanese"}:
        return "日本語"
    if normalized in {"en", "eng", "english"}:
        return "英語"
    return raw or "不明"


def health_label(stats: dict[str, int]) -> tuple[str, list[str]]:
    alerts: list[str] = []
    executed = stats.get("executed_queries", 0)
    success = stats.get("successful_queries", 0)
    if executed and success / executed < 0.8:
        alerts.append("成功クエリ率が80%未満です")
    if executed and stats.get("empty_responses", 0) / executed >= 0.2:
        alerts.append("空応答率が20%以上です")
    if stats.get("http_403", 0) or stats.get("http_429", 0):
        alerts.append("GDELTのアクセス制限を検知しました")
    if stats.get("non_json_responses", 0):
        alerts.append("JSON以外の応答が発生しました")
    if stats.get("timeouts", 0):
        alerts.append("タイムアウトが発生しました")
    if stats.get("carried_over", 0):
        alerts.append("次回持越し案件があります")
    if not executed:
        alerts.append("クエリが実行されていません")
    return ("✅ 正常" if not alerts else "❌ 収集不成立" if success == 0 else "⚠️ 一部失敗・要確認", alerts)


def build_digest(
    candidates: list[dict[str, Any]],
    stats: dict[str, int],
    window_label: str,
    error_messages: list[str] | None = None,
    adaptive: dict[str, Any] | None = None,
    world_news: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    world_news = world_news or []
    translated_titles = translate_to_japanese(
        item.get("title", "") for item in [*world_news, *candidates[:30]]
    )
    health, alerts = health_label(stats)
    subject = f"[GDELTニュース] {datetime.now().date().isoformat()} 重要{len(candidates)}件・世界{len(world_news)}件 {health.split()[0]}"
    lines = [
        "GDELTニュース材料性モニター", "", f"状態: {health}",
        f"集計対象: {window_label}", "",
    ]
    if alerts:
        lines.extend(["【要確認】", *[f"- {alert}" for alert in alerts], ""])
    if error_messages:
        lines.extend(["【直近のエラー内容】", *[f"- {message}" for message in error_messages], ""])
    lines.append(f"【今日の世界の重要ニュース: {len(world_news)}件】")
    if not world_news:
        lines.append("選定できる世界ニュース候補がありませんでした。")
    for index, item in enumerate(world_news, 1):
        title = item.get("japanese_title") or translated_titles.get(item.get("title", ""), item.get("title", ""))
        lines.extend([
            "", f"{index}. {title}",
            f"   分野: {item.get('topic') or '総合'}  世界的重要度: {float(item.get('importance', 0)):.0f}/100",
            f"   概要: {item.get('summary') or '見出し情報のみ'}",
            f"   選定理由: {item.get('reason') or '世界的な重要性を基に選定'}",
            f"   媒体: {item.get('domain') or '不明'}  掲載: {item.get('seen_date') or '不明'}",
            f"   URL: {item.get('url') or 'なし'}",
            "   注意: 株価との関連性は選定条件に含めていません。GDELTの見出し情報による選定です。",
        ])
        if title != item.get("title"):
            lines.insert(len(lines) - 5, f"   原題: {item.get('title')}")
    lines.append("")
    lines.append(f"【重要ニュース候補: {len(candidates)}件】")
    if not candidates:
        lines.append("正常に探索した範囲では、掲載基準を満たすニュースはありませんでした。")
    for index, item in enumerate(candidates[:30], 1):
        companies = "、".join(item.get("companies") or []) or "企業未特定"
        ai_provider = "Gemini" if item.get("gemini_summary") else "GPT" if item.get("gpt_summary") else ""
        ai_summary = item.get("gemini_summary") or item.get("gpt_summary") or ""
        ai_importance = item.get("gemini_importance") if item.get("gemini_summary") else item.get("gpt_importance")
        ai_risk = item.get("gemini_risk") or item.get("gpt_risk") or ""
        lines.extend(
            [
                "", f"{index}. {translated_titles.get(item['title'], item['title'])}",
                f"   企業: {companies}",
                f"   重要度: {item['score']:.1f} / 100  方向: {item['direction']}",
                f"   媒体: {item.get('domain') or '不明'}  原文言語: {_source_language_label(item.get('source_language'))}",
                f"   掲載: {item.get('seen_date') or '不明'}",
                f"   URL: {item.get('url') or 'なし'}",
                "   注意: GDELTのタイトル情報による候補です。元記事で事実確認してください。",
            ]
        )
        translated_title = translated_titles.get(item["title"], item["title"])
        if translated_title != item["title"]:
            lines.insert(len(lines) - 5, f"   原題: {item['title']}")
        if ai_summary:
            lines.append(f"   AI要約({ai_provider}・重要度{float(ai_importance or 0):.0f}): {ai_summary}")
        if ai_risk:
            lines.append(f"   AI注意点: {ai_risk}")
    lines.extend(
        [
            "", "=" * 60, "【運用・収集状況】",
            f"実行回数: {stats.get('run_count', 0)}",
            f"予定クエリ数: {stats.get('planned_queries', 0)}",
            f"実行クエリ数: {stats.get('executed_queries', 0)}",
            f"成功: {stats.get('successful_queries', 0)}",
            f"空応答: {stats.get('empty_responses', 0)}",
            f"非JSON応答: {stats.get('non_json_responses', 0)}",
            f"HTTP 403: {stats.get('http_403', 0)}",
            f"HTTP 429: {stats.get('http_429', 0)}",
            f"タイムアウト: {stats.get('timeouts', 0)}",
            f"その他エラー: {stats.get('other_errors', 0)}",
            "",
            f"探索記事数: {stats.get('fetched_articles', 0)}件",
            f"新規保存: {stats.get('inserted_articles', 0)}件",
            f"重複除外: {stats.get('duplicate_articles', 0)}件",
            f"企業照合: {stats.get('company_matches', 0)}件",
            f"掲載基準通過: {stats.get('scored_candidates', 0)}件",
            f"GPT送信: {stats.get('gpt_sent', 0)}件",
            f"GPT通過: {stats.get('gpt_passed', 0)}件",
            f"Gemini送信: {stats.get('gemini_sent', 0)}件",
            f"最終掲載: {stats.get('final_candidates', 0)}件",
            f"至急候補: {stats.get('urgent_candidates', 0)}件",
            f"未処理・次回持越し: {stats.get('carried_over', 0)}件",
        ]
    )
    if adaptive:
        lines.extend(
            [
                "", "【適応制御】",
                f"収集周期 x: {adaptive.get('adaptive_interval_minutes', '不明')}分",
                f"429休止時間 y: {adaptive.get('adaptive_cooldown_hours', '不明')}時間",
                f"クエリ間隔 z: {adaptive.get('adaptive_query_spacing_seconds', '不明')}秒",
                f"次回収集予定(JST): {_jst_time(adaptive.get('adaptive_next_collection_at'))}",
                f"休止終了予定(JST): {_jst_time(adaptive.get('adaptive_cooldown_until')) if adaptive.get('adaptive_cooldown_until') else '休止中ではありません'}",
                f"休止明け判定待ち: {'はい' if adaptive.get('adaptive_recovery_probe_pending') else 'いいえ'}",
                f"安定期間内の成功回数: {adaptive.get('adaptive_stable_successful_runs', 0)}回",
                f"直近の変更理由: {adaptive.get('adaptive_last_change_reason') or 'なし'}",
                f"安定域判定: {adaptive.get('adaptive_stability_status') or '観測中'}",
                f"安定域判定理由: {adaptive.get('adaptive_stability_reason') or '履歴不足'}",
                f"安定域観測: {adaptive.get('adaptive_stability_samples', 0)}回 / {adaptive.get('adaptive_stability_window_hours', 0)}時間",
            ]
        )
    return subject, "\n".join(lines)


def notification_sent(database: NewsDatabase, key: str) -> bool:
    with database.connection() as connection:
        return connection.execute(
            "SELECT 1 FROM notifications WHERE notification_key=?", (key,)
        ).fetchone() is not None


def send_digest(
    database: NewsDatabase,
    notification_key: str,
    subject: str,
    body: str,
    article_ids: list[str],
) -> None:
    SMTPMailer(SMTPSettings.from_env()).send_text(subject, body)
    with database.connection() as connection:
        connection.execute(
            "INSERT INTO notifications(notification_key,kind,sent_at,article_ids_json) VALUES(?,?,datetime('now'),?)",
            (notification_key, "daily_digest", json.dumps(article_ids, ensure_ascii=False)),
        )


def build_emergency_alert(alert: dict[str, Any]) -> tuple[str, str]:
    evidence = list(alert.get("evidence", []))
    translated_titles = translate_to_japanese(item.get("title", "") for item in evidence)
    event_labels = {
        "nuclear_weapon_event": "核兵器使用・核実験",
        "nuclear_facility_attack": "原子力施設への重大攻撃",
        "head_of_state": "国家元首の死亡・拘束",
        "coup": "クーデター成功",
        "ceasefire": "大規模停戦の成立・破棄",
        "major_transport_hub": "主要港・国際空港の全面閉鎖",
        "financial_sanctions": "大規模金融制裁・SWIFT排除",
        "systemic_financial_event": "資本規制・銀行休業・国家債務不履行",
        "major_disaster": "巨大災害による広域インフラ停止",
        "major_exchange": "大手取引所の全面売買停止",
        "critical_facility": "重要半導体・エネルギー施設の長期停止",
        "chokepoint_status": "重要航路の状態変化",
        "declaration_of_war": "宣戦布告",
    }
    state_labels = {
        "used_or_tested": "使用・実験を確認",
        "attacked": "攻撃を確認",
        "dead_or_detained": "死亡・拘束を確認",
        "succeeded": "政権掌握を確認",
        "effective": "正式発効",
        "collapsed": "破棄・崩壊を確認",
        "imposed": "正式発動",
        "infrastructure_disrupted": "広域停止を確認",
        "trading_halted": "全面売買停止",
        "long_term_shutdown": "長期停止を確認",
        "closed": "閉鎖・封鎖",
        "reopened": "再開・封鎖解除",
        "declared": "宣戦布告",
    }
    event_label = event_labels.get(alert.get("event_type"), str(alert.get("event_type") or "重大事象"))
    state_label = state_labels.get(alert.get("event_state"), str(alert.get("event_state") or "発生"))
    entity = str(alert.get("entity") or "対象不明")
    confirmation = (
        "信頼媒体を検出"
        if alert.get("confirmation") == "trusted_source"
        else "独立した複数媒体で確認"
    )
    ai_review = alert.get("ai_review") or {}
    jev_review = alert.get("jev_review") or {}
    approval_sources = alert.get("approval_sources") or (["openai"] if ai_review else [])
    source_labels = {"openai": "従来GPT", "jev": "Jev"}
    subject = f"[GDELT超重要速報] {entity}: {state_label}"
    lines = [
        "市場監視システムが超重要アクシデント候補を検出しました。",
        "",
        f"種別: {event_label}",
        f"対象: {entity}",
        f"状態: {state_label}",
        f"確認方法: {confirmation}",
        f"速報承認: {', '.join(source_labels.get(item, str(item)) for item in approval_sources) or '―'}",
        "",
    ]
    lines.extend(
        [
            f"GPT市場影響度: {ai_review.get('market_impact', '―')}",
            f"GPT緊急度: {ai_review.get('urgency', '―')}",
            f"影響資産: {', '.join(ai_review.get('affected_assets', [])) or '―'}",
            f"GPT判定理由: {ai_review.get('reason', '―')}",
            "",
        ]
    )
    if jev_review:
        lines.extend(
            [
                f"Jev発生確認確率: {float(jev_review.get('event_confirmed_probability', 0)):.1%}",
                f"Jev同一事象確率: {float(jev_review.get('same_event_probability', 0)):.1%}",
                f"Jev非推測確率: {float(jev_review.get('non_speculation_probability', 0)):.1%}",
                f"Jev市場影響確率: {float(jev_review.get('market_impact_probability', 0)):.1%}",
                f"Jev緊急性確率: {float(jev_review.get('urgency_probability', 0)):.1%}",
                "",
            ]
        )
    lines.append("根拠記事:")
    for index, item in enumerate(evidence, 1):
        original_title = item.get("title") or "タイトル不明"
        translated_title = translated_titles.get(original_title, original_title)
        lines.extend(
            [
                f"{index}. {translated_title}",
                f"   媒体: {item.get('domain') or '不明'}",
                f"   URL: {item.get('url') or 'なし'}",
            ]
        )
        if translated_title != original_title:
            lines.insert(len(lines) - 2, f"   原題: {original_title}")
    lines.extend(
        [
            "",
            "注意: これはタイトル情報を用いた機械判定です。売買判断前に記事本文と公的発表を確認してください。",
        ]
    )
    return subject, "\n".join(lines)


def send_emergency_alert(alert: dict[str, Any]) -> None:
    subject, body = build_emergency_alert(alert)
    SMTPMailer(SMTPSettings.from_env()).send_text(subject, body)


def send_stability_alert(
    database: NewsDatabase,
    result: dict[str, Any],
    stable_parameters: list[str],
    adaptive: dict[str, Any],
    now: datetime,
) -> str:
    labels = {"x": "収集周期x", "y": "休止時間y", "z": "クエリ間隔z"}
    detected = "・".join(labels[name] for name in stable_parameters)
    subject = (
        f"[GDELT安定域検知: {','.join(stable_parameters)}] x={result['x_current']:g}分 "
        f"y={result['y_current']:g}時間 z={result['z_current']:g}秒"
    )
    body = "\n".join(
        [
            "GDELT適応制御が安定域候補を検知しました。",
            "これは数学的な最適値の確定ではなく、実運用履歴上の安定候補です。",
            f"今回安定を検知した項目: {detected}",
            "", "【現在値】",
            f"x: {result['x_current']:g}分",
            f"y: {result['y_current']:g}時間",
            f"z: {result['z_current']:g}秒",
            "", "【観測した中心値】",
            f"x中央値: {result['x_baseline']:g}分",
            f"y中央値: {result['y_baseline']:g}時間",
            f"z中央値: {result['z_baseline']:g}秒",
            "", "【判定根拠】",
            *[
                f"{labels[name]}: {result['parameters'][name]['reason']} "
                f"(中央値={result['parameters'][name]['baseline']:g}, "
                f"変動幅={result['parameters'][name]['range']:g}, "
                f"反転={result['parameters'][name]['reversal_count']}回)"
                for name in stable_parameters
            ],
            f"観測期間: {result['window_hours']:g}時間",
            f"観測回数: {result['sample_count']}回",
            f"変動幅: x={result['x_range']:g}分 / y={result['y_range']:g}時間 / z={result['z_range']:g}秒",
            f"3項目すべて安定: {'はい' if result.get('all_stable') else 'いいえ'}",
            "", "【次回予定】",
            f"次回収集(JST): {_jst_time(adaptive.get('adaptive_next_collection_at'))}",
            "", "この値を基準に次の収集方式を検討できます。",
            "同じ安定域に留まる間、このメールは再送しません。",
        ]
    )
    SMTPMailer(SMTPSettings.from_env()).send_text(subject, body)
    key = f"gdelt-adaptive-stability:{now.astimezone(ZoneInfo('Asia/Tokyo')):%Y%m%dT%H%M%S}"
    with database.connection() as connection:
        connection.execute(
            "INSERT INTO notifications(notification_key,kind,sent_at,article_ids_json) VALUES(?,?,datetime('now'),'[]')",
            (key, "adaptive_stability"),
        )
    return key
