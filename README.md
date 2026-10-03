# LAB Lot Tracker

A phone-friendly tracker for the Love-A-Bull silent auction on 32Auctions. Volunteers open a lot's link, take photos, tick off steps, and flag what's still needed. Progress and flags are saved to the LAB Google spreadsheet; photos are kept at full size on the server.

Each lot goes through:

1. Listed in 32Auctions
2. Photographed (digital lots: Image added). Ticks itself when the first photo uploads.
3. Photos uploaded to 32A (digital lots: Image uploaded to 32A)
4. Description checked
5. Complete (manual sign-off)

Any lot can have open flags. "Needs retake" hides the current photos, unticks steps 2 and 3, and adds a flag.

32Auctions has no API, so steps 1 and 3 are always done in 32Auctions by hand and then ticked here.

## Run it

You need Docker. Then:

```bash
cp .env.example .env
```

Set `TRACKER_TOKEN` in `.env` (make one with `openssl rand -hex 16`), then:

```bash
docker compose up -d --build
```

Open `http://localhost:8000/t/<TRACKER_TOKEN>/`.

With `GOOGLE_SHEET_ID` empty, it runs in **local mode**. It uses the sample lots in `sample/lots.csv` and saves progress inside the container's data volume. This is useful for trying it out before connecting the sheet.

Every page and file lives under `/t/<TRACKER_TOKEN>/`, and any other address returns "not found". Anyone with the link can use the tracker. To cut off old links, change `TRACKER_TOKEN` and run `docker compose up -d`.

## Connect the Google spreadsheet

1. In [Google Cloud console](https://console.cloud.google.com/), create a project (or use an existing one) and enable the **Google Sheets API**.
2. Under **IAM & Admin → Service accounts**, create a service account. You don't need to give it any roles. Open it, go to **Keys → Add key → JSON**, and save the file as `secrets/google-service-account.json` in this folder.
3. Open the LAB spreadsheet and click **Share**. Add the service account's email (it ends in `iam.gserviceaccount.com`) as an **Editor**.
4. In `.env`, set `GOOGLE_SHEET_ID` to the long ID in the spreadsheet's URL: `https://docs.google.com/spreadsheets/d/<THIS PART>/edit`.
5. Run `docker compose up -d`.

On first start the tracker adds two tabs to the spreadsheet:

- **Tracker:** one row per lot, with a checkbox for each step, Needs retake, and Photo count. Rows are added the first time a lot is touched. You can tick boxes here too, and the app picks the changes up within about 20 seconds.
- **Flags:** one row per flag, with the lot, comment, when it was raised and when it was resolved.

The tracker reads lots from the bundle tab: the first tab whose header row has `Lot ID` and `Lot Title`. To use a specific tab, set `BUNDLE_SHEET_NAME`. It also uses `Subtitle`, `Lot Description` and `Physical or Virtual` (`Physical` or `Virtual`; anything else shows as "Type not set"). Rows with no title, or with `#N/A`, are skipped. If your headers differ, set the `COL_*` variables in `.env`.

To make "Open in 32Auctions" go to each lot's own page, add a `32Auctions URL` column to the bundle tab with each item's link. Otherwise the button goes to `AUCTION_URL`.

### Starting flags

To load the open items from the listings handoff as flags:

```bash
docker compose run --rm tracker python -m app.seed_flags seed/initial-flags.csv
```

Running it again skips flags that are already open.

## Put it online with Cloudflare Tunnel

1. In the Cloudflare dashboard, go to **Zero Trust → Networks → Tunnels** and create a tunnel. Copy its token into `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.
2. Add a public hostname to the tunnel (e.g. `lots.example.org`) with service `http://tracker:8000`.
3. Start both containers:

```bash
docker compose --profile tunnel up -d --build
```

Then share `https://lots.example.org/t/<TRACKER_TOKEN>/`. The **Lot links** page lists a link for every lot, with a button to copy them all.

Port 8000 is only published on `127.0.0.1`, so the tunnel is the only way in from outside.

## Photos

- Originals are stored exactly as uploaded, one photo per request. Cloudflare's free plan allows up to 100 MB per request, and the app's own limit is `MAX_PHOTO_MB`, which defaults to 100.
- iPhone HEIC photos are accepted. When photos are picked in the browser, iPhones usually convert them to JPEG.
- **Download photos** on a lot gives a zip of its current photos, named `lot-12-1.jpg` and so on. On a phone, tap a photo to open it full size, then save it.
- Photos and their index live in the `tracker-data` Docker volume. To back it up:

```bash
docker run --rm -v lab-silent-auction_tracker-data:/data -v "$PWD":/backup alpine tar czf /backup/tracker-data.tgz -C /data .
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
TRACKER_TOKEN=dev-token-0123456789 .venv/bin/uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000/t/dev-token-0123456789/>.

| Path | What it is |
|---|---|
| `app/main.py` | Routes and API |
| `app/stores.py` | Google Sheets and local-mode storage |
| `app/photos.py` | Photo storage and previews |
| `app/static/index.html` | The whole interface |
| `tests/` | Tests, including a stand-in for the Google Sheets API |
