import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main, stores
from app.stores import SheetsStore, lots_from_rows

from .fake_sheets import FakeSpreadsheet

B = "/t/test-token-0123456789"
client = TestClient(main.app)


def jpeg(color=(200, 80, 40), size=(1200, 900)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def lot(state, i):
    return next(l for l in state["lots"] if l["id"] == i)


# ---------------------------------------------------------------- pages & access

def test_pages_need_the_token():
    assert client.get("/").status_code == 404
    assert client.get("/t/wrong-token-0123456789/").status_code == 404
    assert client.get("/t/wrong-token-0123456789/api/state").status_code == 404
    for p in ("/", "/links", "/lot/12"):
        r = client.get(B + p)
        assert r.status_code == 200 and "LAB Lot Tracker" in r.text
    assert client.get("/healthz").text == "ok"


def test_state_reads_sample_lots():
    s = client.get(B + "/api/state").json()
    assert s["source"] == "local" and len(s["lots"]) == 12
    l1 = lot(s, 1)
    assert l1["title"] == "Coffee Lover's Basket" and l1["type"] == "physical"
    assert lot(s, 3)["type"] == "digital" and lot(s, 8)["type"] == "unset"
    assert lot(s, 12)["stages"] == {k: False for k in stores.STAGES}


# ---------------------------------------------------------------- workflow

def test_photo_upload_ticks_photographed_and_keeps_original():
    data = jpeg()
    r = client.post(B + "/api/lots/5/photos", files={"photo": ("IMG_0001.JPG", data, "image/jpeg")})
    assert r.status_code == 200, r.text
    photo = r.json()["photo"]
    s = client.get(B + "/api/state").json()
    assert lot(s, 5)["stages"]["photographed"] is True
    orig = client.get(photo["url"])
    assert orig.content == data  # byte-for-byte, not shrunk
    assert 'filename="lot-5-1.jpg"' in orig.headers["content-disposition"]
    thumb = client.get(photo["thumb"])
    assert thumb.status_code == 200 and max(Image.open(io.BytesIO(thumb.content)).size) <= 640


def test_rejects_non_images_and_unknown_lots():
    r = client.post(B + "/api/lots/5/photos", files={"photo": ("x.jpg", b"not an image", "image/jpeg")})
    assert r.status_code == 400 and "isn't a photo" in r.json()["detail"]
    assert client.post(B + "/api/lots/999/photos", files={"photo": ("a.jpg", jpeg(), "image/jpeg")}).status_code == 404
    assert client.post(B + "/api/lots/999/stages", json={"stage": "listed", "done": True}).status_code == 404
    assert client.post(B + "/api/lots/1/stages", json={"stage": "nope", "done": True}).status_code == 400


def test_stage_ticks_round_trip():
    assert client.post(B + "/api/lots/1/stages", json={"stage": "listed", "done": True}).status_code == 200
    assert client.post(B + "/api/lots/1/stages", json={"stage": "complete", "done": True}).status_code == 200
    st = lot(client.get(B + "/api/state").json(), 1)["stages"]
    assert st["listed"] and st["complete"] and not st["uploaded"]
    client.post(B + "/api/lots/1/stages", json={"stage": "complete", "done": False})
    assert not lot(client.get(B + "/api/state").json(), 1)["stages"]["complete"]


def test_retake_hides_photos_unticks_and_flags():
    for _ in range(2):
        client.post(B + "/api/lots/8/photos", files={"photo": ("a.jpg", jpeg(), "image/jpeg")})
    client.post(B + "/api/lots/8/stages", json={"stage": "uploaded", "done": True})
    r = client.post(B + "/api/lots/8/retake", json={"reason": "blurry"})
    assert r.status_code == 200 and r.json()["flag"]["text"] == "Needs retake: blurry"
    s = client.get(B + "/api/state").json()
    l8 = lot(s, 8)
    assert l8["retake"] and not l8["stages"]["photographed"] and not l8["stages"]["uploaded"]
    assert all(p["replaced"] for p in l8["photos"])
    assert client.get(B + "/api/lots/8/photos.zip").status_code == 404  # nothing current
    client.post(B + "/api/lots/8/photos", files={"photo": ("b.jpg", jpeg((0, 0, 255)), "image/jpeg")})
    l8 = lot(client.get(B + "/api/state").json(), 8)
    assert l8["stages"]["photographed"] and not l8["retake"]
    z = zipfile.ZipFile(io.BytesIO(client.get(B + "/api/lots/8/photos.zip").content))
    assert z.namelist() == ["lot-8-1.jpg"]


def test_flags_add_and_resolve():
    f = client.post(B + "/api/lots/3/flags", json={"text": "Missing the gift card"}).json()
    assert f["lot_id"] == 3 and not f["resolved_at"]
    assert client.post(B + "/api/lots/3/flags", json={"text": "   "}).status_code == 400
    r = client.post(B + f"/api/flags/{f['id']}/resolve")
    assert r.status_code == 200 and r.json()["resolved_at"]
    assert client.post(B + "/api/flags/doesnotexist/resolve").status_code == 404


# ---------------------------------------------------------------- Google Sheets store

BUNDLE = [
    ["Bundle SKU", "Lot ID", "Lot Title", "Lot Value", "Subtitle", "Lot Description", "Physical or Virtual"],
    ["001 - Coffee", "1", "Coffee Lover's Basket", "60", "Example Coffee Roasters", "Two bags of coffee.", "Physical"],
    ["002 - Dinner", "2", "Dinner for Two", "100", "Example Bistro", "A $100 gift card.", "Virtual"],
    ["#N/A", "#N/A", "#N/A", "", "", "", ""],
    ["", "3", "", "", "", "", ""],
]


@pytest.fixture
def sheet():
    sh = FakeSpreadsheet({"Donors": [["Business", "Fair Market Value"]], "Bundles": BUNDLE})
    return sh, SheetsStore(sh)


def test_sheets_creates_tabs_and_finds_bundle_tab(sheet):
    sh, store = sheet
    titles = [w.title for w in sh.worksheets()]
    assert "Tracker" in titles and "Flags" in titles
    assert any("setDataValidation" in r for r in sh.requests)  # checkboxes
    snap = store.snapshot(force=True)
    assert store.bundle_title == "Bundles"
    assert [(l.id, l.type) for l in snap["lots"]] == [(1, "physical"), (2, "digital")]


def test_sheets_writes_progress_and_flags(sheet):
    sh, store = sheet
    store.snapshot(force=True)
    store.set_fields(2, {"listed": True})
    store.set_fields(2, {"photographed": True, "photos": 3})
    tracker = next(w for w in sh.worksheets() if w.title == "Tracker").get_all_values()
    assert tracker[0][:3] == ["Lot ID", "Lot Title", "Listed in 32A"]
    assert len(tracker) == 2 and tracker[1][:4] == ["2", "Dinner for Two", "TRUE", "TRUE"]

    flag = store.add_flag(1, "=HYPERLINK(\"x\")  needs value")  # stored as text, not a formula
    store.resolve_flag(flag["id"])
    fresh = SheetsStore(sh).snapshot(force=True)  # a second server reading the same sheet
    assert fresh["progress"][2]["listed"] and fresh["progress"][2]["photos"] == 3
    assert fresh["flags"][0]["text"].startswith("=HYPERLINK") and fresh["flags"][0]["resolved_at"]


def test_sheets_survives_reordered_rows_and_columns(sheet):
    sh, store = sheet
    store.snapshot(force=True)
    store.set_fields(1, {"listed": True})
    store.set_fields(2, {"listed": True})
    ws = next(w for w in sh.worksheets() if w.title == "Tracker")
    ws.rows = [ws.rows[0], ws.rows[2], ws.rows[1]]  # someone sorted the tab
    ws.rows = [r[::-1] for r in ws.rows]  # ...and reversed the columns
    store.set_fields(1, {"checked": True})
    fresh = SheetsStore(sh).snapshot(force=True)["progress"]
    assert fresh[1]["checked"] and not fresh[2]["checked"]


def test_sheets_serves_last_copy_when_google_fails(sheet):
    sh, store = sheet
    store.snapshot(force=True)
    sh.values_batch_get = lambda ranges: (_ for _ in ()).throw(RuntimeError("quota"))
    snap = store.snapshot(force=True)
    assert len(snap["lots"]) == 2 and "quota" in snap["error"]


def test_bundle_rows_with_title_rows_above_header():
    rows = [["Spring Auction 2026"], [], ["Lot ID", "Lot Title"], ["007", "Seven"]]
    assert [(l.id, l.title) for l in lots_from_rows(rows)] == [(7, "Seven")]


def test_heic_upload_kept_as_heic_with_jpeg_preview():
    buf = io.BytesIO()
    try:
        Image.new("RGB", (800, 600), (10, 120, 90)).save(buf, "HEIF")
    except Exception:
        pytest.skip("this pillow-heif build can't encode HEIF")
    r = client.post(B + "/api/lots/11/photos", files={"photo": ("IMG_0002.HEIC", buf.getvalue(), "image/heic")})
    assert r.status_code == 200, r.text
    orig = client.get(r.json()["photo"]["url"])
    assert orig.content == buf.getvalue() and orig.headers["content-type"] == "image/heic"
    assert client.get(r.json()["photo"]["thumb"]).headers["content-type"] == "image/jpeg"
