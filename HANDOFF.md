# Hand-off: getting Embargo running on the GB10

For the teammate at the box from 10:00, or for a fresh Claude Code session on the box
("read HANDOFF.md, README.md and DEMO.md, then do the steps in order").

**What it is:** a local AI export-control officer. It blocks controlled drawings from leaving,
even with the label cropped off. The engine is done and tested on a laptop (README, RESULTS.md).
**Today's job is plugging it into the GB10, Slack and the sandbox,** not rewriting it.
Don't change the engine's rules or thresholds; they're tuned and tested.

> Disclosure: the core was built on Oct 24. Tell the organizers in the morning (DEMO.md checklist).

## 0 · Connect (5 min)

Ubuntu-based "DGX OS", ARM64. Open a terminal on the box:

```
hostname -I                         # the box's IP address
sudo systemctl enable --now ssh     # allow SSH from the laptop (if it isn't on already)
```

Laptop and box must be on the same network: venue wifi, or a phone hotspot if the venue
blocks devices from seeing each other. From the laptop: `ssh <user>@<ip>` (type the password yourself).

## 1 · Copy the code to the box (2 min)

From the laptop (skips the 460 MB Python env and local state):

```
rsync -a --exclude .venv --exclude 'data/state*' ~/IdeaProjects/export-gate/ <user>@<ip>:~/embargo/
```

No network between them? Copy the same folder from a USB stick.

## 2 · Set up (5–10 min)

On the box:

```
cd ~/embargo && bash scripts/bootstrap_box.sh
```

Good looks like: `17 passed`, `19/19 checks passed`, `leaks=0`, and "pictures rendered".
For a bigger night sweep on the GB10 (2,000 files instead of 600): `rm -rf data && SHARE_SIZE=2000 bash scripts/bootstrap_box.sh`.

## 3 · The model (do this first, it's the slow part) [verify]

```
bash scripts/start_vllm.sh          # Qwen3.6-35B-A3B NVFP4 on 127.0.0.1:8000
curl -s 127.0.0.1:8000/v1/models    # must list the model
```

- If the image or model id differs on the box, check NVIDIA's recipe and set `VLLM_IMAGE` / `EG_LLM_MODEL`.
- If NemoClaw already runs a model (Express install), point Embargo at it instead:
  `export EG_LLM_URL=<its OpenAI-compatible URL>` and `EG_LLM_MODEL=<its model name>`.
- Check the model reads images:
  `EG_READER=vlm .venv/bin/python -c "from exportgate.ingest import load; from exportgate.reader import read; r=read(load('data/outgoing/new_design_1_scan.jpg'),'vlm'); print(r.transcript_source, [c.value for c in r.clues])"`
  Good: `vision-model ['ITAR', 'USML XII(E)', 'D', 'KP-2100', 'SKYWARD-7']` (or close).
- Then the full score with the big model: `.venv/bin/python scripts/eval.py --sweep`. The numbers go in the pitch.

## 4 · Run it

```
bash scripts/run_gb10.sh            # console + Slack bot (if tokens are set) + drop-folder watcher
```

Console at `http://127.0.0.1:8790` on the box's screen. From the laptop:
`ssh -L 8790:127.0.0.1:8790 <user>@<ip>`, then open `http://127.0.0.1:8790`.

## 5 · Slack (10 min)

1. api.slack.com/apps → Create → **From a manifest** → paste `slack/manifest.yaml`.
2. Basic Information → App-Level Token, scope `connections:write` → that's `SLACK_APP_TOKEN` (xapp-…).
3. Install to workspace → Bot token → `SLACK_BOT_TOKEN` (xoxb-…).
4. Make channel `#embargo`, `/invite @Embargo`.
5. On the box: `export SLACK_BOT_TOKEN=… SLACK_APP_TOKEN=…` (in the terminal only, never in a file), then `bash scripts/run_gb10.sh`.
6. In Slack: `/gate iam marco.bellini@kestrelprecision.example`, then
   `/gate send A01_bracket_views_only.png to hans.weber@mueller-praezision.example` → a BLOCKED card.

If the wifi drops, decisions keep happening on the box and Slack messages wait in an outbox;
they're delivered when the internet is back.

## 6 · NemoClaw / OpenClaw / OpenShell [verify]

- Agent on our model: NemoClaw custom provider → endpoint `http://localhost:8000/v1`, auth none → `nemoclaw onboard`.
  If that fails by 10:45, use NemoClaw's Express default model and set `EG_LLM_URL` to it (step 3).
- Agent skill: copy `openclaw/skills/embargo/` into the agent's skills folder. The agent explains decisions; it never makes them.
  If the agent runs inside the sandbox, start the console with `EG_API_BIND=0.0.0.0` so it can reach `host.openshell.internal:8790`.
- Internet block: `nemoclaw <sandbox> policy-add --from-file policies/embargo-egress.yaml --yes`,
  then from inside the sandbox `curl https://example.com` must fail (403). Watch `openshell logs <sandbox> --tail`.
  For the console's "Try to reach the internet" button to run inside the sandbox, set
  `EG_EGRESS_CMD='openshell sandbox exec <sandbox> -- curl -sS -m 5 https://example.com'` (check the exact CLI).

## 7 · Rehearse

Follow DEMO.md twice, timed. Press ↺ (top right of the console) to reset the demo data before each run.
For the Kestrel Mail part (`http://mail.kestrel.localhost:8791`), run `bash scripts/make_desktop_folder.sh` once on the
demo machine: it makes `~/Desktop/Kestrel Drawings/` with everyday-named copies of the demo files, so you attach them
with the paperclip (the normal file dialog) or by dragging from Finder. Embargo still catches the renamed copies, by content.
Record a backup screen video of the 4 moments.

## If something breaks

| Problem | Do this |
|---|---|
| vLLM won't start / out of memory | lower `VLLM_UTIL` to 0.45; close other GPU jobs; `sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'` |
| model can't read images | demo still works: attacks A01, A02, A03, A06 are decided by fingerprint + register + PDF text, no model |
| Slack setup stuck | demo from the console (Gate tab); Slack is only the chat window |
| sandbox health check fails | restart it once; otherwise screenshot `openshell logs --tail` showing the 403 |
| console port busy | `uvicorn exportgate.server:app --port 8791` |
| anything weird with data | `rm -rf data && bash scripts/bootstrap_box.sh` regenerates everything |

## Useful paths

| Path | What |
|---|---|
| `data/attacks/` | the 10 sneak attempts for the demo |
| `data/drop/<recipient email>/` | drop a file here and it's checked automatically |
| `RESULTS.md`, `RESULTS_laptop_vlm.md` | measured numbers (laptop) |
| `/classic` | the old plain dashboard (backup UI) |
