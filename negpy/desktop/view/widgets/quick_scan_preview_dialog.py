"""Modal pop-up: a single low-res preview scan and a crop window, for devices with
no addressable frame adapter (Plustek: one manual holder, not a strip/roll feeder —
see StripPreviewDialog for that case).

Zone mode (flatbed+TPU scanner): the same scan preview contains multiple image 
frames,each scanning as its own image. Read after ``exec()`` via``window()`` 
(single-window) or ``zones()`` (zone mode).
"""

import qtawesome as qta
from PyQt6.QtCore import pyqtSlot
from PyQt6.QtGui import QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from typing import cast

from negpy.desktop.converters import ImageConverter
from negpy.desktop.view.styles.templates import StatusStrip, pin_dialog_default
from negpy.desktop.view.styles.theme import THEME
from negpy.desktop.view.widgets.dialog_geometry import remember_dialog_geometry
from negpy.desktop.view.widgets.scan_preview_common import RollPreviewSignalsMixin, preview_positive
from negpy.desktop.view.widgets.scan_window_geometry import default_zone
from negpy.desktop.view.widgets.scan_window_label import ScanWindowLabel
from negpy.desktop.view.widgets.step_combo import StepCombo
from negpy.desktop.view.widgets.zone_canvas_label import ZoneCanvasLabel
from negpy.desktop.workers.scan_worker import RollPreviewRequest
from negpy.infrastructure.scanners.base import ScannerDevice

_PREVIEW_FALLBACK_DPI = 500  # only when the device reports no DPI list at all
_PREVIEW_SLOT = 1  # PerFrameRollSession's only slot on a frame-less device


