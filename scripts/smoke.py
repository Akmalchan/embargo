"""Drive the running server through the demo moments. python scripts/smoke.py [base_url]"""
import sys
import httpx

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
c = httpx.Client(base_url=B, timeout=600)
ENG, EO = "marco.bellini@kestrelprecision.example", "dana.whitfield@kestrelprecision.example"

c.post("/api/demo/reset")
for path, to in [("attacks/A01_bracket_views_only.png", "hans.weber@mueller-praezision.example"),
                 ("attacks/A10_crop_to_us_prime.png", "buyer@northfield-avionics.example"),
                 ("attacks/A06_pn_typo.pdf", "buyer@northfield-avionics.example"),
                 ("outgoing/new_design_1_scan.jpg", "buyer@northfield-avionics.example")]:
    card = c.post("/api/check_path", json={"path": path, "requester": ENG, "recipient": to}).json()
    print(f"\n{card['id']} {card['action']:7} p={card['p']:.3f} {card['codes']} ({card['timings']['total_ms']} ms, read: {card['reading']['source']})")
    print("  " + card["summary"])
    for e in card["ledger"]:
        print(f"   {e['weight']:+.2f}  {e['label']}")
held = [r for r in c.get("/api/requests").json() if r["status"] == "HOLD"]
if held:
    rid = held[0]["id"]
    print("\nself-approve:", c.post(f"/api/requests/{rid}/respond", json={"approver": ENG, "approve": True}).json())
    print("EO approve:  ", c.post(f"/api/requests/{rid}/respond", json={"approver": EO, "approve": True}).json())
    r = c.get(f"/api/requests/{rid}/match.png")
    print("match picture:", r.status_code, len(r.content), "bytes")
print("verify:", c.get("/api/audit/verify").json()["message"])
print("tamper:", c.post("/api/demo/tamper").json()["message"])
print("verify:", c.get("/api/audit/verify").json()["message"])
print("egress:", c.post("/api/system/egress").json()["message"][:90])
