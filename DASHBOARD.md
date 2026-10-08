# Tilt web dashboard — setup (v3)

v3 adds: printable brew reports + CSV export, a History tab of finished brews, batch images, an Admin page (logging interval, per-Tilt data reset, editable flavor/aroma recipe wheels, web-based software updates via `--allow-updates`), a built-in user guide at `/guide`, and a logger that takes interval changes live and buffers through network-share outages. SETUP.md is the complete, current guide — this file is kept as a dashboard-only quick reference.

`tilt_dashboard.py` is a standalone HTTP server that reads the JSON Lines log written by `tilt_logger.py` and serves a live fermentation dashboard you can open from any computer, phone, or tablet on your network. It uses only the Python standard library — nothing to install — and the page is fully self-contained (no CDN), so it works even if the Pi's network has no internet access.

## What you get

**All Tilts overview** — the landing page shows one card per Tilt **that currently has an active batch** (batch name, style, day count, current gravity, ABV, target ABV when set, temp, attenuation, last seen) plus combined gravity, Est. ABV, and temperature charts with one line per Tilt, a legend, and a synced crosshair tooltip that reads every batch at once. Each line is drawn in its Tilt's own colour. Click a card (or its tab) to open the detail view. A Tilt with no active batch — none started yet, or its last one marked finished — gets no card and no chart line, keeping the overview focused on what's actually fermenting; it still gets a colour tab up top so you can open it and start one. Finishing a batch moves its data to **History** and drops it from this live view until a new batch begins.

**Per-Tilt detail** — batch info strip (with a **Chart notes** panel beside it — see below), stat tiles (gravity with 24-hour change, temp vs target, ABV vs target ABV, attenuation vs target FG, signal, battery when reported), gravity, Est. ABV, and temperature charts with dashed target-FG, target-ABV, and target-temp reference lines, and a recent-readings table.

