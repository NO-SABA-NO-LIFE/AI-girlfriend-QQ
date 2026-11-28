"""QQ 成品歌曲清单与文件导入。"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from atri_qq_bot.music import save_song
from atri_webui import server as _server

from ..upload_parser import multipart_file, parse_multipart_form


class HandlerQQMusicMixin:
    def _songs_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "root": str(_server.MUSIC_ROOT),
            "songs": [
                {"title": song.title, "name": song.path.name, "size": song.path.stat().st_size}
                for song in _server.song_catalog()
            ],
        }

    def _handle_song_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0 or content_length > _server.MAX_COMPLETE_MUSIC_UPLOAD_BYTES + 200_000:
            return self._send_error(HTTPStatus.BAD_REQUEST, "完整音乐最大 180MB")
        try:
            form = parse_multipart_form(
                self.headers.get("Content-Type", ""), self.rfile.read(content_length)
            )
            file_item = multipart_file(form, "file")
            if file_item is None or not file_item.filename:
                raise ValueError("请选择完整歌曲文件")
            if len(file_item.data) > _server.MAX_COMPLETE_MUSIC_UPLOAD_BYTES:
                raise ValueError("完整音乐最大 180MB")
            path = save_song(str(file_item.filename), file_item.data)
        except Exception as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        self._send_json({"ok": True, "path": str(path), "name": path.name})
