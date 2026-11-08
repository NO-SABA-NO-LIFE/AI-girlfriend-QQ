"""Diagnose QQ-based identity aggregation.

Loads the real users.json and reports:

* every conversation scope and the QQ number parsed from it;
* the scopes (private chat plus group memberships) grouped by QQ number, which
  shows whether the same person is recognised across private and group chats.

Output is plain ASCII so it is readable in the VNC terminal.

Usage:
    python deploy/check_identity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atri_qq_bot.config.loader import load_config
from atri_qq_bot.memory.helpers import parse_identity_user_id


def main() -> int:
    config = load_config()
    users_file = Path(config.memory_path)
    if not users_file.exists():
        print(f"users file not found: {users_file}")
        return 1

    import json

    data = json.loads(users_file.read_text(encoding="utf-8"))
    conversations = data.get("conversations", {})
    print(f"users file: {users_file}")
    print(f"version: {data.get('version')}  conversation entries: {len(conversations)}")
    print("=" * 60)

    # Map each QQ number to the scopes it appears in.
    by_qq: dict[str, list[str]] = {}
    for cid in sorted(conversations):
        qq = parse_identity_user_id(cid)
        label = f"{cid}  -> qq={qq if qq else 'n/a'}"
        print(label)
        if qq:
            by_qq.setdefault(qq, []).append(cid)

    print("=" * 60)
    print("identity aggregation by QQ number:")
    for qq in sorted(by_qq):
        scopes = by_qq[qq]
        print(f"qq={qq}  scopes={len(scopes)}")
        for scope in scopes:
            print(f"    - {scope}")
        if len(scopes) > 1:
            print("    (same person linked across multiple conversations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
