"""Reclassify captured stickers by vision-detected emotion.

Reads the real .env via load_config, sends each image to the configured vision
model with the sticker-emotion prompt, and moves it into the matching emotion
subdirectory. Start with a small dry-run / trial batch, then run the full set.

Usage:
    python deploy/reclassify_stickers.py --limit 10 --dry-run
    python deploy/reclassify_stickers.py --limit 10
    python deploy/reclassify_stickers.py
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

# Allow running against source without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from atri_qq_bot.config.loader import load_config
from atri_qq_bot.prompting import load_prompt
from atri_qq_bot.sticker_parts.core import _normalize_emotion

IMAGE_SUFFIXES = {".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _unique(target: Path) -> Path:
    """Return a non-existing sibling path so moves never overwrite."""
    if not target.exists():
        return target
    parent, stem, suffix = target.parent, target.stem, target.suffix
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _vision_label(config: Any, image_bytes: bytes, mime: str, system_prompt: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": config.toolbox_vision_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"},
                    }
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": 12,
    }
    headers = {"Authorization": f"Bearer {config.toolbox_vision_api_key or 'ollama'}"}
    url = f"{str(config.toolbox_vision_base_url).rstrip('/')}/chat/completions"
    with httpx.Client(
        timeout=float(getattr(config, "toolbox_vision_timeout_seconds", 30.0)),
        headers=headers,
    ) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
    label = str(
        ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    ).strip()
    return label.split()[0] if label else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="process only the first N images (0 = all)",
    )
    parser.add_argument(
        "--source",
        default="",
        help="source directory (default: sticker_dir/unsorted)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print predicted labels without moving files",
    )
    args = parser.parse_args()

    config = load_config()
    source = Path(args.source) if args.source else config.sticker_dir / "unsorted"
    if not source.exists():
        print(f"source not found: {source}")
        return 1

    images = sorted(
        path
        for path in source.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    if args.limit:
        images = images[: args.limit]

    print(f"vision model: {config.toolbox_vision_model}")
    print(f"source: {source}")
    print(f"candidates: {len(images)}  dry_run={args.dry_run}")
    print("-" * 60)

    system_prompt = load_prompt("sticker_emotion")
    moved = 0
    failed = 0
    counts: dict[str, int] = {}

    for index, path in enumerate(images, start=1):
        try:
            mime = _MIME.get(path.suffix.lower(), "image/jpeg")
            raw_label = _vision_label(config, path.read_bytes(), mime, system_prompt)
        except Exception as exc:
            print(f"[{index:>3}] FAIL {path.name}: {exc}")
            failed += 1
            continue

        emotion = _normalize_emotion(raw_label)
        if emotion in {"neutral", "unsorted"}:
            emotion = "unsorted"
        counts[emotion] = counts.get(emotion, 0) + 1
        tag = "DRY " if args.dry_run else "MOVE"
        print(f"[{index:>3}] {tag} {emotion:<10} <- {path.name}")

        if not args.dry_run and emotion != "unsorted":
            target_dir = config.sticker_dir / emotion
            target_dir.mkdir(parents=True, exist_ok=True)
            target = _unique(target_dir / path.name)
            path.rename(target)
            sidecar = Path(f"{path}.json")
            if sidecar.exists():
                sidecar.rename(_unique(target_dir / sidecar.name))
            moved += 1

    print("-" * 60)
    print("summary:")
    for emotion, count in sorted(counts.items()):
        print(f"  {emotion:<10} {count}")
    print(f"moved={moved}  failed={failed}  total={len(images)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
