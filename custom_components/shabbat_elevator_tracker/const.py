"""Constants for the Shabbat Elevator Tracker integration."""

DOMAIN = "shabbat_elevator_tracker"
API_VERSION = 1

CONF_URL = "url"
CONF_HOME_FLOOR = "home_floor"

POLL_INTERVAL_SECONDS = 60
SSE_RETRY_MAX_SECONDS = 300
EVENT_SYNC = f"{DOMAIN}_sync"

SYNC_STATES = ["idle", "searching", "provisional", "locked", "paused"]
