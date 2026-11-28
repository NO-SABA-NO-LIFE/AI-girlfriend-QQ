from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

URL_RE = re.compile(r"https?://[^\s<>\]）)\"']+", re.IGNORECASE)
WINDOWS_PATH_RE = re.compile(r"(?:[A-Za-z]:\\[^\r\n\"'<>]+)")
TEXT_EXTENSIONS = {".txt", ".md", ".log", ".csv", ".tsv", ".json", ".py", ".js", ".ts", ".tsx",
                   ".jsx", ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".java", ".tex", ".yaml", ".yml"}
DOC_EXTENSIONS = {".docx", ".pdf"}
SHEET_EXTENSIONS = {".xlsx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SUPPORTED_FILE_EXTENSIONS = TEXT_EXTENSIONS | DOC_EXTENSIONS | SHEET_EXTENSIONS | IMAGE_EXTENSIONS
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".flv"}
MAX_TEXT_CHARS = 2800

VISUAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "direct_answer": {"type": "string"},
        "visible_text": {"type": "string"},
        "subjects": {"type": "string"},
        "identity": {"type": "string"},
        "emotion": {"type": "string"},
        "social_intent": {"type": "string"},
        "evidence": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "needs_web_verification": {"type": "boolean"},
        "search_keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "direct_answer",
        "visible_text",
        "subjects",
        "identity",
        "emotion",
        "social_intent",
        "evidence",
        "confidence",
        "needs_web_verification",
        "search_keywords",
    ],
    "additionalProperties": False,
}

OneBotActionCaller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
