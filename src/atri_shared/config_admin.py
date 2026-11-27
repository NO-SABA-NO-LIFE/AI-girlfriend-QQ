from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from atri_qq_bot.config import BotConfig
from atri_qq_bot.runtime import ENV_PATH


# Serializes every configuration-mutating transaction (validate -> snapshot ->
# write -> reload -> rollback) across WebUI worker threads. All WebUI entry
# points that touch .env (config save, model profile switch, voice save, voice
# port) take this lock, so a failed request cannot undo another request's
# successful model switch or overwrite it with an older snapshot. An RLock
# because a request's own rollback runs inside the same critical section.
CONFIG_UPDATE_LOCK = threading.RLock()


CONFIG_FIELDS: dict[str, str] = {
    "OPENAI_API_KEY": "openai_api_key",
    "OPENAI_BASE_URL": "openai_base_url",
    "OPENAI_MODEL": "openai_model",
    "TEMPERATURE": "temperature",
    "FREQUENCY_PENALTY": "frequency_penalty",
    "MAX_TOKENS": "max_tokens",
    "REPLY_MODE": "reply_mode",
    "MESSAGE_SPLIT_MAX_CHARS": "message_split_max_chars",
    "MESSAGE_SPLIT_MAX_PARTS": "message_split_max_parts",
    "MESSAGE_SEND_DELAY_MIN": "message_send_delay_min",
    "MESSAGE_SEND_DELAY_MAX": "message_send_delay_max",
    "TYPING_CHARS_PER_SECOND": "typing_chars_per_second",
    "TYPING_MAX_DELAY_SECONDS": "typing_max_delay_seconds",
    "REPLY_FIRST_DELAY_MIN": "reply_first_delay_min",
    "REPLY_FIRST_DELAY_MAX": "reply_first_delay_max",
    "STICKER_CHANCE": "sticker_chance",
    "STICKER_COOLDOWN_SECONDS": "sticker_cooldown_seconds",
    "MEMORY_RETRIEVAL_ENABLED": "memory_retrieval_enabled",
    "MEMORY_RETRIEVAL_PATH": "memory_retrieval_path",
    "MEMORY_RETRIEVAL_MAX_CHARS": "memory_retrieval_max_chars",
    "MEMORY_RETRIEVAL_LORE_LIMIT": "memory_retrieval_lore_limit",
    "MEMORY_RETRIEVAL_MEMORY_LIMIT": "memory_retrieval_memory_limit",
    "MEMORY_VECTOR_ENABLED": "memory_vector_enabled",
    "MEMORY_EMBEDDING_MODEL": "memory_embedding_model",
    "MEMORY_EMBEDDING_BASE_URL": "memory_embedding_base_url",
    "MEMORY_EMBEDDING_TIMEOUT_SECONDS": "memory_embedding_timeout_seconds",
    "MEMORY_EXTRACTION_ENABLED": "memory_extraction_enabled",
    "MEMORY_EXTRACTION_LLM_ENABLED": "memory_extraction_llm_enabled",
    "MEMORY_EXTRACTION_MODEL": "memory_extraction_model",
    "MEMORY_EXTRACTION_BASE_URL": "memory_extraction_base_url",
    "MEMORY_EXTRACTION_API_KEY": "memory_extraction_api_key",
    "PROACTIVE_V2_ENABLED": "proactive_v2_enabled",
    "IDLE_PROACTIVE_ENABLED": "idle_proactive_enabled",
    "IDLE_MINUTES": "idle_minutes",
    "IDLE_COOLDOWN_MINUTES": "idle_cooldown_minutes",
    "GROUP_PROACTIVE_ENABLED": "group_proactive_enabled",
    "GROUP_PROACTIVE_IDLE_MINUTES": "group_proactive_idle_minutes",
    "GROUP_PROACTIVE_COOLDOWN_MINUTES": "group_proactive_cooldown_minutes",
    "GROUP_PROACTIVE_DAILY_LIMIT": "group_proactive_daily_limit",
    "GROUP_PROACTIVE_MAX_SILENCE_DAYS": "group_proactive_max_silence_days",
    "MORNING_GREETING_ENABLED": "morning_greeting_enabled",
    "MORNING_GREETING_TIME": "morning_greeting_time",
    "TOOLBOX_VISION_ENABLED": "toolbox_vision_enabled",
    "TOOLBOX_VISION_MODEL": "toolbox_vision_model",
    "TOOLBOX_VISION_FALLBACK_MODEL": "toolbox_vision_fallback_model",
    "TOOLBOX_VISION_BASE_URL": "toolbox_vision_base_url",
    "TOOLBOX_VISION_FALLBACK_BASE_URL": "toolbox_vision_fallback_base_url",
    "TOOLBOX_VISION_API_KEY": "toolbox_vision_api_key",
    "TOOLBOX_VISION_RETRY_COUNT": "toolbox_vision_retry_count",
    "TOOLBOX_VISION_RESOURCE_WAIT_SECONDS": "toolbox_vision_resource_wait_seconds",
    "TOOLBOX_VISION_UNLOAD_OTHER_OLLAMA_MODELS": "toolbox_vision_unload_other_ollama_models",
    "TOOLBOX_OCR_ENABLED": "toolbox_ocr_enabled",
    "TOOLBOX_VIDEO_FRAME_ANALYSIS_ENABLED": "toolbox_video_frame_analysis_enabled",
    "TOOLBOX_VIDEO_MAX_FRAMES": "toolbox_video_max_frames",
    "TOOLBOX_VIDEO_DEADLINE_SECONDS": "toolbox_video_deadline_seconds",
    "WEB_SEARCH_ENABLED": "web_search_enabled",
    "WEB_SEARCH_TIMEOUT_SECONDS": "web_search_timeout_seconds",
    "WEB_SEARCH_MAX_RESULTS": "web_search_max_results",
    "TAVILY_API_KEY": "tavily_api_key",
    "SERPAPI_API_KEY": "serpapi_api_key",
    "ATRI_SEARXNG_INSTANCES": "searxng_instances",
    "VOICE_ASR_ENABLED": "voice_asr_enabled",
    "VOICE_TTS_ENABLED": "voice_tts_enabled",
    "VOICE_SERVICE_URL": "voice_service_url",
    "VOICE_SERVICE_TIMEOUT_SECONDS": "voice_service_timeout_seconds",
    "VOICE_PROFILE": "voice_profile",
    "VOICE_GROUP_ENABLED": "voice_group_enabled",
    "VOICE_REPLY_TO_VOICE": "voice_reply_to_voice",
    "VOICE_MAX_CHARS": "voice_max_chars",
    "VOICE_COOLDOWN_SECONDS": "voice_cooldown_seconds",
    "VOICE_INPUT_MAX_BYTES": "voice_input_max_bytes",
    "WEBUI_ENABLED": "webui_enabled",
    "WEBUI_PORT": "webui_port",
}

