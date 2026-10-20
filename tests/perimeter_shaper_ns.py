"""The module globals ``_build_perimeter_binding`` closes over, for the tests
that lift the shaper out of ``__init__.py`` via AST.

Loaded from the REAL ``const.py`` and ``_perimeter.py`` (neither has a
package-relative import), so the lifted shaper validates against the
treatment list, aliases and id cap that ship — a hand-copied tuple here is
how the legacy five treatments outlived the firmware trim in the tests.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "deckhand"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"deckhand_{name}_for_tests", COMPONENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


const = _load("const")
perimeter = _load("_perimeter")
dial_text = _load("dial_text")


class ShaperValidationError(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args or (kwargs.get("translation_key"),))
        self.translation_key = kwargs.get("translation_key")


def shaper_namespace() -> dict[str, Any]:
    return {
        "Any": Any,
        "_LOGGER": logging.getLogger("test"),
        "DOMAIN": const.DOMAIN,
        "ServiceValidationError": ShaperValidationError,
        "PERIMETER_TREATMENTS": const.PERIMETER_TREATMENTS,
        "PERIMETER_TREATMENT_ALIASES": const.PERIMETER_TREATMENT_ALIASES,
        "PERIMETER_ID_MAX": const.PERIMETER_ID_MAX,
        "PERIMETER_DEFAULT_ACTIVE_STATE": const.PERIMETER_DEFAULT_ACTIVE_STATE,
        "perimeter_normalize_treatment": perimeter.normalize_treatment,
        "humanize_entity_id": dial_text.humanize_entity_id,
    }
