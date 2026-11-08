"""Sticker gallery web server.

Serves the local sticker library as a simple browser gallery grouped by
emotion. Uses only the Python standard library and binds to all interfaces so
it can be opened from the user's own browser instead of the VNC desktop.

Usage:
    python deploy/sticker_gallery.py
    python deploy/sticker_gallery.py --port 8788
    python deploy/sticker_gallery.py --dir /opt/atri-qq-bot/data/stickers
Then open http://SERVER_IP:8788 in a local browser.
"""
from __future__ import annotations

import argparse
import html
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# Allow running against source without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

IMAGE_SUFFIXES = {".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _default_sticker_dir() -> Path:
    try:
        from atri_qq_bot.config.loader import load_config

        return Path(load_config().sticker_dir)
    except Exception:
        return Path(__file__).resolve().parents[1] / "data" / "stickers"


class GalleryHandler(BaseHTTPRequestHandler):
    # Subclasses set these on the server object; keep defaults for typing.
    sticker_dir = Path(".")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Keep the console quiet; one line per request is enough.
        sys.stderr.write("[gallery] %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_gallery()
        elif parsed.path == "/img":
            self._send_image(parse_qs(parsed.query))
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _safe_image(self, query: dict[str, list[str]]) -> Path | None:
        rel = (query.get("path") or [""])[0]
        if not rel:
            return None
        root = self.server.sticker_dir.resolve()
        target = (root / rel).resolve()
        # Block any path traversal outside the sticker root.
        try:
            target.relative_to(root)
        except (OSError, ValueError):
            return None
        if not target.is_file() or target.suffix.lower() not in IMAGE_SUFFIXES:
            return None
        return target

    def _send_image(self, query: dict[str, list[str]]) -> None:
        target = self._safe_image(query)
        if target is None:
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def _collect_groups(self) -> dict[str, list[Path]]:
        root = self.server.sticker_dir
        groups: dict[str, list[Path]] = {}
        if not root.exists():
            return groups
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            # Group by the first subdirectory under the root.
            rel_parts = path.relative_to(root).parts
            group = rel_parts[0] if len(rel_parts) > 1 else "root"
            groups.setdefault(group, []).append(path)
        return groups

    def _send_gallery(self) -> None:
        root = self.server.sticker_dir
        groups = self._collect_groups()
        total = sum(len(items) for items in groups.values())
        cards: list[str] = []
        for group in sorted(groups):
            items = groups[group]
            cards.append(f'<h2 id="{quote(group)}">{html.escape(group)}'
                         f'<span class="count">{len(items)}</span></h2>')
            cards.append('<div class="grid">')
            for path in items:
                rel = path.relative_to(root).as_posix()
                name = html.escape(path.name)
                cards.append(
                    '<a class="cell" target="_blank" '
                    f'href="/img?path={quote(rel)}">'
                    f'<img loading="lazy" src="/img?path={quote(rel)}" alt="{name}">'
                    f'<span class="name">{name}</span></a>'
                )
            cards.append("</div>")

        nav = " ".join(
            f'<a href="#{quote(g)}">{html.escape(g)} <b>{len(groups[g])}</b></a>'
            for g in sorted(groups)
        )
        body = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sticker Gallery</title>
<style>
  body {{ margin:0; font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
         background:#15171c; color:#e8eaed; }}
  header {{ position:sticky; top:0; z-index:10; background:#1d2027;
           padding:12px 20px; border-bottom:1px solid #2c2f38; }}
  header h1 {{ font-size:18px; margin:0 0 8px; }}
  nav a {{ display:inline-block; margin:0 10px 4px 0; color:#7db4ff;
          text-decoration:none; font-size:13px; }}
  main {{ padding:18px 20px 60px; }}
  h2 {{ font-size:16px; margin:26px 0 12px; }}
  .count {{ font-size:12px; color:#9aa0a8; margin-left:8px; font-weight:400; }}
  .grid {{ display:grid; gap:12px;
          grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); }}
  .cell {{ display:block; background:#1d2027; border:1px solid #2c2f38;
          border-radius:10px; overflow:hidden; text-decoration:none; color:#c9ccd2; }}
  .cell img {{ width:100%; height:150px; object-fit:cover; display:block;
              background:#111318; }}
  .name {{ display:block; font-size:11px; padding:6px 8px;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
</style></head><body>
<header><h1>Sticker Gallery — {total} images</h1><nav>{nav}</nav></header>
<main>{''.join(cards)}</main></body></html>"""
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--dir", default="", help="sticker directory")
    args = parser.parse_args()

    sticker_dir = Path(args.dir).resolve() if args.dir else _default_sticker_dir().resolve()
    if not sticker_dir.exists():
        print(f"sticker dir not found: {sticker_dir}")
        return 1

    server = ThreadingHTTPServer((args.host, args.port), GalleryHandler)
    server.sticker_dir = sticker_dir
    print(f"serving {sticker_dir}")
    print(f"open http://<server-ip>:{args.port} in your local browser")
    print("press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
