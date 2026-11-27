from __future__ import annotations

import sys

from .loader import load_config


def validate_file(path: str) -> int:
    """Validate one env file using the real loader; isolated process entry point."""
    try:
        load_config(path)
    except (ValueError, TypeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(
            'usage: python -m atri_qq_bot.config.validate_file <env-file>',
            file=sys.stderr,
        )
        return 2
    return validate_file(args[0])


if __name__ == '__main__':
    sys.exit(main())
