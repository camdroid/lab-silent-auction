"""Photo storage on local disk. Originals are kept byte-for-byte as uploaded;
a small JPEG preview is made for the overview and lot pages."""

from __future__ import annotations

import io
import secrets
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from .stores import now_iso

register_heif_opener()  # iPhone HEIC/HEIF

THUMB_EDGE = 640
EXT = {"JPEG": ".jpg", "PNG": ".png", "HEIF": ".heic", "WEBP": ".webp", "MPO": ".jpg", "TIFF": ".tif", "GIF": ".gif"}
MIME = {".jpg": "image/jpeg", ".png": "image/png", ".heic": "image/heic", ".webp": "image/webp",
        ".tif": "image/tiff", ".gif": "image/gif"}


class PhotoError(Exception):
    pass


class PhotoLibrary:
    def __init__(self, root: Path, max_bytes: int):
        self.root, self.max_bytes = root, max_bytes
        self.originals, self.thumbs, self.tmp = root / "photos", root / "thumbs", root / "tmp"
        for d in (self.originals, self.thumbs, self.tmp):
            d.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS photos (
                     id TEXT PRIMARY KEY, lot_id INTEGER NOT NULL, filename TEXT NOT NULL,
                     original_name TEXT, size INTEGER, created_at TEXT, replaced INTEGER DEFAULT 0)"""
            )

    def _db(self):
        conn = sqlite3.connect(self.root / "photos.db")
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, lot_id: int, src: BinaryIO, original_name: str = "") -> dict:
        pid = secrets.token_hex(8)
        tmp = self.tmp / pid
        size = 0
        with open(tmp, "wb") as out:  # stream to disk, enforcing the size limit
            while chunk := src.read(1024 * 1024):
                size += len(chunk)
                if size > self.max_bytes:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise PhotoError(f"This photo is over {self.max_bytes // 1024 // 1024} MB.")
                out.write(chunk)
        try:
            with Image.open(tmp) as img:
                ext = EXT.get(img.format or "", ".img")
                if img.format == "JPEG":
                    img.draft("RGB", (THUMB_EDGE * 2, THUMB_EDGE * 2))  # decode at reduced size: much faster
                thumb = ImageOps.exif_transpose(img).convert("RGB")
                thumb.thumbnail((THUMB_EDGE, THUMB_EDGE))
                thumb.save(self.thumbs / f"{pid}.jpg", "JPEG", quality=82)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise PhotoError("That file isn't a photo this app can read. Use JPEG, PNG or HEIC.")
        lot_dir = self.originals / str(lot_id)
        lot_dir.mkdir(exist_ok=True)
        filename = f"{pid}{ext}"
        shutil.move(tmp, lot_dir / filename)
        rec = {"id": pid, "lot_id": lot_id, "filename": filename, "original_name": original_name[:200],
               "size": size, "created_at": now_iso(), "replaced": 0}
        with self._db() as c:
            c.execute("INSERT INTO photos VALUES (:id, :lot_id, :filename, :original_name, :size, :created_at, :replaced)", rec)
        return rec

    def all(self) -> list[dict]:
        with self._db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM photos ORDER BY created_at, rowid")]

    def for_lot(self, lot_id: int, include_replaced: bool = True) -> list[dict]:
        sql = "SELECT * FROM photos WHERE lot_id = ?" + ("" if include_replaced else " AND replaced = 0")
        with self._db() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY created_at, rowid", (lot_id,))]

    def current_count(self, lot_id: int) -> int:
        return len(self.for_lot(lot_id, include_replaced=False))

    def mark_replaced(self, lot_id: int) -> int:
        with self._db() as c:
            return c.execute("UPDATE photos SET replaced = 1 WHERE lot_id = ? AND replaced = 0", (lot_id,)).rowcount

    def get(self, pid: str) -> dict | None:
        with self._db() as c:
            r = c.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()
            return dict(r) if r else None

    def original_path(self, rec: dict) -> Path:
        return self.originals / str(rec["lot_id"]) / rec["filename"]

    def thumb_path(self, pid: str) -> Path:
        return self.thumbs / f"{pid}.jpg"

    def download_name(self, rec: dict) -> str:
        """lot-12-3.jpg: lot number plus position among that lot's current photos."""
        current = [p["id"] for p in self.for_lot(rec["lot_id"], include_replaced=False)]
        n = current.index(rec["id"]) + 1 if rec["id"] in current else f"old-{rec['id'][:4]}"
        return f"lot-{rec['lot_id']}-{n}{Path(rec['filename']).suffix}"

    def zip_lot(self, lot_id: int) -> io.BytesIO:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # photos are already compressed
            for i, rec in enumerate(self.for_lot(lot_id, include_replaced=False), 1):
                z.write(self.original_path(rec), f"lot-{lot_id}-{i}{Path(rec['filename']).suffix}")
        buf.seek(0)
        return buf
