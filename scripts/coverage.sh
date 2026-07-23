#!/bin/bash
# Python line-coverage for the engine + server: full unittest suite under
# coverage.py (one-time: python3 -m pip install coverage). Extra args are
# passed to `coverage report` (e.g. --show-missing).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m coverage run --source=fleetdash,server -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage report --sort=cover "$@"
