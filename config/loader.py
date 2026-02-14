"""Robust loader for user_config.py.

Supports Python values and common lowercase JSON-like aliases:
- true/false/null
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict


def load_user_config() -> Any:
    cfg_path = Path(__file__).with_name("user_config.py")

    # Default namespace + friendly aliases for non-Python habit (true/false/null)
    ns: Dict[str, Any] = {
        "__file__": str(cfg_path),
        "__name__": "config.user_config",
        "true": True,
        "false": False,
        "null": None,
    }

    try:
        code = compile(cfg_path.read_text(encoding="utf-8"), str(cfg_path), "exec")
        exec(code, ns, ns)
    except Exception:
        # Keep runtime alive even if user file is malformed
        return SimpleNamespace()

    data = {
        k: v
        for k, v in ns.items()
        if not k.startswith("__") and k not in {"true", "false", "null"}
    }
    return SimpleNamespace(**data)