class QuickScanPreviewDialog(RollPreviewSignalsMixin, QDialog):
    """Preview the current holder position at low res; set a crop window or crop zones for the real scan."""

    def __init__(
        self, controller, device: ScannerDevice, initial_window=None, film_type: str = "negative", parent=None, *, repo=None, zone_mode: bool = False, initial_zones=None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._device = device
        self._film_type = film_type
        self._caps = device.capabilities
        self._previewing = False
        self._scan_now = False  # set when the user chooses "Scan" over "Use"
        self._zone_mode = zone_mode
        self.setWindowTitle("Preview — Set Scan Zones" if zone_mode else "Preview — Set the Scan Window")
        self.setModal(True)
        self.resize(560, 480)

        layout = QVBoxLayout(self)

        if zone_mode:
            help_lbl = QLabel(
                "Preview the transparency area, then draw a box for each frame or zone — a corner "
                "to resize, inside to move. Click a zone's border to select it (or use the navigation "
                "arrows), Delete to remove it. Each frame scans as its own image, in order."
            )
        else:
            help_lbl = QLabel(
                "Preview the current holder position, then drag to crop — a corner to resize, "
                "inside to move. Use Apply Window (apply and return) or Scan (start scanning now)."
            )
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet(
            f"color: {THEME.text_secondary}; font-size: {THEME.font_size_small}px;"
            f" background: rgba(255,255,255,0.04); border-radius: 6px; padding: 6px 8px;"
        )
        layout.addWidget(help_lbl)

        top = QHBoxLayout()
        top.addWidget(QLabel("Preview DPI"))
        self.preview_dpi_combo = StepCombo()
        for dpi in sorted(self._caps.supported_dpi) or [_PREVIEW_FALLBACK_DPI]:
            self.preview_dpi_combo.addItem(str(dpi), dpi)
        self.preview_dpi_combo.setCurrentIndex(0)  # lowest: fastest, framing only
        self.preview_dpi_combo.setToolTip("Resolution used for the preview scan")
        top.addWidget(self.preview_dpi_combo)
        top.addStretch()
        if zone_mode:
            self.prev_zone_btn = QPushButton(qta.icon("fa5s.chevron-left", color=THEME.text_primary), "")
            self.prev_zone_btn.setFixedWidth(32)
            self.prev_zone_btn.setToolTip("Select the previous zone (Ctrl+Left)")
            self.prev_zone_btn.clicked.connect(lambda: self._cycle_active(-1))
            top.addWidget(self.prev_zone_btn)
            self.next_zone_btn = QPushButton(qta.icon("fa5s.chevron-right", color=THEME.text_primary), "")
            self.next_zone_btn.setFixedWidth(32)
            self.next_zone_btn.setToolTip("Select the next zone (Ctrl+Right)")
            self.next_zone_btn.clicked.connect(lambda: self._cycle_active(1))
            top.addWidget(self.next_zone_btn)
            self.add_zone_btn = QPushButton(qta.icon("fa5s.plus", color=THEME.text_primary), " Add Zone")
            self.add_zone_btn.setToolTip(
                "Add a new zone (keyboard: A or +) — a copy of the active zone, or a centered "
                "box if there are none yet; drag it into place afterwards"
            )
            self.add_zone_btn.clicked.connect(self._add_zone)
            top.addWidget(self.add_zone_btn)
            self.remove_zone_btn = QPushButton(qta.icon("fa5s.minus", color=THEME.text_primary), " Remove Zone")
            self.remove_zone_btn.setToolTip("Remove the active zone (keyboard: Delete)")
            self.remove_zone_btn.clicked.connect(self._remove_zone)
            top.addWidget(self.remove_zone_btn)
        self.preview_btn = QPushButton(qta.icon("fa5s.eye", color=THEME.text_primary), " Preview")
        self.preview_btn.clicked.connect(self._on_preview)
        top.addWidget(self.preview_btn)
        layout.addLayout(top)

        if zone_mode:
            self.label = ZoneCanvasLabel()
            if initial_zones:
                self.label.set_zones(list(initial_zones))
        else:
            self.label = ScanWindowLabel()
            self.label.set_window(tuple(initial_window) if initial_window else None)
        layout.addWidget(self.label, 1)

        if zone_mode:
            self._install_shortcuts()

        # One reserved row: the pass that is running, or the message it left behind.
        self.status_strip = StatusStrip(lines=1)
        layout.addWidget(self.status_strip)

        btns = QHBoxLayout()
        self.clear_btn = QPushButton("Clear Zones" if zone_mode else "Clear Crop")
        if zone_mode:
            canvas = cast(ZoneCanvasLabel, self.label)
            self.clear_btn.setToolTip("Remove all zones and start over")
            self.clear_btn.clicked.connect(canvas.clear_zones)
        else:
            single = cast(ScanWindowLabel, self.label)
            self.clear_btn.setToolTip("Scan the whole frame instead")
            self.clear_btn.clicked.connect(single.clear_window)
        btns.addWidget(self.clear_btn)
        btns.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        btns.addWidget(self.cancel_btn)
        self.ok_btn = QPushButton("Apply Zones" if zone_mode else "Apply Window")
        self.ok_btn.setToolTip(
            "Keep these zones and return to the Scan panel" if zone_mode else "Keep this window and return to the Scan panel"
        )
        self.ok_btn.clicked.connect(self._on_apply_clicked)
        btns.addWidget(self.ok_btn)
        self.scan_btn = QPushButton(qta.icon("fa5s.play", color=THEME.text_primary), " Scan Zones" if zone_mode else " Scan Frame")
        self.scan_btn.setToolTip("Scan these zones now with the current settings" if zone_mode else "Scan now with the current settings")
        self.scan_btn.clicked.connect(self._on_scan_clicked)
        btns.addWidget(self.scan_btn)
        pin_dialog_default(self.scan_btn, self.clear_btn, self.cancel_btn, self.ok_btn)
        layout.addLayout(btns)

        self._connect_preview_signals()
        remember_dialog_geometry(self, repo, "quick_scan_preview")

    # ── result getters ────────────────────────────────────────────────

    def window(self):
        """The single crop rect (non-zone mode only)."""
        return None if self._zone_mode else self.label.window()

    def zones(self) -> list[tuple[float, float, float, float]]:
        """Ordered normalized (x1,y1,x2,y2) crop rects, in scan order. Empty in single-window mode."""
        if not self._zone_mode:
            return []
        assert isinstance(self.label, ZoneCanvasLabel)
        return self.label.zones()

    def scan_requested(self) -> bool:
        """True when the dialog was accepted via Scan (start now), not Use."""
        return self._scan_now

    # ── zone batch ────────────────────────────────────────────────────

    def _install_shortcuts(self) -> None:
        self._shortcuts: list[QShortcut] = []

        def bind(keys: str, fn) -> None:
            sc = QShortcut(QKeySequence(keys), self)
            sc.activated.connect(fn)
            self._shortcuts.append(sc)

        bind("A", self._add_zone)
        bind("+", self._add_zone)
        bind("=", self._add_zone)
        bind("Delete", self._remove_zone)
        bind("Backspace", self._remove_zone)
        bind("Ctrl+Left", lambda: self._cycle_active(-1))
        bind("Ctrl+Right", lambda: self._cycle_active(1))

    def _add_zone(self) -> None:
        if not self._zone_mode:
            return
        assert isinstance(self.label, ZoneCanvasLabel)
        current = self.label.zones()
        source = current[self.label.active()] if 0 <= self.label.active() < len(current) else default_zone()
        self.label.set_zones([*current, source])
        self.status_strip.set_message("New zone added — drag it, or a corner, to size it.")

    def _remove_zone(self) -> None:
        if not self._zone_mode:
            return
        assert isinstance(self.label, ZoneCanvasLabel)
        idx = self.label.active()
        if idx < 0:
            self.status_strip.set_message("Select a zone first (click its border or use the arrows).")
            return
        zones = self.label.zones()
        del zones[idx]
        self.label.set_zones(zones)
        if zones:
            self.label.set_active(min(idx, len(zones) - 1))
        self.status_strip.set_message("Zone removed.")

    def _cycle_active(self, delta: int) -> None:
        if not self._zone_mode:
            return
        assert isinstance(self.label, ZoneCanvasLabel)
        n = len(self.label.zones())
        if n == 0:
            return
        self.label.set_active((self.label.active() + delta) % n)

    # ── ui state ──────────────────────────────────────────────────────

    def _on_apply_clicked(self) -> None:
        self.accept()

    def _on_scan_clicked(self) -> None:
        self._scan_now = True
        self.accept()

    def _on_cancel_clicked(self) -> None:
        """Stop the pass in flight, or leave when there is none."""
        if self._previewing:
            self.stop_preview()
            return
        self.reject()

    def _set_previewing(self, busy: bool) -> None:
        self.preview_btn.setEnabled(not busy)
        # Committing mid-pass would hand the scan a unit the preview still holds.
        self.ok_btn.setEnabled(not busy)
        self.scan_btn.setEnabled(not busy)
        self.cancel_btn.setText("Stop Preview" if busy else "Cancel")
        if busy:
            self.status_strip.start_progress("Previewing… %p%")
        else:
            self.status_strip.stop_progress()

    def _preview_dpi(self) -> int:
        return int(self.preview_dpi_combo.currentData() or _PREVIEW_FALLBACK_DPI)

    def _on_preview(self) -> None:
        if self._previewing:
            return
        req = RollPreviewRequest(
            device=self._device,
            slots=(_PREVIEW_SLOT,),
            dpi=self._preview_dpi(),
            offsets={},
        )
        try:
            self._controller.start_roll_preview(req)
        except Exception as e:
            self.status_strip.set_message(f"Scanner busy — {e}")
            return
        self._previewing = True
        self._set_previewing(True)
        self.status_strip.set_message("Previewing…")

    @pyqtSlot(object)
    def _on_preview_ready(self, preview) -> None:
        if preview.slot != _PREVIEW_SLOT:
            return
        if preview.error is not None:
            self.status_strip.set_message(f"Preview failed: {preview.error}")
            return
        try:
            positive = preview_positive(preview.rgb, self._film_type)
            pixmap = QPixmap.fromImage(ImageConverter.to_qimage(positive))
        except Exception as e:
            self.status_strip.set_message(f"Could not display preview: {e}")
            return
        self.label.set_frame(pixmap)

    @pyqtSlot()
    def _on_preview_finished(self) -> None:
        self._previewing = False
        self._set_previewing(False)
        if not self.status_strip.message().startswith("Preview failed") and not self.status_strip.message().startswith("Could not display"):
            self.status_strip.set_message("")

    @pyqtSlot(str)
    def _on_error(self, msg) -> None:
        if not self._previewing:
            return
        self._previewing = False
        self._set_previewing(False)
        self.status_strip.set_message(f"Preview failed: {msg}")

    @pyqtSlot()
    def _on_cancelled(self) -> None:
        if not self._previewing:
            return
        self._previewing = False
        self._set_previewing(False)
        self.status_strip.set_message("Preview canceled")
