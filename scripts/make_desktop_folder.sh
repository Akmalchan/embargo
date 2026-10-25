#!/usr/bin/env bash
# Make ~/Desktop/Kestrel Drawings/ for the Kestrel Mail demo: everyday-looking copies of the demo files,
# so the presenter attaches them from Finder like any engineer would. Embargo recognises drawings by
# their content (fingerprint), not their names, so the renamed copies are still caught.
# Copies only (never moves); safe to re-run. Usage: bash scripts/make_desktop_folder.sh [target dir]
set -euo pipefail
cd "$(dirname "$0")/.."
D="${1:-$HOME/Desktop/Kestrel Drawings}"
mkdir -p "$D"
cp_as() { [ -f "data/$1" ] && cp -f "data/$1" "$D/$2" || echo "missing: data/$1 (run scripts/bootstrap_box.sh first)"; }

# the controlled ones (Embargo stops these)
cp_as attacks/A01_bracket_views_only.png "KP-1023 sensor bracket (views).png"   # ITAR, label cropped -> BLOCK to Germany
cp_as attacks/A06_pn_typo.pdf            "KP-1014 rev B for PO 4471.pdf"        # part-number typo -> HELD
cp_as attacks/A11_note_to_the_ai.pdf     "Bracket notes - please read.pdf"      # hidden note to the AI -> flagged
cp_as attacks/A07_whiteout_rescan.jpg    "scan_0412.jpg"                        # whited-out + rescanned
cp_as attacks/A12_rfq_word.docx          "RFQ 4471 - bracket quote.docx"        # drawing pasted into Word -> BLOCK
cp_as attacks/A13_quote_deck.pptx        "Q4 supplier quote.pptx"               # screenshot in a deck -> BLOCK
cp_as attacks/A14_job_folder.zip         "job_4471.zip"                         # drawing inside a zip inside a zip -> BLOCK
cp_as attacks/A15_op10_program.nc        "O4471_OP10.nc"                        # CNC program naming the ITAR part -> BLOCK

# harmless commercial parts (EAR99, these go out)
cp_as outgoing/KP-1047.pdf "KP-1047 catalog part.pdf"            # servo housing -> delivered (e.g. Triton Marine)
cp_as outgoing/KP-1054.pdf "KP-1054 actuator base.pdf"
cp_as outgoing/KP-1044.pdf "KP-1044 propeller hub - Triton.pdf"
cp_as outgoing/KP-1047_op10.nc "KP-1047_OP10.nc"                  # CNC program, commercial part -> delivered

echo "Ready: $D"
ls -1 "$D"