SECRET_KEYS = {"OPENAI_API_KEY", "TOOLBOX_VISION_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY", "MEMORY_EXTRACTION_API_KEY"}

def config_payload(config: BotConfig) -> dict[str, Any]:
    result: dict[str, Any] = {}
    env = read_env()
    for key, attr in CONFIG_FIELDS.items():
        value = getattr(config, attr)
        if isinstance(value, Path):
            value = str(value)
        raw = env.get(key, "")
        result[key] = {
            "value": mask_secret(str(value or "")) if key in SECRET_KEYS else value,
            "raw": mask_secret(raw) if key in SECRET_KEYS else raw,
            "has_secret": bool(raw) if key in SECRET_KEYS else False,
        }
    return result


def merge_env(changes: Mapping[str, Any]) -> dict[str, str]:
    """Compute the merged env for whitelisted changes without touching disk.

    Pure function: callers can validate the resulting configuration *before*
    persisting it, so an invalid value can never be written into .env
    (fail-closed).
    """
    current = read_env()
    allowed = set(CONFIG_FIELDS)
    for key, value in changes.items():
        if key not in allowed:
            continue
        if key in SECRET_KEYS and (
            str(value).strip() == "" or "*" in str(value)
        ):
            continue
        current[key] = normalize_env_value(value)
    return current


