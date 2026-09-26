"""Telegram command for the persisted TMDB Popular Movies catalog."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


_WEEKLY_MODULE_PATH = Path("$HOME/hermes/customizations/weekly-movies/weekly_movies.py")
_WEEKLY_MODULE_NAME = "hermes_weekly_movies_catalog_shared"


def _weekly_module() -> ModuleType:
    loaded = sys.modules.get(_WEEKLY_MODULE_NAME)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(_WEEKLY_MODULE_NAME, _WEEKLY_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("no se pudo cargar el módulo TMDB semanal")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_WEEKLY_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _pelicula(raw: str) -> str:
    try:
        module = _weekly_module()
        return module.query_movie(raw, state_dir=module.DEFAULT_STATE_DIR)
    except Exception:
        return "No se pudo cargar el catálogo TMDB persistido."


def register(ctx) -> None:
    ctx.register_command(
        "pelicula",
        _pelicula,
        "Consulta una película TMDB",
        "",
    )
