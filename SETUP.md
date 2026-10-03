# Tilt logger & dashboard

## Complete Raspberry Pi 3B+ setup guide

End-to-end setup for reading a Tilt / Tilt Pro / Tilt Mini Pro hydrometer with a Raspberry Pi 3B+, logging every reading to a JSON file, and serving a web dashboard to any computer, phone, or tablet on your network. The dashboard's display name and tagline are set from its own Admin page (Admin → Branding) — no code edits needed to make it your own.

**What you'll end up with:**

| Piece | What it does | Runs as |
|---|---|---|
| `tilt_logger.py` | Listens for Tilt Bluetooth beacons, appends each reading to `/var/log/tilt/tilt.jsonl` | `tilt-logger` systemd service |
| `tilt_dashboard.py` | Web dashboard at `http://<pi>:8080/` — multi-Tilt overview, charts, Brewfather-style batch details | `tilt-dashboard` systemd service |

**Files you need on hand** (copy them to a folder on the Pi, e.g. `~/tilt/`): `tilt_logger.py`, `tilt_dashboard.py`, `tilt-logger.service`, `tilt-dashboard.service`, `tilt-logger-watch.path`, `tilt-logger-watch.service` (the last two let the logger restart itself after a web update).

---

## Background: how the Tilt broadcasts

The Tilt transmits Apple iBeacon advertisements over Bluetooth LE. A 16-byte UUID identifies the Tilt's colour; the iBeacon *major* field is temperature in °F and *minor* is specific gravity × 1000. Pro models (Tilt Pro, Mini Pro) broadcast at higher resolution — temperature × 10 and gravity × 10000 — and receivers detect a Pro when the raw gravity value is ≥ 5000. Both scripts auto-detect this, so any mix of Tilts works. Two Tilts of the **same colour can't be told apart** (they share a UUID), so simultaneous batches need different colours.

**Battery age (unofficial, not a charge percentage):** the iBeacon's final byte is normally a fixed, negative TX-power calibration constant. Some Tilt firmware supports a real, intentional battery feature on top of this (implemented by the open-source [TiltBridge](https://github.com/thorrak/tiltbridge) project): that firmware broadcasts a TX power of exactly &minus;59&nbsp;dBm once as an "I support battery reporting" marker, and from then on repurposes the same byte to report **weeks since the battery was last changed** — not a charge level, and not bounded to 0&ndash;100. Both scripts implement the same two-state detection TiltBridge uses (see the `tilt_logger.py` module docstring), it never costs you a gravity/temperature reading (see Step 4/the log format below), and it's a nice-to-have that will simply never appear for hardware/firmware that doesn't support it.

Sources: [TiltBridge source — tiltHydrometer.cpp/.h](https://github.com/thorrak/tiltbridge/blob/master/src/tilt/tiltHydrometer.cpp) (the battery state machine), [kvurd.com — Tilt iBeacon data format](https://kvurd.com/blog/tilt-hydrometer-ibeacon-data-format/) (the base major/minor/UUID field layout only — it does not cover battery reporting), [Tilt Pro product page](https://tilthydrometer.com/products/tilt-pro-wireless-hydrometer-and-thermometer).

---

## Step 1 — Prepare the Pi

Start from a current Raspberry Pi OS (Lite is fine; these steps assume Bookworm or newer). The 3B+ has built-in Bluetooth, and modern Raspberry Pi OS ships BlueZ with BLE enabled — the `--experimental` flag from older guides is no longer needed.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-pip bluez
```

Confirm the radio is up and can see the Tilt. The Tilt only broadcasts while tilted or floating (not flat in its box) and within roughly 10 m:

```bash
hciconfig                      # hci0 should say UP RUNNING
sudo bluetoothctl scan le      # watch for a device named "Tilt"; Ctrl+C to stop
```

## Step 2 — Install the files

```bash
cd ~/tilt                                  # wherever you copied the four files
sudo mkdir -p /opt/tilt-logger /var/log/tilt
sudo cp tilt_logger.py tilt_dashboard.py /opt/tilt-logger/
sudo pip3 install bleak --break-system-packages
```

(`--break-system-packages` is required on Bookworm because pip refuses system-wide installs otherwise; alternatively create a venv and point the service files' `ExecStart` at its python. The dashboard needs no packages at all — standard library only.)

## Step 3 — Create the service user

Both services run as an unprivileged `tilt` user rather than root:

```bash
sudo useradd -r -s /usr/sbin/nologin tilt
sudo chown tilt:tilt /var/log/tilt
sudo chown -R tilt:tilt /opt/tilt-logger   # lets the Admin page install updates
```

## Step 4 — Test interactively (recommended)

Logger — you should see readings within seconds:

```bash
sudo python3 /opt/tilt-logger/tilt_logger.py --logfile /tmp/tilt-test.jsonl
# wait ~10 seconds, Ctrl+C, then:
cat /tmp/tilt-test.jsonl
```

Dashboard — then open `http://<pi-address>:8080/` from another computer:

```bash
python3 /opt/tilt-logger/tilt_dashboard.py --logfile /tmp/tilt-test.jsonl --port 8080
```

Find the Pi's address with `hostname -I`; on networks with mDNS, `http://raspberrypi.local:8080/` (your Pi's hostname) also works.

