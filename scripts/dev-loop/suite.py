#!/usr/bin/env python3
"""Run the full unittest suite and pass only if every failure is a known one.

  suite.py [--known TEST_NAME ...]
Prints the unexpected failures (and the tail of the output) so a worker can fix them.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--known", action="append", default=[], help="a test name allowed to fail")
    args = ap.parse_args()
    env = dict(os.environ, RINGER_NO_SELF_UPDATE="1")
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], env=env, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    failing = re.findall(r"^(?:FAIL|ERROR): (\S+)", out, re.M)
    unexpected = [name for name in failing if name not in args.known]
    ran = re.search(r"^Ran (\d+) tests", out, re.M)
    if not ran:
        print("suite did not run:\n" + out[-2000:])
        return 1
    print(f"{ran.group(0)}; failing: {failing}; unexpected: {unexpected}")
    if unexpected:
        print(out[-4000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
