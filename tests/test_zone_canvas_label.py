"""Tests for the zone canvas: persistent boxes, draw-to-add, border-click select,
interior move, corner resize, arrows, and the zone-mode dialog wiring on top of it.

Pure rect math is covered first (``hit_zone_edge`` / ``default_zone``); the Qt
behaviour drives real mouse events through ``QTest`` under the offscreen platform.
"""

from __future__ import annotations

from typing import cast

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtTest import QTest

from negpy.desktop.view.widgets.scan_window_geometry import default_zone, hit_zone_edge
from negpy.desktop.view.widgets.zone_canvas_label import ZoneCanvasLabel
from negpy.infrastructure.scanners.base import ScanMode, ScannerCapabilities, ScannerDevice

_W, _H = 300, 220


def _canvas(qapp) -> ZoneCanvasLabel:
    canvas = ZoneCanvasLabel()
    pm = QPixmap(_W, _H)
    pm.fill(QColor(128, 128, 128))
    canvas.set_frame(pm)
    canvas.resize(_W, _H)
    canvas.show()
    qapp.processEvents()
    return canvas


def _drag(canvas: ZoneCanvasLabel, qapp, p1: QPoint, p2: QPoint) -> None:
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
    QTest.mouseMove(canvas, p2)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p2)
    qapp.processEvents()


def _zone_device() -> ScannerDevice:
    caps = ScannerCapabilities(
        ir_channel=False,
        supported_dpi=(400, 800, 1600, 3200),
        supported_depths=(8, 16),
        sources=(ScanMode.NEGATIVE,),
        max_area_mm=(68.6, 237.0),
        zone_batch=True,
    )
    return ScannerDevice(id="epkowa:interpreter:001:006", vendor="Epson", model="Perfection V600", capabilities=caps)


# ── pure geometry ────────────────────────────────────────────────────


class TestZoneGeometry:
    def test_hit_zone_edge(self) -> None:
        rect = (0.2, 0.2, 0.5, 0.5)
        assert hit_zone_edge(rect, 0.2, 0.35, 0.01)  # on the left edge
        assert hit_zone_edge(rect, 0.35, 0.5, 0.01)  # on the bottom edge
        assert hit_zone_edge(rect, 0.205, 0.35, 0.01)  # just outside the left edge
        assert hit_zone_edge(rect, 0.5, 0.5, 0.01)  # a corner counts as the edge
        assert not hit_zone_edge(rect, 0.35, 0.35, 0.01)  # deep inside
        assert not hit_zone_edge(rect, 0.1, 0.1, 0.01)  # far outside

    def test_default_zone_is_centred_and_normalized(self) -> None:
        z = default_zone()
        assert z[0] < z[2] and z[1] < z[3]
        assert all(0.0 <= v <= 1.0 for v in z)
        assert (z[0] + z[2]) / 2 == pytest.approx(0.5)
        assert (z[1] + z[3]) / 2 == pytest.approx(0.5)


# ── canvas mouse behaviour ───────────────────────────────────────────