## Step 5 — Install both services

```bash
sudo cp tilt-logger.service tilt-dashboard.service /etc/systemd/system/
sudo cp tilt-logger-watch.path tilt-logger-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tilt-logger tilt-dashboard tilt-logger-watch.path
```

(`tilt-logger-watch.path` watches `/opt/tilt-logger/tilt_logger.py` and restarts the logger service automatically whenever the Admin page installs a new version — the dashboard itself restarts on its own.)

Check them:

```bash
systemctl status tilt-logger tilt-dashboard
journalctl -u tilt-logger -f          # follow the logger's status lines
```

Both start on boot and restart on failure. The logger unit grants itself `CAP_NET_RAW`/`CAP_NET_ADMIN` so BLE scanning works without root, scoped to that service only.

## Step 6 — Set up log rotation

Every-beacon logging produces roughly 4–20 MB per day per Tilt. Create `/etc/logrotate.d/tilt`:

```
/var/log/tilt/tilt.jsonl {
    weekly
    rotate 12
    compress
    copytruncate
    missingok
}
```

The dashboard detects rotation and reparses automatically. If you'd rather log less, edit the `ExecStart` line in `/etc/systemd/system/tilt-logger.service` to add `--interval 60` (one reading per minute per Tilt) or `--interval 900` (15 min), then `sudo systemctl daemon-reload && sudo systemctl restart tilt-logger`.


## Step 7 — Optional: log to a network share (NAS)

To keep the log on a NAS or file server instead of the SD card, mount the share and point both services at it. Example for a CIFS/SMB share, in `/etc/fstab`:

```
//nas.local/brewing  /mnt/brewshare  cifs  credentials=/etc/samba/brew.cred,uid=tilt,gid=tilt,iocharset=utf8,x-systemd.automount,_netdev  0  0
```

(`uid=tilt,gid=tilt` makes the mount writable by the service user; `x-systemd.automount,_netdev` mounts it when the network is up. For NFS use `nfs` with `x-systemd.automount,_netdev` and export the share to the Pi. Install `cifs-utils` for SMB.) Then edit both service files' `ExecStart` lines to use `--logfile /mnt/brewshare/tilt.jsonl`, add `RequiresMountsFor=/mnt/brewshare` under each `[Unit]` section, and `sudo systemctl daemon-reload && sudo systemctl restart tilt-logger tilt-dashboard`. The batches, settings, and image data follow the log automatically (they live beside it).

If the share drops out, the logger buffers up to 20,000 readings in memory and writes them all once the share returns, so short outages lose nothing.

---

## Using the dashboard

Open `http://<pi-address>:8080/` from anything on your network.

**All Tilts overview** — the landing page: one card per Tilt **that currently has an active batch** (batch name, style, day count, current gravity, ABV, target ABV when set, temp, attenuation) plus combined gravity, Est. ABV, and temperature charts, one line per Tilt in its own colour, with a legend and a synced crosshair tooltip. Click a card or tab for detail. A Tilt with no active batch — never started one, or its last one was marked finished — gets no card and no line on the charts, so the overview only ever shows what's actually brewing right now; its colour tab still appears in the top nav so you can open it and start (or restart) a batch. If every known Tilt is batch-less, the overview shows a short "No active batches" message instead of an empty page.

**Per-Tilt detail** — batch info strip (with a **Chart notes** panel alongside it — see below), stat tiles (gravity with 24-h change, temp vs target, ABV vs target ABV, attenuation vs target FG, signal, battery when reported), gravity/Est. ABV/temperature charts with dashed target-FG / target-ABV / target-temp reference lines, and a recent-readings table.

