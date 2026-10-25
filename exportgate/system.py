"""Machine meter (GPU / unified memory) and the egress test."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time

import httpx
import psutil

from . import llm, store


def meter() -> dict:
    vm = psutil.virtual_memory()
    out = {"cpu_pct": psutil.cpu_percent(interval=None), "mem_used_gb": round(vm.used / 2**30, 1),
           "mem_total_gb": round(vm.total / 2**30, 1), "mem_pct": vm.percent, "gpu": None,
           "llm": dict(llm.STATS), "ts": time.time()}
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,power.draw",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
            name, util, used, total, power = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else None
            # On GB10 memory is unified: nvidia-smi may report [N/A]; system memory above is the real number.
            out["gpu"] = {"name": name, "util_pct": num(util), "mem_used_mb": num(used), "mem_total_mb": num(total),
                          "power_w": num(power)}
        except Exception as e:  # meter must never break the dashboard
            out["gpu"] = {"error": str(e)[:120]}
    return out


def egress_test(url: str = "https://example.com") -> dict:
    """Try to reach the internet. Inside the OpenShell sandbox this must fail (CONNECT 403).

    EG_EGRESS_CMD runs the probe inside the sandbox instead of this process, e.g.
    'openshell sandbox exec my-assistant -- curl -sS -m 5 https://example.com'  [verify CLI on the box]
    """
    t0 = time.perf_counter()
    cmd = os.environ.get("EG_EGRESS_CMD")
    if cmd:
        r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=20)
        out = (r.stdout + r.stderr).strip()
        blocked = r.returncode != 0 or "403" in out or "denied" in out.lower()
        res = {"url": url, "blocked": blocked, "status": r.returncode, "via": "openshell sandbox",
               "message": ("blocked inside the sandbox: " if blocked else "REACHED from the sandbox: ") + out[-200:]}
        res["ms"] = round((time.perf_counter() - t0) * 1000)
        store.log("EGRESS_TEST", "system", "", res)
        return res
    try:
        r = httpx.get(url, timeout=4)
        res = {"url": url, "blocked": False, "status": r.status_code,
               "message": f"reached {url} (HTTP {r.status_code}): NOT sandboxed"}
    except httpx.HTTPError as e:
        res = {"url": url, "blocked": True, "status": None, "message": f"blocked: {str(e)[:160]}"}
    res["ms"] = round((time.perf_counter() - t0) * 1000)
    store.log("EGRESS_TEST", "system", "", res)
    return res
