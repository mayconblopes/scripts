#!/usr/bin/env python3
"""Compatibilidade: encaminha execuções antigas ao programa unificado."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clipboard_sync import *  # noqa: E402,F403
from clipboard_sync import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