**Battery status** — when a Tilt has reported its battery age (see "Background" above — this is an unofficial, occasional signal, not guaranteed on every Tilt), you'll see the same small gauge icon (the week count inside it, fill colour fading continuously from green when recently changed to red as it gets stale) in five places: underneath that Tilt's colour tab in the top nav bar, next to the colour and name on its All-Tilts overview card, a "Battery" stat tile on the per-Tilt detail page, a `battery_weeks` column in the CSV export, and a line on the printable report. The number is **weeks since that Tilt's battery was last changed, not a charge percentage** — the icon starts full and green right after a change, then drains and shifts toward red as it goes a year (52 weeks) or more without one. If you never see it for a given Tilt, that just means your unit/firmware hasn't broadcast one — it's not a sign anything's broken.

**Batch details (Brewfather-style)** — "Edit batch" opens a form: name, brew date, style, batch size, yeast, IBU, measured OG, target FG, target ABV, target ferm temp, and notes. Saved to `/var/log/tilt/batches.json`; survives restarts. **Brew date** is what "Day 1" is counted from everywhere (overview cards, detail page, History) — it's pre-filled from the batch's current start (or the first logged reading, if there's no batch yet), and you can correct it any time, whether logging started a bit before or after you actually pitched. It also applies when you use "Start new batch" or "Rebrew this batch" — set it there before starting to begin the new batch's Day 1 on the date you actually brewed rather than the moment you clicked the button. Target ABV auto-calculates from Measured OG and Target FG as you type either one (the same (OG − FG) × 131.25 formula used for the batch's live ABV), so for most brews you never have to type it yourself — just enter your OG and target FG and it fills in. Typing directly into Target ABV switches it to a manual value for the rest of that edit (handy for mead/cider where you pick a target ABV first and work backwards); clearing it back to blank re-enables the auto-calc. It shows in two places once set: on the All-Tilts overview card next to the batch's current calculated ABV (or on its own, "no readings yet · target X% ABV", before readings come in), and on the per-Tilt detail page's "Est. ABV" tile ("from OG 1.060 · target X%"). The Recipe builder's "Apply" button will also fill it in from the calculator's target ABV if you haven't already set one by hand.

**Recipe builder** — shown only while editing a batch that hasn't been started yet (a brand-new Tilt's "Add batch details", or a rebrew prefill before you click "Start batch") — once a batch is actually started and saved, "Edit batch" opens without it, so editing an in-progress brew stays focused on the brew itself. While it's shown, inside "Edit batch", expand "Recipe builder" for four mead-focused tools: a **mead calculator** (math from the [rawhoneyguide.com honey mead calculator](https://rawhoneyguide.com/tools/honey-mead-calculator)) with quick-pick style tiers (Session Hydromel 3–7% / Standard Mead 8–13% / High Gravity Sack 14–18%+ ABV), sweetness tiers (Bone Dry / Semi-Sweet / Sweet-Dessert with their FG ranges), and honey variety (dark ~32 / wildflower ~35 / light ~37 PPG), computing honey weight (lb/kg/oz), estimated OG, water to add, and a yeast suggestion — "Apply OG / FG / size to batch" copies the results into the batch fields and a recipe line into notes; a **yeast strain** picker (Lalvin D47, EC-1118, 71B, SafAle US-05, Mangrove Jack's M05) that highlights strains able to finish your target ABV and drops the chosen name into the Yeast field with its full flavor profile; and **flavor** and **aroma wheels** — color-coded ingredient categories (honeys, fruits/acids, spices, earthy/tannic notes for flavor; floral, citrus, roasted, and resinous/herbal notes for aroma) you click to tag a batch. "Add picks to notes" writes them as "Flavor picks:"/"Aroma picks:" lines in the batch notes, which are read back the next time you open the editor.

**Recipe warnings** — as you adjust the calculator, yeast, and flavor wheel, an advisory box can appear flagging combinations known to risk trouble, with the reasoning spelled out: a picked yeast whose ABV tolerance falls short of your target (with alternatives that cover it); a target ABV above all five strains' typical range; a very high starting gravity (≥ 1.130 OG) that can stall fermentation regardless of yeast; capsaicin heat or heavy roasted bitterness picked alongside a Bone Dry finish, which has no residual sweetness to balance them; and several high-intensity flavor picks stacked together. These are advisory only — nothing is blocked, and Save/Apply work the same either way.

