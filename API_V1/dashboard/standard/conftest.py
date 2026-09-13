"""Makes `app` and `crawlkit` importable for every test under dashboard/.

pytest.ini already puts the dashboard folder and the API_V1 folder on the
path - but only when pytest picks THIS ini file. Run from the API_V1 folder
together with another folder (`pytest crawlkit dashboard/standard/tests/unit`,
the integration command) pytest chooses the first folder's ini, and `from app
import config` fails before the first test. A conftest is loaded whichever
ini won, so the path is fixed here as well; adding a folder twice is harmless.
"""

from __future__ import annotations

import sys
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent
API_ROOT = DASHBOARD_DIR.parents[1]

for folder in (str(API_ROOT), str(DASHBOARD_DIR)):
    if folder not in sys.path:
        sys.path.insert(0, folder)
