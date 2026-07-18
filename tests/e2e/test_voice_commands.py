"""Issue #466: EN/ES language toggle on the "What can I say?" voice cheat sheet.

The catalogue (src/voice_commands.py) is bilingual for the alarm, wake-alarm and
locator groups, so the card offers an All/EN/ES segmented toggle. This drives the
real GET /api/voice-commands (not stubbed) and checks the toggle narrows the list
by the wake word each language leads with ("Okay Nabu," = en, "Hey Mycroft," = es).
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List

from playwright.sync_api import Page, expect

_HA_BODY = {"satellites": [], "interactions": [], "voice_transcriber": False}


def _open_cheat_sheet(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    mock_api(sample_units)
    mock_energy()
    page.route(
        "**/api/ha",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HA_BODY)
        ),
    )
    page.route(
        "**/api/hyperv",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"hyperv": {"available": False}}),
        ),
    )
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#paneHome", state="visible")
    page.locator("#homeAssistantCard > summary").click()
    page.locator("#voiceCommandsCard summary").click()
    # The list is fetched on first open — the toggle only renders once the
    # bilingual catalogue has loaded.
    page.wait_for_selector('[data-testid="voice-lang-es"]')


def test_language_toggle_filters_by_pipeline(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    _open_cheat_sheet(page, base_url, sample_units, mock_api, mock_energy)
    lst = page.locator("#voiceCommandsList")

    # Default "All": both pipelines' examples are present, All is pressed.
    expect(page.locator('[data-testid="voice-lang-all"]')).to_have_attribute("aria-pressed", "true")
    expect(lst).to_contain_text("Okay Nabu,")
    expect(lst).to_contain_text("Hey Mycroft,")

    # ES: only Spanish phrasings survive; the English-only built-ins group is gone.
    page.locator('[data-testid="voice-lang-es"]').click()
    expect(page.locator('[data-testid="voice-lang-es"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator('[data-testid="voice-lang-all"]')).to_have_attribute("aria-pressed", "false")
    expect(lst).to_contain_text("Hey Mycroft,")
    expect(lst).not_to_contain_text("Okay Nabu,")

    # EN: mirror — Spanish-only grocery group disappears.
    page.locator('[data-testid="voice-lang-en"]').click()
    expect(lst).to_contain_text("Okay Nabu,")
    expect(lst).not_to_contain_text("Hey Mycroft,")


def test_alarm_group_is_bilingual_in_the_card(
    page: Page,
    base_url: str,
    sample_units: List[Dict],
    mock_api: Callable,
    mock_energy: Callable,
) -> None:
    """The alarm group now answers on both pipelines (#466) — its Spanish arm
    phrasing is reachable through the ES filter."""
    _open_cheat_sheet(page, base_url, sample_units, mock_api, mock_energy)
    page.locator('[data-testid="voice-lang-es"]').click()
    alarm = page.locator('#voiceCommandsList .voice-group[data-group-id="alarm"]')
    expect(alarm).to_contain_text("arma la alarma")
