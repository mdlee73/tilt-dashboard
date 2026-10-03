#!/usr/bin/env python3
"""
tilt_dashboard.py — Standalone HTTP dashboard for Tilt hydrometer JSON logs.
v3: brew reports & CSV export, in-dashboard logging interval, per-Tilt data
reset, history of past brews, and batch images — on top of v2's multi-Tilt
overview and Brewfather-style batch metadata.

Serves a self-contained HTML dashboard (no internet/CDN required) that
visualizes the JSON Lines log written by tilt_logger.py. View it from any
computer on the network at  http://<pi-address>:8080/

Python 3.9+, standard library only — nothing to pip install.

Endpoints:
  GET  /                    the dashboard page
  GET  /report?color=|id=   printable brew-session report (standalone HTML)
  GET  /api/overview        all Tilts: batch + stats + series each  (?hours=)
  GET  /api/data            one Tilt (?color=&hours=) or a past batch (?batch_id=)
  GET  /api/history         finished batches across all Tilts
  GET  /api/settings        current logger settings ({"interval": N})
  GET  /api/recipe          current flavor/odor wheel data (edited or built-in)
  GET  /api/stages          admin-curated fermentation stage list (name + id each)
  GET  /recipes             standalone recipe explorer (saved mead recipe drafts)
  GET  /api/drafts          saved recipe explorer drafts
  GET  /api/export.csv      raw readings CSV (?color= for live batch, ?id= for past)
  GET  /api/archive?id=     standalone archive (.html) of one finished batch: report
                            + its full raw readings + fields, embedded as JSON
  POST /api/batch           {"action":"save"|"new"|"finish","color":...,"batch":{...}}
  POST /api/settings        {"interval": N} — logger applies it within ~5 s
  POST /api/reset           {"color": ...} — erase that Tilt's readings + active batch
  POST /api/archive/trim    {"id": ...} — erase just that finished batch's raw readings
                            (its snapshot/History entry is kept); not undoable
  POST /api/batch/annotate  {"id":..., "ts":..., "text":...} — pin a permanent note to
                            one reading's timestamp (add-only: no edit or delete)
  POST /api/batch/stage     {"id":..., "ts":..., "stage":...} — pin a permanent
                            fermentation-stage marker (add-only, stage must be one
                            of the current /api/stages names)
  POST /api/stages          {"action":"add"|"rename"|"delete"|"reset", ...} — manage
                            the admin-curated fermentation stage list
  POST /api/recipe          add/edit/delete/reset flavor & odor wheel categories/ingredients
  POST /api/drafts          {"action":"save"|"delete"|"duplicate","id":...,"fields":{...}}

Usage:
  python3 tilt_dashboard.py --logfile /var/log/tilt/tilt.jsonl --port 8080
"""

import argparse
import copy
import html as html_mod
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ----------------------------------------------------------------------------
# Log reading with an incremental cache (cheap on a Pi even for big logs)
# ----------------------------------------------------------------------------

class LogCache:
    """Parses the JSONL log once, then only reads newly appended bytes."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._records = []   # list of (epoch_s, color, temp_f, sg, rssi, model, battery_weeks)
        self._offset = 0
        self._size = -1
        self._battery = {}   # colour -> (battery_weeks, epoch_s) for the latest report seen

    def _parse_line(self, line: str):
        try:
            r = json.loads(line)
            ts = datetime.fromisoformat(r["timestamp"]).timestamp()
            batt = r.get("battery_weeks")
            try:
                batt = int(batt) if batt is not None else None
            except (TypeError, ValueError):
                batt = None
            return (ts, r["color"], float(r["temp_f"]), float(r["sg"]),
                    r.get("rssi_dbm"), r.get("model", "standard"), batt)
        except (ValueError, KeyError, TypeError):
            return None  # skip malformed/partial lines

    def records(self):
        with self._lock:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                return []
            if size < self._size:          # rotated/truncated: reparse
                self._records, self._offset, self._battery = [], 0, {}
            self._size = size
            if size > self._offset:
                with open(self.path, "r", encoding="utf-8") as f:
                    f.seek(self._offset)
                    chunk = f.read()
                end = chunk.rfind("\n") + 1  # only consume complete lines
                for line in chunk[:end].splitlines():
                    rec = self._parse_line(line)
                    if rec:
                        self._records.append(rec)
                        if rec[6] is not None:   # battery_weeks
                            self._battery[rec[1]] = (rec[6], rec[0])
                self._offset += len(chunk[:end].encode("utf-8"))
            return self._records

    def battery_by_color(self) -> dict:
        """{colour: {"weeks": int, "ts": epoch_s}} for the latest battery-age
        report seen per Tilt colour. Maintained incrementally as new lines
        are parsed (O(1) per call) rather than rescanning the whole log."""
        with self._lock:
            return {c: {"weeks": w, "ts": round(t)} for c, (w, t) in self._battery.items()}

    def purge_color(self, color: str) -> int:
        """Physically remove one colour's readings from the log file.

        The logger opens the file per-append, so rewrite+rename is safe apart
        from a sub-second race. Returns the number of lines removed.
        """
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                return 0
            kept, removed = [], 0
            for line in lines:
                try:
                    if json.loads(line).get("color") == color:
                        removed += 1
                        continue
                except ValueError:
                    pass
                kept.append(line)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, self.path)
            self._records, self._offset, self._size = [], 0, -1  # force reparse
            return removed

    def purge_window(self, color: str, start_ts: float, end_ts) -> int:
        """Physically remove just ONE batch's readings: one colour, bounded
        to its own [start_ts, end_ts] window, leaving every other batch (same
        colour, different window) and every other colour's rows untouched.

        This is safe to scope this narrowly because a colour's batches never
        overlap in time -- starting or finishing a batch always closes the
        previous one first (see BatchStore.new_batch/finish) -- so a batch's
        own window can never contain another batch's readings.
        """
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                return 0
            end = end_ts if end_ts else float("inf")
            kept, removed = [], 0
            for line in lines:
                try:
                    r = json.loads(line)
                    if r.get("color") == color:
                        ts = datetime.fromisoformat(r["timestamp"]).timestamp()
                        if start_ts <= ts <= end:
                            removed += 1
                            continue
                except (ValueError, KeyError, TypeError):
                    pass
                kept.append(line)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, self.path)
            self._records, self._offset, self._size = [], 0, -1  # force reparse
            return removed


# ----------------------------------------------------------------------------
# Logger settings shared with tilt_logger.py (re-read there every ~5 s)
# ----------------------------------------------------------------------------

class SettingsStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return {"interval": max(float(cfg.get("interval", 0)), 0.0)}
        except (OSError, ValueError, TypeError):
            return {"interval": 0.0}

    def set_interval(self, interval: float):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"interval": max(float(interval), 0.0)}, f)
            os.replace(tmp, self.path)


# ----------------------------------------------------------------------------
# Branding (brand.json beside the log file) -- the dashboard's display name
# and tagline, shown in the header, browser tab, printable reports, and
# archive files. Deliberately just two short strings rather than logo/label
# artwork (see the generic favicon below), so this stays a one-file,
# no-build-step app: open Admin, type a name, done -- no image hosting or
# upload handling needed.
# ----------------------------------------------------------------------------

DEFAULT_BRAND_NAME = "Tilt Dashboard"
DEFAULT_BRAND_TAGLINE = "Keep tabs on every batch."
MAX_BRAND_NAME = 40
MAX_BRAND_TAGLINE = 80


def _initials(name: str) -> str:
    words = [w for w in (name or "").strip().split() if w]
    if not words:
        return "T"
    s = words[0][0]
    if len(words) > 1:
        s += words[1][0]
    return s.upper()


class BrandStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def get(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            name = str(cfg.get("name") or "").strip()[:MAX_BRAND_NAME] or DEFAULT_BRAND_NAME
            tagline = str(cfg.get("tagline") or "").strip()[:MAX_BRAND_TAGLINE] or DEFAULT_BRAND_TAGLINE
            return {"name": name, "tagline": tagline}
        except (OSError, ValueError, TypeError):
            return {"name": DEFAULT_BRAND_NAME, "tagline": DEFAULT_BRAND_TAGLINE}

    def set(self, name: str, tagline: str) -> dict:
        name = (name or "").strip()[:MAX_BRAND_NAME] or DEFAULT_BRAND_NAME
        tagline = (tagline or "").strip()[:MAX_BRAND_TAGLINE] or DEFAULT_BRAND_TAGLINE
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"name": name, "tagline": tagline}, f)
            os.replace(tmp, self.path)
        return {"name": name, "tagline": tagline}


def _with_brand(html: str, brand: dict) -> str:
    esc = html_mod.escape
    return (html
            .replace("__BRAND_NAME__", esc(brand["name"]))
            .replace("__BRAND_TAGLINE__", esc(brand["tagline"]))
            .replace("__BRAND_INITIALS__", esc(_initials(brand["name"]))))


# ----------------------------------------------------------------------------
# Batch metadata store (batches.json beside the log file)
# ----------------------------------------------------------------------------

STR_FIELDS = ("name", "style", "batch_size", "yeast", "notes")
NUM_FIELDS = ("og_override", "target_fg", "temp_target_f", "ibu", "target_abv")
MAX_IMAGE_BYTES = 800_000   # ~512px client-resized JPEG is well under this
MAX_ANNOTATION_TEXT = 500   # a chart note is a short tag, not an essay
MAX_STAGE_NAME = 60         # a fermentation stage name, e.g. "Secondary fermentation"
DEFAULT_STAGES = ["Secondary fermentation", "Bulk aging", "Oak aging", "Bottle conditioning"]


# ----------------------------------------------------------------------------
# Recipe builder reference data — ABV/sweetness tiers, yeast strains, and the
# flavor/odor wheels shown in the batch editor's "Recipe builder" panel.
# Shipped to the browser as-is (see _inject/__RECIPE_DATA__) so the whole
# picker is data-driven from this one table.
# ----------------------------------------------------------------------------

RECIPE_DATA = json.loads(r'''
{
  "recipeMechanics": {
    "abvTargets": [
      {
        "id": "session_hydromel",
        "label": "Session Hydromel",
        "range": "3% - 7% ABV",
        "minAbv": 3,
        "maxAbv": 7,
        "description": "Light, crisp, quickly fermented, and highly crushable. Typically carbonated."
      },
      {
        "id": "standard_mead",
        "label": "Standard Mead",
        "range": "8% - 13% ABV",
        "minAbv": 8,
        "maxAbv": 13,
        "description": "The traditional craft range; comparable to standard table wines."
      },
      {
        "id": "high_gravity_sack",
        "label": "High Gravity Sack Mead",
        "range": "14% - 18%+ ABV",
        "minAbv": 14,
        "maxAbv": 18,
        "description": "Rich, dense, intensely warming, and requires heavy aging to mellow out the alcohol burn."
      }
    ],
    "sweetnessLevels": [
      {
        "id": "bone_dry",
        "label": "Bone Dry",
        "fg": 1.000,
        "fgRange": "0.990 - 1.000 FG",
        "description": "No residual sugar left. Sharp, crisp, and highlights pure fermentation characteristics, tannins, and acids."
      },
      {
        "id": "semi_sweet",
        "label": "Semi-Sweet",
        "fg": 1.012,
        "fgRange": "1.006 - 1.015 FG",
        "description": "The crowd-pleaser tier. Balanced profile with a noticeable honey character that doesn't overwhelm the palate."
      },
      {
        "id": "sweet_dessert",
        "label": "Sweet / Dessert",
        "fg": 1.024,
        "fgRange": "1.020 - 1.035+ FG",
        "description": "Thick, viscous, and intensely rich. Essential for balancing out aggressive elements like intense capsaicin heat or heavy roasted bitterness."
      }
    ],
    "yeastStrains": [
      {
        "id": "d47",
        "name": "Lalvin D47",
        "abvTolerance": 14,
        "profile": "Clean, leaves great body, and dramatically amplifies ripe fruit and floral esters. Excellent for traditional meads or bright melomels."
      },
      {
        "id": "ec1118",
        "name": "Lalvin EC-1118",
        "abvTolerance": 18,
        "profile": "An aggressive, neutral champagne yeast. It ferments dry, strips away subtle honey aromatics, but reliably handles high-sugar sack meads, extreme fruit additions, and restarts stuck batches."
      },
      {
        "id": "71b",
        "name": "Lalvin 71B",
        "abvTolerance": 14,
        "profile": "Metabolizes harsh malic acid. The absolute gold standard for heavy berry or stone-fruit melomels, yielding a smooth, quick-aging profile."
      },
      {
        "id": "us05",
        "name": "SafAle US-05",
        "abvTolerance": 12,
        "profile": "A clean, highly neutral ale yeast. Keeps original honey profiles completely intact without introducing competing wine notes. Perfect for session hydromels."
      },
      {
        "id": "m05",
        "name": "Mangrove Jack's M05 Mead Yeast",
        "abvTolerance": 18,
        "profile": "High ester production with an incredibly robust fresh floral aroma. Retains high body while pushing all the way into heavy sack mead territory."
      }
    ]
  },
  "flavorWheel": {
    "categories": [
      {
        "id": "sweet_base_body",
        "name": "Sweet Base & Body",
        "colorGroup": "Gold",
        "hex": "#FFD700",
        "ingredients": [
          {"id": "caramelized_honey", "name": "Caramelized Honey (Bochet)", "role": "Adds deep toffee, marshmallow, and residual unfermentable sweetness."},
          {"id": "orange_blossom_honey", "name": "Orange Blossom Honey", "role": "Provides a light, clean, citrus-forward sugar foundation."},
          {"id": "maple_syrup", "name": "Maple Syrup", "role": "Adds woody, rich sweetness (best used post-stabilization)."},
          {"id": "toasted_coconut", "name": "Toasted Coconut", "role": "Imparts a creamy, oily mouthfeel and natural lactone sweetness."},
          {"id": "buckwheat_honey", "name": "Buckwheat Honey", "role": "Adds a bold, malty depth with an almost molasses-like darkness."},
          {"id": "piloncillo", "name": "Piloncillo (Unrefined Cane Sugar)", "role": "Contributes rich caramel and dried-fruit sweetness without honey character."},
          {"id": "dried_fig", "name": "Dried Fig", "role": "Brings a dense, jammy sweetness and dark fruit body."},
          {"id": "date_syrup", "name": "Date Syrup", "role": "Adds a rich, caramelized sweetness with a subtle toffee edge."},
          {"id": "golden_syrup", "name": "Golden Syrup", "role": "Rounds out body with a light treacle warmth."},
          {"id": "lactose", "name": "Lactose (Milk Sugar)", "role": "Leaves permanent residual sweetness and a creamy mouthfeel (non-fermentable)."},
          {"id": "turbinado_sugar", "name": "Turbinado Sugar", "role": "Adds light raw-sugar sweetness with a faint molasses note."},
          {"id": "agave_nectar", "name": "Agave Nectar", "role": "Provides clean, neutral sweetness with a light vegetal note."}
        ]
      },
      {
        "id": "fruit_organic_acids",
        "name": "Fruit & Organic Acids",
        "colorGroup": "Crimson",
        "hex": "#DC143C",
        "ingredients": [
          {"id": "tart_cherry", "name": "Tart Cherry", "role": "Delivers a heavy punch of malic acid to cut through honey density."},
          {"id": "strawberry", "name": "Strawberry", "role": "Offers a bright, volatile fruit sweetness with light natural acidity."},
          {"id": "chardonnay_juice", "name": "Chardonnay Grape Juice (Pyment)", "role": "Contributes tartaric acid and professional winemaking structure."},
          {"id": "yuzu_juice", "name": "Yuzu Juice", "role": "Imparts an intense, exotic, sharp citric punch."},
          {"id": "blackberry", "name": "Blackberry", "role": "Deep, jammy fruit character with moderate acidity."},
          {"id": "raspberry", "name": "Raspberry", "role": "Bright, tart, and floral fruit character."},
          {"id": "blueberry", "name": "Blueberry", "role": "Mild, sweet-tart fruit with a subtle character."},
          {"id": "peach", "name": "Peach", "role": "Soft stone-fruit sweetness with low acidity."},
          {"id": "cranberry", "name": "Cranberry", "role": "Sharp, high-acid tartness with a tannic edge."},
          {"id": "passionfruit", "name": "Passionfruit", "role": "Tropical, intensely tart, and heavily aromatic."},
          {"id": "pomegranate", "name": "Pomegranate", "role": "Winey tartness with light tannin structure."},
          {"id": "black_currant", "name": "Black Currant (Cassis)", "role": "Deep, tart, wine-like fruit character."},
          {"id": "citric_acid", "name": "Citric Acid (Food-Grade)", "role": "A precise, adjustable acid addition without fruit character."},
          {"id": "tartaric_acid", "name": "Tartaric Acid", "role": "Sharp, wine-like acid for backbone, common in grape-based meads."}
        ]
      },
      {
        "id": "spice_pungent_heat",
        "name": "Spice & Pungent Heat",
        "colorGroup": "Orange",
        "hex": "#FF8C00",
        "ingredients": [
          {"id": "carolina_reaper", "name": "Carolina Reaper", "role": "Delivers intense capsaicin heat; requires micro-dosing."},
          {"id": "fresh_ginger", "name": "Fresh Ginger Root", "role": "Adds a sharp, clean, throat-warming culinary bite."},
          {"id": "cinnamon_stick", "name": "Cinnamon Stick", "role": "Introduces a classic, sweet-spiced warming sensation."},
          {"id": "pink_peppercorn", "name": "Pink Peppercorn", "role": "Offers a mild, fruity, almost sweet peppery finish."},
          {"id": "habanero", "name": "Habanero", "role": "Moderate, fruity heat, milder than Carolina Reaper."},
          {"id": "jalapeno", "name": "Jalapeño", "role": "Mild, vegetal heat that's easy to balance."},
          {"id": "star_anise", "name": "Star Anise", "role": "Sweet, licorice-forward spice character."},
          {"id": "clove", "name": "Clove", "role": "Intense, medicinal-sweet spice; use sparingly."},
          {"id": "cardamom", "name": "Cardamom", "role": "Citrusy-floral warm spice note."},
          {"id": "black_peppercorn", "name": "Black Peppercorn", "role": "Sharp, woody heat."},
          {"id": "grains_of_paradise", "name": "Grains of Paradise", "role": "Peppery heat with citrus undertones."},
          {"id": "allspice", "name": "Allspice", "role": "Warm blend of clove, cinnamon, and nutmeg."},
          {"id": "szechuan_peppercorn", "name": "Szechuan Peppercorn", "role": "Tingling, numbing heat with a citrus edge."}
        ]
      },
      {
        "id": "earthy_tannic_bitter",
        "name": "Earthy, Tannic & Bitter",
        "colorGroup": "Forest Green",
        "hex": "#228B22",
        "ingredients": [
          {"id": "hibiscus_petals", "name": "Hibiscus Petals", "role": "Contributes heavy structural tannins and a sharp, cranberry-like dryness."},
          {"id": "toasted_oak_chips", "name": "Toasted Oak Chips", "role": "Mimics barrel aging; adds wood tannins and structure."},
          {"id": "black_tea", "name": "Black Tea", "role": "Provides pure, clean enzyme-tannins to fix a watery mouthfeel."},
          {"id": "dark_roast_coffee", "name": "Dark Roast Coffee Beans", "role": "Introduces sharp, roasty pyrazines and intense backend bitterness."},
          {"id": "sarsaparilla_root", "name": "Sarsaparilla Root", "role": "Adds an earthy, medicinal, old-school draft soda bitterness."},
          {"id": "green_tea", "name": "Green Tea", "role": "Light, grassy tannin structure."},
          {"id": "chamomile", "name": "Chamomile", "role": "Soft, apple-like bitterness and earthy character."},
          {"id": "juniper_berries", "name": "Juniper Berries", "role": "Piney, resinous bitterness."},
          {"id": "grape_tannin", "name": "Grape Tannin (Powdered)", "role": "A precise tannin and structure addition."},
          {"id": "dandelion_root", "name": "Dandelion Root", "role": "Earthy, coffee-like bitterness."},
          {"id": "charred_oak_staves", "name": "Charred Oak Staves", "role": "Deep, smoky tannin, heavier than toasted chips."}
        ]
      }
    ]
  },
  "odorWheel": {
    "categories": [
      {
        "id": "floral_delicate_botanicals",
        "name": "Floral & Delicate Botanicals",
        "colorGroup": "Lavender",
        "hex": "#E6E6FA",
        "ingredients": [
          {"id": "dried_elderflower", "name": "Dried Elderflower", "role": "Emits a sweet, summer-meadow, upper-register floral note."},
          {"id": "lavender_buds", "name": "Lavender Buds", "role": "Provides an intense, clean, calming, highly soapy essential oil aroma."},
          {"id": "wildflower_honey_bouquet", "name": "Wildflower Honey Bouquet", "role": "Brings a rustic, deep, authentic hive-and-pollen scent."},
          {"id": "jasmine", "name": "Jasmine", "role": "Imparts a heady, exotic, night-blooming tropical floral aroma."},
          {"id": "rose_petals", "name": "Rose Petals", "role": "Classic, perfumed floral aroma."},
          {"id": "orange_blossom_water", "name": "Orange Blossom Water", "role": "Delicate citrus-floral top note."},
          {"id": "chamomile_flowers", "name": "Chamomile Flowers", "role": "Soft, apple-like floral aroma."},
          {"id": "honeysuckle", "name": "Honeysuckle", "role": "Sweet, nectar-like floral scent."},
          {"id": "violet", "name": "Violet", "role": "Delicate, powdery floral note."},
          {"id": "meadowsweet", "name": "Meadowsweet", "role": "Traditional mead herb with a honeyed, floral-almond aroma."}
        ]
      },
      {
        "id": "bright_citrus_tropical",
        "name": "Bright, Citrus & Tropical",
        "colorGroup": "Yellow",
        "hex": "#FFFF00",
        "ingredients": [
          {"id": "lemongrass", "name": "Lemongrass", "role": "Emits a sharp, clean, citronella-and-herb scent without the acid."},
          {"id": "lemon_peel", "name": "Lemon Peel", "role": "Contributes bright, volatile limonene oils from the flavedo."},
          {"id": "bergamot", "name": "Bergamot", "role": "Delivers an upscale, perfumed Earl Grey tea citrus aromatic."},
          {"id": "citra_hops", "name": "Citra Hops (Dry Hopped)", "role": "Floods the nose with massive passionfruit, mango, and lychee notes."},
          {"id": "grapefruit_zest", "name": "Grapefruit Zest", "role": "Sharp, bittersweet citrus aroma."},
          {"id": "lime_zest", "name": "Lime Zest", "role": "Zesty, sharp citrus top note."},
          {"id": "mandarin_peel", "name": "Mandarin/Tangerine Peel", "role": "Sweet, bright citrus aroma."},
          {"id": "mango", "name": "Mango", "role": "Sweet, tropical stone-fruit aroma."},
          {"id": "pineapple", "name": "Pineapple", "role": "Bright, tropical, slightly tart aroma."},
          {"id": "guava", "name": "Guava", "role": "Musky-sweet tropical aroma."}
        ]
      },
      {
        "id": "roasted_warm_pyrazic",
        "name": "Roasted, Warm & Pyrazic",
        "colorGroup": "Brown",
        "hex": "#8B4513",
        "ingredients": [
          {"id": "vanilla_bean", "name": "Vanilla Bean", "role": "Radiates heavy, comforting, sweet vanillin aromatics."},
          {"id": "toffee_aromas", "name": "Toffee Aromas", "role": "Derived from Maillard reactions during honey boiling."},
          {"id": "nutmeg", "name": "Nutmeg", "role": "Offers a dusty, sweet, classic autumnal baking spice aroma."},
          {"id": "espresso_crema", "name": "Espresso Cream", "role": "Imparts a sharp, freshly pulled, roasted coffee top-note."},
          {"id": "cacao_nibs", "name": "Cacao Nibs", "role": "Contributes a rich, dark, unsweetened chocolate bakery aroma."},
          {"id": "toasted_almond", "name": "Toasted Almond", "role": "Nutty, warm bakery aroma."},
          {"id": "caramel_butterscotch", "name": "Caramel / Butterscotch", "role": "Rich, sweet dessert aroma."},
          {"id": "molasses", "name": "Molasses", "role": "Deep, dark, treacle-like aroma."},
          {"id": "smoked_wood_chips", "name": "Smoked Wood Chips", "role": "Campfire, smoky pyrazine note."},
          {"id": "graham_cracker", "name": "Graham Cracker", "role": "Sweet, toasty bakery aroma."},
          {"id": "roasted_hazelnut", "name": "Roasted Hazelnut", "role": "Nutty, praline-like warmth."}
        ]
      },
      {
        "id": "resinous_herbal_forest",
        "name": "Resinous, Herbal & Forest",
        "colorGroup": "Sage Green",
        "hex": "#8FBC8F",
        "ingredients": [
          {"id": "fresh_rosemary", "name": "Fresh Rosemary", "role": "Emits a punchy, pine-like, camphoraceous culinary aroma."},
          {"id": "pine_needle", "name": "Pine Needle", "role": "Brings a crisp, wintry, alpine, outdoor freshness."},
          {"id": "sweet_basil", "name": "Sweet Basil", "role": "Offers a peppery, slightly clove-like green herbal scent."},
          {"id": "oak_moss", "name": "Oak Moss", "role": "Imparts a deep, damp-forest, floor-and-bark perfume note."},
          {"id": "earthy_wood_roots", "name": "Earthy Wood Roots", "role": "Delivers a heavy, grounded, rain-on-soil (geosmin) aroma."},
          {"id": "thyme", "name": "Thyme", "role": "Savory, earthy herbal aroma."},
          {"id": "sage", "name": "Sage", "role": "Dusty, savory, slightly peppery herbal note."},
          {"id": "eucalyptus", "name": "Eucalyptus", "role": "Sharp, medicinal, cooling aroma."},
          {"id": "douglas_fir_tips", "name": "Douglas Fir Tips", "role": "Bright, citrusy pine aroma."},
          {"id": "cedar", "name": "Cedar", "role": "Dry, aromatic woody scent."},
          {"id": "bay_leaf", "name": "Bay Leaf", "role": "Savory, slightly peppery herbal note."}
        ]
      }
    ]
  }
}
''')

class BatchStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._batches = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("batches"), list):
                self._batches = data["batches"]
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"batches": self._batches}, f, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def _clean(fields: dict) -> dict:
        out = {}
        for k in STR_FIELDS:
            v = fields.get(k)
            if isinstance(v, str):
                out[k] = v.strip()[:2000]
        for k in NUM_FIELDS:
            v = fields.get(k)
            if v in (None, ""):
                out[k] = None
            else:
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    pass  # ignore junk, keep previous value
        if "image" in fields:   # only touched when the client sends the key
            v = fields["image"]
            if v in ("", None):
                out["image"] = None
            elif (isinstance(v, str) and v.startswith("data:image/")
                  and len(v) <= MAX_IMAGE_BYTES):
                out["image"] = v
        return out

    @staticmethod
    def _start_ts_override(fields: dict, fallback: float) -> float:
        """An explicit "Brew date" from the batch editor overrides the
        fallback (the existing start_ts, or — for a brand-new batch — the
        caller-supplied default). Blank/invalid leaves the fallback alone."""
        v = fields.get("start_ts")
        if v in (None, ""):
            return fallback
        try:
            return float(v)
        except (TypeError, ValueError):
            return fallback

    def active(self, color: str):
        cands = [b for b in self._batches
                 if b["color"] == color and not b.get("end_ts")]
        return max(cands, key=lambda b: b["start_ts"]) if cands else None

    def by_id(self, batch_id: str):
        return next((b for b in self._batches if b["id"] == batch_id), None)

    def for_color(self, color: str):
        return sorted((b for b in self._batches if b["color"] == color),
                      key=lambda b: b["start_ts"])

    def finished(self):
        return sorted((b for b in self._batches if b.get("end_ts")),
                      key=lambda b: b["end_ts"], reverse=True)

    def save_fields(self, color: str, fields: dict, default_start: float):
        """Update the active batch, or create one spanning existing data."""
        with self._lock:
            b = self.active(color)
            if b is None:
                b = {"id": uuid.uuid4().hex[:12], "color": color,
                     "start_ts": self._start_ts_override(fields, default_start),
                     "end_ts": None}
                self._batches.append(b)
            else:
                b["start_ts"] = self._start_ts_override(fields, b["start_ts"])
            b.update(self._clean(fields))
            self._save()
            return b

    def new_batch(self, color: str, fields: dict, now: float, snapshot=None):
        """Finish the active batch (if any) and start a fresh one now
        (or on the brew date given in fields["start_ts"], if set)."""
        with self._lock:
            b = self.active(color)
            if b is not None:
                b["end_ts"] = now
                if snapshot:
                    b["snapshot"] = snapshot
            nb = {"id": uuid.uuid4().hex[:12], "color": color,
                  "start_ts": self._start_ts_override(fields, now), "end_ts": None}
            nb.update(self._clean(fields))
            self._batches.append(nb)
            self._save()
            return nb

    def finish(self, color: str, now: float, snapshot=None):
        with self._lock:
            b = self.active(color)
            if b is not None:
                b["end_ts"] = now
                if snapshot:
                    b["snapshot"] = snapshot
                self._save()
            return b

    def delete_finished(self, ids) -> int:
        """Remove finished batches by id (active batches are never touched)."""
        with self._lock:
            ids = set(ids)
            before = len(self._batches)
            self._batches = [b for b in self._batches
                             if not (b["id"] in ids and b.get("end_ts"))]
            removed = before - len(self._batches)
            if removed:
                self._save()
            return removed

    def delete_active(self, color: str):
        with self._lock:
            b = self.active(color)
            if b is not None:
                self._batches.remove(b)
                self._save()
            return b

    def add_annotation(self, batch_id: str, ts: float, text: str, now: float):
        """Append a permanent note pinned to one specific reading's timestamp.
        Notes are add-only by design -- no edit or delete -- so the brewing
        timeline stays a trustworthy record of what you observed and when."""
        with self._lock:
            b = self.by_id(batch_id)
            if b is None:
                return None
            ann = {"ts": ts, "text": text[:MAX_ANNOTATION_TEXT], "created_ts": now}
            b.setdefault("annotations", []).append(ann)
            b["annotations"].sort(key=lambda a: a["ts"])
            self._save()
            return b

    def add_stage_marker(self, batch_id: str, ts: float, stage: str, now: float):
        """Append a permanent fermentation-stage marker (e.g. "Secondary
        fermentation") at one timestamp. Like chart notes, these are
        add-only -- no edit or delete, just a warning before you confirm --
        so the brewing timeline can't quietly be rewritten after the fact.
        The stage name is copied as plain text at the moment it's picked, so
        later renaming or removing it from the admin-curated list (see
        StageStore) never rewrites a batch's already-recorded history."""
        with self._lock:
            b = self.by_id(batch_id)
            if b is None:
                return None
            m = {"ts": ts, "stage": stage[:MAX_STAGE_NAME], "created_ts": now}
            b.setdefault("stage_markers", []).append(m)
            b["stage_markers"].sort(key=lambda a: a["ts"])
            self._save()
            return b

    def mark_trimmed(self, batch_id: str, removed: int, now: float):
        """Record that a finished batch's raw readings were deliberately
        trimmed from the log (via the archive-then-trim flow), so History
        and the report can say so specifically instead of the vaguer
        "reset or rotated" message. The batch's snapshot is untouched."""
        with self._lock:
            b = self.by_id(batch_id)
            if b is not None:
                b["trimmed"] = True
                b["trimmed_ts"] = now
                b["trimmed_count"] = removed
                self._save()
            return b


# ----------------------------------------------------------------------------
# Fermentation stage store (stages.json beside the log file) — a small,
# admin-curated list of stage names (Secondary fermentation, Bulk aging, ...)
# a batch can be marked with. Deliberately NOT free text and NOT tied to any
# one recipe: picking from a known, editable list keeps "mark a stage" a
# one-click action instead of inventing a label every time, while Admin ->
# Fermentation stages still lets you add, rename, or remove options as your
# own process evolves.
# ----------------------------------------------------------------------------

class StageStore:
    def __init__(self, path: str, defaults=DEFAULT_STAGES):
        self.path = path
        self._lock = threading.Lock()
        self._stages = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("stages"), list):
                self._stages = data["stages"]
        except (OSError, ValueError):
            pass
        if self._stages is None:
            self._stages = [{"id": uuid.uuid4().hex[:12], "name": n} for n in defaults]

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"stages": self._stages}, f, indent=1)
        os.replace(tmp, self.path)

    def list(self):
        with self._lock:
            return [dict(s) for s in self._stages]

    def names(self):
        with self._lock:
            return [s["name"] for s in self._stages]

    def add(self, name: str):
        name = name.strip()[:MAX_STAGE_NAME]
        if not name:
            return None
        with self._lock:
            sid = uuid.uuid4().hex[:12]
            self._stages.append({"id": sid, "name": name})
            self._save()
            return sid

    def rename(self, sid: str, name: str) -> bool:
        name = name.strip()[:MAX_STAGE_NAME]
        if not name:
            return False
        with self._lock:
            s = next((s for s in self._stages if s["id"] == sid), None)
            if s is None:
                return False
            s["name"] = name
            self._save()
            return True

    def delete(self, sid: str) -> bool:
        with self._lock:
            before = len(self._stages)
            self._stages = [s for s in self._stages if s["id"] != sid]
            changed = len(self._stages) != before
            if changed:
                self._save()
            return changed

    def reset(self):
        with self._lock:
            self._stages = [{"id": uuid.uuid4().hex[:12], "name": n} for n in DEFAULT_STAGES]
            self._save()


