"""Where lots, stage progress and flags live.

SheetsStore: the LAB Google spreadsheet. Lots are read from the bundle tab;
progress and flags are written to the Tracker and Flags tabs, which are
created on first run.

LocalStore: SQLite plus a lots CSV, for trying the app without Google.

Both keep a short-lived in-memory snapshot so many phones polling the
overview don't turn into many Google API calls.
"""

from __future__ import annotations

import csv
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

STAGES = ["listed", "photographed", "uploaded", "checked", "complete"]
FLAG_FIELDS = ["retake"]
TRACKER_FIELDS = STAGES + FLAG_FIELDS + ["photos"]

# Tracker tab columns: (field, header). Read by header name, so columns can be reordered.
TRACKER_COLUMNS = [
    ("lot_id", "Lot ID"),
    ("title", "Lot Title"),
    ("listed", "Listed in 32A"),
    ("photographed", "Photographed"),
    ("uploaded", "Photos uploaded to 32A"),
    ("checked", "Description checked"),
    ("complete", "Complete"),
    ("retake", "Needs retake"),
    ("photos", "Photo count"),
]
FLAG_COLUMNS = [
    ("id", "Flag ID"),
    ("lot_id", "Lot ID"),
    ("text", "Comment"),
    ("raised_at", "Raised"),
    ("resolved_at", "Resolved"),
    ("kind", "Kind"),
]


class StoreError(Exception):
    """A read or write to the backing store failed."""


@dataclass
class Lot:
    id: int
    type: str  # physical | digital | unset
    title: str
    subtitle: str
    description: str
    auction_url: str


def now_iso() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).isoformat(timespec="minutes")


def empty_progress() -> dict:
    return {**{k: False for k in STAGES + FLAG_FIELDS}, "photos": 0}


def parse_lot_id(value) -> int | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return int(digits) if digits and len(digits) <= 6 else None


def parse_type(value) -> str:
    v = str(value or "").strip().lower()
    if v.startswith("phys"):
        return "physical"
    if v.startswith(("virt", "digi", "e-gift", "egift")):
        return "digital"
    return "unset"


def parse_bool(value) -> bool:
    return str(value).strip().upper() in ("TRUE", "YES", "Y", "1", "X", "✓")


def _norm(s) -> str:
    return " ".join(str(s or "").split()).lower()


def find_header(rows: list[list], required: str) -> tuple[int, dict[str, int]] | None:
    """Index of the first row (within the first 10) containing `required`, plus header->column map."""
    for i, row in enumerate(rows[:10]):
        cols = {_norm(h): j for j, h in enumerate(row) if str(h).strip()}
        if _norm(required) in cols:
            return i, cols
    return None


def lots_from_rows(rows: list[list]) -> list[Lot]:
    found = find_header(rows, config.COL_LOT_ID)
    if not found:
        raise StoreError(f"Couldn't find a '{config.COL_LOT_ID}' column in the bundle sheet.")
    hi, cols = found

    def cell(row, name):
        j = cols.get(_norm(name))
        return str(row[j]).strip() if j is not None and j < len(row) else ""

    lots, seen = [], set()
    for row in rows[hi + 1:]:
        lot_id = parse_lot_id(cell(row, config.COL_LOT_ID))
        title = cell(row, config.COL_TITLE)
        if lot_id is None or lot_id in seen or not title or title.upper().startswith("#N/A"):
            continue
        seen.add(lot_id)
        lots.append(Lot(
            id=lot_id,
            type=parse_type(cell(row, config.COL_TYPE)),
            title=title,
            subtitle=cell(row, config.COL_SUBTITLE),
            description=cell(row, config.COL_DESCRIPTION),
            auction_url=cell(row, config.COL_AUCTION_URL) or config.AUCTION_URL,
        ))
    return sorted(lots, key=lambda l: l.id)


