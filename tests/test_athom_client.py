"""Unit tests for the Athom per-circuit client (issue #25).

Pure logic only — the SSE parser, the snapshot→state mapping, the channel-count
derivation and the discovery cache policy. Nothing here touches the network.
"""

from __future__ import annotations

import asyncio

import pytest

from src import athom_client as ac
from src.athom_client import MeterEndpoint


def _endpoint(channels: int = 6) -> MeterEndpoint:
    return MeterEndpoint(
        meter_id="AA:BB:CC:DD:EE:01",
        host="192.0.2.73",
        name="Athom Energy Monitor ddee01",
        model="China Athom Technology.Athom Energy Monitor(6 Channels)",
        channel_count=channels,
    )


# A trimmed but byte-faithful excerpt of what the real meter streams on connect:
# a ping banner, a couple of device sensors, one live channel, one idle channel,
# and a log frame that must be ignored.
SNAPSHOT = (
    'event: ping\ndata: {"title":"Athom Energy Monitor ddee01"}\n\n'
    'event: state\ndata: {"id":"sensor-voltage","value":239.3847,"state":"239.4 V"}\n\n'
    'event: state\ndata: {"id":"sensor-wifi_signal_db","value":-68,"state":"-68 dBm"}\n\n'
    'event: state\ndata: {"id":"sensor-total_power","value":291.47,"state":"291 W"}\n\n'
    'event: log\ndata: [D][sensor:131]: sending state\n\n'
    'event: state\ndata: {"id":"sensor-power_1","value":-291.539,"state":"-292 W"}\n\n'
    'event: state\ndata: {"id":"sensor-current_1","value":1.8059,"state":"1.806 A"}\n\n'
    'event: state\ndata: {"id":"sensor-energy_1","value":6.879,"state":"6.879 kWh"}\n\n'
    'event: state\ndata: {"id":"sensor-power_2","value":0,"state":"0 W"}\n\n'
)


class TestParseEvents:
    def test_collects_state_frames_and_ignores_ping_and_log(self) -> None:
        values = ac._parse_events(SNAPSHOT)
        assert values["sensor-voltage"] == pytest.approx(239.3847)
        assert values["sensor-power_1"] == pytest.approx(-291.539)
        # The log frame carried no JSON id and must not have landed anywhere.
        assert all(key.startswith("sensor-") for key in values)

    def test_tolerates_crlf_and_malformed_frames(self) -> None:
        raw = SNAPSHOT.replace("\n", "\r\n") + "event: state\ndata: {not json}\n\n"
        values = ac._parse_events(raw)
        assert values["sensor-power_2"] == 0

    def test_later_frame_wins(self) -> None:
        raw = SNAPSHOT + 'event: state\ndata: {"id":"sensor-power_1","value":-42}\n\n'
        assert ac._parse_events(raw)["sensor-power_1"] == pytest.approx(-42)


class TestAsFloat:
    @pytest.mark.parametrize("raw", [None, "nan", float("nan"), "abc", {}])
    def test_missing_stays_none(self, raw: object) -> None:
        # NaN is ESPHome's "no reading yet" — it must not survive as a number,
        # and must not be mistaken for a real 0.
        assert ac._as_float(raw) is None

    def test_zero_is_a_real_value(self) -> None:
        assert ac._as_float(0) == 0.0


class TestChannelCount:
    def test_reads_the_project_name(self) -> None:
        assert ac._channel_count("Athom Energy Monitor(6 Channels)", "") == 6

    def test_falls_back_to_the_package_suffix(self) -> None:
        assert ac._channel_count("", "github://x/athom-energy-monitor-x3.yaml") == 3

    def test_falls_back_to_six(self) -> None:
        assert ac._channel_count("", "") == ac._FALLBACK_CHANNELS


class TestIsAthomMeter:
    def test_matches_on_package_url(self) -> None:
        assert ac._is_athom_meter(
            {"package_import_url": "github://athom-tech/esp32-configs/athom-energy-monitor-x6.yaml"}
        )

    def test_rejects_other_esphome_devices(self) -> None:
        # The two Home Assistant Voice PE satellites answer the same mDNS
        # service type and must never be listed as energy meters.
        assert not ac._is_athom_meter(
            {
                "package_import_url": "github://esphome/home-assistant-voice-pe/home-assistant-voice.yaml",
                "project_name": "Nabu Casa.Home Assistant Voice PE",
            }
        )


