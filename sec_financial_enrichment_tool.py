from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from sec_financial_enrichment import APP_VERSION, enrich_csv


APP_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = APP_DIR / "outputs" / "stock_bottom_financial_enrichment" / "stock_bottom_with_edinet_financials_with_price_metrics.csv"
DEFAULT_OUTPUT = APP_DIR / "outputs" / "sec_financial_enrichment"


def format_eta(seconds: object) -> str:
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


class SecFinancialEnrichmentApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"米国株 SEC財務結合ツール v{APP_VERSION}")
        self.geometry("1120x760")
        self.minsize(900, 620)
        self.input_path = tk.StringVar(value=str(DEFAULT_INPUT) if DEFAULT_INPUT.exists() else "")
        self.output_dir = tk.StringVar(value=str(DEFAULT_OUTPUT))
        self.user_agent = tk.StringVar(value="")
        self.max_rows = tk.StringVar(value="")
        self.request_interval = tk.DoubleVar(value=0.15)
        self.refresh_cache = tk.BooleanVar(value=False)
        self.progress_text = tk.StringVar(value="待機中")
        self.progress_value = tk.DoubleVar(value=0.0)
        self.running = False
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._build_ui()
        self.after(100, self._drain_events)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        heading = ttk.Frame(root)
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="米国株 SEC財務結合", font=("Yu Gothic UI", 20, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(heading, text=f"v{APP_VERSION}", foreground="#667085").grid(row=0, column=1, sticky="e")
        ttk.Label(
            root,
            text="底検知日までにSECへ提出されていた財務データだけを入力CSVへ追加します。SEC APIキーは不要です。",
            foreground="#475467",
        ).grid(row=1, column=0, sticky="w", pady=(4, 14))

        settings = ttk.LabelFrame(root, text="実行設定", padding=14)
        settings.grid(row=2, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)
        self._file_row(settings, 0, "入力CSV", self.input_path, self._choose_input)
        self._file_row(settings, 1, "出力フォルダ", self.output_dir, self._choose_output)
        ttk.Label(settings, text="SEC User-Agent").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(settings, textvariable=self.user_agent).grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        ttk.Label(settings, text="氏名またはアプリ名＋メールアドレス（例: Taro user@example.com）").grid(row=2, column=2, sticky="w")

        options = ttk.Frame(settings)
        options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(options, text="上から処理する行数").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.max_rows, width=10).grid(row=0, column=1, padx=(8, 16))
        ttk.Label(options, text="空欄なら全行").grid(row=0, column=2, sticky="w")
        ttk.Label(options, text="API間隔(秒)").grid(row=0, column=3, padx=(28, 0))
        ttk.Spinbox(options, from_=0.11, to=2.0, increment=0.05, textvariable=self.request_interval, width=7).grid(row=0, column=4, padx=8)
        ttk.Checkbutton(options, text="キャッシュを更新して再取得", variable=self.refresh_cache).grid(row=0, column=5, padx=(22, 0))

        actions = ttk.Frame(settings)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.start_button = ttk.Button(actions, text="SEC財務の収集・結合を開始", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="中断", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="完成CSVを開く", command=self._open_result).pack(side="left", padx=(14, 0))
        ttk.Button(actions, text="出力フォルダを開く", command=self._open_output).pack(side="left", padx=8)

        progress = ttk.LabelFrame(root, text="進捗", padding=14)
        progress.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        progress.columnconfigure(0, weight=1)
        progress.rowconfigure(2, weight=1)
        ttk.Label(progress, textvariable=self.progress_text, font=("Yu Gothic UI", 12, "bold"), foreground="#175CD3", wraplength=1000).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Progressbar(progress, variable=self.progress_value, maximum=100).grid(row=1, column=0, sticky="ew", pady=(10, 12))
        log_frame = ttk.Frame(progress)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=18, wrap="word", font=("Consolas", 10), state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _file_row(self, parent: ttk.Widget, row: int, label: str, variable: tk.StringVar, command: object) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(parent, text="選択", command=command).grid(row=row, column=2, sticky="e")

    def _choose_input(self) -> None:
        selected = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if selected:
            self.input_path.set(selected)

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.output_dir.set(selected)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _validate(self) -> tuple[Path, Path, int | None] | None:
        input_path = Path(self.input_path.get().strip())
        output_dir = Path(self.output_dir.get().strip())
        if not input_path.exists():
            messagebox.showwarning("入力確認", "入力CSVを選択してください。")
            return None
        if "@" not in self.user_agent.get():
            messagebox.showwarning("入力確認", "SEC User-Agentにメールアドレスを含めてください。")
            return None
        max_rows_text = self.max_rows.get().strip()
        if max_rows_text and (not max_rows_text.isdigit() or int(max_rows_text) < 1):
            messagebox.showwarning("入力確認", "処理行数は空欄または1以上の整数にしてください。")
            return None
        return input_path, output_dir, int(max_rows_text) if max_rows_text else None

    def _start(self) -> None:
        if self.running:
            return
        validated = self._validate()
        if not validated:
            return
        self.running = True
        self.stop_event.clear()
        self.progress_value.set(0)
        self.progress_text.set("入力CSVを確認し、SECティッカーとCIKを対応付けています...")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._append_log("SEC財務結合を開始しました。")
        threading.Thread(target=self._worker, args=validated, daemon=True).start()

    def _worker(self, input_path: Path, output_dir: Path, max_rows: int | None) -> None:
        try:
            report = enrich_csv(
                input_path=input_path,
                output_dir=output_dir,
                user_agent=self.user_agent.get(),
                max_rows=max_rows,
                request_interval=self.request_interval.get(),
                refresh_cache=self.refresh_cache.get(),
                progress=lambda info: self.events.put(("progress", info)),
                stop_event=self.stop_event,
            )
            self.events.put(("complete", report))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _stop(self) -> None:
        if self.running:
            self.stop_event.set()
            self.progress_text.set("中断要求を受け付けました。現在の企業を保存後に終了します...")
            self._append_log("中断要求を送信しました。")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self._handle_progress(payload)
                elif kind == "complete":
                    self._handle_complete(payload)
                elif kind == "error":
                    self._handle_error(str(payload))
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _handle_progress(self, payload: object) -> None:
        info = dict(payload)  # type: ignore[arg-type]
        if info.get("phase") != "collect":
            return
        self.progress_value.set(float(info.get("percent", 0)))
        text = (
            f"{info.get('current', 0)}/{info.get('total', 0)}件 ({info.get('percent', 0):.1f}%) "
            f"現在={info.get('ticker', '')} 成功={info.get('success', 0)} "
            f"財務なし={info.get('no_facts', 0)} エラー={info.get('errors', 0)} "
            f"推定残り={format_eta(info.get('eta_seconds'))}"
        )
        self.progress_text.set(text)
        if int(info.get("current", 0)) == 1 or int(info.get("current", 0)) % 10 == 0:
            self._append_log(text)

    def _handle_complete(self, payload: object) -> None:
        report = dict(payload)  # type: ignore[arg-type]
        self.running = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.progress_value.set(100 if not report.get("stopped") else self.progress_value.get())
        status = "中断・保存しました" if report.get("stopped") else "完了しました"
        summary = (
            f"SEC財務結合が{status}。対象={report.get('eligible_rows', 0)}件 "
            f"処理={report.get('processed_rows', 0)}件 成功={report.get('success_rows', 0)}件 "
            f"財務なし={report.get('no_facts_rows', 0)}件 エラー={report.get('error_rows', 0)}件"
        )
        self.progress_text.set(summary)
        self._append_log(summary)
        self._append_log(f"完成CSV: {report.get('output_csv', '')}")
        messagebox.showinfo("SEC財務結合", summary + f"\n\n完成CSV:\n{report.get('output_csv', '')}")

    def _handle_error(self, error: str) -> None:
        self.running = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.progress_text.set("エラーで終了しました")
        self._append_log("エラー: " + error)
        messagebox.showerror("SEC財務結合", error)

    def _open_result(self) -> None:
        path = Path(self.output_dir.get().strip()) / "stock_bottom_with_sec_financials.csv"
        if not path.exists():
            messagebox.showwarning("ファイル確認", f"完成CSVがありません:\n{path}")
            return
        os.startfile(path)  # type: ignore[attr-defined]

    def _open_output(self) -> None:
        path = Path(self.output_dir.get().strip())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # type: ignore[attr-defined]


if __name__ == "__main__":
    SecFinancialEnrichmentApp().mainloop()
