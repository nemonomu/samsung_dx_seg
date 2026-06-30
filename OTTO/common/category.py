"""Resolve the active OTTO category (tv/ref/ldy) and load its config module.

Category is chosen via the OTTO_CATEGORY env var (default "tv"). Each category
package exposes a config module with the category-specific constants/functions.
"""
from __future__ import annotations

import importlib
import os
from types import ModuleType


def active_category() -> str:
    return (os.getenv("OTTO_CATEGORY") or "tv").strip().lower()


def load_config(category: str | None = None) -> ModuleType:
    cat = (category or active_category())
    return importlib.import_module(f"{cat}.config")
