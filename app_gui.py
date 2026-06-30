from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
import tkinter as tk

import Proj01


APP_TITLE = "91wllm 批量文件获取器"
SETTINGS_FILE = "app_settings.json"


INSTRUCTIONS = """操作流程

1. 先用 Chrome 或 Edge 登录 91wllm 后台，并确认能看到就业信息列表。
2. 打开本工具，在“Cookie 粘贴区”粘贴浏览器里的 Cookie。
3. 选择输出目录。建议先选桌面或文档里的普通文件夹，避免系统目录权限问题。
4. 第一次运行建议选择“仅测试前 5 个”，确认文件名和目录没问题后再改为“全部下载”。
5. 点击“开始下载”。下载结果会按班级分文件夹保存，日志 CSV 会保存在输出目录根目录。

Cookie 获取方法

1. 在 Chrome 或 Edge 中打开已登录的 91wllm 后台页面。
2. 按 F12 打开开发者工具。
3. 点击 Network（网络）标签。
4. 刷新页面。
5. 在请求列表里点击加载就业信息列表的请求，一般地址里会包含 /admin/tempdb/jinfo/list/。
6. 在右侧 Headers（标头）里找到 Request Headers（请求标头）。
7. 找到 Cookie 这一行，复制整行内容，例如 Cookie: PHPSESSID=...; xxx=...
8. 回到本工具，粘贴到 Cookie 粘贴区。

注意事项

- Cookie 等同于临时登录凭证，不要发给别人，不要截图公开。
- Cookie 会过期。提示登录页、下载失败或没有权限时，先重新登录后台并重新复制 Cookie。
- 如果输出目录没有写入权限，请换到桌面、文档等普通目录，或右键选择“以管理员身份运行”。
- 高级设置看不懂可以不改，默认值已经按当前项目配置填写。
- “停止”会阻止后续行继续处理，已经下载完成的文件不会删除。
"""


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def settings_path() -> Path:
    return app_dir() / SETTINGS_FILE


def config_path() -> Path:
    return app_dir() / Proj01.DEFAULT_CONFIG_PATH


def load_default_config() -> dict[str, object]:
    defaults = dict(Proj01.DEFAULT_RUNTIME_CONFIG)
    candidate = config_path()
    if not candidate.exists():
        candidate = Path(__file__).resolve().parent / Proj01.DEFAULT_CONFIG_PATH
    try:
        defaults.update(Proj01.load_runtime_config(candidate))
    except Exception:
        pass
    return defaults


def load_saved_settings() -> dict[str, object]:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    if os.name != "nt":
        return False
    executable = sys.executable
    if getattr(sys, "frozen", False):
        params = ""
    else:
        params = f'"{Path(__file__).resolve()}"'
    try:
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, str(app_dir()), 1)
    except Exception:
        return False
    return result > 32


class DownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x720")
        self.minsize(880, 640)

        self.defaults = load_default_config()
        self.saved_settings = load_saved_settings()
        self.message_queue: queue.Queue[tuple[str, object, object]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None

        self._create_vars()
        self._build_ui()
        self._apply_initial_values()
        self._drain_queue()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_vars(self) -> None:
        self.start_url_var = tk.StringVar()
        self.out_dir_var = tk.StringVar()
        self.limit_mode_var = tk.StringVar(value="limited")
        self.limit_var = tk.StringVar(value="5")
        self.status_var = tk.StringVar(value="就绪")

        self.insecure_var = tk.BooleanVar(value=True)
        self.headful_var = tk.BooleanVar(value=False)
        self.grid_selector_var = tk.StringVar()
        self.next_selector_var = tk.StringVar()
        self.no_pagination_var = tk.BooleanVar(value=False)
        self.max_pages_var = tk.StringVar(value="200")
        self.timeout_ms_var = tk.StringVar(value="30000")
        self.class_col_var = tk.StringVar()
        self.student_id_col_var = tk.StringVar()
        self.name_col_var = tk.StringVar()
        self.attachment_col_var = tk.StringVar()

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        start_tab = ttk.Frame(notebook, padding=12)
        advanced_tab = ttk.Frame(notebook, padding=12)
        help_tab = ttk.Frame(notebook, padding=12)
        notebook.add(start_tab, text="开始下载")
        notebook.add(advanced_tab, text="高级设置")
        notebook.add(help_tab, text="操作说明")

        self._build_start_tab(start_tab)
        self._build_advanced_tab(advanced_tab)
        self._build_help_tab(help_tab)

    def _build_start_tab(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

        ttk.Label(frame, text="Cookie 粘贴区").grid(row=0, column=0, sticky="nw", pady=(0, 6))
        self.cookie_text = scrolledtext.ScrolledText(frame, height=8, wrap=tk.WORD)
        self.cookie_text.grid(row=0, column=1, columnspan=3, sticky="nsew", pady=(0, 10))

        ttk.Label(frame, text="开始地址").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.start_url_var).grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)

        ttk.Label(frame, text="输出目录").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.out_dir_var).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(frame, text="选择目录", command=self._choose_out_dir).grid(row=2, column=2, padx=(8, 0), pady=4)
        self.open_button = ttk.Button(frame, text="打开目录", command=self._open_out_dir)
        self.open_button.grid(row=2, column=3, padx=(8, 0), pady=4)

        ttk.Label(frame, text="下载范围").grid(row=3, column=0, sticky="w", pady=4)
        range_frame = ttk.Frame(frame)
        range_frame.grid(row=3, column=1, columnspan=3, sticky="w", pady=4)
        ttk.Radiobutton(range_frame, text="仅测试前", value="limited", variable=self.limit_mode_var).pack(side=tk.LEFT)
        ttk.Entry(range_frame, textvariable=self.limit_var, width=8).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(range_frame, text="个有证明链接的学生").pack(side=tk.LEFT, padx=(0, 18))
        ttk.Radiobutton(range_frame, text="全部下载", value="all", variable=self.limit_mode_var).pack(side=tk.LEFT)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=4, column=1, columnspan=3, sticky="w", pady=(10, 8))
        self.start_button = ttk.Button(button_frame, text="开始下载", command=self._start_download)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(button_frame, text="停止", command=self._stop_download, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(button_frame, text="保存设置", command=self._save_settings_with_notice).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(frame, textvariable=self.status_var).grid(row=5, column=1, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="实时日志").grid(row=6, column=0, sticky="nw")
        self.log_text = scrolledtext.ScrolledText(frame, height=16, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.grid(row=7, column=0, columnspan=4, sticky="nsew")

    def _build_advanced_tab(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)

        row = 0
        ttk.Checkbutton(frame, text="禁用 TLS 证书校验（INSECURE）", variable=self.insecure_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1
        ttk.Checkbutton(frame, text="抓取时显示浏览器窗口（HEADFUL）", variable=self.headful_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1
        ttk.Checkbutton(frame, text="只处理当前页，不自动翻页（NO_PAGINATION）", variable=self.no_pagination_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1

        entries = [
            ("表格行选择器（GRID_SELECTOR）", self.grid_selector_var),
            ("下一页选择器（NEXT_SELECTOR，可留空）", self.next_selector_var),
            ("最大页数（MAX_PAGES）", self.max_pages_var),
            ("页面超时毫秒（TIMEOUT_MS）", self.timeout_ms_var),
            ("班级列号（CLASS_COL，可留空自动识别）", self.class_col_var),
            ("学号列号（STUDENT_ID_COL，可留空自动识别）", self.student_id_col_var),
            ("姓名列号（NAME_COL，可留空自动识别）", self.name_col_var),
            ("附件列号（ATTACHMENT_COL，可留空自动识别）", self.attachment_col_var),
        ]
        for label, var in entries:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky="ew", pady=5)
            row += 1

    def _build_help_tab(self, frame: ttk.Frame) -> None:
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text = scrolledtext.ScrolledText(frame, wrap=tk.WORD)
        text.grid(row=0, column=0, sticky="nsew")
        text.insert("1.0", INSTRUCTIONS)
        text.configure(state=tk.DISABLED)

    def _apply_initial_values(self) -> None:
        values = {**self.defaults, **self.saved_settings}
        self.start_url_var.set(str(values.get("start_url") or Proj01.DEFAULT_START_URL))
        self.out_dir_var.set(str(values.get("out") or str(app_dir() / "downloads")))

        limit = values.get("limit", 5)
        if limit is None:
            self.limit_mode_var.set("all")
            self.limit_var.set("5")
        else:
            self.limit_mode_var.set("limited")
            self.limit_var.set(str(limit))

        self.insecure_var.set(bool(values.get("insecure", True)))
        self.headful_var.set(bool(values.get("headful", False)))
        self.grid_selector_var.set(str(values.get("grid_selector") or Proj01.DEFAULT_GRID_SELECTOR))
        self.next_selector_var.set(str(values.get("next_selector") or ""))
        self.no_pagination_var.set(bool(values.get("no_pagination", False)))
        self.max_pages_var.set(str(values.get("max_pages") or 200))
        self.timeout_ms_var.set(str(values.get("timeout_ms") or 30000))
        self.class_col_var.set("" if values.get("class_col") in (None, "") else str(values.get("class_col")))
        self.student_id_col_var.set(
            "" if values.get("student_id_col") in (None, "") else str(values.get("student_id_col"))
        )
        self.name_col_var.set("" if values.get("name_col") in (None, "") else str(values.get("name_col")))
        self.attachment_col_var.set(
            "" if values.get("attachment_col") in (None, "") else str(values.get("attachment_col"))
        )

    def _choose_out_dir(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.out_dir_var.get() or str(app_dir()))
        if directory:
            self.out_dir_var.set(directory)

    def _open_out_dir(self) -> None:
        path = Path(self.out_dir_var.get()).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _positive_int(self, value: str, label: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(f"{label} 必须填写正整数。") from exc
        if number <= 0:
            raise ValueError(f"{label} 必须大于 0。")
        return number

    def _optional_positive_int(self, value: str, label: str) -> int | None:
        value = value.strip()
        if not value:
            return None
        return self._positive_int(value, label)

    def _collect_args(self) -> argparse.Namespace:
        cookie_text = self.cookie_text.get("1.0", tk.END).strip()
        if not cookie_text:
            raise ValueError("请先粘贴 Cookie。")

        start_url = self.start_url_var.get().strip()
        if not start_url:
            raise ValueError("请填写开始地址。")

        out_dir = self.out_dir_var.get().strip()
        if not out_dir:
            raise ValueError("请选择输出目录。")

        if self.limit_mode_var.get() == "all":
            limit = None
        else:
            limit = self._positive_int(self.limit_var.get().strip(), "测试数量")

        max_pages = self._positive_int(self.max_pages_var.get().strip(), "最大页数")
        timeout_ms = self._positive_int(self.timeout_ms_var.get().strip(), "页面超时毫秒")
        grid_selector = self.grid_selector_var.get().strip()
        if not grid_selector:
            raise ValueError("表格行选择器不能为空。")

        return argparse.Namespace(
            cookie_json=None,
            cookie_text=cookie_text,
            start_url=start_url,
            out=out_dir,
            insecure=self.insecure_var.get(),
            headful=self.headful_var.get(),
            grid_selector=grid_selector,
            next_selector=self.next_selector_var.get().strip() or None,
            no_pagination=self.no_pagination_var.get(),
            max_pages=max_pages,
            limit=limit,
            timeout_ms=timeout_ms,
            class_col=self._optional_positive_int(self.class_col_var.get(), "班级列号"),
            student_id_col=self._optional_positive_int(self.student_id_col_var.get(), "学号列号"),
            name_col=self._optional_positive_int(self.name_col_var.get(), "姓名列号"),
            attachment_col=self._optional_positive_int(self.attachment_col_var.get(), "附件列号"),
            config=Proj01.DEFAULT_CONFIG_PATH,
            self_test=False,
        )

    def _settings_dict(self) -> dict[str, object]:
        try:
            limit: int | None
            if self.limit_mode_var.get() == "all":
                limit = None
            else:
                limit = self._positive_int(self.limit_var.get().strip(), "测试数量")
        except ValueError:
            limit = 5

        return {
            "start_url": self.start_url_var.get().strip(),
            "out": self.out_dir_var.get().strip(),
            "limit": limit,
            "insecure": self.insecure_var.get(),
            "headful": self.headful_var.get(),
            "grid_selector": self.grid_selector_var.get().strip(),
            "next_selector": self.next_selector_var.get().strip() or None,
            "no_pagination": self.no_pagination_var.get(),
            "max_pages": self.max_pages_var.get().strip(),
            "timeout_ms": self.timeout_ms_var.get().strip(),
            "class_col": self.class_col_var.get().strip() or None,
            "student_id_col": self.student_id_col_var.get().strip() or None,
            "name_col": self.name_col_var.get().strip() or None,
            "attachment_col": self.attachment_col_var.get().strip() or None,
        }

    def _save_settings(self) -> bool:
        try:
            settings_path().write_text(
                json.dumps(self._settings_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self._append_log(f"设置保存失败：{exc}")
            return False
        return True

    def _save_settings_with_notice(self) -> None:
        if self._save_settings():
            messagebox.showinfo("已保存", "非敏感设置已保存。Cookie 不会保存。")

    def _check_output_writable(self, out_dir: Path) -> bool:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            probe = out_dir / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except OSError as exc:
            message = (
                f"输出目录没有写入权限：\n{out_dir}\n\n"
                f"错误信息：{exc}\n\n"
                "建议先换到桌面或文档里的普通文件夹。是否尝试以管理员身份重新打开本工具？"
            )
            if messagebox.askyesno("输出目录无权限", message):
                if relaunch_as_admin():
                    self.destroy()
                else:
                    messagebox.showerror("提权失败", "无法自动以管理员身份重新打开，请手动右键 exe 选择“以管理员身份运行”。")
            return False

    def _start_download(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return

        try:
            args = self._collect_args()
        except ValueError as exc:
            messagebox.showerror("填写有误", str(exc))
            return

        out_dir = Path(args.out).expanduser()
        if not self._check_output_writable(out_dir):
            return
        args.out = str(out_dir)
        self._save_settings()

        self.stop_event = threading.Event()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.status_var.set("正在启动...")
        self._append_log("开始下载任务。")

        self.worker_thread = threading.Thread(target=self._run_worker, args=(args, self.stop_event), daemon=True)
        self.worker_thread.start()

    def _stop_download(self) -> None:
        if self.stop_event:
            self.stop_event.set()
            self._append_log("已请求停止，当前网络操作结束后会停止后续处理。")
            self.stop_button.configure(state=tk.DISABLED)

    def _run_worker(self, args: argparse.Namespace, stop_event: threading.Event) -> None:
        def progress(message: str, counters: Proj01.Counters | None) -> None:
            snapshot = counters.__dict__.copy() if counters else None
            self.message_queue.put(("progress", message, snapshot))

        try:
            code = Proj01.run_live(args, progress_callback=progress, stop_event=stop_event)
        except Exception as exc:
            self.message_queue.put(("error", str(exc), None))
            return
        self.message_queue.put(("done", code, stop_event.is_set()))

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload, extra = self.message_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "progress":
                self._append_log(str(payload))
                if isinstance(extra, dict):
                    self.status_var.set(
                        "页面={pages}  行={table_rows}  有附件={rows_with_attachments}  "
                        "已下载={downloaded}  已跳过={skipped_existing}  失败={download_failures}".format(**extra)
                    )
            elif kind == "error":
                self._append_log(f"程序异常：{payload}")
                self.status_var.set("程序异常")
                self._finish_buttons()
                messagebox.showerror("程序异常", str(payload))
            elif kind == "done":
                code = int(payload)
                stopped = bool(extra)
                self._finish_buttons()
                if stopped:
                    self.status_var.set("已停止")
                    messagebox.showinfo("已停止", "任务已停止。已经下载完成的文件不会删除。")
                elif code == 0:
                    self.status_var.set("完成")
                    messagebox.showinfo("完成", "下载任务完成。")
                else:
                    self.status_var.set(f"失败，退出码 {code}")
                    messagebox.showerror("运行失败", f"任务没有完成，退出码：{code}。请查看实时日志。")

        self.after(120, self._drain_queue)

    def _finish_buttons(self) -> None:
        self.start_button.configure(state=tk.NORMAL)
        self.stop_button.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("任务仍在运行", "下载任务仍在运行。是否请求停止并关闭窗口？"):
                return
            if self.stop_event:
                self.stop_event.set()
        self._save_settings()
        self.destroy()


def main() -> None:
    app = DownloaderApp()
    if is_admin():
        app._append_log("当前已使用管理员权限运行。")
    app.mainloop()


if __name__ == "__main__":
    main()
