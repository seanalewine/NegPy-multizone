"""Tests for zone-batch execution through the scan worker (flatbed+TPU multi-ROI).

A zone batch is a ``BatchRequest`` with ``address_frames=False``: the zone index is
only output numbering — the scan params never carry a ``frame`` selection (a flatbed
has no frame option, and selecting one is a backend error) and there is no feed axis.
"""

from __future__ import annotations

from typing import Any

from negpy.desktop.workers.scan_worker import BatchRequest, ScanWorker
from negpy.infrastructure.scanners.params import ScanParams


class _ZoneService:
    """Records per-zone scans and writes zone-numbered paths, like the batch fake."""

    def __init__(self, *, fail_on: int | None = None, cancel_before: int | None = None) -> None:
        self.fail_on = fail_on
        self.cancel_before = cancel_before
        self.call = 0
        self.frames: list[int | None] = []
        self.windows: list[Any] = []
        self.written_seqs: list[int] = []

    def eject(self, device_id: str) -> bool:
        return False

    def detect_frames(self, device_id: str, *, film_format: str | None = None, film_type: str = "negative") -> int:
        return 0

    def run_scan(self, device_id, params, progress, cancel):
        self.call += 1
        if self.cancel_before is not None and self.call == self.cancel_before:
            cancel.set()
        self.frames.append(params.frame)
        self.windows.append(params.window)
        if self.fail_on is not None and self.call == self.fail_on:
            raise RuntimeError("zone failed")
        if progress:
            progress(1.0)
        return object()

    def write_result(self, *, result, output_folder, filename_pattern, output_format, seq) -> str:
        self.written_seqs.append(seq)
        return f"/tmp/zone_{seq:03d}.tif"


_ZONES = {
    1: (0.0, 0.0, 0.5, 0.5),
    2: (0.5, 0.0, 1.0, 0.5),
    3: (0.0, 0.5, 1.0, 1.0),
}


def _zone_batch() -> BatchRequest:
    return BatchRequest(
        device_id="epkowa:interpreter:001:006",
        params=ScanParams(dpi=1200, depth=16, capture_ir=False),
        output_folder="/tmp",
        filename_pattern='scan-{{ "%03d" % seq }}',
        output_format="TIFF",
        frames=tuple(_ZONES),
        frame_windows=_ZONES,
        eject_when_done=False,
        unit_name="Zone",
        address_frames=False,
    )


class TestZoneBatchWorker:
    def test_scans_each_zone_with_its_own_window(self) -> None:
        worker = ScanWorker()
        service = _ZoneService()
        worker._service = service  # type: ignore[assignment]
        finished: list = []
        worker.batch_finished.connect(finished.append)

        worker.run_batch(_zone_batch())

        # The flatbed is never told a frame number: the zone index is naming only.
        assert service.frames == [None, None, None]
        assert service.windows == list(_ZONES.values())
        assert service.written_seqs == [1, 2, 3]
        assert finished == [["/tmp/zone_001.tif", "/tmp/zone_002.tif", "/tmp/zone_003.tif"]]

    def test_progress_phase_names_zones(self) -> None:
        worker = ScanWorker()
        service = _ZoneService()
        worker._service = service  # type: ignore[assignment]
        phases: list[str] = []
        worker.progress.connect(lambda _frac, phase: phases.append(phase))

        worker.run_batch(_zone_batch())

        assert any("Zone 1 of 3" in p for p in phases)
        assert any("Zone 3 of 3" in p for p in phases)
        assert not any("Frame" in p for p in phases)

    def test_cancel_mid_batch_reports_completed_zones(self) -> None:
        worker = ScanWorker()
        service = _ZoneService(cancel_before=2)
        worker._service = service  # type: ignore[assignment]
        finished: list = []
        worker.batch_finished.connect(finished.append)

        worker.run_batch(_zone_batch())

        # Zone 2's scan runs (the fake sets cancel mid-scan) but is not written.
        assert service.frames == [None, None]
        assert service.written_seqs == [1]
        assert finished == [["/tmp/zone_001.tif"]]

    def test_mid_batch_error_reports_completed_zones_and_error(self) -> None:
        worker = ScanWorker()
        service = _ZoneService(fail_on=2)
        worker._service = service  # type: ignore[assignment]
        finished: list = []
        errors: list = []
        worker.batch_finished.connect(finished.append)
        worker.error.connect(errors.append)

        worker.run_batch(_zone_batch())

        assert service.frames == [None, None]
        assert service.written_seqs == [1]
        assert finished == [["/tmp/zone_001.tif"]]
        assert errors and "zone failed" in errors[0]
