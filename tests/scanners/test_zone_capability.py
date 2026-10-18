"""Tests for zone-batch capability detection (flatbed+TPU "single-surface" devices)."""

from dataclasses import dataclass
from typing import Any

from negpy.infrastructure.scanners.params import ScanMode
from negpy.infrastructure.scanners.sane_backend import _caps_from_options


@dataclass
class FakeOption:
    """Stand-in for python-sane's Option (only the fields _caps_from_options reads)."""

    constraint: Any = None
    desc: str = ""
    unit: Any = None


def _opts(*, source: tuple[str, ...] | None = None, frame: tuple[int, int] | None = None) -> dict:
    o: dict = {}
    if source is not None:
        o["source"] = FakeOption(constraint=list(source))
    if frame is not None:
        o["frame"] = FakeOption(constraint=frame)
    return o


class TestZoneBatchCapability:
    def test_flatbed_tpu_is_zone_batch(self) -> None:
        """Epson V600-style: an explicit Transparency Unit source and no frame slots."""
        caps = _caps_from_options(_opts(source=("Flatbed", "Transparency Unit")))
        assert ScanMode.TRANSPARENCY in caps.sources
        assert caps.zone_batch is True

    def test_epson_source_spellings_all_qualify(self) -> None:
        for source in (("Transparency",), ("Transparency Unit",), ("TPU",), ("Film",), ("tpu",)):
            caps = _caps_from_options(_opts(source=source))
            assert caps.zone_batch is True, source

    def test_flatbed_without_tpu_is_not_zone_batch(self) -> None:
        caps = _caps_from_options(_opts(source=("Flatbed",)))
        assert caps.zone_batch is False
        assert ScanMode.TRANSPARENCY not in caps.sources

    def test_strip_feeder_with_tpu_stays_frame_batch(self) -> None:
        """A feeder that also exposes a transparency source must keep frame batch."""
        caps = _caps_from_options(_opts(source=("Flatbed", "Transparency Unit"), frame=(1, 6)))
        assert ScanMode.TRANSPARENCY in caps.sources
        assert caps.adapter_frame_capacity == 6
        assert caps.zone_batch is False

    def test_dedicated_film_scanner_no_source_is_not_zone_batch(self) -> None:
        """Inferred sources (no `source` option) must never qualify: pieusb & friends."""
        o = {"mode": FakeOption(constraint=["Color", "RGBI"])}
        caps = _caps_from_options(o, device_id="pieusb:libusb:001:003")
        assert ScanMode.TRANSPARENCY in caps.sources  # inference populated it
        assert caps.zone_batch is False  # but it is not a single-surface zone device

    def test_no_source_no_inference_is_not_zone_batch(self) -> None:
        caps = _caps_from_options({})
        assert caps.zone_batch is False

    def test_default_false_when_absent(self) -> None:
        # Explicit-sources default of the capability struct itself.
        caps = _caps_from_options({})
        assert caps.zone_batch is False
