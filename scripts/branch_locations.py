"""Load data/branch_locations.json - branch_uid -> geocoded coordinate.

Built once by `scripts/geocode_branches.py` (KAN-34) and read by anything
downstream that wants to plot or distance-sort branches. A branch missing
from the file, and a branch present with a `null` value, both mean "no
coordinate known" - callers ask `.get(branch_uid)` and treat `None` the
same as "not in the dict"; neither should ever raise or need special-casing.
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "data", "branch_locations.json")


def load_branch_locations(path=DEFAULT_PATH):
    """branch_uid -> {"lat", "lon", "city", "method"}, or `None` if unresolved.

    A missing file (geocoding has never been run) returns an empty dict
    rather than raising, so a caller that has not run the geocoder yet
    simply gets no coordinates for anyone.
    """
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("locations", {})
