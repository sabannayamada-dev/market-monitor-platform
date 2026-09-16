from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from market_cap_model_analysis import (
    APP_VERSION,
    DEFAULT_LEGACY_APP,
    DEFAULT_MARKET_CAP_CSV,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PRICE_DB,
    DEFAULT_ROUTING_SETS,
    AnalysisConfig,
    LARGE_MODEL,
    MID_MODEL,
    SMALL_MODEL,
    ModelPreset,
    enrich_existing_year_end_returns,
    parse_routing_threshold_sets,
    rerun_threshold_set_simulations,
    run_analysis,
)


FIELDS = (
    ("stock_drawdown_percent", "必要下落率(%)"),
    ("recent_low_drawdown_percent", "直近安値下落率(%)"),
    ("stock_min_peak_age", "ピーク経過日数"),
    ("max_range_percent", "最大60日レンジ(%)"),
    ("max_abs_return_percent", "最大20日ボラ(%)"),
    ("setup_required_days", "セットアップ必要日数"),
    ("breakout_min_return_percent", "ブレイク最低上昇率(%)"),
    ("expansion_return_percent", "急伸最低上昇率(%)"),
    ("breakout_volume_ratio", "ブレイク出来高倍率"),
    ("expansion_volume_ratio", "急伸出来高倍率"),
    ("confirmation_days", "確認日数"),
    ("hold_tolerance", "維持許容率"),
    ("cooldown_days", "再検出待機日数"),
)


def eta_text(seconds: object) -> str:
    try:
        value = max(int(float(seconds)), 0)
    except (TypeError, ValueError):
        return "計算中"
    hours, remainder = divmod(value, 3600)
    minutes, sec = divmod(remainder, 60)
    if hours:
        return f"約{hours}時間{minutes}分"
    if minutes:
        return f"約{minutes}分{sec}秒"
    return f"約{sec}秒"


class ModelEditor(ttk.LabelFrame):
    def __init__(self, parent: tk.Widget, preset: ModelPreset, editable: bool) -> None:
        super().__init__(parent, text=preset.display_name, padding=10)
        self.preset = preset
        self.variables: dict[str, tk.StringVar] = {}
        self.columnconfigure(1, weight=1)
        for row, (field, label) in enumerate(FIELDS):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=2)
            variable = tk.StringVar(value=str(getattr(preset, field)))
            self.variables[field] = variable
            entry = ttk.Entry(self, textvariable=variable, width=12)
            entry.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=2)
            if not editable:
                entry.configure(state="readonly")
        if not editable:
            ttk.Label(
                self,
                text="比較基準のため編集不可",
                foreground="#667085",
            ).grid(row=len(FIELDS), column=0, columnspan=2, sticky="w", pady=(8, 0))

    def get(self) -> ModelPreset:
        integer_fields = {"stock_min_peak_age", "setup_required_days", "confirmation_days", "cooldown_days"}
        values: dict[str, object] = {}
        for field, _label in FIELDS:
            raw = self.variables[field].get().strip()
            values[field] = int(raw) if field in integer_fields else float(raw)
        return replace(self.preset, **values)

    def reset(self) -> None:
        for field, _label in FIELDS:
            self.variables[field].set(str(getattr(self.preset, field)))


class MarketCapModelAnalysisApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"時価総額帯別 底検知モデル比較 v{APP_VERSION}")
        self.geometry("1260x900")
        self.minsize(1000, 720)
        base = Path(__file__).resolve().parent
        self.legacy_app = tk.StringVar(value=str(DEFAULT_LEGACY_APP))
        self.price_db = tk.StringVar(value=str(DEFAULT_PRICE_DB))
        self.output_dir = tk.StringVar(value=str((base / DEFAULT_OUTPUT_DIR).resolve()))
        self.size_csv = tk.StringVar(value=str(DEFAULT_MARKET_CAP_CSV) if DEFAULT_MARKET_CAP_CSV.exists() else "")
        self.start_date = tk.StringVar(value="2012-01-01")
        self.end_date = tk.StringVar(value="2024-12-31")
        self.benchmark = tk.StringVar(value="ACWI")
        self.comparison_benchmarks = tk.StringVar(value="ACWI,SPY")
        self.company_limit = tk.StringVar(value="0")
        self.routing_sets = tk.StringVar(value=";".join(
            f"{item.name}:{item.small_max_jpy / 100_000_000:g}:{item.large_min_jpy / 100_000_000:g}"
            for item in DEFAULT_ROUTING_SETS
        ))
        self.run_all = tk.BooleanVar(value=True)
        self.progress_text = tk.StringVar(value="待機中")
        self.progress_value = tk.DoubleVar(value=0)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self._build_ui()
        self.after(100, self._drain_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="時価総額帯別 底検知モデル比較", font=("Yu Gothic UI", 20, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"v{APP_VERSION}", foreground="#667085").grid(row=0, column=1, sticky="e")
        ttk.Label(
            outer,
            text="現行の中型株モデルを固定基準にし、小型・大型向け固定閾値モデルを同一の日足で比較します。回帰分析は使用しません。",
            foreground="#475467",
        ).grid(row=1, column=0, sticky="w", pady=(4, 12))

        notebook = ttk.Notebook(outer)
        notebook.grid(row=2, column=0, sticky="ew")
        common = ttk.Frame(notebook, padding=12)
        models = ttk.Frame(notebook, padding=12)
        notebook.add(common, text="実行設定")
        notebook.add(models, text="固定閾値")
        common.columnconfigure(1, weight=1)
        self._path_row(common, 0, "現行 app.py", self.legacy_app, self._choose_app, "Python")
        self._path_row(common, 1, "株価キャッシュDB", self.price_db, self._choose_db, "SQLite")
        self._path_row(common, 2, "時価総額CSV（現在値でも可）", self.size_csv, self._choose_csv, "CSV")
        self._path_row(common, 3, "出力フォルダ", self.output_dir, self._choose_output, "Folder")

        row = ttk.Frame(common)
        row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        for column in range(12):
            row.columnconfigure(column, weight=0)
        settings = (
            ("開始日", self.start_date, 12), ("終了日", self.end_date, 12),
            ("ベンチマーク", self.benchmark, 9), ("企業上限（0=全社）", self.company_limit, 8),
        )
        for index, (label, variable, width) in enumerate(settings):
            ttk.Label(row, text=label).grid(row=0, column=index * 2, sticky="w", padx=(0 if index == 0 else 12, 4))
            ttk.Entry(row, textvariable=variable, width=width).grid(row=0, column=index * 2 + 1)
        ttk.Label(common, text="時価総額境界セット").grid(row=5, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(common, textvariable=self.routing_sets).grid(
            row=5, column=1, sticky="ew", padx=8, pady=(10, 0)
        )
        ttk.Label(common, text="セット名:小型上限億円:大型下限億円 を ; で区切ります").grid(
            row=5, column=2, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(
            common,
            text="3モデルを全銘柄に適用して横並び比較（推奨）",
            variable=self.run_all,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(common, text="同額購入比較（カンマ区切り）").grid(row=6, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(common, textvariable=self.comparison_benchmarks).grid(
            row=6, column=1, sticky="ew", padx=8, pady=(10, 0)
        )
        ttk.Label(
            common,
            text="現在時価総額CSVは、現在時価総額×シグナル価格÷基準価格でシグナル時点を推定します。",
            foreground="#667085",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(5, 0))

        models.columnconfigure(0, weight=1)
        models.columnconfigure(1, weight=1)
        models.columnconfigure(2, weight=1)
        self.small_editor = ModelEditor(models, SMALL_MODEL, True)
        self.mid_editor = ModelEditor(models, MID_MODEL, False)
        self.large_editor = ModelEditor(models, LARGE_MODEL, True)
        self.small_editor.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.mid_editor.grid(row=0, column=1, sticky="nsew", padx=5)
        self.large_editor.grid(row=0, column=2, sticky="nsew", padx=(5, 0))
        ttk.Button(models, text="小型・大型を初期値へ戻す", command=self._reset_models).grid(row=1, column=0, columnspan=3, pady=(10, 0))

        progress = ttk.LabelFrame(outer, text="実行・進捗", padding=12)
        progress.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        progress.columnconfigure(0, weight=1)
        progress.rowconfigure(4, weight=1)
        actions = ttk.Frame(progress)
        actions.grid(row=0, column=0, sticky="ew")
        self.start_button = ttk.Button(actions, text="比較分析を開始", command=lambda: self._start(False))
        self.start_button.pack(side="left")
        self.resume_button = ttk.Button(actions, text="続きから開始", command=lambda: self._start(True))
        self.resume_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(actions, text="中断", command=self._stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(actions, text="要約CSVを開く", command=lambda: self._open_file("model_summary.csv")).pack(side="left", padx=(18, 8))
        ttk.Button(actions, text="イベントCSVを開く", command=lambda: self._open_file("model_events.csv")).pack(side="left")
        ttk.Button(
            actions,
            text="同額購入比較を開く",
            command=lambda: self._open_file("benchmark_equal_weight_summary.csv"),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            actions,
            text="境界セット比較を開く",
            command=lambda: self._open_file("threshold_set_summary.csv"),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            actions,
            text="境界セットだけ再集計",
            command=self._rerun_routing_sets,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="出力フォルダを開く", command=self._open_output).pack(side="left", padx=8)
        year_actions = ttk.Frame(progress)
        year_actions.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.year_end_button = ttk.Button(
            year_actions, text="既存結果へ年末リターン追加", command=self._start_year_end_enrichment
        )
        self.year_end_button.pack(side="left")
        ttk.Button(
            year_actions,
            text="年末リターンCSVを開く",
            command=lambda: self._open_file("year_end_holding_returns.csv"),
        ).pack(side="left", padx=8)
        ttk.Button(
            year_actions,
            text="年末集計CSVを開く",
            command=lambda: self._open_file("year_end_holding_summary.csv"),
        ).pack(side="left")
        ttk.Button(
            year_actions,
            text="年末列付きイベントCSVを開く",
            command=lambda: self._open_file("model_events_with_year_end_returns.csv"),
        ).pack(side="left", padx=8)
        ttk.Label(progress, textvariable=self.progress_text, foreground="#175CD3", font=("Yu Gothic UI", 11, "bold")).grid(row=2, column=0, sticky="ew", pady=(10, 5))
        ttk.Progressbar(progress, variable=self.progress_value, maximum=100).grid(row=3, column=0, sticky="ew", pady=(0, 8))
        log_frame = ttk.Frame(progress)
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="word", state="disabled", font=("Consolas", 10))
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _path_row(self, parent: tk.Widget, row: int, label: str, variable: tk.StringVar, command: object, kind: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(parent, text="選択", command=command).grid(row=row, column=2)

    def _choose_app(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("Python", "*.py")])
        if value:
            self.legacy_app.set(value)

    def _choose_db(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("SQLite", "*.db"), ("All", "*.*")])
        if value:
            self.price_db.set(value)

    def _choose_csv(self) -> None:
        value = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if value:
            self.size_csv.set(value)

    def _choose_output(self) -> None:
        value = filedialog.askdirectory()
        if value:
            self.output_dir.set(value)

    def _reset_models(self) -> None:
        self.small_editor.reset()
        self.large_editor.reset()

    def _rerun_routing_sets(self) -> None:
        if self.running:
            messagebox.showwarning("境界セット再集計", "解析中は再集計できません")
            return
        try:
            threshold_sets = parse_routing_threshold_sets(self.routing_sets.get())
            output_dir = Path(self.output_dir.get().strip())
            started = time.perf_counter()
            paths = rerun_threshold_set_simulations(output_dir, threshold_sets)
            elapsed = time.perf_counter() - started
            self._append(f"境界セットだけ再集計しました: {len(threshold_sets)}セット / {elapsed:.2f}秒")
            messagebox.showinfo(
                "境界セット再集計",
                f"{len(threshold_sets)}セットを再集計しました。\n所要時間: {elapsed:.2f}秒\n{paths['threshold_set_summary']}",
            )
        except Exception as exc:
            messagebox.showerror("境界セット再集計", str(exc))

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _config(self, resume: bool) -> AnalysisConfig | None:
        try:
            app_path = Path(self.legacy_app.get().strip())
            db_path = Path(self.price_db.get().strip())
            if not app_path.exists() or not db_path.exists():
                raise ValueError("app.pyまたは株価DBが見つかりません")
            start = self.start_date.get().strip()
            end = self.end_date.get().strip()
            if start >= end:
                raise ValueError("開始日は終了日より前にしてください")
            limit = int(self.company_limit.get().strip())
            routing_sets = parse_routing_threshold_sets(self.routing_sets.get())
            primary_set = routing_sets[0]
            if limit < 0:
                raise ValueError("企業上限が不正です")
            if len(routing_sets) > 1 and not self.run_all.get():
                raise ValueError("複数の境界セットを比較する場合は、3モデル横並び比較を有効にしてください")
            size_path = Path(self.size_csv.get().strip()) if self.size_csv.get().strip() else None
            if size_path and not size_path.exists():
                raise ValueError("時価総額CSVが見つかりません")
            models = (self.small_editor.get(), MID_MODEL, self.large_editor.get())
            comparison_benchmarks = tuple(
                symbol.strip().upper()
                for symbol in self.comparison_benchmarks.get().split(",")
                if symbol.strip()
            )
            if not comparison_benchmarks:
                raise ValueError("同額購入比較には1つ以上の銘柄を指定してください")
            return AnalysisConfig(
                app_path, db_path, Path(self.output_dir.get().strip()),
                start_date=start, end_date=end, benchmark_symbol=self.benchmark.get().strip(),
                comparison_benchmark_symbols=comparison_benchmarks,
                company_limit=limit, size_history_csv=size_path,
                small_max_jpy=primary_set.small_max_jpy, large_min_jpy=primary_set.large_min_jpy,
                run_all_models=self.run_all.get(), resume=resume, models=models,
                routing_threshold_sets=routing_sets,
            )
        except Exception as exc:
            messagebox.showwarning("入力確認", str(exc))
            return None

    def _start(self, resume: bool) -> None:
        if self.running:
            return
        config = self._config(resume)
        if config is None:
            return
        self.running = True
        self.stop_event.clear()
        self.progress_value.set(0)
        self.progress_text.set("解析を準備しています")
        self.start_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self.year_end_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._append("続きから解析を再開します" if resume else "3モデルの比較分析を開始します")
        threading.Thread(target=self._worker, args=(config,), daemon=True).start()

    def _worker(self, config: AnalysisConfig) -> None:
        try:
            report = run_analysis(config, lambda payload: self.events.put(("progress", payload)), self.stop_event)
            self.events.put(("done", report))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _start_year_end_enrichment(self) -> None:
        if self.running:
            return
        app_path = Path(self.legacy_app.get().strip())
        db_path = Path(self.price_db.get().strip())
        output_dir = Path(self.output_dir.get().strip())
        if not app_path.exists() or not db_path.exists():
            messagebox.showwarning("年末リターン", "app.pyまたは株価DBが見つかりません")
            return
        if not (output_dir / "model_events.csv").exists():
            messagebox.showwarning("年末リターン", "先に比較分析を実行してください")
            return
        symbols = tuple(
            symbol.strip().upper()
            for symbol in self.comparison_benchmarks.get().split(",")
            if symbol.strip()
        )
        self.running = True
        self.progress_value.set(0)
        self.progress_text.set("既存イベントへ年末リターンを追加しています")
        self.start_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self.year_end_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self._append("既存のmodel_events.csvへ年末保有リターンを後付けします")
        threading.Thread(
            target=self._year_end_worker,
            args=(app_path, db_path, output_dir, symbols),
            daemon=True,
        ).start()

    def _year_end_worker(
        self,
        app_path: Path,
        db_path: Path,
        output_dir: Path,
        symbols: tuple[str, ...],
    ) -> None:
        try:
            report = enrich_existing_year_end_returns(
                app_path,
                db_path,
                output_dir,
                benchmark_symbol=self.benchmark.get().strip(),
                comparison_benchmark_symbols=symbols,
                progress=lambda payload: self.events.put(("progress", payload)),
            )
            self.events.put(("year_done", report))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _stop(self) -> None:
        if self.running:
            self.stop_event.set()
            self.progress_text.set("中断要求を受け付けました。現在企業の処理後に保存します")
            self._append("中断要求を送信しました")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self._progress(dict(payload))  # type: ignore[arg-type]
                elif kind == "done":
                    self._done(dict(payload))  # type: ignore[arg-type]
                elif kind == "year_done":
                    self._year_done(dict(payload))  # type: ignore[arg-type]
                else:
                    self._error(str(payload))
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _progress(self, info: dict[str, object]) -> None:
        phase = str(info.get("phase", ""))
        if phase == "analyze":
            current = int(info.get("current", 0))
            total = max(int(info.get("total", 1)), 1)
            self.progress_value.set(current / total * 100)
            self.progress_text.set(
                f"企業解析 {current}/{total}  現在={info.get('symbol', '')}  "
                f"シグナル={info.get('events', 0)}  残り={eta_text(info.get('eta_seconds'))}"
            )
        elif phase == "warning":
            self._append(f"警告: {info.get('message', '')}")
        elif phase == "year_end":
            current = int(info.get("current", 0))
            total = max(int(info.get("total", 1)), 1)
            self.progress_value.set(current / total * 100)
            self.progress_text.set(
                f"年末リターン追加 {current}/{total}  現在={info.get('symbol', '')}"
            )
        elif phase == "done":
            self.progress_value.set(100)

    def _done(self, report: dict[str, object]) -> None:
        self.running = False
        self.start_button.configure(state="normal")
        self.resume_button.configure(state="normal")
        self.year_end_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        stopped = bool(report.get("stopped"))
        self.progress_text.set("中断データを保存しました" if stopped else "比較分析が完了しました")
        self._append(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        messagebox.showinfo(
            "時価総額帯別モデル比較",
            ("中断地点まで保存しました。" if stopped else "比較分析が完了しました。")
            + f"\n企業数: {report.get('processed_company_count', 0)}"
            + f"\nシグナル数: {report.get('event_count', 0)}",
        )

    def _year_done(self, report: dict[str, object]) -> None:
        self.running = False
        self.start_button.configure(state="normal")
        self.resume_button.configure(state="normal")
        self.year_end_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.progress_value.set(100)
        self.progress_text.set("年末保有リターンの追加が完了しました")
        self._append(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        messagebox.showinfo(
            "年末リターン",
            f"年末保有リターンを追加しました。\n"
            f"イベント数: {report.get('event_count', 0)}\n"
            f"対象年: {report.get('start_year')}～{report.get('end_year')}",
        )

    def _error(self, message: str) -> None:
        self.running = False
        self.start_button.configure(state="normal")
        self.resume_button.configure(state="normal")
        self.year_end_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.progress_text.set("エラーで停止しました")
        self._append("エラー: " + message)
        messagebox.showerror("時価総額帯別モデル比較", message)

    def _open_output(self) -> None:
        path = Path(self.output_dir.get().strip())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # type: ignore[attr-defined]

    def _open_file(self, name: str) -> None:
        path = Path(self.output_dir.get().strip()) / name
        if not path.exists():
            messagebox.showwarning("ファイル確認", f"まだ出力されていません: {path}")
            return
        os.startfile(path)  # type: ignore[attr-defined]


if __name__ == "__main__":
    MarketCapModelAnalysisApp().mainloop()
