# Tilt Dashboard

A self-contained fermentation dashboard for the [Tilt Hydrometer](https://tilthydrometer.com/) (Tilt, Tilt Pro, and Tilt Mini Pro), built to run on a Raspberry Pi. Point it at as many Tilts as you have, and it logs every reading, tracks batches Brewfather-style, charts gravity/ABV/temperature live, and prints a report when the brew's done — all from one Python file, with no database, no build step, and no internet connection required.

## Features

- **Multi-Tilt dashboard** — one overview page for every Tilt currently fermenting something, with live gravity, estimated ABV, temperature, attenuation, and signal/battery status; a detail view per Tilt with synced charts and a recent-readings table.
- **Batch tracking** — name, style, brew date, yeast, target gravity/ABV/temperature, notes, and a photo per batch, with full history of every past brew once it's finished.
- **Chart annotations** — pin a permanent note to an exact moment on the chart (a dry hop, a temperature change), and mark when a batch crossed into a later fermentation stage (secondary, bulk aging, bottle conditioning, or your own custom list) — both show up as markers right on the charts.
- **Mead recipe builder** — a honey/water calculator, yeast picker, and flavor/aroma wheels for planning a mead before you brew, plus a standalone recipe explorer (`/recipes`) for sketching ideas with nothing started yet.
- **Reports, CSV, and archives** — a printable brew report (with charts and notes), raw-data CSV export, and a one-file HTML archive that embeds a finished batch's complete data so it survives log rotation or a reset.
- **Configurable branding** — set your own display name and tagline from the Admin page; it shows in the header, browser tab, and every report, no code changes needed.
- **Self-updating** — install new versions of the dashboard or logger straight from the Admin page, over the network, with no SSH session required after initial setup.
- **Zero dependencies for the dashboard** — `tilt_dashboard.py` uses only the Python standard library. The logger (`tilt_logger.py`) needs one package ([`bleak`](https://github.com/hbldh/bleak)) to talk to Bluetooth.

## Quick start (Raspberry Pi)

```bash
git clone <this repo's URL>
cd tilt-dashboard
sudo bash install.sh
```

Then open `http://<your-pi-address>:8080/` from any computer, phone, or tablet on your network. That's it — the installer sets up both services (the Bluetooth logger and the web dashboard), creates a dedicated service user, configures log rotation, and enables everything to start on boot.

Full walkthrough, including testing each piece by hand before trusting it to run unattended: see **[SETUP.md](SETUP.md)**. Feature-by-feature reference and the full HTTP API: see **[DASHBOARD.md](DASHBOARD.md)**. Once it's running, the dashboard also serves its own end-user guide at `/guide` — no internet needed to read it.

## Why a Raspberry Pi, and why one file?

A Pi 3B+ (or better) with Bluetooth is cheap, sips power, and can sit next to the fermenter for the life of the brew — no laptop needs to stay on. Keeping the dashboard to a single Python file with no external dependencies (standard library only) means there's nothing to `pip install`, nothing to go out of date, and nothing that breaks because a package registry is unreachable — you can `scp` one file to a Pi with no internet access at all and it just runs.

## What it looks like

![Tilt Dashboard overview with two batches fermenting, synced gravity/ABV/temperature charts](docs/screenshot.png)

This is the generic default branding, straight out of the box — set your own name and tagline under **Admin → Branding** and it's yours, no code changes needed. Beyond the overview above, there's also a History tab of every finished batch, and an Admin page for logging interval, data resets, recipe-wheel editing, the fermentation-stage list, branding, and software updates — all from the browser.

## License

[MIT](LICENSE) — use it, modify it, brew with it.
