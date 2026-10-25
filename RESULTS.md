# Export Gate results

Reader mode `text`, model `nvidia/Qwen3.6-35B-A3B-NVFP4` at `http://127.0.0.1:8000/v1`. 55 files, 7.3s, 0 model calls. All data is fictional (scripts/make_dataset.py).

## Detection: is it export-controlled? (flagged = p >= 0.50)

| | truly controlled | not controlled |
|---|---|---|
| flagged | 24 | 0 |
| not flagged | 4 | 27 |

**Precision 1.00 · recall 0.86** · jurisdiction right on 24/28 controlled files.

## Safety: controlled data released to someone not authorized

**0 leaks** out of 21 files going to unauthorized recipients.

## Caught, by how the file was disguised

| disguise | caught |
|---|---|
| crop_frame | 3/3 |
| crop_views | 4/4 |
| new_design_scan | 0/3 |
| plain | 7/7 |
| pn_typo | 1/1 |
| renamed | 2/2 |
| rescan | 3/3 |
| revB_unmarked | 1/1 |
| screenshot | 1/1 |
| split_half | 0/1 |
| whiteout | 1/1 |
| whiteout_rescan | 1/1 |

## Attack set (demo)

| file | disguise | p | decision | rules |
|---|---|---|---|---|
| A01_bracket_views_only.png | crop_views | 0.841 | BLOCK | HOLD_MARKINGS_UNREAD;HOLD_UNCERTAIN;BLOCK_ITAR_NO_LICENSE |
| A02_whiteout_markings.pdf | whiteout | 0.841 | BLOCK | HOLD_MARKINGS_UNREAD;HOLD_UNCERTAIN;BLOCK_ITAR_NO_LICENSE;HOLD_REQUESTER_NOT_CLEARED |
| A03_screenshot.png | screenshot | 0.841 | BLOCK | HOLD_MARKINGS_UNREAD;HOLD_UNCERTAIN;BLOCK_ITAR_NO_LICENSE |
| A04_left_half.png | split_half | 0.414 | HOLD | HOLD_MARKINGS_UNREAD |
| A05_revB_unmarked.pdf | revB_unmarked | 1.000 | BLOCK | BLOCK_ITAR_NO_LICENSE;HOLD_ALTERED_PART_NUMBER;HOLD_MARKING_MISSING |
| A06_pn_typo.pdf | pn_typo | 1.000 | BLOCK | BLOCK_ITAR_NO_LICENSE;HOLD_ALTERED_PART_NUMBER;HOLD_MARKING_MISSING |
| A07_whiteout_rescan.jpg | whiteout_rescan | 0.841 | BLOCK | HOLD_MARKINGS_UNREAD;HOLD_UNCERTAIN;BLOCK_ITAR_NO_LICENSE;HOLD_REQUESTER_NOT_CLEARED |
| A08_marked_to_sanctioned.pdf | plain | 1.000 | BLOCK | BLOCK_SANCTIONED_DESTINATION;BLOCK_ITAR_NO_LICENSE;HOLD_REQUESTER_NOT_CLEARED |
| A09_licensed_canada.pdf | plain | 1.000 | BLOCK | BLOCK_ITAR_NO_LICENSE;HOLD_REQUESTER_NOT_CLEARED |
| A10_crop_to_us_prime.png | crop_frame | 0.841 | HOLD | HOLD_MARKINGS_UNREAD;HOLD_UNCERTAIN;HOLD_REQUESTER_NOT_CLEARED |

## Cost

55/55 files (100%) decided with **no model call** (fingerprint + register + PDF text layer). Median time per file 104 ms.

## Night sweep

600 files / 600 pages in 59.9s (601.5 pages/min), 0 model calls, 100.0% settled without the model.

Planted problems found: **25/25** (leaks 3/3, unmarked copies 12/12). Flagged but not planted: 0.
