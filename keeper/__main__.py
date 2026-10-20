"""Allows `python -m keeper <folder>` without installing the entry point."""

from .cli import main

raise SystemExit(main())