def update_env(changes: Mapping[str, Any]) -> None:
    """Persist whitelisted configuration changes after they are validated."""
    write_env(merge_env(changes))


def validate_env_isolated(
    changes: Mapping[str, Any], *, timeout: float = 30.0
) -> bytes:
    """Validate candidate config in a separate process without side effects.

    The candidate is serialized to a same-directory temp file and checked by an
    isolated interpreter, so the WebUI process os.environ is never touched (a
    load_dotenv override cannot leak back) and dotenv quoting is decoded by the
    real loader. The temp file is permission-restricted and always removed.
    """
    candidate = _render_env(merge_env(changes), ENV_PATH)
    # Unique per transaction: concurrent requests must never read or overwrite
    # each other's candidate file, otherwise a valid request could be rejected
    # against another request's invalid values (or an invalid one pass against
    # a deleted file). The atomic writer creates this file with mode 0600.
    temp_path = ENV_PATH.with_name(
        f".{ENV_PATH.name}.candidate.{uuid.uuid4().hex}.tmp"
    )
    try:
        _atomic_write_bytes(temp_path, candidate)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "atri_qq_bot.config.validate_file",
                str(temp_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ValueError(detail or "invalid configuration")
        return candidate
    finally:
        with contextlib.suppress(OSError):
            temp_path.unlink()


def read_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if not ENV_PATH.exists():
        return result
    for line in ENV_PATH.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def write_env(
    values: Mapping[str, str],
    *,
    extra_keys: tuple[str, ...] = (),
    target_path: Path | None = None,
) -> None:
    target = ENV_PATH if target_path is None else target_path
    _atomic_write_bytes(target, _render_env(values, target, extra_keys=extra_keys))


def write_validated_env(data: bytes) -> None:
    """Publish the exact bytes returned by isolated validation."""
    _atomic_write_bytes(ENV_PATH, data)


def _render_env(
    values: Mapping[str, str], target: Path, *, extra_keys: tuple[str, ...] = ()
) -> bytes:
    existing_lines: list[str] = []
    if target.exists():
        existing_lines = target.read_text(encoding="utf-8-sig", errors="replace").splitlines()

    seen: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            new_lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            new_lines.append(line)

    for key in (*CONFIG_FIELDS, *extra_keys):
        if key in values and key not in seen:
            new_lines.append(f"{key}={values[key]}")
    return ("\n".join(new_lines).rstrip() + "\n").encode("utf-8")


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Replace ``target`` atomically while preserving its permission bits.

    The temp file is created with mode 0600 because it may hold API keys; if
    the target already exists, its permission bits are copied onto the temp
    file before the rename, while a brand-new file stays 0600. This avoids the
    default umask (commonly 022) widening a 0600 .env to 0644 on replace.
    """
    if target.exists():
        target_mode = stat.S_IMODE(target.stat().st_mode)
    else:
        target_mode = 0o600
    tmp = target.with_name(
        f".{target.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}"
    )
    try:
        # O_EXCL with 0600: the secret-bearing file is restricted from creation.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # The buffered stream finishes partial writes or raises on write/close.
        # Only publish the file after the complete stream was closed successfully.
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(tmp, target_mode)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def snapshot_process_env(keys: Iterable[str]) -> dict[str, str | None]:
    """Capture each key's current value and presence in the WebUI process."""
    return {str(key): os.environ.get(str(key)) for key in keys}


def restore_process_env(snapshot: Mapping[str, str | None]) -> None:
    """Restore process env to the captured values and presence state.

    A key that was absent before the save is removed instead of being left with
    the newly applied value, because load_dotenv sets keys but never deletes
    them on its own.
    """
    for key, previous in snapshot.items():
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def restore_env_file(previous_bytes: bytes | None) -> None:
    """Atomically restore the previous .env, or remove it if it did not exist."""
    if previous_bytes is None:
        if ENV_PATH.exists():
            ENV_PATH.unlink()
    else:
        _atomic_write_bytes(ENV_PATH, previous_bytes)


def normalize_env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if value == "ollama":
        return value
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}****{value[-4:]}"

