#!/bin/sh
# Run the Home Assistant integration tests in a throwaway Python 3.13 container.
#   sh tools/run-ha-tests.sh
# (python:3.13, not -slim: some Home Assistant dependencies need a compiler.)
set -e
cd "$(dirname "$0")/.."
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd)/custom_components:/w/custom_components:ro" \
  -v "$(pwd)/tests/ha:/w/tests/ha:ro" -w /w python:3.13 sh -ec '
    pip install --quiet --root-user-action=ignore --disable-pip-version-check pytest-homeassistant-custom-component
    printf "[pytest]\nasyncio_mode = auto\ntestpaths = tests/ha\n" > pytest.ini
    touch tests/__init__.py
    python -m pytest -q -p no:cacheprovider tests/ha'
