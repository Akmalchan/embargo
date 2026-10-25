# Export Gate results

Reader mode `auto`, model `qwen3-vl:4b-instruct` at `http://127.0.0.1:11434/v1`. 13 files, 599.2s, 68 model calls. All data is fictional (scripts/make_dataset.py).

## Detection: is it export-controlled? (flagged = p >= 0.50)

| | truly controlled | not controlled |
|---|---|---|
| flagged | 8 | 0 |
| not flagged | 0 | 5 |

**Precision 1.00 · recall 1.00** · jurisdiction right on 8/8 controlled files.

## Safety: controlled data released to someone not authorized

**0 leaks** out of 5 files going to unauthorized recipients.

## Caught, by how the file was disguised

| disguise | caught |
|---|---|
| crop_views | 4/4 |
| new_design_scan | 3/3 |
| split_half | 1/1 |

## Attack set (demo)

| file | disguise | p | decision | rules |
|---|---|---|---|---|
| A01_bracket_views_only.png | crop_views | 0.964 | BLOCK | BLOCK_ITAR_NO_LICENSE;HOLD_MARKING_MISSING |
| A04_left_half.png | split_half | 0.972 | BLOCK | BLOCK_ITAR_NO_LICENSE;HOLD_MARKING_MISSING |

## Cost

3/13 files (23%) decided with **no model call** (fingerprint + register + PDF text layer). Median time per file 51284 ms.
