"""GUI smoke tests — headless-safe. The window itself needs a display, so we
guard the real-widget test behind an importorskip and a $DISPLAY check."""

from __future__ import annotations

import os

import pytest


def test_gui_imports():
    # Importing the module must never require a display (tkinter is imported
    # at module level but no Tk root is built until main()).
    import keeper.gui  # noqa: F401
    assert keeper.gui.__name__ == "keeper.gui"


def test_mode_choices_map_to_engine_modes():
    from keeper.engine import MODE_DEFAULT, MODE_MODEL, MODE_RULES
    from keeper.gui import MODE_CHOICES

    keys = [k for _label, k in MODE_CHOICES]
    assert set(keys) == {MODE_DEFAULT, MODE_RULES, MODE_MODEL}


def test_themes_have_dark_and_light():
    from keeper.gui import THEMES

    assert "dark" in THEMES and "light" in THEMES
    # Both palettes expose the same color roles so theming can't KeyError.
    assert set(THEMES["dark"].keys()) == set(THEMES["light"].keys())


@pytest.mark.skipif(not os.environ.get("DISPLAY") and os.name != "nt",
                    reason="no display for Tk")
def test_window_builds_and_theme_toggles():
    import tkinter as tk

    from keeper.gui import THEMES, KeeperApp

    root = tk.Tk()
    try:
        root.withdraw()  # don't flash a window
        app = KeeperApp(root)
        assert app.theme_name in THEMES
        before = app.palette["root"]
        before_name = app.theme_name
        app._toggle_theme()
        assert app.theme_name != before_name
        assert app.palette["root"] != before  # palette actually swapped
        # Restore so the config file isn't left on the wrong theme.
        app._toggle_theme()
        assert app.theme_name == before_name
    finally:
        root.destroy()
