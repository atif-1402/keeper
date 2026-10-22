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
import sys
import threading
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
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

# Dark and light, tuned for a clean modern flat look: one restrained accent,
# real elevation (root -> surface -> card), and high-contrast text. Kept as
# flat dicts so the whole look lives in one place.
THEMES = {
    "dark": {
        "root": "#0f1116", "surface": "#171b22", "card": "#1c212b",
        "raise": "#252b36", "hover": "#2f3742", "press": "#1a1f27",
        "text": "#e8ebf2", "muted": "#8b94a6", "faint": "#5a6273",
        "accent": "#4f8cff", "accent_hover": "#6ba0ff", "accent_press": "#3b74e6",
        "accent_fg": "#ffffff", "green": "#3fb950", "yellow": "#d4a72c",
        "red": "#f85149", "row": "#1c212b", "row_alt": "#20262f",
        "sel": "#2d4373", "entry": "#12161d", "border": "#2a313c",
        "focus": "#4f8cff", "danger": "#2a1518", "danger_fg": "#f85149",
    },
    "light": {
        "root": "#f6f7f9", "surface": "#eceef2", "card": "#ffffff",
        "raise": "#e7eaef", "hover": "#dbe0e7", "press": "#ccd2db",
        "text": "#12151c", "muted": "#626a79", "faint": "#99a1b0",
        "accent": "#2f6fe4", "accent_hover": "#4a83ee", "accent_press": "#2559c4",
        "accent_fg": "#ffffff", "green": "#1a7f37", "yellow": "#9a6700",
        "red": "#cf222e", "row": "#ffffff", "row_alt": "#f4f6f9",
        "sel": "#cfe0ff", "entry": "#ffffff", "border": "#e0e4ea",
        "focus": "#2f6fe4", "danger": "#fdeced", "danger_fg": "#cf222e",
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
        root.geometry("940x720")
        root.minsize(760, 560)
        # On X11/Wayland a window that doesn't request a cursor explicitly can
        # be handed a blank one, which makes the pointer seem to vanish over
        # the app. Request the standard pointer by name. Use "left_ptr" (the
        # same glyph the desktop uses), NOT "arrow" — Tk's "arrow" maps to a
        # different X11 glyph (top_left_arrow) that renders mirrored on many
        # cursor themes. Windows/macOS render the correct pointer by default,
        # so we leave those alone.
        if sys.platform.startswith("linux"):
            root.configure(cursor="left_ptr")

        self._setup_fonts()
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
    def _setup_fonts(self) -> None:
        """A small, consistent type scale on the platform's default UI font.

        Using the default family means it reads as native on every OS
        (Segoe UI on Windows, SF/Helvetica on macOS, DejaVu on Linux)
        instead of forcing a font some systems won't have.
        """
        base = tkfont.nametofont("TkDefaultFont")
        family = base.cget("family")

        def f(size: int, weight: str = "normal") -> tkfont.Font:
            return tkfont.Font(family=family, size=size, weight=weight)

        self.fonts = {
            "title": f(22, "bold"),
            "subtitle": f(11),
            "label": f(9, "bold"),     # small caps-ish section labels
            "body": f(11),
            "button": f(11),
            "button_big": f(12, "bold"),
            "caption": f(10),
            "tree": f(10),
            "tree_head": f(10, "bold"),
        }

    def _build_widgets(self) -> None:
        p = self.palette
        self.root.configure(bg=p["root"])

        # Tk frames/labels keep the bg they're born with, so track every one
        # with its color role — a theme toggle repaints them from here.
        self._themable: list[tuple[tk.Widget, str, str]] = []

        def track(w: tk.Widget, bg: str = "root", fg: str = "text") -> tk.Widget:
            # Frames only take bg; labels take bg+fg. Set fg only when the
            # widget actually supports it (has a foreground option).
            self._themable.append((w, bg, fg))
            if "foreground" in w.keys():
                w.configure(bg=p[bg], fg=p[fg])
            else:
                w.configure(bg=p[bg])
            return w

        def section(parent: tk.Widget, text: str) -> tk.Label:
            # Small, quiet, uppercase section label — the modern look.
            lbl = tk.Label(parent, text=text.upper(), font=self.fonts["label"],
                           anchor="w", padx=1)
            return track(lbl, "root", "faint")

        outer = ttk.Frame(self.root, padding=(22, 18, 22, 16))
        outer.pack(fill="both", expand=True)

        # ---- header bar: name + tagline left, theme toggle right ----
        header = track(tk.Frame(outer))
        header.pack(fill="x", pady=(0, 4))
        title_box = track(tk.Frame(header))
        title_box.pack(side="left")
        self.title_lbl = tk.Label(title_box, text="Keeper",
                                  font=self.fonts["title"], anchor="w")
        track(self.title_lbl, "root", "text").pack(anchor="w")
        tagline = tk.Label(title_box, text="Which copy do I keep?",
                           font=self.fonts["subtitle"], anchor="w")
        track(tagline, "root", "muted").pack(anchor="w")
        self.theme_btn = ttk.Button(header, text=self._theme_btn_text(),
                                    command=self._toggle_theme)
        self.theme_btn.pack(side="right")

        # A hairline under the header.
        track(tk.Frame(outer, height=1), "border", "border").pack(
            fill="x", pady=(14, 18))

        # ---- folder ----
        section(outer, "Folder").pack(fill="x", pady=(0, 6))
        frow = track(tk.Frame(outer))
        frow.pack(fill="x", pady=(0, 18))
        self.folder_var = tk.StringVar()
        self.folder_entry = ttk.Entry(frow, textvariable=self.folder_var,
                                      font=self.fonts["body"])
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.browse_btn = ttk.Button(frow, text="Browse", command=self._browse)
        self.browse_btn.pack(side="right")

        # ---- run controls: Start (primary) + mode + model ----
        section(outer, "Scan").pack(fill="x", pady=(0, 6))
        arow = track(tk.Frame(outer))
        arow.pack(fill="x", pady=(0, 18))
        self.start_btn = ttk.Button(arow, text="Start", command=self._start,
                                    style="Accent.TButton")
        self.start_btn.pack(side="left")
        self.mode_var = tk.StringVar(value=MODE_CHOICES[0][0])
        self.mode_combo = ttk.Combobox(arow, textvariable=self.mode_var,
                                       values=[c[0] for c in MODE_CHOICES],
                                       state="readonly", width=24,
                                       font=self.fonts["body"])
        self.mode_combo.pack(side="left", padx=(12, 0))
        self.model_var = tk.StringVar(value=self.cfg.get("model", DEFAULT_MODEL))
        self.model_entry = ttk.Entry(arow, textvariable=self.model_var, width=16,
                                     font=self.fonts["body"])
        self.model_entry.pack(side="right")

        # ---- progress + status ----
        self.pbar = ttk.Progressbar(outer, mode="determinate", length=100)
        self.pbar.pack(fill="x", pady=(0, 8))
        self.status_lbl = tk.Label(
            outer, text="Pick a folder and press Start. Nothing moves until you Apply.",
            font=self.fonts["caption"], anchor="w")
        track(self.status_lbl, "root", "muted").pack(fill="x", pady=(0, 12))

        # ---- results, in a raised card ----
        self.card = track(tk.Frame(outer), "card", "card")
        self.card.pack(fill="both", expand=True)
        self.card.configure(highlightbackground=p["border"],
                            highlightthickness=1, highlightcolor=p["border"])
        cols = ("cluster", "copies", "keep", "decided", "why", "losers")
        # A modest row count keeps the table's *minimum* size small so the
        # Apply/Restore buttons below stay on screen even in a short window;
        # the table then grows to fill whatever space is available. The font
        # comes from the Style, not the constructor.
        self.tree = ttk.Treeview(self.card, columns=cols, show="headings",
                                 height=8)
        for cid, heading, width, anchor in (
            ("cluster", "#", 40, "e"),
            ("copies", "Copies", 60, "e"),
            ("keep", "KEEP", 220, "w"),
            ("decided", "Decided", 110, "w"),
            ("why", "Why", 300, "w"),
            ("losers", "Losers", 160, "w"),
        ):
            self.tree.heading(cid, text=heading, anchor=anchor)
            self.tree.column(cid, width=width, anchor=anchor)
        self.tree.pack(fill="both", expand=True, padx=1, pady=1)

        # ---- bottom actions ----
        brow = track(tk.Frame(outer))
        brow.pack(fill="x", pady=(16, 0))
        self.apply_btn = ttk.Button(brow, text="Apply quarantine", command=self._apply,
                                    state="disabled")
        self.apply_btn.pack(side="left")
        self.restore_btn = ttk.Button(brow, text="Restore", command=self._restore)
        self.restore_btn.pack(side="left", padx=(10, 0))
        self.clutter_lbl = tk.Label(brow, text="", font=self.fonts["caption"],
                                    anchor="e")
        track(self.clutter_lbl, "root", "muted").pack(side="right")

    # ------------------------------------------------------------------ theme
    def _theme_btn_text(self) -> str:
        return "Light" if self.theme_name == "dark" else "Dark"

    def _use_ttk_theme(self, style: ttk.Style) -> bool:
        """Pick the ttk theme. On Windows in light mode we use the native
        theme (vista/winnative/xpnative) so the app adapts to the OS: Win10
        renders boxy, Win11 rounded — the classic adaptive Windows look,
        driven by the OS's own theming engine. Dark mode has no native
        equivalent, and neither does Linux/macOS, so those fall back to
        clam with our own flat colors. Returns True when the caller should
        paint ttk widgets with the clam palette.
        """
        if sys.platform == "win32" and self.theme_name == "light":
            for name in ("vista", "winnative", "xpnative"):
                if name in style.theme_names():
                    style.theme_use(name)
                    return False
        style.theme_use("clam")
        return True

    def _apply_theme(self) -> None:
        p = self.palette
        self.root.configure(bg=p["root"])
        style = ttk.Style(self.root)
        using_clam = self._use_ttk_theme(style)

        if using_clam:
            # Flat palette for dark mode and non-Windows platforms.
            style.configure(".", background=p["root"], foreground=p["text"],
                            fieldbackground=p["entry"], bordercolor=p["border"],
                            lightcolor=p["raise"], darkcolor=p["surface"])
            style.configure("TFrame", background=p["root"])
            style.configure("TEntry", fieldbackground=p["entry"], foreground=p["text"],
                            insertcolor=p["text"], bordercolor=p["border"], borderwidth=1,
                            font=self.fonts["body"])
            style.map("TEntry", bordercolor=[("focus", p["focus"])])
            style.configure("TCombobox", fieldbackground=p["entry"], foreground=p["text"],
                            background=p["raise"], arrowcolor=p["muted"],
                            bordercolor=p["border"], borderwidth=1,
                            postoffset=0, font=self.fonts["body"])
            style.map("TCombobox",
                      fieldbackground=[("readonly", p["entry"])],
                      foreground=[("readonly", p["text"])],
                      bordercolor=[("focus", p["focus"])])

            # Buttons. clam draws hover from the "active" state and the press
            # from "pressed" — give each a distinct shade so feedback reads.
            # Secondary/tertiary actions (Browse, theme, Restore, Apply).
            style.configure("TButton", background=p["raise"], foreground=p["text"],
                            bordercolor=p["border"], focusthickness=1, borderwidth=1,
                            padding=(14, 7), font=self.fonts["button"])
            style.map("TButton",
                      background=[("active", p["hover"]), ("pressed", p["press"]),
                                  ("disabled", p["surface"])],
                      foreground=[("active", p["text"]), ("disabled", p["faint"])],
                      bordercolor=[("focus", p["focus"]),
                                   ("disabled", p["border"])])
            # Primary action (Start) — the one accent button.
            style.configure("Accent.TButton", background=p["accent"],
                            foreground=p["accent_fg"], padding=(26, 9),
                            font=self.fonts["button_big"], borderwidth=0)
            style.map("Accent.TButton",
                      background=[("active", p["accent_hover"]),
                                  ("pressed", p["accent_press"]),
                                  ("disabled", p["surface"])],
                      foreground=[("active", p["accent_fg"]),
                                  ("disabled", p["faint"])])

            # Progressbar — slim, accent fill on a subtle trough.
            style.configure("TProgressbar", troughcolor=p["surface"],
                            background=p["accent"], bordercolor=p["surface"],
                            lightcolor=p["accent"], darkcolor=p["accent"],
                            borderwidth=0, thickness=6)

            # Treeview — sits in the card; use card bg with subtle striping.
            style.configure("Treeview", background=p["card"],
                            fieldbackground=p["card"], foreground=p["text"],
                            bordercolor=p["border"], borderwidth=0,
                            font=self.fonts["tree"])
            style.configure("Treeview.Heading", background=p["surface"],
                            foreground=p["muted"], relief="flat", anchor="w",
                            padding=(8, 8), font=self.fonts["tree_head"])
            style.map("Treeview",
                      background=[("selected", p["sel"])],
                      foreground=[("selected", p["text"])])
            style.map("Treeview.Heading", background=[("active", p["surface"])])
        # Row height is layout, not color — set it either way so the
        # decided-by tags line up on every theme.
        style.configure("Treeview", rowheight=30)

        # Repaint every tracked tk widget from its color role. Plain tk
        # frames/labels keep the bg they were born with, so a theme toggle
        # would otherwise leave stale bars. Frames have no foreground.
        for widget, bg_role, fg_role in getattr(self, "_themable", []):
            if "foreground" in widget.keys():
                widget.configure(bg=p[bg_role], fg=p[fg_role])
            else:
                widget.configure(bg=p[bg_role])
        # The results card also carries a border highlight.
        self.card.configure(highlightbackground=p["border"],
                            highlightcolor=p["border"])

        # Row striping + decided-by colors for the results table.
        self.tree.tag_configure("model", foreground=p["green"])
        self.tree.tag_configure("rule", foreground=p["yellow"])
        self.tree.tag_configure("identical", foreground=p["accent"])
        self.tree.tag_configure("odd", background=p["card"])
        self.tree.tag_configure("even", background=p["row_alt"])

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
            # decided-by color tag + alternating row background for striping.
            stripe = "even" if i % 2 == 0 else "odd"
            self.tree.insert("", "end", tags=(dec.method, stripe),
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
