"""Capture real screenshots of the FA-2 dashboard section for the FA-2 presentation.

    python run.py                          (in another terminal)
    python tools/capture_fa2_screenshots.py

Drives the real system through every FA-2 demo and saves panel screenshots to
docs/screenshots/fa2/ (the FA-1 screenshots in docs/screenshots/ are not touched).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots" / "fa2"
BASE = "http://127.0.0.1:8000"
NODES = ("PUNE-TR-01", "MUM-TR-01", "NSK-TR-01")


async def shot(page, name: str, selector: str) -> None:
    await page.evaluate("document.getElementById('toast').hidden = true")
    loc = page.locator(selector)
    await loc.scroll_into_view_if_needed()
    await loc.screenshot(path=str(OUT / name))
    print("saved", (OUT / name).relative_to(ROOT))


async def click_and_wait(page, action: str, settle: float = 1.5) -> None:
    """Click an FA-2 button and wait until its request finished (button re-enabled)."""
    btn = page.locator(f"[data-fa2='{action}']")
    await btn.click()
    await page.wait_for_function(f"!document.querySelector(\"[data-fa2='{action}']\").disabled", timeout=60000)
    await asyncio.sleep(settle)


async def wait_leader(c: httpx.AsyncClient, leader: str, timeout: float = 20) -> None:
    for _ in range(int(timeout / 0.5)):
        st = (await c.get("/api/fa2/state")).json()
        if st["leader"] == leader and all(st["nodes"][n].get("election", {}).get("leader") in (leader, None)
                                          for n in NODES if st["nodes"][n].get("available")):
            return
        await asyncio.sleep(0.5)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        for n in NODES:
            await c.post(f"/api/nodes/{n}/recover")
        await c.post("/api/reset")
        await c.post("/api/fa2/reset")
        await wait_leader(c, "NSK-TR-01")
        # one FA-1 incident so the event log, FaaS trigger and ledger have real content
        await c.post("/api/incidents", json={"type": "Accident", "location": "Pune - Mumbai-Bengaluru Hwy, km 42",
                                             "severity": "High", "description": "Multi-vehicle accident, two lanes blocked",
                                             "detected_by": "PUNE-TR-01"})

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1.5)
            page = await ctx.new_page()
            await page.goto(BASE)
            await page.wait_for_function("document.querySelectorAll('#fa2-node-table tbody tr').length === 3")
            await asyncio.sleep(4)

            # ---- clocks
            await click_and_wait(page, "reskew")
            await c.post("/api/fa2/clock/reskew", json={"randomize": False})
            await asyncio.sleep(2.5)
            await shot(page, "01_node_state.png", "#fa2-panel-nodes")
            await shot(page, "02a_clocks_skewed.png", "#fa2-panel-clock")
            await click_and_wait(page, "cristian")
            await shot(page, "02b_cristian.png", "#fa2-panel-clock")
            await click_and_wait(page, "reskew")
            await click_and_wait(page, "berkeley")
            await shot(page, "02c_berkeley.png", "#fa2-panel-clock")
            await shot(page, "03_beacons.png", "#fa2-panel-beacon")

            # ---- causal order + logical clocks
            await click_and_wait(page, "causal", settle=5)
            await shot(page, "04_logical_causal.png", "#fa2-panel-logical")

            # ---- election
            await page.select_option("#fa2-elect-init", "PUNE-TR-01")
            await click_and_wait(page, "bully", settle=2.5)
            await shot(page, "05a_bully.png", "#fa2-panel-election")
            await page.select_option("#fa2-elect-init", "MUM-TR-01")
            await click_and_wait(page, "ring", settle=2.5)
            await shot(page, "05b_ring.png", "#fa2-panel-election")

            # ---- leader failure -> beacons -> re-election (FA-1 fail button)
            await page.click("button[data-fail='NSK-TR-01']")
            await wait_leader(c, "MUM-TR-01")
            await asyncio.sleep(3)
            await shot(page, "06a_leader_failed_nodes.png", "#fa2-panel-nodes")
            await shot(page, "06b_leader_failed_beacons.png", "#fa2-panel-beacon")
            await shot(page, "06c_leader_failed_election.png", "#fa2-panel-election")
            await page.click("button[data-recover='NSK-TR-01']")
            await wait_leader(c, "NSK-TR-01")
            await asyncio.sleep(3)

            # ---- mutual exclusion (all four, NONE last)
            for algo in ("LODHA_KSHEMKALYANI", "RICART_AGRAWALA", "LAMPORT"):
                await page.select_option("#fa2-me-algo", algo)
                await click_and_wait(page, "mutex", settle=2.5)
                if algo == "LODHA_KSHEMKALYANI":
                    await shot(page, "07a_mutex_lk.png", "#fa2-panel-mutex")
            await page.select_option("#fa2-me-algo", "NONE")
            await click_and_wait(page, "mutex", settle=2.5)
            await shot(page, "07b_mutex_compare_none.png", "#fa2-panel-mutex")

            # ---- snapshot
            await click_and_wait(page, "snapshot", settle=2.5)
            await shot(page, "08_snapshot.png", "#fa2-panel-snapshot")

            # ---- deadlock
            await click_and_wait(page, "dl-setup", settle=2.5)
            await page.select_option("#fa2-dl-init", "MUM-TR-01")
            await click_and_wait(page, "dl-edge", settle=2.5)
            await shot(page, "09a_deadlock_detected.png", "#fa2-panel-deadlock")
            await click_and_wait(page, "dl-global", settle=1.5)
            await click_and_wait(page, "dl-resolve", settle=2.5)
            await shot(page, "09b_deadlock_resolved.png", "#fa2-panel-deadlock")
            await click_and_wait(page, "dl-reset", settle=0.5)

            # ---- distributed objects
            await page.select_option("#fa2-obj-ref", "fa2obj://PUNE-TR-01/TrafficSignal/SIG-PUN-SWARGATE")
            await page.select_option("#fa2-obj-method", "sync_with")
            await page.click("#fa2-obj-form button[type=submit]")
            await asyncio.sleep(1.5)
            await shot(page, "10_objects.png", "#fa2-panel-objects")

            # ---- DFS: report via FaaS, corrupt one replica, read with failover
            incidents = (await c.get("/api/incidents")).json()["incidents"]
            await asyncio.sleep(10.5)                       # incident dropdown refresh
            await page.select_option("#fa2-dfs-incident", incidents[0]["incident_id"])
            await click_and_wait(page, "dfs-report", settle=2.5)
            path = f"/reports/{incidents[0]['incident_id']}.txt"
            await page.click(f"[data-dfs-corrupt='{path}']")
            await asyncio.sleep(1)
            await page.click(f"[data-dfs-read='{path}']")
            await asyncio.sleep(1.5)
            await shot(page, "11_dfs_failover.png", "#fa2-panel-dfs")
            await click_and_wait(page, "dfs-repair", settle=2.5)

            # ---- serverless
            await page.click("[data-faas='congestion_advisory']")
            await asyncio.sleep(2)
            await page.click("[data-faas='congestion_advisory']")
            await asyncio.sleep(2.5)
            await shot(page, "12_faas.png", "#fa2-panel-faas")

            # ---- ledger
            await click_and_wait(page, "ledger-note", settle=1)
            await click_and_wait(page, "ledger-tamper", settle=2.5)
            await shot(page, "13a_ledger_tampered.png", "#fa2-panel-ledger")
            await click_and_wait(page, "ledger-repair", settle=4.5)
            await shot(page, "13b_ledger_repaired.png", "#fa2-panel-ledger")

            # ---- web caching
            await click_and_wait(page, "web-get", settle=0.5)
            await click_and_wait(page, "web-reval", settle=2.5)
            await shot(page, "14_web_cache.png", "#fa2-panel-web")

            # ---- case studies + FA-2 entries in the FA-1 communication log
            await shot(page, "15_case_studies.png", "#fa2-panel-cases")
            await page.click("#log-filters .chip[data-filter='FA2']")
            await asyncio.sleep(0.5)
            await shot(page, "16_comm_log_fa2.png", "#panel-log")
            await page.click("#log-filters .chip[data-filter='ALL']")
            await browser.close()

        await c.post("/api/fa2/reset")
    print("done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print("screenshot capture failed:", exc)
        sys.exit(1)
