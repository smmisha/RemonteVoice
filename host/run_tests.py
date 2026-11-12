"""Run the Host test suite with a bounded CI diagnostic.

The normal local command is `python -m unittest discover -s ..\tests`. In CI the
same discovery is wrapped in faulthandler so a blocking test dumps every thread
stack and fails the job instead of leaving it in progress indefinitely.
"""

from __future__ import annotations

import faulthandler
import unittest


def main() -> int:
    faulthandler.dump_traceback_later(300, exit=True)
    suite = unittest.TestLoader().discover(r"..\tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
