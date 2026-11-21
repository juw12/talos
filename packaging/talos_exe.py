"""Entry point for the one-file Windows build (see .github/workflows/release.yml)."""
from talos.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
