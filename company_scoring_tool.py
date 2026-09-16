from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app_meta import APP_BUILD_DATE, APP_VERSION, version_label


APP_DIR = Path(__file__).resolve().parent
ENGINE_SCRIPT = APP_DIR / "company_scoring.py"
COLLECTION_SCRIPT = APP_DIR / "company_collection.py"
SHOKUBA_SCRIPT = APP_DIR / "shokuba_enrichment.py"
WOMEN_ACTIVITY_SCRIPT = APP_DIR / "women_activity_enrichment.py"
PYTRENDS_SCRIPT = APP_DIR / "pytrends_enrichment.py"
STOCK_BOTTOM_SCRIPT = APP_DIR / "stock_bottom_financial_enrichment.py"
OUTPUT_DIR = APP_DIR / "outputs"
WEIGHT_KEYS = [
    ("rd", "研究開発"),
    ("growth", "昇給ポテンシャル"),
    ("wage", "リアル時給"),
    ("tenure", "定着"),
    ("location", "拠点マッチ"),
]


def _load_saved_openai_api_key() -> str:
    environment_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if environment_key:
        return environment_key
    try:
        from patent_monitor.secure_store import load_credentials

        return str(load_credentials().get("openai_api_key", "")).strip()
    except Exception:
        return ""


