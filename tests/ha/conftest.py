"""HA tests run separately (tools/run-ha-tests.sh) because they need Home Assistant itself."""

import importlib.util

collect_ignore_glob = [] if importlib.util.find_spec("pytest_homeassistant_custom_component") else ["test_*.py"]
