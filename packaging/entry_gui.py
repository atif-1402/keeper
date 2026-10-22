"""Single-line GUI entry point for PyInstaller (see build.sh / release.yml).

In a frozen (onefile) build the Tcl/Tk runtime libs and their init-script
directories don't sit where Tk expects them, so we point TCL_LIBRARY /
TK_LIBRARY and the loader at the bundled copies before tkinter is imported.
On a normal install this block is a no-op.
"""

import os
import sys
from pathlib import Path


def _prep_tcl_tk() -> None:
    if not getattr(sys, "frozen", False):
        return  # running from source — Tk finds its own libs
    base = Path(getattr(sys, "_MEIPASS", Path(sys.argv[0]).parent))
    for env, name in (("TCL_LIBRARY", "tcl9.0"), ("TK_LIBRARY", "tk9.0")):
        d = base / name
        if d.is_dir():
            os.environ.setdefault(env, str(d))
    # Make the bundled shared libs visible to the dynamic linker.
    for so_dir in (base, base / "lib"):
        if so_dir.is_dir():
            existing = os.environ.get("LD_LIBRARY_PATH", "")
            os.environ["LD_LIBRARY_PATH"] = (
                f"{so_dir}:{existing}" if existing else str(so_dir)
            )


_prep_tcl_tk()

from keeper.gui import main  # noqa: E402

raise SystemExit(main())
