"""Windowed PyInstaller entry point."""
from multiprocessing import freeze_support
from suseoro.simple.desktop import main

if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