**Battery status** — some Tilt firmware supports a real battery-age feature (implemented by the open-source [TiltBridge](https://github.com/thorrak/tiltbridge) project, which this dashboard's logger mirrors); when a report comes through, you'll see a small gauge icon — the week count inside it, colour fading from green (just changed) to red (a year or more since) — in three places: underneath that Tilt's colour tab in the top nav bar, next to the colour and name on its All-Tilts overview card, and as a "Battery" stat tile on its detail page — plus a column in the CSV export and a line on the printable report. **The number is weeks since that Tilt's battery was last changed, not a charge percentage.** This is an unofficial feature (undocumented by Tilt Hydrometer itself) that not every Tilt or firmware version supports, so don't be surprised if it never appears for your hardware — it's a bonus when it's there, not something to rely on. No battery reading ever drops a logged measurement (see `tilt_logger.py` notes below).

**Batch details, Brewfather-style** — the "Edit batch" button opens a form for batch name, brew date, style, batch size, yeast, IBU, measured OG, target FG, target ABV, target fermentation temperature, and free-form notes. **Brew date** sets what "Day 1" is — it's pre-filled from the batch's current start (or the first logged reading, if there's no batch yet), and you can change it any time if it's ever wrong, whether you're editing an existing batch or starting a new one, and the "Day N" count everywhere (overview cards, detail page, History) recalculates from it immediately. Data is saved to a `batches.json` file next to the log (default) and survives restarts. Target ABV auto-calculates from Measured OG and Target FG as you type either one; type directly into it instead to set it manually (e.g. working backwards from a target for mead/cider) — clearing it back to blank resumes the auto-calc. The overview card and the per-Tilt detail page both show it alongside the batch's live calculated ABV.

**Recipe builder** — shown only while editing a batch that hasn't been started yet (a new Tilt's "Add batch details", or a rebrew prefill before "Start batch"); once a batch is actually started and saved, "Edit batch" opens without it. While shown, inside "Edit batch", expand "Recipe builder" for a mead calculator (math from the [rawhoneyguide.com honey mead calculator](https://rawhoneyguide.com/tools/honey-mead-calculator)) with quick-pick ABV style tiers (Session Hydromel / Standard Mead / High Gravity Sack) and sweetness tiers (Bone Dry / Semi-Sweet / Sweet-Dessert), a yeast strain picker (five common mead yeasts, highlighting ones that can finish your target ABV, dropped straight into the Yeast field with its flavor profile), and flavor/aroma ingredient wheels you click to tag a batch. "Apply OG / FG / size to batch" copies the mead calculator's results into the batch fields; "Add picks to notes" writes the flavor/aroma tags into the batch notes (and reads them back next time you open the editor).

**Batch lifecycle** — stats are scoped to the batch's time window. "Start new batch" closes the current batch and starts a fresh one from that moment, so OG, ABV, attenuation, and the "Batch" chart range all reset — no need to clear the log between brews. "Mark finished" freezes a batch when you package, moves it to **History**, and removes that Tilt's card/chart line from the live All Tilts overview (its colour tab stays, just with no current batch) until a new one is started. The first time you add details to a Tilt that has no batch yet, the batch spans all of that Tilt's existing readings.

**Chart notes** — click any point on a batch's gravity, Est. ABV, or temperature chart (detail view) to pin a permanent note to that moment. It shows as a diamond marker on all three synced charts — click a marker to read its note — and lists in a scrollable **Chart notes** panel beside the batch info card, timestamp first. Works on the live batch and on a non-trimmed past brew from History. Notes are add-only (no edit or delete) and ride along into the batch's History entry, report/CSV, and any archive; a rebrew always starts with an empty notes timeline.

**Primary start & fermentation stages** — two more ways to mark a batch's timeline, separate from chart notes. **Set primary start…** (live batch only) arms the charts so your next click sets that moment as the batch's start_ts (Day 1) — functionally the same as editing Brew date, just picked visually off the chart; a confirmation shows the exact date/time first, and nothing is deleted, so it's freely re-editable afterward. **Mark stage…** (live batch, and any non-trimmed past batch from History) lets you pick a stage name from an Admin-curated list (Admin → Fermentation stages — add/rename/remove entries the same way as Recipe wheels), then click a chart point to permanently record when the batch crossed into it; a confirmation warns this **can't be changed or removed afterward**. Stage markers render as a labeled vertical dashed line across all three synced charts (distinct from both the horizontal target lines and the note diamonds, since it marks a moment in time rather than a reading's value) and list in a **Fermentation stages** panel above Chart notes. Like chart notes, stage markers ride along into History, the report/CSV, and any archive, and never carry over to a rebrew.

**Archive & Trim** — from a finished brew's own page in History, **Archive batch** downloads a single self-contained `.html` file holding the printable report (chart notes included) plus every one of that batch's raw readings at full resolution, so the whole brew travels in one file. **Trim archived data…**, right next to it, permanently deletes just that batch's raw readings from the server to free up space (with a confirmation first, since it's not undoable) — its History entry, summary stats, and image are untouched, and once trimmed the buttons disappear from that batch's page.

**Rebrewing** — you never pick a colour for a rebrew; it's always whichever Tilt you're rebrewing onto. A Tilt with no active batch offers **Rebrew from history…** (a dropdown of every finished brew, any colour) and **Rebrew from archive…** (pick a previously-downloaded archive `.html` file; read entirely in your browser). From a past brew's own page in History, **Rebrew this batch** automatically picks a free Tilt for you (alphabetically first colour with no active batch) rather than assuming the brew's original colour — if every Tilt is busy, you're told a new batch can't start until one is finished or reset. However you get there, the editor opens pre-filled from that old brew's details (everything but the measured OG) with today as Day 1 — review and tweak, then "Start batch"; the original brew stays in History untouched. Chart notes never carry over to a rebrew — the new batch always starts with an empty notes timeline.

**Branding** — the page carries a configurable identity: a circular initials badge and display name in the header and browser tab, and a tagline in the footer, printable reports, and archive files, plus a parchment/sepia (light) and black/tan/olive (dark) theme. The name and tagline are set from Admin → Branding (two text fields, a Save button) and apply immediately, no restart or code edit needed — a fresh install with no `brand.json` yet just shows the generic default ("Tilt Dashboard").

Everything auto-refreshes every 30 seconds and follows your device's light/dark theme. Mini Pro readings display at 4-decimal resolution; standard Tilts at 3.

Formulas: ABV = (OG − SG) × 131.25 and apparent attenuation = (OG − SG) / (OG − 1) × 100 (the standard homebrewing approximations). OG is your entered "Measured OG" if set, otherwise the first reading of the batch.

**Same-colour Tilts** — two Tilts of one colour share a UUID, but each broadcast also carries the sender's Bluetooth address, which the logger records (`"address"` in every log line). The dashboard uses it to tell them apart: the first address it ever sees for a colour keeps the plain colour name (`Red`), further ones become `Red-2`, `Red-3`, … (shown as **Red #2** and drawn in a progressively lighter shade), each with its own tab, batches, History and reports. The mapping is stored in `tilts.json` beside the log. This needs the current `tilt_logger.py` as well as the dashboard — the logger is what records the address; older log lines with no address are treated as the first Tilt of their colour, so existing data is untouched.

**Thin old data (Admin)** — a log that has been running a long time (especially with the logging interval at "every beacon", which adds roughly 100,000 readings per day per Tilt) makes every page load and refresh slower. **Admin → Thin old data** shrinks the *old* part of the log: pick a cutoff (1, 2, 3, 7, 14 or 30 days) and a spacing (one reading per Tilt every 5, 10, 15 or 30 minutes) and click **Thin old data…**. A progress line shows it working. It first checks the log and tells you exactly how many readings would be removed and how much smaller the file would get — nothing changes until you confirm. Batches are not affected: batch records (names, recipes, notes, brew dates, stage markers, chart notes, History summaries) live in separate files and are never touched, and each batch keeps its first and last reading and its highest and lowest temperature, so OG, final gravity and min/max stats are unchanged. Older charts, reports and CSVs simply have fewer points. Readings newer than the cutoff, readings carrying a battery report, and any line the dashboard can't parse are left exactly as they are. Before rewriting, the log is copied to `tilt.jsonl.pre-thin.bak` beside it; once everything looks right, **Delete backup** in the same card removes it (only the most recent backup is kept, and thinning again replaces it). It needs enough free disk space for the backup, streams the file rather than loading it into memory, and may take a while on a big log on a Pi — keep the page open until it says **Done**.

## Install

Assuming you followed SETUP.md (logger in `/opt/tilt-logger`, log at `/var/log/tilt/tilt.jsonl`, `tilt` user created):

```bash
sudo cp tilt_dashboard.py /opt/tilt-logger/
sudo cp tilt-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tilt-dashboard
```

Then from any computer on the same network, open:

```
http://<pi-address>:8080/
```

Find the Pi's address with `hostname -I` on the Pi; on networks with mDNS, `http://raspberrypi.local:8080/` (your Pi's hostname) also works. Batch data lands in `/var/log/tilt/batches.json`, which the `tilt` service user can already write. Upgrading from v1: just overwrite the old `tilt_dashboard.py` and `sudo systemctl restart tilt-dashboard` — the log format is unchanged.

## Quick test without systemd

```bash
python3 tilt_dashboard.py --logfile /var/log/tilt/tilt.jsonl --port 8080
```

Options: `--port` (default 8080), `--logfile`, `--batchfile` (default: `batches.json` beside the log), `--stagesfile` (default: `stages.json` beside the log), `--brandfile` (default: `brand.json` beside the log — the Admin → Branding name/tagline), `--tiltsfile` (default: `tilts.json` beside the log — the same-colour Tilt registry), and `--host` (default `0.0.0.0`, i.e. reachable from other machines; use `127.0.0.1` to restrict to the Pi itself).

## Performance notes

The server reads the log once at startup (in the background) and afterwards only reads newly appended lines; readings are indexed per Tilt and time windows found by binary search, so refreshes stay fast on a large log. Chart data is downsampled server-side to ≤400 points per series. Log size is what slows things down — use a 1-minute (or longer) logging interval and **Admin → Thin old data** if the log has grown big (hundreds of thousands of readings). Log rotation (SETUP.md) is detected and handled automatically.

## Security note

There is no authentication. Viewing is open to your network, and so is editing batch names/notes (the log itself can't be altered through the dashboard). That's fine on a home LAN; don't port-forward it to the open internet — for remote access use a VPN (WireGuard/Tailscale) or a reverse proxy with auth.

## API

```
GET  /api/overview?hours=168          Tilts WITH an active batch: batch, stats, series each
                                       (every known colour still appears under "colors")
GET  /api/data?color=Red&hours=0      one Tilt in detail (+50 recent readings); empty
                                       stats/series if that Tilt has no active batch
POST /api/tilts                       {"action":"rename"|"forget","key":"Red-2","name":...}
POST /api/thin                        {"days":7,"minutes":10,"dry_run":true|false}
POST /api/thin/backup/delete          deletes tilt.jsonl.pre-thin.bak
POST /api/batch                       {"action":"save"|"new"|"finish",
                                       "color":"Red","batch":{"name":...}}
```

`hours=0` (default) means the active batch's window, or nothing if no batch is currently active (see "All Tilts overview" above — start a batch, or look the Tilt up in `/api/history`, to get data for it). Batch fields: `name`, `style`, `batch_size`, `yeast`, `notes` (strings) and `ibu`, `og_override`, `target_fg`, `target_abv`, `temp_target_f` (numbers). A batch also carries `annotations` (its chart notes) and `stage_markers` (its fermentation-stage markers) — both read-only here, set via `POST /api/batch/annotate` and `POST /api/batch/stage` below; neither is ever inherited by a rebrew's new batch.

Reports and CSV follow the same rule: `GET /report?color=Red` and `GET /api/export.csv?color=Red` come back empty for a Tilt with no active batch — pass `id=<batch_id>` (from `/api/history`) instead to get a specific past batch's report or CSV.

```
GET  /api/archive?id=<batch_id>       downloads a finished batch as a self-contained
                                       .html archive (report + full raw readings)
POST /api/archive/trim                {"id": "<batch_id>"} -- permanently erases that
                                       finished batch's raw readings (keeps History)
POST /api/batch/annotate              {"id":..., "ts":..., "text":...} -- pin a
                                       permanent chart note to one reading's
                                       timestamp (add-only: no edit or delete)
GET  /api/stages                      current Admin-curated fermentation stage list
POST /api/stages                      {"action":"add"|"rename"|"delete"|"reset", ...}
POST /api/batch/stage                 {"id":..., "ts":..., "stage":...} -- pin a
                                       permanent fermentation-stage marker to one
                                       timestamp (add-only: no edit or delete)
POST /api/brand                       {"name":..., "tagline":...} -- sets the
                                       dashboard's display name/tagline (Admin ->
                                       Branding); applies immediately, no restart
```

Every JSON endpoint also includes a `"battery"` object (`{"Red": {"weeks": 12, "ts": 1234567890}, ...}`) with weeks since each Tilt colour's battery was last changed, when any has been reported.
