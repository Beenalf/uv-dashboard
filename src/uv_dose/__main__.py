"""Entry point: ``uv run uv-dose`` or ``python -m uv_dose``."""

from __future__ import annotations


def main() -> None:
    from uv_dose.gui.app import run

    run()


if __name__ == "__main__":
    main()