class TestZoneCanvas:
    def test_drag_on_empty_canvas_adds_a_zone(self, qapp) -> None:
        canvas = _canvas(qapp)
        _drag(canvas, qapp, QPoint(40, 30), QPoint(140, 90))

        zones = canvas.zones()
        assert len(zones) == 1
        x1, y1, x2, y2 = zones[0]
        assert x1 == pytest.approx(40 / _W, abs=0.01)
        assert y1 == pytest.approx(30 / _H, abs=0.01)
        assert x2 == pytest.approx(140 / _W, abs=0.01)
        assert y2 == pytest.approx(90 / _H, abs=0.01)
        assert canvas.active() == 0

    def test_drawing_a_second_box_adds_a_second_zone(self, qapp) -> None:
        canvas = _canvas(qapp)
        canvas.set_zones([(0.05, 0.05, 0.3, 0.3)])
        qapp.processEvents()
        _drag(canvas, qapp, QPoint(170, 20), QPoint(290, 100))

        assert len(canvas.zones()) == 2
        assert canvas.active() == 1

    def test_border_click_selects_zone_without_moving_it(self, qapp) -> None:
        canvas = _canvas(qapp)
        canvas.set_zones([(0.2, 0.2, 0.5, 0.5)])
        qapp.processEvents()
        # On the left border: x = 0.2*W, mid-height.
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(61, 77))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(61, 77))
        qapp.processEvents()

        assert canvas.active() == 0
        assert canvas.zones() == [(0.2, 0.2, 0.5, 0.5)]  # not moved

    def test_interior_drag_moves_the_zone(self, qapp) -> None:
        canvas = _canvas(qapp)
        canvas.set_zones([(0.2, 0.2, 0.5, 0.5)])
        qapp.processEvents()
        # Press well inside (x=120px → 0.4), drag right-down by (30, 20) px.
        _drag(canvas, qapp, QPoint(120, 77), QPoint(150, 97))

        z = canvas.zones()[0]
        assert z[0] == pytest.approx(0.3, abs=0.01)
        assert z[1] == pytest.approx(0.2 + 20 / _H, abs=0.01)
        assert z[2] == pytest.approx(0.6, abs=0.01)
        assert z[3] == pytest.approx(0.5 + 20 / _H, abs=0.01)

    def test_corner_drag_resizes_the_active_zone(self, qapp) -> None:
        canvas = _canvas(qapp)
        canvas.set_zones([(0.2, 0.2, 0.5, 0.5)])
        qapp.processEvents()
        # Grab the bottom-right corner and pull it out.
        _drag(canvas, qapp, QPoint(150, 110), QPoint(200, 150))

        z = canvas.zones()[0]
        assert z[0] == pytest.approx(0.2, abs=0.01)  # top-left stays
        assert z[1] == pytest.approx(0.2, abs=0.01)
        assert z[2] == pytest.approx(200 / _W, abs=0.01)
        assert z[3] == pytest.approx(150 / _H, abs=0.01)

    def test_clear_zones_removes_everything(self, qapp) -> None:
        canvas = _canvas(qapp)
        canvas.set_zones([(0.2, 0.2, 0.5, 0.5), (0.6, 0.6, 0.9, 0.9)])
        qapp.processEvents()
        canvas.clear_zones()

        assert canvas.zones() == []
        assert canvas.active() == -1

    def test_edits_emit_zones_changed(self, qapp) -> None:
        canvas = _canvas(qapp)
        changed: list = []
        canvas.zonesChanged.connect(changed.append)

        _drag(canvas, qapp, QPoint(40, 30), QPoint(140, 90))

        assert len(changed) == 1
        assert changed[0] == [(40 / _W, 30 / _H, 140 / _W, 90 / _H)]


# ── dialog zone mode ─────────────────────────────────────────────────


class TestZoneModeDialog:
    def _dialog(self, qapp):
        from negpy.desktop.view.widgets.quick_scan_preview_dialog import QuickScanPreviewDialog
        from tests.conftest import FakeController

        dialog = QuickScanPreviewDialog(FakeController(), _zone_device(), film_type="negative", zone_mode=True)
        qapp.processEvents()
        return dialog

    def test_initial_zones_render_and_survive_construction(self, qapp) -> None:
        from negpy.desktop.view.widgets.quick_scan_preview_dialog import QuickScanPreviewDialog
        from tests.conftest import FakeController

        saved = [(0.1, 0.1, 0.5, 0.5), (0.55, 0.1, 0.9, 0.5)]
        dialog = QuickScanPreviewDialog(
            FakeController(),
            _zone_device(),
            film_type="negative",
            zone_mode=True,
            initial_zones=saved,
        )
        qapp.processEvents()

        assert dialog.zones() == saved
        assert cast(ZoneCanvasLabel, dialog.label).active() == 1  # the last saved zone is active

    def test_add_duplicates_active_then_remove_deletes_it(self, qapp) -> None:
        dialog = self._dialog(qapp)
        canvas = cast(ZoneCanvasLabel, dialog.label)
        assert dialog.zones() == []

        dialog._add_zone()  # no zones yet → centred default
        assert len(dialog.zones()) == 1
        assert dialog.zones()[0] == default_zone()

        dialog._add_zone()  # duplicates the active zone
        assert len(dialog.zones()) == 2
        assert dialog.zones()[1] == default_zone()

        canvas.set_active(0)
        dialog._remove_zone()
        assert len(dialog.zones()) == 1
        assert dialog.zones()[0] == default_zone()

    def test_arrows_cycle_the_active_zone(self, qapp) -> None:
        dialog = self._dialog(qapp)
        canvas = cast(ZoneCanvasLabel, dialog.label)
        canvas.set_zones([(0.1, 0.1, 0.3, 0.3), (0.4, 0.4, 0.6, 0.6), (0.7, 0.7, 0.9, 0.9)])
        qapp.processEvents()

        assert canvas.active() == 2  # last is active
        dialog._cycle_active(1)  # wraps forward
        assert canvas.active() == 0
        dialog._cycle_active(-1)  # wraps backward
        assert canvas.active() == 2

    def test_load_and_update_buttons_are_gone(self, qapp) -> None:
        dialog = self._dialog(qapp)
        assert not hasattr(dialog, "load_zone_btn")
        assert not hasattr(dialog, "update_zone_btn")
        assert not hasattr(dialog, "zone_list")
