"""Tests for the zone-batch settings: field coercion and selection resolution."""

from negpy.infrastructure.scanners.settings import ScannerSettings, resolve_zone_selection


class TestZoneSettingsRoundTrip:
    def test_defaults(self) -> None:
        assert ScannerSettings.defaults().zones is None

    def test_from_dict_coerces_list_of_lists_to_tuples(self) -> None:
        settings = ScannerSettings.from_dict({"zones": [[0.0, 0.0, 0.5, 0.5], [0.5, 0.0, 1.0, 0.5]]})
        assert settings.zones == [(0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 1.0, 0.5)]
        assert settings.zones is not None
        assert all(isinstance(z, tuple) for z in settings.zones)

    def test_unknown_keys_ignored(self) -> None:
        settings = ScannerSettings.from_dict({"zones": [[0.1, 0.1, 0.9, 0.9]], "bogus_key": 1})
        assert settings.zones == [(0.1, 0.1, 0.9, 0.9)]


class TestResolveZoneSelection:
    def test_orders_zones_1_based_in_list_order(self) -> None:
        settings = ScannerSettings(zones=[(0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 1.0, 0.5), (0.0, 0.5, 1.0, 1.0)])
        frames, windows = resolve_zone_selection(settings)
        assert frames == (1, 2, 3)
        assert windows == {
            1: (0.0, 0.0, 0.5, 0.5),
            2: (0.5, 0.0, 1.0, 0.5),
            3: (0.0, 0.5, 1.0, 1.0),
        }

    def test_no_zones_yields_empty_selection(self) -> None:
        frames, windows = resolve_zone_selection(ScannerSettings())
        assert frames == ()
        assert windows == {}
        frames, windows = resolve_zone_selection(ScannerSettings(zones=None))
        assert frames == ()
        assert windows == {}
