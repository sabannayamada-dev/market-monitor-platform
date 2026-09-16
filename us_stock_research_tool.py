from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from us_stock_research_pipeline import (
    APP_VERSION,
    DEFAULT_LEGACY_APP,
    DEFAULT_OUTPUT_DIR,
    PipelineConfig,
    run_pipeline,
)


def eta_text(seconds: object) -> str:
    try:
        value = max(int(float(seconds)), 0)
    except (TypeError, ValueError):
        return "計算中"
    minutes, sec = divmod(value, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"約{hours}時間{minutes}分"
    if minutes:
        return f"約{minutes}分{sec}秒"
    return f"約{sec}秒"


class UsStockResearchApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"米国株 大量底検知・SEC財務研究ツール v{APP_VERSION}")
        self.geometry("1180x820")
        self.minsize(930, 680)
        self.output_dir = tk.StringVar(value=str((Path(__file__).resolve().parent / DEFAULT_OUTPUT_DIR).resolve()))
        self.legacy_app = tk.StringVar(value=str(DEFAULT_LEGACY_APP))
        self.user_agent = tk.StringVar(value="")
        self.target_companies = tk.IntVar(value=1500)
        self.price_start = tk.StringVar(value="2010-01-01")
        self.signal_start = tk.StringVar(value="2012-01-01")
        self.minimum_price = tk.DoubleVar(value=1.0)
        self.minimum_dollar_volume = tk.DoubleVar(value=1_000_000)
        self.refresh_universe = tk.BooleanVar(value=False)
        self.progress_text = tk.StringVar(value="待機中")
        self.progress_value = tk.DoubleVar(value=0.0)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.started_at = 0.0
        self._build_ui()
        self.after(100, self._drain_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="米国株 大量底検知・SEC財務研究", font=("Yu Gothic UI", 20, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"v{APP_VERSION}", foreground="#667085").grid(row=0, column=1, sticky="e")
        ttk.Label(
            outer,
            text="現役米国株を流動性で選び、従来と同じ底検知を実行し、候補日時点のSEC財務水準と財務変化を結合します。",
            foreground="#475467",
        ).grid(row=1, column=0, sticky="w", pady=(4, 14))

        settings = ttk.LabelFrame(outer, text="実行設定", padding=14)
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)
        self._path_row(settings, 0, "出力フォルダ", self.output_dir, self._choose_output)
        self._path_row(settings, 1, "従来の底検知 app.py", self.legacy_app, self._choose_legacy)
        ttk.Label(settings, text="SEC User-Agent").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(settings, textvariable=self.user_agent).grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        ttk.Label(settings, text="連絡可能なメールを含めます").grid(row=2, column=2, sticky="w")

        controls = ttk.Frame(settings)
        controls.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(controls, text="対象企業数").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(controls, from_=100, to=10000, increment=100, textvariable=self.target_companies, width=8).grid(row=0, column=1, padx=(8, 22))
        ttk.Label(controls, text="価格取得開始").grid(row=0, column=2)
        ttk.Entry(controls, textvariable=self.price_start, width=12).grid(row=0, column=3, padx=(8, 22))
        ttk.Label(controls, text="底検知開始").grid(row=0, column=4)
        ttk.Entry(controls, textvariable=self.signal_start, width=12).grid(row=0, column=5, padx=(8, 22))
        ttk.Label(controls, text="最低株価($)").grid(row=0, column=6)
        ttk.Spinbox(controls, from_=0, to=100, increment=0.5, textvariable=self.minimum_price, width=8).grid(row=0, column=7, padx=8)

        controls2 = ttk.Frame(settings)
        controls2.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(controls2, text="最低日次売買代金($)").grid(row=0, column=0)
        ttk.Entry(controls2, textvariable=self.minimum_dollar_volume, width=14).grid(row=0, column=1, padx=(8, 20))
        ttk.Checkbutton(controls2, text="SEC企業一覧を更新", variable=self.refresh_universe).grid(row=0, column=2)
        ttk.Label(controls2, text="価格・SECデータは中断後もキャッシュされ、次回再利用されます。", foreground="#667085").grid(row=0, column=3, padx=(24, 0))

        actions = ttk.Frame(settings)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.start_button = ttk.Button(actions, text="大量収集・分析を開始", command=lambda: self._start(False))
        self.start_button.pack(side="left")
        self.resume_button = ttk.Button(actions, text="続きから始める", command=lambda: self._start(True))
        self.resume_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="中断", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="完成CSVを開く", command=self._open_csv).pack(side="left", padx=(16, 0))
        ttk.Button(actions, text="相関CSVを開く", command=self._open_correlations).pack(side="left", padx=8)
        ttk.Button(actions, text="出力フォルダを開く", command=self._open_output).pack(side="left", padx=8)

        progress = ttk.LabelFrame(outer, text="進捗", padding=14)
        progress.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        progress.columnconfigure(0, weight=1)
        progress.rowconfigure(2, weight=1)
        ttk.Label(progress, textvariable=self.progress_text, font=("Yu Gothic UI", 12, "bold"), foreground="#175CD3", wraplength=1050).grid(row=0, column=0, sticky="ew")
        ttk.Progressbar(progress, variable=self.progress_value, maximum=100).grid(row=1, column=0, sticky="ew", pady=(10, 12))
        log_frame = ttk.Frame(progress)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="word", font=("Consolas", 10), state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _path_row(self, parent: ttk.Widget, row: int, label: str, variable: tk.StringVar, command: object) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(parent, text="選択", command=command).grid(row=row, column=2)

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.output_dir.set(selected)

    def _choose_legacy(self) -> None:
        selected = filedialog.askopenfilename(filetypes=[("Python", "*.py"), ("All", "*.*")])
        if selected:
            self.legacy_app.set(selected)

    def _append(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _validate(self) -> PipelineConfig | None:
        legacy = Path(self.legacy_app.get().strip())
        if not legacy.exists():
            messagebox.showwarning("入力確認", "従来の底検知app.pyを選択してください。")
            return None
        if "@" not in self.user_agent.get():
            messagebox.showwarning("入力確認", "SEC User-Agentにメールアドレスを含めてください。")
            return None
        try:
            target = int(self.target_companies.get())
            if not 100 <= target <= 10000:
                raise ValueError
            for value in (self.price_start.get(), self.signal_start.get()):
                __import__("datetime").datetime.strptime(value, "%Y-%m-%d")
        except Exception:
            messagebox.showwarning("入力確認", "企業数または日付設定を確認してください。")
            return None
        if self.price_start.get() >= self.signal_start.get():
            messagebox.showwarning("入力確認", "価格取得開始日は底検知開始日より前にしてください。")
            return None
        return PipelineConfig(
            output_dir=Path(self.output_dir.get().strip()), legacy_app_path=legacy,
            sec_user_agent=self.user_agent.get().strip(), target_companies=target,
            minimum_price=float(self.minimum_price.get()), minimum_dollar_volume=float(self.minimum_dollar_volume.get()),
            price_start=self.price_start.get(), signal_start=self.signal_start.get(), refresh_universe=self.refresh_universe.get(),
        )

    def _start(self, resume: bool = False) -> None:
        if self.running:
            return
        config = self._validate()
        if not config:
            return
        config.resume = resume
        self.running = True
        self.stop_event.clear()
        self.progress_value.set(0)
        self.progress_text.set("米国上場企業一覧を準備しています...")
        self.start_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._append(
            "チェックポイントを確認して続きから再開します。"
            if resume else "大量収集を開始しました。初回は価格取得に時間がかかります。"
        )
        threading.Thread(target=self._worker, args=(config,), daemon=True).start()

    def _worker(self, config: PipelineConfig) -> None:
        try:
            report = run_pipeline(config, lambda info: self.events.put(("progress", info)), self.stop_event)
            self.events.put(("complete", report))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _stop(self) -> None:
        if self.running:
            self.stop_event.set()
            self.progress_text.set("中断要求を受け付けました。現在のバッチ完了後に保存して停止します...")
            self._append("中断要求を送信しました。")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self._progress(dict(payload))  # type: ignore[arg-type]
                elif kind == "complete":
                    self._complete(dict(payload))  # type: ignore[arg-type]
                else:
                    self._error(str(payload))
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _progress(self, info: dict[str, object]) -> None:
        phase = str(info.get("phase", ""))
        if phase == "universe":
            text = str(info.get("message", "企業一覧取得中")); value = 2
        elif phase == "resume":
            text = str(info.get("message", "再開地点を確認しています")); value = max(self.progress_value.get(), 2)
        elif phase == "liquidity":
            current, total = int(info.get("batch", 0)), max(int(info.get("total_batches", 1)), 1)
            failure_sample = info.get("failure_sample")
            text = (
                f"流動性確認: {current}/{total}バッチ "
                f"候補確認={info.get('checked', 0)}/{info.get('candidates', 0)} "
                f"価格取得成功={info.get('price_available', 0)} 失敗={info.get('failures', 0)}"
            ); value = 5 + current / total * 15
            if failure_sample and not info.get("price_available"):
                ticker, reason = failure_sample
                self._append(f"価格取得失敗例: {ticker}: {reason}")
        elif phase == "prices":
            current, total = int(info.get("batch", 0)), max(int(info.get("total_batches", 1)), 1)
            text = f"長期価格取得: {current}/{total}バッチ 完了={info.get('completed', 0)}/{info.get('total', 0)} キャッシュ={info.get('cached', 0)}"; value = 20 + current / total * 35
        elif phase == "detect":
            current, total = int(info.get("current", 0)), max(int(info.get("total", 1)), 1)
            text = f"底検知: {current}/{total}社 現在={info.get('ticker', '')} 検出イベント={info.get('events', 0)} エラー={info.get('errors', 0)}"; value = 55 + current / total * 20
        elif phase == "sec":
            current, total = int(info.get("current", 0)), max(int(info.get("total", 1)), 1)
            text = f"SEC財務結合: {current}/{total}件 現在={info.get('ticker', '')} 成功={info.get('success', 0)} エラー={info.get('errors', 0)}"; value = 75 + current / total * 24
        elif phase == "sec_fetch":
            current, total = int(info.get("current", 0)), max(int(info.get("total", 1)), 1)
            text = f"SEC企業データ並列取得: {current}/{total}社 成功={info.get('success', 0)} エラー={info.get('errors', 0)}"; value = 75 + current / total * 10
        elif phase == "done":
            text = "全工程が完了しました"; value = 100
        else:
            return
        self.progress_text.set(text)
        self.progress_value.set(value)
        if phase in {"universe", "done"} or int(info.get("batch", info.get("current", 0)) or 0) % 5 == 0:
            self._append(text)

    def _complete(self, report: dict[str, object]) -> None:
        self.running = False
        self.start_button.configure(state="normal")
        self.resume_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if report.get("stopped"):
            summary = f"中断・保存しました。停止工程={report.get('phase', '')}。再実行するとキャッシュを利用します。"
        else:
            self.progress_value.set(100)
            summary = f"完了しました。対象企業={report.get('selected_companies', 0)}社、底検知イベント={report.get('event_count', 0)}件、SEC成功={report.get('sec_success_events', 0)}件"
        self.progress_text.set(summary)
        self._append(summary)
        timings = report.get("stage_timings_seconds")
        if isinstance(timings, dict) and timings:
            timing_text = ", ".join(f"{name}={seconds}s" for name, seconds in timings.items())
            self._append("stage timings: " + timing_text)
        messagebox.showinfo("米国株大量研究", summary)

    def _error(self, error: str) -> None:
        self.running = False
        self.start_button.configure(state="normal")
        self.resume_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.progress_text.set("エラーで終了しました")
        self._append("エラー: " + error)
        messagebox.showerror("米国株大量研究", error)

    def _open(self, filename: str) -> None:
        path = Path(self.output_dir.get().strip()) / filename
        if not path.exists():
            messagebox.showwarning("ファイル確認", f"まだ作成されていません:\n{path}")
            return
        os.startfile(path)  # type: ignore[attr-defined]

    def _open_csv(self) -> None:
        self._open("us_bottom_events_with_sec.csv")

    def _open_correlations(self) -> None:
        self._open("sec_change_correlations.csv")

    def _open_output(self) -> None:
        path = Path(self.output_dir.get().strip())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # type: ignore[attr-defined]


if __name__ == "__main__":
    UsStockResearchApp().mainloop()