class Store:
    source = "base"

    def __init__(self):
        self._lock = threading.RLock()
        self._snap: dict | None = None
        self._snap_at = 0.0

    # --- implemented by subclasses ---
    def _load(self) -> dict:  # -> {"lots": [Lot], "progress": {id: {...}}, "flags": [dict]}
        raise NotImplementedError

    def _write_fields(self, lot: Lot, fields: dict) -> None:
        raise NotImplementedError

    def _append_flag(self, flag: dict) -> None:
        raise NotImplementedError

    def _write_resolved(self, flag_id: str, resolved_at: str) -> None:
        raise NotImplementedError

    # --- shared ---
    def snapshot(self, force: bool = False) -> dict:
        with self._lock:
            fresh = self._snap and time.monotonic() - self._snap_at < config.REFRESH_SECONDS
            if fresh and not force:
                return self._snap
            try:
                data = self._load()
                self._snap = {**data, "refreshed_at": now_iso(), "error": None}
            except Exception as e:  # keep serving the last good copy
                if not self._snap:
                    raise StoreError(str(e)) from e
                self._snap = {**self._snap, "error": f"Couldn't reach the spreadsheet: {e}"}
            self._snap_at = time.monotonic()
            return self._snap

    def lot(self, lot_id: int) -> Lot | None:
        return next((l for l in self.snapshot()["lots"] if l.id == lot_id), None)

    def set_fields(self, lot_id: int, fields: dict) -> dict:
        bad = set(fields) - set(TRACKER_FIELDS)
        if bad:
            raise ValueError(f"Unknown fields: {bad}")
        with self._lock:
            lot = self.lot(lot_id)
            if not lot:
                raise KeyError(lot_id)
            try:
                self._write_fields(lot, fields)
            except Exception as e:
                raise StoreError(str(e)) from e
            prog = self._snap["progress"].setdefault(lot_id, empty_progress())
            prog.update(fields)
            return prog

    def add_flag(self, lot_id: int, text: str, kind: str = "") -> dict:
        with self._lock:
            if not self.lot(lot_id):
                raise KeyError(lot_id)
            flag = {"id": secrets.token_hex(4), "lot_id": lot_id, "text": text.strip()[:2000],
                    "raised_at": now_iso(), "resolved_at": "", "kind": kind}
            try:
                self._append_flag(flag)
            except Exception as e:
                raise StoreError(str(e)) from e
            self._snap["flags"].append(flag)
            return flag

    def resolve_flag(self, flag_id: str) -> dict:
        with self._lock:
            flag = next((f for f in self.snapshot(force=True)["flags"] if f["id"] == flag_id), None)
            if not flag:
                raise KeyError(flag_id)
            if not flag["resolved_at"]:
                ts = now_iso()
                try:
                    self._write_resolved(flag_id, ts)
                except Exception as e:
                    raise StoreError(str(e)) from e
                flag["resolved_at"] = ts
            return flag


# ---------------------------------------------------------------- local mode

