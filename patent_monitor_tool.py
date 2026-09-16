from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import subprocess
import threading
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from patent_monitor.pipeline import (
    APP_VERSION,
    EPOOPSProvider,
    GeminiReviewer,
    OpenAIReviewer,
    PatentPipeline,
    PipelineConfig,
    mock_patents,
    read_companies,
    read_patent_csv,
)
from patent_monitor.secure_store import delete_credentials, load_credentials, save_credentials
from patent_monitor.market_feedback import MarketFeedbackService
from collect_patent_backfill_forever import DEFAULT_OUTPUT as DEFAULT_COLLECTOR_OUTPUT, build_parser, run_collector


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "patent_monitor_config.json"
DEFAULT_COMPANIES = ROOT / "patent_company_master.csv"
DEFAULT_OUTPUT = ROOT / "outputs" / "patent_monitor"
DEFAULT_DB = DEFAULT_OUTPUT / "patent_monitor.sqlite3"
STATE_PATH = ROOT / ".patent_monitor_gui_state.json"


def should_log_collector_progress(message: str) -> bool:
    if message.startswith("放置収集") or message.startswith("EPOレート調整"):
        return True
    if "stopped" in message.lower() or "paused" in message.lower() or "停止" in message:
        return True
    match = re.search(r"EPO企業別検索\s+(\d+)/(\d+)社:.*失敗=(\d+)", message)
    if match:
        index = int(match.group(1))
        failures = int(match.group(3))
        return failures > 0 or index == 1 or index % 25 == 0
    if message.startswith("EPO詳細補完"):
        match = re.search(r"EPO詳細補完\s+(\d+)/(\d+)件", message)
        if match:
            index = int(match.group(1))
            total = int(match.group(2))
            return index == 1 or index == total or index % 25 == 0
    return False


class PatentMonitorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"特許材料性モニター v{APP_VERSION}")
        self.geometry("1320x850")
        self.minsize(1050, 700)
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.collector_stop_event = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.last_output = ""
        self._variables()
        self._build()
        self._load_state()
        self._load_adaptive_threshold()
        self._load_saved_credentials(silent=True)
        self.after(100, self._poll)

    def _variables(self) -> None:
        today = date.today()
        self.source_var = tk.StringVar(value="CSV")
        self.csv_var = tk.StringVar()
        self.company_var = tk.StringVar(value=str(DEFAULT_COMPANIES))
        self.config_var = tk.StringVar(value=str(DEFAULT_CONFIG))
        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        self.db_var = tk.StringVar(value=str(DEFAULT_DB))
        self.openai_key_var = tk.StringVar(value=os.getenv("OPENAI_API_KEY", ""))
        self.gemini_key_var = tk.StringVar(value=os.getenv("GEMINI_API_KEY", ""))
        self.ops_key_var = tk.StringVar(value=os.getenv("EPO_OPS_KEY", ""))
        self.ops_secret_var = tk.StringVar(value=os.getenv("EPO_OPS_SECRET", ""))
        self.start_date_var = tk.StringVar(value=(today - timedelta(days=1)).isoformat())
        self.end_date_var = tk.StringVar(value=today.isoformat())
        self.max_records_var = tk.IntVar(value=100)
        self.ops_query_var = tk.StringVar()
        self.threshold_var = tk.DoubleVar(value=68.0)
        self.percentile_var = tk.DoubleVar(value=95.0)
        self.audit_rate_var = tk.DoubleVar(value=5.0)
        self.monthly_gpt_limit_var = tk.IntVar(value=500)
        self.gpt_model_var = tk.StringVar(value="gpt-5-nano")
        self.monthly_limit_var = tk.IntVar(value=500)
        self.model_var = tk.StringVar(value="gemini-3.1-flash-lite")
        self.escalation_threshold_var = tk.DoubleVar(value=85.0)
        self.adaptive_escalation_var = tk.BooleanVar(value=True)
        self.target_escalation_rate_var = tk.DoubleVar(value=10.0)
        self.remember_credentials_var = tk.BooleanVar(value=True)
        self.auto_feedback_var = tk.BooleanVar(value=True)
        self.collector_output_var = tk.StringVar(value=str(DEFAULT_COLLECTOR_OUTPUT))
        self.collector_window_days_var = tk.IntVar(value=7)
        self.collector_max_per_company_var = tk.IntVar(value=50)
        self.collector_detail_max_records_var = tk.IntVar(value=200)
        self.collector_name_limit_var = tk.IntVar(value=12)
        self.smtp_host_var = tk.StringVar(value="smtp.gmail.com")
        self.smtp_port_var = tk.IntVar(value=587)
        self.smtp_user_var = tk.StringVar()
        self.smtp_password_var = tk.StringVar()
        self.email_recipient_var = tk.StringVar()
        self.schedule_time_var = tk.StringVar(value="20:00")
        self.status_var = tk.StringVar(value="待機中")
        self.progress_var = tk.DoubleVar(value=0.0)

    def _build(self) -> None:
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=("Yu Gothic UI", 20, "bold"))
        style.configure("Status.TLabel", font=("Yu Gothic UI", 13, "bold"), foreground="#145c8e")
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="特許材料性モニター", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"v{APP_VERSION}", foreground="#667085").grid(row=0, column=1, sticky="e")
        ttk.Label(
            header,
            text="対象企業の新着特許をGPT-5 nanoで一次審査し、重要候補だけをGeminiで再審査します。",
            foreground="#475467",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        notebook = ttk.Notebook(outer)
        notebook.grid(row=1, column=0, sticky="ew")
        usual = ttk.Frame(notebook, padding=14)
        detail = ttk.Frame(notebook, padding=14)
        automation = ttk.Frame(notebook, padding=14)
        notebook.add(usual, text="いつも使う")
        notebook.add(detail, text="詳細設定")
        notebook.add(automation, text="自動実行・通知")
        self._build_usual(usual)
        self._build_detail(detail)
        self._build_automation(automation)

        lower = ttk.Panedwindow(outer, orient="vertical")
        lower.grid(row=2, column=0, sticky="nsew", pady=(14, 0))
        progress_frame = ttk.LabelFrame(lower, text="進捗", padding=12)
        log_frame = ttk.LabelFrame(lower, text="実行ログ・結果", padding=8)
        lower.add(progress_frame, weight=1)
        lower.add(log_frame, weight=4)
        progress_frame.columnconfigure(0, weight=1)
        ttk.Label(progress_frame, textvariable=self.status_var, style="Status.TLabel", wraplength=1200).grid(row=0, column=0, sticky="w")
        ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100).grid(row=1, column=0, sticky="ew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="word", font=("Consolas", 10), height=18)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

    def _row(self, frame: ttk.Frame, row: int, label: str, variable: tk.Variable, browse=None, show: str | None = None) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        entry = ttk.Entry(frame, textvariable=variable, show=show or "")
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        if browse:
            ttk.Button(frame, text="選択", command=browse).grid(row=row, column=2, padx=(8, 0), pady=4)

    def _build_usual(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="入力元").grid(row=0, column=0, sticky="w")
        source = ttk.Combobox(
            frame,
            textvariable=self.source_var,
            values=["CSV", "EPO OPS", "モック", "株価影響モック"],
            state="readonly",
            width=18,
        )
        source.grid(row=0, column=1, sticky="w", pady=4)
        self._row(frame, 1, "特許CSV", self.csv_var, lambda: self._file(self.csv_var, [("CSV", "*.csv")]))
        self._row(frame, 2, "企業マスタ", self.company_var, lambda: self._file(self.company_var, [("CSV", "*.csv")]))
        self._row(frame, 3, "出力フォルダ", self.output_var, lambda: self._directory(self.output_var))

        dates = ttk.Frame(frame)
        dates.grid(row=4, column=0, columnspan=3, sticky="ew", pady=5)
        ttk.Label(dates, text="EPO公開日").pack(side="left")
        ttk.Entry(dates, textvariable=self.start_date_var, width=13).pack(side="left", padx=(8, 3))
        ttk.Label(dates, text="から").pack(side="left")
        ttk.Entry(dates, textvariable=self.end_date_var, width=13).pack(side="left", padx=3)
        ttk.Label(dates, text="最大取得件数").pack(side="left", padx=(18, 5))
        ttk.Spinbox(dates, from_=0, to=10000, textvariable=self.max_records_var, width=10).pack(side="left")

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(9, 0))
        self.start_button = ttk.Button(buttons, text="収集・評価を開始", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="中断", command=self.stop_event.set, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="最新結果を開く", command=self._open_latest).pack(side="left")
        ttk.Button(buttons, text="出力フォルダを開く", command=lambda: self._open_path(self.output_var.get())).pack(side="left", padx=8)
        self.feedback_button = ttk.Button(buttons, text="株価で答え合わせ・学習", command=self._start_feedback)
        self.feedback_button.pack(side="left", padx=8)
        ttk.Checkbutton(
            buttons, text="EPO収集後に自動更新", variable=self.auto_feedback_var
        ).pack(side="left", padx=(4, 0))

        collector = ttk.LabelFrame(frame, text="過去データ放置収集", padding=10)
        collector.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        collector.columnconfigure(1, weight=1)
        self._row(collector, 0, "保存フォルダ", self.collector_output_var, lambda: self._directory(self.collector_output_var))
        settings = ttk.Frame(collector)
        settings.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        for label, variable, width in [
            ("日数/窓", self.collector_window_days_var, 6),
            ("会社別上限", self.collector_max_per_company_var, 8),
            ("詳細上限", self.collector_detail_max_records_var, 8),
            ("検索名数", self.collector_name_limit_var, 6),
        ]:
            ttk.Label(settings, text=label).pack(side="left", padx=(0, 4))
            ttk.Spinbox(settings, from_=1, to=10000, textvariable=variable, width=width).pack(side="left", padx=(0, 12))
        self.collector_start_button = ttk.Button(settings, text="過去データ放置収集を開始", command=self._start_collector)
        self.collector_start_button.pack(side="left", padx=(4, 0))
        self.collector_stop_button = ttk.Button(settings, text="放置収集を停止", command=self._request_collector_stop, state="disabled")
        self.collector_stop_button.pack(side="left", padx=8)
        ttk.Button(settings, text="放置収集フォルダを開く", command=lambda: self._open_path(self.collector_output_var.get())).pack(side="left")

    def _start_feedback(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not Path(self.db_var.get()).exists():
            messagebox.showerror("株価答え合わせ", "履歴DBがありません。先に特許評価を実行してください。")
            return
        self.start_button.configure(state="disabled")
        self.feedback_button.configure(state="disabled")
        self.status_var.set("株価答え合わせを開始します")
        self.progress_var.set(0)
        self.worker = threading.Thread(target=self._feedback_worker, daemon=True)
        self.worker.start()

    def _feedback_worker(self) -> None:
        try:
            companies = read_companies(self.company_var.get())
            service = MarketFeedbackService(self.db_var.get(), companies, self.output_var.get())
            result = service.run(progress=lambda message, ratio: self.events.put(("progress", (message, ratio))))
            self.events.put(("feedback_done", result))
        except Exception as exc:
            self.events.put(("feedback_error", f"{type(exc).__name__}: {exc}"))

    def _start_collector(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            for path, label in [(self.company_var.get(), "企業マスタ"), (self.config_var.get(), "設定JSON")]:
                if not Path(path).exists():
                    raise FileNotFoundError(f"{label}がありません: {path}")
            if not self.ops_key_var.get() or not self.ops_secret_var.get():
                loaded = load_credentials()
                if not loaded.get("epo_ops_key") or not loaded.get("epo_ops_secret"):
                    raise ValueError("EPO OPSのConsumer KeyとConsumer Secretを設定または保存してください")
            if self.remember_credentials_var.get():
                self._save_credentials(silent=True)
        except Exception as exc:
            messagebox.showerror("過去データ放置収集", str(exc))
            return
        self._save_state()
        self.collector_stop_event.clear()
        self.start_button.configure(state="disabled")
        self.feedback_button.configure(state="disabled")
        self.collector_start_button.configure(state="disabled")
        self.collector_stop_button.configure(state="normal")
        self.log.delete("1.0", "end")
        self.status_var.set("過去データ放置収集を開始します")
        self.progress_var.set(0)
        self._append(
            "過去データ放置収集を開始します: "
            f"窓={self.collector_window_days_var.get()}日 / "
            f"会社別上限={self.collector_max_per_company_var.get()} / "
            f"詳細上限={self.collector_detail_max_records_var.get()} / "
            f"検索名数={self.collector_name_limit_var.get()}"
        )
        self.worker = threading.Thread(target=self._collector_worker, daemon=True)
        self.worker.start()

    def _request_collector_stop(self) -> None:
        self.collector_stop_event.set()
        self._append("放置収集の停止を要求しました。実行中のEPOリクエスト終了後に中断します。")
        self.status_var.set("放置収集の停止を要求しました")

    def _collector_progress(self, message: str, ratio: float) -> None:
        self.events.put(("progress", (message, ratio)))
        if should_log_collector_progress(str(message)):
            self.events.put(("log", str(message)))

    def _collector_worker(self) -> None:
        try:
            parser = build_parser()
            args = parser.parse_args([])
            args.output_dir = self.collector_output_var.get()
            args.company_master = self.company_var.get()
            args.config = self.config_var.get()
            args.window_days = int(self.collector_window_days_var.get())
            args.max_per_company = int(self.collector_max_per_company_var.get())
            args.detail_max_records = int(self.collector_detail_max_records_var.get())
            args.company_search_name_limit = int(self.collector_name_limit_var.get())
            args.max_windows = 0
            result = run_collector(
                args,
                stop_requested=self.collector_stop_event.is_set,
                progress=self._collector_progress,
            )
            self.events.put(("collector_done", result))
        except Exception as exc:
            self.events.put(("collector_error", f"{type(exc).__name__}: {exc}"))

    def _build_detail(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        self._row(frame, 0, "設定JSON", self.config_var, lambda: self._file(self.config_var, [("JSON", "*.json")]))
        self._row(frame, 1, "履歴DB", self.db_var, lambda: self._save_file(self.db_var, [("SQLite", "*.sqlite3")]))
        self._row(frame, 2, "OpenAI APIキー", self.openai_key_var, show="*")
        self._row(frame, 3, "Gemini APIキー", self.gemini_key_var, show="*")
        self._row(frame, 4, "EPO OPS Consumer Key", self.ops_key_var, show="*")
        self._row(frame, 5, "EPO OPS Consumer Secret", self.ops_secret_var, show="*")
        self._row(frame, 6, "EPO CQL（空欄は日付のみ）", self.ops_query_var)
        credentials = ttk.Frame(frame)
        credentials.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 2))
        ttk.Checkbutton(
            credentials,
            text="Windowsユーザー用に暗号化して保存",
            variable=self.remember_credentials_var,
        ).pack(side="left")
        ttk.Button(credentials, text="資格情報を保存", command=self._save_credentials).pack(side="left", padx=(12, 5))
        ttk.Button(credentials, text="保存済みを削除", command=self._delete_credentials).pack(side="left", padx=5)
        ttk.Button(credentials, text="OpenAI接続確認", command=self._test_openai).pack(side="left", padx=(12, 5))
        ttk.Button(credentials, text="Gemini接続確認", command=self._test_gemini).pack(side="left", padx=(12, 5))
        ttk.Button(credentials, text="EPO OPS接続確認", command=self._test_epo).pack(side="left", padx=5)

        controls = ttk.Frame(frame)
        controls.grid(row=8, column=0, columnspan=3, sticky="w", pady=8)
        for label, variable, start, end, width in [
            ("一次候補閾値", self.threshold_var, 0, 100, 7),
            ("企業内percentile", self.percentile_var, 0, 100, 7),
            ("棄却監査%", self.audit_rate_var, 0, 100, 7),
            ("Gemini再審査閾値", self.escalation_threshold_var, 0, 100, 7),
            ("月間GPT上限", self.monthly_gpt_limit_var, 0, 100000, 9),
            ("月間Gemini上限", self.monthly_limit_var, 0, 100000, 9),
        ]:
            ttk.Label(controls, text=label).pack(side="left", padx=(0, 4))
            ttk.Spinbox(controls, from_=start, to=end, textvariable=variable, width=width).pack(side="left", padx=(0, 15))
        models = ttk.Frame(frame)
        models.grid(row=9, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(models, text="GPTモデル").pack(side="left")
        ttk.Entry(models, textvariable=self.gpt_model_var, width=20).pack(side="left", padx=(5, 15))
        ttk.Label(models, text="Geminiモデル").pack(side="left")
        ttk.Entry(models, textvariable=self.model_var, width=28).pack(side="left", padx=5)
        ttk.Checkbutton(
            models, text="Gemini昇格率を自動調整", variable=self.adaptive_escalation_var
        ).pack(side="left", padx=(18, 5))
        ttk.Label(models, text="目標%").pack(side="left")
        ttk.Spinbox(
            models, from_=1, to=50, increment=1, textvariable=self.target_escalation_rate_var, width=6
        ).pack(side="left", padx=5)
        ttk.Label(
            frame,
            text="APIキーはWindowsユーザー用暗号化保存、または環境変数 OPENAI_API_KEY / GEMINI_API_KEY / EPO_OPS_KEY / EPO_OPS_SECRETを利用できます。",
            foreground="#667085",
        ).grid(row=10, column=0, columnspan=3, sticky="w")

    def _build_automation(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        self._row(frame, 0, "SMTPサーバー", self.smtp_host_var)
        self._row(frame, 1, "SMTPポート", self.smtp_port_var)
        self._row(frame, 2, "送信元メール", self.smtp_user_var)
        self._row(frame, 3, "メール用アプリパスワード", self.smtp_password_var, show="*")
        self._row(frame, 4, "通知先メール", self.email_recipient_var)
        self._row(frame, 5, "毎日の実行時刻 (HH:MM)", self.schedule_time_var)
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 4))
        ttk.Button(buttons, text="設定と資格情報を保存", command=self._save_automation_settings).pack(side="left")
        ttk.Button(buttons, text="テストメール送信", command=self._test_email).pack(side="left", padx=8)
        ttk.Button(buttons, text="毎日自動実行を登録", command=self._register_schedule).pack(side="left", padx=8)
        ttk.Button(buttons, text="自動実行を解除", command=self._delete_schedule).pack(side="left", padx=8)
        ttk.Label(
            frame,
            text="Gmailでは通常のログインパスワードではなく、Googleアカウントで発行したアプリパスワードを使用します。\n"
                 "Windowsタスクは現在のユーザーで実行され、暗号化済みAPIキーを安全に読み込みます。",
            foreground="#667085",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _save_automation_settings(self) -> None:
        self._save_state()
        self._save_credentials()

    def _test_email(self) -> None:
        from patent_monitor.notifications import test_smtp
        host = self.smtp_host_var.get().strip()
        user = self.smtp_user_var.get().strip()
        password = self.smtp_password_var.get().strip()
        recipient = self.email_recipient_var.get().strip() or user
        if not host or not user or not password or not recipient:
            messagebox.showerror("メール設定", "SMTPサーバー、送信元、アプリパスワード、通知先を入力してください")
            return
        try:
            test_smtp(host, int(self.smtp_port_var.get()), user, password, recipient)
            messagebox.showinfo("メール設定", "テストメールを送信しました")
        except Exception as exc:
            messagebox.showerror("メール設定", f"{type(exc).__name__}: {exc}")

    def _register_schedule(self) -> None:
        import re
        value = self.schedule_time_var.get().strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            messagebox.showerror("自動実行", "実行時刻をHH:MM形式で入力してください")
            return
        self._save_state()
        if self.remember_credentials_var.get():
            self._save_credentials(silent=True)
        bat = ROOT / "run_patent_monitor_daily.bat"
        result = subprocess.run(
            ["schtasks", "/Create", "/TN", "PatentMaterialityMonitorDaily", "/TR", f'"{bat}"',
             "/SC", "DAILY", "/ST", value, "/F"],
            capture_output=True, text=True, encoding="cp932", errors="replace",
        )
        if result.returncode == 0:
            messagebox.showinfo("自動実行", f"毎日 {value} の自動実行を登録しました")
        else:
            messagebox.showerror("自動実行", result.stderr or result.stdout)

    def _delete_schedule(self) -> None:
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", "PatentMaterialityMonitorDaily", "/F"],
            capture_output=True, text=True, encoding="cp932", errors="replace",
        )
        if result.returncode == 0:
            messagebox.showinfo("自動実行", "自動実行を解除しました")
        else:
            messagebox.showerror("自動実行", result.stderr or result.stdout)

    def _file(self, variable: tk.StringVar, types) -> None:
        path = filedialog.askopenfilename(filetypes=types)
        if path:
            variable.set(path)

    def _save_file(self, variable: tk.StringVar, types) -> None:
        path = filedialog.asksaveasfilename(filetypes=types, defaultextension=types[0][1].replace("*", ""))
        if path:
            variable.set(path)

    def _directory(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            variable.set(path)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            for path, label in [(self.company_var.get(), "企業マスタ"), (self.config_var.get(), "設定JSON")]:
                if not Path(path).exists():
                    raise FileNotFoundError(f"{label}がありません: {path}")
            if self.source_var.get() == "CSV" and not Path(self.csv_var.get()).exists():
                raise FileNotFoundError(f"特許CSVがありません: {self.csv_var.get()}")
            if self.source_var.get() == "EPO OPS" and (not self.ops_key_var.get() or not self.ops_secret_var.get()):
                raise ValueError("EPO OPSのConsumer KeyとSecretを設定してください")
        except Exception as exc:
            messagebox.showerror("入力確認", str(exc))
            return
        self._save_state()
        if self.remember_credentials_var.get():
            try:
                self._save_credentials(silent=True)
            except Exception as exc:
                messagebox.showerror("資格情報保存", str(exc))
                return
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.log.delete("1.0", "end")
        self.progress_var.set(0)
        self.worker = threading.Thread(target=self._run_worker, daemon=True)
        self.worker.start()

    def _test_gemini(self) -> None:
        api_key = self.gemini_key_var.get().strip()
        if not api_key:
            messagebox.showerror("Gemini接続確認", "Gemini APIキーを入力してください")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Gemini接続確認", "現在の処理が終わってから確認してください")
            return
        self.status_var.set("Geminiの利用可能モデルを確認しています")
        self.worker = threading.Thread(target=self._test_gemini_worker, args=(api_key,), daemon=True)
        self.worker.start()

    def _test_openai(self) -> None:
        api_key = self.openai_key_var.get().strip()
        if not api_key:
            messagebox.showerror("OpenAI接続確認", "OpenAI APIキーを入力してください")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("OpenAI接続確認", "現在の処理が終わってから確認してください")
            return
        self.status_var.set("OpenAIモデルへの接続を確認しています")
        self.worker = threading.Thread(target=self._test_openai_worker, args=(api_key,), daemon=True)
        self.worker.start()

    def _test_openai_worker(self, api_key: str) -> None:
        try:
            reviewer = OpenAIReviewer(api_key, self.gpt_model_var.get().strip() or "gpt-5-nano")
            self.events.put(("openai_test", reviewer.probe()))
        except Exception as exc:
            self.events.put(("openai_test_error", f"{type(exc).__name__}: {exc}"))

    def _test_gemini_worker(self, api_key: str) -> None:
        try:
            reviewer = GeminiReviewer(api_key, self.model_var.get().strip() or "auto")
            self.events.put(("gemini_test", reviewer.probe()))
        except Exception as exc:
            self.events.put(("gemini_test_error", f"{type(exc).__name__}: {exc}"))

    def _test_epo(self) -> None:
        key = self.ops_key_var.get().strip()
        secret = self.ops_secret_var.get().strip()
        if not key or not secret:
            messagebox.showerror("EPO OPS接続確認", "Consumer KeyとConsumer Secretを入力してください")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("EPO OPS接続確認", "現在の処理が終わってから確認してください")
            return
        start = self.start_date_var.get().replace("-", "")
        end = self.end_date_var.get().replace("-", "")
        query = self.ops_query_var.get().strip() or f'pd within "{start} {end}"'
        self.status_var.set("EPO OPSの認証と1件検索を確認しています")
        self.worker = threading.Thread(target=self._test_epo_worker, args=(key, secret, query), daemon=True)
        self.worker.start()

    def _test_epo_worker(self, key: str, secret: str, query: str) -> None:
        try:
            config = PipelineConfig.from_json(self.config_var.get())
            provider = EPOOPSProvider(key, secret, config.ops_requests_per_minute, config.request_timeout_seconds)
            self.events.put(("epo_test", provider.probe(query)))
        except Exception as exc:
            self.events.put(("epo_test_error", f"{type(exc).__name__}: {exc}"))

    def _run_worker(self) -> None:
        try:
            config = PipelineConfig.from_json(self.config_var.get())
            config.gemini_threshold = float(self.threshold_var.get())
            config.company_percentile_threshold = float(self.percentile_var.get())
            config.random_reject_audit_rate = float(self.audit_rate_var.get()) / 100.0
            config.gemini_escalation_threshold = float(self.escalation_threshold_var.get())
            config.adaptive_gemini_escalation = bool(self.adaptive_escalation_var.get())
            config.gemini_target_escalation_rate = float(self.target_escalation_rate_var.get()) / 100.0
            config.monthly_gpt_limit = int(self.monthly_gpt_limit_var.get())
            config.gpt_model = self.gpt_model_var.get().strip()
            config.monthly_gemini_limit = int(self.monthly_limit_var.get())
            config.gemini_model = self.model_var.get().strip()
            companies = read_companies(self.company_var.get())
            source = self.source_var.get()
            detail_summary = None
            if source == "CSV":
                records = read_patent_csv(self.csv_var.get())
            elif source == "モック":
                records = mock_patents()
            elif source == "株価影響モック":
                records = [
                    record
                    for record in mock_patents()
                    if record.publication_number == "JP7301490B2"
                ]
                if not records:
                    raise RuntimeError("株価影響モック JP7301490B2 が見つかりません")
                # Keep the displayed thresholds intact and record forced routing explicitly.
                config.force_two_stage_review = True
                self.events.put((
                    "log",
                    "株価影響モック: 日東精工 特許第7301490号をGPT一次審査後、Geminiへ強制送信します。"
                    "閾値は変更せず、検証フラグで二段審査を行います。既知の株価反応はプロンプトに含めません。",
                ))
            else:
                start = self.start_date_var.get().replace("-", "")
                end = self.end_date_var.get().replace("-", "")
                query = self.ops_query_var.get().strip() or f'pd within "{start} {end}"'
                provider = EPOOPSProvider(
                    self.ops_key_var.get(), self.ops_secret_var.get(), config.ops_requests_per_minute, config.request_timeout_seconds
                )
                records = provider.search(query, int(self.max_records_var.get()), lambda msg: self.events.put(("log", msg)))
                if config.ops_enrich_details:
                    self.events.put(("log", "監視企業候補のCPC/IPC・英語要約をEPOから補完します。"))
                    detail_summary = provider.enrich_records(
                        records,
                        companies,
                        max_records=config.ops_detail_max_records,
                        match_threshold=config.fuzzy_match_threshold,
                        match_margin=config.fuzzy_match_margin,
                        progress=lambda msg: self.events.put(("log", msg)),
                    )
                    self.events.put(("log", "EPO詳細補完結果: " + json.dumps(detail_summary, ensure_ascii=False)))
            self.events.put(("log", f"入力={len(records)}件 / 対象企業={len(companies)}社"))
            gemini_api_key = self.gemini_key_var.get().strip()
            if source == "モック":
                gemini_api_key = ""
                self.events.put(("log", "モック検証では外部APIを呼びません。一次審査候補はgpt_pendingとして保存します。"))
            openai_api_key = self.openai_key_var.get().strip()
            if source == "モック":
                openai_api_key = ""
            elif source == "株価影響モック" and (not openai_api_key or not gemini_api_key):
                raise ValueError("株価影響モックの二段審査にはOpenAIとGeminiのAPIキーが必要です")
            pipeline = PatentPipeline(
                config, companies, self.db_var.get(), self.output_var.get(),
                openai_api_key=openai_api_key,
                gemini_api_key=gemini_api_key,
                progress=lambda message, ratio: self.events.put(("progress", (message, ratio))),
                stop_requested=self.stop_event.is_set,
            )
            result = pipeline.run(records, source.lower().replace(" ", "_"))
            if detail_summary is not None:
                result["epo_detail_enrichment"] = detail_summary
            if source == "EPO OPS" and self.auto_feedback_var.get():
                try:
                    self.events.put(("log", "EPO収集完了後の株価答え合わせ・再学習を開始します。"))
                    feedback = MarketFeedbackService(self.db_var.get(), companies, self.output_var.get()).run(
                        progress=lambda message, ratio: self.events.put(("progress", (message, ratio)))
                    )
                    result["market_feedback"] = feedback.__dict__
                except Exception as exc:
                    result["market_feedback_warning"] = f"{type(exc).__name__}: {exc}"
                    self.events.put(("log", "株価答え合わせ警告: " + result["market_feedback_warning"]))
            self.events.put(("done", result))
        except Exception as exc:
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    message, ratio = payload
                    self.status_var.set(str(message))
                    self.progress_var.set(float(ratio) * 100)
                elif kind == "log":
                    self._append(str(payload))
                elif kind == "done":
                    result = dict(payload)
                    if "escalation_threshold_final" in result:
                        self.escalation_threshold_var.set(result["escalation_threshold_final"])
                        self._save_state()
                    self.last_output = result.get("evaluation_csv", "")
                    self._append(json.dumps(result, ensure_ascii=False, indent=2))
                    self.status_var.set("中断済み" if self.stop_event.is_set() else "完了")
                    self.progress_var.set(100)
                    self._idle()
                    messagebox.showinfo(
                        "特許評価",
                        f"処理が完了しました。\n入力: {result['input_count']}件\nファミリー統合後: {result['family_count']}件\n"
                        f"GPT一次審査: {result['gpt_count']}件\nGemini再審査: {result['gemini_count']}件\n"
                        f"Gemini実昇格率: {result.get('actual_escalation_rate', 0) * 100:.1f}%\n"
                        f"次回閾値: {result.get('escalation_threshold_final', self.escalation_threshold_var.get())}\n"
                        f"名寄せ確認: {result['review_count']}件\nエラー: {result['error_count']}件",
                    )
                elif kind == "error":
                    self._append(str(payload))
                    self.status_var.set("エラー")
                    self._idle()
                    messagebox.showerror("特許評価", str(payload))
                elif kind == "feedback_done":
                    result = payload
                    self._append(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
                    self.status_var.set("株価答え合わせ・学習完了")
                    self.progress_var.set(100)
                    self._idle()
                    messagebox.showinfo(
                        "株価答え合わせ",
                        f"完了しました。\n対象特許: {result.event_count}件\n"
                        f"保存した期間別結果: {result.outcome_count}件\n"
                        f"60日モデル学習件数: {result.model_sample_count}件\n"
                        f"学習モデル作成: {'はい' if result.model_created else 'まだ30件未満または片寄りあり'}",
                    )
                elif kind == "feedback_error":
                    self._append(str(payload))
                    self.status_var.set("株価答え合わせエラー")
                    self._idle()
                    messagebox.showerror("株価答え合わせ", str(payload))
                elif kind == "collector_done":
                    result = dict(payload)
                    self._append(json.dumps(result, ensure_ascii=False, indent=2))
                    self.status_var.set("過去データ放置収集を停止しました" if self.collector_stop_event.is_set() else "過去データ放置収集完了")
                    self.progress_var.set(100)
                    self._idle()
                    messagebox.showinfo(
                        "過去データ放置収集",
                        f"停止地点を保存しました。\n完了ウィンドウ: {result['completed_windows']}件\n"
                        f"次回再開日: {result['next_end_date']}\n保存先: {result['collector_dir']}",
                    )
                elif kind == "collector_error":
                    self._append(str(payload))
                    self.status_var.set("過去データ放置収集エラー")
                    self._idle()
                    messagebox.showerror("過去データ放置収集", str(payload))
                elif kind == "gemini_test":
                    result = dict(payload)
                    self.model_var.set(result["resolved_model"])
                    self.status_var.set(f"Gemini接続OK: {result['resolved_model']}")
                    messagebox.showinfo(
                        "Gemini接続確認",
                        "接続できました。\n"
                        f"使用モデル: {result['resolved_model']}\n"
                        f"利用可能な生成モデル: {result['generate_content_model_count']}件",
                    )
                elif kind == "gemini_test_error":
                    self.status_var.set("Gemini接続エラー")
                    messagebox.showerror("Gemini接続確認", str(payload))
                elif kind == "openai_test":
                    result = dict(payload)
                    self.gpt_model_var.set(result["resolved_model"])
                    self.status_var.set(f"OpenAI接続OK: {result['resolved_model']}")
                    messagebox.showinfo(
                        "OpenAI接続確認",
                        f"接続できました。\n使用モデル: {result['resolved_model']}",
                    )
                elif kind == "openai_test_error":
                    self.status_var.set("OpenAI接続エラー")
                    messagebox.showerror("OpenAI接続確認", str(payload))
                elif kind == "epo_test":
                    result = dict(payload)
                    self.status_var.set("EPO OPS接続OK")
                    messagebox.showinfo(
                        "EPO OPS接続確認",
                        "認証と検索の両方に成功しました。\n"
                        f"確認クエリ: {result['query']}\n"
                        f"サンプル取得: {result['sample_count']}件",
                    )
                elif kind == "epo_test_error":
                    self.status_var.set("EPO OPS接続エラー")
                    self._append(str(payload))
                    messagebox.showerror("EPO OPS接続確認", str(payload))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _idle(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.feedback_button.configure(state="normal")
        self.collector_start_button.configure(state="normal")
        self.collector_stop_button.configure(state="disabled")

    def _append(self, text: str) -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def _open_latest(self) -> None:
        if self.last_output and Path(self.last_output).exists():
            self._open_path(self.last_output)
            return
        candidates = sorted(Path(self.output_var.get()).glob("*/patent_evaluations.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
        if candidates:
            self._open_path(str(candidates[0]))
        else:
            messagebox.showinfo("最新結果", "まだ結果CSVがありません")

    @staticmethod
    def _open_path(path: str) -> None:
        if Path(path).exists():
            os.startfile(str(Path(path).resolve()))

    def _save_state(self) -> None:
        data = {
            "source": self.source_var.get(), "csv": self.csv_var.get(), "company": self.company_var.get(),
            "config": self.config_var.get(), "output": self.output_var.get(), "db": self.db_var.get(),
            "start_date": self.start_date_var.get(), "end_date": self.end_date_var.get(),
            "max_records": self.max_records_var.get(), "ops_query": self.ops_query_var.get(),
            "threshold": self.threshold_var.get(), "percentile": self.percentile_var.get(),
            "audit_rate": self.audit_rate_var.get(),
            "escalation_threshold": self.escalation_threshold_var.get(),
            "adaptive_escalation": self.adaptive_escalation_var.get(),
            "target_escalation_rate": self.target_escalation_rate_var.get(),
            "monthly_gpt_limit": self.monthly_gpt_limit_var.get(), "gpt_model": self.gpt_model_var.get(),
            "monthly_limit": self.monthly_limit_var.get(), "model": self.model_var.get(),
            "remember_credentials": self.remember_credentials_var.get(),
            "auto_feedback": self.auto_feedback_var.get(),
            "collector_output": self.collector_output_var.get(),
            "collector_window_days": self.collector_window_days_var.get(),
            "collector_max_per_company": self.collector_max_per_company_var.get(),
            "collector_detail_max_records": self.collector_detail_max_records_var.get(),
            "collector_name_limit": self.collector_name_limit_var.get(),
            "smtp_host": self.smtp_host_var.get(), "smtp_port": self.smtp_port_var.get(),
            "smtp_user": self.smtp_user_var.get(), "email_recipient": self.email_recipient_var.get(),
            "schedule_time": self.schedule_time_var.get(),
        }
        STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_state(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            mapping = {
                "source": self.source_var, "csv": self.csv_var, "company": self.company_var, "config": self.config_var,
                "output": self.output_var, "db": self.db_var, "start_date": self.start_date_var, "end_date": self.end_date_var,
                "max_records": self.max_records_var, "ops_query": self.ops_query_var, "threshold": self.threshold_var,
                "percentile": self.percentile_var, "audit_rate": self.audit_rate_var, "monthly_limit": self.monthly_limit_var,
                "escalation_threshold": self.escalation_threshold_var,
                "adaptive_escalation": self.adaptive_escalation_var,
                "target_escalation_rate": self.target_escalation_rate_var,
                "monthly_gpt_limit": self.monthly_gpt_limit_var, "gpt_model": self.gpt_model_var,
                "model": self.model_var,
                "remember_credentials": self.remember_credentials_var,
                "auto_feedback": self.auto_feedback_var,
                "collector_output": self.collector_output_var,
                "collector_window_days": self.collector_window_days_var,
                "collector_max_per_company": self.collector_max_per_company_var,
                "collector_detail_max_records": self.collector_detail_max_records_var,
                "collector_name_limit": self.collector_name_limit_var,
                "smtp_host": self.smtp_host_var, "smtp_port": self.smtp_port_var,
                "smtp_user": self.smtp_user_var, "email_recipient": self.email_recipient_var,
                "schedule_time": self.schedule_time_var,
            }
            for key, variable in mapping.items():
                if key in data:
                    variable.set(data[key])
            saved_model = self.model_var.get().strip().lower()
            if saved_model.startswith("gemini-2.0-") or saved_model == "gemini-2.5-flash-lite":
                self.model_var.set("gemini-3.1-flash-lite")
        except Exception:
            pass

    def _load_adaptive_threshold(self) -> None:
        path = Path(self.db_var.get())
        if not path.exists():
            return
        try:
            connection = sqlite3.connect(path)
            row = connection.execute(
                "SELECT setting_value FROM adaptive_settings WHERE setting_key='gemini_escalation_threshold'"
            ).fetchone()
            connection.close()
            if row:
                self.escalation_threshold_var.set(float(row[0]))
        except (sqlite3.Error, ValueError):
            pass

    def _save_credentials(self, silent: bool = False) -> None:
        values = {
            "openai_api_key": self.openai_key_var.get().strip(),
            "gemini_api_key": self.gemini_key_var.get().strip(),
            "epo_ops_key": self.ops_key_var.get().strip(),
            "epo_ops_secret": self.ops_secret_var.get().strip(),
            "smtp_password": self.smtp_password_var.get().strip(),
        }
        if not any(values.values()):
            if not silent:
                messagebox.showinfo("資格情報保存", "保存するAPIキーが入力されていません")
            return
        path = save_credentials(values)
        if not silent:
            messagebox.showinfo(
                "資格情報保存",
                "現在のWindowsユーザーだけが復号できる形式で保存しました。\n"
                f"保存先: {path}",
            )

    def _load_saved_credentials(self, silent: bool = False) -> None:
        try:
            values = load_credentials()
            if values.get("openai_api_key"):
                self.openai_key_var.set(values["openai_api_key"])
            if values.get("gemini_api_key"):
                self.gemini_key_var.set(values["gemini_api_key"])
            if values.get("epo_ops_key"):
                self.ops_key_var.set(values["epo_ops_key"])
            if values.get("epo_ops_secret"):
                self.ops_secret_var.set(values["epo_ops_secret"])
            if values.get("smtp_password"):
                self.smtp_password_var.set(values["smtp_password"])
            if values and not silent:
                messagebox.showinfo("資格情報", "保存済みの資格情報を読み込みました")
        except Exception as exc:
            if not silent:
                messagebox.showerror("資格情報読込", str(exc))

    def _delete_credentials(self) -> None:
        if delete_credentials():
            self.openai_key_var.set("")
            self.gemini_key_var.set("")
            self.ops_key_var.set("")
            self.ops_secret_var.set("")
            self.smtp_password_var.set("")
            messagebox.showinfo("資格情報", "保存済み資格情報を削除しました")
        else:
            messagebox.showinfo("資格情報", "保存済み資格情報はありません")


if __name__ == "__main__":
    PatentMonitorApp().mainloop()
