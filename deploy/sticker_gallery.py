"""Sticker gallery and review web server.

Serves the local sticker library as a browser gallery grouped by emotion and
adds a review workflow:

* new stickers wait in the ``_review`` subdirectory (ignored by the sending
  logic) until the owner approves them;
* approve runs the vision classifier and moves the image into an emotion
  directory; if vision fails the owner can pick the emotion manually;
* delete removes the image and its JSON sidecar (with browser confirmation).

Uses the standard library plus the project's httpx for the vision call and
binds to all interfaces so it works from the user's own browser.

Usage:
    python deploy/sticker_gallery.py
    python deploy/sticker_gallery.py --port 8788
    python deploy/sticker_gallery.py --dir /opt/atri-qq-bot/data/stickers
Then open http://SERVER_IP:8788 in a local browser.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# Allow running against source without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

IMAGE_SUFFIXES = {".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp"}
REVIEW_DIR = "_review"
# Pending review images older than this are removed automatically.
REVIEW_TTL_SECONDS = 2 * 60 * 60
# How often the background cleanup thread scans the review area.
REVIEW_SWEEP_SECONDS = 10 * 60

# Manual classification choices (the core 12 emotions).
MANUAL_EMOTIONS = [
    "happy", "comfort", "sad", "angry", "tired", "proud",
    "confused", "speechless", "surprised", "awkward", "teasing", "shy",
]


def _load_config():
    from atri_qq_bot.config.loader import load_config

    return load_config()


def _default_sticker_dir() -> Path:
    try:
        return Path(_load_config().sticker_dir)
    except Exception:
        return Path(__file__).resolve().parents[1] / "data" / "stickers"


def _normalize_emotion(label: str) -> str:
    try:
        from atri_qq_bot.sticker_parts.core import _normalize_emotion as norm

        return norm(label)
    except Exception:
        return label.strip().lower()


def _unique_target(target_dir: Path, name: str) -> Path:
    """Return a non-existing path inside target_dir, adding a suffix if needed."""
    candidate = target_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while True:
        candidate = target_dir / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _move_with_sidecar(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    sidecar = source.with_suffix(source.suffix + ".json")
    if sidecar.exists():
        sidecar.rename(_unique_target(target.parent, target.name + ".json"))


def _delete_image_and_sidecar(image: Path) -> None:
    image.unlink()
    sidecar = image.with_suffix(image.suffix + ".json")
    if sidecar.exists():
        sidecar.unlink()


def purge_expired_review(sticker_dir: Path, ttl_seconds: int) -> int:
    """Delete review images older than ttl; return the number removed."""
    review = sticker_dir / REVIEW_DIR
    if not review.exists():
        return 0
    now = time.time()
    removed = 0
    for image in review.iterdir():
        if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        age = now - image.stat().st_mtime
        if age > ttl_seconds:
            _delete_image_and_sidecar(image)
            removed += 1
    return removed


def _start_review_sweeper(sticker_dir: Path, ttl_seconds: int) -> None:
    def loop() -> None:
        while True:
            time.sleep(REVIEW_SWEEP_SECONDS)
            try:
                purge_expired_review(sticker_dir, ttl_seconds)
            except Exception as exc:
                sys.stderr.write(f"[gallery] review sweep failed: {exc}\n")

    threading.Thread(target=loop, daemon=True).start()


class GalleryHandler(BaseHTTPRequestHandler):
    # Subclasses set these on the server object; keep defaults for typing.
    sticker_dir = Path(".")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("[gallery] %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_gallery()
        elif parsed.path == "/img":
            self._send_image(parse_qs(parsed.query))
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/batch-delete":
                # JSON body: {"paths": ["rel1", "rel2", ...]}
                self._handle_batch_delete(json.loads(raw or "{}"))
            else:
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                if parsed.path == "/api/approve":
                    self._handle_approve(form)
                elif parsed.path == "/api/manual":
                    self._handle_manual(form)
                elif parsed.path == "/api/reclassify":
                    self._handle_reclassify(form)
                elif parsed.path == "/api/delete":
                    self._handle_delete(form)
                else:
                    self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # never leak a traceback to the browser
            self._send_json(500, {"ok": False, "error": str(exc)})

    def _resolve_inside_root(self, rel: str) -> Path | None:
        if not rel:
            return None
        root = self.server.sticker_dir.resolve()
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except (OSError, ValueError):
            return None
        if not target.is_file() or target.suffix.lower() not in IMAGE_SUFFIXES:
            return None
        return target

    def _require_review_image(self, form: dict) -> Path:
        target = self._resolve_inside_root(form.get("path", ""))
        if target is None:
            raise ValueError("invalid image path")
        if target.parent.name != REVIEW_DIR:
            raise ValueError("only review images can be approved")
        return target

    def _vision_emotion(self, image: Path) -> str:
        """Call the vision model synchronously; raise on failure/neutral."""
        import httpx

        config = _load_config()
        prompt_path = Path(config.prompts_dir) / "sticker_emotion.md"
        prompt = prompt_path.read_text(encoding="utf-8")
        b64 = base64.b64encode(image.read_bytes()).decode("ascii")
        payload = {
            "model": config.toolbox_vision_model,
            "temperature": 0.1,
            "max_tokens": 12,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        }
        headers = {"Authorization": f"Bearer {config.toolbox_vision_api_key}"}
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{config.toolbox_vision_base_url}/chat/completions",
                headers=headers, json=payload,
            )
            response.raise_for_status()
            label = response.json()["choices"][0]["message"]["content"].strip()
        emotion = _normalize_emotion(label)
        if emotion in {"", "neutral", "unsorted"}:
            raise ValueError("vision returned no clear emotion")
        return emotion

    def _handle_approve(self, form: dict) -> None:
        image = self._require_review_image(form)
        emotion = self._vision_emotion(image)
        target_dir = self.server.sticker_dir / emotion
        target = _unique_target(target_dir, image.name)
        _move_with_sidecar(image, target)
        self._send_json(200, {"ok": True, "emotion": emotion})

    def _handle_manual(self, form: dict) -> None:
        image = self._require_review_image(form)
        emotion = _normalize_emotion(form.get("emotion", ""))
        if emotion not in MANUAL_EMOTIONS:
            raise ValueError("invalid emotion")
        target = _unique_target(self.server.sticker_dir / emotion, image.name)
        _move_with_sidecar(image, target)
        self._send_json(200, {"ok": True, "emotion": emotion})

    def _handle_reclassify(self, form: dict) -> None:
        image = self._resolve_inside_root(form.get("path", ""))
        if image is None:
            raise ValueError("invalid image path")
        if image.parent.name == REVIEW_DIR:
            raise ValueError("review images use manual classification")
        emotion = _normalize_emotion(form.get("emotion", ""))
        if emotion not in MANUAL_EMOTIONS:
            raise ValueError("invalid emotion")
        if emotion == image.parent.name:
            raise ValueError("image is already in that category")
        target = _unique_target(self.server.sticker_dir / emotion, image.name)
        _move_with_sidecar(image, target)
        self._send_json(200, {"ok": True, "emotion": emotion})

    def _handle_delete(self, form: dict) -> None:
        image = self._resolve_inside_root(form.get("path", ""))
        if image is None:
            raise ValueError("invalid image path")
        image.unlink()
        sidecar = image.with_suffix(image.suffix + ".json")
        if sidecar.exists():
            sidecar.unlink()
        self._send_json(200, {"ok": True})

    def _handle_batch_delete(self, body: dict) -> None:
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            raise ValueError("no paths provided")
        deleted = 0
        failed: list[str] = []
        for rel in paths:
            image = self._resolve_inside_root(rel)
            if image is None:
                failed.append(str(rel))
                continue
            image.unlink()
            sidecar = image.with_suffix(image.suffix + ".json")
            if sidecar.exists():
                sidecar.unlink()
            deleted += 1
        self._send_json(200, {"ok": True, "deleted": deleted, "failed": failed})

    def _send_image(self, query: dict[str, list[str]]) -> None:
        target = self._resolve_inside_root((query.get("path") or [""])[0])
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
            rel_parts = path.relative_to(root).parts
            group = rel_parts[0] if len(rel_parts) > 1 else "root"
            groups.setdefault(group, []).append(path)
        return groups

    def _review_actions(self, rel: str) -> str:
        options = "".join(
            f'<option value="{em}">{em}</option>' for em in MANUAL_EMOTIONS
        )
        return (
            '<div class="actions">'
            f'<button onclick="approve(this,\'{rel}\')">通过</button>'
            f'<select class="pick">{options}</select>'
            f'<button class="ghost" onclick="manual(this,\'{rel}\')">手动分类</button>'
            f'<button class="danger" onclick="del(this,\'{rel}\')">删除</button>'
            '</div>'
        )

    def _library_actions(self, rel: str) -> str:
        options = "".join(
            f'<option value="{em}">{em}</option>' for em in MANUAL_EMOTIONS
        )
        return (
            '<div class="actions">'
            f'<select class="pick">{options}</select>'
            f'<button class="ghost" onclick="reclassify(this,\'{rel}\')">移动</button>'
            f'<button class="danger" onclick="del(this,\'{rel}\')">删除</button>'
            '</div>'
        )

    def _send_gallery(self) -> None:
        root = self.server.sticker_dir
        groups = self._collect_groups()
        review_items = groups.pop(REVIEW_DIR, [])
        total = sum(len(items) for items in groups.values()) + len(review_items)

        cards: list[str] = []
        if review_items:
            cards.append(f'<h2 id="{REVIEW_DIR}">待审核'
                         f'<span class="count">{len(review_items)}</span></h2>')
            cards.append('<div class="grid">')
            for path in review_items:
                rel = path.relative_to(root).as_posix()
                cards.append(
                    '<div class="cell review">'
                    f'<label class="check"><input type="checkbox" class="pickbox" '
                    f'value="{html.escape(rel)}" onchange="updateBar()"></label>'
                    f'<img loading="lazy" src="/img?path={quote(rel)}">'
                    f'{self._review_actions(rel)}</div>'
                )
            cards.append("</div>")

        for group in sorted(groups):
            items = groups[group]
            cards.append(f'<h2 id="{quote(group)}">{html.escape(group)}'
                         f'<span class="count">{len(items)}</span></h2>')
            cards.append('<div class="grid">')
            for path in items:
                rel = path.relative_to(root).as_posix()
                cards.append(
                    '<div class="cell">'
                    f'<label class="check"><input type="checkbox" class="pickbox" '
                    f'value="{html.escape(rel)}" onchange="updateBar()"></label>'
                    f'<a target="_blank" href="/img?path={quote(rel)}">'
                    f'<img loading="lazy" src="/img?path={quote(rel)}"></a>'
                    f'{self._library_actions(rel)}</div>'
                )
            cards.append("</div>")

        nav_parts = []
        if review_items:
            nav_parts.append(f'<a href="#{REVIEW_DIR}">待审核 <b>{len(review_items)}</b></a>')
        nav_parts.extend(
            f'<a href="#{quote(g)}">{html.escape(g)} <b>{len(groups[g])}</b></a>'
            for g in sorted(groups)
        )
        nav = " ".join(nav_parts)

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
          grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); }}
  .cell {{ background:#1d2027; border:1px solid #2c2f38;
          border-radius:10px; overflow:hidden; }}
  .cell.review {{ border-color:#5a4a2a; }}
  .cell img {{ width:100%; height:150px; object-fit:cover; display:block;
              background:#111318; }}
  .cell a {{ display:block; }}
  .actions {{ display:flex; flex-wrap:wrap; gap:4px; padding:6px; align-items:center; }}
  button {{ font-size:11px; padding:5px 8px; border:0; border-radius:6px;
           background:#3b6fd4; color:#fff; cursor:pointer; }}
  button.ghost {{ background:#3a4150; }}
  button.danger {{ background:#b0413e; }}
  .pick {{ font-size:11px; flex:1; min-width:64px; }}
  .check {{ position:absolute; top:6px; left:6px; z-index:5; padding:3px;
           background:rgba(0,0,0,.55); border-radius:5px; line-height:0; }}
  .cell {{ position:relative; }}
  .check input {{ width:15px; height:15px; cursor:pointer; margin:0; }}
  #batchbar {{ position:fixed; left:0; right:0; bottom:0; z-index:20;
              display:none; background:#232733; border-top:1px solid #3a4150;
              padding:10px 18px; align-items:center; gap:14px; font-size:13px; }}
  #batchbar button {{ font-size:12px; padding:7px 12px; }}
  #batchbar .spacer {{ flex:1; }}
</style></head><body>
<header><h1>Sticker Gallery — {total} images</h1><nav>{nav}</nav></header>
<main>{''.join(cards)}</main>
<div id="batchbar">
  <span id="batchcount">已选 0 张</span>
  <button class="ghost" onclick="toggleAll()">全选/取消全选</button>
  <span class="spacer"></span>
  <button class="danger" onclick="batchDelete()">批量删除</button>
</div>
<script>
function post(url, data) {{
  return fetch(url, {{method:'POST',
    headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
    body:new URLSearchParams(data)}}).then(r => r.json());
}}
function approve(btn, path) {{
  btn.disabled = true;
  post('/api/approve', {{path}}).then(res => {{
    if (res.ok) {{ location.reload(); }}
    else {{ alert('视觉分类失败：' + res.error + '\\n请用手动分类选择情绪。'); btn.disabled=false; }}
  }}).catch(e => {{ alert(e); btn.disabled=false; }});
}}
function manual(btn, path) {{
  const emotion = btn.parentElement.querySelector('.pick').value;
  post('/api/manual', {{path, emotion}}).then(res => {{
    if (res.ok) location.reload(); else alert(res.error);
  }});
}}
function reclassify(btn, path) {{
  const emotion = btn.parentElement.querySelector('.pick').value;
  post('/api/reclassify', {{path, emotion}}).then(res => {{
    if (res.ok) location.reload(); else alert(res.error);
  }});
}}
function del(btn, path) {{
  if (!confirm('确定删除这张图？此操作不可恢复。')) return;
  post('/api/delete', {{path}}).then(res => {{
    if (res.ok) location.reload(); else alert(res.error);
  }});
}}
function selectedPaths() {{
  return Array.from(document.querySelectorAll('.pickbox:checked')).map(c => c.value);
}}
function updateBar() {{
  const n = selectedPaths().length;
  const bar = document.getElementById('batchbar');
  bar.style.display = n ? 'flex' : 'none';
  document.getElementById('batchcount').textContent = '已选 ' + n + ' 张';
}}
function toggleAll() {{
  const boxes = Array.from(document.querySelectorAll('.pickbox'));
  const anyUnchecked = boxes.some(c => !c.checked);
  boxes.forEach(c => {{ c.checked = anyUnchecked; }});
  updateBar();
}}
function batchDelete() {{
  const paths = selectedPaths();
  if (!paths.length) return;
  if (!confirm('确定批量删除选中的 ' + paths.length + ' 张图？此操作不可恢复。')) return;
  fetch('/api/batch-delete', {{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{paths}})}})
    .then(r => r.json()).then(res => {{
      if (res.ok) {{
        if (res.failed && res.failed.length) alert('部分删除失败：' + res.failed.join(', '));
        location.reload();
      }} else alert(res.error);
    }}).catch(alert);
}}
</script></body></html>"""
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--dir", default="", help="sticker directory")
    parser.add_argument(
        "--review-ttl", type=int, default=REVIEW_TTL_SECONDS,
        help="seconds before a pending review image is auto-deleted",
    )
    args = parser.parse_args()

    sticker_dir = Path(args.dir).resolve() if args.dir else _default_sticker_dir().resolve()
    if not sticker_dir.exists():
        print(f"sticker dir not found: {sticker_dir}")
        return 1

    # Purge anything already overdue, then keep sweeping in the background.
    purged = purge_expired_review(sticker_dir, args.review_ttl)
    if purged:
        print(f"removed {purged} expired review image(s)")
    _start_review_sweeper(sticker_dir, args.review_ttl)

    server = ThreadingHTTPServer((args.host, args.port), GalleryHandler)
    server.sticker_dir = sticker_dir
    print(f"serving {sticker_dir}")
    print(f"review images auto-delete after {args.review_ttl}s")
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
