"""本地成品歌曲清单与导入；每首歌曲就是一个音频文件。"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..runtime.paths import MUSIC_ROOT


AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".opus"}
NON_SONG_ARTIFACT_NAMES = {"execute", "function", "tool", "assistant", "none", "null", "unknown"}
_song_import_lock = threading.Lock()


@dataclass(frozen=True)
class Song:
    title: str
    path: Path


def song_catalog(root: Path = MUSIC_ROOT) -> list[Song]:
    """只列出 data/music 直属的成品音频，不读子目录或音频内容。"""
    if not root.is_dir():
        return []
    return [
        Song(path.stem.strip(), path.resolve())
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
        and path.stem.strip().casefold() not in NON_SONG_ARTIFACT_NAMES
    ]


def save_song(filename: str, data: bytes, *, root: Path = MUSIC_ROOT) -> Path:
    """WebUI 导入成品；保留同名文件，写完后才加入歌单。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        raise ValueError("仅支持 WAV、FLAC、MP3、OGG、M4A、AAC 或 OPUS")
    if not data:
        raise ValueError("歌曲文件为空")
    stem = re.sub(r'[\\/:*?"<>|]+', "_", Path(filename).stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._")[:80]
    if not stem or stem.casefold() in {"con", "prn", "aux", "nul"}:
        raise ValueError("歌曲名称不能为空或为 Windows 保留名称")
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".{uuid.uuid4().hex}.uploading"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
        with _song_import_lock:
            target = root / f"{stem}{suffix}"
            counter = 2
            while target.exists() or target.is_symlink():
                target = root / f"{stem}_{counter}{suffix}"
                counter += 1
            temporary.replace(target)
        return target.resolve()
    finally:
        temporary.unlink(missing_ok=True)
