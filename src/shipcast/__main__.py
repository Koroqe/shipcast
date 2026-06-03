"""Allow `python -m shipcast` to dispatch to the CLI."""

from shipcast.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