# ----------------------------------------------------------------------------
# Recipe wheel store (recipe-data.json beside the log file) — lets the
# flavor/odor wheels be edited from the Admin page instead of hand-editing
# this file. Falls back to the built-in RECIPE_DATA wheels until edited, and
# "reset" can always revert back to them.
# ----------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _slugify(name: str, existing) -> str:
    base = _SLUG_RE.sub("_", name.strip().lower()).strip("_") or "item"
    slug, n = base, 2
    while slug in existing:
        slug = f"{base}_{n}"
        n += 1
    return slug


class RecipeStore:
    def __init__(self, path: str, defaults: dict):
        self.path = path
        self._lock = threading.Lock()
        self._defaults = copy.deepcopy(defaults)   # {"flavorWheel":..., "odorWheel":...}
        self._data = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "flavorWheel" in d and "odorWheel" in d:
                self._data = d
        except (OSError, ValueError):
            pass
        if self._data is None:
            self._data = copy.deepcopy(self._defaults)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=1)
        os.replace(tmp, self.path)

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    @staticmethod
    def _key(wheel):
        return "flavorWheel" if wheel == "flavor" else "odorWheel"

    def _wheel(self, wheel):
        return self._data[self._key(wheel)]

    def add_category(self, wheel: str, name: str, hex_: str):
        with self._lock:
            w = self._wheel(wheel)
            cid = _slugify(name, {c["id"] for c in w["categories"]})
            w["categories"].append({"id": cid, "name": name, "colorGroup": name,
                                    "hex": hex_, "ingredients": []})
            self._save()
            return cid

    def edit_category(self, wheel: str, cid: str, name=None, hex_=None) -> bool:
        with self._lock:
            cat = next((c for c in self._wheel(wheel)["categories"] if c["id"] == cid), None)
            if cat is None:
                return False
            if name:
                cat["name"] = name
            if hex_:
                cat["hex"] = hex_
            self._save()
            return True

    def delete_category(self, wheel: str, cid: str) -> bool:
        with self._lock:
            w = self._wheel(wheel)
            before = len(w["categories"])
            w["categories"] = [c for c in w["categories"] if c["id"] != cid]
            changed = len(w["categories"]) != before
            if changed:
                self._save()
            return changed

    def add_ingredient(self, wheel: str, cid: str, name: str, role: str):
        with self._lock:
            w = self._wheel(wheel)
            cat = next((c for c in w["categories"] if c["id"] == cid), None)
            if cat is None:
                return None
            existing = {i["id"] for c in w["categories"] for i in c["ingredients"]}
            iid = _slugify(name, existing)
            cat["ingredients"].append({"id": iid, "name": name, "role": role})
            self._save()
            return iid

    def edit_ingredient(self, wheel: str, cid: str, iid: str, name=None, role=None) -> bool:
        with self._lock:
            cat = next((c for c in self._wheel(wheel)["categories"] if c["id"] == cid), None)
            ing = next((i for i in (cat["ingredients"] if cat else []) if i["id"] == iid), None)
            if ing is None:
                return False
            if name:
                ing["name"] = name
            if role:
                ing["role"] = role
            self._save()
            return True

    def delete_ingredient(self, wheel: str, cid: str, iid: str) -> bool:
        with self._lock:
            cat = next((c for c in self._wheel(wheel)["categories"] if c["id"] == cid), None)
            if cat is None:
                return False
            before = len(cat["ingredients"])
            cat["ingredients"] = [i for i in cat["ingredients"] if i["id"] != iid]
            changed = len(cat["ingredients"]) != before
            if changed:
                self._save()
            return changed

    def reset(self, wheel=None):
        with self._lock:
            if wheel in ("flavor", "odor"):
                self._data[self._key(wheel)] = copy.deepcopy(self._defaults[self._key(wheel)])
            else:
                self._data = copy.deepcopy(self._defaults)
            self._save()


# ----------------------------------------------------------------------------
# Recipe draft library (recipe-drafts.json beside the log file) — saved,
# named mead recipes from the standalone /recipes explorer page. Independent
# of any Tilt or batch; a finished draft is copied into a real batch's notes
# by hand when it's time to brew it.
# ----------------------------------------------------------------------------

DRAFT_STR_FIELDS = ("name", "yeast", "notes")
DRAFT_NUM_FIELDS = ("target_abv", "sweetness_fg", "volume", "honey_ppg")
MAX_PICKS = 60


class DraftStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._drafts = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("drafts"), list):
                self._drafts = data["drafts"]
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"drafts": self._drafts}, f, indent=1)
        os.replace(tmp, self.path)

    def list(self):
        with self._lock:
            return sorted(self._drafts, key=lambda d: d.get("updated_ts", 0), reverse=True)

    @staticmethod
    def _clean(fields: dict) -> dict:
        out = {}
        for k in DRAFT_STR_FIELDS:
            v = fields.get(k)
            if isinstance(v, str):
                out[k] = v.strip()[:2000]
        for k in DRAFT_NUM_FIELDS:
            v = fields.get(k)
            if v in (None, ""):
                out[k] = None
            else:
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    pass
        for k in ("flavor_ids", "odor_ids"):
            v = fields.get(k)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[k] = v[:MAX_PICKS]
        return out

    def save(self, did, fields: dict, now: float):
        with self._lock:
            d = next((x for x in self._drafts if x["id"] == did), None) if did else None
            if d is None:
                d = {"id": uuid.uuid4().hex[:12], "created_ts": now,
                     "flavor_ids": [], "odor_ids": []}
                self._drafts.append(d)
            d.update(self._clean(fields))
            if not d.get("name"):
                d["name"] = "Untitled recipe"
            d["updated_ts"] = now
            self._save()
            return d

    def delete(self, did) -> bool:
        with self._lock:
            before = len(self._drafts)
            self._drafts = [d for d in self._drafts if d["id"] != did]
            changed = len(self._drafts) != before
            if changed:
                self._save()
            return changed

    def duplicate(self, did, now: float):
        with self._lock:
            d = next((x for x in self._drafts if x["id"] == did), None)
            if d is None:
                return None
            nd = copy.deepcopy(d)
            nd["id"] = uuid.uuid4().hex[:12]
            nd["name"] = (d.get("name") or "Untitled recipe") + " (copy)"
            nd["created_ts"] = nd["updated_ts"] = now
            self._drafts.append(nd)
            self._save()
            return nd


# ----------------------------------------------------------------------------
# Payload building
# ----------------------------------------------------------------------------

def downsample(points, max_buckets=400):
    """Time-bucket mean. points = [(ts, temp, sg)] sorted by ts."""
    if len(points) <= max_buckets:
        return [{"t": round(t), "temp": round(temp, 2), "sg": round(sg, 4)}
                for t, temp, sg in points]
    t0, t1 = points[0][0], points[-1][0]
    span = max(t1 - t0, 1)
    buckets = {}
    for t, temp, sg in points:
        i = min(int((t - t0) / span * max_buckets), max_buckets - 1)
        b = buckets.setdefault(i, [0, 0.0, 0.0, 0.0])
        b[0] += 1; b[1] += t; b[2] += temp; b[3] += sg
    out = []
    for i in sorted(buckets):
        n, st, stemp, ssg = buckets[i]
        out.append({"t": round(st / n), "temp": round(stemp / n, 2),
                    "sg": round(ssg / n, 4)})
    return out


def batch_window(mine, batch):
    """Readings belonging to the batch (or all readings if no batch)."""
    if not batch:
        return mine
    s, e = batch["start_ts"], batch.get("end_ts") or float("inf")
    return [r for r in mine if s <= r[0] <= e]


def stats_for(wrecs, batch, now):
    if not wrecs:
        return None
    og = (batch or {}).get("og_override") or wrecs[0][3]
    last = wrecs[-1]
    sg, temp_f = last[3], last[2]
    abv = max((og - sg) * 131.25, 0.0)     # standard (OG-FG)*131.25 formula
    atten = ((og - sg) / (og - 1.0) * 100.0) if og > 1.0 else 0.0
    temps = [r[2] for r in wrecs]
    # Battery age is only broadcast occasionally, and only by Tilt firmware
    # that supports it (see tilt_logger.py), so most readings carry no value
    # — find the most recent one that does.
    batt_rec = next((r for r in reversed(wrecs) if r[6] is not None), None)
    return {
        "sg": sg, "temp_f": temp_f,
        "temp_c": round((temp_f - 32) * 5 / 9, 2),
        "og": round(og, 4), "abv": round(abv, 2),
        "attenuation": round(atten, 1),
        "temp_min": round(min(temps), 1), "temp_max": round(max(temps), 1),
        "n": len(wrecs),
        "last_seen": round(last[0]), "rssi": last[4],
        "first_ts": round(wrecs[0][0]),
        "sg_24h_ago": next((r[3] for r in wrecs if r[0] >= last[0] - 86400), None),
        "battery_weeks": batt_rec[6] if batt_rec else None,
        "battery_ts": round(batt_rec[0]) if batt_rec else None,
    }


def series_window(wrecs, hours, now):
    if hours and hours > 0:
        cut = now - hours * 3600
        wrecs = [r for r in wrecs if r[0] >= cut]
    return downsample([(r[0], r[2], r[3]) for r in wrecs])


def tilt_summary(mine, batch, hours, now):
    wrecs = batch_window(mine, batch)
    return {
        "model": mine[-1][5] if mine else "standard",
        "batch": batch,
        "stats": stats_for(wrecs, batch, now),
        "series": series_window(wrecs, hours, now),
        "n_total": len(wrecs),
    }


def build_overview(cache: LogCache, store: BatchStore, hours: float):
    recs = cache.records()
    now = datetime.now(timezone.utc).timestamp()
    colors = sorted({r[1] for r in recs})
    tilts = []
    for c in colors:
        batch = store.active(c)
        if batch is None:
            continue  # no active batch: keep the colour tab, but no overview card/chart
        mine = [r for r in recs if r[1] == c]
        t = tilt_summary(mine, batch, hours, now)
        t["color"] = c
        tilts.append(t)
    return {"colors": colors, "tilts": tilts, "server_time": round(now)}


def build_detail(cache: LogCache, store: BatchStore, hours: float,
                 color=None, batch_id=None):
    recs = cache.records()
    now = datetime.now(timezone.utc).timestamp()
    colors = sorted({r[1] for r in recs})
    batch = None
    if batch_id:
        batch = store.by_id(batch_id)
        if batch is None:
            return {"error": "unknown batch", "colors": colors,
                    "server_time": round(now)}
        color = batch["color"]
        hours = 0  # a past batch always shows its whole window
    else:
        if not colors:
            return {"colors": [], "stats": None, "series": [], "recent": [],
                    "batch": None, "finished": False, "server_time": round(now)}
        if color not in colors:
            color = colors[0]
        batch = store.active(color)
    mine = [r for r in recs if r[1] == color]
    if batch_id is None and batch is None:
        # Live view, no current batch: once a batch finishes (or before one is
        # ever started) its readings only live in History, so show an empty
        # state here instead of falling back to all-time data.
        out = {"model": mine[-1][5] if mine else "standard", "batch": None,
               "stats": None, "series": [], "n_total": 0}
        wrecs = []
    else:
        out = tilt_summary(mine, batch, hours, now)
        wrecs = batch_window(mine, batch)
    if out["stats"] is None and batch and batch.get("snapshot"):
        out["stats"] = batch["snapshot"]   # data purged/rotated: use snapshot
    out.update({
        "colors": colors, "color": color,
        "finished": bool(batch and batch.get("end_ts")),
        "recent": [{"t": round(r[0]), "temp": r[2], "sg": r[3], "rssi": r[4]}
                   for r in wrecs[-50:]][::-1],
        "server_time": round(now),
    })
    return out


def build_history(cache: LogCache, store: BatchStore):
    recs = cache.records()
    now = datetime.now(timezone.utc).timestamp()
    out = []
    for b in store.finished():
        stats = b.get("snapshot")
        if not stats:
            mine = [r for r in recs if r[1] == b["color"]]
            stats = stats_for(batch_window(mine, b), b, now)
        out.append({
            "id": b["id"], "color": b["color"],
            "name": b.get("name") or "Unnamed batch",
            "style": b.get("style"), "yeast": b.get("yeast"),
            "start_ts": b["start_ts"], "end_ts": b["end_ts"],
            "image": b.get("image"), "stats": stats,
            "trimmed": bool(b.get("trimmed")),
        })
    return {"batches": out, "server_time": round(now)}


# ----------------------------------------------------------------------------
# Brew report (standalone printable HTML) + CSV export
# ----------------------------------------------------------------------------

RPT = {  # fixed parchment print palette
    "page": "#f7f0df", "surface": "#fdf8ec", "ink": "#241d12", "ink2": "#5d5340",
    "muted": "#87795d", "grid": "#ddd1b4", "axis": "#b9ab8a", "olive": "#6a6d3a",
}

def _nice_step(rng, cands):
    for c in cands:
        if rng / c <= 6:
            return c
    return cands[-1]


def _svg_chart(series, key, color, fmt, cands, target=None, W=900, H=250, annotations=None,
                stage_markers=None):
    """Static SVG line chart for the report (mirrors the dashboard styling).
    `annotations` (optional) is the batch's list of {ts, text} chart notes --
    each gets the same diamond marker used on the live dashboard, pinned to
    its nearest plotted point, so a downloaded report/archive carries the
    same visual record as the live charts. `stage_markers` (optional) is the
    batch's list of {ts, stage} fermentation-stage markers -- each gets the
    same labeled, full-height vertical dashed line used on the live dashboard
    (distinct from the annotation diamonds and the horizontal target line),
    since it marks a timeline boundary rather than one reading's value."""
    if not series:
        return "<p style='color:%s'>no data</p>" % RPT["muted"]
    ml, mr, mt, mb = 64, 16, 10, 26
    t0, t1 = series[0]["t"], series[-1]["t"] or series[0]["t"] + 1
    vals = [p[key] for p in series]
    lo, hi = min(vals), max(vals)
    if target is not None:
        lo, hi = min(lo, target), max(hi, target)
    if hi - lo < 1e-9:
        lo -= 0.5; hi += 0.5
    pad = (hi - lo) * 0.12; lo -= pad; hi += pad
    step = _nice_step(hi - lo, cands)
    lo = (lo // step) * step
    hi = -((-hi) // step) * step
    X = lambda t: ml + (t - t0) / max(t1 - t0, 1) * (W - ml - mr)
    Y = lambda v: mt + (hi - v) / (hi - lo) * (H - mt - mb)
    parts = ['<svg viewBox="0 0 %d %d" width="100%%" style="display:block">' % (W, H)]
    v = lo
    while v <= hi + 1e-9:
        parts.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/>'
                     % (ml, W - mr, Y(v), Y(v), RPT["grid"]))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" fill="%s">%s</text>'
                     % (ml - 8, Y(v) + 4, RPT["muted"], fmt(v)))
        v += step
    span = t1 - t0
    prev = None
    for i in range(6):
        tt = t0 + span * i / 5
        lbl = datetime.fromtimestamp(tt).strftime("%b %d" if span > 3*86400 else "%b %d %H:%M")
        if lbl == prev:
            continue
        prev = lbl
        anchor = "start" if i == 0 else ("end" if i == 5 else "middle")
        parts.append('<text x="%.1f" y="%d" text-anchor="%s" font-size="11" fill="%s">%s</text>'
                     % (X(tt), H - 8, anchor, RPT["muted"], lbl))
    parts.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/>'
                 % (ml, W - mr, H - mb, H - mb, RPT["axis"]))
    if target is not None:
        parts.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" stroke-dasharray="5 4"/>'
                     % (ml, W - mr, Y(target), Y(target), RPT["muted"]))
        parts.append('<text x="%d" y="%.1f" font-size="11" fill="%s">target %s</text>'
                     % (ml + 6, Y(target) - 5, RPT["muted"], fmt(target)))
    pts = ["%.1f %.1f" % (X(p["t"]), Y(p[key])) for p in series]
    path = "M" + "L".join(pts)
    parts.append('<path d="%sL%.1f %.1fL%.1f %.1fZ" fill="%s" opacity="0.1"/>'
                 % (path, X(series[-1]["t"]), H - mb, X(series[0]["t"]), H - mb, color))
    parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="2" '
                 'stroke-linejoin="round" stroke-linecap="round"/>' % (path, color))
    lx, ly = X(series[-1]["t"]), Y(series[-1][key])
    parts.append('<circle cx="%.1f" cy="%.1f" r="6" fill="%s"/>' % (lx, ly, RPT["surface"]))
    parts.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s"/>' % (lx, ly, color))
    parts.append('<text x="%.1f" y="%.1f" text-anchor="end" font-size="12" '
                 'font-weight="600" fill="%s">%s</text>'
                 % (lx - 10, max(ly - 10, 12), RPT["ink"], fmt(series[-1][key])))
    if annotations:
        span2 = max(t1 - t0, 1)
        tol = max(span2 / 40, 3600)
        for a in annotations:
            best, bd = None, float("inf")
            for p in series:
                d = abs(p["t"] - a["ts"])
                if d < bd:
                    bd, best = d, p
            if best is None or bd > tol:
                continue
            mx, my = X(best["t"]), Y(best[key])
            parts.append(
                '<path d="M%.1f %.1fL%.1f %.1fL%.1f %.1fL%.1f %.1fZ" '
                'fill="%s" stroke="%s" stroke-width="2"><title>%s — %s</title></path>'
                % (mx, my - 7, mx + 7, my, mx, my + 7, mx - 7, my,
                   RPT["surface"], RPT["ink"],
                   html_mod.escape(datetime.fromtimestamp(a["ts"]).strftime("%b %d, %H:%M")),
                   html_mod.escape(a["text"])))
    if stage_markers:
        for sm in stage_markers:
            sx = min(max(X(sm["ts"]), ml), W - mr)
            parts.append(
                '<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s" '
                'stroke-width="1.5" stroke-dasharray="2 3"/>'
                % (sx, sx, mt, H - mb, RPT["olive"]))
            parts.append(
                '<text x="%.1f" y="%d" font-size="11" font-weight="600" fill="%s" '
                'style="paint-order:stroke" stroke="%s" stroke-width="3">%s</text>'
                % (sx + 4, mt + 12, RPT["olive"], RPT["surface"], html_mod.escape(sm["stage"])))
    parts.append("</svg>")
    return "".join(parts)