class TestBuildState:
    def test_maps_device_sensors(self) -> None:
        state = ac._build_state(_endpoint(), ac._parse_events(SNAPSHOT), {})
        assert state.reachable is True
        assert state.voltage_v == pytest.approx(239.3847)
        assert state.wifi_rssi_dbm == -68
        assert state.total_power_w == pytest.approx(291.47)

    def test_every_channel_is_present_even_with_no_clamp(self) -> None:
        """The headline contract: six channels reported, six channels returned.

        Channels 3-6 published nothing in this snapshot. They must still appear
        so a clamp fitted later starts reading with no code change.
        """
        state = ac._build_state(_endpoint(), ac._parse_events(SNAPSHOT), {})
        assert [c.channel for c in state.channels] == [1, 2, 3, 4, 5, 6]
        assert state.channels[0].power_w == pytest.approx(-291.539)
        # Channel 2 genuinely measured 0 W; channels 3-6 measured nothing.
        assert state.channels[1].power_w == 0.0
        assert all(c.power_w is None for c in state.channels[2:])

    def test_channel_keys_are_meter_scoped(self) -> None:
        state = ac._build_state(_endpoint(), ac._parse_events(SNAPSHOT), {})
        assert state.channels[0].key == "AA:BB:CC:DD:EE:01:1"

    def test_invert_flips_the_sign_and_keeps_the_raw_value(self) -> None:
        inverted = {"AA:BB:CC:DD:EE:01:1": True}
        state = ac._build_state(_endpoint(), ac._parse_events(SNAPSHOT), inverted)
        channel = state.channels[0]
        assert channel.power_raw_w == pytest.approx(-291.539)
        assert channel.power_w == pytest.approx(291.539)
        assert channel.inverted is True

    def test_invert_leaves_a_missing_reading_missing(self) -> None:
        inverted = {"AA:BB:CC:DD:EE:01:4": True}
        state = ac._build_state(_endpoint(), ac._parse_events(SNAPSHOT), inverted)
        assert state.channels[3].power_w is None

    def test_a_higher_published_channel_beats_the_advertised_count(self) -> None:
        values = ac._parse_events(SNAPSHOT + 'event: state\ndata: {"id":"sensor-power_8","value":5}\n\n')
        state = ac._build_state(_endpoint(channels=6), values, {})
        assert [c.channel for c in state.channels] == [1, 2, 3, 4, 5, 6, 7, 8]


class TestUnreachable:
    def test_keeps_every_channel_so_circuits_do_not_vanish(self) -> None:
        state = ac._unreachable(_endpoint(), "Offline")
        assert state.reachable is False
        assert state.error == "Offline"
        assert [c.channel for c in state.channels] == [1, 2, 3, 4, 5, 6]
        assert all(c.power_w is None for c in state.channels)


class TestDiscoveryCache:
    """An mDNS sweep that finds nothing must not delete known meters.

    Measured on the live network, one cold 3 s browse missed the meter 1 time in
    20, and mDNS reports that as an empty result rather than an error. Treating
    it as fact would make circuits blink out of the card at random.
    """

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ac.clear_caches()
        monkeypatch.delenv("ATHOM_METER_HOSTS", raising=False)
        # discover_meters() reloads .env on every call; keep the developer's
        # real one out of the test.
        monkeypatch.setattr(ac, "load_dotenv", lambda **_kw: None)
        yield
        ac.clear_caches()

    def _browse(self, monkeypatch: pytest.MonkeyPatch, results: list) -> list:
        """Stub the browse; returns the list of timeout windows it was called with."""
        calls: list = []

        def fake(timeout: float) -> list:
            calls.append(timeout)
            return results.pop(0) if results else []

        monkeypatch.setattr(ac, "_discover_sync", fake)
        return calls

    def test_empty_sweep_keeps_the_previous_meters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        endpoint = _endpoint()
        self._browse(monkeypatch, [[endpoint]])
        found, error = asyncio.run(ac.discover_meters())
        assert found == [endpoint] and error is None

        # Next sweep (forced past the TTL) finds nothing at all.
        self._browse(monkeypatch, [])
        found, _ = asyncio.run(ac.discover_meters(force=True))
        assert found == [endpoint], "a missed browse must not delete a known meter"

    def test_empty_sweep_with_nothing_known_reports_no_meters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._browse(monkeypatch, [])
        found, error = asyncio.run(ac.discover_meters())
        assert found == [] and error is None

    def test_an_empty_first_browse_is_retried_once_with_a_wider_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeating the same short window can land in the same interference."""
        endpoint = _endpoint()
        calls = self._browse(monkeypatch, [[], [endpoint]])
        found, _ = asyncio.run(ac.discover_meters())
        assert calls == [ac._DISCOVERY_TIMEOUT_S, ac._DISCOVERY_RETRY_TIMEOUT_S]
        assert ac._DISCOVERY_RETRY_TIMEOUT_S > ac._DISCOVERY_TIMEOUT_S
        assert found == [endpoint]

    def test_a_successful_browse_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._browse(monkeypatch, [[_endpoint()]])
        asyncio.run(ac.discover_meters())
        assert len(calls) == 1

    def test_cached_result_is_reused_without_browsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._browse(monkeypatch, [[_endpoint()]])
        asyncio.run(ac.discover_meters())
        asyncio.run(ac.discover_meters())
        assert len(calls) == 1

    def test_clear_read_cache_keeps_discovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sign flip must not force a fresh (possibly empty) mDNS sweep."""
        calls = self._browse(monkeypatch, [[_endpoint()]])
        asyncio.run(ac.discover_meters())
        ac.clear_read_cache()
        found, _ = asyncio.run(ac.discover_meters())
        assert len(calls) == 1
        assert len(found) == 1


class TestParseHost:
    @pytest.mark.parametrize(
        "raw,host,port",
        [
            ("192.0.2.73", "192.0.2.73", 80),
            ("http://192.0.2.73/", "192.0.2.73", 80),
            ("meter.local:8080", "meter.local", 8080),
        ],
    )
    def test_parses(self, raw: str, host: str, port: int) -> None:
        endpoint = ac._parse_host(raw)
        assert endpoint is not None
        assert (endpoint.host, endpoint.port) == (host, port)

    @pytest.mark.parametrize("raw", ["", "   ", "http://"])
    def test_rejects_blank(self, raw: str) -> None:
        assert ac._parse_host(raw) is None


class TestNormaliseMac:
    def test_expands_bare_hex(self) -> None:
        assert ac._normalise_mac("aabbccddee01") == "AA:BB:CC:DD:EE:01"

    def test_accepts_dashed(self) -> None:
        assert ac._normalise_mac("aa-bb-cc-dd-ee-01") == "AA:BB:CC:DD:EE:01"
