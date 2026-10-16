"""UI tests against MOCK only: realtime reads and user edits auto-send."""

import json
import os
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

URL = os.environ.get("NX100_TEST_URL", "http://127.0.0.1:8000")
with urlopen(URL + "/api/config") as response:
    if json.load(response)["mode"] != "MOCK":
        raise RuntimeError("Refusing to test motion against REAL mode")

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1200})
    errors, moves = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "request",
        lambda request: (
            moves.append(request.post_data_json)
            if request.method == "POST" and "/api/move/cartesian" in request.url
            else None
        ),
    )
    page.goto(URL)
    expect(page.locator("#modeBadge")).to_contain_text("MOCK")
    assert page.locator("#read").count() == 0
    assert page.locator("#move").count() == 0
    expect(page.locator("#connection")).to_have_text("DISCONNECTED")
    assert not moves
    page.click("#connect")
    expect(page.locator("#connection")).to_have_class("connection connected")
    expect(page.locator("#axis-x")).to_have_value("1000.000")
    page.wait_for_timeout(1300)
    assert not moves, "CONNECT and realtime pose updates must not send motion"

    # Pausing mid-number or leaving the field must never send partial keyboard input.
    page.locator("#axis-x").fill("100")
    page.wait_for_timeout(1200)
    assert not moves, "Pausing at 100 while typing 1000 must not move the robot"
    page.locator("#axis-x").fill("1000")
    page.locator("#axis-x").press("Tab")
    page.wait_for_timeout(700)
    assert not moves, "Blur/change must not commit typed values"
    expect(page.locator("#axis-x")).to_have_value("1000")
    page.locator("#axis-x").fill("1010")
    page.wait_for_timeout(600)
    assert not moves
    page.locator("#axis-x").press("Enter")
    expect(page.locator("#motionPhase")).to_have_text("COMMAND ACCEPTED", timeout=3000)
    assert len(moves) == 1 and moves[0]["x"] == 1010
    expect(page.locator("#motionPhase")).to_have_text("MOVE COMPLETE", timeout=12000)

    # Sliders still auto-send and coalesce multiple changes into the final target.
    slider = page.locator(".axis input[type=range]").nth(1)
    slider.fill("203")
    slider.dispatch_event("input")
    page.wait_for_timeout(70)
    slider.fill("205")
    slider.dispatch_event("input")
    expect(page.locator("#axis-y")).to_have_value("205.000")
    assert len(moves) == 1, "Debounce must wait before sending"
    expect(page.locator("#motionPhase")).to_have_text("COMMAND ACCEPTED", timeout=3000)
    expect(page.locator("#axis-x")).to_be_disabled()
    assert len(moves) == 2 and moves[-1]["x"] == 1010 and moves[-1]["y"] == 205
    assert "reference_id" not in moves[-1]
    expect(page.locator("#motionPhase")).to_have_text("MOVE COMPLETE", timeout=12000)
    page.wait_for_timeout(1300)
    assert len(moves) == 2, "Completion/polling must not replay the last target"

    # Speed and mode are settings; neither creates a new position command.
    page.click("#speedNumber")
    page.locator("#speedNumber").fill("4")
    page.select_option("#motion", "MOVJ")
    assert page.locator("#speedUnit").inner_text() == "%"
    page.wait_for_timeout(600)
    assert len(moves) == 2
    page.click("#jointTab")
    expect(page.locator("#axis-s")).to_have_value("12000")
    expect(page.locator("#axis-s")).to_be_disabled()
    page.click("#cartTab")
    page.wait_for_timeout(600)
    assert len(moves) == 2, "Tab switch and realtime initialization must never send"

    # HOLD cancels a target still in debounce, with no delayed motion afterwards.
    slider = page.locator(".axis input[type=range]").first
    slider.fill("1100")
    slider.dispatch_event("input")
    page.click("#hold")
    expect(page.locator("#motionPhase")).to_have_text("HOLD ACCEPTED")
    expect(page.locator("#blockedReason")).to_contain_text("HOLD ON", timeout=4000)
    page.wait_for_timeout(700)
    assert len(moves) == 2, "HOLD must cancel pending debounce"
    page.click("#disconnect")
    expect(page.locator("#connection")).to_have_class("connection disconnected")
    page.click("#connect")
    expect(page.locator("#axis-x")).to_have_value("1000.000")
    page.wait_for_timeout(700)
    assert len(moves) == 2, "Reconnect must not replay pending changes"

    # Nudge also auto-sends. HOLD remains usable during an accepted move.
    page.locator(".axis").first.locator("button").last.click()
    expect(page.locator("#motionPhase")).to_have_text("COMMAND ACCEPTED", timeout=3000)
    assert len(moves) == 3 and moves[-1]["x"] == 1001
    page.click("#hold")
    expect(page.locator("#motionPhase")).to_have_text("HOLD ACCEPTED")
    page.click("#disconnect")
    expect(page.locator("#connection")).to_have_class("connection disconnected")
    page.click("#connect")
    expect(page.locator("#connection")).to_have_class("connection connected")

    # RESET cancels an edit; no delayed MOVE after reset or a page reload.
    page.locator("#axis-x").fill("1300")
    page.click("#reset")
    page.wait_for_timeout(700)
    assert len(moves) == 3
    page.reload()
    expect(page.locator("#axis-x")).to_have_value("1000.000")
    page.wait_for_timeout(700)
    assert len(moves) == 3
    page.locator("#rawToggle").check()
    expect(page.locator(".log-entry.raw").first).to_be_visible()
    page.locator("#rawToggle").uncheck()
    assert page.locator(".log-entry.raw").count() == 0
    evidence = Path(__file__).resolve().parents[1] / "qa"
    evidence.mkdir(exist_ok=True)
    page.locator("#toast").wait_for(state="hidden", timeout=10000)
    page.screenshot(path=str(evidence / "desktop.png"), full_page=True)
    for width, height in [(1024, 900), (390, 844)]:
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(evidence / "mobile.png"), full_page=True)
    assert not errors, errors
    page.click("#disconnect")
    expect(page.locator("#connection")).to_have_class("connection disconnected")
    browser.close()
    print(
        "Browser checks passed: keyboard waits for Enter (pause/blur never send); sliders/nudge auto-send; realtime read/no replay; running block; HOLD cancel; reconnect/reset/reload; Joint read-only; responsive; no JS errors."
    )