class LocalStore(Store):
    source = "local"

    def __init__(self, db_path: Path, lots_csv: Path):
        super().__init__()
        self.db_path, self.lots_csv = db_path, lots_csv
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS progress (
                  lot_id INTEGER PRIMARY KEY, listed INT DEFAULT 0, photographed INT DEFAULT 0,
                  uploaded INT DEFAULT 0, checked INT DEFAULT 0, complete INT DEFAULT 0,
                  retake INT DEFAULT 0, photos INT DEFAULT 0);
                CREATE TABLE IF NOT EXISTS flags (
                  id TEXT PRIMARY KEY, lot_id INTEGER, text TEXT, raised_at TEXT,
                  resolved_at TEXT DEFAULT '', kind TEXT DEFAULT '');
                """
            )

    def _db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _load(self) -> dict:
        path = self.lots_csv if self.lots_csv.is_file() else config.SAMPLE_LOTS_CSV
        with open(path, newline="", encoding="utf-8-sig") as f:
            lots = lots_from_rows(list(csv.reader(f)))
        with self._db() as c:
            progress = {
                r["lot_id"]: {**{k: bool(r[k]) for k in STAGES + FLAG_FIELDS}, "photos": r["photos"]}
                for r in c.execute("SELECT * FROM progress")
            }
            flags = [dict(r) for r in c.execute("SELECT * FROM flags ORDER BY raised_at")]
        return {"lots": lots, "progress": progress, "flags": flags}

    def _write_fields(self, lot: Lot, fields: dict) -> None:
        with self._db() as c:
            c.execute("INSERT OR IGNORE INTO progress (lot_id) VALUES (?)", (lot.id,))
            for k, v in fields.items():  # keys are validated against TRACKER_FIELDS
                c.execute(f"UPDATE progress SET {k} = ? WHERE lot_id = ?", (int(v), lot.id))

    def _append_flag(self, flag: dict) -> None:
        with self._db() as c:
            c.execute("INSERT INTO flags VALUES (:id, :lot_id, :text, :raised_at, :resolved_at, :kind)", flag)

    def _write_resolved(self, flag_id: str, resolved_at: str) -> None:
        with self._db() as c:
            c.execute("UPDATE flags SET resolved_at = ? WHERE id = ?", (resolved_at, flag_id))


# ---------------------------------------------------------------- Google Sheets

def _col(i: int) -> str:
    """0-based column index -> A1 letter."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _q(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


