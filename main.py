#!/usr/bin/env python3
"""
7Zip-Master-GUI — macOS dark-mode style front-end for 7-Zip (7zz).
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, TclError, filedialog, messagebox

import customtkinter as ctk

APP_NAME = "7Zip-Master-GUI"

LGPL_ABOUT_TEXT = (
    "Powered by 7-Zip (created by Igor Pavlov). The 7-Zip command line utility "
    "is bundled with this application and is licensed under the GNU LGPL. "
    "This frontend is an independent wrapper."
)

# Keep UI responsive: max queue items handled per timer tick; avoid blocking wait() on main thread
_MAX_DRAIN_BATCH = 64
# Batch per-byte "raw" queue items from the reader thread (fewer UI wakeups for long 7zz output)
_RAW_QUEUE_BATCH = 512

# Tk Text mark for in-place updates of the current (no trailing newline yet) line
_LIVE_MARK = "sevenzip_live"

# Queue: char stream, completed lines, coalesced 7-Zip "N  N%  N" progress text, % value, or end
QueueItem = tuple[str, str] | tuple[str, int] | None

# 7-Zip pads the next progress update with spaces instead of always using \r/\b
_TRIPLET_PROGRESS = re.compile(r"\d+\s+\d+%\s+\d+")
# Compression often reports "12% …" without the N N% N triplet — drive bar from any NN% in the line tail
_PCT_IN_LINE = re.compile(r"(?<!\d)(\d{1,3})\s*%")

def _redact_cmd_for_log(cmd: list[str]) -> list[str]:
    out: list[str] = []
    for part in cmd:
        if part.startswith("-p") and len(part) > 2:
            out.append("-p***")
        else:
            out.append(part)
    return out


def _decrypt_log_path() -> Path:
    """Where decrypt_logs.log is written (project dir in dev; MacOS folder inside .app when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "decrypt_logs.log"
    return Path(__file__).resolve().parent / "decrypt_logs.log"


def resolve_7zz() -> str | None:
    """Bundled 7zz next to the frozen executable (PyInstaller), else system fallback."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "7zz"
        if bundled.is_file():
            return str(bundled)
    fallback = Path("/usr/local/bin/7zip")
    if fallback.is_file():
        return str(fallback)
    return None


class SevenZipMasterGUI(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title(APP_NAME)
        self._win_w = 900
        self._compact_h = 520
        self._expanded_h = 760
        self._min_w = 720
        self._min_compact_h = 520
        self._min_expanded_h = 520
        self.geometry(f"{self._win_w}x{self._compact_h}")
        self.minsize(self._min_w, self._min_compact_h)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self._seven_zip = resolve_7zz()
        self._proc: subprocess.Popen | None = None
        self._reader_done = threading.Event()
        self._output_queue: queue.Queue[QueueItem] = queue.Queue()
        self._poll_after_id: str | None = None
        self._stream_line: list[str] = []
        self._user_stopped = False
        self._details_expanded = False
        self._decrypt_log_lock = threading.Lock()
        self._extract_mode_value = "Extract"
        self._mirror_to_extract_list_panel = False
        self._extract_list_committed = ""

        self._build_ui()
        self._refresh_binary_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_progress_value(0.0)
        self.after(0, self._autosize_to_content)

    def _autosize_to_content(self) -> None:
        """Ensure the initial window is tall enough to show the active tab's controls."""
        try:
            self.update_idletasks()
            req_h = int(self.winfo_reqheight())
            req_w = int(self.winfo_reqwidth())
            screen_h = int(self.winfo_screenheight())
        except Exception:
            return

        target_w = max(self._min_w, self._win_w, req_w)
        target_h = max(self._min_compact_h, self._compact_h, req_h)
        target_h = min(target_h, max(420, screen_h - 80))

        self._win_w = target_w
        self._compact_h = target_h
        self.geometry(f"{target_w}x{target_h}")
        self.minsize(self._min_w, self._min_compact_h)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        '''
        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        top_bar.grid_columnconfigure(0, weight=1)
        self._status = ctk.CTkLabel(
            top_bar,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        self._status.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            top_bar,
            text="About / License",
            width=118,
            command=self._show_about_license,
            fg_color="transparent",
            border_width=1,
            border_color=("#4a6fa5", "#4a6fa5"),
            text_color=("#a0c4e8", "#a0c4e8"),
        ).grid(row=0, column=1, sticky="e")
        '''
        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        top_bar.grid_columnconfigure(0, weight=1)
        
        self._status = ctk.CTkLabel(
            top_bar,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        self._status.grid(row=0, column=0, sticky="w")

# --- NEW 3-STAGE TOGGLE (Minimalist Symbols) ---
        self._appearance_toggle = ctk.CTkSegmentedButton(
            top_bar,
            values=["◐", "☼", "☾"],
            command=self._change_appearance_mode,
            width=120,
            selected_color=("#4a6fa5", "#4a6fa5"),
            selected_hover_color=("#3a5b8a", "#3a5b8a"),
        )
        self._appearance_toggle.set("☾") # Set Dark mode as the default
        self._appearance_toggle.grid(row=0, column=1, sticky="e", padx=(0, 12))

        ctk.CTkButton(
            top_bar,
            text="About / License",
            width=118,
            command=self._show_about_license,
            fg_color="transparent",
            border_width=1,
            border_color=("#4a6fa5", "#4a6fa5"),
            text_color=("#a0c4e8", "#a0c4e8"),
        ).grid(row=0, column=2, sticky="e")

        tabs = ctk.CTkTabview(self)
        tabs.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        tabs.add("Compress")
        tabs.add("Extract")

        self._build_compress_tab(tabs.tab("Compress"))
        self._build_extract_tab(tabs.tab("Extract"))

        pct_row = ctk.CTkFrame(self, fg_color="transparent")
        pct_row.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 2))
        pct_row.grid_columnconfigure(0, weight=1)
        self._pct_label = ctk.CTkLabel(
            pct_row,
            text="0%",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        self._pct_label.grid(row=0, column=0)

        progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        progress_frame.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 4))
        progress_frame.grid_columnconfigure(0, weight=1)

        self._progress = ctk.CTkProgressBar(progress_frame, mode="determinate")
        self._progress.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self._stop_btn = ctk.CTkButton(
            progress_frame,
            text="Stop",
            width=88,
            command=self._stop_7zz,
            state="disabled",
            fg_color=("#8b3a3a", "#6b2a2a"),
            hover_color=("#a34545", "#853535"),
        )
        self._stop_btn.grid(row=0, column=1, sticky="e")

        details_row = ctk.CTkFrame(self, fg_color="transparent")
        details_row.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        details_row.grid_columnconfigure(0, weight=1)
        details_row.grid_columnconfigure(2, weight=1)
        self._details_btn = ctk.CTkButton(
            details_row,
            text="Show more details",
            command=self._toggle_details,
            height=32,
            fg_color="transparent",
            border_width=1,
            border_color=("#4a6fa5", "#4a6fa5"),
            text_color=("#a0c4e8", "#a0c4e8"),
        )
        self._details_btn.grid(row=0, column=1)

        self._log_frame = ctk.CTkFrame(self)
        self._log_frame.grid_rowconfigure(0, weight=1)
        self._log_frame.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=0, minsize=0)

        self._log = ctk.CTkTextbox(
            self._log_frame,
            width=400,
            height=200,
            font=ctk.CTkFont(family="Menlo", size=11),
            fg_color="#0d0d0d",
            text_color="#c8f5c8",
            border_color="#333333",
            border_width=1,
        )
        self._log.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    def _build_compress_tab(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(parent, text="Source:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self._compress_source = ctk.CTkEntry(parent, placeholder_text="No folder or file selected")
        self._compress_source.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        ctk.CTkButton(parent, text="Select Folder/File", width=140, command=self._pick_compress_source).grid(
            row=0, column=2, padx=8, pady=6
        )

        ctk.CTkLabel(parent, text="Destination:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self._compress_dest = ctk.CTkEntry(parent, placeholder_text="Output archive path (.7z, .zip, …)")
        self._compress_dest.grid(row=1, column=1, sticky="ew", padx=8, pady=6)
        ctk.CTkButton(parent, text="Destination", width=140, command=self._pick_compress_dest).grid(
            row=1, column=2, padx=8, pady=6
        )

        ctk.CTkLabel(parent, text="Compression:").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self._compress_level = ctk.CTkOptionMenu(parent, values=["Fast", "Normal", "Ultra"])
        self._compress_level.set("Normal")
        self._compress_level.grid(row=2, column=1, sticky="w", padx=8, pady=6)

        ctk.CTkLabel(parent, text="Password (optional):").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        self._compress_password = ctk.CTkEntry(parent, placeholder_text="Leave empty for no encryption", show="•")
        self._compress_password.grid(row=3, column=1, columnspan=2, sticky="ew", padx=8, pady=6)

        adv = ctk.CTkFrame(parent, fg_color=("gray92", "gray18"), corner_radius=8)
        adv.grid(row=4, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
        adv.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(adv, text="Advanced", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4)
        )

        self._compress_sdel = BooleanVar(value=False)
        ctk.CTkCheckBox(
            adv,
            text="Delete original files after compression (-sdel)",
            variable=self._compress_sdel,
            onvalue=True,
            offvalue=False,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=4)

        ctk.CTkLabel(adv, text="Archive format (-t):").grid(row=2, column=0, sticky="w", padx=10, pady=4)
        self._compress_format = ctk.CTkOptionMenu(adv, values=["7z", "zip", "tar"])
        self._compress_format.set("7z")
        self._compress_format.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=4)

        ctk.CTkLabel(adv, text="Split volumes (-v):").grid(row=3, column=0, sticky="w", padx=10, pady=4)
        self._compress_volume = ctk.CTkOptionMenu(
            adv, values=["Don't Split", "100m", "1g", "4g"]
        )
        self._compress_volume.set("Don't Split")
        self._compress_volume.grid(row=3, column=1, sticky="ew", padx=(0, 10), pady=4)

        ctk.CTkLabel(adv, text="CPU threads (-mmt):").grid(row=4, column=0, sticky="w", padx=10, pady=(4, 10))
        self._compress_mmt = ctk.CTkOptionMenu(adv, values=["Auto", "1", "2", "4", "8"])
        self._compress_mmt.set("Auto")
        self._compress_mmt.grid(row=4, column=1, sticky="ew", padx=(0, 10), pady=(4, 10))

        ctk.CTkButton(parent, text="Start compression", command=self._start_compress).grid(
            row=5, column=0, columnspan=3, pady=16
        )

    def _build_extract_tab(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(parent, text="Archive:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self._extract_archive = ctk.CTkEntry(parent, placeholder_text="No archive selected")
        self._extract_archive.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        ctk.CTkButton(parent, text="Select Archive", width=140, command=self._pick_extract_archive).grid(
            row=0, column=2, padx=8, pady=6
        )

        ctk.CTkLabel(parent, text="Action:").grid(row=1, column=0, sticky="nw", padx=8, pady=6)
        self._extract_mode = ctk.CTkSegmentedButton(
            parent,
            values=["Extract", "Test", "List"],
            command=self._on_extract_mode_change,
        )
        self._extract_mode.set("Extract")
        self._extract_mode.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=6)

        ctk.CTkLabel(parent, text="Extract to:").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self._extract_dest = ctk.CTkEntry(parent, placeholder_text="Output folder")
        self._extract_dest.grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        self._extract_dest_btn = ctk.CTkButton(
            parent, text="Extract To", width=140, command=self._pick_extract_dest
        )
        self._extract_dest_btn.grid(row=2, column=2, padx=8, pady=6)

        self._extract_pwd_enabled = BooleanVar(value=False)
        self._extract_pwd_check = ctk.CTkCheckBox(
            parent,
            text="Archive has a password",
            variable=self._extract_pwd_enabled,
            onvalue=True,
            offvalue=False,
            command=self._sync_extract_password_entry_state,
        )
        self._extract_pwd_check.grid(row=3, column=0, sticky="w", padx=8, pady=6)
        self._extract_password = ctk.CTkEntry(
            parent,
            placeholder_text="Password",
            show="•",
            state="disabled",
        )
        self._extract_password.grid(row=3, column=1, columnspan=2, sticky="ew", padx=8, pady=6)

        ctk.CTkButton(parent, text="Run", command=self._start_extract).grid(
            row=4, column=0, columnspan=3, pady=(16, 8)
        )

        ctk.CTkLabel(
            parent,
            text="Listing output (shown here when Action is List — also in Details log)",
            anchor="w",
            font=ctk.CTkFont(size=12),
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 4))
        self._extract_list_panel = ctk.CTkTextbox(
            parent,
            width=400,
            height=220,
            font=ctk.CTkFont(family="Menlo", size=11),
            fg_color="#0d0d0d",
            text_color="#c8f5c8",
            border_color="#333333",
            border_width=1,
            wrap="none",
        )
        self._extract_list_panel.grid(row=6, column=0, columnspan=3, sticky="nsew", padx=8, pady=(0, 8))
        parent.grid_rowconfigure(6, weight=1)
        self._extract_list_panel.insert("1.0", "Select Action: List, then Run — output appears here.\n")
        self._extract_list_panel.configure(state="disabled")

        self._sync_extract_password_entry_state()
        self._on_extract_mode_change("Extract")

    def _sync_extract_password_entry_state(self) -> None:
        st = "normal" if self._extract_pwd_enabled.get() else "disabled"
        self._extract_password.configure(state=st)

    def _on_extract_mode_change(self, value: str | None = None) -> None:
        mode = value if value is not None else self._extract_mode.get()
        self._extract_mode_value = mode
        need_dest = mode == "Extract"
        self._extract_dest_btn.configure(state="normal" if need_dest else "disabled")
        # Use "readonly" (not "disabled") so the field does not confuse Tk/CTk focus &
        # placeholder handling when Test/List ignore this path — avoids odd tab/widget loops.
        self._extract_dest.configure(state="normal" if need_dest else "readonly")

    def _show_about_license(self) -> None:
        messagebox.showinfo(f"{APP_NAME} — About", LGPL_ABOUT_TEXT, parent=self)

    def _change_appearance_mode(self, selected_mode: str) -> None:
        if selected_mode == "◐":
            ctk.set_appearance_mode("System")
        elif selected_mode == "☼":
            ctk.set_appearance_mode("Light")
        elif selected_mode == "☾":
            ctk.set_appearance_mode("Dark")

    def _refresh_binary_status(self) -> None:
        if self._seven_zip:
            self._status.configure(
                text=f"7-Zip engine: {self._seven_zip}",
                text_color=("#2fa36b", "#2fa36b"),
            )
        else:
            self._status.configure(
                text="7-Zip engine not found (bundle 7zz with PyInstaller or install to /usr/local/bin/7zip).",
                text_color=("#e85d5d", "#e85d5d"),
            )

    def _append_to_decrypt_log(self, text: str) -> None:
        if not text:
            return
        try:
            path = _decrypt_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._decrypt_log_lock:
                with open(path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(text)
                    f.flush()
        except OSError:
            pass

    def _append_extract_list_panel_text(self, text: str) -> None:
        """Append to the Extract-tab listing (List mode only). Never full-widget replace — that froze the UI on large streams."""
        if not self._mirror_to_extract_list_panel or not text:
            return
        self._extract_list_panel.configure(state="normal")
        self._extract_list_panel.insert("end", text)
        self._extract_list_panel.see("end")
        self._extract_list_panel.configure(state="disabled")

    def _append_log(self, line: str, *, to_file: bool = True) -> None:
        self._log.insert("end", line)
        self._log.see("end")
        if to_file:
            self._append_to_decrypt_log(line)
        if self._mirror_to_extract_list_panel:
            self._extract_list_committed += line
            self._append_extract_list_panel_text(line)

    def _report_error(self, msg: str) -> None:
        text = msg.strip()
        self._append_log(text + "\n", to_file=True)
        messagebox.showerror(APP_NAME, text, parent=self)

    def _set_progress_value(self, fraction: float) -> None:
        f = max(0.0, min(1.0, fraction))
        self._progress.set(f)
        self._pct_label.configure(text=f"{int(round(f * 100))}%")

    def _toggle_details(self) -> None:
        if self._details_expanded:
            self._log_frame.grid_remove()
            self.grid_rowconfigure(5, weight=0, minsize=0)
            self._details_expanded = False
            self._details_btn.configure(text="Show more details")
            self.geometry(f"{self._win_w}x{self._compact_h}")
            self.minsize(self._min_w, self._min_compact_h)
        else:
            self._log_frame.grid(row=5, column=0, sticky="nsew", padx=16, pady=(0, 16))
            self.grid_rowconfigure(5, weight=2, minsize=160)
            self._details_expanded = True
            self._details_btn.configure(text="Hide details")
            self.geometry(f"{self._win_w}x{self._expanded_h}")
            self.minsize(self._min_w, self._min_expanded_h)

    def _textbox(self):
        return getattr(self._log, "_textbox", None)

    def _init_live_log_mark(self) -> None:
        tb = self._textbox()
        if tb is None:
            return
        try:
            tb.mark_set(_LIVE_MARK, "end")
            tb.mark_gravity(_LIVE_MARK, "left")
        except TclError:
            pass

    def _sync_live_log_line(self) -> None:
        tb = self._textbox()
        if tb is None:
            return
        try:
            tb.delete(_LIVE_MARK, "end")
            tb.insert(_LIVE_MARK, "".join(self._stream_line))
        except TclError:
            pass
        self._log.see("end")
        # Do not mirror in-progress lines to the Extract listing: that required a full textbox
        # rebuild every character and caused macOS beachball hangs on large `7zz l` output.

    def _commit_log_line(self, line: str) -> None:
        tb = self._textbox()
        if tb is None:
            self._append_log(line)
            return
        try:
            tb.delete(_LIVE_MARK, "end")
            tb.insert(_LIVE_MARK, line)
            tb.mark_set(_LIVE_MARK, "end")
            tb.mark_gravity(_LIVE_MARK, "left")
        except TclError:
            self._append_log(line)
            return
        self._log.see("end")
        if self._mirror_to_extract_list_panel:
            self._extract_list_committed += line
            self._append_extract_list_panel_text(line)

    def _feed_stream_chunk(self, chunk: str) -> None:
        """Apply raw stdout bytes without syncing the Tk textbox per character (avoids beachball)."""
        for ch in chunk:
            if ch == "\b":
                if self._stream_line:
                    self._stream_line.pop()
            elif ch == "\r":
                self._stream_line.clear()
            elif ch == "\n":
                text = "".join(self._stream_line)
                self._stream_line.clear()
                self._commit_log_line(text + "\n")
            else:
                self._stream_line.append(ch)
        self._sync_live_log_line()

    def _flush_partial_stream_line(self) -> None:
        if not self._stream_line:
            return
        text = "".join(self._stream_line)
        self._stream_line.clear()
        self._commit_log_line(text + "\n")

    def _pick_compress_source(self) -> None:
        path = filedialog.askopenfilename(title="Select file") or filedialog.askdirectory(title="Select folder")
        if path:
            self._compress_source.delete(0, "end")
            self._compress_source.insert(0, path)

    def _pick_compress_dest(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save archive as",
            defaultextension=".7z",
            filetypes=[
                ("7-Zip archive", "*.7z"),
                ("Zip archive", "*.zip"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._compress_dest.delete(0, "end")
            self._compress_dest.insert(0, path)

    def _pick_extract_archive(self) -> None:
        path = filedialog.askopenfilename(
            title="Select archive",
            filetypes=[
                ("Archives", "*.7z *.zip *.rar *.tar *.gz *.bz2 *.xz"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._extract_archive.delete(0, "end")
            self._extract_archive.insert(0, path)

    def _pick_extract_dest(self) -> None:
        if self._extract_mode_value != "Extract":
            return
        path = filedialog.askdirectory(title="Extract to folder")
        if path:
            self._extract_dest.delete(0, "end")
            self._extract_dest.insert(0, path)

    def _level_to_mx(self) -> str:
        mapping = {"Fast": "1", "Normal": "5", "Ultra": "9"}
        return mapping.get(self._compress_level.get(), "5")

    def _busy(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _set_stop_enabled(self, enabled: bool) -> None:
        self._stop_btn.configure(state="normal" if enabled else "disabled")

    def _stop_7zz(self) -> None:
        if not self._busy() or self._proc is None:
            return
        self._user_stopped = True
        proc = self._proc
        try:
            proc.terminate()
        except OSError:
            pass

        def _kill_if_still_running() -> None:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass

        self.after(1000, _kill_if_still_running)

    def _reset_progress_ui(self) -> None:
        self._set_progress_value(0.0)
        self._log.delete("1.0", "end")
        self._stream_line.clear()
        self._append_to_decrypt_log(
            f"\n{'=' * 60}\n# log file: {_decrypt_log_path()}\n"
            f"{datetime.now().isoformat(timespec='seconds')}\n{'=' * 60}\n"
        )

    def _start_compress(self) -> None:
        if self._busy():
            messagebox.showinfo(APP_NAME, "7-Zip is already running. Use Stop or wait for it to finish.", parent=self)
            return
        if not self._seven_zip:
            self._report_error("7-Zip binary not found.")
            return
        src = self._compress_source.get().strip()
        dest = self._compress_dest.get().strip()
        if not src or not dest:
            self._report_error("Select a source and a destination archive path.")
            return
        if not Path(src).exists():
            self._report_error(f"Source does not exist:\n{src}")
            return

        cmd: list[str] = [
            self._seven_zip,
            "a",
            "-bsp1",
            "-bb1",
            f"-t{self._compress_format.get()}",
            f"-mx={self._level_to_mx()}",
        ]
        mmt = self._compress_mmt.get()
        if mmt != "Auto":
            cmd.append(f"-mmt={mmt}")
        vol = self._compress_volume.get()
        if vol != "Don't Split":
            cmd.append(f"-v{vol}")
        if self._compress_sdel.get():
            cmd.append("-sdel")
        cmd.append(dest)
        cmd.append(src)
        pwd = self._compress_password.get()
        if pwd:
            cmd.insert(cmd.index(dest), f"-p{pwd}")

        self._reset_progress_ui()
        self._run_7zz(cmd)

    def _start_extract(self) -> None:
        if self._busy():
            messagebox.showinfo(APP_NAME, "7-Zip is already running. Use Stop or wait for it to finish.", parent=self)
            return
        if not self._seven_zip:
            self._report_error("7-Zip binary not found.")
            return
        archive = self._extract_archive.get().strip()
        mode = self._extract_mode_value
        if mode not in ("Extract", "Test", "List"):
            self._report_error("Choose Extract, Test, or List.")
            return
        if not archive:
            self._report_error("Select an archive.")
            return
        if not Path(archive).is_file():
            self._report_error(f"Archive not found:\n{archive}")
            return

        if mode == "Extract":
            out = self._extract_dest.get().strip()
            if not out:
                self._report_error("Select an output folder for extraction.")
                return
            out_dir = Path(out)
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                self._seven_zip,
                "x",
                "-bsp1",
                "-bb1",
                "-y",
                f"-o{out_dir}",
                archive,
            ]
        elif mode == "Test":
            cmd = [self._seven_zip, "t", "-bsp1", "-bb1", archive]
        elif mode == "List":
            cmd = [self._seven_zip, "l", "-bsp1", "-bb1", archive]
        else:
            self._report_error("Unknown action.")
            return

        if self._extract_pwd_enabled.get():
            pwd = self._extract_password.get().strip()
            if not pwd:
                self._report_error('Enter the archive password, or uncheck "Archive has a password".')
                return
            cmd.insert(-1, f"-p{pwd}")

        if mode == "List":
            self._mirror_to_extract_list_panel = True
            self._extract_list_committed = ""
            self._extract_list_panel.configure(state="normal")
            self._extract_list_panel.delete("1.0", "end")
            self._extract_list_panel.configure(state="disabled")
        else:
            self._mirror_to_extract_list_panel = False

        self._reset_progress_ui()
        self._run_7zz(cmd)

    def _run_7zz(self, cmd: list[str]) -> None:
        self._reader_done.clear()
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=False,
                bufsize=0,
                env={**os.environ, "LANG": "C"},
                creationflags=creationflags,
            )
        except OSError as e:
            self._report_error(f"Failed to start 7-Zip:\n{e}")
            self._proc = None
            self._mirror_to_extract_list_panel = False
            return

        self._append_log("$ " + " ".join(_redact_cmd_for_log(cmd)) + "\n\n")
        self._init_live_log_mark()

        def reader() -> None:
            assert self._proc and self._proc.stdout
            line_chars: list[str] = []
            last_bar_pct: int | None = None
            last_live_display: str | None = None
            raw_log_buf: list[str] = []
            raw_queue_pending: list[str] = []
            log_path = _decrypt_log_path()

            def flush_raw_queue() -> None:
                if not raw_queue_pending:
                    return
                self._output_queue.put(("raw", "".join(raw_queue_pending)))
                raw_queue_pending.clear()

            def flush_raw_log() -> None:
                if not raw_log_buf:
                    return
                blob = "".join(raw_log_buf)
                raw_log_buf.clear()
                try:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with self._decrypt_log_lock:
                        with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
                            lf.write(blob)
                            lf.flush()
                except OSError:
                    pass

            try:
                while True:
                    chunk = self._proc.stdout.read(1)
                    if not chunk:
                        break
                    ch = chunk.decode("latin-1")
                    raw_log_buf.append(ch)
                    if ch == "\n" or len(raw_log_buf) >= 4096:
                        flush_raw_log()

                    if ch == "\n":
                        flush_raw_queue()
                        self._output_queue.put(("line", "".join(line_chars) + "\n"))
                        line_chars.clear()
                        last_bar_pct = None
                        last_live_display = None
                        continue

                    if ch == "\b":
                        if line_chars:
                            line_chars.pop()
                    elif ch == "\r":
                        line_chars.clear()
                    else:
                        line_chars.append(ch)

                    s = "".join(line_chars)
                    tail = s[-512:] if len(s) > 512 else s
                    bar_matches = list(_PCT_IN_LINE.finditer(tail))
                    if bar_matches:
                        try:
                            v = int(bar_matches[-1].group(1))
                        except ValueError:
                            v = -1
                        else:
                            v = max(0, min(100, v))
                            if last_bar_pct is None or v != last_bar_pct:
                                last_bar_pct = v
                                flush_raw_queue()
                                self._output_queue.put(("pct", v))

                    triplets = list(_TRIPLET_PROGRESS.finditer(s))
                    if triplets:
                        m = triplets[-1]
                        triplet_text = m.group(0)
                        display_text = s[: m.start()] + triplet_text
                        if last_live_display != display_text:
                            last_live_display = display_text
                            flush_raw_queue()
                            self._output_queue.put(("live", display_text))
                    else:
                        raw_queue_pending.append(ch)
                        if len(raw_queue_pending) >= _RAW_QUEUE_BATCH:
                            flush_raw_queue()
            finally:
                flush_raw_log()
                flush_raw_queue()
                if line_chars:
                    self._output_queue.put(("line", "".join(line_chars) + "\n"))
                self._output_queue.put(None)
                self._reader_done.set()

        threading.Thread(target=reader, daemon=True).start()
        self._set_stop_enabled(True)
        self._schedule_drain_queue()

    def _schedule_drain_queue(self) -> None:
        if self._poll_after_id:
            self.after_cancel(self._poll_after_id)
            self._poll_after_id = None

        def drain() -> None:
            processed = 0
            try:
                while processed < _MAX_DRAIN_BATCH:
                    item = self._output_queue.get_nowait()
                    processed += 1
                    if item is None:
                        self._flush_partial_stream_line()
                        self._finish_7zz_async()
                        return
                    kind, payload = item
                    if kind == "pct":
                        self._set_progress_value(int(payload) / 100.0)
                    elif kind == "live":
                        self._stream_line = list(str(payload))
                        self._sync_live_log_line()
                    elif kind == "line":
                        self._stream_line.clear()
                        self._commit_log_line(str(payload))
                    elif kind == "raw":
                        self._feed_stream_chunk(str(payload))
            except queue.Empty:
                pass
            self._poll_after_id = self.after(50, drain)

        self._poll_after_id = self.after(0, drain)

    def _finish_7zz_async(self) -> None:
        """Never block the UI thread on proc.wait() — that freezes macOS with the spinning cursor."""
        self._poll_after_id = None
        proc = self._proc
        if proc is None:
            self._set_stop_enabled(False)
            self._mirror_to_extract_list_panel = False
            return

        def reap_when_done() -> None:
            if proc.poll() is None:
                self._poll_after_id = self.after(50, reap_when_done)
                return
            code = proc.returncode
            stopped = self._user_stopped
            self._user_stopped = False
            self._proc = None
            self._set_stop_enabled(False)
            self._poll_after_id = None
            if stopped:
                self._append_log("\nStopped by user.\n")
                self._set_progress_value(0.0)
            elif code == 0:
                self._append_log("\nDone.\n")
                self._set_progress_value(1.0)
            else:
                self._append_log(f"\nExited with code {code}.\n")
                self._set_progress_value(0.0)
            self._mirror_to_extract_list_panel = False

        self.after(0, reap_when_done)

    def _on_close(self) -> None:
        if self._busy() and self._proc:
            try:
                self._proc.terminate()
            except OSError:
                pass
        self.destroy()


def main() -> None:
    # Ensure bundled binary is executable (PyInstaller copy may strip +x)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "7zz"
        if bundled.is_file():
            mode = bundled.stat().st_mode
            bundled.chmod(mode | 0o111)

    app = SevenZipMasterGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
