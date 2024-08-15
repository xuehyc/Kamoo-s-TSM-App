import os
import urllib3

from ah.defs import SECONDS_IN
from ah.utils import get_temp_path

BN_CLIENT_ID = os.environ.get("BN_CLIENT_ID")
BN_CLIENT_SECRET = os.environ.get("BN_CLIENT_SECRET")
# minimum 60 days
MIN_RECORD_EXPIRES = 60 * SECONDS_IN.DAY
LOGGING_LEVEL = os.environ.get("LOGGING_LEVEL", "INFO")
DEFAULT_CACHE_PATH = os.path.join(get_temp_path(), "ah_cache")
DEFAULT_CACHE_EXPIRES_IN = SECONDS_IN.WEEK
DEFAULT_DB_PATH = "db"
DEFAULT_DB_COMPRESS = True
# how often to take a snapshot of the system memory / cpu usage
DEFAULT_SNAPSHOT_INTERVAL = 10
MAX_SNAPSHOTS = 100
# /data/wow/connected-realm/index randomly encounters SSL errors
VERIFY_SSL = False
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# of which tag the database files are released
TAG_DB_RELEASE = "latest"
# what is the archive name in the build release,
# should be the same in `dist_glob_patterns` in `pyproject.toml`
RELEASED_ARCHIVE_NAME = "archive.zip"

BN_OAUTH_URL = os.environ.get("BN_OAUTH_URL")
BN_OAUTH_URL_CN = os.environ.get("BN_OAUTH_URL_CN")
SKIP_UPDATE = os.environ.get("SKIP_UPDATE", "0") == "1"