**Recipe explorer (`/recipes`)** — a standalone page, linked from the dashboard footer, for sketching mead recipes without opening a Tilt's batch first. It has the same mead calculator, yeast picker, flavor/aroma wheels, and recipe warnings as the batch editor's Recipe builder, plus a small saved-recipe library: **Save recipe** names and stores a draft (in `recipe-drafts.json` beside the log) that you can reopen, edit, duplicate, or delete later from the recipe list; **Copy summary** writes the whole recipe — style, calculator results, yeast, flavor/aroma picks, and notes — to your clipboard, ready to paste into a real batch's notes on brew day. Drafts are entirely independent of any Tilt or batch until you copy one over, and edits to a saved draft don't touch anything live.

**Batch lifecycle** — stats are scoped to the batch window. **Start new batch** closes the current batch and starts fresh from that moment (OG/ABV/charts reset — no clearing the log between brews). **Mark finished** freezes a batch at packaging and moves it to the **History** tab — and, since there's no longer a current batch, that Tilt's card and chart line disappear from the live All Tilts overview too (its colour tab stays, just with nothing plotted) until a new batch is started on it. The first time you add details to a Tilt with no batch, the batch spans all its existing readings.

**Brew images** — the batch editor accepts an image (photo or label art); it's resized in the browser and shown on the batch tile, in History, and in the detail header.

**Chart notes** — on a batch's detail view, click any point on the gravity, Est. ABV, or temperature chart to pin a permanent note to that exact moment (a dry hop, a temperature change, a krausen drop, whatever's worth remembering). It shows up as a small diamond marker on all three synced charts — click an existing marker to read its note — and in a scrollable **Chart notes** panel next to the batch info card, earliest first with its timestamp. Works on the live batch and on a past brew from History too, as long as its raw data hasn't been trimmed (trimming removes the charts themselves). Notes are **add-only — there's no edit or delete** — by design, so the timeline stays a trustworthy record; they ride along with the batch into its History entry, printable report/CSV, and any archive, and a rebrew always starts with an empty notes timeline even though everything else pre-fills.

**Set primary start…** — on the live batch's toolbar, this lets you set "Day 1" by clicking the actual chart rather than typing a date into Edit batch. Click the button, a banner tells you to click a point on a chart below, and whichever point you click becomes the batch's new start — the same confirmation shows the exact date/time first. It's really just Brew date, picked visually: readings from before that point stop counting toward the batch's stats and charts (nothing is deleted, so you can change it again later, same as editing Brew date always worked). Handy when the Tilt was logging for a while before you'd actually call it "pitched" — set primary start once fermentation visibly begins and the erroneous early readings quietly drop out of the window.