class CompanyScoringTool(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(version_label())
        self.geometry("1180x860")
        self.minsize(980, 720)

        self.job_file = tk.StringVar(value=str(APP_DIR / "work" / "sample_job_input.csv"))
        self.corporate_master = tk.StringVar(value="")
        self.config_file = tk.StringVar(value=str(APP_DIR / "scoring_config.json"))
        self.clean_data_file = tk.StringVar(value=str(OUTPUT_DIR / "all_records_clean.csv"))
        self.output_dir = tk.StringVar(value=str(OUTPUT_DIR))
        self.shokuba_file = tk.StringVar(value="")
        self.shokuba_target_file = tk.StringVar(value=str(OUTPUT_DIR / "collections" / "collected_companies.csv"))
        self.women_activity_file = tk.StringVar(value="")
        self.women_activity_target_file = tk.StringVar(value=str(OUTPUT_DIR / "collections" / "collected_companies.csv"))
        self.pytrends_target_file = tk.StringVar(value=str(OUTPUT_DIR / "collections" / "collected_companies.csv"))
        self.pytrends_months = tk.IntVar(value=12)
        self.pytrends_max_companies = tk.IntVar(value=100)
        self.pytrends_query_suffix = tk.StringVar(value="株価")
        self.pytrends_geo = tk.StringVar(value="JP")
        self.pytrends_openai_api_key = tk.StringVar(value=_load_saved_openai_api_key())
        self.pytrends_alias_model = tk.StringVar(value="gpt-5-nano-2025-08-07")
        self.pytrends_openai_key_value = ""
        self.stock_bottom_file = tk.StringVar(value="")
        self.stock_bottom_max_rows = tk.StringVar(value="")
        self.edinet_api_key = tk.StringVar(value=os.environ.get("EDINET_API_KEY", ""))
        self.use_edinet_api = tk.BooleanVar(value=False)
        self.edinet_dry_run = tk.BooleanVar(value=True)
        self.rescore = tk.BooleanVar(value=False)
        self.lookback_days = tk.IntVar(value=7)
        self.xbrl_limit = tk.IntVar(value=3000)
        self.collection_limit = tk.IntVar(value=3000)
        self.stock_bottom_lookback_days = tk.IntVar(value=460)
        self.exclude_non_analysis_candidates = tk.BooleanVar(value=True)
        self.continuous_collection = tk.BooleanVar(value=False)
        self.continuous_refresh_all = tk.BooleanVar(value=False)
        self.collection_progress_text = tk.StringVar(value="待機中")
        self.target_locations = tk.StringVar(value="金沢,札幌,新潟,関西")
        self.min_valid_score_count = tk.IntVar(value=2)
        self.weight_vars = {key: tk.DoubleVar(value=1.0) for key, _ in WEIGHT_KEYS}
        self.issue_filter = tk.StringVar(value="すべて")
        self.issue_search = tk.StringVar(value="")
        self.issue_count_text = tk.StringVar(value="0件")
        self.collection_source = tk.StringVar(value="job_file")
        self.collection_source_labels: dict[str, str] = {}
        self.collection_source_details: dict[str, dict[str, object]] = {}
        self.connection_test_running = False
        self.collection_running = False
        self.collection_process: subprocess.Popen[str] | None = None
        self.collection_stop_supported = False
        self.shokuba_running = False
        self.women_activity_running = False
        self.pytrends_running = False
        self.pytrends_process: subprocess.Popen[str] | None = None
        self.pytrends_output_lines: list[str] = []
        self.stock_bottom_running = False
        self.stock_bottom_progress_lines: list[str] = []
        self.running = False

        self._build_ui()
        self._load_config_into_form(show_message=False)
        self._refresh_manifest_summary()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(12, 10, 12, 4))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="企業評価スコアリングツール", font=("", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"v{APP_VERSION}", foreground="#666666").grid(row=0, column=1, padx=(8, 12), sticky="e")
        ttk.Button(header, text="出力フォルダを開く", command=self._open_output_dir).grid(row=0, column=2, padx=(8, 0))

        notebook = ttk.Notebook(self)
        notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 12))
        simple_frame = ttk.Frame(notebook)
        detail_frame = ttk.Frame(notebook)
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(1, weight=1)
        notebook.add(simple_frame, text="いつも使う")
        notebook.add(detail_frame, text="詳細分析")
        simple_frame.columnconfigure(0, weight=1)
        simple_frame.rowconfigure(0, weight=1)
        simple_canvas = tk.Canvas(simple_frame, highlightthickness=0)
        simple_scrollbar = ttk.Scrollbar(simple_frame, orient=tk.VERTICAL, command=simple_canvas.yview)
        simple_canvas.grid(row=0, column=0, sticky="nsew")
        simple_scrollbar.grid(row=0, column=1, sticky="ns")
        simple_canvas.configure(yscrollcommand=simple_scrollbar.set)
        simple_inner = ttk.Frame(simple_canvas, padding=16)
        simple_window = simple_canvas.create_window((0, 0), window=simple_inner, anchor="nw")
        simple_inner.bind("<Configure>", lambda _event: simple_canvas.configure(scrollregion=simple_canvas.bbox("all")))
        simple_canvas.bind("<Configure>", lambda event: simple_canvas.itemconfigure(simple_window, width=event.width))
        simple_canvas.bind_all("<MouseWheel>", lambda event: simple_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))
        self._build_simple_collection_screen(simple_inner)

        controls = ttk.LabelFrame(detail_frame, text="実行設定", padding=12)
        controls.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 8))
        controls.columnconfigure(1, weight=1)

        self._file_row(controls, 0, "求人ファイル", self.job_file, [("CSV/Excel", "*.csv *.xlsx *.xlsm *.xls"), ("All files", "*.*")])
        self._file_row(controls, 1, "法人番号マスタ", self.corporate_master, [("CSV/Excel", "*.csv *.xlsx *.xlsm *.xls"), ("All files", "*.*")])
        self._file_row(controls, 2, "設定ファイル", self.config_file, [("JSON", "*.json"), ("All files", "*.*")])
        self._file_row(controls, 3, "再スコア用CSV", self.clean_data_file, [("CSV", "*.csv"), ("All files", "*.*")])
        self._folder_row(controls, 4, "出力フォルダ", self.output_dir)

        mode_frame = ttk.Frame(controls)
        mode_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(mode_frame, text="再スコアリング", variable=self.rescore, command=self._on_mode_changed).grid(row=0, column=0, padx=(0, 16), sticky="w")
        ttk.Checkbutton(mode_frame, text="EDINET APIを使う", variable=self.use_edinet_api).grid(row=0, column=1, padx=(0, 16), sticky="w")
        ttk.Checkbutton(mode_frame, text="EDINETドライラン", variable=self.edinet_dry_run).grid(row=0, column=2, padx=(0, 16), sticky="w")
        ttk.Checkbutton(mode_frame, text="分析候補外をCSVに入れない", variable=self.exclude_non_analysis_candidates).grid(row=0, column=3, padx=(0, 16), sticky="w")
        ttk.Checkbutton(mode_frame, text="止めるまでEDINET収集", variable=self.continuous_collection).grid(row=0, column=4, padx=(0, 16), sticky="w")
        ttk.Checkbutton(mode_frame, text="既存データも全て再取得", variable=self.continuous_refresh_all).grid(row=0, column=5, padx=(0, 16), sticky="w")
        ttk.Label(mode_frame, text="収集元").grid(row=0, column=6, padx=(12, 4), sticky="e")
        self.collection_source_combo = ttk.Combobox(mode_frame, textvariable=self.collection_source, state="readonly", width=28)
        self.collection_source_combo.grid(row=0, column=7, sticky="w")
        self.collection_source_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_collection_source_changed())
        self._load_collection_sources()

        api_frame = ttk.Frame(controls)
        api_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        api_frame.columnconfigure(1, weight=1)
        ttk.Label(api_frame, text="EDINET APIキー").grid(row=0, column=0, sticky="w")
        ttk.Entry(api_frame, textvariable=self.edinet_api_key, show="*", width=48).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(api_frame, text="遡る日数").grid(row=0, column=2, padx=(12, 4), sticky="e")
        ttk.Spinbox(api_frame, from_=1, to=400, textvariable=self.lookback_days, width=6).grid(row=0, column=3, sticky="w")
        ttk.Label(api_frame, text="XBRL上限").grid(row=0, column=4, padx=(12, 4), sticky="e")
        ttk.Spinbox(api_frame, from_=0, to=100000, increment=100, textvariable=self.xbrl_limit, width=8).grid(row=0, column=5, sticky="w")

        weights = ttk.LabelFrame(controls, text="重み設定", padding=8)
        weights.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        for col, (key, label) in enumerate(WEIGHT_KEYS):
            ttk.Label(weights, text=label).grid(row=0, column=col, padx=4, sticky="w")
            ttk.Spinbox(weights, from_=0.0, to=20.0, increment=0.1, textvariable=self.weight_vars[key], width=8).grid(row=1, column=col, padx=4, sticky="w")
        ttk.Label(weights, text="勤務地キーワード").grid(row=0, column=5, padx=(16, 4), sticky="w")
        ttk.Entry(weights, textvariable=self.target_locations, width=32).grid(row=1, column=5, padx=(16, 4), sticky="ew")
        ttk.Label(weights, text="最小指標数").grid(row=0, column=6, padx=4, sticky="w")
        ttk.Spinbox(weights, from_=1, to=5, textvariable=self.min_valid_score_count, width=6).grid(row=1, column=6, padx=4, sticky="w")
        ttk.Button(weights, text="設定読込", command=lambda: self._load_config_into_form(show_message=True)).grid(row=1, column=7, padx=(12, 4))
        ttk.Button(weights, text="設定保存", command=self._save_config_from_form).grid(row=1, column=8, padx=4)

        action_frame = ttk.Frame(controls)
        action_frame.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.run_button = ttk.Button(action_frame, text="実行", command=self._run)
        self.run_button.grid(row=0, column=0, padx=(0, 8))
        self.collection_button = ttk.Button(action_frame, text="大量収集開始", command=self._run_collection)
        self.collection_button.grid(row=0, column=1, padx=(0, 8))
        self.collection_stop_button = ttk.Button(action_frame, text="収集中断", command=self._request_collection_stop, state="disabled")
        self.collection_stop_button.grid(row=0, column=2, padx=(0, 8))
        ttk.Button(action_frame, text="入力チェック", command=self._validate_inputs).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(action_frame, text="列チェック", command=self._check_input_columns).grid(row=0, column=4, padx=(0, 8))
        self.edinet_test_button = ttk.Button(action_frame, text="EDINET接続確認", command=self._run_edinet_connection_test)
        self.edinet_test_button.grid(row=0, column=5, padx=(0, 8))
        ttk.Button(action_frame, text="コマンド確認", command=self._show_command).grid(row=0, column=6, padx=(0, 8))
        ttk.Button(action_frame, text="結果サマリー更新", command=self._refresh_manifest_summary).grid(row=0, column=7, padx=(0, 8))
        ttk.Button(action_frame, text="Excelを開く", command=self._open_latest_excel).grid(row=0, column=8, padx=(0, 8))
        ttk.Button(action_frame, text="CSVを開く", command=self._open_ranking_csv).grid(row=0, column=9, padx=(0, 8))
        ttk.Button(action_frame, text="診断ZIP作成", command=self._create_debug_bundle).grid(row=0, column=10, padx=(0, 8))
        ttk.Button(action_frame, text="最新収集CSVを使う", command=self._use_latest_collection_csv).grid(row=1, column=0, padx=(0, 8), pady=(8, 0))
        ttk.Button(action_frame, text="法人番号確認CSVを開く", command=self._open_latest_review_queue).grid(row=1, column=1, padx=(0, 8), pady=(8, 0))
        ttk.Button(action_frame, text="収集フォルダを開く", command=self._open_latest_collection_folder).grid(row=1, column=2, padx=(0, 8), pady=(8, 0))
        ttk.Button(action_frame, text="全収集CSVを開く", command=self._open_all_collected_records).grid(row=1, column=3, padx=(0, 8), pady=(8, 0))
        ttk.Button(action_frame, text="収集品質CSVを開く", command=self._open_collection_quality_summary).grid(row=1, column=4, padx=(0, 8), pady=(8, 0))
        ttk.Button(action_frame, text="重複監査CSVを開く", command=self._open_duplicate_audit).grid(row=1, column=5, padx=(0, 8), pady=(8, 0))
        ttk.Button(action_frame, text="企業一覧CSVを開く", command=self._open_collected_companies).grid(row=1, column=6, padx=(0, 8), pady=(8, 0))
        ttk.Button(action_frame, text="企業一覧CSV全件を開く", command=self._open_all_collected_companies).grid(row=1, column=7, padx=(0, 8), pady=(8, 0))

        lower = ttk.PanedWindow(detail_frame, orient=tk.VERTICAL)
        lower.grid(row=1, column=0, sticky="nsew")

        log_frame = ttk.LabelFrame(lower, text="実行ログ", padding=8)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=18, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        lower.add(log_frame, weight=3)

        summary_frame = ttk.LabelFrame(lower, text="最新結果サマリー", padding=8)
        summary_frame.rowconfigure(0, weight=1)
        summary_frame.columnconfigure(0, weight=1)
        self.summary_text = tk.Text(summary_frame, height=8, wrap="word")
        self.summary_text.grid(row=0, column=0, sticky="nsew")
        summary_scroll = ttk.Scrollbar(summary_frame, command=self.summary_text.yview)
        summary_scroll.grid(row=0, column=1, sticky="ns")
        self.summary_text.configure(yscrollcommand=summary_scroll.set)
        lower.add(summary_frame, weight=1)

        ranking_frame = ttk.LabelFrame(lower, text="ランキングプレビュー", padding=8)
        ranking_frame.rowconfigure(0, weight=1)
        ranking_frame.columnconfigure(0, weight=1)
        self.ranking_tree = ttk.Treeview(ranking_frame, columns=("rank", "company", "score", "join", "name_check"), show="headings", height=6)
        for key, text, width, anchor in [
            ("rank", "順位", 60, "center"),
            ("company", "企業名", 360, "w"),
            ("score", "総合スコア", 100, "e"),
            ("join", "JOIN", 120, "center"),
            ("name_check", "企業名確認", 140, "center"),
        ]:
            self.ranking_tree.heading(key, text=text)
            self.ranking_tree.column(key, width=width, anchor=anchor, stretch=(key == "company"))
        self.ranking_tree.grid(row=0, column=0, sticky="nsew")
        self.ranking_tree.bind("<Double-1>", lambda _event: self._show_selected_detail(self.ranking_tree))
        ranking_scroll = ttk.Scrollbar(ranking_frame, command=self.ranking_tree.yview)
        ranking_scroll.grid(row=0, column=1, sticky="ns")
        self.ranking_tree.configure(yscrollcommand=ranking_scroll.set)
        lower.add(ranking_frame, weight=1)

        issue_frame = ttk.LabelFrame(lower, text="注意が必要な行", padding=8)
        issue_frame.rowconfigure(1, weight=1)
        issue_frame.columnconfigure(0, weight=1)
        issue_tools = ttk.Frame(issue_frame)
        issue_tools.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        issue_tools.columnconfigure(3, weight=1)
        ttk.Label(issue_tools, text="表示").grid(row=0, column=0, sticky="w")
        issue_combo = ttk.Combobox(
            issue_tools,
            textvariable=self.issue_filter,
            values=("すべて", "JOIN失敗", "企業名不一致", "スコア不足", "法人番号補完", "EDINET/XBRL"),
            state="readonly",
            width=16,
        )
        issue_combo.grid(row=0, column=1, padx=(6, 14), sticky="w")
        issue_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_issue_preview())
        ttk.Label(issue_tools, text="検索").grid(row=0, column=2, sticky="w")
        search_entry = ttk.Entry(issue_tools, textvariable=self.issue_search)
        search_entry.grid(row=0, column=3, padx=(6, 8), sticky="ew")
        search_entry.bind("<Return>", lambda _event: self._refresh_issue_preview())
        ttk.Button(issue_tools, text="絞り込み", command=self._refresh_issue_preview).grid(row=0, column=4, padx=(0, 8))
        ttk.Button(issue_tools, text="解除", command=self._clear_issue_filter).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(issue_tools, text="注意行CSV保存", command=self._export_filtered_issues).grid(row=0, column=6, padx=(0, 8))
        ttk.Label(issue_tools, textvariable=self.issue_count_text, foreground="#666666").grid(row=0, column=7, sticky="e")
        self.issue_tree = ttk.Treeview(
            issue_frame,
            columns=("job_id", "company", "reason", "status", "memo"),
            show="headings",
            height=6,
        )
        for key, text, width, anchor in [
            ("job_id", "求人番号", 90, "w"),
            ("company", "企業名", 260, "w"),
            ("reason", "理由", 190, "w"),
            ("status", "状態", 150, "w"),
            ("memo", "メモ", 420, "w"),
        ]:
            self.issue_tree.heading(key, text=text)
            self.issue_tree.column(key, width=width, anchor=anchor, stretch=(key == "memo"))
        self.issue_tree.grid(row=1, column=0, sticky="nsew")
        self.issue_tree.bind("<Double-1>", lambda _event: self._show_selected_detail(self.issue_tree))
        issue_scroll = ttk.Scrollbar(issue_frame, command=self.issue_tree.yview)
        issue_scroll.grid(row=1, column=1, sticky="ns")
        self.issue_tree.configure(yscrollcommand=issue_scroll.set)
        lower.add(issue_frame, weight=1)

    def _build_simple_collection_screen(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(5, weight=1)
        card = ttk.LabelFrame(parent, text="EDINET企業収集", padding=16)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="EDINET APIキー").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(card, textvariable=self.edinet_api_key, show="*", width=48).grid(row=0, column=1, columnspan=3, sticky="ew", padx=(12, 0), pady=6)

        ttk.Label(card, text="遡る日数").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Spinbox(card, from_=1, to=400, textvariable=self.lookback_days, width=8).grid(row=1, column=1, sticky="w", padx=(12, 24), pady=6)

        ttk.Label(card, text="最大取得件数").grid(row=1, column=2, sticky="w", pady=6)
        ttk.Spinbox(card, from_=1, to=100000, increment=100, textvariable=self.collection_limit, width=10).grid(row=1, column=3, sticky="w", padx=(12, 0), pady=6)

        ttk.Label(card, text="詳しい数値を取る企業数").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Spinbox(card, from_=0, to=100000, increment=10, textvariable=self.xbrl_limit, width=10).grid(row=2, column=1, sticky="w", padx=(12, 24), pady=6)

        ttk.Checkbutton(card, text="分析候補外をCSVに入れない", variable=self.exclude_non_analysis_candidates).grid(
            row=2, column=2, columnspan=2, sticky="w", pady=(8, 0)
        )

        ttk.Checkbutton(card, text="止めるまで収集し続ける（閉じても次回再開できます）", variable=self.continuous_collection).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(card, text="既存データも全て再取得（過去データに疑いがある時用）", variable=self.continuous_refresh_all).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )

        progress = ttk.LabelFrame(card, text="進捗", padding=8)
        progress.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        progress.columnconfigure(0, weight=1)
        ttk.Label(progress, textvariable=self.collection_progress_text, font=("", 12, "bold"), foreground="#1f5f99", wraplength=920).grid(
            row=0, column=0, sticky="ew"
        )

        action_row = ttk.Frame(card)
        action_row.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(16, 0))
        self.simple_collection_button = ttk.Button(action_row, text="EDINETから情報を収集開始", command=self._run_simple_edinet_collection)
        self.simple_collection_button.grid(row=0, column=0, padx=(0, 10))
        self.simple_collection_stop_button = ttk.Button(action_row, text="収集中断", command=self._request_collection_stop, state="disabled")
        self.simple_collection_stop_button.grid(row=0, column=1, padx=(0, 10))
        ttk.Button(action_row, text="収集した最近のCSVファイルを開く", command=self._open_collected_companies).grid(row=0, column=2, padx=(0, 10))

        stock_bottom = ttk.LabelFrame(parent, text="株底検知CSVにEDINET財務を結合", padding=16)
        stock_bottom.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        stock_bottom.columnconfigure(1, weight=1)
        self._file_row(stock_bottom, 0, "底検知CSV", self.stock_bottom_file, [("CSV", "*.csv"), ("All files", "*.*")])
        ttk.Label(stock_bottom, text="探索日数").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Spinbox(stock_bottom, from_=30, to=3000, increment=30, textvariable=self.stock_bottom_lookback_days, width=8).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=6
        )
        ttk.Label(stock_bottom, text="底検知日以前に提出済みの有価証券報告書だけを使います。").grid(
            row=1, column=1, sticky="w", padx=(96, 0), pady=6
        )
        ttk.Label(stock_bottom, text="上から処理する件数").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(stock_bottom, textvariable=self.stock_bottom_max_rows, width=10).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=6)
        ttk.Label(stock_bottom, text="空欄なら全件処理します。").grid(row=2, column=1, sticky="w", padx=(96, 0), pady=6)
        stock_bottom_actions = ttk.Frame(stock_bottom)
        stock_bottom_actions.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.stock_bottom_button = ttk.Button(
            stock_bottom_actions,
            text="底検知CSVに財務データを追加",
            command=self._run_stock_bottom_enrichment,
        )
        self.stock_bottom_button.grid(row=0, column=0, padx=(0, 10))
        ttk.Button(stock_bottom_actions, text="完成CSVを開く", command=self._open_stock_bottom_enriched_csv).grid(row=0, column=1, padx=(0, 10))
        ttk.Button(stock_bottom_actions, text="底検知結合フォルダを開く", command=self._open_stock_bottom_output_dir).grid(row=0, column=2, padx=(0, 10))

        shokuba = ttk.LabelFrame(parent, text="しょくばらぼ結合", padding=16)
        shokuba.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        shokuba.columnconfigure(1, weight=1)
        self._file_row(shokuba, 0, "しょくばらぼCSV", self.shokuba_file, [("CSV", "*.csv"), ("All files", "*.*")])
        self._file_row(shokuba, 1, "結合したいCSV", self.shokuba_target_file, [("CSV", "*.csv"), ("All files", "*.*")])
        shokuba_actions = ttk.Frame(shokuba)
        shokuba_actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.shokuba_button = ttk.Button(shokuba_actions, text="しょくばらぼ情報を結合", command=self._run_shokuba_enrichment)
        self.shokuba_button.grid(row=0, column=0, padx=(0, 10))
        ttk.Button(shokuba_actions, text="最新の企業一覧CSVを使う", command=self._use_collected_companies_for_shokuba).grid(row=0, column=1, padx=(0, 10))
        ttk.Button(shokuba_actions, text="詳細結合CSVを開く", command=self._open_shokuba_key_metrics_csv).grid(row=0, column=2, padx=(0, 10))
        ttk.Button(shokuba_actions, text="要約CSVを開く", command=self._open_shokuba_summary_csv).grid(row=0, column=3, padx=(0, 10))
        ttk.Button(shokuba_actions, text="しょくばらぼ出力フォルダを開く", command=self._open_shokuba_output_dir).grid(row=0, column=4, padx=(0, 10))

        women = ttk.LabelFrame(parent, text="女性活躍DB結合", padding=16)
        women.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        women.columnconfigure(1, weight=1)
        self._file_row(women, 0, "女性活躍DB CSV", self.women_activity_file, [("CSV", "*.csv"), ("All files", "*.*")])
        self._file_row(women, 1, "結合したいCSV", self.women_activity_target_file, [("CSV", "*.csv"), ("All files", "*.*")])
        women_actions = ttk.Frame(women)
        women_actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.women_activity_button = ttk.Button(women_actions, text="女性活躍DB情報を結合", command=self._run_women_activity_enrichment)
        self.women_activity_button.grid(row=0, column=0, padx=(0, 10))
        ttk.Button(women_actions, text="最新の企業一覧CSVを使う", command=self._use_collected_companies_for_women_activity).grid(row=0, column=1, padx=(0, 10))
        ttk.Button(women_actions, text="詳細結合CSVを開く", command=self._open_women_activity_key_metrics_csv).grid(row=0, column=2, padx=(0, 10))
        ttk.Button(women_actions, text="要約CSVを開く", command=self._open_women_activity_summary_csv).grid(row=0, column=3, padx=(0, 10))
        ttk.Button(women_actions, text="女性活躍DB出力フォルダを開く", command=self._open_women_activity_output_dir).grid(row=0, column=4, padx=(0, 10))

        trends = ttk.LabelFrame(parent, text="Google Trends収集・結合（pytrends）", padding=16)
        trends.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        trends.columnconfigure(1, weight=1)
        self._file_row(trends, 0, "結合したいCSV", self.pytrends_target_file, [("CSV", "*.csv"), ("All files", "*.*")])
        ttk.Label(trends, text="取得期間（月）").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Spinbox(trends, from_=3, to=60, increment=3, textvariable=self.pytrends_months, width=8).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=6
        )
        ttk.Label(trends, text="最大企業数").grid(row=1, column=1, sticky="w", padx=(110, 0), pady=6)
        ttk.Spinbox(trends, from_=0, to=10000, increment=10, textvariable=self.pytrends_max_companies, width=8).grid(
            row=1, column=1, sticky="w", padx=(190, 0), pady=6
        )
        ttk.Label(trends, text="0なら全件。まず100件以下での確認を推奨").grid(row=1, column=1, sticky="w", padx=(270, 0), pady=6)
        ttk.Label(trends, text="検索語の末尾").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(trends, textvariable=self.pytrends_query_suffix, width=18).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=6)
        ttk.Label(trends, text="地域").grid(row=2, column=1, sticky="w", padx=(190, 0), pady=6)
        ttk.Entry(trends, textvariable=self.pytrends_geo, width=6).grid(row=2, column=1, sticky="w", padx=(235, 0), pady=6)
        ttk.Label(trends, text="OpenAI APIキー").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(trends, textvariable=self.pytrends_openai_api_key, show="*").grid(
            row=3, column=1, sticky="ew", padx=(8, 300), pady=6
        )
        ttk.Label(trends, text="呼称モデル").grid(row=3, column=1, sticky="e", padx=(0, 195), pady=6)
        ttk.Entry(trends, textvariable=self.pytrends_alias_model, width=25).grid(
            row=3, column=1, sticky="e", padx=(0, 0), pady=6
        )
        ttk.Label(
            trends,
            text="初回のみ全対象企業の一般呼称をGPTで決定しSQLiteへ保存します。2回目以降は保存呼称を使います。",
            foreground="#666666",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        trends_actions = ttk.Frame(trends)
        trends_actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.pytrends_button = ttk.Button(trends_actions, text="Google Trendsを収集・結合", command=self._run_pytrends_enrichment)
        self.pytrends_button.grid(row=0, column=0, padx=(0, 10))
        self.pytrends_stop_button = ttk.Button(trends_actions, text="中断", command=self._stop_pytrends_enrichment, state="disabled")
        self.pytrends_stop_button.grid(row=0, column=1, padx=(0, 10))
        ttk.Button(trends_actions, text="動作環境確認", command=self._check_pytrends_environment).grid(row=0, column=2, padx=(0, 10))
        ttk.Button(trends_actions, text="最新の企業一覧CSVを使う", command=self._use_collected_companies_for_pytrends).grid(row=0, column=3, padx=(0, 10))
        ttk.Button(trends_actions, text="結合CSVを開く", command=self._open_pytrends_enriched_csv).grid(row=0, column=4, padx=(0, 10))
        ttk.Button(trends_actions, text="時系列CSVを開く", command=self._open_pytrends_series_csv).grid(row=0, column=5, padx=(0, 10))
        ttk.Button(trends_actions, text="出力フォルダを開く", command=self._open_pytrends_output_dir).grid(row=0, column=6, padx=(0, 10))

        result = ttk.LabelFrame(parent, text="最新収集サマリー", padding=12)
        result.grid(row=5, column=0, sticky="nsew", pady=(12, 0))
        result.rowconfigure(0, weight=1)
        result.columnconfigure(0, weight=1)
        self.simple_summary_text = tk.Text(result, height=22, wrap="word")
        self.simple_summary_text.grid(row=0, column=0, sticky="nsew")
        simple_scroll = ttk.Scrollbar(result, command=self.simple_summary_text.yview)
        simple_scroll.grid(row=0, column=1, sticky="ns")
        self.simple_summary_text.configure(yscrollcommand=simple_scroll.set)

    def _file_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, filetypes: list[tuple[str, str]]) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=3)
        ttk.Button(parent, text="選択", command=lambda: self._choose_file(variable, filetypes)).grid(row=row, column=2, sticky="e", pady=3)

    def _folder_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=3)
        ttk.Button(parent, text="選択", command=lambda: self._choose_folder(variable)).grid(row=row, column=2, sticky="e", pady=3)

    def _choose_file(self, variable: tk.StringVar, filetypes: list[tuple[str, str]]) -> None:
        path = filedialog.askopenfilename(initialdir=str(APP_DIR), filetypes=filetypes)
        if path:
            variable.set(path)

    def _choose_folder(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory(initialdir=str(APP_DIR))
        if path:
            variable.set(path)

    def _load_collection_sources(self) -> None:
        registry_path = APP_DIR / "work" / "data_source_registry_template.json"
        values: list[str] = []
        self.collection_source_labels.clear()
        self.collection_source_details.clear()
        try:
            with registry_path.open("r", encoding="utf-8-sig") as handle:
                registry = json.load(handle)
            for source in registry.get("sources", []):
                source_id = str(source.get("source_id", ""))
                if not source_id:
                    continue
                label = f"{source_id}: {source.get('description', '')}"
                values.append(label)
                self.collection_source_labels[label] = source_id
                self.collection_source_details[source_id] = source
        except Exception:
            values = ["job_file: 求人CSV/Excel入力"]
            self.collection_source_labels = {values[0]: "job_file"}
            self.collection_source_details = {
                "job_file": {"source_id": "job_file", "source_type": "file", "enabled": True, "description": "求人CSV/Excel入力"}
            }
        self.collection_source_combo.configure(values=values)
        for label, source_id in self.collection_source_labels.items():
            if source_id == "job_file":
                self.collection_source.set(label)
                break

    def _selected_collection_source_id(self) -> str:
        value = self.collection_source.get()
        return self.collection_source_labels.get(value, value.split(":", 1)[0].strip() or "job_file")

    def _set_collection_source_by_id(self, source_id: str) -> bool:
        for label, candidate_id in self.collection_source_labels.items():
            if candidate_id == source_id:
                self.collection_source.set(label)
                self._on_collection_source_changed()
                return True
        return False

    def _on_collection_source_changed(self) -> None:
        source_id = self._selected_collection_source_id()
        source = self.collection_source_details.get(source_id, {})
        source_type = source.get("source_type", "")
        enabled = bool(source.get("enabled", False))
        if source_id == "edinet_api":
            self._append_log_now("[大量収集] EDINET API収集を選択しました。APIキーを入力してから実行してください。\n")
        elif source_id != "job_file":
            status = "未実装" if source_type == "api" else "未対応"
            if not enabled:
                status += " / 台帳では無効"
            self._append_log_now(f"[大量収集] 収集元 {source_id} は現在 {status} です。まずは求人CSV/Excel入力を使ってください。\n")

    def _config_path(self) -> Path:
        return Path(self.config_file.get().strip() or APP_DIR / "scoring_config.json")

    def _load_config_into_form(self, show_message: bool) -> None:
        path = self._config_path()
        if not path.exists():
            if show_message:
                messagebox.showwarning("設定読込", f"設定ファイルが見つかりません: {path}")
            return
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                config = json.load(handle)
            weights = config.get("score_weights", {})
            for key, _ in WEIGHT_KEYS:
                if key in weights:
                    self.weight_vars[key].set(float(weights[key]))
            locations = config.get("target_locations", [])
            if isinstance(locations, list):
                self.target_locations.set(",".join(str(item) for item in locations))
            self.min_valid_score_count.set(int(config.get("min_valid_score_count", 2)))
            if show_message:
                messagebox.showinfo("設定読込", "設定を読み込みました")
        except Exception as exc:
            messagebox.showerror("設定読込", f"設定を読めません: {exc}")

    def _save_config_from_form(self) -> None:
        path = self._config_path()
        try:
            weights = {key: float(var.get()) for key, var in self.weight_vars.items()}
            locations = [item.strip() for item in self.target_locations.get().replace("、", ",").split(",") if item.strip()]
            config = {
                "score_weights": weights,
                "target_locations": locations,
                "min_valid_score_count": int(self.min_valid_score_count.get()),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8-sig") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
            messagebox.showinfo("設定保存", f"設定を保存しました: {path}")
        except Exception as exc:
            messagebox.showerror("設定保存", f"設定を保存できません: {exc}")

    def _on_mode_changed(self) -> None:
        if self.rescore.get():
            self.use_edinet_api.set(False)

    def _validate_inputs(self) -> None:
        messages: list[str] = []
        warnings: list[str] = []

        config_path = self._config_path()
        if config_path.exists():
            try:
                with config_path.open("r", encoding="utf-8-sig") as handle:
                    config = json.load(handle)
                weights = config.get("score_weights", {})
                if not isinstance(weights, dict):
                    warnings.append("設定ファイルの score_weights が辞書形式ではありません")
                else:
                    total_weight = 0.0
                    for key, _label in WEIGHT_KEYS:
                        try:
                            total_weight += float(weights.get(key, self.weight_vars[key].get()))
                        except (TypeError, ValueError):
                            warnings.append(f"重み {key} が数値として読めません")
                    if total_weight <= 0:
                        warnings.append("重みの合計が0以下です")
                messages.append(f"設定ファイル: OK ({config_path})")
            except Exception as exc:
                warnings.append(f"設定ファイルを読めません: {exc}")
        else:
            warnings.append(f"設定ファイルが見つかりません: {config_path}")

        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            messages.append(f"出力フォルダ: OK ({output_dir})")
        except Exception as exc:
            warnings.append(f"出力フォルダを作成できません: {exc}")

        if self.rescore.get():
            clean_data = Path(self.clean_data_file.get().strip())
            if clean_data.exists():
                messages.append(f"再スコア用CSV: OK ({clean_data})")
            else:
                warnings.append(f"再スコア用CSVが見つかりません: {clean_data}")
        else:
            job_file = Path(self.job_file.get().strip())
            if job_file.exists():
                messages.append(f"求人ファイル: OK ({job_file})")
            else:
                warnings.append(f"求人ファイルが見つかりません: {job_file}")
            master_value = self.corporate_master.get().strip()
            if master_value:
                master_path = Path(master_value)
                if master_path.exists():
                    messages.append(f"法人番号マスタ: OK ({master_path})")
                else:
                    warnings.append(f"法人番号マスタが見つかりません: {master_path}")

        if self.use_edinet_api.get() and not self.edinet_api_key.get().strip() and not os.environ.get("EDINET_API_KEY"):
            warnings.append("EDINET APIを使う設定ですが、APIキーが未入力です")

        summary = "\n".join(messages)
        if warnings:
            summary += "\n\n確認が必要:\n" + "\n".join(f"- {item}" for item in warnings)
            messagebox.showwarning("入力チェック", summary)
        else:
            messagebox.showinfo("入力チェック", summary + "\n\nこの設定で実行できます。")
        self._append_log_now("[入力チェック]\n" + summary + "\n")

    def _check_input_columns(self) -> None:
        try:
            if self.rescore.get():
                path = Path(self.clean_data_file.get().strip())
                self._show_column_check_window("再スコア用CSV", path, mode="rescore")
                return

            job_path = Path(self.job_file.get().strip())
            self._show_column_check_window("求人ファイル", job_path, mode="job")

            master_value = self.corporate_master.get().strip()
            if master_value:
                self._show_column_check_window("法人番号マスタ", Path(master_value), mode="master")
        except Exception as exc:
            messagebox.showerror("列チェック", f"列チェックに失敗しました: {exc}")

    def _read_input_preview(self, path: Path, max_rows: int = 5) -> tuple[list[str], list[dict[str, object]]]:
        if not path.exists():
            raise FileNotFoundError(f"ファイルが見つかりません: {path}")
        suffix = path.suffix.lower()
        if suffix == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = []
                for index, row in enumerate(reader):
                    if index >= max_rows:
                        break
                    rows.append(dict(row))
                return list(reader.fieldnames or []), rows
        if suffix in {".xlsx", ".xlsm", ".xls"}:
            try:
                import pandas as pd
            except ImportError as exc:
                raise RuntimeError("Excelの列チェックには pandas / openpyxl が必要です。pip install -r requirements.txt を実行してください") from exc
            frame = pd.read_excel(path, dtype=str, nrows=max_rows)
            return [str(column) for column in frame.columns], frame.fillna("").to_dict(orient="records")
        raise ValueError(f"未対応のファイル形式です: {path.suffix}")

    def _build_column_report(self, columns: list[str], mode: str) -> tuple[list[str], list[str]]:
        from company_scoring import CORPORATE_MASTER_COLUMN_ALIASES, JOB_COLUMN_ALIASES, normalize_column_label

        if mode == "master":
            aliases = CORPORATE_MASTER_COLUMN_ALIASES
            important = ["corporate_number", "company_name", "address"]
        elif mode == "rescore":
            aliases = {key: [key] for key in [
                "company_name",
                "corporate_number",
                "join_status",
                "average_annual_salary",
                "rd_expenses",
                "net_sales",
                "total_score",
            ]}
            important = ["company_name", "corporate_number", "join_status"]
        else:
            aliases = JOB_COLUMN_ALIASES
            important = ["company_name", "job_id", "corporate_number", "basic_salary_min", "annual_holidays", "overtime_hours_avg"]

        normalized_columns = {normalize_column_label(column): column for column in columns}
        used: set[str] = set()
        lines: list[str] = []
        mapped_targets: set[str] = set()
        for target, candidates in aliases.items():
            source = ""
            for alias in candidates:
                matched = normalized_columns.get(normalize_column_label(alias))
                if matched and matched not in used:
                    source = matched
                    used.add(matched)
                    mapped_targets.add(target)
                    break
            label = "OK" if source else "未検出"
            lines.append(f"{label}  {target}  <-  {source or '(該当列なし)'}")

        missing = [target for target in important if target not in mapped_targets]
        unused = [column for column in columns if column not in used]
        warnings: list[str] = []
        if missing:
            warnings.append("重要列の未検出: " + ", ".join(missing))
        if unused:
            warnings.append("未使用列: " + ", ".join(unused[:20]) + (" ..." if len(unused) > 20 else ""))
        return lines, warnings

    def _show_column_check_window(self, title: str, path: Path, mode: str) -> None:
        columns, rows = self._read_input_preview(path)
        lines, warnings = self._build_column_report(columns, mode)

        window = tk.Toplevel(self)
        window.title(f"列チェック: {title}")
        window.geometry("920x620")
        window.minsize(760, 460)
        window.rowconfigure(0, weight=1)
        window.columnconfigure(0, weight=1)

        text = tk.Text(window, wrap="word")
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(window, command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)

        output: list[str] = [
            f"対象: {title}",
            f"ファイル: {path}",
            "",
            "[列対応]",
            *lines,
        ]
        if warnings:
            output.extend(["", "[確認が必要]", *warnings])
        output.extend(["", "[先頭データ]"])
        if rows:
            for index, row in enumerate(rows, start=1):
                compact = ", ".join(f"{key}={value}" for key, value in list(row.items())[:10])
                output.append(f"{index}: {compact}")
        else:
            output.append("(データ行なし)")
        text.insert(tk.END, "\n".join(output))
        text.configure(state="disabled")
        self._append_log_now(f"[列チェック] {title}: {len(columns)}列, 確認事項 {len(warnings)}件\n")

    def _build_command(self) -> list[str]:
        command = [sys.executable, str(ENGINE_SCRIPT)]
        output_dir = self.output_dir.get().strip()
        config_file = self.config_file.get().strip()
        if output_dir:
            command.extend(["--output-dir", output_dir])
        if config_file:
            command.extend(["--config", config_file])
        if self.rescore.get():
            command.append("--rescore")
            clean_data = self.clean_data_file.get().strip()
            if clean_data:
                command.extend(["--input-clean-data", clean_data])
            return command

        job_file = self.job_file.get().strip()
        if job_file:
            command.extend(["--job-file", job_file])
        corporate_master = self.corporate_master.get().strip()
        if corporate_master:
            command.extend(["--corporate-master", corporate_master])
        if self.use_edinet_api.get():
            command.append("--use-edinet-api")
            if self.edinet_dry_run.get():
                command.append("--edinet-dry-run")
            command.extend(["--edinet-lookback-days", str(self.lookback_days.get())])
            command.extend(["--edinet-xbrl-limit", str(self.xbrl_limit.get())])
        return command

    def _show_command(self) -> None:
        command = self._build_command()
        self._append_log(" ".join(f'"{item}"' if " " in item else item for item in command) + "\n")

    def _build_collection_command(self) -> tuple[list[str], Path]:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        registry_path = APP_DIR / "work" / "data_source_registry_template.json"
        command = [
            sys.executable,
            str(COLLECTION_SCRIPT),
            "--registry",
            str(registry_path),
            "--source-id",
            self._selected_collection_source_id(),
            "--output-dir",
            str(output_dir),
        ]
        job_file = self.job_file.get().strip()
        if job_file and self._selected_collection_source_id() == "job_file":
            command.extend(["--input-file", job_file])
        corporate_master = self.corporate_master.get().strip()
        if corporate_master:
            command.extend(["--corporate-master", corporate_master])
        if self._selected_collection_source_id() == "edinet_api":
            if self.continuous_collection.get():
                collections_root = output_dir / "collections"
                command.append("--continuous-edinet")
                command.extend(["--continuous-state-file", str(collections_root / "continuous_edinet_state.json")])
                command.extend(["--continuous-stop-file", str(collections_root / "continuous_edinet_stop.request")])
                if self.continuous_refresh_all.get():
                    command.append("--continuous-refresh-all")
            else:
                command.extend(["--edinet-lookback-days", str(self.lookback_days.get())])
                command.extend(["--max-records", str(self.collection_limit.get())])
                command.extend(["--edinet-xbrl-limit", str(self.xbrl_limit.get())])
        if self.exclude_non_analysis_candidates.get():
            command.append("--exclude-non-analysis-candidates")
        return command, output_dir

    def _continuous_stop_file_path(self) -> Path:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        return output_dir / "collections" / "continuous_edinet_stop.request"

    def _run_simple_edinet_collection(self) -> None:
        self.use_edinet_api.set(True)
        self.edinet_dry_run.set(False)
        self._set_collection_source_by_id("edinet_api")
        self._run_collection()

    def _run_collection(self) -> None:
        if self.collection_running:
            return
        if not COLLECTION_SCRIPT.exists():
            messagebox.showerror("大量収集", f"収集プログラムが見つかりません: {COLLECTION_SCRIPT}")
            return
        if self.rescore.get():
            messagebox.showwarning("大量収集", "再スコアリングを解除してから大量収集を実行してください")
            return
        source_id = self._selected_collection_source_id()
        if source_id == "job_file" and self.use_edinet_api.get():
            if self._set_collection_source_by_id("edinet_api"):
                source_id = "edinet_api"
                self._append_log_now("[大量収集] EDINET APIを使う設定のため、収集元を EDINET API v2 に切り替えました。\n")
            else:
                messagebox.showwarning("大量収集", "EDINET収集元が見つかりません。アプリを最新版に更新してから再度試してください。")
                return
        if source_id == "edinet_api":
            if not self.edinet_api_key.get().strip() and not os.environ.get("EDINET_API_KEY"):
                messagebox.showwarning("大量収集", "EDINET APIキーを入力してからEDINET収集を実行してください")
                return
        elif source_id != "job_file":
            source = self.collection_source_details.get(source_id, {})
            messagebox.showwarning(
                "大量収集",
                f"収集元 '{source_id}' はまだ実装準備中です。\n\n"
                f"{source.get('notes', 'API仕様、認証、利用条件を確認してから実装します。')}\n\n"
                "現在実行できる大量収集は、求人CSV/Excel入力です。",
            )
            return
        if source_id == "job_file":
            job_path = Path(self.job_file.get().strip())
            normalized_inside_collections = (
                job_path.name.lower() == "normalized_records.csv"
                and "collections" in {part.lower() for part in job_path.parts}
            )
            if normalized_inside_collections:
                proceed = messagebox.askyesno(
                    "大量収集",
                    "収集済みCSVをもう一度収集しようとしています。\n\n"
                    "通常は「大量収集」ではなく「実行」を押すと分析できます。\n"
                    "それでも再収集しますか？",
                )
                if not proceed:
                    return
        self.collection_running = True
        self.collection_stop_supported = source_id == "edinet_api" and self.continuous_collection.get()
        if self.collection_stop_supported:
            self.collection_progress_text.set("止めるまで収集を開始しています。中断ボタンで止められます。")
        else:
            self.collection_progress_text.set("収集を開始しています...")
        self.collection_button.configure(state="disabled")
        stop_state = "normal" if self.collection_stop_supported else "disabled"
        if hasattr(self, "collection_stop_button"):
            self.collection_stop_button.configure(state=stop_state)
        if hasattr(self, "simple_collection_button"):
            self.simple_collection_button.configure(state="disabled")
        if hasattr(self, "simple_collection_stop_button"):
            self.simple_collection_stop_button.configure(state=stop_state)
        self._append_log("\n[大量収集]\n")
        threading.Thread(target=self._collection_worker, daemon=True).start()

    def _request_collection_stop(self) -> None:
        if not self.collection_running:
            return
        if not self.collection_stop_supported:
            messagebox.showinfo("大量収集", "中断ボタンは「止めるまで収集し続ける」モード用です。")
            return
        try:
            stop_file = self._continuous_stop_file_path()
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
            self.collection_progress_text.set("中断要求を送りました。現在処理中の1件が終わると保存して止まります。")
            self._append_log_now(f"[大量収集] 中断要求を送りました: {stop_file}\n")
        except Exception as exc:
            messagebox.showwarning("大量収集", f"中断要求を送れませんでした: {exc}")

    def _collection_worker(self) -> None:
        command, output_dir = self._build_collection_command()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        api_key = self.edinet_api_key.get().strip()
        if api_key:
            env["EDINET_API_KEY"] = api_key
        self._append_log("収集ジョブを開始します。\n")
        self._append_log(" ".join(f'"{item}"' if " " in item else item for item in command) + "\n\n")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            self.collection_process = process
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(line)
            return_code = process.wait()
            self._append_log(f"\n大量収集 終了コード: {return_code}\n")
            self.after(0, lambda: self._summarize_collection(output_dir, return_code))
        except Exception as exc:
            self._append_log(f"\n大量収集中にエラー: {exc}\n")
            self.after(0, lambda: messagebox.showerror("大量収集", f"大量収集に失敗しました: {exc}"))
        finally:
            self.collection_process = None
            self.after(0, self._on_collection_finished)

    def _summarize_collection(self, output_dir: Path, return_code: int) -> None:
        latest_path = output_dir / "collections" / "latest_collection.json"
        if not latest_path.exists():
            messagebox.showwarning("大量収集", f"収集結果のlatest_collection.jsonが見つかりません。\n終了コード: {return_code}")
            return
        try:
            with latest_path.open("r", encoding="utf-8-sig") as handle:
                latest = json.load(handle)
        except Exception as exc:
            messagebox.showwarning("大量収集", f"収集結果を読めません: {exc}")
            return

        normalized_csv = str(latest.get("normalized_csv", ""))
        manifest = str(latest.get("manifest", ""))
        review_count = int(latest.get("corporate_number_review_count", 0) or 0)
        review_queue = str(latest.get("corporate_number_review_queue_csv", ""))
        same_input = bool(latest.get("same_input_as_previous"))
        previous_run = latest.get("previous_same_input_run_id") or ""
        latest_source_id = str(latest.get("source_id", ""))
        flagged_company_count = int(latest.get("data_integrity_flagged_company_count", 0) or 0)
        flagged_company_rate = float(latest.get("data_integrity_flagged_company_rate", 0.0) or 0.0)
        flagged_record_count = int(latest.get("data_integrity_flagged_record_count", 0) or 0)
        flagged_record_rate = float(latest.get("data_integrity_flagged_record_rate", 0.0) or 0.0)
        top_integrity_flags = str(latest.get("data_integrity_top_flags", "") or "")
        if normalized_csv and latest_source_id == "job_file":
            self.job_file.set(normalized_csv)

        lines = [
            "大量収集が完了しました。",
            f"collection_run_id: {latest.get('collection_run_id')}",
            f"source_id: {latest.get('source_id', '')}",
            f"record_count: {latest.get('record_count', 0)}",
            f"error_count: {latest.get('error_count', 0)}",
            f"XBRL解析済み件数: {latest.get('xbrl_parsed_count', 0)}",
            f"法人番号の確認待ち: {review_count}",
            f"全収集レコード数: {latest.get('all_collected_record_count', 0)}",
            f"推定ユニーク企業数: {latest.get('unique_company_count', 0)}",
            f"分析候補企業数: {latest.get('analysis_candidate_company_count', 0)}",
            f"CSV出力企業数: {latest.get('selected_company_count', latest.get('unique_company_count', 0))}",
            f"分析候補外除外: {'on' if latest.get('exclude_non_analysis_candidates') else 'off'}",
            f"法人番号あり企業数: {latest.get('unique_corporate_number_count', 0)}",
            f"法人番号なし件数: {latest.get('corporate_number_missing_count', 0)}",
            f"収集警告件数: {latest.get('source_warning_count', 0)}",
            f"データ整合性フラグあり企業数: {flagged_company_count} ({flagged_company_rate:.1%})",
            f"データ整合性フラグありレコード数: {flagged_record_count} ({flagged_record_rate:.1%})",
            f"主な整合性フラグ: {top_integrity_flags}" if top_integrity_flags else "",
            f"重複グループ数: {latest.get('duplicate_group_count', 0)}",
            f"normalized_csv: {normalized_csv}",
            f"corporate_number_review_queue_csv: {review_queue}",
            f"all_collected_records_csv: {latest.get('all_collected_records_csv', '')}",
            f"collected_companies_csv: {latest.get('collected_companies_csv', '')}",
            f"collected_companies_all_csv: {latest.get('collected_companies_all_csv', '')}",
            f"analysis_candidate_companies_csv: {latest.get('analysis_candidate_companies_csv', '')}",
            f"collection_quality_summary_csv: {latest.get('collection_quality_summary_csv', '')}",
            f"duplicate_audit_csv: {latest.get('duplicate_audit_csv', '')}",
            f"manifest: {manifest}",
        ]
        if same_input:
            lines.append(f"同じ入力ファイルの過去収集があります: {previous_run}")
            lines.append("既存データは消さず、新しい収集runとして保存しました。")
        lines.append("")
        if latest_source_id == "job_file":
            lines.append("求人ファイル欄を収集済みCSVに更新しました。次に「実行」を押すと分析できます。")
        elif latest_source_id == "edinet_api":
            lines.append("EDINET収集結果を保存しました。求人ファイル欄は変更していません。")
            lines.append("EDINETのrecord_countは企業数ではなく書類件数です。企業数は「推定ユニーク企業数」を見てください。")
            lines.append("求人データと組み合わせた分析は、求人CSVまたはハローワーク収集データを用意してから実行します。")
        message = "\n".join(lines)
        self._append_log_now(message + "\n")
        self._refresh_simple_collection_summary(latest)
        if return_code == 0:
            messagebox.showinfo("大量収集", message)
        else:
            messagebox.showwarning("大量収集", message)

    def _on_collection_finished(self) -> None:
        self.collection_running = False
        self.collection_stop_supported = False
        if self.collection_progress_text.get() not in {"待機中", "収集が完了しました"}:
            self.collection_progress_text.set("収集が完了しました")
        self.collection_button.configure(state="normal")
        if hasattr(self, "collection_stop_button"):
            self.collection_stop_button.configure(state="disabled")
        if hasattr(self, "simple_collection_button"):
            self.simple_collection_button.configure(state="normal")
        if hasattr(self, "simple_collection_stop_button"):
            self.simple_collection_stop_button.configure(state="disabled")

    def _build_shokuba_command(self) -> tuple[list[str], Path]:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        shokuba_output_dir = output_dir / "shokuba_enrichment"
        command = [
            sys.executable,
            str(SHOKUBA_SCRIPT),
            "--shokuba-file",
            self.shokuba_file.get().strip(),
            "--company-file",
            self.shokuba_target_file.get().strip(),
            "--ranking-file",
            "",
            "--output-dir",
            str(shokuba_output_dir),
            "--top-n",
            "50",
        ]
        return command, shokuba_output_dir

    def _run_shokuba_enrichment(self) -> None:
        if self.shokuba_running:
            return
        if not SHOKUBA_SCRIPT.exists():
            messagebox.showerror("しょくばらぼ結合", f"結合プログラムが見つかりません: {SHOKUBA_SCRIPT}")
            return
        shokuba_path = Path(self.shokuba_file.get().strip())
        target_path = Path(self.shokuba_target_file.get().strip())
        if not shokuba_path.exists():
            messagebox.showwarning("しょくばらぼ結合", "しょくばらぼCSVを選択してください")
            return
        if not target_path.exists():
            messagebox.showwarning("しょくばらぼ結合", "結合したいCSVを選択してください")
            return
        self.shokuba_running = True
        if hasattr(self, "shokuba_button"):
            self.shokuba_button.configure(state="disabled")
        self.collection_progress_text.set("しょくばらぼ結合を開始しています...")
        self._append_log("\n[しょくばらぼ結合]\n")
        threading.Thread(target=self._shokuba_worker, daemon=True).start()

    def _shokuba_worker(self) -> None:
        command, output_dir = self._build_shokuba_command()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self._append_log("しょくばらぼ結合ジョブを開始します。\n")
        self._append_log(" ".join(f'"{item}"' if " " in item else item for item in command) + "\n\n")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(line)
            return_code = process.wait()
            self._append_log(f"\nしょくばらぼ結合 終了コード: {return_code}\n")
            self.after(0, lambda: self._summarize_shokuba_enrichment(output_dir, return_code))
        except Exception as exc:
            self._append_log(f"\nしょくばらぼ結合中にエラー: {exc}\n")
            self.after(0, lambda: messagebox.showerror("しょくばらぼ結合", f"しょくばらぼ結合に失敗しました: {exc}"))
        finally:
            self.after(0, self._on_shokuba_finished)

    def _summarize_shokuba_enrichment(self, output_dir: Path, return_code: int) -> None:
        report_path = output_dir / "shokuba_enrichment_report.json"
        if not report_path.exists():
            messagebox.showwarning("しょくばらぼ結合", f"結合レポートが見つかりません。\n終了コード: {return_code}")
            return
        try:
            with report_path.open("r", encoding="utf-8-sig") as handle:
                report = json.load(handle)
        except Exception as exc:
            messagebox.showwarning("しょくばらぼ結合", f"結合レポートを読めません: {exc}")
            return
        outputs = report.get("outputs", {}) if isinstance(report.get("outputs"), dict) else {}
        lines = [
            "しょくばらぼ結合が完了しました。",
            f"対象CSV件数: {report.get('company_input_count', 0)}",
            f"元CSVの法人番号あり件数: {report.get('company_with_original_corporate_number_count', report.get('company_with_corporate_number_count', 0))}",
            f"証券コードあり件数: {report.get('company_with_security_code_count', 0)}",
            f"一致件数: {report.get('shokuba_matched_company_count', 0)}",
            f"一致内訳: 法人番号 {report.get('shokuba_match_by_corporate_number_count', 0)} / 証券コード {report.get('shokuba_match_by_security_code_count', 0)} / 企業名 {report.get('shokuba_match_by_company_name_count', 0)}",
            f"一致率: {float(report.get('shokuba_match_rate', 0) or 0) * 100:.1f}%",
            f"抽出列数: {report.get('selected_shokuba_column_count', 0)}",
            f"要約CSV: {outputs.get('companies_with_shokuba_summary_metrics_csv', '')}",
            f"詳細CSV: {outputs.get('companies_with_shokuba_key_metrics_csv', '')}",
            f"取得率CSV: {outputs.get('shokuba_metric_coverage_csv', '')}",
        ]
        message = "\n".join(lines)
        self._append_log_now(message + "\n")
        self.collection_progress_text.set("しょくばらぼ結合が完了しました")
        if return_code == 0:
            messagebox.showinfo("しょくばらぼ結合", message)
        else:
            messagebox.showwarning("しょくばらぼ結合", message)

    def _on_shokuba_finished(self) -> None:
        self.shokuba_running = False
        if hasattr(self, "shokuba_button"):
            self.shokuba_button.configure(state="normal")

    def _build_women_activity_command(self) -> tuple[list[str], Path]:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        women_output_dir = output_dir / "women_activity_enrichment"
        command = [
            sys.executable,
            str(WOMEN_ACTIVITY_SCRIPT),
            "--women-file",
            self.women_activity_file.get().strip(),
            "--company-file",
            self.women_activity_target_file.get().strip(),
            "--output-dir",
            str(women_output_dir),
        ]
        return command, women_output_dir

    def _run_women_activity_enrichment(self) -> None:
        if self.women_activity_running:
            return
        if not WOMEN_ACTIVITY_SCRIPT.exists():
            messagebox.showerror("女性活躍DB結合", f"結合プログラムが見つかりません: {WOMEN_ACTIVITY_SCRIPT}")
            return
        women_path = Path(self.women_activity_file.get().strip())
        target_path = Path(self.women_activity_target_file.get().strip())
        if not women_path.exists():
            messagebox.showwarning("女性活躍DB結合", "女性活躍DB CSVを選択してください")
            return
        if not target_path.exists():
            messagebox.showwarning("女性活躍DB結合", "結合したいCSVを選択してください")
            return
        self.women_activity_running = True
        if hasattr(self, "women_activity_button"):
            self.women_activity_button.configure(state="disabled")
        self.collection_progress_text.set("女性活躍DB結合を開始しています...")
        self._append_log("\n[女性活躍DB結合]\n")
        threading.Thread(target=self._women_activity_worker, daemon=True).start()

    def _women_activity_worker(self) -> None:
        command, output_dir = self._build_women_activity_command()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self._append_log("女性活躍DB結合ジョブを開始します。\n")
        self._append_log(" ".join(f'"{item}"' if " " in item else item for item in command) + "\n\n")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(line)
            return_code = process.wait()
            self._append_log(f"\n女性活躍DB結合 終了コード: {return_code}\n")
            self.after(0, lambda: self._summarize_women_activity_enrichment(output_dir, return_code))
        except Exception as exc:
            self._append_log(f"\n女性活躍DB結合中にエラー: {exc}\n")
            self.after(0, lambda: messagebox.showerror("女性活躍DB結合", f"女性活躍DB結合に失敗しました: {exc}"))
        finally:
            self.after(0, self._on_women_activity_finished)

    def _summarize_women_activity_enrichment(self, output_dir: Path, return_code: int) -> None:
        report_path = output_dir / "women_activity_enrichment_report.json"
        if not report_path.exists():
            messagebox.showwarning("女性活躍DB結合", f"結合レポートが見つかりません。\n終了コード: {return_code}")
            return
        try:
            with report_path.open("r", encoding="utf-8-sig") as handle:
                report = json.load(handle)
        except Exception as exc:
            messagebox.showwarning("女性活躍DB結合", f"結合レポートを読めません: {exc}")
            return
        outputs = report.get("outputs", {}) if isinstance(report.get("outputs"), dict) else {}
        lines = [
            "女性活躍DB結合が完了しました。",
            f"対象CSV件数: {report.get('company_input_count', 0)}",
            f"元CSVの法人番号あり件数: {report.get('company_with_original_corporate_number_count', 0)}",
            f"証券コードあり件数: {report.get('company_with_security_code_count', 0)}",
            f"一致件数: {report.get('women_activity_matched_company_count', 0)}",
            f"一致内訳: 法人番号 {report.get('women_activity_match_by_corporate_number_count', 0)} / 証券コード {report.get('women_activity_match_by_security_code_count', 0)} / 企業名 {report.get('women_activity_match_by_company_name_count', 0)}",
            f"一致率: {float(report.get('women_activity_match_rate', 0) or 0) * 100:.1f}%",
            f"抽出列数: {report.get('selected_women_activity_column_count', 0)}",
            f"再結合防止で削除した既存女性活躍DB列: {report.get('dropped_existing_women_activity_column_count', 0)}",
            f"ランキング用CSV: {outputs.get('companies_with_women_activity_ranking_ready_csv', '')}",
            f"要約CSV: {outputs.get('companies_with_women_activity_summary_metrics_csv', '')}",
            f"詳細CSV: {outputs.get('companies_with_women_activity_key_metrics_csv', '')}",
            f"取得率CSV: {outputs.get('women_activity_metric_coverage_csv', '')}",
        ]
        message = "\n".join(lines)
        self._append_log_now(message + "\n")
        self.collection_progress_text.set("女性活躍DB結合が完了しました")
        if return_code == 0:
            messagebox.showinfo("女性活躍DB結合", message)
        else:
            messagebox.showwarning("女性活躍DB結合", message)

    def _on_women_activity_finished(self) -> None:
        self.women_activity_running = False
        if hasattr(self, "women_activity_button"):
            self.women_activity_button.configure(state="normal")

    def _build_pytrends_command(self) -> tuple[list[str], Path]:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        trends_output_dir = output_dir / "pytrends_enrichment"
        command = [
            sys.executable,
            str(PYTRENDS_SCRIPT),
            "--company-file",
            self.pytrends_target_file.get().strip(),
            "--output-dir",
            str(trends_output_dir),
            "--months",
            str(max(int(self.pytrends_months.get() or 12), 1)),
            "--geo",
            self.pytrends_geo.get().strip().upper() or "JP",
            "--query-suffix",
            self.pytrends_query_suffix.get().strip(),
            "--max-companies",
            str(max(int(self.pytrends_max_companies.get() or 0), 0)),
            "--delay-seconds",
            "2.0",
            "--cache-days",
            "7",
            "--retries",
            "2",
            "--alias-model",
            self.pytrends_alias_model.get().strip() or "gpt-5-nano-2025-08-07",
            "--alias-retries",
            "2",
        ]
        return command, trends_output_dir

    def _run_pytrends_enrichment(self) -> None:
        if self.pytrends_running:
            return
        if not PYTRENDS_SCRIPT.exists():
            messagebox.showerror("Google Trends結合", f"結合プログラムが見つかりません: {PYTRENDS_SCRIPT}")
            return
        target_path = Path(self.pytrends_target_file.get().strip())
        if not target_path.exists():
            messagebox.showwarning("Google Trends結合", "結合したいCSVを選択してください")
            return
        if not self._pytrends_module_available():
            self._show_pytrends_dependency_error()
            return
        self.pytrends_running = True
        self.pytrends_openai_key_value = self.pytrends_openai_api_key.get().strip()
        self.pytrends_output_lines = [
            "Google Trends収集・結合を開始しました。",
            f"入力CSV: {target_path}",
            "検索方式: GPT一般呼称（初回決定後はSQLiteキャッシュ）",
            f"使用Python: {sys.executable}",
        ]
        self.pytrends_button.configure(state="disabled")
        self.pytrends_stop_button.configure(state="normal")
        self.collection_progress_text.set("Google Trends収集・結合を開始しています...")
        self._render_pytrends_summary()
        self._append_log("\n[Google Trends収集・結合]\n")
        threading.Thread(target=self._pytrends_worker, daemon=True).start()

    @staticmethod
    def _pytrends_module_available() -> bool:
        try:
            return importlib.util.find_spec("pytrends") is not None
        except (ImportError, AttributeError, ValueError):
            return False

    def _pytrends_install_command(self) -> str:
        return f'"{sys.executable}" -m pip install -r "{APP_DIR / "requirements.txt"}"'

    def _show_pytrends_dependency_error(self) -> None:
        lines = [
            "[Google Trends 実行前チェックエラー]",
            "",
            "原因: pytrendsが現在のPython環境にインストールされていません。",
            f"使用Python: {sys.executable}",
            "",
            "Anaconda Promptまたはターミナルで次を1回実行してください:",
            self._pytrends_install_command(),
            "",
            "インストール後にアプリを再起動し、『動作環境確認』を押してください。",
        ]
        message = "\n".join(lines)
        self.collection_progress_text.set("Google Trends実行不可: pytrendsが未導入です")
        self._set_simple_summary(message)
        self._append_log_now(message + "\n")
        messagebox.showerror("Google Trends結合", message)

    def _check_pytrends_environment(self) -> None:
        if not self._pytrends_module_available():
            self._show_pytrends_dependency_error()
            return
        try:
            import pytrends  # type: ignore[import-not-found]

            version = str(getattr(pytrends, "__version__", "不明"))
        except Exception as exc:
            message = f"pytrendsは見つかりましたが、読み込みに失敗しました。\n{type(exc).__name__}: {exc}"
            self._set_simple_summary("[Google Trends 動作環境エラー]\n\n" + message)
            messagebox.showerror("Google Trends動作環境確認", message)
            return
        message = f"Google Trends動作環境は利用可能です。\nPython: {sys.executable}\npytrends: {version}"
        self._set_simple_summary("[Google Trends 動作環境確認]\n\n" + message)
        messagebox.showinfo("Google Trends動作環境確認", message)

    def _pytrends_worker(self) -> None:
        command, output_dir = self._build_pytrends_command()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        if self.pytrends_openai_key_value:
            env["OPENAI_API_KEY"] = self.pytrends_openai_key_value
        self._append_log("Google Trends収集・結合ジョブを開始します。\n")
        self._append_log(" ".join(f'"{item}"' if " " in item else item for item in command) + "\n\n")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            self.pytrends_process = process
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(line)
                clean = line.strip()
                if clean:
                    self.after(0, lambda value=clean: self._record_pytrends_output(value))
            return_code = process.wait()
            self._append_log(f"\nGoogle Trends収集・結合 終了コード: {return_code}\n")
            self.after(0, lambda code=return_code: self._summarize_pytrends_enrichment(output_dir, code))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._append_log(f"\nGoogle Trends収集・結合中にエラー: {error}\n")
            self.after(0, lambda value=error: self._show_pytrends_worker_error(value))
        finally:
            self.pytrends_process = None
            self.after(0, self._on_pytrends_finished)

    def _record_pytrends_output(self, line: str) -> None:
        self.pytrends_output_lines.append(line)
        self.pytrends_output_lines = self.pytrends_output_lines[-80:]
        if (
            line.startswith("Google Trends取得中:")
            or line.startswith("Google Trends再試行待ち:")
            or line.startswith("GPT企業呼称")
        ):
            self.collection_progress_text.set(line)
        self._render_pytrends_summary()

    def _render_pytrends_summary(self) -> None:
        lines = ["[Google Trends収集・結合 進捗]", ""]
        lines.extend(self.pytrends_output_lines[-35:])
        self._set_simple_summary("\n".join(lines))

    def _show_pytrends_worker_error(self, error: str) -> None:
        self.pytrends_output_lines.append("致命的エラー: " + error)
        self.collection_progress_text.set("Google Trends収集・結合でエラーが発生しました")
        self._render_pytrends_summary()
        messagebox.showerror("Google Trends結合", f"Google Trends結合に失敗しました。\n\n{error}")

    def _stop_pytrends_enrichment(self) -> None:
        process = self.pytrends_process
        if not process or process.poll() is not None:
            return
        self.collection_progress_text.set("Google Trends収集を中断しています...")
        try:
            process.terminate()
        except Exception as exc:
            messagebox.showerror("Google Trends結合", f"中断できませんでした: {exc}")

    def _summarize_pytrends_enrichment(self, output_dir: Path, return_code: int) -> None:
        report_path = output_dir / "pytrends_enrichment_report.json"
        if return_code != 0:
            failure_path = output_dir / "pytrends_failure.json"
            failure_message = ""
            if failure_path.exists():
                try:
                    failure = json.loads(failure_path.read_text(encoding="utf-8-sig"))
                    failure_message = f"{failure.get('error_type', '')}: {failure.get('error', '')}".strip(": ")
                except Exception as exc:
                    failure_message = f"失敗レポート読込エラー: {exc}"
            tail = "\n".join(self.pytrends_output_lines[-20:])
            lines = [
                "[Google Trends収集・結合 エラー]",
                "",
                f"終了コード: {return_code}",
                f"原因: {failure_message or '詳細を取得できませんでした'}",
                "前回の完成CSVは更新されていません。",
                f"失敗レポート: {failure_path if failure_path.exists() else '未作成'}",
                "",
                "最後の実行ログ:",
                tail or "（ログなし）",
            ]
            message = "\n".join(lines)
            self.collection_progress_text.set("Google Trends収集・結合に失敗しました")
            self._set_simple_summary(message)
            messagebox.showwarning("Google Trends結合", message)
            return
        if not report_path.exists():
            message = "処理は終了しましたが、今回の成功レポートが作成されていません。前回のCSVは開かないでください。"
            self.collection_progress_text.set("Google Trends結合結果を確認できません")
            self._set_simple_summary("[Google Trends収集・結合 エラー]\n\n" + message)
            messagebox.showwarning("Google Trends結合", message)
            return
        try:
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            messagebox.showwarning("Google Trends結合", f"結合レポートを読めません: {exc}")
            return
        outputs = report.get("outputs", {}) if isinstance(report.get("outputs"), dict) else {}
        lines = [
            "Google Trends収集・結合が完了しました。",
            f"入力CSV: {report.get('company_file', '')}",
            f"企業名として使用した列: {report.get('company_name_column', '')}",
            f"証券コードとして使用した列: {report.get('security_code_column', '')}",
            "検索方式: GPT一般呼称（初回決定後はSQLiteキャッシュ）",
            f"GPT呼称対象: {report.get('alias_target_count', 0)}",
            f"GPT API呼び出し: {report.get('alias_api_count', 0)}",
            f"保存済み呼称利用: {report.get('alias_cache_used_count', 0)}",
            f"GPT呼称エラー: {report.get('alias_error_count', 0)}",
            f"入力ファイル識別子: {str(report.get('company_file_sha256', ''))[:12]}",
            f"対象CSV件数: {report.get('company_input_count', 0)}",
            f"検索した企業数: {report.get('unique_query_count', 0)}",
            f"取得成功: {report.get('success_count', 0)}",
            f"データなし: {report.get('no_data_count', 0)}",
            f"エラー: {report.get('error_count', 0)}",
            f"キャッシュ利用: {report.get('cache_used_count', 0)}",
            f"成功率: {float(report.get('success_rate', 0) or 0) * 100:.1f}%",
            f"結合CSV: {outputs.get('companies_with_google_trends_csv', '')}",
            f"時系列CSV: {outputs.get('google_trends_time_series_csv', '')}",
            f"企業呼称一覧CSV: {outputs.get('company_search_aliases_csv', '')}",
            "注意: 指数は企業ごとの0〜100相対値で、企業間の絶対検索量比較には使えません。",
        ]
        message = "\n".join(lines)
        self._append_log_now(message + "\n")
        self._set_simple_summary(message)
        self.collection_progress_text.set("Google Trends収集・結合が完了しました")
        if return_code == 0:
            messagebox.showinfo("Google Trends結合", message)
        else:
            messagebox.showwarning("Google Trends結合", message)

    def _on_pytrends_finished(self) -> None:
        self.pytrends_running = False
        self.pytrends_button.configure(state="normal")
        self.pytrends_stop_button.configure(state="disabled")

    def _build_stock_bottom_command(self) -> tuple[list[str], Path]:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        stock_bottom_output_dir = output_dir / "stock_bottom_financial_enrichment"
        command = [
            sys.executable,
            str(STOCK_BOTTOM_SCRIPT),
            "--stock-file",
            self.stock_bottom_file.get().strip(),
            "--output-dir",
            str(stock_bottom_output_dir),
            "--lookback-days",
            str(max(int(self.stock_bottom_lookback_days.get() or 460), 1)),
        ]
        max_rows_text = self.stock_bottom_max_rows.get().strip()
        if max_rows_text:
            command.extend(["--max-rows", max_rows_text])
        return command, stock_bottom_output_dir

    def _run_stock_bottom_enrichment(self) -> None:
        if self.stock_bottom_running:
            return
        if not STOCK_BOTTOM_SCRIPT.exists():
            messagebox.showerror("底検知EDINET結合", f"結合プログラムが見つかりません: {STOCK_BOTTOM_SCRIPT}")
            return
        if not self.edinet_api_key.get().strip() and not os.environ.get("EDINET_API_KEY"):
            messagebox.showwarning("底検知EDINET結合", "EDINET APIキーを入力してから実行してください")
            return
        stock_path = Path(self.stock_bottom_file.get().strip())
        if not stock_path.exists():
            messagebox.showwarning("底検知EDINET結合", "底検知CSVを選択してください")
            return
        max_rows_text = self.stock_bottom_max_rows.get().strip()
        if max_rows_text and (not max_rows_text.isdigit() or int(max_rows_text) <= 0):
            messagebox.showwarning("底検知EDINET結合", "上から処理する件数は、空欄または1以上の整数で入力してください")
            return
        self.stock_bottom_running = True
        self.stock_bottom_progress_lines = ["底検知EDINET結合を開始しました。"]
        self._render_stock_bottom_progress_summary()
        if hasattr(self, "stock_bottom_button"):
            self.stock_bottom_button.configure(state="disabled")
        self.collection_progress_text.set("底検知CSVにEDINET財務データを追加しています...")
        self._append_log("\n[底検知EDINET結合]\n")
        threading.Thread(target=self._stock_bottom_worker, daemon=True).start()

    def _stock_bottom_worker(self) -> None:
        command, output_dir = self._build_stock_bottom_command()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        api_key = self.edinet_api_key.get().strip()
        if api_key:
            env["EDINET_API_KEY"] = api_key
        self._append_log("底検知EDINET結合ジョブを開始します。\n")
        self._append_log(" ".join(f'"{item}"' if " " in item else item for item in command) + "\n\n")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(line)
            return_code = process.wait()
            self._append_log(f"\n底検知EDINET結合 終了コード: {return_code}\n")
            self.after(0, lambda: self._summarize_stock_bottom_enrichment(output_dir, return_code))
        except Exception as exc:
            self._append_log(f"\n底検知EDINET結合中にエラー: {exc}\n")
            self.after(0, lambda: messagebox.showerror("底検知EDINET結合", f"底検知EDINET結合に失敗しました: {exc}"))
        finally:
            self.after(0, self._on_stock_bottom_finished)

    def _summarize_stock_bottom_enrichment(self, output_dir: Path, return_code: int) -> None:
        report_path = output_dir / "stock_bottom_financial_enrichment_report.json"
        if not report_path.exists():
            messagebox.showwarning("底検知EDINET結合", f"結合レポートが見つかりません。\n終了コード: {return_code}")
            return
        try:
            with report_path.open("r", encoding="utf-8-sig") as handle:
                report = json.load(handle)
        except Exception as exc:
            messagebox.showwarning("底検知EDINET結合", f"結合レポートを読めません: {exc}")
            return
        counts = report.get("counts", {}) if isinstance(report.get("counts"), dict) else {}
        outputs = report.get("outputs", {}) if isinstance(report.get("outputs"), dict) else {}
        bottom_year_counts = report.get("bottom_year_counts", {}) if isinstance(report.get("bottom_year_counts"), dict) else {}
        bottom_year_summary = ", ".join(
            f"{year}年={count}件" if year != "底検知日なし" else f"{year}={count}件"
            for year, count in bottom_year_counts.items()
        )
        lines = [
            "底検知EDINET結合が完了しました。",
            f"対象行数: {counts.get('input_rows', 0)}",
            f"底検知年別: {bottom_year_summary}" if bottom_year_summary else "",
            f"書類発見行数: {counts.get('document_found', 0)}",
            f"XBRL解析成功行数: {counts.get('xbrl_parsed', 0)}",
            f"書類未発見行数: {counts.get('document_not_found', 0)}",
            f"底検知日なし行数: {counts.get('bottom_date_missing', 0)}",
            f"銘柄コードなし行数: {counts.get('stock_code_missing', 0)}",
            f"エラー行数: {counts.get('errors', 0)}",
            f"一括探索対象日数: {report.get('batch_required_date_count', 0)}",
            f"索引化したEDINET書類数: {report.get('batch_indexed_document_count', 0)}",
            f"索引化した証券コード数: {report.get('batch_indexed_sec_code_count', 0)}",
            f"EDINET日次一覧API取得回数: {report.get('edinet_document_list_network_fetch_count', 0)}",
            f"EDINET日次一覧キャッシュ利用回数: {report.get('edinet_document_list_cache_hit_count', 0)}",
            f"完成CSV: {outputs.get('stock_bottom_with_edinet_financials_csv', '')}",
        ]
        message = "\n".join(lines)
        self._append_log_now(message + "\n")
        self.stock_bottom_progress_lines.append("")
        self.stock_bottom_progress_lines.extend(line for line in lines if line)
        self.stock_bottom_progress_lines = self.stock_bottom_progress_lines[-40:]
        self._render_stock_bottom_progress_summary()
        self.collection_progress_text.set("底検知EDINET結合が完了しました")
        if return_code == 0:
            messagebox.showinfo("底検知EDINET結合", message)
        else:
            messagebox.showwarning("底検知EDINET結合", message)

    def _on_stock_bottom_finished(self) -> None:
        self.stock_bottom_running = False
        if hasattr(self, "stock_bottom_button"):
            self.stock_bottom_button.configure(state="normal")

    def _record_stock_bottom_progress(self, line: str) -> None:
        if not hasattr(self, "stock_bottom_progress_lines"):
            self.stock_bottom_progress_lines = []
        clean = line.strip()
        if not clean:
            return
        if not clean.startswith("底検知EDINET"):
            return
        self.stock_bottom_progress_lines.append(clean)
        self.stock_bottom_progress_lines = self.stock_bottom_progress_lines[-40:]
        self._render_stock_bottom_progress_summary()

    def _render_stock_bottom_progress_summary(self) -> None:
        if not hasattr(self, "simple_summary_text"):
            return
        lines = ["[底検知EDINET結合 進捗]", ""]
        lines.extend(self.stock_bottom_progress_lines[-40:])
        self.simple_summary_text.delete("1.0", tk.END)
        self.simple_summary_text.insert(tk.END, "\n".join(lines) + "\n")
        self.simple_summary_text.see(tk.END)

    def _set_simple_summary(self, message: str) -> None:
        if not hasattr(self, "simple_summary_text"):
            return
        self.simple_summary_text.delete("1.0", tk.END)
        self.simple_summary_text.insert(tk.END, message.rstrip() + "\n")
        self.simple_summary_text.see(tk.END)

    def _refresh_simple_collection_summary(self, latest: dict[str, object]) -> None:
        if not hasattr(self, "simple_summary_text"):
            return
        flagged_company_count = int(latest.get("data_integrity_flagged_company_count", 0) or 0)
        flagged_company_rate = float(latest.get("data_integrity_flagged_company_rate", 0.0) or 0.0)
        flagged_record_count = int(latest.get("data_integrity_flagged_record_count", 0) or 0)
        flagged_record_rate = float(latest.get("data_integrity_flagged_record_rate", 0.0) or 0.0)
        top_integrity_flags = str(latest.get("data_integrity_top_flags", "") or "")
        lines = [
            f"収集ID: {latest.get('collection_run_id', '')}",
            f"収集元: {latest.get('source_id', '')}",
            f"収集レコード数: {latest.get('record_count', 0)}",
            f"推定ユニーク企業数: {latest.get('unique_company_count', 0)}",
            f"分析候補企業数: {latest.get('analysis_candidate_company_count', 0)}",
            f"CSV出力企業数: {latest.get('selected_company_count', latest.get('unique_company_count', 0))}",
            f"分析候補外除外: {'on' if latest.get('exclude_non_analysis_candidates') else 'off'}",
            f"XBRL解析済み件数: {latest.get('xbrl_parsed_count', 0)}",
            f"法人番号あり企業数: {latest.get('unique_corporate_number_count', 0)}",
            f"法人番号なし件数: {latest.get('corporate_number_missing_count', 0)}",
            f"整合性フラグあり企業数: {flagged_company_count} ({flagged_company_rate:.1%})",
            f"整合性フラグありレコード数: {flagged_record_count} ({flagged_record_rate:.1%})",
            f"主な整合性フラグ: {top_integrity_flags}" if top_integrity_flags else "",
            f"エラー数: {latest.get('error_count', 0)}",
            "",
            "最近のCSV:",
            str(latest.get("collected_companies_csv", "")),
            "",
            "全件CSV:",
            str(latest.get("collected_companies_all_csv", "")),
            "",
            "分析候補CSV:",
            str(latest.get("analysis_candidate_companies_csv", "")),
        ]
        self.simple_summary_text.delete("1.0", tk.END)
        self.simple_summary_text.insert(tk.END, "\n".join(lines) + "\n")

    def _build_edinet_connection_test_command(self) -> tuple[list[str], Path]:
        test_output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "edinet_connection_test"
        job_file = Path(self.job_file.get().strip() or APP_DIR / "work" / "job_input_template.csv")
        if not job_file.exists():
            job_file = APP_DIR / "work" / "job_input_template.csv"
        command = [
            sys.executable,
            str(ENGINE_SCRIPT),
            "--job-file",
            str(job_file),
            "--output-dir",
            str(test_output_dir),
            "--config",
            str(self._config_path()),
            "--use-edinet-api",
            "--edinet-dry-run",
            "--edinet-lookback-days",
            "1",
            "--edinet-xbrl-limit",
            "0",
        ]
        return command, test_output_dir

    def _run_edinet_connection_test(self) -> None:
        if self.connection_test_running:
            return
        if not self.edinet_api_key.get().strip() and not os.environ.get("EDINET_API_KEY"):
            messagebox.showwarning("EDINET接続確認", "EDINET APIキーを入力してから接続確認を実行してください")
            return
        self.connection_test_running = True
        self.edinet_test_button.configure(state="disabled")
        threading.Thread(target=self._edinet_connection_test_worker, daemon=True).start()

    def _edinet_connection_test_worker(self) -> None:
        command, test_output_dir = self._build_edinet_connection_test_command()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        api_key = self.edinet_api_key.get().strip()
        if api_key:
            env["EDINET_API_KEY"] = api_key
        self._append_log("\n[EDINET接続確認]\n")
        self._append_log("1日分のEDINET書類一覧をドライラン取得します。\n")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(line)
            return_code = process.wait()
            self._append_log(f"EDINET接続確認 終了コード: {return_code}\n")
            self.after(0, lambda: self._summarize_edinet_connection_test(test_output_dir, return_code))
        except Exception as exc:
            self._append_log(f"EDINET接続確認中にエラー: {exc}\n")
            self.after(0, lambda: messagebox.showerror("EDINET接続確認", f"接続確認に失敗しました: {exc}"))
        finally:
            self.after(0, self._on_edinet_connection_test_finished)

    def _summarize_edinet_connection_test(self, test_output_dir: Path, return_code: int) -> None:
        manifest_path = test_output_dir / "run_manifest.json"
        errors_path = test_output_dir / "errors.jsonl"
        snapshot_path = test_output_dir / "edinet_documents_snapshot.csv"
        warnings: list[str] = []
        errors: list[str] = []
        if errors_path.exists():
            with errors_path.open("r", encoding="utf-8", newline="") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    level = event.get("level")
                    message = str(event.get("message", ""))
                    event_type = str(event.get("event_type", ""))
                    if level == "ERROR":
                        errors.append(f"{event_type}: {message}")
                    elif level == "WARNING":
                        warnings.append(f"{event_type}: {message}")

        document_count = 0
        if snapshot_path.exists():
            with snapshot_path.open("r", encoding="utf-8-sig", newline="") as handle:
                document_count = max(sum(1 for _ in handle) - 1, 0)

        manifest_note = ""
        if manifest_path.exists():
            try:
                with manifest_path.open("r", encoding="utf-8-sig") as handle:
                    manifest = json.load(handle)
                manifest_note = f"run_id: {manifest.get('run_id')}\n"
            except Exception:
                manifest_note = ""

        if return_code == 0 and not errors and not any("edinet_api_key_missing" in item for item in warnings):
            message = (
                "EDINET接続確認が完了しました。\n"
                f"{manifest_note}"
                f"書類候補スナップショット件数: {document_count}\n"
                f"出力先: {test_output_dir}"
            )
            if warnings:
                message += "\n\n警告:\n" + "\n".join(warnings[:5])
            messagebox.showinfo("EDINET接続確認", message)
        else:
            detail = "\n".join((errors or warnings or ["詳細ログを確認してください"])[:8])
            messagebox.showwarning(
                "EDINET接続確認",
                f"EDINET接続確認で確認事項があります。\n{manifest_note}出力先: {test_output_dir}\n\n{detail}",
            )

    def _on_edinet_connection_test_finished(self) -> None:
        self.connection_test_running = False
        self.edinet_test_button.configure(state="normal")

    def _run(self) -> None:
        if self.running:
            return
        if not ENGINE_SCRIPT.exists():
            messagebox.showerror("エラー", f"実行ファイルが見つかりません: {ENGINE_SCRIPT}")
            return
        self.running = True
        self.run_button.configure(state="disabled")
        self.log_text.delete("1.0", tk.END)
        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self) -> None:
        command = self._build_command()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        api_key = self.edinet_api_key.get().strip()
        if api_key:
            env["EDINET_API_KEY"] = api_key
        self._append_log("実行開始\n")
        self._append_log(" ".join(command) + "\n\n")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(line)
            return_code = process.wait()
            self._append_log(f"\n終了コード: {return_code}\n")
        except Exception as exc:
            self._append_log(f"\n実行中にエラー: {exc}\n")
        finally:
            self.after(0, self._on_run_finished)

    def _append_log(self, message: str) -> None:
        self.after(0, lambda: self._append_log_now(message))

    def _append_log_now(self, message: str) -> None:
        self.log_text.insert(tk.END, message)
        self.log_text.see(tk.END)
        self._update_collection_progress_from_log(message)

    def _update_collection_progress_from_log(self, message: str) -> None:
        progress_markers = [
            "止めるまで収集中:",
            "個別CSV保存中:",
            "定期保存完了:",
            "EDINET書類一覧取得中:",
            "EDINET書類一覧取得完了:",
            "EDINET書類処理中:",
            "XBRL取得中:",
            "底検知EDINET年別サマリー:",
            "底検知EDINET結合中:",
            "底検知EDINET一括取得準備:",
            "底検知EDINET一括取得中:",
            "底検知EDINET日付探索中:",
            "底検知EDINET財務取得状況:",
            "保存対象なし:",
            "中断前の未反映バッチを集約CSVへ反映しています。",
            "集約CSV更新完了:",
            "中断要求を検知したため",
            "大量収集 終了コード:",
        ]
        for line in message.splitlines():
            for marker in progress_markers:
                if marker in line:
                    self.collection_progress_text.set(f"{marker} {line.split(marker, 1)[1].strip()}" if marker != "大量収集 終了コード:" else "収集が完了しました")
                    if line.strip().startswith("底検知EDINET"):
                        self._record_stock_bottom_progress(line)
                    break

    def _on_run_finished(self) -> None:
        self.running = False
        self.run_button.configure(state="normal")
        self._refresh_manifest_summary()

    def _refresh_manifest_summary(self) -> None:
        self.summary_text.delete("1.0", tk.END)
        manifest_path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "run_manifest.json"
        if not manifest_path.exists():
            self.summary_text.insert(tk.END, "まだ実行結果がありません。\n")
            self._refresh_simple_collection_summary_from_disk()
            return
        try:
            with manifest_path.open("r", encoding="utf-8-sig") as handle:
                manifest = json.load(handle)
        except Exception as exc:
            self.summary_text.insert(tk.END, f"run_manifest.json を読めません: {exc}\n")
            return
        lines = [
            f"app_version: {manifest.get('app_version', f'{APP_VERSION} ({APP_BUILD_DATE})')}",
            f"run_id: {manifest.get('run_id')}",
            f"mode: {manifest.get('mode')}",
            f"ranking_count: {manifest.get('ranking_count')}",
            f"input_count: {manifest.get('input_count')}",
            f"join_matched_count: {manifest.get('join_matched_count')}",
            f"join_failed_count: {manifest.get('join_failed_count')}",
            f"join_name_mismatch_count: {manifest.get('join_name_mismatch_count')}",
            f"warning_count: {manifest.get('warning_count')}",
            f"error_count: {manifest.get('error_count')}",
        ]
        self.summary_text.insert(tk.END, "\n".join(lines) + "\n")
        self._refresh_ranking_preview()
        self._refresh_issue_preview()
        self._refresh_simple_collection_summary_from_disk()

    def _refresh_simple_collection_summary_from_disk(self) -> None:
        if getattr(self, "stock_bottom_running", False):
            return
        latest_path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "latest_collection.json"
        if not latest_path.exists() or not hasattr(self, "simple_summary_text"):
            return
        try:
            with latest_path.open("r", encoding="utf-8-sig") as handle:
                latest = json.load(handle)
        except Exception:
            return
        self._refresh_simple_collection_summary(latest)

    def _refresh_ranking_preview(self) -> None:
        if not hasattr(self, "ranking_tree"):
            return
        for item in self.ranking_tree.get_children():
            self.ranking_tree.delete(item)
        ranking_path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "ranking.csv"
        if not ranking_path.exists():
            return
        try:
            with ranking_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for index, row in enumerate(reader, start=1):
                    if index > 20:
                        break
                    score = row.get("total_score", "")
                    try:
                        score = f"{float(score):.2f}"
                    except (TypeError, ValueError):
                        pass
                    self.ranking_tree.insert(
                        "",
                        tk.END,
                        values=(index, row.get("company_name", ""), score, row.get("join_status", ""), row.get("join_name_check_status", "")),
                    )
        except Exception as exc:
            self._append_log_now(f"ランキングプレビューを読めません: {exc}\n")

    def _refresh_issue_preview(self) -> None:
        if not hasattr(self, "issue_tree"):
            return
        for item in self.issue_tree.get_children():
            self.issue_tree.delete(item)
        records_path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "all_records_clean.csv"
        if not records_path.exists():
            self.issue_count_text.set("0件")
            return
        try:
            with records_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                count = 0
                total_issues = 0
                for row in reader:
                    join_status = row.get("join_status", "")
                    name_status = row.get("join_name_check_status", "")
                    score_status = row.get("score_status", "")
                    if join_status == "matched" and name_status in ("", "ok", "not_checked") and score_status in ("ok", ""):
                        continue
                    total_issues += 1
                    if not self._issue_row_matches_filter(row):
                        continue
                    reason_parts: list[str] = []
                    if join_status != "matched":
                        reason_parts.append(row.get("join_failure_reason", join_status))
                    if name_status not in ("", "ok", "not_checked"):
                        reason_parts.append(name_status)
                    if score_status not in ("", "ok"):
                        reason_parts.append(score_status)
                    memo = row.get("join_diagnostic_notes") or row.get("join_name_check_notes") or row.get("missing_fields") or ""
                    self.issue_tree.insert(
                        "",
                        tk.END,
                        values=(
                            row.get("job_id", ""),
                            row.get("company_name", ""),
                            ", ".join(part for part in reason_parts if part),
                            f"{join_status} / {score_status}",
                            memo,
                        ),
                    )
                    count += 1
                    if count >= 50:
                        break
                self.issue_count_text.set(f"{count}件表示 / {total_issues}件")
        except Exception as exc:
            self._append_log_now(f"注意行プレビューを読めません: {exc}\n")

    def _issue_row_matches_filter(self, row: dict[str, str]) -> bool:
        filter_value = self.issue_filter.get()
        join_status = row.get("join_status", "")
        name_status = row.get("join_name_check_status", "")
        score_status = row.get("score_status", "")
        corp_status = row.get("corporate_number_resolution_status", "")
        edinet_status = row.get("edinet_status", "")
        xbrl_status = row.get("xbrl_parse_status", "")

        if filter_value == "JOIN失敗" and join_status == "matched":
            return False
        if filter_value == "企業名不一致" and name_status not in {"name_mismatch", "estimated_number_name_mismatch"}:
            return False
        if filter_value == "スコア不足" and score_status not in {"partial", "insufficient_data"}:
            return False
        if filter_value == "法人番号補完" and corp_status not in {"estimated", "ambiguous", "not_found"}:
            return False
        if filter_value == "EDINET/XBRL" and not (
            edinet_status in {"not_found", "api_failed", "xbrl_not_parsed"}
            or xbrl_status in {"not_parsed", "parse_failed", "missing_fields"}
        ):
            return False

        search = self.issue_search.get().strip().lower()
        if search:
            haystack = " ".join(str(value) for value in row.values()).lower()
            if search not in haystack:
                return False
        return True

    def _clear_issue_filter(self) -> None:
        self.issue_filter.set("すべて")
        self.issue_search.set("")
        self._refresh_issue_preview()

    def _export_filtered_issues(self) -> None:
        records = self._filtered_issue_records(limit=None)
        if not records:
            messagebox.showwarning("注意行CSV保存", "保存する注意行がありません")
            return
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = output_dir / f"issue_records_{stamp}.csv"
        try:
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()))
                writer.writeheader()
                writer.writerows(records)
            messagebox.showinfo("注意行CSV保存", f"注意行CSVを保存しました。\n{path}\n\n件数: {len(records)}")
            self._append_log_now(f"注意行CSVを保存しました: {path}\n")
        except Exception as exc:
            messagebox.showerror("注意行CSV保存", f"保存できません: {exc}")

    def _is_issue_row(self, row: dict[str, str]) -> bool:
        join_status = row.get("join_status", "")
        name_status = row.get("join_name_check_status", "")
        score_status = row.get("score_status", "")
        return not (join_status == "matched" and name_status in ("", "ok", "not_checked") and score_status in ("ok", ""))

    def _filtered_issue_records(self, limit: int | None) -> list[dict[str, str]]:
        records_path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "all_records_clean.csv"
        if not records_path.exists():
            return []
        records: list[dict[str, str]] = []
        with records_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if not self._is_issue_row(row):
                    continue
                if not self._issue_row_matches_filter(row):
                    continue
                records.append(row)
                if limit is not None and len(records) >= limit:
                    break
        return records

    def _load_all_records(self) -> list[dict[str, str]]:
        records_path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "all_records_clean.csv"
        if not records_path.exists():
            return []
        with records_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def _show_selected_detail(self, tree: ttk.Treeview) -> None:
        selected = tree.selection()
        if not selected:
            return
        values = tree.item(selected[0], "values")
        if not values:
            return
        if tree is self.ranking_tree:
            company_name = str(values[1])
            job_id = ""
        else:
            job_id = str(values[0])
            company_name = str(values[1])

        target_row: dict[str, str] | None = None
        for row in self._load_all_records():
            if job_id and row.get("job_id") == job_id:
                target_row = row
                break
            if not job_id and row.get("company_name") == company_name:
                target_row = row
                break
        if not target_row:
            messagebox.showwarning("詳細表示", "該当行を all_records_clean.csv から見つけられませんでした")
            return
        self._show_record_detail_window(target_row)

    def _show_record_detail_window(self, row: dict[str, str]) -> None:
        window = tk.Toplevel(self)
        window.title(f"詳細: {row.get('company_name', '')}")
        window.geometry("880x620")
        window.minsize(720, 480)
        window.rowconfigure(0, weight=1)
        window.columnconfigure(0, weight=1)
        text = tk.Text(window, wrap="word")
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(window, command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)

        important = [
            "job_id",
            "company_name",
            "corporate_number",
            "corporate_number_status",
            "corporate_number_resolution_status",
            "corporate_number_resolution_confidence",
            "join_status",
            "join_failure_reason",
            "join_diagnostic_notes",
            "join_name_check_status",
            "join_name_check_notes",
            "score_status",
            "missing_fields",
            "total_score",
            "rd_score_norm",
            "growth_score_norm",
            "wage_score_norm",
            "tenure_score_norm",
            "location_score_norm",
            "edinet_status",
            "xbrl_parse_status",
            "xbrl_extracted_fields",
            "xbrl_missing_fields",
            "xbrl_diagnostic_notes",
            "data_quality_notes",
        ]
        lines: list[str] = []
        for key in important:
            if key in row:
                lines.append(f"{key}: {row.get(key, '')}")
        lines.append("")
        lines.append("--- all columns ---")
        for key, value in row.items():
            if key not in important:
                lines.append(f"{key}: {value}")
        text.insert(tk.END, "\n".join(lines))
        text.configure(state="disabled")

    def _latest_collection_record(self) -> dict[str, object] | None:
        latest_path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "latest_collection.json"
        if not latest_path.exists():
            messagebox.showwarning("大量収集", "まだ収集結果がありません")
            return None
        try:
            with latest_path.open("r", encoding="utf-8-sig") as handle:
                return json.load(handle)
        except Exception as exc:
            messagebox.showwarning("大量収集", f"収集結果を読めません: {exc}")
            return None

    def _use_latest_collection_csv(self) -> None:
        latest = self._latest_collection_record()
        if not latest:
            return
        path = str(latest.get("normalized_csv", ""))
        if not path:
            messagebox.showwarning("大量収集", "最新収集CSVが見つかりません")
            return
        self.job_file.set(path)
        self._append_log_now(f"求人ファイル欄を最新収集CSVに更新しました: {path}\n")

    def _open_latest_review_queue(self) -> None:
        latest = self._latest_collection_record()
        if not latest:
            return
        path = str(latest.get("corporate_number_review_queue_csv", ""))
        if not path:
            messagebox.showwarning("法人番号確認CSV", "法人番号確認CSVが見つかりません")
            return
        self._open_file(Path(path))

    def _open_latest_collection_folder(self) -> None:
        latest = self._latest_collection_record()
        if not latest:
            return
        manifest_text = str(latest.get("manifest", ""))
        if not manifest_text:
            messagebox.showwarning("大量収集", "最新収集フォルダが見つかりません")
            return
        manifest = Path(manifest_text)
        self._open_file(manifest.parent)

    def _open_all_collected_records(self) -> None:
        latest = self._latest_collection_record()
        if not latest:
            return
        path = str(latest.get("all_collected_records_csv", ""))
        if not path:
            path = str(Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "all_collected_records.csv")
        self._open_file(Path(path))

    def _open_collection_quality_summary(self) -> None:
        latest = self._latest_collection_record()
        if not latest:
            return
        path = str(latest.get("collection_quality_summary_csv", ""))
        if not path:
            path = str(Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "collection_quality_summary.csv")
        self._open_file(Path(path))

    def _open_duplicate_audit(self) -> None:
        latest = self._latest_collection_record()
        if not latest:
            return
        path = str(latest.get("duplicate_audit_csv", ""))
        if not path:
            path = str(Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "duplicate_audit.csv")
        self._open_file(Path(path))

    def _open_collected_companies(self) -> None:
        latest = self._latest_collection_record()
        if not latest:
            return
        path = str(latest.get("collected_companies_csv", ""))
        if not path:
            path = str(Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "collected_companies.csv")
        self._open_file(Path(path))

    def _use_collected_companies_for_shokuba(self) -> None:
        latest = self._latest_collection_record()
        path = ""
        if latest:
            path = str(latest.get("collected_companies_csv", ""))
        if not path:
            path = str(Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "collected_companies.csv")
        self.shokuba_target_file.set(path)

    def _use_collected_companies_for_women_activity(self) -> None:
        latest = self._latest_collection_record()
        path = ""
        if latest:
            path = str(latest.get("collected_companies_csv", ""))
        if not path:
            path = str(Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "collected_companies.csv")
        self.women_activity_target_file.set(path)

    def _use_collected_companies_for_pytrends(self) -> None:
        latest = self._latest_collection_record()
        path = ""
        if latest:
            path = str(latest.get("collected_companies_csv", ""))
        if not path:
            path = str(Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "collected_companies.csv")
        self.pytrends_target_file.set(path)

    def _open_shokuba_summary_csv(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "shokuba_enrichment" / "companies_with_shokuba_summary_metrics.csv"
        self._open_file(path)

    def _open_shokuba_key_metrics_csv(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "shokuba_enrichment" / "companies_with_shokuba_key_metrics.csv"
        self._open_file(path)

    def _open_shokuba_output_dir(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "shokuba_enrichment"
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("しょくばらぼ結合", f"フォルダを開けません: {exc}")

    def _open_women_activity_summary_csv(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "women_activity_enrichment" / "companies_with_women_activity_summary_metrics.csv"
        self._open_file(path)

    def _open_women_activity_key_metrics_csv(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "women_activity_enrichment" / "companies_with_women_activity_key_metrics.csv"
        self._open_file(path)

    def _open_women_activity_output_dir(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "women_activity_enrichment"
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("女性活躍DB結合", f"フォルダを開けません: {exc}")

    def _open_pytrends_enriched_csv(self) -> None:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "pytrends_enrichment"
        if not self._pytrends_latest_run_succeeded(output_dir):
            return
        path = output_dir / "companies_with_google_trends.csv"
        self._open_file(path)

    def _open_pytrends_series_csv(self) -> None:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "pytrends_enrichment"
        if not self._pytrends_latest_run_succeeded(output_dir):
            return
        path = output_dir / "google_trends_time_series.csv"
        self._open_file(path)

    @staticmethod
    def _pytrends_latest_run_succeeded(output_dir: Path) -> bool:
        report_path = output_dir / "pytrends_enrichment_report.json"
        failure_path = output_dir / "pytrends_failure.json"
        if failure_path.exists() and (
            not report_path.exists() or failure_path.stat().st_mtime > report_path.stat().st_mtime
        ):
            reason = ""
            try:
                failure = json.loads(failure_path.read_text(encoding="utf-8-sig"))
                reason = f"{failure.get('error_type', '')}: {failure.get('error', '')}".strip(": ")
            except Exception:
                reason = "失敗レポートを確認してください"
            messagebox.showwarning(
                "Google Trends結合",
                "直近の結合処理は失敗しています。表示可能なCSVは前回成功時の古いものです。\n\n"
                f"原因: {reason}",
            )
            return False
        if not report_path.exists():
            messagebox.showwarning("Google Trends結合", "成功した結合結果がまだありません。")
            return False
        return True

    def _open_pytrends_output_dir(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "pytrends_enrichment"
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Google Trends結合", f"フォルダを開けません: {exc}")

    def _open_stock_bottom_enriched_csv(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "stock_bottom_financial_enrichment" / "stock_bottom_with_edinet_financials.csv"
        self._open_file(path)

    def _open_stock_bottom_output_dir(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR) / "stock_bottom_financial_enrichment"
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("底検知EDINET結合", f"フォルダを開けません: {exc}")

    def _open_all_collected_companies(self) -> None:
        latest = self._latest_collection_record()
        if not latest:
            return
        path = str(latest.get("collected_companies_all_csv", ""))
        if not path:
            path = str(Path(self.output_dir.get().strip() or OUTPUT_DIR) / "collections" / "collected_companies_all.csv")
        self._open_file(Path(path))

    def _open_file(self, path: Path) -> None:
        if not path.exists():
            messagebox.showwarning("ファイルを開く", f"ファイルが見つかりません: {path}")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("ファイルを開く", f"ファイルを開けません: {exc}")

    def _open_latest_excel(self) -> None:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        files = sorted(output_dir.glob("company_scoring_result_*.xlsx"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            messagebox.showwarning("Excelを開く", "Excel出力が見つかりません")
            return
        self._open_file(files[0])

    def _open_ranking_csv(self) -> None:
        self._open_file(Path(self.output_dir.get().strip() or OUTPUT_DIR) / "ranking.csv")

    def _open_output_dir(self) -> None:
        path = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("エラー", f"フォルダを開けません: {exc}")

    def _create_debug_bundle(self) -> None:
        output_dir = Path(self.output_dir.get().strip() or OUTPUT_DIR)
        if not output_dir.exists():
            messagebox.showwarning("診断ZIP作成", "出力フォルダがまだありません")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bundle_path = output_dir / f"debug_bundle_{stamp}.zip"
        candidates = [
            output_dir / "run_manifest.json",
            output_dir / "errors.jsonl",
            output_dir / "ranking.csv",
            output_dir / "all_records_clean.csv",
            output_dir / "score_components.csv",
            output_dir / "self_check_report.json",
            self._config_path(),
        ]
        candidates.extend(sorted((output_dir / "logs").glob("*.log"))[-5:])
        candidates.extend(sorted((output_dir / "logs").glob("*_events.jsonl"))[-5:])

        written = 0
        try:
            with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "debug_bundle_info.txt",
                    f"app_version={APP_VERSION}\napp_build_date={APP_BUILD_DATE}\ncreated_at={datetime.now().isoformat(timespec='seconds')}\n",
                )
                written += 1
                for path in candidates:
                    if path.exists() and path.is_file():
                        try:
                            archive.write(path, arcname=path.name if path.parent == output_dir else str(path.relative_to(APP_DIR)))
                            written += 1
                        except ValueError:
                            archive.write(path, arcname=path.name)
                            written += 1
            messagebox.showinfo("診断ZIP作成", f"診断ZIPを作成しました。\n{bundle_path}\n\n収録ファイル数: {written}")
            self._append_log_now(f"診断ZIPを作成しました: {bundle_path}\n")
        except Exception as exc:
            messagebox.showerror("診断ZIP作成", f"診断ZIPを作成できません: {exc}")


def main() -> None:
    app = CompanyScoringTool()
    app.mainloop()


if __name__ == "__main__":
    main()