def _hourly_rows(wrecs):
    """Aggregate raw readings to hourly means for the report table."""
    buckets = {}
    for r in wrecs:
        h = int(r[0] // 3600)
        b = buckets.setdefault(h, [0, 0.0, 0.0, 0.0])
        b[0] += 1; b[1] += r[3]; b[2] += r[2]
        b[3] += r[4] if r[4] is not None else 0
    rows = []
    for h in sorted(buckets):
        n, ssg, st, srssi = buckets[h]
        rows.append((h * 3600, ssg / n, st / n, srssi / n, n))
    return rows[:2400]


def build_report(cache, store, color=None, batch_id=None, brand=None):
    d = build_detail(cache, store, 0, color=color, batch_id=batch_id)
    # The "no colours at all" guard only makes sense for a live report (no
    # Tilt has ever logged anything): a historical batch_id is still valid
    # even if its colour's raw readings have since been purged/rotated away
    # entirely (e.g. a single-Tilt setup that was reset) -- its snapshot
    # stats should still produce a report, just without charts/table.
    if d.get("error") or (not batch_id and not d.get("colors")):
        return None
    brand = brand or {"name": DEFAULT_BRAND_NAME, "tagline": DEFAULT_BRAND_TAGLINE}
    b = d.get("batch") or {}
    no_active_batch = not batch_id and not d.get("batch")
    S = d.get("stats")
    esc = html_mod.escape
    model = d.get("model", "standard")
    nd = 4 if model == "pro" else 3
    fsg = lambda v: ("%." + str(nd) + "f") % v
    name = esc(b.get("name") or (d["color"] + " Tilt batch"))
    hue = {"Red": "#c23837", "Green": "#006300", "Black": "#5d5340",
           "Purple": "#4a3aa7", "Orange": "#c2521f", "Blue": "#1c5cab",
           "Yellow": "#8a6a00", "Pink": "#b04070"}.get(d["color"], "#1c5cab")

    meta = []
    def m(label, val):
        if val not in (None, "", "None"):
            meta.append("<div class='m'><div class='ml'>%s</div><div class='mv'>%s</div></div>"
                        % (label, val))
    fmt_d = lambda ts: datetime.fromtimestamp(ts).strftime("%b %d, %Y %H:%M")
    start = b.get("start_ts") or (S and S.get("first_ts"))
    end = b.get("end_ts")
    m("Tilt", esc(d["color"]) + (" Pro" if model == "pro" else ""))
    m("Style", esc(b.get("style") or ""))
    m("Yeast", esc(b.get("yeast") or ""))
    m("Batch size", esc(b.get("batch_size") or ""))
    m("IBU", b.get("ibu"))
    if start: m("Started", fmt_d(start))
    if not no_active_batch:
        m("Finished", fmt_d(end) if end else "in progress")
    if start:
        days = ((end or datetime.now().timestamp()) - start) / 86400
        m("Duration", "%.1f days" % days)
    if S:
        m("OG", fsg(S["og"]) + (" (measured)" if b.get("og_override") else ""))
        m("Current / final SG", fsg(S["sg"]))
        if b.get("target_fg"): m("Target FG", fsg(b["target_fg"]))
        m("Est. ABV", "%.2f%%" % S["abv"])
        m("Apparent attenuation", "%.1f%%" % S["attenuation"])
        m("Temp range", "%.1f–%.1f °F" % (S["temp_min"], S["temp_max"])
          if "temp_min" in S else None)
        m("Readings", "{:,}".format(S.get("n", d.get("n_total", 0))))
        if S.get("battery_weeks") is not None:
            m("Battery age (last reported)",
              "%d week%s since changed" % (S["battery_weeks"], "" if S["battery_weeks"] == 1 else "s"))

    # Mirror build_detail's live-view rule: with no batch_id (a live report) and
    # no current batch, there's nothing to show -- don't fall back to an
    # all-time dump for either the charts or the hourly table below.
    real_batch = d.get("batch")

    anns = b.get("annotations") or []
    stages = b.get("stage_markers") or []
    charts = ""
    if d["series"]:
        og = S.get("og") if S else None
        abv_series = ([{"t": p["t"], "abv": max((og - p["sg"]) * 131.25, 0)}
                       for p in d["series"]] if og is not None else [])
        target_abv = b.get("target_abv")
        try:
            target_abv = float(target_abv) if target_abv not in (None, "") else None
        except (TypeError, ValueError):
            target_abv = None
        charts = ("<h2>Specific gravity</h2>"
                  + _svg_chart(d["series"], "sg", hue, fsg,
                               [0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05],
                               b.get("target_fg"), annotations=anns, stage_markers=stages)
                  + "<h2>Est. ABV</h2>"
                  + (_svg_chart(abv_series, "abv", hue, lambda v: "%.1f%%" % v,
                                [0.5, 1, 2, 5, 10], target_abv, annotations=anns, stage_markers=stages)
                     if abv_series else "<p class='muted'>No OG on record yet.</p>")
                  + "<h2>Temperature (°F)</h2>"
                  + _svg_chart(d["series"], "temp", hue, lambda v: "%.1f" % v,
                               [0.5, 1, 2, 5, 10, 20], b.get("temp_target_f"), annotations=anns,
                               stage_markers=stages))
    elif no_active_batch:
        charts = "<p class='muted'>This Tilt has no active batch right now, so there's nothing to report — start a batch on it, or pull up a past brew from History instead.</p>"
    elif b.get("trimmed"):
        charts = ("<p class='muted'>This batch's raw readings were trimmed from the server%s "
                  "— see its archive file for the full charts and data.</p>"
                  % ((" on " + fmt_d(b["trimmed_ts"])) if b.get("trimmed_ts") else ""))
    else:
        charts = "<p class='muted'>No logged readings remain for this batch (data was reset or rotated).</p>"

    recs = cache.records()
    mine = [r for r in recs if r[1] == d["color"]]
    wrecs = [] if no_active_batch else batch_window(mine, real_batch)
    trs = []
    for ts, sg, tf, rssi, n in _hourly_rows(wrecs):
        trs.append("<tr><td>%s</td><td>%s</td><td>%.1f</td><td>%.1f</td><td>%.0f</td><td>%d</td></tr>"
                   % (datetime.fromtimestamp(ts).strftime("%b %d %H:00"),
                      fsg(sg), tf, (tf - 32) * 5 / 9, rssi, n))
    table = ("<table><thead><tr><th>Hour</th><th>SG avg</th><th>°F avg</th>"
             "<th>°C avg</th><th>RSSI avg</th><th>readings</th></tr></thead><tbody>"
             + "".join(trs) + "</tbody></table>") if trs else ""

    notes = ("<h2>Notes</h2><p class='notes'>%s</p>" % esc(b.get("notes"))
             ) if b.get("notes") else ""
    if stages:
        srows = "".join(
            "<div class='chartnote'><span class='cnwhen'>%s</span> %s</div>"
            % (esc(datetime.fromtimestamp(s["ts"]).strftime("%b %d, %Y %H:%M")), esc(s["stage"]))
            for s in sorted(stages, key=lambda s: s["ts"]))
        notes += "<h2>Fermentation stages</h2><div class='notes'>%s</div>" % srows
    if anns:
        rows = "".join(
            "<div class='chartnote'><span class='cnwhen'>%s</span> %s</div>"
            % (esc(datetime.fromtimestamp(a["ts"]).strftime("%b %d, %Y %H:%M")), esc(a["text"]))
            for a in sorted(anns, key=lambda a: a["ts"]))
        notes += "<h2>Chart notes</h2><div class='notes'>%s</div>" % rows
    csv_q = ("id=" + b["id"]) if b.get("id") else ("color=" + d["color"])
    gen = datetime.now().strftime("%b %d, %Y %H:%M")

    return REPORT_TMPL % {
        "name": name, "hue": hue, "meta": "".join(meta), "charts": charts,
        "notes": notes, "table": table, "csv_q": csv_q, "generated": gen,
        "brand_name": brand["name"], "brand_tagline": brand["tagline"],
        "brand_initials": _initials(brand["name"]),
        **RPT,
    }


REPORT_TMPL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(name)s — %(brand_name)s</title>
<link rel="icon" type="image/svg+xml" href="__FAVICON__">
<style>
 * { box-sizing:border-box; margin:0; }
 body { background:%(page)s; color:%(ink)s; padding:28px 22px;
        font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
        max-width:960px; margin:0 auto; }
 header { display:flex; align-items:center; gap:14px; border-bottom:2px solid %(axis)s;
          padding-bottom:14px; margin-bottom:18px; }
 header img { height:56px; filter:brightness(.55) contrast(1.1); }
 .brandmark { height:56px; width:56px; border-radius:50%%; flex:none;
              background:%(ink)s; color:%(page)s; display:flex;
              align-items:center; justify-content:center;
              font:700 20px Georgia,"Iowan Old Style",serif; }
 .wm { font:700 22px/1.1 Georgia,"Iowan Old Style",serif; letter-spacing:.05em; text-transform:uppercase; }
 .wm2 { font-size:11px; letter-spacing:.32em; color:%(olive)s; text-transform:uppercase; margin-top:3px; font-weight:600; }
 h1 { font:700 26px/1.2 Georgia,"Iowan Old Style",serif; margin:4px 0 2px; }
 .sub { color:%(muted)s; margin-bottom:16px; }
 .dot { display:inline-block; width:11px; height:11px; border-radius:50%%; background:%(hue)s; margin-right:7px; }
 h2 { font:600 13px Georgia,serif; letter-spacing:.16em; text-transform:uppercase;
      color:%(ink2)s; margin:22px 0 8px; }
 .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:10px; }
 .m { background:%(surface)s; border:1px solid %(grid)s; border-radius:8px; padding:9px 12px; }
 .ml { font-size:11px; color:%(muted)s; }
 .mv { font-size:15px; font-weight:600; }
 .notes { white-space:pre-wrap; background:%(surface)s; border:1px solid %(grid)s;
          border-radius:8px; padding:12px 14px; }
 .chartnote { padding:4px 0; border-bottom:1px solid %(grid)s; }
 .chartnote:last-child { border-bottom:none; }
 .cnwhen { font-weight:600; color:%(ink2)s; margin-right:8px; }
 table { width:100%%; border-collapse:collapse; font-size:12.5px; background:%(surface)s; }
 th,td { text-align:right; padding:5px 10px; border-bottom:1px solid %(grid)s;
         font-variant-numeric:tabular-nums; }
 th:first-child,td:first-child { text-align:left; }
 th { color:%(muted)s; font-weight:500; }
 .muted { color:%(muted)s; }
 footer { text-align:center; color:%(muted)s; font-size:11px; letter-spacing:.3em;
          text-transform:uppercase; margin:30px 0 6px; }
 .gen { text-align:center; color:%(muted)s; font-size:11px; letter-spacing:0; text-transform:none; }
 .noprint { margin:14px 0; }
 .noprint a { color:%(ink2)s; }
 @media print { .noprint { display:none; } body { background:#fff; } }
</style></head><body>
<header>
  <div class="brandmark">%(brand_initials)s</div>
  <div><div class="wm">%(brand_name)s</div><div class="wm2">Brew Report</div></div>
</header>
<h1><span class="dot"></span>%(name)s</h1>
<div class="sub">Fermentation report</div>
<div class="noprint">Print this page (Ctrl/Cmd-P) to save as PDF &middot;
  <a href="/api/export.csv?%(csv_q)s">download all raw readings (CSV)</a></div>
<div class="grid">%(meta)s</div>
%(notes)s
%(charts)s
<h2>Data table (hourly averages)</h2>
%(table)s
<div class="noprint" style="margin-top:8px"><a href="/api/export.csv?%(csv_q)s">Full raw data as CSV</a></div>
<footer>%(brand_tagline)s</footer>
<div class="gen">Generated %(generated)s</div>
</body></html>
"""


def build_csv(cache, store, color=None, batch_id=None):
    b = store.by_id(batch_id) if batch_id else (store.active(color) if color else None)
    if batch_id and b is None:
        return None, None
    if b:
        color = b["color"]
    recs = cache.records()
    mine = [r for r in recs if r[1] == color]
    # Same live-view rule as the overview/detail/report: no batch_id and no
    # current batch means nothing to export yet (its past data, if any, is
    # reachable by exporting its finished batch from History instead).
    wrecs = [] if (not batch_id and b is None) else batch_window(mine, b)
    lines = ["timestamp,color,sg,temp_f,temp_c,rssi_dbm,model,battery_weeks"]
    for r in wrecs:
        lines.append("%s,%s,%s,%s,%.2f,%s,%s,%s" % (
            datetime.fromtimestamp(r[0]).isoformat(timespec="seconds"),
            r[1], r[3], r[2], (r[2] - 32) * 5 / 9,
            "" if r[4] is None else r[4], r[5],
            "" if r[6] is None else r[6]))
    fname = (((b.get("name") if b else None) or color or "tilt").replace(" ", "_")[:40] or "tilt") + ".csv"
    fname = "".join(ch for ch in fname if ch.isalnum() or ch in "._-")
    return "\n".join(lines) + "\n", fname


ARCHIVE_FORMAT_VERSION = 1


def build_archive(cache, store, batch_id, brand=None):
    """A standalone, self-contained HTML file for one FINISHED batch: the
    same printable report a person would read, plus every one of that
    batch's raw readings (and its full editable fields) embedded as a JSON
    block so the file can later be read back in -- even after the server's
    own copy of the raw data has been trimmed or rotated away -- to either
    view it again or pre-fill a "Rebrew from archive" on a free Tilt.

    Only offered for finished batches: an active batch's window is still
    open, so there's nothing stable yet to freeze into an archive.
    """
    b = store.by_id(batch_id)
    if b is None or not b.get("end_ts"):
        return None, None

    report_html = build_report(cache, store, batch_id=batch_id, brand=brand)
    if report_html is None:
        return None, None

    recs = cache.records()
    mine = [r for r in recs if r[1] == b["color"]]
    wrecs = batch_window(mine, b)
    # Compact arrays (not objects) to keep the embedded payload reasonably
    # sized -- same columns as the CSV export, full resolution, no averaging.
    readings = [[round(r[0], 3), r[3], r[2], r[4], r[5], r[6]] for r in wrecs]

    payload = {
        "tilt_archive": ARCHIVE_FORMAT_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "batch": {k: v for k, v in b.items() if k not in ("snapshot",)},
        "snapshot": b.get("snapshot"),
        "readings_columns": ["t", "sg", "temp_f", "rssi_dbm", "model", "battery_weeks"],
        "readings": readings,
    }
    script = ('\n<script type="application/json" id="tilt-dashboard-archive">'
              + json.dumps(payload) + "</script>\n")
    html = report_html.replace("</body>", script + "</body>")

    base = (b.get("name") or (b["color"] + " batch")).strip().replace(" ", "_")[:40] or "batch"
    base = "".join(ch for ch in base if ch.isalnum() or ch in "._-")
    date = datetime.fromtimestamp(b["start_ts"]).strftime("%Y-%m-%d")
    fname = "%s_%s_%s.archive.html" % (base, b["color"], date)
    return html, fname


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    cache: LogCache = None      # set in main()
    store: BatchStore = None
    settings: SettingsStore = None
    recipe: RecipeStore = None
    stages: StageStore = None
    drafts: DraftStore = None
    allow_updates = False       # web-based software updates (--allow-updates)

    def _send(self, code, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            hours = float(q.get("hours", ["0"])[0])
        except ValueError:
            hours = 0.0
        color = q.get("color", [None])[0]
        batch_id = q.get("batch_id", [None])[0] or q.get("id", [None])[0]

        if url.path == "/":
            self._send(200, _with_brand(PAGE, self.brand.get()).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif url.path == "/report":
            page = build_report(self.cache, self.store, color=color, batch_id=batch_id,
                                brand=self.brand.get())
            if page is None:
                self._send(404, b"batch not found", "text/plain")
            else:
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/overview":
            self._json({**build_overview(self.cache, self.store, hours),
                        "battery": self.cache.battery_by_color()})
        elif url.path == "/api/data":
            self._json({**build_detail(self.cache, self.store, hours,
                                       color=color, batch_id=batch_id),
                        "battery": self.cache.battery_by_color()})
        elif url.path == "/api/history":
            self._json({**build_history(self.cache, self.store),
                        "battery": self.cache.battery_by_color()})
        elif url.path == "/api/admin":
            self._json({**self._admin_payload(),
                        "battery": self.cache.battery_by_color()})
        elif url.path == "/guide":
            self._send(200, _with_brand(GUIDE, self.brand.get()).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif url.path == "/api/settings":
            self._json({**self.settings.get(),
                        "allow_updates": self.allow_updates})
        elif url.path == "/api/recipe":
            self._json(self.recipe.get())
        elif url.path == "/api/stages":
            self._json({"stages": self.stages.list()})
        elif url.path == "/recipes":
            self._send(200, _with_brand(RECIPES_TMPL, self.brand.get()).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif url.path == "/api/drafts":
            self._json({"drafts": self.drafts.list()})
        elif url.path == "/api/export.csv":
            csv_text, fname = build_csv(self.cache, self.store,
                                        color=color, batch_id=batch_id)
            if csv_text is None:
                self._send(404, b"batch not found", "text/plain")
            else:
                self._send(200, csv_text.encode("utf-8"), "text/csv; charset=utf-8",
                           {"Content-Disposition": 'attachment; filename="%s"' % fname})
        elif url.path == "/api/archive":
            if not batch_id:
                self._send(400, b"id is required", "text/plain")
            else:
                html, fname = build_archive(self.cache, self.store, batch_id,
                                            brand=self.brand.get())
                if html is None:
                    self._send(404, b"batch not found, or it isn't finished yet",
                              "text/plain")
                else:
                    self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                               {"Content-Disposition": 'attachment; filename="%s"' % fname})
        else:
            self._send(404, b"not found", "text/plain")

    def _admin_payload(self):
        recs = self.cache.records()
        now = datetime.now(timezone.utc).timestamp()
        colors = sorted({r[1] for r in recs})
        tilts = []
        for c in colors:
            mine = [r for r in recs if r[1] == c]
            tilts.append({"color": c, "n": len(mine),
                          "first_ts": round(mine[0][0]),
                          "last_ts": round(mine[-1][0])})
        try:
            log_bytes = os.path.getsize(self.cache.path)
        except OSError:
            log_bytes = 0
        here = os.path.abspath(__file__)
        logger_path = os.path.join(os.path.dirname(here), "tilt_logger.py")
        mt = lambda p: round(os.path.getmtime(p)) if os.path.exists(p) else None
        return {
            "colors": colors, "tilts": tilts,
            "log": {"path": self.cache.path, "bytes": log_bytes},
            "batches_path": self.store.path,
            "interval": self.settings.get()["interval"],
            "allow_updates": self.allow_updates,
            "versions": {"dashboard": mt(here), "logger": mt(logger_path)},
            "server_time": round(now),
            "brand": self.brand.get(),
        }

    def _read_body(self, cap):
        try:
            length = min(int(self.headers.get("Content-Length", 0)), cap)
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return None

    def _snapshot(self, color):
        recs = self.cache.records()
        mine = [r for r in recs if r[1] == color]
        b = self.store.active(color)
        now = datetime.now(timezone.utc).timestamp()
        return stats_for(batch_window(mine, b), b, now)

    def do_POST(self):
        url = urlparse(self.path)
        now = datetime.now(timezone.utc).timestamp()

        if url.path == "/api/settings":
            body = self._read_body(4096)
            if body is None or "interval" not in body:
                return self._json({"error": "bad request"}, 400)
            try:
                self.settings.set_interval(float(body["interval"]))
            except (TypeError, ValueError):
                return self._json({"error": "bad interval"}, 400)
            return self._json({"ok": True, **self.settings.get()})

        if url.path == "/api/brand":
            body = self._read_body(4096)
            if body is None:
                return self._json({"error": "bad request"}, 400)
            name, tagline = body.get("name"), body.get("tagline")
            if not isinstance(name, str) or not isinstance(tagline, str):
                return self._json({"error": "bad request"}, 400)
            return self._json({"ok": True, **self.brand.set(name, tagline)})

        if url.path == "/api/reset":
            body = self._read_body(4096)
            color = (body or {}).get("color")
            if not isinstance(color, str) or not color:
                return self._json({"error": "bad request"}, 400)
            removed = self.cache.purge_color(color)
            deleted = self.store.delete_active(color)
            return self._json({"ok": True, "readings_removed": removed,
                               "batch_deleted": bool(deleted)})

        if url.path == "/api/archive/trim":
            body = self._read_body(4096)
            batch_id = (body or {}).get("id")
            if not isinstance(batch_id, str) or not batch_id:
                return self._json({"error": "bad request"}, 400)
            b = self.store.by_id(batch_id)
            if b is None:
                return self._json({"error": "batch not found"}, 404)
            if not b.get("end_ts"):
                return self._json({"error": "only a finished batch can be trimmed"}, 400)
            removed = self.cache.purge_window(b["color"], b["start_ts"], b["end_ts"])
            self.store.mark_trimmed(batch_id, removed, now)
            return self._json({"ok": True, "readings_removed": removed})

        if url.path == "/api/batch/annotate":
            body = self._read_body(8192)
            batch_id = (body or {}).get("id")
            ts = (body or {}).get("ts")
            text = str((body or {}).get("text") or "").strip()
            if (not isinstance(batch_id, str) or not batch_id
                    or not isinstance(ts, (int, float))
                    or not text):
                return self._json({"error": "bad request"}, 400)
            b = self.store.by_id(batch_id)
            if b is None:
                return self._json({"error": "batch not found"}, 404)
            # keep notes pinned to this batch's own window (with a little slack
            # for clock/rounding) rather than an arbitrary timestamp
            lo, hi = b["start_ts"] - 3600, (b.get("end_ts") or now) + 3600
            if not (lo <= ts <= hi):
                return self._json({"error": "timestamp outside this batch's window"}, 400)
            updated = self.store.add_annotation(batch_id, float(ts), text, now)
            return self._json({"ok": True, "annotations": updated.get("annotations", [])})

        if url.path == "/api/batch/stage":
            body = self._read_body(4096)
            batch_id = (body or {}).get("id")
            ts = (body or {}).get("ts")
            stage = str((body or {}).get("stage") or "").strip()
            if (not isinstance(batch_id, str) or not batch_id
                    or not isinstance(ts, (int, float))
                    or not stage):
                return self._json({"error": "bad request"}, 400)
            if stage not in self.stages.names():
                return self._json({"error": "unknown fermentation stage"}, 400)
            b = self.store.by_id(batch_id)
            if b is None:
                return self._json({"error": "batch not found"}, 404)
            # same window-bound rule as chart notes: a stage marker is pinned
            # to this specific batch's own timeline, not an arbitrary moment
            lo, hi = b["start_ts"] - 3600, (b.get("end_ts") or now) + 3600
            if not (lo <= ts <= hi):
                return self._json({"error": "timestamp outside this batch's window"}, 400)
            updated = self.store.add_stage_marker(batch_id, float(ts), stage, now)
            return self._json({"ok": True, "stage_markers": updated.get("stage_markers", [])})

        if url.path == "/api/stages":
            body = self._read_body(4096)
            action = (body or {}).get("action")
            if action == "add":
                name = str((body or {}).get("name") or "").strip()[:MAX_STAGE_NAME]
                if not name:
                    return self._json({"error": "name is required"}, 400)
                sid = self.stages.add(name)
                return self._json({"ok": True, "id": sid, "stages": self.stages.list()})
            if action == "rename":
                sid = (body or {}).get("id")
                name = str((body or {}).get("name") or "").strip()[:MAX_STAGE_NAME]
                if not isinstance(sid, str) or not name or not self.stages.rename(sid, name):
                    return self._json({"error": "stage not found"}, 404)
                return self._json({"ok": True, "stages": self.stages.list()})
            if action == "delete":
                sid = (body or {}).get("id")
                if not isinstance(sid, str) or not self.stages.delete(sid):
                    return self._json({"error": "stage not found"}, 404)
                return self._json({"ok": True, "stages": self.stages.list()})
            if action == "reset":
                self.stages.reset()
                return self._json({"ok": True, "stages": self.stages.list()})
            return self._json({"error": "unknown action"}, 400)

        if url.path == "/api/history/delete":
            body = self._read_body(65536)
            ids = (body or {}).get("ids")
            if (not isinstance(ids, list)
                    or not all(isinstance(i, str) for i in ids)):
                return self._json({"error": "bad request"}, 400)
            removed = self.store.delete_finished(ids)
            return self._json({"ok": True, "deleted": removed})

        if url.path == "/api/recipe":
            body = self._read_body(20000)
            if body is None:
                return self._json({"error": "bad request"}, 400)
            action = body.get("action")
            wheel = body.get("wheel")
            if action == "reset":
                if wheel not in ("flavor", "odor", "all"):
                    return self._json({"error": "bad wheel"}, 400)
                self.recipe.reset(None if wheel == "all" else wheel)
                return self._json({"ok": True, **self.recipe.get()})
            if wheel not in ("flavor", "odor"):
                return self._json({"error": "bad wheel"}, 400)

            def clean_hex(v):
                v = (v or "").strip()
                return v if HEX_RE.match(v) else None

            if action == "add_category":
                name = str(body.get("name") or "").strip()[:80]
                hexv = clean_hex(body.get("hex"))
                if not name or not hexv:
                    return self._json({"error": "name and a #RRGGBB color are required"}, 400)
                cid = self.recipe.add_category(wheel, name, hexv)
                return self._json({"ok": True, "id": cid, **self.recipe.get()})
            if action == "edit_category":
                cid = body.get("id")
                if not isinstance(cid, str):
                    return self._json({"error": "bad request"}, 400)
                ok = self.recipe.edit_category(wheel, cid,
                    name=(str(body["name"]).strip()[:80] if body.get("name") else None),
                    hex_=clean_hex(body.get("hex")) if body.get("hex") else None)
                if not ok:
                    return self._json({"error": "category not found"}, 404)
                return self._json({"ok": True, **self.recipe.get()})
            if action == "delete_category":
                cid = body.get("id")
                if not isinstance(cid, str) or not self.recipe.delete_category(wheel, cid):
                    return self._json({"error": "category not found"}, 404)
                return self._json({"ok": True, **self.recipe.get()})
            if action == "add_ingredient":
                cid = body.get("category_id")
                name = str(body.get("name") or "").strip()[:80]
                role = str(body.get("role") or "").strip()[:400]
                if not isinstance(cid, str) or not name or not role:
                    return self._json({"error": "category, name, and role are required"}, 400)
                iid = self.recipe.add_ingredient(wheel, cid, name, role)
                if iid is None:
                    return self._json({"error": "category not found"}, 404)
                return self._json({"ok": True, "id": iid, **self.recipe.get()})
            if action == "edit_ingredient":
                cid, iid = body.get("category_id"), body.get("id")
                if not isinstance(cid, str) or not isinstance(iid, str):
                    return self._json({"error": "bad request"}, 400)
                ok = self.recipe.edit_ingredient(wheel, cid, iid,
                    name=(str(body["name"]).strip()[:80] if body.get("name") else None),
                    role=(str(body["role"]).strip()[:400] if body.get("role") else None))
                if not ok:
                    return self._json({"error": "ingredient not found"}, 404)
                return self._json({"ok": True, **self.recipe.get()})
            if action == "delete_ingredient":
                cid, iid = body.get("category_id"), body.get("id")
                if (not isinstance(cid, str) or not isinstance(iid, str)
                        or not self.recipe.delete_ingredient(wheel, cid, iid)):
                    return self._json({"error": "ingredient not found"}, 404)
                return self._json({"ok": True, **self.recipe.get()})
            return self._json({"error": "unknown action"}, 400)

        if url.path == "/api/drafts":
            body = self._read_body(200000)
            if body is None:
                return self._json({"error": "bad request"}, 400)
            action = body.get("action")
            if action == "save":
                did = body.get("id")
                fields = body.get("fields")
                if not isinstance(fields, dict) or (did is not None and not isinstance(did, str)):
                    return self._json({"error": "bad request"}, 400)
                d = self.drafts.save(did, fields, now)
                return self._json({"ok": True, "draft": d})
            if action == "delete":
                did = body.get("id")
                if not isinstance(did, str) or not self.drafts.delete(did):
                    return self._json({"error": "recipe not found"}, 404)
                return self._json({"ok": True})
            if action == "duplicate":
                did = body.get("id")
                d = self.drafts.duplicate(did, now) if isinstance(did, str) else None
                if d is None:
                    return self._json({"error": "recipe not found"}, 404)
                return self._json({"ok": True, "draft": d})
            return self._json({"error": "unknown action"}, 400)

        if url.path == "/api/batch":
            body = self._read_body(1_000_000)
            if body is None:
                return self._json({"error": "bad request"}, 400)
            action = body.get("action")
            color = body.get("color", "")
            fields = body.get("batch") or {}
            if not isinstance(fields, dict) or not isinstance(color, str):
                return self._json({"error": "bad request"}, 400)
            recs = self.cache.records()
            mine = [r for r in recs if r[1] == color]
            if not mine and action != "save":
                return self._json({"error": "unknown tilt colour"}, 400)

            if action == "save":
                start = mine[0][0] if mine else now
                b = self.store.save_fields(color, fields, default_start=start)
            elif action == "new":
                b = self.store.new_batch(color, fields, now,
                                         snapshot=self._snapshot(color))
            elif action == "finish":
                b = self.store.finish(color, now, snapshot=self._snapshot(color))
            else:
                return self._json({"error": "unknown action"}, 400)
            return self._json({"ok": True, "batch": b})

        if url.path == "/api/update":
            if not self.allow_updates:
                return self._json({"error": "updates disabled — start the "
                                   "dashboard with --allow-updates"}, 403)
            body = self._read_body(5_000_000)
            target = (body or {}).get("target")
            source = (body or {}).get("source")
            if target not in ("dashboard", "logger") or not isinstance(source, str):
                return self._json({"error": "bad request"}, 400)
            marker = "tilt_dashboard" if target == "dashboard" else "tilt_logger"
            if marker not in source[:4000]:
                return self._json({"error": "that file doesn't look like %s.py "
                                   "— wrong file selected?" % marker}, 400)
            try:
                compile(source, marker + ".py", "exec")
            except SyntaxError as e:
                return self._json({"error": "syntax error in uploaded file: "
                                   "line %s: %s" % (e.lineno, e.msg)}, 400)
            here = os.path.abspath(__file__)
            path = here if target == "dashboard" else \
                os.path.join(os.path.dirname(here), "tilt_logger.py")
            try:
                if os.path.exists(path):        # keep a rollback copy
                    with open(path, "r", encoding="utf-8") as f:
                        old = f.read()
                    with open(path + ".bak", "w", encoding="utf-8") as f:
                        f.write(old)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(source)
                os.chmod(tmp, 0o755)
                os.replace(tmp, path)
            except OSError as e:
                return self._json({"error": "could not write %s: %s. Is the "
                                   "install directory owned by the service "
                                   "user? (sudo chown -R tilt:tilt %s)"
                                   % (path, e, os.path.dirname(here))}, 500)
            restarting = False
            if target == "dashboard":
                restarting = True
                def _reexec():   # replace this process with the new code;
                    os.execv(sys.executable,   # systemd sees the same service
                             [sys.executable, here] + sys.argv[1:])
                threading.Timer(1.0, _reexec).start()
            return self._json({"ok": True, "path": path, "restarting": restarting,
                               "note": None if restarting else
                               "Logger file installed. It restarts automatically "
                               "if the tilt-logger-watch units are installed; "
                               "otherwise run: sudo systemctl restart tilt-logger"})

        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # quiet access log
        pass


# ----------------------------------------------------------------------------
# The dashboard page (self-contained: no CDN, works on offline networks)
# ----------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__BRAND_NAME__ — Fermentation</title>
<link rel="icon" type="image/png" href="__FAVICON__">
<style>
  /* Default theme palette — parchment / black / tan / olive / sepia */
  :root {
    color-scheme: light;
    --page:#e7dcc3; --surface:#f2ead6; --ink:#241d12; --ink-2:#5d5340;
    --muted:#87795d; --grid:#d9ccae; --axis:#b9ab8a;
    --border:rgba(60,45,20,.18); --accent:#6d4f2a; --accent-ink:#f2ead6;
    --olive:#6a6d3a; --good:#5c6231; --danger:#a83232;
    --logo-filter:brightness(.55) contrast(1.1);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --page:#0d0b08; --surface:#171410; --ink:#ede3cf; --ink-2:#c8bba0;
      --muted:#8f8570; --grid:#2a251d; --axis:#3e372c;
      --border:rgba(237,227,207,.13); --accent:#d2ae87; --accent-ink:#221a10;
      --olive:#8b8b52; --good:#a3a86b; --danger:#e06060;
      --logo-filter:none;
    }
  }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--page); color:var(--ink);
         font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
         padding:20px; max-width:1060px; margin:0 auto; }
  header { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-bottom:16px; }
  .brand { display:flex; align-items:center; gap:12px; }
  .brandmark { height:46px; width:46px; border-radius:50%; flex:none;
               background:var(--ink); color:var(--page); display:flex;
               align-items:center; justify-content:center;
               font:700 17px Georgia,"Iowan Old Style",serif; }
  .wm { font:700 20px/1.1 Georgia,"Iowan Old Style","Times New Roman",serif;
        letter-spacing:.05em; text-transform:uppercase; }
  .wm2 { font-size:10.5px; letter-spacing:.34em; color:var(--olive);
         text-transform:uppercase; margin-top:3px; font-weight:600; }
  #sub { color:var(--muted); font-size:13px; }
  footer { text-align:center; color:var(--muted); font-size:11px;
           letter-spacing:.3em; text-transform:uppercase; margin:26px 0 8px; }
  .filters { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }
  .seg { display:flex; border:1px solid var(--border); border-radius:8px; overflow:hidden; background:var(--surface); }
  .seg:empty { display:none; }
  .seg button { border:0; background:transparent; color:var(--ink-2); padding:6px 12px;
                font:inherit; font-size:13px; cursor:pointer; white-space:nowrap; }
  .seg button[aria-pressed="true"] { background:var(--accent); color:var(--accent-ink); }
  #nav button { display:flex; flex-direction:column; align-items:center; gap:3px; padding-bottom:5px; }
  .navrow { display:flex; align-items:center; }
  .battbadge { display:none; line-height:0; color:var(--ink-2); }
  .battbadge svg { display:block; }
  select { font:inherit; font-size:13px; background:var(--surface); color:var(--ink);
           border:1px solid var(--border); border-radius:8px; padding:6px 8px; }
  .spacer { flex:1; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
           gap:10px; margin-bottom:16px; }
  .tile { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:12px 14px; }
  .tile .lbl { color:var(--muted); font-size:12px; margin-bottom:2px; }
  .tile .val { font-size:26px; font-weight:600; letter-spacing:-.01em; }
  .tile .delta { font-size:12px; color:var(--ink-2); margin-top:2px; }
  .tile .delta.down { color:var(--good); }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:10px;
          padding:14px 14px 6px; margin-bottom:14px; transition:opacity .2s; }
  .card h2 { font:600 12px Georgia,"Iowan Old Style","Times New Roman",serif;
             letter-spacing:.18em; text-transform:uppercase;
             color:var(--ink-2); margin-bottom:6px; }
  .card.loading { opacity:.55; }
  .legend { display:flex; gap:16px; flex-wrap:wrap; margin:2px 0 8px; font-size:12px; color:var(--ink-2); }
  .legend .key { display:inline-block; width:16px; height:0; border-top:3px solid;
                 border-radius:2px; margin-right:6px; vertical-align:middle; }
  svg { display:block; width:100%; }
  svg text { font:11px system-ui,-apple-system,"Segoe UI",sans-serif; fill:var(--muted); }
  .tt { position:fixed; pointer-events:none; background:var(--surface); color:var(--ink);
        border:1px solid var(--border); border-radius:8px; padding:8px 10px; font-size:12px;
        box-shadow:0 4px 14px rgba(0,0,0,.18); display:none; z-index:9; min-width:160px; }
  .tt .when { color:var(--muted); margin-bottom:4px; }
  .tt .row { display:flex; align-items:center; gap:7px; margin-top:2px; }
  .tt .key { width:14px; height:0; border-top:3px solid; border-radius:2px; flex:none; }
  .tt .v { font-weight:650; }
  .tt .n { color:var(--ink-2); }
  /* batch cards (overview + history) */
  .bcards { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
            gap:10px; margin-bottom:16px; }
  .bcard { background:var(--surface); border:1px solid var(--border); border-radius:10px;
           padding:14px; cursor:pointer; }
  .bcard:hover { border-color:var(--accent); }
  .bcard .bimg { width:100%; height:118px; object-fit:cover; border-radius:8px;
                 margin-bottom:10px; display:block; }
  .bcard .who { font-size:12px; color:var(--muted); margin-bottom:2px;
                display:flex; align-items:center; gap:6px; }
  .bcard .who .dot { margin-right:0; }
  .cardbatt { line-height:0; color:var(--ink-2); }
  .cardbatt svg { display:block; }
  .bcard .bname { font-size:15px; font-weight:650; margin-bottom:1px; }
  .bcard .bmeta { font-size:12px; color:var(--ink-2); margin-bottom:8px; }
  .bcard .sgnow { font-size:28px; font-weight:600; letter-spacing:-.01em; }
  .bcard .row2 { display:flex; gap:14px; margin-top:6px; font-size:12px; color:var(--ink-2); }
  .bcard .row2 b { color:var(--ink); font-weight:600; }
  /* history selection mode */
  .histbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
  .histbar:empty { display:none; }
  .bcard { position:relative; }
  .bcard.sel { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); }
  .bcard .selmark { position:absolute; top:10px; right:10px; width:22px; height:22px;
    border-radius:50%; border:1.5px solid var(--muted); background:var(--surface);
    display:flex; align-items:center; justify-content:center;
    font-size:14px; color:transparent; }
  .bcard.sel .selmark { background:var(--accent); border-color:var(--accent);
    color:var(--accent-ink); }
  /* batch info strip (detail view) */
  .binfo { background:var(--surface); border:1px solid var(--border); border-radius:10px;
           padding:14px; margin-bottom:14px; display:flex; gap:14px; align-items:stretch;
           flex-wrap:wrap; }
  .binfo-main { flex:1 1 320px; min-width:0; display:flex; gap:14px; align-items:flex-start;
                justify-content:space-between; flex-wrap:wrap; }
  .binfo .bleft { display:flex; gap:14px; align-items:flex-start; min-width:0; }
  .binfo .bthumb { width:86px; height:86px; object-fit:cover; border-radius:10px; flex:none; }
  .binfo .bname { font-size:16px; font-weight:650; }
  .binfo .bmeta { font-size:12.5px; color:var(--ink-2); margin-top:2px; }
  .binfo .bnotes { font-size:13px; color:var(--ink-2); margin-top:8px; white-space:pre-wrap; }
  /* chart notes panel -- a scrollable frame on the right of the batch info
     strip listing every note pinned to a chart point, timestamp first */
  .binfo-notes { flex:0 0 240px; max-width:100%; border:1px solid var(--border);
                 border-radius:8px; background:var(--page); padding:8px 10px;
                 display:flex; flex-direction:column; }
  .notes-head { font-size:11px; font-weight:650; text-transform:uppercase;
                letter-spacing:.06em; color:var(--ink-2); margin-bottom:6px; }
  .notes-scroll { overflow-y:auto; max-height:140px; display:flex;
                  flex-direction:column; gap:8px; }
  .notes-empty { font-size:12px; color:var(--muted); }
  .note-row { font-size:12.5px; }
  .note-when { font-weight:650; color:var(--ink-2); margin-bottom:1px; }
  .note-text { color:var(--ink); white-space:pre-wrap; }
  /* shown while a chart click is armed for something other than a plain note
     (set primary start / mark a stage) -- makes it obvious the next click
     does something special, with an easy way out */
  .pickbanner { background:var(--olive); color:#fff; border-radius:9px;
                padding:9px 14px; margin-bottom:14px; display:flex; align-items:center;
                justify-content:space-between; gap:12px; font-size:13.5px; font-weight:600; }
  .pickbanner[hidden] { display:none; }   /* unconditional display:flex above would
    otherwise win over the browser's default [hidden] rule (author styles beat the
    user-agent stylesheet at equal specificity), leaving the banner stuck on screen
    forever, Cancel included -- this re-asserts display:none whenever the attribute
    is actually set, e.g. by cancelPickMode(). */
  .pickbanner .link { color:#fff; text-decoration:underline; font-weight:600; }
  .btns { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
  .btn { font:inherit; font-size:13px; padding:6px 12px; border-radius:8px; cursor:pointer;
         border:1px solid var(--border); background:var(--surface); color:var(--ink);
         text-decoration:none; display:inline-block; }
  .btn.primary { background:var(--accent); border-color:var(--accent); color:var(--accent-ink); }
  .btn.danger { color:var(--danger); }
  .badge { display:inline-block; font-size:11px; letter-spacing:.12em; text-transform:uppercase;
           color:var(--olive); border:1px solid var(--olive); border-radius:6px;
           padding:1px 7px; margin-left:8px; vertical-align:2px; }
  #tablewrap { display:none; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:right; padding:6px 10px; border-bottom:1px solid var(--grid);
          font-variant-numeric:tabular-nums; }
  th:first-child,td:first-child { text-align:left; }
  th { color:var(--muted); font-weight:500; }
  .link { background:none; border:none; color:var(--ink-2); font:inherit; font-size:13px;
          cursor:pointer; text-decoration:underline; padding:6px 4px; }
  #empty { color:var(--muted); padding:30px 0 40px; text-align:center; display:none; }
  #empty img { width:min(300px,70vw); height:auto; display:block; margin:0 auto 18px; }
  /* modal */
  #overlay { position:fixed; inset:0; background:rgba(0,0,0,.45); display:none;
             align-items:flex-start; justify-content:center; z-index:20; overflow:auto; padding:30px 12px; }
  #modal { background:var(--surface); border:1px solid var(--border); border-radius:12px;
           padding:20px; width:min(560px,100%); }
  #modal h3 { font-size:16px; margin-bottom:12px; }
  .frow { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  .fld { margin-bottom:10px; }
  .fld label { display:block; font-size:12px; color:var(--muted); margin-bottom:3px; }
  .fld input, .fld select, .fld textarea { width:100%; font:inherit; font-size:13.5px; color:var(--ink);
        background:var(--page); border:1px solid var(--border); border-radius:8px; padding:7px 9px; }
  .fld textarea { min-height:64px; resize:vertical; }
  .imgrow { display:flex; gap:10px; align-items:center; }
  .imgrow img { width:64px; height:64px; object-fit:cover; border-radius:8px;
                border:1px solid var(--border); display:none; }
  .mbtns { display:flex; gap:8px; margin-top:14px; flex-wrap:wrap; }
  .mbtns .spacer { flex:1; }
  .calc { margin-top:4px; border:1px solid var(--border); border-radius:10px; }
  .calc summary { cursor:pointer; padding:9px 12px; font-size:13px; font-weight:600;
                  color:var(--ink-2); list-style:none; user-select:none; }
  .calc summary::before { content:"\25B8  "; color:var(--muted); }
  .calc[open] summary::before { content:"\25BE  "; }
  .calc .cbody { padding:2px 12px 12px; }
  .cout { background:var(--page); border:1px solid var(--border); border-radius:8px;
          padding:10px 12px; font-size:13px; margin-top:2px; line-height:1.6; }
  .cout .big { font-size:19px; font-weight:650; }
  .cout b { font-weight:650; }
  .chint { font-size:11.5px; color:var(--muted); margin-top:8px; }
  .rbh { font-size:12.5px; font-weight:650; color:var(--ink-2); margin:16px 0 7px;
         text-transform:uppercase; letter-spacing:.06em; }
  .rbh:first-child { margin-top:2px; }
  .rbhint { font-size:11px; font-weight:400; text-transform:none; letter-spacing:0;
            color:var(--muted); margin-left:6px; }
  .pillrow { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
  .pill { border:1px solid var(--border); background:var(--page); color:var(--ink-2);
          border-radius:999px; padding:5px 11px; font-size:12.5px; cursor:pointer;
          font:inherit; }
  .pill[aria-pressed="true"] { background:var(--accent); color:var(--accent-ink);
                                border-color:var(--accent); }
  .pill.rec:not([aria-pressed="true"]) { border-color:var(--olive); color:var(--olive); }
  .wheelcat { display:flex; align-items:center; gap:7px; margin:10px 0 6px; }
  .wheelcat .dot { width:12px; height:12px; margin:0; flex:none;
                    border:1px solid var(--border); }
  .wheelcat b { font-size:12.5px; }
  .tagrow { display:flex; flex-wrap:wrap; gap:6px; margin:2px 0 4px; min-height:0; }
  .tagrow:empty { display:none; }
  .tag { display:inline-flex; align-items:center; gap:5px; background:var(--page);
         border:1px solid var(--border); border-radius:999px; padding:3px 5px 3px 10px;
         font-size:12px; color:var(--ink-2); }
  .tag button { border:0; background:none; color:var(--muted); font:inherit;
                cursor:pointer; padding:0 4px; line-height:1; }
  .tag button:hover { color:var(--danger); }
  .rbwarn { margin:10px 0 2px; display:flex; flex-direction:column; gap:6px; }
  .rbwarn:empty { display:none; }
  .rbwarn-item { border-radius:8px; padding:8px 11px; font-size:12.5px; line-height:1.5;
                 border:1px solid; background:var(--page); }
  .rbwarn-item.danger { border-color:var(--danger); }
  .rbwarn-item.caution { border-color:var(--olive); }
  .rbwarn-item b { display:block; font-size:12.5px; margin-bottom:2px; }
  .rbwarn-item.danger b { color:var(--danger); }
  .rbwarn-item.caution b { color:var(--olive); }
  #mhint { font-size:12px; color:var(--muted); margin-top:10px; }
  /* admin */
  #vAdmin .card { padding-bottom:14px; }
  .arow { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:8px 0; }
  .arow label, .alabel { font-size:13px; min-width:84px; }
  .ahint { font-size:12px; color:var(--muted); }
  .ahint code { font-family:ui-monospace,monospace; font-size:11.5px; }
  .ainfo { font-size:12.5px; color:var(--ink-2); margin-top:8px; }
  .ainfo2 { font-size:12px; color:var(--muted); min-width:150px; }
  #vAdmin input[type=file] { font-size:12.5px; color:var(--ink-2); max-width:230px; }
  #vAdmin td .btn { padding:3px 10px; font-size:12.5px; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brandmark" id="brandmark" aria-hidden="true">__BRAND_INITIALS__</div>
    <div>
      <div class="wm" id="wmName">__BRAND_NAME__</div>
      <div class="wm2" id="wmTag">__BRAND_TAGLINE__</div>
    </div>
  </div>
  <span id="sub"></span>
</header>

<div class="filters">
  <div class="seg" id="nav" role="group" aria-label="View"></div>
  <div class="seg" id="ranges" role="group" aria-label="Time range">
    <button data-h="24">24h</button><button data-h="72">3d</button>
    <button data-h="168">7d</button><button data-h="720">30d</button>
    <button data-h="0" aria-pressed="true">Batch</button>
  </div>
  <button class="link" id="tbtn" hidden>Table view</button>
</div>

<div id="empty">
  <span id="emptymsg">No readings found yet — is tilt-logger running and the Tilt in range?</span>
</div>

<!-- overview -->
<div id="vAll" hidden>
  <div class="bcards" id="bcards"></div>
  <div class="card chart" id="allsgcard"><h2>Specific gravity</h2>
    <div class="legend" id="sgLegend"></div><div id="allsg"></div></div>
  <div class="card chart" id="allabvcard"><h2>Est. ABV</h2>
    <div class="legend" id="abvLegend"></div><div id="allabv"></div></div>
  <div class="card chart" id="alltcard"><h2>Temperature (&deg;F)</h2>
    <div class="legend" id="tLegend"></div><div id="allt"></div></div>
</div>

<!-- history -->
<div id="vHist" hidden>
  <div class="histbar" id="histbar"></div>
  <div class="bcards" id="histcards"></div>
</div>

<!-- admin -->
<div id="vAdmin" hidden>
  <div class="card"><h2>Branding</h2>
    <div class="arow">
      <label for="brandName">Name</label>
      <input type="text" id="brandName" maxlength="40" style="max-width:220px">
    </div>
    <div class="arow">
      <label for="brandTagline">Tagline</label>
      <input type="text" id="brandTagline" maxlength="80" style="max-width:340px">
    </div>
    <div class="arow">
      <button class="btn primary" id="brandSave">Save</button>
      <span class="ahint" id="brandHint">shown in the header, browser tab, and reports — applies immediately, no restart needed</span>
    </div>
  </div>

  <div class="card"><h2>Logging</h2>
    <div class="arow">
      <label for="logint">Logging interval</label>
      <select id="logint">
        <option value="0">Every beacon (~1–4 s)</option>
        <option value="60">Every 1 minute</option>
        <option value="300">Every 5 minutes</option>
        <option value="900">Every 15 minutes</option>
        <option value="3600">Every hour</option>
      </select>
      <span class="ahint">applies within ~5 seconds, no restart needed</span>
    </div>
    <div class="ainfo" id="loginfo"></div>
  </div>

  <div class="card"><h2>Tilt data</h2>
    <table><thead><tr><th>Tilt</th><th>Readings</th><th>First</th><th>Latest</th><th></th></tr></thead>
    <tbody id="adminTilts"></tbody></table>
    <div class="ahint" style="margin:8px 0 6px">Reset erases a Tilt's logged readings and its
      current batch so the next brew starts clean. Finished batch summaries stay in History
      — export any reports you want to keep first.</div>
  </div>

  <div class="card"><h2>Recipe wheels</h2>
    <div class="ahint" style="margin-bottom:6px">Flavor and aroma ingredients shown in the
      batch editor's Recipe builder. New ingredients always go into an existing category
      (so they can't end up in the wrong place) and take that category's color
      automatically — new categories warn if their color is too close to an existing one.</div>
    <div id="rwFlavor"></div>
    <div id="rwOdor"></div>
  </div>

  <div class="card"><h2>Fermentation stages</h2>
    <div class="ahint" style="margin-bottom:6px">The list a batch's "Mark stage…" button
      picks from (Secondary fermentation, Bulk aging, …) — kept short and curated on
      purpose, so marking a batch is always a pick from a known list, never free text.
      Renaming or removing one here doesn't change what's already recorded on a batch;
      it only affects new markers going forward.</div>
    <div id="stageList"></div>
    <div class="arow" style="margin-top:8px">
      <input type="text" id="stageNew" placeholder="New stage name, e.g. “Dry hopping”" style="flex:1">
      <button class="btn" id="stageAdd">Add stage</button>
    </div>
  </div>

  <div class="card"><h2>Software</h2>
    <div id="upwrap">
      <div class="arow"><b class="alabel">Dashboard</b>
        <span class="ainfo2" id="verDash"></span>
        <input type="file" id="upDash" accept=".py">
        <button class="btn" id="upDashGo">Install &amp; restart</button></div>
      <div class="arow"><b class="alabel">Logger</b>
        <span class="ainfo2" id="verLog"></span>
        <input type="file" id="upLog" accept=".py">
        <button class="btn" id="upLogGo">Install</button></div>
      <div class="ahint" id="upstatus" style="margin-top:8px"></div>
      <div class="ahint" style="margin-top:6px">Upload a new tilt_dashboard.py or tilt_logger.py.
        Files are syntax-checked before install and the old version is kept as a .bak beside it.
        The dashboard restarts itself; the logger restarts automatically when the
        tilt-logger-watch units are installed (see SETUP.md), otherwise run
        <code>sudo systemctl restart tilt-logger</code>.</div>
    </div>
    <div class="ahint" id="updisabled" hidden>Web updates are disabled. Start the dashboard
      with <code>--allow-updates</code> (the shipped tilt-dashboard.service includes it)
      to enable installing new versions from this page.</div>
  </div>
</div>

<!-- detail -->
<div id="vOne" hidden>
  <div class="binfo" id="binfo"></div>
  <div class="pickbanner" id="pickBanner" hidden>
    <span id="pickBannerText"></span>
    <button class="link" id="pickBannerCancel" type="button">Cancel</button>
  </div>
  <div class="tiles" id="tiles"></div>
  <div class="card chart" id="sgcard"><h2>Specific gravity</h2><div id="sgchart"></div></div>
  <div class="card chart" id="abvcard"><h2>Est. ABV</h2><div id="abvchart"></div></div>
  <div class="card chart" id="tcard"><h2>Temperature (&deg;F)</h2><div id="tchart"></div></div>
  <div class="card" id="tablewrap">
    <h2>Recent readings</h2>
    <table><thead><tr><th>Time</th><th>SG</th><th>Temp &deg;F</th><th>Temp &deg;C</th><th>RSSI dBm</th></tr></thead>
    <tbody id="tbody"></tbody></table>
  </div>
</div>

<footer id="footerTag">__BRAND_TAGLINE__ &nbsp;&middot;&nbsp;
  <a href="/guide" style="color:var(--muted)">User guide</a> &nbsp;&middot;&nbsp;
  <a href="/recipes" style="color:var(--muted)">Recipe explorer</a></footer>

<div class="tt" id="tt"></div>

<!-- batch editor -->
<div id="overlay">
  <div id="modal" role="dialog" aria-modal="true">
    <h3 id="mtitle">Batch details</h3>
    <div class="fld"><label for="f_name">Batch name</label>
      <input id="f_name" placeholder="e.g. Juicy Bits NEIPA #12"></div>
    <div class="frow">
      <div class="fld"><label for="f_style">Style</label><input id="f_style" placeholder="e.g. NEIPA"></div>
      <div class="fld"><label for="f_start">Brew date (Day 1)</label><input id="f_start" type="date"></div>
      <div class="fld"><label for="f_size">Batch size</label><input id="f_size" placeholder="e.g. 5.5 gal"></div>
      <div class="fld"><label for="f_yeast">Yeast</label><input id="f_yeast" placeholder="e.g. Imperial A38 Juice"></div>
      <div class="fld"><label for="f_ibu">IBU</label><input id="f_ibu" inputmode="decimal" placeholder="e.g. 45"></div>
      <div class="fld"><label for="f_og">Measured OG (overrides first reading)</label>
        <input id="f_og" inputmode="decimal" placeholder="e.g. 1.0620"></div>
      <div class="fld"><label for="f_fg">Target FG</label><input id="f_fg" inputmode="decimal" placeholder="e.g. 1.0120"></div>
      <div class="fld"><label for="f_abv">Target ABV (%, auto from OG &amp; Target FG)</label>
        <input id="f_abv" inputmode="decimal" placeholder="e.g. 6.5"></div>
      <div class="fld"><label for="f_temp">Target ferm temp (&deg;F)</label>
        <input id="f_temp" inputmode="decimal" placeholder="e.g. 68"></div>
    </div>
    <div class="fld"><label for="f_img">Brew image (shown on the batch tile)</label>
      <div class="imgrow">
        <img id="imgprev" alt="">
        <input id="f_img" type="file" accept="image/*">
        <button class="btn" id="imgclear" type="button" hidden>Remove</button>
      </div></div>
    <div class="fld"><label for="f_notes">Notes</label>
      <textarea id="f_notes" placeholder="Hop schedule, dry hop days, anything…"></textarea></div>
    <details class="calc" id="calc">
      <summary>Recipe builder <span class="rbhint">mead calculator &middot; yeast &middot; flavor &amp; aroma</span></summary>
      <div class="cbody">
        <div class="rbh">Target style</div>
        <div class="pillrow" id="abvTiers"></div>
        <div class="frow">
          <div class="fld"><label for="c_vol">Batch volume (US gal)</label>
            <input id="c_vol" inputmode="decimal" value="5"></div>
          <div class="fld"><label for="c_abv">Target ABV %</label>
            <input id="c_abv" inputmode="decimal" value="12"></div>
          <div class="fld"><label for="c_sweet">Sweetness</label>
            <select id="c_sweet"></select></div>
          <div class="fld"><label for="c_honey">Honey variety</label>
            <select id="c_honey">
              <option value="32">Dark / high-moisture (~32 PPG)</option>
              <option value="35" selected>Wildflower / clover (~35 PPG)</option>
              <option value="37">Light / dry (~37 PPG)</option>
            </select></div>
        </div>
        <div class="cout" id="cout"></div>
        <div class="rbwarn" id="rbWarn"></div>
        <div class="mbtns">
          <button class="btn" id="cApply" type="button">Apply OG / FG / size to batch</button>
        </div>
        <div class="chint">Based on the
          <a href="https://rawhoneyguide.com/tools/honey-mead-calculator" target="_blank" rel="noopener">rawhoneyguide.com honey mead calculator</a>:
          OG = FG + ABV&#47;131.25 &middot; honey lb = (OG&minus;1)&times;1000&times;gal&divide;PPG &middot;
          water assumes honey &asymp; 12 lb&#47;gal. Approximations &mdash; measure your actual OG on brew day.</div>

        <div class="rbh">Yeast strain <span class="rbhint">highlighted = handles your target ABV</span></div>
        <div class="pillrow" id="yeastPicks"></div>
        <div class="cout" id="yeastInfo" hidden></div>

        <div class="rbh">Flavor wheel <span class="rbhint">click ingredients to tag this batch</span></div>
        <div id="flavorWheel"></div>
        <div class="tagrow" id="flavorTags"></div>

        <div class="rbh">Aroma wheel</div>
        <div id="odorWheel"></div>
        <div class="tagrow" id="odorTags"></div>

        <div class="mbtns">
          <button class="btn" id="rbApply" type="button">Add picks to notes</button>
        </div>
        <div class="chint">Yeast, flavor and aroma reference data drawn from a mead
          recipe-builder guide covering ABV/sweetness tiers, five common mead yeasts,
          and flavor/aroma ingredient wheels.</div>
      </div>
    </details>
    <div class="mbtns">
      <button class="btn primary" id="mSave">Save</button>
      <button class="btn" id="mCancel">Cancel</button>
      <span class="spacer"></span>
      <button class="btn" id="mNew">Start new batch</button>
      <button class="btn danger" id="mFinish">Mark finished</button>
    </div>
    <div id="mhint">"Save" edits the current batch. "Start new batch" closes the
      current one and begins a fresh batch now — OG, ABV and charts reset from
      this moment. Data is stored in batches.json on the Pi.</div>
  </div>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const darkMq = matchMedia("(prefers-color-scheme: dark)");
const RECIPE_DATA = __RECIPE_DATA__;

/* Series colours keyed to the Tilt's physical colour, using validated steps
   from the dashboard palette (identity follows the entity: a Red Tilt draws
   a red line). Legend, direct end labels, tooltip, and the table are the
   secondary identity channels. */
const TILT_HUES = {
  Red:{l:"#e34948",d:"#e66767"}, Green:{l:"#008300",d:"#008300"},
  Black:{l:"#52514e",d:"#c3c2b7"}, Purple:{l:"#4a3aa7",d:"#9085e9"},
  Orange:{l:"#eb6834",d:"#d95926"}, Blue:{l:"#2a78d6",d:"#3987e5"},
  Yellow:{l:"#eda100",d:"#c98500"}, Pink:{l:"#e87ba4",d:"#d55181"},
};
const hue = c => (TILT_HUES[c] || {l:"#2a78d6",d:"#3987e5"})[darkMq.matches ? "d" : "l"];

let state = { view:"all", pastId:null, hours:0, data:null,
              histSelect:false, selIds:new Set() };
let imgState;   // undefined = keep, "" = remove, dataURL = new image
let battByColor = {};   // colour -> {weeks, ts}, refreshed each render() from D.battery
let navBattHosts = {};  // colour -> the <span class="battbadge"> element under its nav button

// Freshness scale for the battery-age icon: 0 weeks since change = full green,
// BATT_CAP_WEEKS+ = empty red. Not a real "charge level" — Tilt doesn't report
// one — this is literally weeks since the battery was last changed.
const BATT_CAP_WEEKS = 52;
function freshnessPct(weeks) {
  return Math.max(0, Math.min(100, 100 - (weeks / BATT_CAP_WEEKS) * 100));
}
/* Continuous red->green hue ramp (not segmented) for a freshness percent. */
function battColor(freshPct) {
  const p = Math.max(0, Math.min(100, freshPct));
  return `hsl(${(p * 1.2).toFixed(0)},72%,42%)`;
}
function batteryIconSVG(weeks) {
  const w0 = Math.max(0, Math.round(weeks));
  const label = w0 + "w";
  // ~25% larger than the original 30x14 icon.
  const w = 37.5, h = 17.5, nub = 3.1, bodyW = w - nub, pad = 2;
  const innerW = bodyW - pad * 2, innerH = h - pad * 2;
  const fresh = freshnessPct(w0);
  const fillW = Math.max(1.5, innerW * fresh / 100);
  const color = battColor(fresh);
  const fontSize = label.length > 3 ? 8 : 10;
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="${w0} week${w0 === 1 ? "" : "s"} since battery change">
    <rect x="0.5" y="0.5" width="${bodyW - 1}" height="${h - 1}" rx="2.5" fill="none" stroke="currentColor" stroke-opacity=".55"/>
    <rect x="${(bodyW + 0.5).toFixed(1)}" y="${(h / 2 - nub / 2 - 0.5).toFixed(1)}" width="${nub}" height="${nub + 3}" rx="1" fill="currentColor" fill-opacity=".55"/>
    <rect x="${pad}" y="${pad}" width="${innerW.toFixed(1)}" height="${innerH.toFixed(1)}" rx="1.2" fill="${color}" fill-opacity=".2"/>
    <rect x="${pad}" y="${pad}" width="${fillW.toFixed(1)}" height="${innerH.toFixed(1)}" rx="1.2" fill="${color}"/>
    <text x="${(bodyW / 2).toFixed(1)}" y="${h / 2}" text-anchor="middle" dominant-baseline="central"
          font-size="${fontSize}" font-weight="700" style="fill:#000">${label}</text>
  </svg>`;
}
function updateNavBattery() {
  const now = (state.data && state.data.server_time) || Date.now() / 1000;
  for (const [color, host] of Object.entries(navBattHosts)) {
    const info = battByColor[color];
    if (info && info.weeks != null) {
      host.innerHTML = batteryIconSVG(info.weeks);
      host.title = info.weeks + " week" + (info.weeks === 1 ? "" : "s") + " since this Tilt's battery was " +
                   "last changed (reported " + ago(info.ts, now) + ") — only some Tilt firmware reports this.";
      host.style.display = "block";
    } else {
      host.innerHTML = "";
      host.removeAttribute("title");
      host.style.display = "none";
    }
  }
}

function fmtSG(v, model) { return v == null ? "—" : v.toFixed(model === "pro" ? 4 : 3); }
function fmtTime(t, span) {
  const d = new Date(t * 1000);
  if (span > 3 * 86400)
    return d.toLocaleDateString([], { month:"short", day:"numeric" });
  return d.toLocaleString([], { month:"short", day:"numeric", hour:"numeric", minute:"2-digit" });
}
function fmtDate(t) {
  return new Date(t * 1000).toLocaleDateString([], { year:"numeric", month:"short", day:"numeric" });
}
function ago(t, now) {
  const s = Math.max(now - t, 0);
  if (s < 90) return Math.round(s) + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 172800) return (s / 3600).toFixed(1) + "h ago";
  return (s / 86400).toFixed(1) + "d ago";
}
function dayNo(startTs, now) { return Math.max(1, Math.floor((now - startTs) / 86400) + 1); }
// <input type=date> value <-> epoch seconds, anchored at local noon so the
// chosen calendar day never shifts across a timezone/DST boundary.
function isoDateLocal(ts) {
  const d = new Date(ts * 1000);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
         "-" + String(d.getDate()).padStart(2, "0");
}
function dateStrToTs(s) {
  if (!s) return null;
  const t = new Date(s + "T12:00:00");
  return isNaN(t) ? null : Math.floor(t.getTime() / 1000);
}
function niceStep(range, cands) {
  for (const c of cands) if (range / c <= 6) return c;
  return cands[cands.length - 1];
}
const SG_TICKS = [0.0005,0.001,0.002,0.005,0.01,0.02,0.05];
const T_TICKS  = [0.5,1,2,5,10,20];
const ABV_TICKS = [0.5,1,2,5,10];
// Est. ABV at a point in time: (batch OG - that reading's SG) * 131.25, the same
// standard approximation used for the live "Est. ABV" stat tile and the report.
const abvAt = (og, sg) => Math.max((og - sg) * 131.25, 0);

async function load(spin) {
  if (spin) for (const c of document.querySelectorAll(".card.chart")) c.classList.add("loading");
  try {
    let url;
    if (state.pastId) url = "/api/data?batch_id=" + encodeURIComponent(state.pastId);
    else if (state.view === "all") url = "/api/overview?hours=" + state.hours;
    else if (state.view === "history") url = "/api/history";
    else if (state.view === "admin") url = "/api/admin";
    else url = "/api/data?hours=" + state.hours + "&color=" + encodeURIComponent(state.view);
    state.data = await (await fetch(url)).json();
    render();
  } catch (e) { $("sub").textContent = "connection lost — retrying…"; }
  for (const c of document.querySelectorAll(".card.chart")) c.classList.remove("loading");
}

/* ---------- navigation ---------- */
function buildNav(colors, battery) {
  const nav = $("nav");
  const want = ["all", ...colors, "history"].join("|");
  if (nav.dataset.built !== want) {
    nav.dataset.built = want;
    nav.textContent = "";
    navBattHosts = {};
    const mk = (label, view, dotColor) => {
      const b = document.createElement("button");
      const row = document.createElement("span");
      row.className = "navrow";
      if (dotColor) { const d = document.createElement("span");
        d.className = "dot"; d.style.background = dotColor; row.append(d); }
      row.append(document.createTextNode(label));
      b.append(row);
      if (dotColor) {
        const batt = document.createElement("span");
        batt.className = "battbadge";
        b.append(batt);
        navBattHosts[view] = batt;
      }
      b.dataset.view = view;
      b.addEventListener("click", () => {
        state.view = view; state.pastId = null;
        state.histSelect = false; state.selIds.clear();
        cancelPickMode();
        syncNav(); load(true);
      });
      nav.append(b);
    };
    mk("All Tilts", "all", null);
    for (const c of colors) mk(c, c, hue(c));
    mk("History", "history", null);
    mk("Admin", "admin", null);
  }
  syncNav();
  battByColor = battery || {};
  updateNavBattery();
}
function syncNav() {
  for (const b of $("nav").children)
    b.setAttribute("aria-pressed", b.dataset.view === state.view);
  $("ranges").style.display =
    (state.view === "history" || state.view === "admin" || state.pastId) ? "none" : "";
}

/* ---------- rendering ---------- */
function el(parent, tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  parent.append(e);
  return e;
}

let knownColors = [];
function render() {
  const D = state.data;
  if (D.colors) knownColors = D.colors;
  buildNav(knownColors, D.battery);
  const isAdmin = state.view === "admin" && !state.pastId;
  const isHist = state.view === "history" && !state.pastId;
  const isDetail = state.pastId ||
    (state.view !== "all" && state.view !== "history" && state.view !== "admin");
  const histEmpty = isHist && !(D.batches && D.batches.length);
  const liveEmpty = !isHist && !isAdmin && !state.pastId && !knownColors.length;
  const noActiveEmpty = state.view === "all" && !state.pastId &&
    knownColors.length > 0 && !(D.tilts && D.tilts.length);
  const empty = histEmpty || liveEmpty || noActiveEmpty;

  $("empty").style.display = empty ? "block" : "none";
  $("emptymsg").textContent = histEmpty
    ? "No finished brews yet — use “Mark finished” when a batch is done and it will appear here."
    : noActiveEmpty
    ? "No active batches right now — open a Tilt's colour tab above and use “Add batch details” (or “Rebrew this batch” from History) to start one."
    : "No readings found yet — is tilt-logger running and the Tilt in range?";
  $("vAll").hidden = empty || state.view !== "all" || !!state.pastId;
  $("vHist").hidden = empty || !isHist;
  $("vAdmin").hidden = !isAdmin;
  $("vOne").hidden = empty || !isDetail;
  $("tbtn").hidden = !isDetail;
  charts = {};
  if (empty) { $("sub").textContent = ""; return; }
  if (isAdmin) renderAdmin(D);
  else if (isHist) renderHist(D);
  else if (state.view === "all" && !state.pastId) renderAll(D);
  else renderOne(D);
}

function batchTitle(b) { return (b && b.name) || "Unnamed batch"; }

function renderAll(D) {
  const now = D.server_time;
  const live = D.tilts.filter(t => t.stats);
  $("sub").textContent = D.tilts.length + " Tilt" + (D.tilts.length > 1 ? "s" : "") +
    " · updated " + (live.length ? ago(Math.max(...live.map(t => t.stats.last_seen)), now) : "—");

  const cards = $("bcards"); cards.textContent = "";
  for (const t of D.tilts) {
    const c = el(cards, "div", "bcard");
    c.addEventListener("click", () => {
      state.view = t.color; state.pastId = null; syncNav(); load(true);
    });
    if (t.batch && t.batch.image) {
      const im = el(c, "img", "bimg"); im.src = t.batch.image; im.alt = "";
    }
    const who = el(c, "div", "who");
    const d = el(who, "span", "dot"); d.style.background = hue(t.color);
    el(who, "span", null, t.color + " Tilt" + (t.model === "pro" ? " Pro" : ""));
    const cardBatt = battByColor[t.color];
    if (cardBatt && cardBatt.weeks != null) {
      const bb = el(who, "span", "cardbatt");
      bb.innerHTML = batteryIconSVG(cardBatt.weeks);
      bb.title = cardBatt.weeks + " week" + (cardBatt.weeks === 1 ? "" : "s") + " since this Tilt's " +
                 "battery was last changed (reported " + ago(cardBatt.ts, now) + ") — only some Tilt " +
                 "firmware reports this.";
    }
    el(c, "div", "bname", batchTitle(t.batch));
    const bits = [];
    if (t.batch && t.batch.style) bits.push(t.batch.style);
    const start = t.batch ? t.batch.start_ts : t.stats && t.stats.first_ts;
    if (start) bits.push("Day " + dayNo(start, now));
    el(c, "div", "bmeta", bits.join(" · ") || " ");
    const targetAbv = t.batch && t.batch.target_abv != null ? t.batch.target_abv : null;
    if (t.stats) {
      el(c, "div", "sgnow", fmtSG(t.stats.sg, t.model));
      const r = el(c, "div", "row2");
      const row2items = [["ABV", t.stats.abv.toFixed(1) + "%"]];
      if (targetAbv != null) row2items.push(["Target", Number(targetAbv).toFixed(1) + "%"]);
      row2items.push(["Temp", t.stats.temp_f.toFixed(1) + "°F"],
                     ["Atten", t.stats.attenuation.toFixed(0) + "%"]);
      for (const [k, v] of row2items) {
        const s = el(r, "span"); el(s, "b", null, v); s.append(" " + k);
      }
      el(c, "div", "bmeta", "last seen " + ago(t.stats.last_seen, now))
        .style.marginTop = "6px";
    } else {
      el(c, "div", "bmeta", targetAbv != null
        ? "no readings yet · target " + Number(targetAbv).toFixed(1) + "% ABV"
        : "no readings in this batch yet");
    }
  }

  const defs = key => D.tilts.filter(t => t.series.length).map(t => ({
    label: batchTitle(t.batch) === "Unnamed batch" ? t.color : batchTitle(t.batch),
    sub: t.color, color: hue(t.color), model: t.model,
    pts: t.series.map(p => ({ t: p.t, v: p[key] })),
  }));
  const sgDefs = defs("sg"), tDefs = defs("temp");
  const abvDefs = D.tilts.filter(t => t.series.length && t.stats).map(t => ({
    label: batchTitle(t.batch) === "Unnamed batch" ? t.color : batchTitle(t.batch),
    sub: t.color, color: hue(t.color), model: t.model,
    pts: t.series.map(p => ({ t: p.t, v: abvAt(t.stats.og, p.sg) })),
  }));
  legend("sgLegend", sgDefs); legend("abvLegend", abvDefs); legend("tLegend", tDefs);
  drawChart("allsg", sgDefs, v => v.toFixed(3), SG_TICKS, null, "SG");
  // no dashed target line here -- different Tilts can have different target ABVs,
  // so (as with gravity/temp above) a single reference line wouldn't mean much
  drawChart("allabv", abvDefs, v => v.toFixed(1), ABV_TICKS, null, "%");
  drawChart("allt", tDefs, v => v.toFixed(1), T_TICKS, null, "°F");
}

function legend(id, defs) {
  const L = $(id); L.textContent = "";
  if (defs.length < 2) return;   // single series: the card title names it
  for (const d of defs) {
    const s = el(L, "span");
    const k = el(s, "span", "key"); k.style.borderTopColor = d.color;
    s.append(d.label + (d.sub && d.sub !== d.label ? " (" + d.sub + ")" : ""));
  }
}

function renderHist(D) {
  const now = D.server_time;
  const nSel = state.selIds.size;
  $("sub").textContent = D.batches.length + " finished brew" +
    (D.batches.length === 1 ? "" : "s") +
    (state.histSelect ? " · " + nSel + " selected" : "");

  // toolbar: enter/leave selection mode, delete selected
  const bar = $("histbar"); bar.textContent = "";
  if (state.histSelect) {
    const del = el(bar, "button", "btn danger",
                   "Delete selected (" + nSel + ")");
    del.disabled = !nSel;
    del.addEventListener("click", deleteSelectedBrews);
    const cancel = el(bar, "button", "btn", "Cancel");
    cancel.addEventListener("click", () => {
      state.histSelect = false; state.selIds.clear(); render();
    });
    el(bar, "span", "ahint",
       "Click brews to select them, then delete. Deleting removes the summary "
       + "and image from History; logged readings stay until rotation or a Tilt reset.");
  } else if (D.batches.length) {
    const sel = el(bar, "button", "btn", "Select brews…");
    sel.addEventListener("click", () => { state.histSelect = true; render(); });
  }

  const cards = $("histcards"); cards.textContent = "";
  for (const b of D.batches) {
    const c = el(cards, "div", "bcard");
    if (state.histSelect) {
      if (state.selIds.has(b.id)) c.classList.add("sel");
      el(c, "div", "selmark", "✓");
      c.addEventListener("click", () => {
        if (state.selIds.has(b.id)) state.selIds.delete(b.id);
        else state.selIds.add(b.id);
        render();
      });
    } else {
      c.addEventListener("click", () => { state.pastId = b.id; cancelPickMode(); syncNav(); load(true); });
    }
    if (b.image) { const im = el(c, "img", "bimg"); im.src = b.image; im.alt = ""; }
    const who = el(c, "div", "who");
    const d = el(who, "span", "dot"); d.style.background = hue(b.color);
    who.append(b.color + " Tilt");
    el(c, "div", "bname", b.name);
    const days = ((b.end_ts - b.start_ts) / 86400).toFixed(1);
    el(c, "div", "bmeta", [b.style, fmtDate(b.start_ts) + " – " + fmtDate(b.end_ts),
                           days + " days"].filter(Boolean).join(" · "));
    if (b.stats) {
      const model = (b.stats.og && String(b.stats.og).length > 6) ? "pro" : "standard";
      el(c, "div", "sgnow", b.stats.abv.toFixed(1) + "% ABV");
      const r = el(c, "div", "row2");
      for (const [k, v] of [["OG", fmtSG(b.stats.og, model)],
                            ["FG", fmtSG(b.stats.sg, model)],
                            ["Atten", b.stats.attenuation.toFixed(0) + "%"]]) {
        const s = el(r, "span"); el(s, "b", null, v); s.append(" " + k);
      }
    } else {
      el(c, "div", "bmeta", "no stats recorded");
    }
  }
}

function renderOne(D) {
  const S = D.stats, b = D.batch, now = D.server_time;
  const finished = D.finished;
  $("sub").textContent = D.color + " Tilt (" + (D.model === "pro" ? "Pro" : "standard") + ")" +
    (S ? " · " + (D.n_total || 0).toLocaleString() + " readings this batch" +
         (finished ? "" : " · updated " + ago(S.last_seen, now)) : "");

  // batch info strip
  const bi = $("binfo"); bi.textContent = "";
  const main = el(bi, "div", "binfo-main");
  const left = el(main, "div", "bleft");
  if (b && b.image) { const im = el(left, "img", "bthumb"); im.src = b.image; im.alt = ""; }
  const lt = el(left, "div");
  const nameRow = el(lt, "div", "bname", batchTitle(b));
  if (finished) el(nameRow, "span", "badge", "Finished");
  const meta = [];
  if (b && b.style) meta.push(b.style);
  if (b && b.yeast) meta.push(b.yeast);
  if (b && b.ibu != null) meta.push(b.ibu + " IBU");
  if (b && b.batch_size) meta.push(b.batch_size);
  const start = b ? b.start_ts : S && S.first_ts;
  if (start) meta.push("brewed " + fmtDate(start) +
      (finished ? "" : " · Day " + dayNo(start, now)));
  if (b && b.end_ts) meta.push("finished " + fmtDate(b.end_ts));
  el(lt, "div", "bmeta", meta.join(" · ") || "No batch details yet — add them with Edit.");
  if (b && b.notes) el(lt, "div", "bnotes", b.notes);

  const btns = el(main, "div", "btns");
  const q = state.pastId ? "id=" + encodeURIComponent(state.pastId)
                         : "color=" + encodeURIComponent(D.color);
  const rep = el(btns, "a", "btn", "Export report");
  rep.href = "/report?" + q; rep.target = "_blank";
  const csv = el(btns, "a", "btn", "CSV");
  csv.href = "/api/export.csv?" + q;
  if (state.pastId) {
    const back = el(btns, "button", "btn", "Back to history");
    back.addEventListener("click", () => { state.pastId = null; state.view = "history"; cancelPickMode(); syncNav(); load(true); });
    if (b) {
      if (!b.trimmed) {
        const arch = el(btns, "a", "btn", "Archive batch");
        arch.href = "/api/archive?id=" + encodeURIComponent(state.pastId);
        const trim = el(btns, "button", "btn danger", "Trim archived data…");
        trim.addEventListener("click", () => trimBatch(b));
        const mstg = el(btns, "button", "btn", "Mark stage…");
        mstg.addEventListener("click", () => startMarkStage(b));
      }
      const rebrew = el(btns, "button", "btn primary", "Rebrew this batch");
      rebrew.addEventListener("click", () => rebrewToFreeTilt(b));
    }
  } else {
    const eb = el(btns, "button", "btn", b ? "Edit batch" : "Add batch details");
    eb.addEventListener("click", () => openModal());
    if (b) {
      // Primary start = Day 1, same thing Brew date in the editor already
      // sets -- this is just a quicker way to pick the exact moment off the
      // chart instead of typing a date, for when the Tilt started logging
      // earlier than you'd actually call "primary" (yeast lag, temp
      // stabilizing). Freely re-settable afterward, same as Brew date always
      // has been -- no data is deleted, everything before the new start just
      // stops counting toward this batch (as it already does today).
      const sps = el(btns, "button", "btn", "Set primary start…");
      sps.addEventListener("click", () => startSetPrimary(b));
      const mstg = el(btns, "button", "btn", "Mark stage…");
      mstg.addEventListener("click", () => startMarkStage(b));
    }
    if (!b) {
      const rh = el(btns, "button", "btn", "Rebrew from history…");
      rh.addEventListener("click", () => openRebrewHistoryPicker(D.color));
      const ra = el(btns, "button", "btn", "Rebrew from archive…");
      ra.addEventListener("click", () => openRebrewArchivePicker(D.color));
    }
    const rst = el(btns, "button", "btn danger", "Reset Tilt");
    rst.addEventListener("click", () => resetTilt(D.color));
  }
  if (b) renderNotesPanel(bi, b);

  // tiles
  const tiles = $("tiles"); tiles.textContent = "";
  const tile = (label, value, sub, subCls) => {
    const t = el(tiles, "div", "tile");
    el(t, "div", "lbl", label); el(t, "div", "val", value);
    if (sub) el(t, "div", "delta" + (subCls ? " " + subCls : ""), sub);
  };
  if (S) {
    const d24 = S.sg_24h_ago != null ? S.sg - S.sg_24h_ago : null;
    tile(finished ? "Final gravity" : "Current gravity", fmtSG(S.sg, D.model),
         !finished && d24 != null ? (d24 > 0 ? "+" : "") + fmtSG(d24, D.model) + " vs 24h ago" : null,
         d24 != null && d24 < 0 ? "down" : null);
    tile("Temperature", S.temp_f.toFixed(1) + " °F",
         S.temp_c.toFixed(1) + " °C" +
         (b && b.temp_target_f != null ? " · target " + b.temp_target_f + " °F" : ""));
    tile("Est. ABV", S.abv.toFixed(2) + "%",
         "from OG " + fmtSG(S.og, D.model) + (b && b.og_override ? " (measured)" : "") +
         (b && b.target_abv != null ? " · target " + Number(b.target_abv).toFixed(1) + "%" : ""));
    tile("Attenuation", S.attenuation.toFixed(1) + "%",
         b && b.target_fg != null ? "target FG " + fmtSG(b.target_fg, D.model) : "apparent");
    if (!finished) tile("Signal", (S.rssi ?? "—") + " dBm", "last seen " + ago(S.last_seen, now));
    if (S.battery_weeks != null)
      tile("Battery", S.battery_weeks + (S.battery_weeks === 1 ? " week" : " weeks") + " old",
           "as of " + ago(S.battery_ts, now));
  } else if (b) {
    tile("Waiting for readings", "—",
         b.target_abv != null
           ? "target " + Number(b.target_abv).toFixed(1) + "% ABV"
           : "new batch, no data yet");
  } else {
    tile("No active batch", "—",
         "use “Add batch details” above to start one, or check History for past brews");
  }

  const pts = key => D.series.map(p => ({ t: p.t, v: p[key] }));
  // Clicking a point on a batch's own charts pins a permanent note to that
  // reading's timestamp (see openAnnotateAdd); only wired when there's an
  // actual batch to attach the note to.
  const chartOpts = b ? { annotations: b.annotations || [],
                          stage_markers: b.stage_markers || [],
                          onPointClick: ts => handleChartPick(b.id, ts) } : undefined;
  drawChart("sgchart", [{ label:"gravity", color:hue(D.color), model:D.model, pts:pts("sg") }],
            v => fmtSG(v, D.model), SG_TICKS,
            b && b.target_fg != null ? { v:b.target_fg, label:"target FG" } : null, "SG", chartOpts);
  const abvPts = S ? D.series.map(p => ({ t: p.t, v: abvAt(S.og, p.sg) })) : [];
  drawChart("abvchart", [{ label:"ABV", color:hue(D.color), model:D.model, pts:abvPts }],
            v => v.toFixed(1), ABV_TICKS,
            b && b.target_abv != null ? { v:Number(b.target_abv), label:"target" } : null, "%", chartOpts);
  drawChart("tchart", [{ label:"temperature", color:hue(D.color), model:D.model, pts:pts("temp") }],
            v => v.toFixed(1), T_TICKS,
            b && b.temp_target_f != null ? { v:b.temp_target_f, label:"target" } : null, "°F", chartOpts);

  const tb = $("tbody"); tb.textContent = "";
  for (const r of (D.recent || [])) {
    const tr = document.createElement("tr");
    for (const txt of [
      new Date(r.t * 1000).toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit",second:"2-digit"}),
      fmtSG(r.sg, D.model), r.temp.toFixed(1),
      ((r.temp - 32) * 5 / 9).toFixed(1), r.rssi ?? "—"
    ]) { const td = document.createElement("td"); td.textContent = txt; tr.append(td); }
    tb.append(tr);
  }
}

async function deleteSelectedBrews() {
  const n = state.selIds.size;
  if (!n) return;
  if (!confirm("Permanently delete " + n + " finished brew" + (n === 1 ? "" : "s") +
      " from History?\n\nThe summary, stats and image are removed. Export any " +
      "reports you want to keep first — this cannot be undone."))
    return;
  try {
    const r = await fetch("/api/history/delete", { method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ ids:[...state.selIds] }) });
    if (!r.ok) throw new Error();
    state.histSelect = false; state.selIds.clear();
    load(true);
  } catch (e) { alert("Delete failed — is the dashboard server still running?"); }
}

/* ---------- reset ---------- */
async function resetTilt(color) {
  if (!confirm("Erase ALL logged readings for the " + color + " Tilt and delete its " +
      "current batch?\n\nFinished batch summaries stay in History, but their charts " +
      "and reports lose their data — export any reports you want to keep first.\n\n" +
      "This cannot be undone."))
    return;
  try {
    const r = await fetch("/api/reset", { method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ color }) });
    const j = await r.json();
    if (!r.ok) throw new Error();
    alert("Erased " + (j.readings_removed || 0).toLocaleString() + " readings for the "
          + color + " Tilt. New readings will start a fresh brew.");
    state.view = "all"; state.pastId = null; syncNav(); load(true);
  } catch (e) { alert("Reset failed — is the dashboard server still running?"); }
}

/* ---------- archive / trim / rebrew-from-history ---------- */
async function trimBatch(b) {
  const range = (b.start_ts ? fmtDate(b.start_ts) : "?") + " – " + (b.end_ts ? fmtDate(b.end_ts) : "?");
  if (!confirm("Permanently remove the raw logged readings for “" + batchTitle(b) + "” (" +
      b.color + " Tilt, " + range + ") from the server?\n\nThe History summary, stats and image " +
      "stay — only the detailed charts, table and CSV data for this batch go away. Download an " +
      "archive first if you want to keep the full data.\n\nThis cannot be undone."))
    return;
  try {
    const r = await fetch("/api/archive/trim", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: state.pastId }) });
    const j = await r.json();
    if (!r.ok) throw new Error();
    alert("Trimmed " + (j.readings_removed || 0).toLocaleString() + " readings from the server. " +
          "An archive file downloaded earlier still has the full data.");
    load(true);
  } catch (e) { alert("Trim failed — is the dashboard server still running?"); }
}

async function rebrewToFreeTilt(b) {
  let overview;
  try {
    overview = await (await fetch("/api/overview?hours=" + state.hours)).json();
  } catch (e) {
    alert("Could not check available Tilts — is the dashboard server still running?");
    return;
  }
  const busy = new Set((overview.tilts || []).map(t => t.color));
  const free = (overview.colors || []).filter(c => !busy.has(c)).sort();
  if (!free.length) {
    alert("Every known Tilt currently has a batch running — we can't start a new batch until " +
          "one is finished or reset.");
    return;
  }
  const color = free[0];
  state.pastId = null; state.view = color; syncNav(); load(true);
  openModal({ color, batch: b });
}

/* small ad-hoc overlay for the two rebrew pickers below — reuses the same
   look as the batch-editor modal (via CSS variables) without touching its
   markup or state. */
function closePicker() {
  const ov = document.getElementById("pickOverlay");
  if (ov) ov.remove();
}
function openPicker(title) {
  closePicker();
  const ov = document.createElement("div");
  ov.id = "pickOverlay";
  ov.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;" +
    "align-items:flex-start;justify-content:center;z-index:30;overflow:auto;padding:30px 12px;";
  ov.addEventListener("click", ev => { if (ev.target === ov) closePicker(); });
  const box = document.createElement("div");
  box.style.cssText = "background:var(--surface);border:1px solid var(--border);" +
    "border-radius:12px;padding:20px;width:min(480px,100%);";
  const h = el(box, "h3", null, title);
  h.style.cssText = "font-size:16px;margin-bottom:12px;";
  ov.append(box);
  document.body.append(ov);
  return box;
}

// a yes/no confirmation, built the same way as the rest of the app's modals
// (openPicker's overlay) instead of the browser's native confirm(). Native
// confirm()/alert()/prompt() dialogs can be silently suppressed by the
// browser after a person checks "Prevent this page from creating additional
// dialogs" -- which is easy to trigger by accident if a few dialogs show up
// in quick succession -- and once that happens, confirm() just returns
// false forever (no popup, no error) until the page is reloaded. Since
// "Set primary start..." and "Mark stage..." both write permanent-ish data,
// silently doing nothing is exactly the wrong failure mode, so they confirm
// this way instead.
function confirmModal(title, body, confirmLabel, onConfirm) {
  const box = openPicker(title);
  const p = el(box, "div", "bmeta", body);
  p.style.cssText = "white-space:pre-wrap;margin-bottom:4px;";
  const row = el(box, "div", "mbtns");
  el(row, "span", "spacer");
  const cancel = el(row, "button", "btn", "Cancel");
  cancel.addEventListener("click", closePicker);
  const ok = el(row, "button", "btn primary", confirmLabel || "Confirm");
  ok.addEventListener("click", () => { closePicker(); onConfirm(); });
}

// chart notes: a permanent, add-only log of observations pinned to a specific
// reading's timestamp. Rendered as a scrollable panel beside the batch info
// card, and as markers on the synced charts below (see drawChart's opts).
// Fermentation-stage markers (also permanent/add-only) get their own small
// section above the notes, since they're a different kind of thing -- a
// boundary in the timeline, not an observation.
function renderNotesPanel(parent, batch) {
  const panel = el(parent, "div", "binfo-notes");
  const stages = (batch.stage_markers || []).slice().sort((a, b2) => a.ts - b2.ts);
  if (stages.length) {
    el(panel, "div", "notes-head", "Fermentation stages");
    const sscroll = el(panel, "div", "notes-scroll");
    for (const m of stages) {
      const row = el(sscroll, "div", "note-row");
      const when = new Date(m.ts * 1000).toLocaleString([],
        { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      el(row, "div", "note-when", when);
      el(row, "div", "note-text", m.stage);
    }
  }
  const notesHead = el(panel, "div", "notes-head", "Chart notes");
  if (stages.length) notesHead.style.marginTop = "12px";
  const scroll = el(panel, "div", "notes-scroll");
  const anns = (batch.annotations || []).slice().sort((a, b2) => a.ts - b2.ts);
  if (!anns.length) {
    el(scroll, "div", "notes-empty", "Click a point on any chart below to pin a note to it.");
    return;
  }
  for (const a of anns) {
    const row = el(scroll, "div", "note-row");
    const when = new Date(a.ts * 1000).toLocaleString([],
      { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    el(row, "div", "note-when", when);
    el(row, "div", "note-text", a.text);
  }
}

// ---- pick mode: arms the charts so the NEXT click does something other
// than the default "add a note" -- setting primary start, or marking a
// fermentation stage. A plain click (nothing armed) always opens a note, so
// there's never ambiguity about what a bare click does. ----
let pickMode = null;   // null | {kind:"primary", batchId} | {kind:"stage", batchId, stage}

function armPickMode(mode, bannerText) {
  pickMode = mode;
  $("pickBannerText").textContent = bannerText;
  $("pickBanner").hidden = false;
}
function cancelPickMode() {
  pickMode = null;
  $("pickBanner").hidden = true;
}
$("pickBannerCancel").addEventListener("click", cancelPickMode);

function handleChartPick(batchId, ts) {
  if (pickMode && pickMode.batchId === batchId) {
    const mode = pickMode;
    cancelPickMode();
    if (mode.kind === "primary") return confirmSetPrimaryStart(batchId, ts);
    if (mode.kind === "stage") return confirmMarkStage(batchId, mode.stage, ts);
  }
  openAnnotateAdd(batchId, ts);
}

function startSetPrimary(b) {
  armPickMode({ kind: "primary", batchId: b.id },
    "Click a point on a chart below to set that moment as Primary fermentation's start (Day 1).");
}

function confirmSetPrimaryStart(batchId, ts) {
  const when = new Date(ts * 1000).toLocaleString([],
    { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
  confirmModal("Set Primary fermentation start (Day 1) to " + when + "?",
    "Readings from before this point will no longer count toward this batch's " +
    "stats or charts (nothing is deleted — you can change this again later, " +
    "the same as editing Brew date in Edit batch).",
    "Set start",
    () => {
      fetch("/api/batch", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "save", color: state.data.color,
                               batch: { start_ts: ts } }) })
        .then(r => { if (!r.ok) throw new Error(); load(true); })
        .catch(() => alert("Could not save — is the dashboard server still running?"));
    });
}

function startMarkStage(b) {
  if (!STAGES.length) {
    alert("No fermentation stages are defined yet — add one in Admin → Fermentation stages first.");
    return;
  }
  const box = openPicker("Mark a fermentation stage");
  el(box, "div", "bmeta", "Pick which stage, then click a point on a chart below.").style.marginBottom = "8px";
  for (const s of STAGES) {
    const row = el(box, "div"); row.style.cssText = "margin:4px 0;";
    const pick = el(row, "button", "btn", s.name);
    pick.type = "button"; pick.style.width = "100%"; pick.style.textAlign = "left";
    pick.addEventListener("click", () => {
      closePicker();
      armPickMode({ kind: "stage", batchId: b.id, stage: s.name },
        "Click a point on a chart below to mark the start of " + s.name + ".");
    });
  }
  const row = el(box, "div", "mbtns");
  el(row, "span", "spacer");
  const cancel = el(row, "button", "btn", "Cancel");
  cancel.addEventListener("click", closePicker);
}

function confirmMarkStage(batchId, stage, ts) {
  const when = new Date(ts * 1000).toLocaleString([],
    { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
  confirmModal("Mark " + stage + " as starting at " + when + "?",
    "This can't be changed or removed afterward.",
    "Mark stage",
    () => {
      fetch("/api/batch/stage", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: batchId, ts, stage }) })
        .then(r => { if (!r.ok) throw new Error(); load(true); })
        .catch(() => alert("Could not save — is the dashboard server still running?"));
    });
}

function openAnnotateAdd(batchId, ts) {
  const box = openPicker("Add a note");
  const when = new Date(ts * 1000).toLocaleString([],
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  el(box, "div", "bmeta", when);
  const ta = document.createElement("textarea");
  ta.placeholder = "What happened at this point? (dry hop, temp change, krausen drop, ...)";
  ta.style.cssText = "width:100%;min-height:80px;font:inherit;font-size:13.5px;color:var(--ink);" +
    "background:var(--page);border:1px solid var(--border);border-radius:8px;padding:7px 9px;" +
    "margin:8px 0;resize:vertical;box-sizing:border-box;";
  box.append(ta);
  const row = el(box, "div", "mbtns");
  el(row, "span", "spacer");
  const cancel = el(row, "button", "btn", "Cancel");
  cancel.addEventListener("click", closePicker);
  const save = el(row, "button", "btn primary", "Save note");
  save.addEventListener("click", async () => {
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }
    save.disabled = true;
    try {
      const r = await fetch("/api/batch/annotate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: batchId, ts, text }),
      });
      if (!r.ok) throw new Error();
      closePicker();
      load(true);
    } catch (e) {
      save.disabled = false;
      alert("Could not save the note — is the dashboard server still running?");
    }
  });
  ta.focus();
}

function openAnnotateView(ts, text) {
  const box = openPicker("Note");
  const when = new Date(ts * 1000).toLocaleString([],
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  el(box, "div", "bmeta", when).style.marginBottom = "6px";
  const p = el(box, "div");
  p.style.cssText = "white-space:pre-wrap;font-size:13.5px;";
  p.textContent = text;
  const row = el(box, "div", "mbtns");
  el(row, "span", "spacer");
  const close = el(row, "button", "btn primary", "Close");
  close.addEventListener("click", closePicker);
}

async function openRebrewHistoryPicker(destColor) {
  let hist;
  try { hist = await (await fetch("/api/history")).json(); }
  catch (e) { alert("Could not load History — is the dashboard server still running?"); return; }
  const batches = (hist.batches || []).slice().sort((a, b) => b.end_ts - a.end_ts);
  if (!batches.length) {
    alert("No finished brews in History yet to rebrew from.");
    return;
  }
  const box = openPicker("Rebrew " + destColor + " Tilt from history");
  el(box, "div", "bmeta", "Pick a past brew to use as a starting point:");
  const sel = el(box, "select");
  sel.style.cssText = "width:100%;font:inherit;font-size:13.5px;color:var(--ink);" +
    "background:var(--page);border:1px solid var(--border);border-radius:8px;" +
    "padding:7px 9px;margin:8px 0 4px;";
  for (const b of batches) {
    const o = document.createElement("option");
    o.value = b.id;
    o.textContent = b.color + " · " + (b.name || "Unnamed batch") +
      (b.style ? " (" + b.style + ")" : "") + " · " + fmtDate(b.end_ts) +
      (b.trimmed ? " · trimmed" : "");
    sel.append(o);
  }
  const row = el(box, "div", "mbtns");
  el(row, "span", "spacer");
  const cancel = el(row, "button", "btn", "Cancel");
  cancel.addEventListener("click", closePicker);
  const go = el(row, "button", "btn primary", "Use this batch");
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const full = await (await fetch("/api/data?batch_id=" + encodeURIComponent(sel.value))).json();
      closePicker();
      openModal({ color: destColor, batch: full.batch || {} });
    } catch (e) {
      go.disabled = false;
      alert("Could not load that batch's details — is the dashboard server still running?");
    }
  });
}

function openRebrewArchivePicker(destColor) {
  const inp = document.createElement("input");
  inp.type = "file"; inp.accept = ".html,text/html";
  inp.style.display = "none";
  document.body.append(inp);
  const cleanup = () => inp.remove();
  inp.addEventListener("change", () => {
    const f = inp.files[0];
    if (!f) { cleanup(); return; }
    const rd = new FileReader();
    rd.onload = () => {
      cleanup();
      let payload;
      try {
        const doc = new DOMParser().parseFromString(String(rd.result), "text/html");
        const tag = doc.getElementById("tilt-dashboard-archive");
        if (!tag) throw new Error("no archive payload");
        payload = JSON.parse(tag.textContent);
        if (!payload || !payload.tilt_archive || !payload.batch)
          throw new Error("bad archive payload");
      } catch (e) {
        alert("That doesn't look like a dashboard archive file — use the one downloaded " +
              "with the “Archive batch” button in History.");
        return;
      }
      openModal({ color: destColor, batch: payload.batch });
    };
    rd.onerror = () => { cleanup(); alert("Could not read that file."); };
    rd.readAsText(f);
  });
  inp.click();
}

/* ---------- admin page ---------- */
function renderAdmin(D) {
  $("sub").textContent = "Admin · dashboard and logger settings";
  if (document.activeElement !== $("brandName")) $("brandName").value = D.brand.name;
  if (document.activeElement !== $("brandTagline")) $("brandTagline").value = D.brand.tagline;
  const sel = $("logint");
  const v = String(Math.round(D.interval || 0));
  if ([...sel.options].some(o => o.value === v)) sel.value = v;
  else { const o = document.createElement("option");
    o.value = v; o.textContent = "Every " + v + " s"; sel.append(o); sel.value = v; }
  const totalN = D.tilts.reduce((a, t) => a + t.n, 0);
  $("loginfo").textContent = "Log file: " + D.log.path + " · " +
    (D.log.bytes / 1048576).toFixed(1) + " MB · " + totalN.toLocaleString() +
    " readings · batches file: " + D.batches_path;

  const tb = $("adminTilts"); tb.textContent = "";
  for (const t of D.tilts) {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    const d = document.createElement("span"); d.className = "dot";
    d.style.background = hue(t.color);
    td1.append(d, t.color);
    tr.append(td1);
    for (const txt of [t.n.toLocaleString(),
                       fmtDate(t.first_ts), fmtDate(t.last_ts)]) {
      const td = document.createElement("td"); td.textContent = txt; tr.append(td);
    }
    const ta = document.createElement("td");
    const csv = document.createElement("a"); csv.className = "btn";
    csv.textContent = "CSV";
    csv.href = "/api/export.csv?color=" + encodeURIComponent(t.color);
    const rst = document.createElement("button"); rst.className = "btn danger";
    rst.textContent = "Reset"; rst.style.marginLeft = "6px";
    rst.addEventListener("click", () => resetTilt(t.color));
    ta.append(csv, rst); tr.append(ta);
    tb.append(tr);
  }
  if (!D.tilts.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td"); td.colSpan = 5;
    td.textContent = "no readings logged yet"; td.style.color = "var(--muted)";
    tr.append(td); tb.append(tr);
  }

  $("upwrap").hidden = !D.allow_updates;
  $("updisabled").hidden = D.allow_updates;
  const vt = ts => ts ? "installed " + new Date(ts * 1000).toLocaleString([],
    { month:"short", day:"numeric", hour:"numeric", minute:"2-digit" }) : "not found";
  $("verDash").textContent = vt(D.versions.dashboard);
  $("verLog").textContent = vt(D.versions.logger);
  loadWheels().then(renderWheelsAdmin);
  loadStages().then(renderStagesAdmin);
}

/* ---------- recipe wheel admin (add/edit/delete flavor & odor ingredients) ---------- */
function hexToRgb(h) {
  const m = /^#([0-9a-f]{6})$/i.exec(h || ""); if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function colorDistance(a, b) {
  const pa = hexToRgb(a), pb = hexToRgb(b);
  if (!pa || !pb) return 999;
  return Math.sqrt((pa[0]-pb[0])**2 + (pa[1]-pb[1])**2 + (pa[2]-pb[2])**2);
}
const SWATCH_PALETTE = ["#5b8ec4","#c46b5b","#8a9e4f","#a06bc4","#4fa3a0","#c49a4f","#7f7fbf","#c45b8e"];
function nextSwatchColor(wheel) {
  const used = new Set(wheel.categories.map(c => c.hex.toLowerCase()));
  return SWATCH_PALETTE.find(h => !used.has(h)) || SWATCH_PALETTE[wheel.categories.length % SWATCH_PALETTE.length];
}
async function recipeAction(body) {
  try {
    const r = await fetch("/api/recipe", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) { alert(j.error || "Request failed"); return null; }
    applyWheels(j);
    renderWheelsAdmin();
    return j;
  } catch (e) { alert("Could not reach the dashboard server."); return null; }
}
function editCategory(wheelKey, cat) {
  const wheel = wheelKey === "flavor" ? WHEELS.flavorWheel : WHEELS.odorWheel;
  const name = prompt("Category name:", cat.name);
  if (name === null || !name.trim()) return;
  const hex = prompt("Category color (hex, e.g. #5b8ec4):", cat.hex);
  if (hex === null) return;
  const v = hex.trim();
  if (!/^#[0-9a-fA-F]{6}$/.test(v)) { alert("Color must look like #RRGGBB."); return; }
  const clash = wheel.categories.find(c => c.id !== cat.id && colorDistance(c.hex, v) < 45);
  if (clash && !confirm('That color is close to "' + clash.name + '"\'s — use it anyway?')) return;
  recipeAction({ action: "edit_category", wheel: wheelKey, id: cat.id, name: name.trim(), hex: v });
}
function renderWheelBlock(hostId, wheel, wheelKey) {
  const host = $(hostId); host.textContent = "";
  const h = document.createElement("h3"); h.textContent = wheelKey === "flavor" ? "Flavor wheel" : "Aroma wheel";
  h.style.cssText = "font-size:13px;margin:14px 0 6px";
  host.append(h);

  for (const cat of wheel.categories) {
    const row = document.createElement("div"); row.className = "wheelcat";
    const dot = document.createElement("span"); dot.className = "dot"; dot.style.background = cat.hex;
    const b = document.createElement("b"); b.textContent = cat.name;
    const ren = document.createElement("button"); ren.className = "link"; ren.textContent = "rename/recolor";
    ren.type = "button"; ren.addEventListener("click", () => editCategory(wheelKey, cat));
    const del = document.createElement("button"); del.className = "link"; del.textContent = "delete category";
    del.type = "button"; del.style.color = "var(--danger)"; del.style.marginLeft = "auto";
    del.addEventListener("click", () => {
      if (confirm('Delete category "' + cat.name + '" and all ' + cat.ingredients.length +
                  ' of its ingredients? This cannot be undone.'))
        recipeAction({ action: "delete_category", wheel: wheelKey, id: cat.id });
    });
    row.append(dot, b, ren, del);
    host.append(row);

    const tags = document.createElement("div"); tags.className = "tagrow";
    for (const ing of cat.ingredients) {
      const t = document.createElement("span"); t.className = "tag"; t.title = ing.role;
      t.append(document.createTextNode(ing.name));
      const x = document.createElement("button"); x.type = "button"; x.textContent = "×";
      x.setAttribute("aria-label", "Remove");
      x.addEventListener("click", () => {
        if (confirm('Remove "' + ing.name + '" from ' + cat.name + '?'))
          recipeAction({ action: "delete_ingredient", wheel: wheelKey, category_id: cat.id, id: ing.id });
      });
      t.append(x); tags.append(t);
    }
    host.append(tags);

    const f = document.createElement("div"); f.className = "arow";
    const ni = document.createElement("input"); ni.placeholder = "New ingredient name";
    ni.style.maxWidth = "180px";
    const ri = document.createElement("input"); ri.placeholder = "What it contributes (shown on hover)";
    ri.style.flex = "1"; ri.style.minWidth = "220px";
    const ab = document.createElement("button"); ab.className = "btn"; ab.type = "button";
    ab.textContent = "+ Add to " + cat.name;
    ab.addEventListener("click", async () => {
      if (!ni.value.trim() || !ri.value.trim()) { alert("Enter a name and what it contributes."); return; }
      const j = await recipeAction({ action: "add_ingredient", wheel: wheelKey, category_id: cat.id,
                                     name: ni.value.trim(), role: ri.value.trim() });
      if (j) { ni.value = ""; ri.value = ""; }
    });
    f.append(ni, ri, ab); host.append(f);
  }

  const nf = document.createElement("div"); nf.className = "arow"; nf.style.marginTop = "10px";
  const cn = document.createElement("input"); cn.placeholder = "New category name"; cn.style.maxWidth = "180px";
  const cc = document.createElement("input"); cc.type = "color"; cc.value = nextSwatchColor(wheel);
  const cprev = document.createElement("span"); cprev.className = "dot";
  const chint = document.createElement("span"); chint.className = "ahint";
  const sync = () => {
    cprev.style.background = cc.value;
    const clash = wheel.categories.find(c => colorDistance(c.hex, cc.value) < 45);
    chint.textContent = clash ? "close to “" + clash.name + "”'s color — pick something more distinct" : "";
    chint.style.color = clash ? "var(--danger)" : "var(--muted)";
  };
  cc.addEventListener("input", sync); sync();
  const cb = document.createElement("button"); cb.className = "btn"; cb.type = "button"; cb.textContent = "+ New category";
  cb.addEventListener("click", async () => {
    if (!cn.value.trim()) { alert("Enter a category name."); return; }
    const j = await recipeAction({ action: "add_category", wheel: wheelKey, name: cn.value.trim(), hex: cc.value });
    if (j) cn.value = "";
  });
  nf.append(cn, cprev, cc, cb, chint);
  host.append(nf);

  const rb = document.createElement("button"); rb.className = "btn"; rb.type = "button";
  rb.style.marginTop = "8px";
  rb.textContent = "Reset " + (wheelKey === "flavor" ? "flavor" : "aroma") + " wheel to built-in";
  rb.addEventListener("click", () => {
    if (confirm("Discard all edits to the " + (wheelKey === "flavor" ? "flavor" : "aroma") +
                " wheel and restore the built-in ingredients? This cannot be undone."))
      recipeAction({ action: "reset", wheel: wheelKey });
  });
  host.append(rb);
}
function renderWheelsAdmin() {
  if (!$("rwFlavor")) return;   // admin view not open
  renderWheelBlock("rwFlavor", WHEELS.flavorWheel, "flavor");
  renderWheelBlock("rwOdor", WHEELS.odorWheel, "odor");
}

$("logint").addEventListener("change", async ev => {
  try {
    const r = await fetch("/api/settings", { method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ interval: parseFloat(ev.target.value) }) });
    if (!r.ok) throw new Error();
  } catch (e) { alert("Could not save the logging interval."); load(false); }
});

function brandInitials(name) {
  const words = (name || "").trim().split(/\s+/).filter(Boolean);
  return ((words[0] || "T")[0] + (words[1] ? words[1][0] : "")).toUpperCase();
}

$("brandSave").addEventListener("click", async () => {
  const name = $("brandName").value, tagline = $("brandTagline").value;
  try {
    const r = await fetch("/api/brand", { method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ name, tagline }) });
    if (!r.ok) throw new Error();
    const saved = await r.json();
    document.title = saved.name + " — Fermentation";
    $("wmName").textContent = saved.name;
    $("wmTag").textContent = saved.tagline;
    $("brandmark").textContent = brandInitials(saved.name);
    if ($("footerTag")) $("footerTag").firstChild.textContent = saved.tagline + " ";
    $("brandHint").textContent = "Saved.";
  } catch (e) { alert("Could not save branding."); }
});

/* ---------- software updates (admin) ---------- */
async function installUpdate(target, input, label) {
  const st = $("upstatus");
  const f = input.files[0];
  if (!f) { st.textContent = "Choose a " + label + " .py file first."; return; }
  st.textContent = "Checking and installing " + f.name + "…";
  try {
    const source = await f.text();
    const r = await fetch("/api/update", { method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ target, source }) });
    const j = await r.json();
    if (!r.ok) { st.textContent = "Install failed: " + (j.error || r.status); return; }
    if (j.restarting) {
      st.textContent = "Installed — dashboard is restarting, page will reload…";
      const t0 = Date.now();
      const poll = async () => {
        try { const p = await fetch("/api/settings", { cache:"no-store" });
          if (p.ok) { location.reload(); return; } } catch (e) {}
        if (Date.now() - t0 < 30000) setTimeout(poll, 1000);
        else st.textContent = "Dashboard did not come back — check " +
          "journalctl -u tilt-dashboard on the Pi (a .bak of the old version " +
          "sits beside the script).";
      };
      setTimeout(poll, 2500);
    } else {
      st.textContent = "Installed " + f.name + ". " + (j.note || "");
      input.value = "";
      load(false);
    }
  } catch (e) { st.textContent = "Install failed — connection error."; }
}
$("upDashGo").addEventListener("click", () => installUpdate("dashboard", $("upDash"), "dashboard"));
$("upLogGo").addEventListener("click", () => installUpdate("logger", $("upLog"), "logger"));

/* ---------- chart engine (shared, 1..n series, synced crosshair) ---------- */
let charts = {};   // hostId -> chart object
function drawChart(id, seriesList, fmt, tickCands, target, unit, opts) {
  const host = $(id); host.textContent = "";
  const W = host.clientWidth || 960, H = 220, m = { l:56, r:14, t:10, b:22 };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("height", H);
  host.append(svg);
  const sv = (n, at) => { const e = document.createElementNS(svg.namespaceURI, n);
    for (const k in at) e.setAttribute(k, at[k]); svg.append(e); return e; };

  const all = seriesList.flatMap(s => s.pts);
  if (!all.length) { const t = sv("text", { x:W/2, y:H/2, "text-anchor":"middle" });
    t.textContent = "no data in range"; return; }

  const t0 = Math.min(...all.map(p => p.t)), t1 = Math.max(...all.map(p => p.t)) || t0 + 1;
  let lo = Math.min(...all.map(p => p.v)), hi = Math.max(...all.map(p => p.v));
  if (target) { lo = Math.min(lo, target.v); hi = Math.max(hi, target.v); }
  if (hi - lo < 1e-9) { lo -= 0.5; hi += 0.5; }
  const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
  const step = niceStep(hi - lo, tickCands);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;

  const X = t => m.l + (t - t0) / Math.max(t1 - t0, 1) * (W - m.l - m.r);
  const Y = v => m.t + (hi - v) / (hi - lo) * (H - m.t - m.b);

  for (let v = lo; v <= hi + 1e-9; v += step) {
    sv("line", { x1:m.l, x2:W-m.r, y1:Y(v), y2:Y(v), stroke:css("--grid"), "stroke-width":1 });
    const t = sv("text", { x:m.l-8, y:Y(v)+4, "text-anchor":"end" }); t.textContent = fmt(v);
  }
  const span = t1 - t0, nT = Math.min(6, Math.max(2, Math.floor(W / 160)));
  let prevLbl = null;
  for (let i = 0; i <= nT; i++) {
    const tt = t0 + span * i / nT, lbl = fmtTime(tt, span);
    if (lbl === prevLbl) continue;
    prevLbl = lbl;
    const t = sv("text", { x:X(tt), y:H-6,
      "text-anchor": i === 0 ? "start" : i === nT ? "end" : "middle" });
    t.textContent = lbl;
  }
  sv("line", { x1:m.l, x2:W-m.r, y1:H-m.b, y2:H-m.b, stroke:css("--axis"), "stroke-width":1 });

  if (target) {  // reference line (muted, dashed — distinct from solid gridlines)
    sv("line", { x1:m.l, x2:W-m.r, y1:Y(target.v), y2:Y(target.v),
                 stroke:css("--muted"), "stroke-width":1, "stroke-dasharray":"5 4" });
    // label at the LEFT end so it never collides with series end labels
    const t = sv("text", { x:m.l+6, y:Y(target.v)-4,
      style:"paint-order:stroke", stroke:css("--surface"), "stroke-width":3 });
    t.textContent = target.label + " " + fmt(target.v);
  }

  const endLbls = [];
  for (const s of seriesList) {
    const pts = s.pts.map(p => [X(p.t), Y(p.v)]);
    const line = pts.map((p,i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
    if (seriesList.length === 1)  // area wash only for a single series
      sv("path", { d: line + `L${pts[pts.length-1][0].toFixed(1)} ${H-m.b}L${pts[0][0].toFixed(1)} ${H-m.b}Z`,
                   fill:s.color, opacity:0.1 });
    sv("path", { d:line, fill:"none", stroke:s.color, "stroke-width":2,
                 "stroke-linejoin":"round", "stroke-linecap":"round" });
    const last = pts[pts.length - 1];
    sv("circle", { cx:last[0], cy:last[1], r:6, fill:css("--surface") });
    sv("circle", { cx:last[0], cy:last[1], r:4, fill:s.color });
    endLbls.push({ x:last[0], y:last[1], v:s.pts[s.pts.length-1].v });
  }
  // direct end labels; nudge apart if they collide (keep near their line)
  endLbls.sort((a,b) => a.y - b.y);
  let lastY = -99;
  for (const L of endLbls) {
    let y = Math.max(L.y - 10, 12);
    if (y - lastY < 13) y = lastY + 13;
    lastY = y;
    // surface halo (paint-order) keeps the label legible over other lines
    const t = sv("text", { x:L.x-10, y, "text-anchor":"end", "font-weight":600,
      fill:css("--ink"), style:"paint-order:stroke", stroke:css("--surface"),
      "stroke-width":3 });
    t.textContent = fmt(L.v);
  }

  // chart notes: a diamond marker at each annotation's (nearest) plotted point,
  // clickable to view; drawn after the lines so markers sit on top.
  const markerPositions = [];
  if (opts && opts.annotations && opts.annotations.length) {
    const span2 = Math.max(t1 - t0, 1);
    for (const a of opts.annotations) {
      let best = null, bd = Infinity;
      for (const s of seriesList) for (const p of s.pts) {
        const d = Math.abs(p.t - a.ts);
        if (d < bd) { bd = d; best = p; }
      }
      if (!best) continue;
      const tol = Math.max(span2 / 40, 3600);
      if (bd > tol) continue;  // data for this moment isn't plotted here anymore
      const x = X(best.t), y = Y(best.v);
      const mk = sv("path", {
        d: `M${x} ${y-7}L${x+7} ${y}L${x} ${y+7}L${x-7} ${y}Z`,
        fill: css("--surface"), stroke: css("--ink"), "stroke-width": 2,
        style: "cursor:pointer",
      });
      const title = document.createElementNS(svg.namespaceURI, "title");
      title.textContent = new Date(a.ts * 1000).toLocaleString([],
        { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + " — " + a.text;
      mk.append(title);
      markerPositions.push({ x, y, ts: a.ts, text: a.text });
    }
  }

  // fermentation-stage markers: a labeled vertical dashed line at the
  // marker's own timestamp, drawn full-height across the plot -- this marks
  // "time crossed this line" rather than one reading's value, so it reads
  // differently from both the horizontal target lines and the note diamonds
  // above. Not clickable (stage markers are view-only once set). Skipped if
  // the marker falls outside the chart's current time range (e.g. a short
  // 24h view looking past a stage marked days ago).
  if (opts && opts.stage_markers && opts.stage_markers.length) {
    for (const sm of opts.stage_markers) {
      if (sm.ts < t0 || sm.ts > t1) continue;
      const x = X(sm.ts);
      sv("line", { x1:x, x2:x, y1:m.t, y2:H-m.b, stroke:css("--olive"),
                   "stroke-width":1.5, "stroke-dasharray":"2 3" });
      const lbl = sv("text", { x:x+4, y:m.t+12, "font-size":11, "font-weight":600,
        fill:css("--olive"), style:"paint-order:stroke", stroke:css("--surface"),
        "stroke-width":3 });
      lbl.textContent = sm.stage;
    }
  }

  const cross = sv("line", { x1:0, x2:0, y1:m.t, y2:H-m.b, stroke:css("--axis"),
    "stroke-width":1, visibility:"hidden" });
  const dots = seriesList.map(s => sv("circle", { r:5, fill:s.color,
    stroke:css("--surface"), "stroke-width":2, visibility:"hidden" }));

  charts[id] = { svg, cross, dots, X, Y, W, seriesList, fmt, unit };
  svg.addEventListener("pointermove", ev => hover(ev, id));
  svg.addEventListener("pointerleave", () => hover(null));

  // click-to-add-a-note: only wired for charts that pass onPointClick (the
  // per-Tilt detail charts when a batch exists) — the All-Tilts overview
  // charts never get this, since a note belongs to one specific batch.
  if (opts && opts.onPointClick) {
    svg.style.cursor = "crosshair";
    svg.addEventListener("click", ev => {
      const r = svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * (W / r.width);
      const py = (ev.clientY - r.top) * (H / r.height);
      const hitMarker = markerPositions.find(mp => Math.hypot(mp.x - px, mp.y - py) < 10);
      if (hitMarker) { openAnnotateView(hitMarker.ts, hitMarker.text); return; }
      let snapT = null, bd = Infinity;
      for (const s of seriesList) for (const p of s.pts) {
        const d = Math.abs(X(p.t) - px);
        if (d < bd) { bd = d; snapT = p.t; }
      }
      if (snapT != null) opts.onPointClick(snapT);
    });
  }
}

function hover(ev, srcId) {
  const tt = $("tt");
  if (!ev) {
    tt.style.display = "none";
    for (const c of Object.values(charts)) {
      c.cross.setAttribute("visibility","hidden");
      for (const d of c.dots) d.setAttribute("visibility","hidden");
    }
    return;
  }
  const c0 = charts[srcId];
  if (!c0) return;
  const r = c0.svg.getBoundingClientRect();
  const px = (ev.clientX - r.left) * (c0.W / r.width);
  // snap to the nearest data time in the hovered chart
  let snapT = null, bd = Infinity;
  for (const s of c0.seriesList) for (const p of s.pts) {
    const d = Math.abs(c0.X(p.t) - px);
    if (d < bd) { bd = d; snapT = p.t; }
  }
  if (snapT == null) return;

  tt.textContent = "";
  const when = document.createElement("div"); when.className = "when";
  when.textContent = new Date(snapT * 1000).toLocaleString([],
    { month:"short", day:"numeric", hour:"numeric", minute:"2-digit" });
  tt.append(when);

  for (const [id, c] of Object.entries(charts)) {
    const x = c.X(snapT);
    c.cross.setAttribute("x1", x); c.cross.setAttribute("x2", x);
    c.cross.setAttribute("visibility", "visible");
    c.seriesList.forEach((s, i) => {
      let best = null, d0 = Infinity;
      for (const p of s.pts) { const d = Math.abs(p.t - snapT);
        if (d < d0) { d0 = d; best = p; } }
      const dot = c.dots[i];
      // hide series with no data near this time (e.g. a batch not started yet)
      const span = s.pts.length > 1 ? s.pts[s.pts.length-1].t - s.pts[0].t : 0;
      const tol = Math.max(span / 40, 3600);
      if (!best || d0 > tol) { dot.setAttribute("visibility","hidden"); return; }
      dot.setAttribute("cx", c.X(best.t)); dot.setAttribute("cy", c.Y(best.v));
      dot.setAttribute("visibility", "visible");
      // tooltip row (every series of every chart in the view)
      const row = document.createElement("div"); row.className = "row";
      const k = document.createElement("span"); k.className = "key";
      k.style.borderTopColor = s.color;
      const v = document.createElement("span"); v.className = "v";
      v.textContent = c.fmt(best.v) + (c.unit === "°F" ? " °F" : c.unit === "%" ? "%" : "");
      const n = document.createElement("span"); n.className = "n"; n.textContent = s.label;
      row.append(k, v, n); tt.append(row);
    });
  }
  tt.style.display = "block";
  const tw = tt.offsetWidth, th = tt.offsetHeight;
  tt.style.left = Math.min(ev.clientX + 14, innerWidth - tw - 8) + "px";
  tt.style.top = Math.min(ev.clientY + 14, innerHeight - th - 8) + "px";
}

/* ---------- batch editor ---------- */
function showImgPreview(src) {
  const p = $("imgprev");
  if (src) { p.src = src; p.style.display = "block"; $("imgclear").hidden = false; }
  else { p.removeAttribute("src"); p.style.display = "none"; $("imgclear").hidden = true; }
}
let rebrewFrom = null;   // {color, batch} when the editor is seeded from a past brew
// Target ABV auto-calc: while abvAuto is true, Target ABV tracks Measured OG and
// Target FG using the same (OG-FG)*131.25 formula used for the batch's actual ABV.
// Typing directly into Target ABV switches it to manual for the rest of this edit;
// clearing it back to blank re-enables auto-calc.
let abvAuto = true;
function autoCalcAbv() {
  if (!abvAuto) return;
  const og = parseFloat($("f_og").value), fg = parseFloat($("f_fg").value);
  if (og > 1 && fg > 0 && og > fg)
    $("f_abv").value = ((og - fg) * 131.25).toFixed(1);
}
$("f_og").addEventListener("input", autoCalcAbv);
$("f_fg").addEventListener("input", autoCalcAbv);
$("f_abv").addEventListener("input", () => {
  abvAuto = $("f_abv").value.trim() === "";
});
function openModal(rebrew) {
  // guard against a stray event object being passed in (e.g. a bare
  // addEventListener("click", openModal)) — only a real {color, batch} seed counts
  rebrewFrom = (rebrew && typeof rebrew === "object" && rebrew.batch) ? rebrew : null;
  const D = state.data;
  const b = rebrewFrom ? rebrewFrom.batch : (D.batch || {});
  const color = rebrewFrom ? rebrewFrom.color : (D.color || "");
  $("mtitle").textContent = rebrewFrom
    ? "New batch from “" + batchTitle(b) + "” — " + color + " Tilt"
    : color + " Tilt — batch details";
  $("f_name").value = b.name || "";
  $("f_style").value = b.style || "";
  // Rebrewing always starts a fresh "Day 1" today; otherwise show whatever
  // Day 1 currently is (the batch's stored start, or — if there's no batch
  // yet — the first logged reading) so it's easy to see and correct.
  const curStart = rebrewFrom ? (Date.now() / 1000)
    : (b.start_ts ?? (D.stats && D.stats.first_ts));
  $("f_start").value = curStart != null ? isoDateLocal(curStart) : "";
  $("f_size").value = b.batch_size || "";
  $("f_yeast").value = b.yeast || "";
  $("f_ibu").value = b.ibu ?? "";
  $("f_og").value = rebrewFrom ? "" : (b.og_override ?? "");
  $("f_fg").value = b.target_fg ?? "";
  $("f_abv").value = b.target_abv ?? "";
  abvAuto = (b.target_abv == null);   // no stored target yet: keep it synced to OG/FG
  $("f_temp").value = b.temp_target_f ?? "";
  $("f_notes").value = b.notes || "";
  imgState = rebrewFrom ? (b.image || "") : undefined;
  $("f_img").value = "";
  showImgPreview(b.image || null);
  $("mSave").style.display = rebrewFrom ? "none" : "";
  $("mFinish").style.display = (!rebrewFrom && D.batch) ? "" : "none";
  $("mNew").textContent = rebrewFrom ? "Start batch" : "Start new batch";
  $("mhint").textContent = rebrewFrom
    ? ("“Start batch” creates a new " + color + " Tilt batch using these details " +
       "as a starting point. If that Tilt currently has a batch in progress, it's closed first.")
    : ("\"Save\" edits the current batch. \"Start new batch\" closes the current one and " +
       "begins a fresh batch now — OG, ABV and charts reset from this moment. Data is " +
       "stored in batches.json on the Pi.");
  // Recipe builder is for planning a batch before it exists -- once a batch has
  // actually been started and saved, editing it shouldn't re-offer the mead
  // calculator/yeast/flavor wheels (a rebrew is still "unsaved" at this point,
  // so it keeps showing there).
  $("calc").style.display = (D.batch && !rebrewFrom) ? "none" : "";
  $("calc").open = false;
  flavorSel = parsePicks(b.notes, "Flavor picks: ");
  odorSel = parsePicks(b.notes, "Aroma picks: ");
  refreshFlavor(); refreshOdor();
  calcMead();
  autoCalcAbv();
  $("overlay").style.display = "flex";
  $("f_name").focus();
  // pick up any Admin-page wheel edits made since the last fetch, then
  // re-parse notes against the (possibly renamed) ingredients and re-render
  loadWheels().then(() => {
    flavorSel = parsePicks(b.notes, "Flavor picks: ");
    odorSel = parsePicks(b.notes, "Aroma picks: ");
    refreshFlavor(); refreshOdor();
  });
}
$("f_img").addEventListener("change", ev => {
  const f = ev.target.files[0]; if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    const im = new Image();
    im.onload = () => {
      const s = Math.min(1, 512 / Math.max(im.width, im.height));
      const c = document.createElement("canvas");
      c.width = Math.max(Math.round(im.width * s), 1);
      c.height = Math.max(Math.round(im.height * s), 1);
      c.getContext("2d").drawImage(im, 0, 0, c.width, c.height);
      imgState = c.toDataURL("image/jpeg", 0.82);
      showImgPreview(imgState);
    };
    im.src = rd.result;
  };
  rd.readAsDataURL(f);
});
$("imgclear").addEventListener("click", () => {
  imgState = ""; $("f_img").value = ""; showImgPreview(null);
});
function fields() {
  const o = {
    name: $("f_name").value, style: $("f_style").value,
    batch_size: $("f_size").value, yeast: $("f_yeast").value,
    ibu: $("f_ibu").value, og_override: $("f_og").value,
    target_fg: $("f_fg").value, target_abv: $("f_abv").value,
    temp_target_f: $("f_temp").value,
    notes: $("f_notes").value,
  };
  const st = dateStrToTs($("f_start").value);
  if (st != null) o.start_ts = st;
  if (imgState !== undefined) o.image = imgState;
  return o;
}
async function postBatch(action, colorOverride) {
  try {
    const color = colorOverride || state.view;
    const r = await fetch("/api/batch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, color, batch: fields() }),
    });
    if (!r.ok) throw new Error();
    $("overlay").style.display = "none";
    if (colorOverride) { state.pastId = null; state.view = colorOverride; }
    load(true);
  } catch (e) { alert("Could not save — is the dashboard server still running?"); }
}

/* ---------- recipe builder: mead calc, yeast picker, flavor/aroma wheels ---------- */
const RM = RECIPE_DATA.recipeMechanics;
let flavorSel = new Set(), odorSel = new Set();

function pickYeast(abv) {
  const cands = RM.yeastStrains.filter(y => y.abvTolerance >= abv)
                                .sort((a, b) => a.abvTolerance - b.abvTolerance);
  return cands.length ? cands[0]
    : { name: "none of the usual strains", abvTolerance: 18,
        profile: "above typical yeast tolerance (~18%) — step-feed or fortify instead." };
}
function calcMead() {
  const vol = parseFloat($("c_vol").value), abv = parseFloat($("c_abv").value);
  const fg = parseFloat($("c_sweet").value), ppg = parseFloat($("c_honey").value);
  const out = $("cout"); out.textContent = "";
  syncAbvTiers(); syncYeastPicks(); refreshWarnings();
  if (!(vol > 0) || !(abv > 0)) {
    out.textContent = "Enter a batch volume and target ABV.";
    return null;
  }
  const og = fg + abv / 131.25;
  const lbs = (og - 1) * 1000 * vol / ppg;
  const water = Math.max(vol - lbs / 12, 0);
  const y = pickYeast(abv);
  const big = document.createElement("div"); big.className = "big";
  big.textContent = lbs.toFixed(1) + " lb honey";
  out.append(big);
  const line = (label, val) => { const d = document.createElement("div");
    const b = document.createElement("b"); b.textContent = val;
    d.append(label + ": ", b); out.append(d); };
  line("(also", (lbs * 0.4536).toFixed(2) + " kg / " + (lbs * 16).toFixed(0) + " oz)");
  line("Est. OG", og.toFixed(3));
  line("Target FG", fg.toFixed(3));
  line("Water to add", water.toFixed(1) + " US gal");
  line("Yeast suggestion", y.name + " (to ~" + y.abvTolerance + "% ABV)");
  return { og, fg, lbs, water, vol, abv, ppg };
}
for (const id of ["c_vol", "c_abv", "c_sweet", "c_honey"])
  $(id).addEventListener("input", calcMead);
$("cApply").addEventListener("click", () => {
  const r = calcMead(); if (!r) return;
  $("f_og").value = r.og.toFixed(3);
  $("f_fg").value = r.fg.toFixed(3);
  if (!$("f_abv").value.trim()) $("f_abv").value = r.abv;
  if (!$("f_size").value.trim()) $("f_size").value = r.vol + " gal";
  const note = "Mead calc: " + r.lbs.toFixed(1) + " lb honey (" + r.ppg +
    " PPG) + " + r.water.toFixed(1) + " gal water → " + r.vol +
    " gal @ " + r.abv + "% ABV, OG " + r.og.toFixed(3) + ", FG " + r.fg.toFixed(3) + ".";
  const n = $("f_notes");
  if (!n.value.includes("Mead calc:")) n.value += (n.value ? "\n" : "") + note;
});

function buildAbvTiers() {
  const row = $("abvTiers"); row.textContent = "";
  for (const t of RM.abvTargets) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "pill"; b.title = t.description;
    b.dataset.id = t.id;
    b.textContent = t.label + " (" + t.range + ")";
    b.addEventListener("click", () => {
      $("c_abv").value = Math.round((t.minAbv + t.maxAbv) / 2);
      calcMead();
    });
    row.append(b);
  }
}
function syncAbvTiers() {
  const abv = parseFloat($("c_abv").value);
  for (const b of $("abvTiers").children) {
    const t = RM.abvTargets.find(x => x.id === b.dataset.id);
    b.setAttribute("aria-pressed", !!(t && abv >= t.minAbv && abv <= t.maxAbv));
  }
}
function buildSweetness() {
  const sel = $("c_sweet"); sel.textContent = "";
  RM.sweetnessLevels.forEach((s, i) => {
    const o = document.createElement("option");
    o.value = s.fg; o.textContent = s.label + " (" + s.fgRange + ")"; o.title = s.description;
    if (i === 1) o.selected = true;
    sel.append(o);
  });
}
function buildYeastPicks() {
  const row = $("yeastPicks"); row.textContent = "";
  for (const y of RM.yeastStrains) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "pill"; b.dataset.id = y.id;
    b.textContent = y.name + " (to ~" + y.abvTolerance + "%)";
    b.addEventListener("click", () => {
      $("f_yeast").value = y.name;
      showYeastInfo(y);
      syncYeastPicks();
      refreshWarnings();
    });
    row.append(b);
  }
}
function syncYeastPicks() {
  const abv = parseFloat($("c_abv").value);
  const cur = $("f_yeast").value.trim();
  for (const b of $("yeastPicks").children) {
    const y = RM.yeastStrains.find(x => x.id === b.dataset.id);
    b.classList.toggle("rec", abv > 0 && y.abvTolerance >= abv);
    b.setAttribute("aria-pressed", cur === y.name);
  }
}
function showYeastInfo(y) {
  const box = $("yeastInfo"); box.hidden = false; box.textContent = "";
  const head = document.createElement("div");
  const b = document.createElement("b");
  b.textContent = y.name + " — tolerance ~" + y.abvTolerance + "% ABV";
  head.append(b); box.append(head);
  const p = document.createElement("div"); p.textContent = y.profile;
  box.append(p);
}

function wheelIngredients(wheel) {
  const m = new Map();
  for (const cat of wheel.categories) for (const ing of cat.ingredients) m.set(ing.id, ing.name);
  return m;
}
/* WHEELS starts from the built-in defaults (injected below) so the picker
   works instantly; loadWheels() then swaps in any Admin-edited version. */
let WHEELS = { flavorWheel: RECIPE_DATA.flavorWheel, odorWheel: RECIPE_DATA.odorWheel };
let FLAVOR_NAMES = wheelIngredients(WHEELS.flavorWheel);
let ODOR_NAMES = wheelIngredients(WHEELS.odorWheel);
function applyWheels(w) {
  WHEELS = w;
  FLAVOR_NAMES = wheelIngredients(w.flavorWheel);
  ODOR_NAMES = wheelIngredients(w.odorWheel);
}
async function loadWheels() {
  try { applyWheels(await (await fetch("/api/recipe")).json()); }
  catch (e) { /* keep the built-in defaults already in WHEELS */ }
}

/* Admin-curated fermentation stage list (Secondary fermentation, Bulk aging,
   ...) a batch's "Mark stage..." picker offers. Loaded once at startup (like
   WHEELS) so it's ready the first time a batch detail view opens, not just
   after visiting Admin. */
let STAGES = [];
async function loadStages() {
  try { STAGES = (await (await fetch("/api/stages")).json()).stages || []; }
  catch (e) { /* keep whatever STAGES already had */ }
}
async function stageAction(body) {
  try {
    const r = await fetch("/api/stages", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) { alert(j.error || "Request failed"); return null; }
    STAGES = j.stages || [];
    renderStagesAdmin();
    return j;
  } catch (e) { alert("Could not reach the dashboard server."); return null; }
}
function renderStagesAdmin() {
  const host = $("stageList");
  if (!host) return;   // admin view not open
  host.textContent = "";
  for (const s of STAGES) {
    const row = el(host, "div", "wheelcat");
    el(row, "b", null, s.name);
    const ren = el(row, "button", "link", "rename");
    ren.type = "button";
    ren.addEventListener("click", () => {
      const name = prompt("Stage name:", s.name);
      if (name === null || !name.trim()) return;
      stageAction({ action: "rename", id: s.id, name: name.trim() });
    });
    const del = el(row, "button", "link", "delete");
    del.type = "button"; del.style.color = "var(--danger)"; del.style.marginLeft = "auto";
    del.addEventListener("click", () => {
      if (confirm('Remove "' + s.name + '" from the fermentation stage list? Batches that ' +
                  'already have a "' + s.name + '" marker keep it -- this only affects new ones.'))
        stageAction({ action: "delete", id: s.id });
    });
  }
  if (!STAGES.length)
    el(host, "div", "ahint", "No stages defined — add one below.");
}
$("stageAdd").addEventListener("click", () => {
  const v = $("stageNew").value.trim();
  if (!v) { alert("Enter a stage name."); return; }
  stageAction({ action: "add", name: v }).then(j => { if (j) $("stageNew").value = ""; });
});
function wheelBlock(container, wheel, sel, onChange) {
  container.textContent = "";
  for (const cat of wheel.categories) {
    const head = document.createElement("div"); head.className = "wheelcat";
    const dot = document.createElement("span"); dot.className = "dot";
    dot.style.background = cat.hex;
    const b = document.createElement("b"); b.textContent = cat.name;
    head.append(dot, b);
    container.append(head);
    const row = document.createElement("div"); row.className = "pillrow";
    for (const ing of cat.ingredients) {
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "pill"; btn.title = ing.role;
      btn.textContent = ing.name;
      btn.setAttribute("aria-pressed", sel.has(ing.id));
      btn.addEventListener("click", () => {
        if (sel.has(ing.id)) sel.delete(ing.id); else sel.add(ing.id);
        onChange();
      });
      row.append(btn);
    }
    container.append(row);
  }
}
function renderTags(container, sel, names, onChange) {
  container.textContent = "";
  for (const id of sel) {
    const t = document.createElement("span"); t.className = "tag";
    t.append(document.createTextNode(names.get(id) || id));
    const x = document.createElement("button"); x.type = "button"; x.textContent = "×";
    x.setAttribute("aria-label", "Remove");
    x.addEventListener("click", () => { sel.delete(id); onChange(); });
    t.append(x);
    container.append(t);
  }
}
function refreshFlavor() {
  wheelBlock($("flavorWheel"), WHEELS.flavorWheel, flavorSel, refreshFlavor);
  renderTags($("flavorTags"), flavorSel, FLAVOR_NAMES, refreshFlavor);
  refreshWarnings();
}
function refreshOdor() {
  wheelBlock($("odorWheel"), WHEELS.odorWheel, odorSel, refreshOdor);
  renderTags($("odorTags"), odorSel, ODOR_NAMES, refreshOdor);
  refreshWarnings();
}

/* ---------- recipe warnings: data-driven advisory checks ----------
   Not a hard gate — Save/Apply still work either way. These flag
   combinations that are well-known to risk a stuck fermentation or an
   unbalanced mead, with the reasoning spelled out so you can judge for
   yourself whether to adjust. */
function isIntense(role) {
  const r = role.toLowerCase();
  return ["intense", "heavy", "aggressive", "micro-dosing", "extreme"].some(w => r.includes(w));
}
const HEAT_IDS = new Set(["carolina_reaper"]);
const BITTER_IDS = new Set(["dark_roast_coffee", "sarsaparilla_root"]);

function computeWarnings() {
  const out = [];
  const abv = parseFloat($("c_abv").value);
  const fg = parseFloat($("c_sweet").value);
  const sweetOpt = $("c_sweet").selectedOptions[0];
  const sweetIdx = sweetOpt ? [...$("c_sweet").options].indexOf(sweetOpt) : -1;
  const sweetId = sweetIdx >= 0 && RM.sweetnessLevels[sweetIdx] ? RM.sweetnessLevels[sweetIdx].id : null;

  if (abv > 0) {
    const curName = $("f_yeast").value.trim();
    const matched = RM.yeastStrains.find(y => y.name === curName);
    if (matched && matched.abvTolerance < abv) {
      const covers = RM.yeastStrains.filter(y => y.abvTolerance >= abv).map(y => y.name);
      out.push({ level: "danger", title: "Yeast may not reach your target ABV",
        detail: matched.name + " typically tops out around " + matched.abvTolerance +
          "% ABV, below your " + abv + "% target — fermentation risks stalling short. " +
          (covers.length ? "Consider " + covers.join(" or ") + " instead."
                         : "None of these five reliably cover " + abv +
                           "% — step-feed the honey in stages or fortify after fermentation.") });
    } else if (!matched && abv > 18) {
      out.push({ level: "danger", title: "Target ABV exceeds typical yeast tolerance",
        detail: abv + "% is above what any of these five strains reliably ferment to (~18% max) — " +
          "expect a stuck fermentation unless you step-feed the honey in stages or fortify after the fact." });
    }
  }

  if (abv > 0 && !isNaN(fg)) {
    const og = fg + abv / 131.25;
    if (og >= 1.130) {
      out.push({ level: "danger", title: "Very high starting gravity (est. OG " + og.toFixed(3) + ")",
        detail: "Above roughly 1.130 OG, osmotic stress can stall even a tolerant yeast. Rehydrate " +
          "with a stress protectant (e.g. Go-Ferm) and add the honey in 2–3 stages (TOSNA-style " +
          "staggered nutrient additions) instead of pitching the full gravity at once." });
    }
  }

  const heatPicked = [...flavorSel].filter(id => HEAT_IDS.has(id)).map(id => FLAVOR_NAMES.get(id));
  if (heatPicked.length && sweetId === "bone_dry") {
    out.push({ level: "caution", title: "Capsaicin heat with a bone-dry finish",
      detail: heatPicked.join(" and ") + "'s heat is usually balanced by residual sweetness — " +
        "fermented fully dry, the capsaicin can taste harsh and one-dimensional. Consider " +
        "Semi-Sweet or Sweet/Dessert, or dose the pepper very conservatively." });
  }

  const bitterPicked = [...flavorSel].filter(id => BITTER_IDS.has(id)).map(id => FLAVOR_NAMES.get(id));
  if (bitterPicked.length && sweetId === "bone_dry") {
    out.push({ level: "caution", title: "Heavy roasted bitterness with a bone-dry finish",
      detail: bitterPicked.join(" and ") + " lean hard on roasty bitterness, which a bone-dry mead " +
        "has no residual sugar to round out — expect a sharp, astringent finish. Semi-Sweet or " +
        "Sweet/Dessert usually balances this better." });
  }

  const intenseNames = new Map(WHEELS.flavorWheel.categories.flatMap(c => c.ingredients)
    .filter(ing => isIntense(ing.role)).map(ing => [ing.id, ing.name]));
  const stacked = [...flavorSel].filter(id => intenseNames.has(id)).map(id => intenseNames.get(id));
  if (stacked.length >= 2) {
    out.push({ level: "caution", title: "Several high-intensity additions at once",
      detail: stacked.join(", ") + " are each called out as intense or heavy on their own. Combined " +
        "at full rates they risk a muddled, overly sharp or astringent mead — consider picking one as " +
        "the lead flavor and dropping or reducing the rest." });
  }
  return out;
}
function refreshWarnings() {
  const box = $("rbWarn"); if (!box) return;
  box.textContent = "";
  for (const w of computeWarnings()) {
    const d = document.createElement("div"); d.className = "rbwarn-item " + w.level;
    const b = document.createElement("b"); b.textContent = "⚠ " + w.title;
    const p = document.createElement("div"); p.textContent = w.detail;
    d.append(b, p); box.append(d);
  }
}
$("f_yeast").addEventListener("input", () => { syncYeastPicks(); refreshWarnings(); });
function parsePicks(notes, prefix) {
  const line = (notes || "").split("\n").find(l => l.startsWith(prefix));
  const names = prefix.startsWith("Flavor") ? FLAVOR_NAMES : ODOR_NAMES;
  const byName = new Map([...names].map(([id, nm]) => [nm, id]));
  if (!line) return new Set();
  return new Set(line.slice(prefix.length).replace(/\.$/, "").split(",")
    .map(s => byName.get(s.trim())).filter(Boolean));
}
function setNoteLine(prefix, text) {
  const n = $("f_notes");
  let lines = n.value.split("\n").filter(l => !l.startsWith(prefix));
  if (lines.length === 1 && lines[0] === "") lines = [];
  if (text) lines.push(prefix + text);
  n.value = lines.join("\n");
}
$("rbApply").addEventListener("click", () => {
  const flavors = [...flavorSel].map(id => FLAVOR_NAMES.get(id));
  const odors = [...odorSel].map(id => ODOR_NAMES.get(id));
  setNoteLine("Flavor picks: ", flavors.length ? flavors.join(", ") + "." : "");
  setNoteLine("Aroma picks: ", odors.length ? odors.join(", ") + "." : "");
});
buildAbvTiers(); buildSweetness(); buildYeastPicks();

$("mSave").addEventListener("click", () => postBatch("save"));
$("mNew").addEventListener("click", () => {
  if (rebrewFrom) {
    if (confirm("Start a new " + rebrewFrom.color + " Tilt batch using these details? " +
                "If that Tilt currently has a batch in progress, it will be closed first."))
      postBatch("new", rebrewFrom.color);
  } else if (confirm("Close the current batch and start a new one from now? " +
              "OG and stats will reset from this moment."))
    postBatch("new");
});
$("mFinish").addEventListener("click", () => {
  if (confirm("Mark this batch finished? It moves to History and a future " +
              "'Start new batch' begins fresh."))
    postBatch("finish");
});
$("mCancel").addEventListener("click", () => { rebrewFrom = null; $("overlay").style.display = "none"; });
$("overlay").addEventListener("click", ev => {
  if (ev.target === $("overlay")) { rebrewFrom = null; $("overlay").style.display = "none"; }
});

/* ---------- controls ---------- */
$("ranges").addEventListener("click", ev => {
  const b = ev.target.closest("button"); if (!b) return;
  for (const x of $("ranges").children) x.setAttribute("aria-pressed", x === b);
  state.hours = parseFloat(b.dataset.h);
  load(true);
});
$("tbtn").addEventListener("click", () => {
  const w = $("tablewrap"), open = w.style.display === "block";
  w.style.display = open ? "none" : "block";
  $("tbtn").textContent = open ? "Table view" : "Hide table";
});
addEventListener("resize", () => { if (state.data) render(); });
darkMq.addEventListener("change", () => { if (state.data) render(); });

load(true);
loadWheels();
loadStages();
setInterval(() => { if ($("overlay").style.display !== "flex") load(false); }, 30000);
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Built-in user guide (served at /guide, linked from the dashboard footer)
# ----------------------------------------------------------------------------

GUIDE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>User Guide — __BRAND_NAME__</title>
<link rel="icon" type="image/png" href="__FAVICON__">
<style>
 :root { color-scheme:light;
   --page:#e7dcc3; --surface:#f2ead6; --ink:#241d12; --ink-2:#5d5340;
   --muted:#87795d; --border:rgba(60,45,20,.18); --olive:#6a6d3a;
   --logo-filter:brightness(.55) contrast(1.1); }
 @media (prefers-color-scheme: dark) { :root { color-scheme:dark;
   --page:#0d0b08; --surface:#171410; --ink:#ede3cf; --ink-2:#c8bba0;
   --muted:#8f8570; --border:rgba(237,227,207,.13); --olive:#8b8b52;
   --logo-filter:none; } }
 * { box-sizing:border-box; margin:0; }
 body { background:var(--page); color:var(--ink); padding:24px 20px;
        font:14.5px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
        max-width:820px; margin:0 auto; }
 header { display:flex; align-items:center; gap:13px; margin-bottom:6px; }
 header img { height:46px; filter:var(--logo-filter); }
 .brandmark { height:46px; width:46px; border-radius:50%; flex:none;
              background:var(--ink); color:var(--page); display:flex;
              align-items:center; justify-content:center;
              font:700 17px Georgia,"Iowan Old Style",serif; }
 .wm { font:700 20px/1.1 Georgia,"Iowan Old Style",serif; letter-spacing:.05em; text-transform:uppercase; }
 .wm2 { font-size:10.5px; letter-spacing:.34em; color:var(--olive); text-transform:uppercase; margin-top:3px; font-weight:600; }
 a { color:var(--ink-2); }
 .back { font-size:13px; margin:10px 0 20px; display:inline-block; }
 h2 { font:600 14px Georgia,serif; letter-spacing:.16em; text-transform:uppercase;
      color:var(--ink-2); margin:26px 0 8px; border-bottom:1px solid var(--border); padding-bottom:5px; }
 p, li { color:var(--ink-2); }
 b, strong { color:var(--ink); }
 ul { padding-left:20px; margin:6px 0; }
 li { margin:4px 0; }
 code { font-family:ui-monospace,monospace; font-size:12.5px; background:var(--surface);
        border:1px solid var(--border); border-radius:5px; padding:1px 5px; }
 .tip { background:var(--surface); border:1px solid var(--border); border-radius:9px;
        padding:10px 14px; margin:10px 0; font-size:13.5px; color:var(--ink-2); }
 footer { text-align:center; color:var(--muted); font-size:11px; letter-spacing:.3em;
          text-transform:uppercase; margin:34px 0 8px; }
</style></head><body>
<header><div class="brandmark" aria-hidden="true">__BRAND_INITIALS__</div>
  <div><div class="wm">__BRAND_NAME__</div><div class="wm2">User Guide</div></div></header>
<a class="back" href="/">&larr; Back to the dashboard</a>

<h2>Reading the dashboard</h2>
<p><b>All Tilts</b> is the home view: one card per Tilt hydrometer showing its batch
name, day count, current gravity, ABV (and target ABV, if you've set one),
temperature, and attenuation, plus combined
gravity, Est. ABV, and temperature charts — one line per Tilt, drawn in that Tilt's
colour. Hover (or tap) a chart for exact values; the crosshair follows all three
charts together. Click a card or a colour tab to open that Tilt's <b>detail view</b>,
with stat tiles, the same three charts for just that Tilt (dashed lines mark your
target FG, target ABV, and target temperature), and a table of recent readings
behind the <b>Table view</b> link. The time-range buttons (24h &middot; 3d &middot;
7d &middot; 30d &middot; Batch) rescale everything; <b>Batch</b> means "since this
brew started". The page refreshes itself every 30 seconds.</p>
<div class="tip">A Tilt only gets a card (and a line on the combined charts) while it
has a <b>current batch</b>. A Tilt that's never had a batch started, or whose batch
has been marked finished, still gets a colour tab up top so you can jump to it and
start a fresh batch with <b>Add batch details</b>, or begin one from an old brew with
<b>Rebrew from history&hellip;</b> or <b>Rebrew from archive&hellip;</b> (see
"Rebrewing" under History below) — it just won't clutter the overview with stale or
empty data. Once finished, a batch's data lives in <b>History</b> instead.</div>

<h2>Battery status</h2>
<p>Some Tilt firmware supports a real battery-age feature. When a report comes
through for a Tilt, you'll see a small gauge icon underneath that Tilt's colour tab
in the top nav bar, and the same icon next to the colour and name on its All-Tilts
overview card — a number of weeks shown inside the icon, with its fill colour
fading continuously from green (just changed) to red (a year or more since). The
same reading also appears as a <b>Battery</b> stat tile in that Tilt's detail view,
as a <code>battery_weeks</code> column in its CSV export, and as a line on its
printable report. <b>The number is weeks since that Tilt's battery was last
changed &mdash; not a charge percentage.</b></p>
<div class="tip">Battery reporting is <b>not an official Tilt feature</b>, but it is
a real one, implemented by the open-source
<a href="https://github.com/thorrak/tiltbridge">TiltBridge</a> project, which this
dashboard's logger mirrors: some Tilt firmware broadcasts it occasionally, and
other units never do at all. If you never see a battery icon or tile for one of
your Tilts, that simply means yours hasn't reported one; it isn't a sign of a
problem, and there's no setting to turn it on.</div>

<h2>Batches — your brew sessions</h2>
<p>Each Tilt carries one <b>active batch</b>. In a Tilt's detail view, <b>Edit
batch</b> opens the batch editor: name, brew date, style, batch size, yeast, IBU,
measured OG, target FG, target ABV, target fermentation temperature, an image, and
notes.</p>
<ul>
 <li><b>Brew date</b> — sets what "Day 1" is, everywhere it's shown (overview cards,
   this page, History). It's pre-filled from the batch's current start (or the first
   logged reading, if there's no batch yet) — change it any time if logging started
   a bit before or after you actually pitched. It also applies when you use
   <b>Start new batch</b> or <b>Rebrew this batch</b>: set it before starting to
   begin Day 1 on the date you actually brewed rather than the moment you clicked
   the button.</li>
 <li><b>Measured OG</b> — enter your brew-day hydrometer/refractometer reading and
   ABV is computed from it; otherwise the first logged reading is used as OG.</li>
 <li><b>Target ABV</b> — auto-calculates from Measured OG and Target FG as you type
   either one (same formula as Est. ABV below), so for most brews you just enter OG
   and target FG and it fills itself in. Type directly into it to set a value by
   hand instead — useful for mead/cider where you often pick a target ABV first and
   work backwards; clear it back to blank to resume auto-calculating. It shows in
   two places: on the All Tilts overview card next to the batch's live calculated
   ABV, and on this page's "Est. ABV" tile. The Recipe builder's "Apply" button
   fills it in from the calculator's target ABV if you haven't already set one by
   hand.</li>
 <li><b>Brew image</b> — upload any photo or label art; it's resized in your browser
   and shown on the batch tile, in History, and in the detail header.</li>
 <li><b>Start new batch</b> — closes the current batch and starts fresh from this
   moment. OG, ABV, attenuation and the Batch chart range all reset. Use this on
   brew day when a new wort goes on the same Tilt.</li>
 <li><b>Mark finished</b> — freezes the batch at packaging and moves it to History; the
   Tilt then disappears from the live All Tilts overview (its colour tab stays, but with
   no current batch) until you start a new one.</li>
</ul>
<div class="tip">Formulas: ABV = (OG &minus; SG) &times; 131.25 &middot; apparent
attenuation = (OG &minus; SG) &divide; (OG &minus; 1) &times; 100.</div>

<h2>Recipe builder</h2>
<p>Inside the batch editor, expand <b>Recipe builder</b>. It has four parts:</p>
<div class="tip">The Recipe builder is for planning a batch before it exists, so it
only shows up while you're editing a batch that hasn't been started yet — a brand
new Tilt's <b>Add batch details</b>, or a rebrew prefill before you click <b>Start
batch</b>. Once a batch has actually been started and saved, <b>Edit batch</b> opens
without it, keeping the editor focused on the brew in progress.</div>
<ul>
 <li><b>Mead calculator</b> — pick a target style (Session Hydromel, Standard Mead,
   or High Gravity Sack — or type your own ABV%), sweetness (Bone Dry / Semi-Sweet /
   Sweet-Dessert), batch volume, and honey variety, and it computes the honey weight,
   estimated OG, water to add, and a yeast suggestion. <b>Apply OG / FG / size to
   batch</b> copies the results into the batch fields and adds a recipe line to your
   notes. (Math per the
   <a href="https://rawhoneyguide.com/tools/honey-mead-calculator">rawhoneyguide.com
   calculator</a>.)</li>
 <li><b>Yeast strain</b> — five common mead yeasts (Lalvin D47, EC-1118, 71B, SafAle
   US-05, Mangrove Jack's M05), each with its ABV tolerance and flavor profile.
   Strains that can finish your target ABV are outlined; click one to drop its name
   into the batch's Yeast field and read its full profile.</li>
 <li><b>Flavor wheel</b> — additive ingredients (honeys, fruits/acids, spices, and
   earthy/tannic/bitter notes) grouped into four color-coded categories. Click an
   ingredient to tag it; hover for what it contributes.</li>
 <li><b>Aroma wheel</b> — the same idea for how a mead <i>smells</i>: floral, bright
   citrus/tropical, roasted/warm, and resinous/herbal/forest categories.</li>
</ul>
<p>Missing an ingredient? Add it from <b>Admin &rarr; Recipe wheels</b> &mdash; see
below.</p>
<p>Selected flavor and aroma picks aren't saved until you click <b>Add picks to
notes</b>, which writes a "Flavor picks:" and "Aroma picks:" line into the batch
notes (re-opening the editor reloads your picks from those lines, so it's safe to
keep tweaking them across visits).</p>
<p>As you fill in the calculator, yeast, and flavor wheel, a <b>warnings</b> box can
appear below the calculator output flagging combinations known to risk a stuck
fermentation or an unbalanced mead &mdash; each with the reasoning spelled out. It
checks: a chosen yeast whose ABV tolerance is below your target (and suggests one
that covers it); a target ABV above all five strains' typical range; a very high
starting gravity (&ge; 1.130 OG) that risks stalling even a tolerant yeast; capsaicin
heat or heavy roasted bitterness picked alongside a Bone Dry finish (no residual
sweetness to round them out); and several high-intensity flavor picks stacked
together. These are advisory only &mdash; Save and Apply still work regardless.</p>

<h2>Chart notes</h2>
<p>On a batch's detail view, click any point on the gravity, Est. ABV, or temperature
chart to pin a note to that exact moment — a dry hop, a temperature change, a
krausen drop, anything worth remembering later. The note gets a small diamond
marker on all three charts (they're synced, so it shows up at the same moment on
each one); click an existing marker to read its note. Every note you've added shows
up in the <b>Chart notes</b> panel on the right of the batch's info card, earliest
first with its timestamp. This works on the live batch and on a past brew from
History too, as long as its raw data hasn't been trimmed (trimming removes the
charts themselves, so there's nothing left to click).</p>
<div class="tip">Chart notes are permanent once saved — there's no edit or delete,
by design, so the timeline stays a trustworthy record of what you actually observed
and when. They ride along with the batch into its History entry, its printable
report and CSV, and any archive you download (markers and all), so the full
annotated picture travels with the brew. Rebrewing — from History or from an
archive file — always starts a new batch with an empty notes timeline; old notes
stay with the original brew.</div>

<h2>Primary start &amp; fermentation stages</h2>
<p>Two more ways to mark a batch's timeline, separate from chart notes above — useful
when you want the moment itself recorded, not just a note about it.</p>
<ul>
 <li><b>Set primary start&hellip;</b> — on the live batch's toolbar. Click it, and a
   banner tells you to click a point on a chart below; whichever point you click
   becomes the batch's new start (Day 1), with a confirmation showing the exact
   date/time first. This is really just <b>Brew date</b>, set visually instead of
   typed — readings from before that point stop counting toward the batch's stats and
   charts, but nothing is deleted, so you can change it again later exactly like Brew
   date always worked. Handy when a Tilt was logging for a while before you'd
   actually call it "pitched": set primary start once fermentation visibly begins and
   the earlier, not-yet-fermenting readings quietly drop out of the window.</li>
 <li><b>Mark stage&hellip;</b> — on the live batch's toolbar, and on any non-trimmed
   past batch's page in History. Click it, pick a stage name from the list (see
   Admin, below), then click a point on a chart. A confirmation warns this <b>can't
   be changed or removed afterward</b> before it saves.</li>
</ul>
<p>A plain click on a chart (no "Set primary start&hellip;" or "Mark stage&hellip;"
armed) always opens <b>Add a note</b> as usual — these two buttons are the only way
to change what a click does, and a banner with a <b>Cancel</b> link always shows
while one is armed, so there's never any ambiguity about what the next click will
do.</p>
<p>Once set, a fermentation-stage marker shows as a labeled vertical dashed line
across all three synced charts — deliberately different from both the horizontal
target-reference lines and the chart-note diamonds, since it marks a moment the
batch crossed into a new stage rather than one reading's value. Every stage marker
also lists in a <b>Fermentation stages</b> panel above Chart notes, earliest first.</p>
<div class="tip">Like chart notes, fermentation-stage markers are permanent once
saved and ride along with the batch into its History entry, printable report and
CSV, and any archive you download. Rebrewing always starts a new batch with an empty
stage timeline, same as it does for chart notes.</div>

<h2>Reports &amp; exporting data</h2>
<p>In any batch's detail view, <b>Export report</b> opens a printable brew report —
batch details, stats, gravity/Est. ABV/temperature charts (with any chart notes
plotted as markers and any fermentation-stage markers as labeled vertical lines,
plus a timestamped list of each), batch notes, and an hourly data table. Print it
(Ctrl/Cmd-P) to save a PDF — it includes a battery line when one has been reported.
<b>CSV</b> downloads every raw reading of the batch for spreadsheets or
Brewfather-style analysis, with a trailing <code>battery_weeks</code> column (blank on
rows with no battery reading). Both also work for finished brews from History.</p>
<div class="tip">Both follow the same active-batch rule as the live dashboard: from a
Tilt's colour tab with <b>no current batch</b>, Export report/CSV come back empty
rather than dumping all its history — open that brew from <b>History</b> instead to
get its report or CSV.</div>

<h2>History</h2>
<p>The <b>History</b> tab lists every finished brew with its image, dates, duration,
OG &rarr; FG, ABV, and attenuation. Click one to revisit its full charts and table,
or export its report and CSV. Summaries are snapshotted when a batch is finished, so
they survive log rotation, Tilt resets, and a deliberate Trim (below) — the charts,
table, and CSV need the raw data, so export a report or make an archive before any
of those happen if you'll want the full detail later. To remove old brews, click
<b>Select brews&hellip;</b>, pick the ones to drop, and <b>Delete selected</b> — this
permanently removes their summaries and images, so export any reports you want to
keep first.</p>

<h3>Archive &amp; Trim</h3>
<p>Open a finished brew and you'll see <b>Archive batch</b> and <b>Trim archived
data&hellip;</b> alongside Rebrew. <b>Archive batch</b> downloads one self-contained
<code>.html</code> file: the same printable report (chart notes and all), plus every one
of that batch's raw readings at full resolution, embedded right in the file — so the
whole brew travels as a single thing you can keep, email, or store off the Pi.
Downloading an archive doesn't change anything on the server.</p>
<p><b>Trim archived data&hellip;</b> is separate, and it does change the server: it
permanently deletes just that one batch's raw readings from the log to free up
space, leaving its History entry, summary stats, and image exactly as they were.
You're asked to confirm first, spelling out the batch, Tilt, and date range, because
<b>this cannot be undone</b> — make sure you've downloaded an archive (or don't need
the detail) before confirming. Once a batch is trimmed, its report explains the
charts/table/CSV are gone and points to the archive for the full data, and the
Archive/Trim buttons disappear from that batch's page, since there's nothing left to
archive or trim a second time.</p>

<h3>Rebrewing</h3>
<p>You never pick a colour for a rebrew — it's always whichever Tilt you're rebrewing
onto. There are three ways in:</p>
<ul>
 <li>From a Tilt with <b>no active batch</b>, its page offers <b>Rebrew from
   history&hellip;</b> — a dropdown of every finished brew across every colour; pick
   one and it's pre-filled onto this Tilt.</li>
 <li>The same page also offers <b>Rebrew from archive&hellip;</b> — pick an archive
   <code>.html</code> file you downloaded earlier, and the dashboard reads the batch
   details straight out of the file in your browser. Nothing is uploaded anywhere,
   so this works even for a batch that's since been trimmed or whose Tilt has since
   been reset.</li>
 <li>From a past brew's own page in <b>History</b>, <b>Rebrew this batch</b> picks a
   free Tilt for you automatically — whichever known colour currently has no active
   batch, alphabetically first — instead of assuming the brew's original colour, so
   you don't have to go find an open one yourself. If every Tilt currently has
   something brewing, you're told plainly that a new batch can't start until one is
   finished or reset.</li>
</ul>
<p>However you get there, the editor opens pre-filled from that old brew's details as
a starting point: name, style, batch size, yeast, IBU, target FG, target ABV, target
ferm temp, image, and notes (including flavor/aroma picks) all carry over — only the
measured OG is left blank, since that's specific to the new brew day — with today's
date as the new batch's Day 1. <b>Chart notes don't carry over</b> — the new batch
always starts with an empty notes timeline, even though everything else is
pre-filled. Review or edit anything first, then <b>Start batch</b>. The original
stays in History untouched; if the destination Tilt already has a batch in progress,
starting the rebrew closes it first, same as the regular "Start new batch".</p>

<h2>Admin</h2>
<ul>
 <li><b>Logging interval</b> — how often readings are recorded, from every beacon
   (~1&ndash;4 s) to hourly. The logger applies changes within about 5 seconds; no
   restart needed.</li>
 <li><b>Tilt data</b> — per-Tilt reading counts, CSV export, and <b>Reset</b>, which
   erases that Tilt's logged readings and its current batch so the next brew starts
   clean. Finished-brew summaries stay in History. Reset cannot be undone — export
   any reports first.</li>
 <li><b>Recipe wheels</b> — add, rename/recolor, or remove flavor and aroma
   categories and ingredients shown in the Recipe builder, right from the browser.
   A new ingredient always goes into a category you pick from what already exists,
   so it can't land in the wrong place, and it automatically takes that category's
   color. A new or recolored category checks its color against the wheel's other
   categories and asks for confirmation if it's too close to an existing one, so two
   categories don't end up looking the same. <b>Reset&hellip; to built-in</b> discards
   edits to that one wheel and restores the ingredients this dashboard shipped with.
   Edits are saved to <code>recipe-data.json</code> beside the log and survive
   restarts and software updates.</li>
 <li><b>Fermentation stages</b> — the list of stage names offered by <b>Mark
   stage&hellip;</b> on a batch's page. Add, rename, or remove entries the same way
   as Recipe wheels, above. Renaming or removing a stage here never rewrites markers
   already recorded on a batch — the stage name is copied as plain text the moment
   it's picked, so past records stay exactly as they were even as this list changes.
   <b>Reset&hellip; to built-in</b> restores the four stages this dashboard ships
   with (Secondary fermentation, Bulk aging, Oak aging, Bottle conditioning). Saved
   to <code>stages.json</code> beside the log and survives restarts and software
   updates.</li>
 <li><b>Software</b> — upload a new <code>tilt_dashboard.py</code> or
   <code>tilt_logger.py</code> to update without touching the command line. Files are
   syntax-checked first and the old version is kept as a <code>.bak</code>. The
   dashboard restarts itself; the logger restarts automatically when the watch units
   from SETUP.md are installed.</li>
</ul>

<h2>Recipe explorer</h2>
<p>The <a href="/recipes">Recipe explorer</a> is a separate page for sketching out
mead recipes before you brew, without needing a Tilt or batch open. It has the same
mead calculator, yeast strain picker, flavor wheel, aroma wheel, and recipe
warnings as the batch editor's Recipe builder, plus a <b>Save recipe</b> button so
you can build up a small library of named drafts and come back to refine them.
<b>Copy summary</b> writes the whole recipe as text to your clipboard, ready to
paste into a real batch's notes on brew day. Saved drafts live in
<code>recipe-drafts.json</code> beside the log and are completely independent of
any Tilt or batch until you copy one over.</p>

<h2>Tips &amp; troubleshooting</h2>
<ul>
 <li>The Tilt only broadcasts while floating or tilted — flat in its box it's silent.</li>
 <li>Two Tilts of the same colour can't be told apart; run concurrent brews on
   different colours.</li>
 <li>A "no readings" card usually means the Tilt is out of range (~10 m), the
   batch just started, or the logger service is stopped.</li>
 <li>Gravity readings drift with krausen and CO&#8322; bubbles early in fermentation —
   trust the trend, and calibrate against a hydrometer sample at packaging.</li>
 <li>No battery icon or tile for a Tilt just means yours hasn't broadcast one yet —
   it's an occasional, unofficial signal some Tilt firmwares send and others don't;
   see "Battery status" above.</li>
 <li>Trimming a batch's raw readings is permanent — there's no way to get them back
   from the dashboard afterward. If you made an archive first, the full data still
   lives in that downloaded file, and "Rebrew from archive&hellip;" can read it back
   in even though the server's own copy is gone.</li>
 <li>"That doesn't look like a dashboard archive file" when using Rebrew from
   archive&hellip; means the chosen file isn't one — only a <code>.html</code> file
   downloaded via a batch's <b>Archive batch</b> button carries the embedded data;
   a plain "Export report" file looks similar but doesn't.</li>
 <li>Everything here (this guide included) is served by the Pi itself — no internet
   required.</li>
 <li><b>Branding</b> — the name and tagline shown in the header, browser tab, and
   reports are set from the <b>Admin</b> page and apply immediately, no restart
   needed.</li>
</ul>

<footer id="footerTag">__BRAND_TAGLINE__</footer>
</body></html>
"""


RECIPES_TMPL = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recipe Explorer — __BRAND_NAME__</title>
<link rel="icon" type="image/png" href="__FAVICON__">
<style>
  :root { color-scheme:light;
    --page:#e7dcc3; --surface:#f2ead6; --ink:#241d12; --ink-2:#5d5340;
    --muted:#87795d; --border:rgba(60,45,20,.18); --accent:#6d4f2a; --accent-ink:#f2ead6;
    --olive:#6a6d3a; --danger:#a83232; --logo-filter:brightness(.55) contrast(1.1); }
  @media (prefers-color-scheme: dark) { :root { color-scheme:dark;
    --page:#0d0b08; --surface:#171410; --ink:#ede3cf; --ink-2:#c8bba0;
    --muted:#8f8570; --border:rgba(237,227,207,.13); --accent:#d2ae87; --accent-ink:#221a10;
    --olive:#8b8b52; --danger:#e06060; --logo-filter:none; } }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--page); color:var(--ink);
         font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
         padding:20px; max-width:860px; margin:0 auto; }
  header { display:flex; align-items:center; gap:13px; margin-bottom:6px; }
  header img { height:46px; filter:var(--logo-filter); }
  .brandmark { height:46px; width:46px; border-radius:50%; flex:none;
               background:var(--ink); color:var(--page); display:flex;
               align-items:center; justify-content:center;
               font:700 17px Georgia,"Iowan Old Style",serif; }
  .wm { font:700 20px/1.1 Georgia,"Iowan Old Style","Times New Roman",serif;
        letter-spacing:.05em; text-transform:uppercase; }
  .wm2 { font-size:10.5px; letter-spacing:.34em; color:var(--olive);
         text-transform:uppercase; margin-top:3px; font-weight:600; }
  a { color:var(--ink-2); }
  .back { font-size:13px; margin:10px 0 6px; display:inline-block; }
  .lede { color:var(--ink-2); font-size:13.5px; margin-bottom:18px; max-width:640px; }
  footer { text-align:center; color:var(--muted); font-size:11px; letter-spacing:.3em;
           text-transform:uppercase; margin:30px 0 8px; }
  .listhead { display:flex; align-items:center; justify-content:space-between;
              gap:10px; margin-bottom:12px; flex-wrap:wrap; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
           gap:10px; margin-bottom:10px; }
  .dcard { background:var(--surface); border:1px solid var(--border); border-radius:10px;
           padding:13px 14px; cursor:pointer; }
  .dcard:hover { border-color:var(--accent); }
  .dcard .dname { font-size:14.5px; font-weight:650; margin-bottom:3px; }
  .dcard .dmeta { font-size:12px; color:var(--ink-2); line-height:1.5; }
  .dcard .dtags { font-size:11.5px; color:var(--muted); margin-top:6px; }
  .dcard .drow { display:flex; gap:8px; margin-top:9px; }
  .dcard .drow button { flex:none; }
  .tip { background:var(--surface); border:1px solid var(--border); border-radius:9px;
         padding:12px 14px; margin:10px 0; font-size:13.5px; color:var(--ink-2); }
  .btn { font:inherit; font-size:13px; padding:6px 12px; border-radius:8px; cursor:pointer;
         border:1px solid var(--border); background:var(--surface); color:var(--ink);
         text-decoration:none; display:inline-block; }
  .btn.primary { background:var(--accent); border-color:var(--accent); color:var(--accent-ink); }
  .btn.danger { color:var(--danger); }
  .link { background:none; border:none; color:var(--ink-2); font:inherit; font-size:13px;
          cursor:pointer; text-decoration:underline; padding:6px 4px; }
  select { font:inherit; font-size:13px; background:var(--page); color:var(--ink);
           border:1px solid var(--border); border-radius:8px; padding:6px 8px; }
  .frow { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  .fld { margin-bottom:10px; }
  .fld label { display:block; font-size:12px; color:var(--muted); margin-bottom:3px; }
  .fld input, .fld select, .fld textarea { width:100%; font:inherit; font-size:13.5px; color:var(--ink);
        background:var(--page); border:1px solid var(--border); border-radius:8px; padding:7px 9px; }
  .fld textarea { min-height:64px; resize:vertical; }
  .mbtns { display:flex; gap:8px; margin-top:14px; flex-wrap:wrap; align-items:center; }
  .mbtns .spacer { flex:1; }
  .cout { background:var(--surface); border:1px solid var(--border); border-radius:8px;
          padding:10px 12px; font-size:13px; margin-top:2px; line-height:1.6; }
  .cout .big { font-size:19px; font-weight:650; }
  .cout b { font-weight:650; }
  .rbh { font-size:12.5px; font-weight:650; color:var(--ink-2); margin:16px 0 7px;
         text-transform:uppercase; letter-spacing:.06em; }
  .rbh:first-child { margin-top:2px; }
  .rbhint { font-size:11px; font-weight:400; text-transform:none; letter-spacing:0;
            color:var(--muted); margin-left:6px; }
  .pillrow { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
  .pill { border:1px solid var(--border); background:var(--surface); color:var(--ink-2);
          border-radius:999px; padding:5px 11px; font-size:12.5px; cursor:pointer; font:inherit; }
  .pill[aria-pressed="true"] { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); }
  .pill.rec:not([aria-pressed="true"]) { border-color:var(--olive); color:var(--olive); }
  .wheelcat { display:flex; align-items:center; gap:7px; margin:10px 0 6px; }
  .wheelcat .dot { width:12px; height:12px; margin:0; flex:none; border:1px solid var(--border); border-radius:50%; }
  .wheelcat b { font-size:12.5px; }
  .tagrow { display:flex; flex-wrap:wrap; gap:6px; margin:2px 0 4px; min-height:0; }
  .tagrow:empty { display:none; }
  .tag { display:inline-flex; align-items:center; gap:5px; background:var(--surface);
         border:1px solid var(--border); border-radius:999px; padding:3px 5px 3px 10px;
         font-size:12px; color:var(--ink-2); }
  .tag button { border:0; background:none; color:var(--muted); font:inherit; cursor:pointer; padding:0 4px; line-height:1; }
  .tag button:hover { color:var(--danger); }
  .rbwarn { margin:10px 0 2px; display:flex; flex-direction:column; gap:6px; }
  .rbwarn:empty { display:none; }
  .rbwarn-item { border-radius:8px; padding:8px 11px; font-size:12.5px; line-height:1.5; border:1px solid; background:var(--surface); }
  .rbwarn-item.danger { border-color:var(--danger); }
  .rbwarn-item.caution { border-color:var(--olive); }
  .rbwarn-item b { display:block; font-size:12.5px; margin-bottom:2px; }
  .rbwarn-item.danger b { color:var(--danger); }
  .rbwarn-item.caution b { color:var(--olive); }
  .ahint { font-size:12px; color:var(--muted); margin-top:8px; }
</style></head><body>
<header><div class="brandmark" aria-hidden="true">__BRAND_INITIALS__</div>
  <div><div class="wm">__BRAND_NAME__</div><div class="wm2">Recipe Explorer</div></div></header>
<a class="back" href="/">&larr; Back to the dashboard</a>
<p class="lede">Sketch out mead recipes before you brew — pick a style, yeast, and
flavor/aroma additions, save as many drafts as you like, and copy a finished one
into a Tilt's batch notes when you're ready. Nothing here touches a live batch
until you copy it over.</p>

<div id="listView">
  <div class="listhead"><h2 style="font-size:14px">Saved recipes</h2>
    <button class="btn primary" id="newBtn" type="button">+ New recipe</button></div>
  <div id="draftCards" class="cards"></div>
  <div id="emptyDrafts" class="tip" hidden>No saved recipes yet — click "+ New recipe" to start one.</div>
</div>

<div id="editView" hidden>
  <button class="link" id="backBtn" type="button" style="padding-left:0">&larr; All recipes</button>
  <div class="fld" style="margin-top:8px"><label for="r_name">Recipe name</label>
    <input id="r_name" placeholder="e.g. Cranberry Sage Sack Mead"></div>

  <div class="rbh">Target style</div>
  <div class="pillrow" id="abvTiers"></div>
  <div class="frow">
    <div class="fld"><label for="r_vol">Batch volume (US gal)</label>
      <input id="r_vol" inputmode="decimal" value="5"></div>
    <div class="fld"><label for="r_abv">Target ABV %</label>
      <input id="r_abv" inputmode="decimal" value="12"></div>
    <div class="fld"><label for="r_sweet">Sweetness</label><select id="r_sweet"></select></div>
    <div class="fld"><label for="r_honey">Honey variety</label>
      <select id="r_honey">
        <option value="32">Dark / high-moisture (~32 PPG)</option>
        <option value="35" selected>Wildflower / clover (~35 PPG)</option>
        <option value="37">Light / dry (~37 PPG)</option>
      </select></div>
  </div>
  <div class="cout" id="r_out"></div>
  <div class="rbwarn" id="r_warn"></div>

  <div class="rbh">Yeast strain <span class="rbhint">highlighted = handles your target ABV</span></div>
  <div class="pillrow" id="r_yeastPicks"></div>
  <div class="cout" id="r_yeastInfo" hidden></div>
  <div class="fld" style="margin-top:8px"><label for="r_yeast_name">Yeast (pick above, or type your own)</label>
    <input id="r_yeast_name"></div>

  <div class="rbh">Flavor wheel <span class="rbhint">click ingredients to tag this recipe</span></div>
  <div id="r_flavorWheel"></div>
  <div class="tagrow" id="r_flavorTags"></div>

  <div class="rbh">Aroma wheel</div>
  <div id="r_odorWheel"></div>
  <div class="tagrow" id="r_odorTags"></div>

  <div class="fld" style="margin-top:12px"><label for="r_notes">Notes</label>
    <textarea id="r_notes" placeholder="Process notes, nutrient schedule, aging plan…"></textarea></div>

  <div class="mbtns">
    <button class="btn primary" id="r_save" type="button">Save recipe</button>
    <button class="btn" id="r_copy" type="button">Copy summary</button>
    <button class="btn" id="r_dup" type="button" hidden>Duplicate</button>
    <span class="spacer"></span>
    <button class="btn danger" id="r_delete" type="button" hidden>Delete recipe</button>
  </div>
  <div class="ahint" id="r_status"></div>
</div>

<footer id="footerTag">__BRAND_TAGLINE__</footer>
<script>
"use strict";
const $ = id => document.getElementById(id);
const RECIPE_DATA = __RECIPE_DATA__;
const RM = RECIPE_DATA.recipeMechanics;
let WHEELS = { flavorWheel: RECIPE_DATA.flavorWheel, odorWheel: RECIPE_DATA.odorWheel };
let FLAVOR_NAMES, ODOR_NAMES;
let flavorSel = new Set(), odorSel = new Set();
let currentId = null;
let drafts = [];

function wheelIngredients(wheel) {
  const m = new Map();
  for (const cat of wheel.categories) for (const ing of cat.ingredients) m.set(ing.id, ing.name);
  return m;
}
function applyWheels(w) {
  WHEELS = w;
  FLAVOR_NAMES = wheelIngredients(w.flavorWheel);
  ODOR_NAMES = wheelIngredients(w.odorWheel);
}
applyWheels(WHEELS);
async function loadWheels() {
  try { applyWheels(await (await fetch("/api/recipe")).json()); }
  catch (e) { /* keep whatever was already loaded */ }
}

/* ---------- mead calculator + yeast picker (same math as the batch editor) ---------- */
function pickYeast(abv) {
  const cands = RM.yeastStrains.filter(y => y.abvTolerance >= abv)
                                .sort((a, b) => a.abvTolerance - b.abvTolerance);
  return cands.length ? cands[0]
    : { name: "none of the usual strains", abvTolerance: 18,
        profile: "above typical yeast tolerance (~18%) — step-feed or fortify instead." };
}
function calc() {
  const vol = parseFloat($("r_vol").value), abv = parseFloat($("r_abv").value);
  const fg = parseFloat($("r_sweet").value), ppg = parseFloat($("r_honey").value);
  const out = $("r_out"); out.textContent = "";
  syncAbvTiers(); syncYeastPicks(); refreshWarnings();
  if (!(vol > 0) || !(abv > 0)) { out.textContent = "Enter a batch volume and target ABV."; return null; }
  const og = fg + abv / 131.25;
  const lbs = (og - 1) * 1000 * vol / ppg;
  const water = Math.max(vol - lbs / 12, 0);
  const y = pickYeast(abv);
  const big = document.createElement("div"); big.className = "big";
  big.textContent = lbs.toFixed(1) + " lb honey";
  out.append(big);
  const line = (label, val) => { const d = document.createElement("div");
    const b = document.createElement("b"); b.textContent = val;
    d.append(label + ": ", b); out.append(d); };
  line("(also", (lbs * 0.4536).toFixed(2) + " kg / " + (lbs * 16).toFixed(0) + " oz)");
  line("Est. OG", og.toFixed(3));
  line("Target FG", fg.toFixed(3));
  line("Water to add", water.toFixed(1) + " US gal");
  line("Yeast suggestion", y.name + " (to ~" + y.abvTolerance + "% ABV)");
  return { og, fg, lbs, water, vol, abv, ppg };
}
for (const id of ["r_vol", "r_abv", "r_sweet", "r_honey"]) $(id).addEventListener("input", calc);

function buildAbvTiers() {
  const row = $("abvTiers"); row.textContent = "";
  for (const t of RM.abvTargets) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "pill"; b.title = t.description; b.dataset.id = t.id;
    b.textContent = t.label + " (" + t.range + ")";
    b.addEventListener("click", () => { $("r_abv").value = Math.round((t.minAbv + t.maxAbv) / 2); calc(); });
    row.append(b);
  }
}
function syncAbvTiers() {
  const abv = parseFloat($("r_abv").value);
  for (const b of $("abvTiers").children) {
    const t = RM.abvTargets.find(x => x.id === b.dataset.id);
    b.setAttribute("aria-pressed", !!(t && abv >= t.minAbv && abv <= t.maxAbv));
  }
}
function buildSweetness() {
  const sel = $("r_sweet"); sel.textContent = "";
  RM.sweetnessLevels.forEach((s, i) => {
    const o = document.createElement("option");
    o.value = s.fg; o.textContent = s.label + " (" + s.fgRange + ")"; o.title = s.description;
    if (i === 1) o.selected = true;
    sel.append(o);
  });
}
function buildYeastPicks() {
  const row = $("r_yeastPicks"); row.textContent = "";
  for (const y of RM.yeastStrains) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "pill"; b.dataset.id = y.id;
    b.textContent = y.name + " (to ~" + y.abvTolerance + "%)";
    b.addEventListener("click", () => { $("r_yeast_name").value = y.name; showYeastInfo(y); syncYeastPicks(); refreshWarnings(); });
    row.append(b);
  }
}
function syncYeastPicks() {
  const abv = parseFloat($("r_abv").value);
  const cur = $("r_yeast_name").value.trim();
  for (const b of $("r_yeastPicks").children) {
    const y = RM.yeastStrains.find(x => x.id === b.dataset.id);
    b.classList.toggle("rec", abv > 0 && y.abvTolerance >= abv);
    b.setAttribute("aria-pressed", cur === y.name);
  }
}
function showYeastInfo(y) {
  const box = $("r_yeastInfo"); box.hidden = false; box.textContent = "";
  const head = document.createElement("div"); const b = document.createElement("b");
  b.textContent = y.name + " — tolerance ~" + y.abvTolerance + "% ABV";
  head.append(b); box.append(head);
  const p = document.createElement("div"); p.textContent = y.profile; box.append(p);
}
$("r_yeast_name").addEventListener("input", () => { syncYeastPicks(); refreshWarnings(); });

/* ---------- flavor & aroma wheels ---------- */
function wheelBlock(container, wheel, sel, onChange) {
  container.textContent = "";
  for (const cat of wheel.categories) {
    const head = document.createElement("div"); head.className = "wheelcat";
    const dot = document.createElement("span"); dot.className = "dot"; dot.style.background = cat.hex;
    const b = document.createElement("b"); b.textContent = cat.name;
    head.append(dot, b); container.append(head);
    const row = document.createElement("div"); row.className = "pillrow";
    for (const ing of cat.ingredients) {
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "pill"; btn.title = ing.role; btn.textContent = ing.name;
      btn.setAttribute("aria-pressed", sel.has(ing.id));
      btn.addEventListener("click", () => {
        if (sel.has(ing.id)) sel.delete(ing.id); else sel.add(ing.id);
        onChange();
      });
      row.append(btn);
    }
    container.append(row);
  }
}
function renderTags(container, sel, names, onChange) {
  container.textContent = "";
  for (const id of sel) {
    const t = document.createElement("span"); t.className = "tag";
    t.append(document.createTextNode(names.get(id) || id));
    const x = document.createElement("button"); x.type = "button"; x.textContent = "×";
    x.setAttribute("aria-label", "Remove");
    x.addEventListener("click", () => { sel.delete(id); onChange(); });
    t.append(x); container.append(t);
  }
}
function refreshFlavor() {
  wheelBlock($("r_flavorWheel"), WHEELS.flavorWheel, flavorSel, refreshFlavor);
  renderTags($("r_flavorTags"), flavorSel, FLAVOR_NAMES, refreshFlavor);
  refreshWarnings();
}
function refreshOdor() {
  wheelBlock($("r_odorWheel"), WHEELS.odorWheel, odorSel, refreshOdor);
  renderTags($("r_odorTags"), odorSel, ODOR_NAMES, refreshOdor);
  refreshWarnings();
}

/* ---------- recipe warnings (same checks as the batch editor) ---------- */
function isIntense(role) {
  const r = role.toLowerCase();
  return ["intense", "heavy", "aggressive", "micro-dosing", "extreme"].some(w => r.includes(w));
}
const HEAT_IDS = new Set(["carolina_reaper"]);
const BITTER_IDS = new Set(["dark_roast_coffee", "sarsaparilla_root"]);
function computeWarnings() {
  const out = [];
  const abv = parseFloat($("r_abv").value);
  const fg = parseFloat($("r_sweet").value);
  const sweetOpt = $("r_sweet").selectedOptions[0];
  const sweetIdx = sweetOpt ? [...$("r_sweet").options].indexOf(sweetOpt) : -1;
  const sweetId = sweetIdx >= 0 && RM.sweetnessLevels[sweetIdx] ? RM.sweetnessLevels[sweetIdx].id : null;

  if (abv > 0) {
    const curName = $("r_yeast_name").value.trim();
    const matched = RM.yeastStrains.find(y => y.name === curName);
    if (matched && matched.abvTolerance < abv) {
      const covers = RM.yeastStrains.filter(y => y.abvTolerance >= abv).map(y => y.name);
      out.push({ level: "danger", title: "Yeast may not reach your target ABV",
        detail: matched.name + " typically tops out around " + matched.abvTolerance +
          "% ABV, below your " + abv + "% target — fermentation risks stalling short. " +
          (covers.length ? "Consider " + covers.join(" or ") + " instead."
                         : "None of these five reliably cover " + abv +
                           "% — step-feed the honey in stages or fortify after fermentation.") });
    } else if (!matched && abv > 18) {
      out.push({ level: "danger", title: "Target ABV exceeds typical yeast tolerance",
        detail: abv + "% is above what any of these five strains reliably ferment to (~18% max) — " +
          "expect a stuck fermentation unless you step-feed the honey in stages or fortify after the fact." });
    }
  }
  if (abv > 0 && !isNaN(fg)) {
    const og = fg + abv / 131.25;
    if (og >= 1.130) {
      out.push({ level: "danger", title: "Very high starting gravity (est. OG " + og.toFixed(3) + ")",
        detail: "Above roughly 1.130 OG, osmotic stress can stall even a tolerant yeast. Rehydrate " +
          "with a stress protectant (e.g. Go-Ferm) and add the honey in 2–3 stages (TOSNA-style " +
          "staggered nutrient additions) instead of pitching the full gravity at once." });
    }
  }
  const heatPicked = [...flavorSel].filter(id => HEAT_IDS.has(id)).map(id => FLAVOR_NAMES.get(id));
  if (heatPicked.length && sweetId === "bone_dry") {
    out.push({ level: "caution", title: "Capsaicin heat with a bone-dry finish",
      detail: heatPicked.join(" and ") + "'s heat is usually balanced by residual sweetness — " +
        "fermented fully dry, the capsaicin can taste harsh and one-dimensional. Consider " +
        "Semi-Sweet or Sweet/Dessert, or dose the pepper very conservatively." });
  }
  const bitterPicked = [...flavorSel].filter(id => BITTER_IDS.has(id)).map(id => FLAVOR_NAMES.get(id));
  if (bitterPicked.length && sweetId === "bone_dry") {
    out.push({ level: "caution", title: "Heavy roasted bitterness with a bone-dry finish",
      detail: bitterPicked.join(" and ") + " lean hard on roasty bitterness, which a bone-dry mead " +
        "has no residual sugar to round out — expect a sharp, astringent finish. Semi-Sweet or " +
        "Sweet/Dessert usually balances this better." });
  }
  const intenseNames = new Map(WHEELS.flavorWheel.categories.flatMap(c => c.ingredients)
    .filter(ing => isIntense(ing.role)).map(ing => [ing.id, ing.name]));
  const stacked = [...flavorSel].filter(id => intenseNames.has(id)).map(id => intenseNames.get(id));
  if (stacked.length >= 2) {
    out.push({ level: "caution", title: "Several high-intensity additions at once",
      detail: stacked.join(", ") + " are each called out as intense or heavy on their own. Combined " +
        "at full rates they risk a muddled, overly sharp or astringent mead — consider picking one as " +
        "the lead flavor and dropping or reducing the rest." });
  }
  return out;
}
function refreshWarnings() {
  const box = $("r_warn"); if (!box) return;
  box.textContent = "";
  for (const w of computeWarnings()) {
    const d = document.createElement("div"); d.className = "rbwarn-item " + w.level;
    const b = document.createElement("b"); b.textContent = "⚠ " + w.title;
    const p = document.createElement("div"); p.textContent = w.detail;
    d.append(b, p); box.append(d);
  }
}

/* ---------- draft library ---------- */
async function loadDrafts() {
  try { const r = await fetch("/api/drafts"); const j = await r.json(); drafts = j.drafts || []; }
  catch (e) { drafts = []; }
  renderDraftList();
}
function summaryLine(d) {
  const bits = [];
  if (d.target_abv) bits.push(d.target_abv + "% ABV");
  const sw = RM.sweetnessLevels.find(s => Math.abs(s.fg - (d.sweetness_fg ?? -1)) < 0.0005);
  if (sw) bits.push(sw.label);
  if (d.yeast) bits.push(d.yeast);
  return bits.join(" · ") || "No details yet";
}
function renderDraftList() {
  const host = $("draftCards"); host.textContent = "";
  $("emptyDrafts").hidden = drafts.length > 0;
  for (const d of drafts) {
    const c = document.createElement("div"); c.className = "dcard";
    const name = document.createElement("div"); name.className = "dname"; name.textContent = d.name || "Untitled recipe";
    const meta = document.createElement("div"); meta.className = "dmeta"; meta.textContent = summaryLine(d);
    const nTags = (d.flavor_ids || []).length + (d.odor_ids || []).length;
    const tags = document.createElement("div"); tags.className = "dtags";
    tags.textContent = nTags ? nTags + " flavor/aroma pick" + (nTags === 1 ? "" : "s") : "No flavor/aroma picks yet";
    c.append(name, meta, tags);
    const row = document.createElement("div"); row.className = "drow";
    const dup = document.createElement("button"); dup.className = "btn"; dup.type = "button"; dup.textContent = "Duplicate";
    dup.addEventListener("click", async ev => {
      ev.stopPropagation();
      const r = await fetch("/api/drafts", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "duplicate", id: d.id }) });
      if (r.ok) loadDrafts();
    });
    const del = document.createElement("button"); del.className = "btn danger"; del.type = "button"; del.textContent = "Delete";
    del.addEventListener("click", async ev => {
      ev.stopPropagation();
      if (!confirm('Delete "' + (d.name || "this recipe") + '"? This cannot be undone.')) return;
      const r = await fetch("/api/drafts", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", id: d.id }) });
      if (r.ok) loadDrafts();
    });
    row.append(dup, del); c.append(row);
    c.addEventListener("click", () => openEditor(d));
    host.append(c);
  }
}
function openEditor(d) {
  currentId = d ? d.id : null;
  $("r_name").value = d?.name || "";
  $("r_vol").value = d?.volume ?? 5;
  $("r_abv").value = d?.target_abv ?? 12;
  $("r_honey").value = d?.honey_ppg ?? 35;
  $("r_yeast_name").value = d?.yeast || "";
  $("r_notes").value = d?.notes || "";
  flavorSel = new Set(d?.flavor_ids || []);
  odorSel = new Set(d?.odor_ids || []);
  $("r_yeastInfo").hidden = true;
  $("r_status").textContent = "";
  $("r_dup").hidden = !d;
  $("r_delete").hidden = !d;
  loadWheels().then(() => {
    if (d && d.sweetness_fg != null) {
      const opt = [...$("r_sweet").options].find(o => Math.abs(parseFloat(o.value) - d.sweetness_fg) < 0.0005);
      if (opt) $("r_sweet").value = opt.value;
    }
    refreshFlavor(); refreshOdor(); calc();
  });
  $("listView").hidden = true; $("editView").hidden = false;
  $("r_name").focus();
}
$("newBtn").addEventListener("click", () => openEditor(null));
$("backBtn").addEventListener("click", () => { $("editView").hidden = true; $("listView").hidden = false; loadDrafts(); });

function draftFields() {
  return {
    name: $("r_name").value, target_abv: $("r_abv").value, sweetness_fg: $("r_sweet").value,
    volume: $("r_vol").value, honey_ppg: $("r_honey").value, yeast: $("r_yeast_name").value,
    notes: $("r_notes").value, flavor_ids: [...flavorSel], odor_ids: [...odorSel],
  };
}
$("r_save").addEventListener("click", async () => {
  try {
    const r = await fetch("/api/drafts", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "save", id: currentId, fields: draftFields() }) });
    const j = await r.json();
    if (!r.ok) throw new Error();
    currentId = j.draft.id;
    $("r_dup").hidden = false; $("r_delete").hidden = false;
    $("r_status").textContent = "Saved.";
  } catch (e) { $("r_status").textContent = "Could not save — is the dashboard server still running?"; }
});
$("r_delete").addEventListener("click", async () => {
  if (!currentId || !confirm('Delete "' + ($("r_name").value || "this recipe") + '"? This cannot be undone.')) return;
  await fetch("/api/drafts", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "delete", id: currentId }) });
  $("editView").hidden = true; $("listView").hidden = false; loadDrafts();
});
$("r_dup").addEventListener("click", async () => {
  if (!currentId) return;
  const r = await fetch("/api/drafts", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "duplicate", id: currentId }) });
  const j = await r.json();
  if (r.ok) openEditor(j.draft);
});
$("r_copy").addEventListener("click", async () => {
  const r = calc();
  const lines = [
    "Recipe: " + ($("r_name").value || "Untitled recipe"),
    "Target: " + $("r_abv").value + "% ABV, " + $("r_sweet").selectedOptions[0].textContent + ", " + $("r_vol").value + " gal",
  ];
  if (r) lines.push("Mead calc: " + r.lbs.toFixed(1) + " lb honey (" + r.ppg + " PPG) + " +
    r.water.toFixed(1) + " gal water → OG " + r.og.toFixed(3) + ", FG " + r.fg.toFixed(3) + ".");
  if ($("r_yeast_name").value.trim()) lines.push("Yeast: " + $("r_yeast_name").value.trim());
  if (flavorSel.size) lines.push("Flavor picks: " + [...flavorSel].map(id => FLAVOR_NAMES.get(id)).join(", ") + ".");
  if (odorSel.size) lines.push("Aroma picks: " + [...odorSel].map(id => ODOR_NAMES.get(id)).join(", ") + ".");
  if ($("r_notes").value.trim()) lines.push("Notes: " + $("r_notes").value.trim());
  const text = lines.join("\n");
  try { await navigator.clipboard.writeText(text); $("r_status").textContent = "Copied — paste into a batch's notes when you're ready to brew."; }
  catch (e) { $("r_status").textContent = "Couldn't access the clipboard — here it is to copy by hand:\n\n" + text; }
});