class SheetsStore(Store):
    """`spreadsheet` is a gspread Spreadsheet (or a test double with the same methods)."""

    source = "sheets"

    def __init__(self, spreadsheet):
        super().__init__()
        self.sh = spreadsheet
        self.bundle_title = config.BUNDLE_SHEET_NAME or None
        self._ensure_tabs()

    def _tab(self, title: str):
        return next((ws for ws in self.sh.worksheets() if ws.title == title), None)

    def _ensure_tabs(self) -> None:
        for title, columns in ((config.TRACKER_SHEET_NAME, TRACKER_COLUMNS), (config.FLAGS_SHEET_NAME, FLAG_COLUMNS)):
            if self._tab(title):
                continue
            ws = self.sh.add_worksheet(title=title, rows=200, cols=len(columns))
            ws.append_row([h for _, h in columns], value_input_option="RAW", table_range="A1")
            requests = [{"repeatCell": {  # bold, frozen header
                "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold"}},
                {"updateSheetProperties": {"properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                                           "fields": "gridProperties.frozenRowCount"}}]
            if columns is TRACKER_COLUMNS:  # stage columns as checkboxes
                requests.append({"setDataValidation": {
                    "range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 8},
                    "rule": {"condition": {"type": "BOOLEAN"}}}})
            self.sh.batch_update({"requests": requests})

    def _find_bundle_tab(self) -> str:
        skip = {config.TRACKER_SHEET_NAME, config.FLAGS_SHEET_NAME}
        titles = [ws.title for ws in self.sh.worksheets() if ws.title not in skip]
        res = self.sh.values_batch_get([f"{_q(t)}!1:10" for t in titles])
        for title, vr in zip(titles, res.get("valueRanges", [])):
            rows = vr.get("values", [])
            found = find_header(rows, config.COL_LOT_ID)
            if found and _norm(config.COL_TITLE) in found[1]:
                return title
        raise StoreError(f"No tab has both '{config.COL_LOT_ID}' and '{config.COL_TITLE}' columns. "
                         "Set BUNDLE_SHEET_NAME.")

    def _load(self) -> dict:
        if not self.bundle_title:
            self.bundle_title = self._find_bundle_tab()
        res = self.sh.values_batch_get([
            _q(self.bundle_title), _q(config.TRACKER_SHEET_NAME), _q(config.FLAGS_SHEET_NAME)])
        bundle, tracker, flags = (vr.get("values", []) for vr in res["valueRanges"])
        return {"lots": lots_from_rows(bundle), "progress": self._parse_tracker(tracker),
                "flags": self._parse_flags(flags)}

    @staticmethod
    def _header_map(rows, columns) -> dict[str, int]:
        head = {_norm(h): j for j, h in enumerate(rows[0])} if rows else {}
        return {field: head[_norm(h)] for field, h in columns if _norm(h) in head}

    def _parse_tracker(self, rows) -> dict:
        cols = self._header_map(rows, TRACKER_COLUMNS)
        out = {}
        for row in rows[1:]:
            get = lambda f: row[cols[f]] if f in cols and cols[f] < len(row) else ""
            lot_id = parse_lot_id(get("lot_id"))
            if lot_id is None:
                continue
            prog = {k: parse_bool(get(k)) for k in STAGES + FLAG_FIELDS}
            prog["photos"] = parse_lot_id(get("photos")) or 0
            out[lot_id] = prog
        return out

    def _parse_flags(self, rows) -> list:
        cols = self._header_map(rows, FLAG_COLUMNS)
        out = []
        for row in rows[1:]:
            get = lambda f: str(row[cols[f]]).strip() if f in cols and cols[f] < len(row) else ""
            lot_id = parse_lot_id(get("lot_id"))
            if lot_id is None or not get("id"):
                continue
            out.append({"id": get("id"), "lot_id": lot_id, "text": get("text"), "raised_at": get("raised_at"),
                        "resolved_at": get("resolved_at"), "kind": get("kind")})
        return out

    def _write_fields(self, lot: Lot, fields: dict) -> None:
        ws = self._tab(config.TRACKER_SHEET_NAME)
        rows = ws.get_all_values()  # re-read so we never write to a row someone moved
        cols = self._header_map(rows, TRACKER_COLUMNS)
        missing = [h for f, h in TRACKER_COLUMNS if f not in cols]
        if missing:
            raise StoreError(f"The {config.TRACKER_SHEET_NAME} tab is missing columns: {', '.join(missing)}")
        row_no = next((i + 1 for i, r in enumerate(rows) if i and cols["lot_id"] < len(r)
                       and parse_lot_id(r[cols["lot_id"]]) == lot.id), None)
        if row_no is None:
            values = {**empty_progress(), **fields, "lot_id": lot.id, "title": lot.title}
            row = [""] * (max(cols.values()) + 1)
            for f, j in cols.items():
                row[j] = values[f]
            ws.append_row(row, value_input_option="RAW", table_range="A1")
        else:
            ws.batch_update([{"range": f"{_col(cols[f])}{row_no}", "values": [[v]]} for f, v in fields.items()],
                            value_input_option="RAW")

    def _append_flag(self, flag: dict) -> None:
        ws = self._tab(config.FLAGS_SHEET_NAME)
        ws.append_row([flag[f] for f, _ in FLAG_COLUMNS], value_input_option="RAW", table_range="A1")

    def _write_resolved(self, flag_id: str, resolved_at: str) -> None:
        ws = self._tab(config.FLAGS_SHEET_NAME)
        rows = ws.get_all_values()
        cols = self._header_map(rows, FLAG_COLUMNS)
        row_no = next((i + 1 for i, r in enumerate(rows) if i and cols["id"] < len(r)
                       and str(r[cols["id"]]).strip() == flag_id), None)
        if row_no is None:
            raise StoreError("That flag is no longer in the sheet.")
        ws.batch_update([{"range": f"{_col(cols['resolved_at'])}{row_no}", "values": [[resolved_at]]}],
                        value_input_option="RAW")


def open_store() -> Store:
    if config.GOOGLE_SHEET_ID:
        import gspread  # only needed in Sheets mode

        client = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
        return SheetsStore(client.open_by_key(config.GOOGLE_SHEET_ID))
    return LocalStore(config.DATA_DIR / "tracker.db", config.LOTS_CSV)


def lot_dict(lot: Lot) -> dict:
    return asdict(lot)
