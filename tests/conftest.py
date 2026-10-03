import os
import sys
import tempfile
from pathlib import Path

# Configure before the app modules are imported.
_data = tempfile.mkdtemp(prefix="lab-tracker-test-")
os.environ.update({"TRACKER_TOKEN": "test-token-0123456789", "DATA_DIR": _data, "GOOGLE_SHEET_ID": "",
                   "REFRESH_SECONDS": "0", "LOTS_CSV": str(Path(_data) / "none.csv")})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
