"""PyInstaller dedicated broker entry; no auto-elevation or service installation."""
from remontevoice.broker_entry import main

if __name__ == '__main__':
    raise SystemExit(main())