**Mark stage…** — a separate, permanent record of when a batch crossed into a later fermentation stage (secondary, bulk aging, oak aging, bottle conditioning, or whatever your Admin-curated list includes — see "Fermentation stages (Admin)" below). Click the button, pick a stage from the list, then click a point on a chart — a confirmation warns this **can't be changed or removed afterward** before it's saved. Available on the live batch and on any non-trimmed past brew from History. Once set, it shows as a labeled vertical dashed line across all three synced charts (visually distinct from both the horizontal target-reference lines and the chart-notes diamonds, since it marks a moment in the timeline rather than one reading's value) and lists in a **Fermentation stages** panel above Chart notes, earliest first. Like chart notes, stage markers ride along into History, the printable report/CSV, and any archive, and never carry over to a rebrew.

**Fermentation stages (Admin)** — the list of stage names offered by "Mark stage…" is curated once, server-wide, from Admin → Fermentation stages — add, rename, or remove entries the same way you manage Recipe wheels. Renaming or removing a stage from this list never rewrites markers already recorded on a batch (the stage name is copied as plain text at the moment it's picked), so past records stay intact even as the list evolves. "Reset to built-in" restores the shipped defaults (Secondary fermentation, Bulk aging, Oak aging, Bottle conditioning). Saved to `stages.json` beside the log and survives restarts and software updates.

**Reports & CSV** — every batch (live or finished) has **Export report**, a printable brew report with details, stats, gravity/Est. ABV/temperature charts (with any chart notes plotted as markers and any fermentation-stage markers as labeled vertical lines, plus timestamped lists of each), batch notes, and an hourly data table (print to PDF from the browser), and **CSV**, every raw reading for spreadsheets. Both respect the same active-batch boundary as the live dashboard: for a Tilt with no current batch, Export report/CSV from its colour tab come back empty rather than dumping all its historical readings — open the batch from **History** instead to get its report/CSV scoped to that specific past brew.

**History** — the History tab lists all finished brews with image, dates, duration, OG→FG, ABV and attenuation; click one to revisit it or export its report/CSV. A batch's **summary stats** (OG, FG, ABV, attenuation, reading count, battery) are snapshotted the moment it's finished, so the History card — and a report pulled up by that specific brew (not by colour) — keeps showing them even after the raw log data behind them is gone (log rotation, a **Reset Tilt**, or a deliberate **Trim**, below). The **charts, hourly table, and CSV export need the raw readings themselves**, though, so if those are already gone, the report opens fine but shows a message instead of a chart, and CSV comes back empty — export a report/CSV (or make an archive) *before* resetting a Tilt or letting old log data rotate out if you want to keep the full detail, not just the summary. "Select brews…" enters selection mode to permanently delete chosen brews from History (summaries and images are removed; any surviving logged readings stay until rotation or a Tilt reset).

**Archive & Trim** — from a finished brew's own page in History, **Archive batch** downloads one self-contained `.html` file: the same printable report (chart notes and all), plus every one of that batch's raw readings at full resolution embedded in it, so the whole brew — summary and complete log — travels in a single file you can keep, email, or store off the Pi. Downloading an archive doesn't touch anything on the server by itself. To actually free up space on the Pi, use **Trim archived data…** right next to it: it permanently deletes just that one batch's raw readings from the log, leaving its History entry, summary stats, and image exactly as they were. You're asked to confirm first — spelling out the batch, Tilt, and date range — because trimming is not undoable; make sure you've downloaded an archive (or don't need the detail) before confirming. Once a batch is trimmed, its report explains the charts/table/CSV are gone and points to the archive for the full data, and the Archive/Trim buttons disappear from that batch's page, since there's nothing left to archive or trim a second time.

**Starting a new batch from an old one** — you never pick a colour for a rebrew; it's always whichever Tilt you're rebrewing onto. There are three ways in:

- From a Tilt with **no active batch**, its page offers **Rebrew from history…** (a dropdown of every finished brew across every colour — pick one and it's pre-filled onto this Tilt) and **Rebrew from archive…** (pick an archive `.html` file you downloaded earlier; the dashboard reads the batch details straight out of the file in your browser — nothing is uploaded anywhere, so this works even for a batch that's since been trimmed or whose Tilt has since been reset).
- From a past brew's own page in **History**, **Rebrew this batch** picks a free Tilt for you automatically — whichever known colour currently has no active batch, alphabetically first — rather than assuming the brew's original colour, so you don't have to go find an open one yourself. If every Tilt currently has something brewing, you're told plainly that a new batch can't start until one is finished or reset.

However you get there, the editor opens pre-filled from that old brew's name, style, batch size, yeast, IBU, target FG, target ABV, target ferm temp, image, and notes (including any flavor/aroma picks) — everything except the measured OG, which is brew-specific and left blank for you to re-enter on brew day — with today's date as the new batch's Day 1. **Chart notes don't carry over** — the new batch always starts with an empty notes timeline. Review or tweak the fields, then **Start batch**. If the destination Tilt already has a batch in progress, starting the rebrew closes it first, and you're warned before it happens; the original brew stays untouched in History either way.

**Admin page** — set the dashboard's **Branding** (display name and tagline — see below), change the logging interval (every beacon up to hourly; the logger applies it within ~5 seconds, no restart), see per-Tilt data counts, export or **Reset** a Tilt (erases its readings and current batch so the next brew starts clean — cannot be undone), edit the **Recipe wheels** and **Fermentation stages** (see below for each), and install new versions of `tilt_dashboard.py` / `tilt_logger.py` from the browser.

**Branding (Admin)** — the dashboard's display name and tagline (shown in the header badge, the browser tab, printable reports, and archive files) are set from Admin → Branding, two plain text fields with a Save button. Changes apply immediately to every page, no restart needed, and persist to `brand.json` beside the log file. Nothing to edit in the code; a fresh install with no `brand.json` yet just shows the generic default ("Tilt Dashboard"). Uploads are syntax-checked, the old version is kept as a `.bak`, the dashboard restarts itself, and the logger is restarted by the watch units from Step 5. Web updates require the `--allow-updates` flag (included in the shipped service file — remove it to disable, since anyone on the network could otherwise push code to the Pi).

**Recipe wheels (Admin)** — the flavor and aroma ingredients offered in the Recipe builder aren't fixed: Admin → Recipe wheels lets you add, rename/recolor, or remove categories and ingredients for both wheels straight from the browser. A new ingredient is always filed into a category you pick from the ones that already exist, so it can't end up in the wrong place, and it automatically takes that category's color rather than getting one of its own — there's no per-ingredient color to get wrong. Adding or recoloring a category checks its color against the wheel's other categories (a simple RGB-distance check) and asks you to confirm if it's too close to an existing one, so two categories don't end up looking the same at a glance. "Reset … to built-in" discards edits to just that one wheel and restores what the dashboard shipped with. Edits are saved to `recipe-data.json` beside the log and survive restarts and software updates (a fresh install with no `recipe-data.json` yet just uses the built-in wheels).

**Built-in user guide** — the dashboard serves its own end-user manual at `/guide`, linked from the page footer, covering all of the above for whoever is viewing the dashboard. No internet needed.

Time-range presets (24h/3d/7d/30d/Batch), 30-second auto-refresh, and automatic light/dark theming (parchment by day, black/tan/olive by night) are built in, with a small initials badge, display name, and tagline in the header, browser tab, and reports — all set from Admin → Branding, no internet needed.

**Formulas**: ABV = (OG − SG) × 131.25; apparent attenuation = (OG − SG) / (OG − 1) × 100 (standard homebrew approximations, e.g. [Brewer's Friend](https://www.brewersfriend.com/abv-calculator/)). OG is your entered "Measured OG" if set, otherwise the batch's first reading.

### The log format

One JSON object per line (JSON Lines) in `/var/log/tilt/tilt.jsonl`:

```json
{"timestamp": "2026-08-24T14:03:07-04:00", "color": "Red", "model": "pro", "temp_f": 68.5, "temp_c": 20.28, "sg": 1.0165, "tx_power_dbm": -59, "battery_weeks": null, "raw_major": 685, "raw_minor": 10165, "rssi_dbm": -71, "address": "5A:09:9B:16:A3:04"}
```

`battery_weeks` is `null` on almost every line — it's only a number (weeks since that Tilt's battery was last changed) on firmware that supports battery reporting, and only once that Tilt has broadcast its one-time "-59 dBm" marker (see "Background" above). `tx_power_dbm` stays at its normal negative value on every other line, including for Tilts that don't support this at all.

Loads directly into pandas: `pd.read_json("/var/log/tilt/tilt.jsonl", lines=True)`.

### The API (for scripting)

```
GET  /api/overview?hours=168          Tilts WITH an active batch: batch, stats, series each
                                       (still lists every known colour under "colors")
GET  /api/data?color=Red&hours=0      one Tilt in detail (+50 recent readings);
                                       empty stats/series if it has no active batch
GET  /api/data?batch_id=<id>          a past batch in detail (unaffected by the above)
GET  /api/history                     finished batches with snapshots
GET  /api/admin                       log info, per-Tilt counts, versions, current
                                       brand {"name": ..., "tagline": ...}
GET  /api/settings                    {"interval": N, "allow_updates": bool}
GET  /api/recipe                      current flavor & odor wheel data
GET  /api/stages                      current Admin-curated fermentation stage list
GET  /api/export.csv?color=|id=       raw readings CSV; color= comes back empty (header
                                       only) if that Tilt has no active batch -- use id=
                                       for a past batch's readings instead
GET  /report?color=|id=               printable brew report (HTML); same rule as above
                                       applies to color= with no active batch
GET  /api/archive?id=<batch_id>       downloads a finished batch as a self-contained
                                       .html archive (report + full raw readings as
                                       embedded JSON); 404 if the batch isn't finished
GET  /guide                           built-in user guide
POST /api/batch                       {"action":"save"|"new"|"finish",
                                       "color":"Red","batch":{...,"image":dataURL}}
POST /api/settings                    {"interval": N}
POST /api/brand                       {"name": ..., "tagline": ...} -- sets the
                                       dashboard's display name/tagline (Admin ->
                                       Branding); applies immediately, no restart
POST /api/reset                       {"color": "Red"}
POST /api/archive/trim                {"id": "<batch_id>"} -- permanently erases just
                                       that finished batch's raw readings (400 if it's
                                       still active); its History entry/snapshot stay
POST /api/batch/annotate              {"id":..., "ts":..., "text":...} -- pin a
                                       permanent chart note to one reading's timestamp
                                       (add-only: no edit or delete; 400 if the
                                       timestamp falls outside that batch's window)
POST /api/batch/stage                 {"id":..., "ts":..., "stage":...} -- pin a
                                       permanent fermentation-stage marker to one
                                       timestamp (add-only: no edit or delete; 400 if
                                       the stage isn't in the current Admin-curated
                                       list, or the timestamp falls outside the
                                       batch's window). Setting a batch's start_ts via
                                       POST /api/batch (above) is how "Set primary
                                       start..." works -- there's no separate endpoint
                                       for it.
POST /api/stages                      {"action":"add"|"rename"|"delete"|"reset", ...}
                                       -- manage the Admin-curated fermentation stage
                                       list (same shape as /api/recipe's actions)
POST /api/history/delete              {"ids": ["<batch_id>", ...]}
POST /api/update                      {"target":"dashboard"|"logger","source":py}
POST /api/recipe                      {"action":"add_category"|"edit_category"|
                                       "delete_category"|"add_ingredient"|
                                       "edit_ingredient"|"delete_ingredient"|"reset",
                                       "wheel":"flavor"|"odor", ...}
GET  /recipes                         standalone recipe explorer page
GET  /api/drafts                      saved recipe explorer drafts
POST /api/drafts                      {"action":"save"|"delete"|"duplicate",
                                       "id":"<draft_id>"|null,"fields":{...}}
```

`hours=0` (default) means the active batch's window (full history if no batch). Batch fields: `name`, `style`, `batch_size`, `yeast`, `notes` (strings); `ibu`, `og_override`, `target_fg`, `target_abv`, `temp_target_f` (numbers); `start_ts` (epoch seconds — Day 1; omit to leave the existing start alone on a `"save"`, or default to "now" on a `"new"` batch — this is also what "Set primary start..." sets from the live dashboard). A batch returned by `/api/data` or `/api/history` also carries `annotations` — the list of chart notes (`{"ts":..., "text":..., "created_ts":...}`) — and `stage_markers` — the list of fermentation-stage markers (`{"ts":..., "stage":..., "created_ts":...}`) — both read-only here and set only via `POST /api/batch/annotate` and `POST /api/batch/stage` respectively; a `"new"` batch never inherits either, even when rebrewing from an old one.

`/api/overview`, `/api/data`, `/api/history`, and `/api/admin` each also include a `"battery"` object keyed by Tilt colour, e.g. `{"Red": {"weeks": 12, "ts": 1234567890}}` — weeks since that Tilt's battery was last changed, when it's been reported (see "Battery status" above). `/api/export.csv` adds a trailing `battery_weeks` column (blank when not reported on that row).

### Command-line options

```
tilt_logger.py     --logfile PATH   (default /var/log/tilt/tilt.jsonl)
                   --color Red      log only one colour (default: all)
                   --interval N     min seconds between logged readings per Tilt (0 = every beacon)
                   --adapter hci0

tilt_dashboard.py  --logfile PATH     (default /var/log/tilt/tilt.jsonl)
                   --batchfile PATH   (default: batches.json beside the log)
                   --settingsfile PATH (default: logger-settings.json beside the log)
                   --recipefile PATH  (default: recipe-data.json beside the log)
                   --draftsfile PATH  (default: recipe-drafts.json beside the log)
                   --stagesfile PATH  (default: stages.json beside the log)
                   --brandfile PATH   (default: brand.json beside the log)
                   --port 8080
                   --host 0.0.0.0     (use 127.0.0.1 to restrict to the Pi itself)
                   --allow-updates    enable installing new versions from the Admin page
```

---

## Security note

The dashboard has no authentication: anyone on your network can view it and edit batch names/notes (the readings log itself can't be altered through it). Fine on a home LAN — but don't port-forward it to the open internet. For remote access, use a VPN (WireGuard/Tailscale on the Pi) or a reverse proxy with auth.

## Performance notes

The dashboard parses the log once at first request and afterwards only reads newly appended lines, so it stays fast even with every-beacon logging — expect a several-second first page load on a Pi 3B+ with a large log, near-instant after that. Chart data is downsampled server-side to ≤400 points per series.

## Troubleshooting

- **No readings**: the Tilt only broadcasts when tilted/floating. Verify with `sudo bluetoothctl scan le`; check `hciconfig hci0 up`.
- **`SetDiscoveryFilter failed: org.bluez.Error.NotReady`**: the Bluetooth adapter isn't powered on. Run `bluetoothctl power on` (then check `hciconfig` shows UP RUNNING); if `rfkill list` shows bluetooth soft-blocked, `sudo rfkill unblock bluetooth`. To make it stick across reboots, set `AutoEnable=true` under `[Policy]` in `/etc/bluetooth/main.conf` and `sudo systemctl restart bluetooth`. A single NotReady error in the journal right after boot is just the startup race — the service retries every 10 s until the adapter is ready.
- **`bleak` errors about D-Bus/BlueZ**: make sure `bluetooth.service` is running (`systemctl status bluetooth`) and BlueZ is ≥ 5.55 (`bluetoothctl --version`); any current Raspberry Pi OS qualifies.
- **Dashboard shows "No readings found yet"**: the logger isn't writing, or the dashboard is pointed at a different `--logfile`. Check `journalctl -u tilt-logger -n 20`.
- **ABV/attenuation look wrong**: if logging started mid-fermentation, OG defaults to the first logged reading — enter your measured OG in Edit batch to correct it. If a previous brew's readings share the batch window, use "Start new batch."
- **"Day N" / brew date is wrong**: Edit batch has a **Brew date** field — set it to whatever day Day 1 should actually be, for an in-progress batch or right when you start a new one or rebrew. This commonly happens when the logger was already running (or a Tilt was still reporting old readings) before you actually pitched.
- **A Tilt's card/chart line disappeared from the overview**: this is expected once its batch is marked finished, or if a batch was never started on it in the first place — see "All Tilts overview" above. Click that Tilt's colour tab in the nav bar (it's still there) and use "Add batch details" or "Start new batch" to bring it back to the live view; its past data, if any, is in History.
- **A History batch's report shows no chart / its CSV is empty**: its summary stats (OG, FG, ABV, attenuation) still show fine on the card and in the report meta — those are snapshotted permanently when the batch finishes — but the raw per-reading data behind the charts, hourly table, and CSV export is gone, either from log rotation or a **Reset Tilt** on that colour since then, or because you deliberately used **Trim archived data…** on it (the report says which, when it was a trim). There's no way to get the raw readings back on the dashboard itself once that's happened either way — but if you made an **archive** of that batch beforehand, the full data still lives in that downloaded `.html` file.
- **"That doesn't look like a dashboard archive file" when using Rebrew from archive…**: you've picked a file that isn't an archive this dashboard made — only a `.html` file downloaded via a batch's **Archive batch** button has the embedded data this needs. A printable report exported with "Export report" looks similar but doesn't carry it.
- **`PermissionError: [Errno 13] Permission denied: '/var/log/tilt/tilt.jsonl'`**: the log directory or file isn't owned by the user running the logger. Fix with `sudo chown -R tilt:tilt /var/log/tilt` then `sudo systemctl restart tilt-logger`. This usually means the Step 3 chown was skipped, or an earlier `sudo` test run created a root-owned tilt.jsonl. The current service units also set `LogsDirectory=tilt`, so systemd recreates the directory with correct ownership on every start.
- **Batch edits don't save**: check that `/var/log/tilt` is writable by the `tilt` user (`ls -la /var/log/tilt`), and see `journalctl -u tilt-dashboard`.
- **Readings look 10× off**: the raw major/minor fields in the log let you verify Pro auto-detection; with the ≥ 5000 gravity threshold this can't misfire on real-world wort.
- **Wi-Fi/Bluetooth interference on the 3B+**: the combo chip shares an antenna; if beacons drop under heavy Wi-Fi load, a USB BT dongle on a short extension (`--adapter hci1`) is a known fix in the Tilt community.
- **Upgrading either script**: overwrite it in `/opt/tilt-logger/` and `sudo systemctl restart tilt-logger` (or `tilt-dashboard`). The log format is stable across versions.
- **No battery icon/tile ever appears for a Tilt**: this is expected for many Tilts. Battery reporting is an unofficial, occasional signal some firmwares broadcast and others don't (see "Background" above) — it's not something every unit supports, and there's no setting to turn it on. If you want to confirm your hardware is one that reports it, grep the log: `grep -o '"battery_weeks": [0-9]*' /var/log/tilt/tilt.jsonl | tail`.
- **Battery icon shows a much higher or lower week count than you expect**: remember it's weeks since the battery was *last changed* on that physical Tilt, not a charge percentage — a freshly-changed battery should read near 0, and it climbs from there. If you just swapped the battery and it still shows an old week count, give it a little time: the Tilt only sends this occasionally, not every beacon.
- **Only updated `tilt_dashboard.py`, battery data still doesn't show up**: `tilt_logger.py` is the file that actually detects and records the battery-age signal; the dashboard only displays whatever the logger already wrote to the log. Reinstall `tilt_logger.py` from Admin → Software (or copy it manually) and restart the logger service — see "Upgrading either script" below.
