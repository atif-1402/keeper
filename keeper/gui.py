"""Keeper's desktop app — a thin Tkinter shell over the same engine the CLI uses.

    python -m keeper.gui            launch it
    keeper-gui                      (installed console script)

Three modes map exactly onto the CLI flags:
    Default (model + rules)   ->  keeper <folder>
    Rules only (no model)     ->  keeper <folder> --no-model
    Model only (require)      ->  keeper <folder> --model-only

Dry-run by default: Start never moves a file. "Apply quarantine" (with a
confirmation) is the only destructive-ish step, and "Restore" reverses it
byte-for-byte. Same safety rails as the command line.
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .decide import DEFAULT_MODEL
from .engine import MODE_DEFAULT, MODE_MODEL, MODE_RULES, run_pipeline
from .quarantine import QDIR, apply_quarantine, restore
from .report import fmt_bytes

# The three modes, shown label-first so the dropdown reads like English.
MODE_CHOICES = [
    ("Default (model + rules)", MODE_DEFAULT),
    ("Rules only (no model)", MODE_RULES),
    ("Model only (require Ollama)", MODE_MODEL),
]

# Dark and light, classic. Kept as flat dicts so the whole look lives here.
THEMES = {
    "dark": {
        "root": "#1c1c1e", "surface": "#2a2a2c", "raise": "#3a3a3c",
        "hover": "#48484a", "press": "#2a2a2c",
        "text": "#f2f2f7", "muted": "#8e8e93", "accent": "#0a84ff",
        "accent_hover": "#3395ff", "accent_press": "#0060df",
        "accent_fg": "#ffffff", "green": "#30d158", "yellow": "#ffd60a",
        "red": "#ff453a", "row": "#242426", "row_alt": "#2c2c2e",
        "sel": "#0a84ff", "entry": "#1c1c1e", "border": "#3a3a3c",
    },
    "light": {
        "root": "#f2f2f7", "surface": "#ffffff", "raise": "#e5e5ea",
        "hover": "#d8d8dc", "press": "#c7c7cc",
        "text": "#1c1c1e", "muted": "#6c6c70", "accent": "#007aff",
        "accent_hover": "#3395ff", "accent_press": "#0060df",
        "accent_fg": "#ffffff", "green": "#248a3d", "yellow": "#b8860b",
        "red": "#d70015", "row": "#ffffff", "row_alt": "#f7f7fa",
        "sel": "#007aff", "entry": "#ffffff", "border": "#d1d1d6",
    },
}

CONFIG_DIR = Path.home() / ".config" / "keeper"
CONFIG_FILE = CONFIG_DIR / "gui.json"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass  # a read-only home must not break the app


class KeeperApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = _load_config()
        self.theme_name = self.cfg.get("theme", "dark")
        self.palette = THEMES[self.theme_name]

        self.queue: queue.Queue = queue.Queue()
        self.result = None
        self.running = False

        root.title(f"Keeper {__version__}")
        root.geometry("860x620")
        root.minsize(720, 520)

        self._build_widgets()
        self._apply_theme()

        # Restore last folder + mode.
        last = self.cfg.get("last_folder", "")
        if last:
            self.folder_var.set(last)
        last_mode = self.cfg.get("last_mode", MODE_DEFAULT)
        for label, key in MODE_CHOICES:
            if key == last_mode:
                self.mode_var.set(label)
                break

        self.root.after(100, self._poll)

    # ------------------------------------------------------------------ build
    def _build_widgets(self) -> None:
        p = self.palette
        self.root.configure(bg=p["root"])

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        # Plain tk frames keep the bg they were born with — track them so a
        # theme toggle can recolor them (ttk widgets follow the Style).
        self._tk_rows: list[tk.Frame] = []

        def row() -> tk.Frame:
            f = tk.Frame(outer, bg=p["root"])
            self._tk_rows.append(f)
            return f

        # Title bar + theme toggle.
        top = row()
        top.pack(fill="x", pady=(0, 14))
        self.title_lbl = tk.Label(top, text="Keeper", font=("TkDefaultFont", 20, "bold"), bg=p["root"], fg=p["text"])
        self.title_lbl.pack(side="left")
        self.theme_btn = tk.Button(top, text=self._theme_btn_text(), command=self._toggle_theme,
                                   relief="flat", padx=12, pady=4,)
        self.theme_btn.pack(side="right")

        # Folder row.
        self.folder_lbl = tk.Label(outer, text="Folder", bg=p["root"], fg=p["muted"], anchor="w")
        self.folder_lbl.pack(fill="x")
        frow = row()
        frow.pack(fill="x", pady=(2, 12))
        self.folder_var = tk.StringVar()
        self.folder_entry = ttk.Entry(frow, textvariable=self.folder_var)
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.browse_btn = tk.Button(frow, text="Browse", command=self._browse, relief="flat", padx=14, pady=4,)
        self.browse_btn.pack(side="right")

        # Action row: Start on the left, the 3-mode dropdown right beside it,
        # model name tucked at the far right for power users.
        arow = row()
        arow.pack(fill="x", pady=(2, 12))
        self.start_btn = tk.Button(arow, text="Start", command=self._start,
                                   relief="flat", padx=26, pady=8,
                                   font=("TkDefaultFont", 11, "bold"))
        self.start_btn.pack(side="left")
        self.mode_var = tk.StringVar(value=MODE_CHOICES[0][0])
        self.mode_combo = ttk.Combobox(arow, textvariable=self.mode_var,
                                       values=[c[0] for c in MODE_CHOICES],
                                       state="readonly", width=24)
        self.mode_combo.pack(side="left", padx=(10, 0))
        self.model_var = tk.StringVar(value=self.cfg.get("model", DEFAULT_MODEL))
        self.model_entry = ttk.Entry(arow, textvariable=self.model_var, width=16)
        self.model_entry.pack(side="right")

        # Progress + status.
        self.pbar = ttk.Progressbar(outer, mode="determinate", length=100)
        self.pbar.pack(fill="x", pady=(6, 4))
        self.status_lbl = tk.Label(outer, text="Pick a folder and press Start. Nothing moves until you Apply.",
                                   bg=p["root"], fg=p["muted"], anchor="w")
        self.status_lbl.pack(fill="x")

        # Results table.
        cols = ("cluster", "copies", "keep", "decided", "why", "losers")
        self.tree = ttk.Treeview(outer, columns=cols, show="headings", height=12)
        for cid, heading, width, anchor in (
            ("cluster", "#", 40, "e"),
            ("copies", "Copies", 60, "e"),
            ("keep", "KEEP", 220, "w"),
            ("decided", "Decided", 110, "w"),
            ("why", "Why", 300, "w"),
            ("losers", "Losers", 160, "w"),
        ):
            self.tree.heading(cid, text=heading)
            self.tree.column(cid, width=width, anchor=anchor)
        self.tree.pack(fill="both", expand=True, pady=(14, 8))
        self.tree.tag_configure("keep", foreground=p["green"])
        self.tree.tag_configure("model", foreground=p["green"])
        self.tree.tag_configure("rule", foreground=p["yellow"])
        self.tree.tag_configure("identical", foreground=p["accent"])

        # Bottom action buttons.
        brow = row()
        brow.pack(fill="x")
        self.apply_btn = tk.Button(brow, text="Apply quarantine", command=self._apply,
                                   relief="flat", padx=16, pady=6, state="disabled",)
        self.apply_btn.pack(side="left")
        self.restore_btn = tk.Button(brow, text="Restore", command=self._restore,
                                     relief="flat", padx=16, pady=6,)
        self.restore_btn.pack(side="left", padx=(8, 0))
        self.clutter_lbl = tk.Label(brow, text="", bg=p["root"], fg=p["muted"])
        self.clutter_lbl.pack(side="right")

    # ------------------------------------------------------------------ theme
    def _hover_btn(self, btn: tk.Button, *, rest: str, hover: str) -> None:
        """Give a tk.Button a real hover state. Tk's activebackground only
        fires on press, not hover, so we bind enter/leave ourselves."""
        btn.configure(bg=rest)
        btn.bind("<Enter>", lambda e, b=btn, c=hover: b.configure(bg=c))
        btn.bind("<Leave>", lambda e, b=btn, c=rest: b.configure(bg=c))

    def _theme_btn_text(self) -> str:
        return "Light" if self.theme_name == "dark" else "Dark"

    def _apply_theme(self) -> None:
        p = self.palette
        self.root.configure(bg=p["root"])
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=p["root"], foreground=p["text"],
                        fieldbackground=p["entry"], bordercolor=p["border"],
                        lightcolor=p["raise"], darkcolor=p["surface"])
        style.configure("TFrame", background=p["root"])
        style.configure("TEntry", fieldbackground=p["entry"], foreground=p["text"],
                        insertcolor=p["text"])
        style.configure("TCombobox", fieldbackground=p["entry"], foreground=p["text"],
                        background=p["raise"], arrowcolor=p["text"])
        style.map("TCombobox", fieldbackground=[("readonly", p["entry"])],
                  foreground=[("readonly", p["text"])])

        # Buttons.
        for name in ("TButton",):
            style.configure(name, background=p["raise"], foreground=p["text"],
                            bordercolor=p["border"], focusthickness=1)
            style.map(name, background=[("active", p["raise"])])
        style.configure("Accent.TButton", background=p["accent"], foreground=p["accent_fg"])

        # Progressbar.
        style.configure("TProgressbar", troughcolor=p["surface"],
                        background=p["accent"], bordercolor=p["border"])

        # Treeview.
        style.configure("Treeview", background=p["row"], fieldbackground=p["row"],
                        foreground=p["text"], bordercolor=p["border"],
                        rowheight=28)
        style.configure("Treeview.Heading", background=p["raise"],
                        foreground=p["text"], relief="flat")
        style.map("Treeview", background=[("selected", p["sel"])],
                  foreground=[("selected", p["accent_fg"])])

        # Re-color the tk widgets we made by hand. Plain tk frames keep the
        # background they were born with, so the tracked rows get repainted
        # here too — otherwise a toggle leaves dark bars in the light theme.
        for f in getattr(self, "_tk_rows", []):
            f.configure(bg=p["root"])
        for widget, bg, fg in (
            (self.title_lbl, p["root"], p["text"]),
            (self.folder_lbl, p["root"], p["muted"]),
            (self.status_lbl, p["root"], p["muted"]),
            (self.clutter_lbl, p["root"], p["muted"]),
        ):
            widget.configure(bg=bg, fg=fg)

        for btn in (self.theme_btn, self.browse_btn, self.start_btn,
                    self.apply_btn, self.restore_btn):
            btn.configure(bg=p["raise"], fg=p["text"],
                          activebackground=p["press"], activeforeground=p["text"],
                          relief="flat", bd=0, highlightthickness=0)
            self._hover_btn(btn, rest=p["raise"], hover=p["hover"])

        # The Start button reads as the primary action.
        self.start_btn.configure(bg=p["accent"], fg=p["accent_fg"],
                                 activebackground=p["accent_press"],
                                 activeforeground=p["accent_fg"])
        self._hover_btn(self.start_btn, rest=p["accent"], hover=p["accent_hover"])

        self.tree.tag_configure("model", foreground=p["green"])
        self.tree.tag_configure("rule", foreground=p["yellow"])
        self.tree.tag_configure("identical", foreground=p["accent"])

    def _toggle_theme(self) -> None:
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.palette = THEMES[self.theme_name]
        self.theme_btn.configure(text=self._theme_btn_text())
        self._apply_theme()
        self.cfg["theme"] = self.theme_name
        _save_config(self.cfg)

    # ------------------------------------------------------------------ actions
    def _browse(self) -> None:
        chosen = filedialog.askdirectory(title="Choose a folder to scan")
        if chosen:
            self.folder_var.set(chosen)

    def _mode_key(self) -> str:
        label = self.mode_var.get()
        for lbl, key in MODE_CHOICES:
            if lbl == label:
                return key
        return MODE_DEFAULT

    def _start(self) -> None:
        if self.running:
            return
        raw = self.folder_var.get().strip()
        if not raw:
            messagebox.showwarning("Keeper", "Pick a folder first.")
            return
        root = Path(raw).expanduser()
        if not root.is_dir():
            messagebox.showerror("Keeper", f"{root} is not a folder.")
            return

        # Remember the choices.
        self.cfg["last_folder"] = str(root)
        self.cfg["last_mode"] = self._mode_key()
        self.cfg["model"] = self.model_var.get().strip() or DEFAULT_MODEL
        _save_config(self.cfg)

        self.running = True
        self.result = None
        self.start_btn.configure(state="disabled")
        self.apply_btn.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.clutter_lbl.configure(text="")
        self.pbar.configure(mode="indeterminate", value=0)
        self.pbar.start(12)
        self.status_lbl.configure(text="Working…")

        mode = self._mode_key()
        model = self.model_var.get().strip() or DEFAULT_MODEL
        threading.Thread(target=self._worker, args=(root, model, mode), daemon=True).start()

    def _worker(self, root: Path, model: str, mode: str) -> None:
        def progress(text: str, done: int, total: int) -> None:
            self.queue.put(("progress", text, done, total))
        try:
            result = run_pipeline(root, model=model, mode=mode, progress=progress)
            self.queue.put(("done", result, None))
        except Exception as exc:  # noqa: BLE001 — surface, never crash the app
            self.queue.put(("done", None, exc))

    def _poll(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, text, done, total = msg
                    self.status_lbl.configure(text=text)
                    if total and total > 0:
                        self.pbar.stop()
                        self.pbar.configure(mode="determinate",
                                            value=int(done / total * 100))
                    else:
                        self.pbar.configure(mode="indeterminate")
                        self.pbar.start(12)
                elif kind == "done":
                    _, result, exc = msg
                    self._on_done(result, exc)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_done(self, result, exc) -> None:
        self.pbar.stop()
        self.pbar.configure(value=0)
        self.running = False
        self.start_btn.configure(state="normal")

        if exc is not None:
            self.status_lbl.configure(text=f"Error: {exc}")
            messagebox.showerror("Keeper", str(exc))
            return

        self.result = result
        for i, gd in enumerate(result.decisions, start=1):
            dec = gd.decision
            keep = Path(dec.keep.rel).name
            loser_names = [Path(f.rel).name for f in gd.losers]
            losers = ", ".join(loser_names[:3])
            if len(loser_names) > 3:
                losers += f" +{len(loser_names) - 3} more"
            self.tree.insert("", "end", tags=(dec.method,),
                             values=(i, len(gd.files), keep, dec.method,
                                     dec.reason, losers))

        loser_total = result.losers
        self.clutter_lbl.configure(
            text=f"{loser_total} loser(s) · {fmt_bytes(result.clutter_bytes)} of clutter")
        if result.model_error:
            self.status_lbl.configure(text=result.model_error)
        elif result.clusters == 0:
            self.status_lbl.configure(text="No duplicate clusters found. That folder is already tidy.")
        else:
            self.status_lbl.configure(
                text=f"Found {result.clusters} cluster(s), {loser_total} loser(s). "
                     f"Dry-run — nothing was moved. Apply to quarantine.")

        if loser_total:
            self.apply_btn.configure(state="normal")

    def _apply(self) -> None:
        if self.result is None or not self.result.losers:
            return
        n = self.result.losers
        ok = messagebox.askyesno(
            "Confirm quarantine",
            f"Move {n} losing file(s) into {self.result.root / QDIR}/?\n\n"
            "Nothing is deleted — Restore (or restore.sh) puts every file back.")
        if not ok:
            return
        manifest = apply_quarantine(self.result.root, self.result.decisions)
        if manifest:
            self.status_lbl.configure(
                text=f"Quarantined {n} file(s) -> {self.result.root / QDIR}/")
            self.apply_btn.configure(state="disabled")
            messagebox.showinfo("Keeper",
                                f"Quarantined {n} file(s).\nUndo with Restore.")

    def _restore(self) -> None:
        raw = self.folder_var.get().strip()
        if not raw:
            return
        root = Path(raw).expanduser()
        restored, error = restore(root)
        if error and restored == 0:
            messagebox.showerror("Keeper", error)
            return
        msg = f"Restored {restored} file(s)."
        if error:
            msg += f"\nNote: {error}"
        self.status_lbl.configure(text=msg)
        messagebox.showinfo("Keeper", msg)


def main() -> int:
    root = tk.Tk()
    KeeperApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
