# Pictures and video

## Sign-in background (real footage)

Five clips rotate in step with the headline word. All from Pexels under the
[Pexels license](https://www.pexels.com/license/) (free, commercial use OK, no attribution required;
credited in the page footer anyway). Downloaded through each clip's official Download link,
cut to 9 s, audio removed, re-encoded with ffmpeg (~1–2.5 MB each), stored locally so they play offline.

| Word | File | Pexels clip |
|---|---|---|
| weapons blueprints | `web/static/media/lathe.mp4` | [19791092](https://www.pexels.com/video/19791092/) CNC lathe turning steel (Daniel Smyth) |
| chip designs | `web/static/media/pcb.mp4` | [6754824](https://www.pexels.com/video/6754824/) real macro shot of a circuit board and chip |
| aircraft parts | `web/static/media/sparks.mp4` | [30409130](https://www.pexels.com/video/30409130/) laser cutting with sparks (vertical clip, centre cropped) |
| drone designs | `web/static/media/drone.mp4` | [31904479](https://www.pexels.com/video/31904479/) reconnaissance drone |
| government files | `web/static/media/servers.mp4` | [7140928](https://www.pexels.com/video/7140928/) server racks |

Each has a `*_poster.jpg` still frame shown until the video loads.

## Rendered visuals (not used on the sign-in page any more)

Every picture in the console is **rendered from our own fictional data** by `scripts/make_visuals.py`
and `scripts/make_assets.py`: the machined parts are extruded from the vault drawings themselves.
No downloads, no licenses to worry about, and they work with the wifi off.

| File | What it shows | Industry slide |
|---|---|---|
| `web/static/img/v_defense.jpg` | black-anodized bracket, extruded from drawing KP-1023 | Defense (live in the demo) |
| `web/static/img/v_chips.jpg` | chip die: cell rows, routing, bond pads | Semiconductors |
| `web/static/img/v_health.jpg` | patient record, identifiers redacted, ECG trace | Healthcare |
| `web/static/img/v_biotech.jpg` | 96-well lab plate | Biotech |
| `web/static/img/v_gov.jpg` | stack of CUI-marked files | Government |
| `v_part_silver.jpg`, `v_part_gold.jpg` | two more parts in other finishes (spare) | |

Regenerate after a new dataset: `python scripts/make_visuals.py`

## Swapping in real photos (optional)

Drop a photo into `web/static/img/` **with the same file name** (JPG, ~1600×1000, landscape) and reload.
Nothing else changes. Only use photos you're cleared to use (hackathon rule 6):

- **US government photos** (Department of Defense, NASA, NIH, NIST) are generally public domain.
  Good for defense, government and lab slides. Check each photo's page for the credit line.
- **Unsplash / Pexels** photos are free to use under their own licenses.
- Avoid anything showing a real company's logo or product, or real people's records.
