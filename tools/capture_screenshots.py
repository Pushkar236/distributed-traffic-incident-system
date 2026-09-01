"""Capture real screenshots of the running dashboard for the presentation.

    python run.py                      (in another terminal)
    python tools/capture_screenshots.py

Uses Playwright (Chromium) with fake camera/microphone devices so the optional
WebRTC audio/video path can be exercised without hardware. Output: docs/screenshots/
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
BASE = "http://localhost:8000"


async def wait_text(page, selector: str, text: str, timeout: int = 15000) -> None:
    await page.wait_for_function(
        "([sel, txt]) => { const el = document.querySelector(sel); return el && el.textContent.includes(txt); }",
        arg=[selector, text], timeout=timeout)


async def shot(page, name: str, selector: str | None = None, full: bool = False) -> None:
    path = OUT / name
    await page.evaluate("document.getElementById('toast').hidden = true")
    if selector:
        await page.locator(selector).screenshot(path=str(path))
    else:
        await page.screenshot(path=str(path), full_page=full)
    print("saved", path.relative_to(ROOT))


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        await c.post("/api/reset")
        # make sure every node is online before we start
        for nid in ("PUNE-TR-01", "MUM-TR-01", "NSK-TR-01"):
            await c.post(f"/api/nodes/{nid}/recover")

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1.5)
        await ctx.grant_permissions(["camera", "microphone"], origin=BASE)
        page = await ctx.new_page()
        await page.goto(BASE)
        await wait_text(page, "#ws-status", "LIVE")
        await page.wait_for_function("document.querySelectorAll('#node-table tbody tr').length === 3")
        await page.wait_for_function("document.querySelectorAll('#incident-table tbody tr').length >= 3")
        await asyncio.sleep(2.5)   # let a snapshot + heartbeat timestamps arrive

        # 1. main dashboard
        await shot(page, "01_dashboard.png", full=True)
        # 2. network status + diagram
        await shot(page, "02_network_status.png", "#panel-network")
        await shot(page, "02b_network_diagram.png", "#panel-diagram")

        # 3. incident creation (Pune, High -> local PT-02 + P2P MT-03 from Mumbai)
        await page.select_option("#f-node", "PUNE-TR-01")
        await page.select_option("#f-type", "Accident")
        await page.fill("#f-location", "Pune - Mumbai-Bengaluru Hwy, km 42")
        await page.select_option("#f-severity", "High")
        await page.fill("#f-description", "Multi-vehicle accident, two lanes blocked")
        await page.click("#btn-report")
        await wait_text(page, "#trace-list", "unit(s) assigned")
        await asyncio.sleep(2.5)   # let the incident table refresh through the stream
        await shot(page, "03_incident_creation.png", "#panel-create")
        await shot(page, "03b_live_incidents.png", "#panel-incidents")

        # 4. distributed communication log (P2P filter and all)
        await shot(page, "04_communication_log.png", "#panel-log")
        await page.click("#log-filters .chip[data-filter='P2P']")
        await asyncio.sleep(0.3)
        await shot(page, "04b_communication_log_p2p.png", "#panel-log")
        await page.click("#log-filters .chip[data-filter='ALL']")

        # 7. traffic status
        await shot(page, "07_traffic_status.png", "#panel-traffic")

        # 5. fault tolerance: fail Pune, report another Pune incident -> redirected to Mumbai
        await page.click("button[data-fail='PUNE-TR-01']")
        await wait_text(page, "#fault-status", "Pune: OFFLINE")
        await asyncio.sleep(1.0)
        await shot(page, "05_fault_tolerance_controls.png", "#panel-fault")
        await shot(page, "05b_network_status_offline.png", "#panel-network")
        await page.select_option("#f-node", "PUNE-TR-01")
        await page.select_option("#f-type", "Road Blockage")
        await page.fill("#f-location", "Pune - Chandni Chowk flyover")
        await page.select_option("#f-severity", "High")
        await page.fill("#f-description", "Debris across carriageway after landslide")
        await page.click("#btn-report")
        await wait_text(page, "#trace-list", "redirected")
        await asyncio.sleep(2.5)
        await shot(page, "05c_failover_trace.png", "#panel-create")
        await shot(page, "05d_failover_log.png", "#panel-log")
        await shot(page, "05e_dashboard_failover.png", full=True)
        await page.click("button[data-recover='PUNE-TR-01']")
        await wait_text(page, "#fault-status", "Pune: ONLINE")
        await asyncio.sleep(1.5)
        await shot(page, "05f_recovery_log.png", "#panel-log")

        # 6. WebRTC: second tab answers, data channel + fake camera
        page2 = await ctx.new_page()
        await page2.goto(BASE)
        await wait_text(page2, "#ws-status", "LIVE")
        await page.bring_to_front()
        await page.click("#btn-rtc-media")             # fake camera on the caller side
        await asyncio.sleep(1.0)
        await page.click("#btn-rtc-connect")
        await wait_text(page, "#rtc-state", "OPEN", 20000)
        await wait_text(page2, "#rtc-state", "OPEN", 20000)
        await page.fill("#rtc-input", "Ambulance MT-03 dispatched to INC-1004, ETA 12 min")
        await page.click("#btn-rtc-send")
        await page2.fill("#rtc-input", "Received. Response team clearing lane 2 at km 42.")
        await page2.click("#btn-rtc-send")
        await wait_text(page, "#rtc-log", "Response Team:")
        await wait_text(page2, "#rtc-log", "Traffic Control:")
        remote_ok = await page2.wait_for_function(
            "() => { const v = document.querySelector('#remote-video'); return v.srcObject && v.videoWidth > 0; }",
            timeout=15000)
        print("remote video rendering on peer tab:", bool(remote_ok))
        await asyncio.sleep(1.0)
        await page.locator("#panel-webrtc").scroll_into_view_if_needed()
        await shot(page, "06_webrtc_control.png", "#panel-webrtc")
        await shot(page2, "06b_webrtc_response_team.png", "#panel-webrtc")

        await browser.close()

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        await c.post("/api/reset")
    print("done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print("screenshot capture failed:", exc)
        sys.exit(1)
