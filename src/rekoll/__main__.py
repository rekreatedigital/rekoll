"""``python -m rekoll`` — same CLI as the ``rekoll`` console script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
