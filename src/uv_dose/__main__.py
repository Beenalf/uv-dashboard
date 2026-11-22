"""Entry point: ``uv run uv-dose`` or ``python -m uv_dose``."""

from __future__ import annotations


def main() -> None:
    from datetime import date

    from uv_dose.arpansa import DEFAULT_SITE_CODE, FALLBACK_SITES, ArpansaClient
    from uv_dose.settings import Settings

    # Start downloading today's UV data first: loading the window's libraries (matplotlib,
    # numpy, Tk) takes about as long as the download, so the two overlap.
    settings = Settings.load()
    client = ArpansaClient()
    codes = {s.code: s for s in FALLBACK_SITES}
    client.prefetch(codes.get(settings.site_code) or codes[DEFAULT_SITE_CODE], date.today())

    from uv_dose.gui.app import run

    run(client, settings)


if __name__ == "__main__":
    main()
