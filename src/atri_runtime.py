"""Qbot source paths and optional isolated user state; no desktop dependency."""
from __future__ import annotations

import hashlib
import os
import sysconfig
from pathlib import Path

VERSION = "0.5.0"


def resource_root() -> Path:
    module_dir = Path(__file__).resolve().parent
    candidates = (
        module_dir.parent,
        module_dir / "share" / "atri-qq-bot",
        Path(sysconfig.get_path("data")) / "share" / "atri-qq-bot",
        Path(sysconfig.get_path("data", scheme=sysconfig.get_preferred_scheme("user")))
        / "share" / "atri-qq-bot",
    )
    for root in candidates:
        if (root / "docs" / "prompts" / "group_chat.md").is_file():
            return root
    return module_dir.parent


def user_root() -> Path:
    configured = os.environ.get("ATRI_QBOT_USER_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    source = Path(__file__).resolve().parents[1]
    if (source / "pyproject.toml").is_file():
        return source
    return Path.home() / ".atri-qq-bot"


def instance_suffix() -> str:
    if os.environ.get("ATRI_QBOT_USER_DIR"):
        return "-" + hashlib.sha256(str(user_root()).casefold().encode()).hexdigest()[:12]
    return ""
