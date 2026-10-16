"""`python -m airplay_windows` starts the desktop app; `python -m airplay_windows cli ...` the command line."""

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        from airplay_windows.cli import main as cli_main

        return cli_main(sys.argv[2:])
    from airplay_windows.ui.app import main as app_main

    return app_main()


if __name__ == "__main__":
    sys.exit(main())