buildAbvTiers(); buildSweetness(); buildYeastPicks();
loadWheels();
loadDrafts();
</script>
</body></html>
"""


# ----------------------------------------------------------------------------
# Generic favicon -- a tiny inline SVG, base64-encoded (NOT the same thing as
# "brand artwork": this is a fixed, generic placemark, not a copied logo, and
# it's base64 rather than URL-percent-encoded specifically so it contains no
# literal "%" characters -- those would otherwise corrupt REPORT_TMPL's own
# %-based substitution below). The header/report "brandmark" badge (the
# circle with the brand's initials, see .brandmark CSS + _initials()) is
# what's actually configurable, via Admin > Branding.
# ----------------------------------------------------------------------------

_FAVICON_DATAURI = (
    "data:image/svg+xml;base64,"
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2"
    "NCI+PGNpcmNsZSBjeD0iMzIiIGN5PSIzMiIgcj0iMzAiIGZpbGw9IiMyNDFkMTIiLz48dGV4dCB4"
    "PSIzMiIgeT0iNDMiIGZvbnQtZmFtaWx5PSJHZW9yZ2lhLHNlcmlmIiBmb250LXNpemU9IjMwIiBm"
    "b250LXdlaWdodD0iNzAwIiBmaWxsPSIjZTdkY2MzIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIj5UPC90"
    "ZXh0Pjwvc3ZnPg=="
)


def _inject(page: str) -> str:
    return (page
            .replace("__FAVICON__", _FAVICON_DATAURI)
            .replace("__RECIPE_DATA__", json.dumps(RECIPE_DATA)))

PAGE = _inject(PAGE)
REPORT_TMPL = _inject(REPORT_TMPL)
GUIDE = _inject(GUIDE)
RECIPES_TMPL = _inject(RECIPES_TMPL)


def main():
    ap = argparse.ArgumentParser(description="HTTP dashboard for Tilt JSONL logs.")
    ap.add_argument("--logfile", default="/var/log/tilt/tilt.jsonl")
    ap.add_argument("--batchfile", default=None,
                    help="Where batch metadata is stored "
                         "(default: batches.json next to the log file)")
    ap.add_argument("--settingsfile", default=None,
                    help="Logger settings file shared with tilt_logger.py "
                         "(default: logger-settings.json next to the log file)")
    ap.add_argument("--brandfile", default=None,
                    help="Where the dashboard's display name/tagline (set "
                         "from Admin > Branding) are stored "
                         "(default: brand.json next to the log file)")
    ap.add_argument("--recipefile", default=None,
                    help="Where edited flavor/odor wheel data is stored "
                         "(default: recipe-data.json next to the log file)")
    ap.add_argument("--draftsfile", default=None,
                    help="Where saved /recipes explorer drafts are stored "
                         "(default: recipe-drafts.json next to the log file)")
    ap.add_argument("--stagesfile", default=None,
                    help="Where the admin-curated fermentation stage list is "
                         "stored (default: stages.json next to the log file)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0",
                    help="Bind address (default all interfaces, so other "
                         "computers on the network can connect)")
    ap.add_argument("--allow-updates", action="store_true",
                    help="Enable installing new tilt_dashboard.py / "
                         "tilt_logger.py versions from the Admin page. Anyone "
                         "on the network can then push code — keep the "
                         "dashboard on a trusted LAN.")
    args = ap.parse_args()

    logdir = os.path.dirname(os.path.abspath(args.logfile))
    Handler.cache = LogCache(args.logfile)
    Handler.store = BatchStore(args.batchfile or os.path.join(logdir, "batches.json"))
    Handler.settings = SettingsStore(args.settingsfile
                                     or os.path.join(logdir, "logger-settings.json"))
    Handler.brand = BrandStore(args.brandfile
                               or os.path.join(logdir, "brand.json"))
    Handler.recipe = RecipeStore(args.recipefile
                                 or os.path.join(logdir, "recipe-data.json"),
                                 {"flavorWheel": RECIPE_DATA["flavorWheel"],
                                  "odorWheel": RECIPE_DATA["odorWheel"]})
    Handler.drafts = DraftStore(args.draftsfile
                                or os.path.join(logdir, "recipe-drafts.json"))
    Handler.stages = StageStore(args.stagesfile
                                or os.path.join(logdir, "stages.json"))
    Handler.allow_updates = args.allow_updates
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving Tilt dashboard on http://{args.host}:{args.port}/ "
          f"(log: {args.logfile})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
