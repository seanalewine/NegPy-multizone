"""QLabel showing a scan preview with N persistent, draggable/resizable crop zones.

Each zone is a 0..1 rect (crop convention), drawn as a numbered box that stays
visible once created. The active zone (most recently added, or picked via the
arrows / a border click) is highlighted with drag handles and can be moved
(interior drag) or resized (corner drag). Drawing a fresh box in empty space adds
a zone immediately; clicking a zone's border selects it without dragging it.

Rect math lives in ``scan_window_geometry``; scaffolding mirrors ``scan_window_label``
and ``sidebar/roi_image.py``.
"""

from typing import Optional

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy

from negpy.desktop.view.styles.theme import THEME
from negpy.desktop.view.widgets.scan_window_geometry import (
    Rect,
    hit_corner,
    hit_zone_edge,
    normalize_rect,
    resize_corner,
)
from negpy.features.geometry.logic import translate_normalized_rect

_HANDLE_TOL = 0.03  # corner grab radius, fraction of frame
_HANDLE_PX = 5  # drawn handle half-size, widget px
_EDGE_TOL = 0.015  # border-click band, fraction of frame


class ZoneCanvasLabel(QLabel):
    """Preview frame with any number of persistent crop-zone rectangles."""

    zonesChanged = pyqtSignal(object)  # list[(x1, y1, x2, y2)] in 0..1, after any edit
    activeChanged = pyqtSignal(int)  # index of the active zone, -1 when none

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._pixmap: Optional[QPixmap] = None
        self._coverage: Optional[tuple[float, float]] = None
        self._zones: list[Rect] = []
        self._active = -1
        self._draft: Optional[Rect] = None  # the box being drawn, committed on release
        self._mode: Optional[str] = None  # "draw" | "move" | "resize" | "select"
        self._active_corner: Optional[int] = None
        self._press_frac: Optional[tuple[float, float]] = None
        self._rect_at_press: Optional[Rect] = None
        self._zone_at_press: Optional[int] = None

    # ── public API ────────────────────────────────────────────────────

    def set_frame(self, pixmap: QPixmap, coverage: Optional[tuple[float, float]] = None) -> None:
        """Show a preview. `coverage` semantics match ``ScanWindowLabel`` (raster span)."""
        self._pixmap = pixmap
        self._coverage = coverage
        self.update()

    def set_zones(self, zones: list[Rect]) -> None:
        """Replace the zone set; the last zone becomes active when any exist."""
        self._zones = [tuple(z) for z in zones]  # type: ignore[assignment]
        self._set_active(len(self._zones) - 1)
        self.update()

    def zones(self) -> list[Rect]:
        """Ordered normalized (x1,y1,x2,y2) rects, in scan order."""
        return [tuple(z) for z in self._zones]  # type: ignore[return-value]

    def set_active(self, index: int) -> None:
        if 0 <= index < len(self._zones):
            self._set_active(index)

    def active(self) -> int:
        return self._active

    def clear_zones(self) -> None:
        self._zones = []
        self._set_active(-1)
        self.update()
        self.zonesChanged.emit(self.zones())

    def has_frame(self) -> bool:
        return self._pixmap is not None and not self._pixmap.isNull()

    def _set_active(self, index: int) -> None:
        if index != self._active:
            self._active = index
            self.activeChanged.emit(index)
            self.update()

    # ── geometry (mirrors ScanWindowLabel) ────────────────────────────

    def _display(self) -> Optional[QRect]:
        """Widget-px rect the full frame is drawn into (letterboxed & centred)."""
        if self._pixmap is None or self._pixmap.isNull():
            return None
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw <= 0 or ph <= 0:
            return None
        span = self._coverage_span()
        pw = pw / span  # the frame is wider than the raster when the scan started past 0
        scale = min(self.width() / pw, self.height() / ph)
        dw, dh = int(pw * scale), int(ph * scale)
        return QRect((self.width() - dw) // 2, (self.height() - dh) // 2, dw, dh)

    def _coverage_span(self) -> float:
        if self._coverage is None:
            return 1.0
        start, end = self._coverage
        return max(1e-3, end - start)

    def _content_rect(self, draw_rect: QRect) -> QRect:
        """Sub-rect the pixmap occupies; may overflow draw_rect (painting clips)."""
        if self._coverage is None:
            return draw_rect
        x = draw_rect.left() + int(self._coverage[0] * draw_rect.width())
        return QRect(x, draw_rect.top(), max(1, int(self._coverage_span() * draw_rect.width())), draw_rect.height())

    @staticmethod
    def _to_fraction(p: QPoint, draw_rect: QRect) -> tuple[float, float]:
        fx = min(1.0, max(0.0, (p.x() - draw_rect.x()) / max(1, draw_rect.width())))
        fy = min(1.0, max(0.0, (p.y() - draw_rect.y()) / max(1, draw_rect.height())))
        return fx, fy

    @staticmethod
    def _rect_in_widget(rect: Rect, draw_rect: QRect) -> QRect:
        x1, y1, x2, y2 = rect
        ax = draw_rect.x() + int(x1 * draw_rect.width())
        ay = draw_rect.y() + int(y1 * draw_rect.height())
        bx = draw_rect.x() + int(x2 * draw_rect.width())
        by = draw_rect.y() + int(y2 * draw_rect.height())
        return QRect(QPoint(ax, ay), QPoint(bx, by)).normalized()

    # ── mouse ─────────────────────────────────────────────────────────

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        draw_rect = self._display()
        if draw_rect is None:
            return
        fx, fy = self._to_fraction(ev.pos(), draw_rect)
        self._press_frac = (fx, fy)
        # 1. A corner of the active zone resizes it (handles only on the active zone).
        if 0 <= self._active < len(self._zones):
            rect = self._zones[self._active]
            corner = hit_corner(rect, fx, fy, _HANDLE_TOL)
            if corner is not None:
                self._zone_at_press = self._active
                self._rect_at_press = rect
                self._mode, self._active_corner = "resize", corner
                return
        # 2. On a zone's border → select it (highlight) without starting a drag,
        #    so Remove and the arrows act on the box the user is pointing at.
        for i in range(len(self._zones) - 1, -1, -1):
            if hit_zone_edge(self._zones[i], fx, fy, _EDGE_TOL):
                self._zone_at_press = i
                self._set_active(i)
                self._mode = "select"
                return
        # 3. Inside a zone → select it and drag to move.
        for i in range(len(self._zones) - 1, -1, -1):
            x1, y1, x2, y2 = self._zones[i]
            if x1 <= fx <= x2 and y1 <= fy <= y2:
                self._zone_at_press = i
                self._rect_at_press = self._zones[i]
                self._set_active(i)
                self._mode = "move"
                return
        # 4. Empty space → draw a fresh zone (committed on release).
        self._mode = "draw"
        self._draft = (fx, fy, fx, fy)
        self.update()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        draw_rect = self._display()
        if draw_rect is None or self._mode is None or self._press_frac is None:
            return
        fx, fy = self._to_fraction(ev.pos(), draw_rect)
        if self._mode == "draw":
            px, py = self._press_frac
            self._draft = (px, py, fx, fy)
            self.update()
        elif self._mode == "resize" and self._zone_at_press is not None and self._active_corner is not None:
            self._zones[self._zone_at_press] = resize_corner(self._zones[self._zone_at_press], self._active_corner, fx, fy)
            self.update()
        elif self._mode == "move" and self._zone_at_press is not None and self._rect_at_press is not None:
            self._zones[self._zone_at_press] = translate_normalized_rect(
                self._rect_at_press, fx - self._press_frac[0], fy - self._press_frac[1]
            )
            self.update()

    def mouseReleaseEvent(self, _ev: QMouseEvent) -> None:
        if self._mode is None:
            return
        if self._mode == "draw" and self._draft is not None:
            rect = normalize_rect(self._draft)
            self._draft = None
            self._zones.append(rect)
            self._set_active(len(self._zones) - 1)
            self.zonesChanged.emit(self.zones())
        elif self._mode in ("resize", "move") and self._zone_at_press is not None:
            self._zones[self._zone_at_press] = normalize_rect(self._zones[self._zone_at_press])
            self.zonesChanged.emit(self.zones())
        self._mode = None
        self._active_corner = None
        self._press_frac = None
        self._rect_at_press = None
        self._zone_at_press = None
        self.update()

    # ── paint ─────────────────────────────────────────────────────────

    def paintEvent(self, _ev) -> None:
        painter = QPainter(self)
        draw_rect = self._display()
        if draw_rect is not None and self._pixmap is not None:
            content = self._content_rect(draw_rect)
            if content != draw_rect:
                painter.fillRect(draw_rect, QColor(THEME.bg_dark))
            painter.save()
            painter.setClipRect(draw_rect)
            painter.drawPixmap(content, self._pixmap)
            painter.restore()
            for i, rect in enumerate(self._zones):
                self._paint_zone(painter, i, rect, draw_rect)
            if self._draft is not None:
                wr = self._rect_in_widget(self._draft, draw_rect)
                pen = QPen(QColor(THEME.status_success), 2)
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(wr)
            # Drawn last, so zones and a crop cannot hide the frame boundary.
            painter.setPen(QPen(QColor(THEME.accent_primary), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(draw_rect.adjusted(0, 0, -1, -1))
        else:
            painter.fillRect(self.rect(), QColor(THEME.bg_dark))
        painter.end()

    def _paint_zone(self, painter: QPainter, index: int, rect: Rect, draw_rect: QRect) -> None:
        """Draw one zone: numbered box; the active one is highlighted with handles."""
        is_active = index == self._active
        wr = self._rect_in_widget(rect, draw_rect)
        color = QColor(THEME.status_success) if is_active else QColor(THEME.accent_primary)
        painter.setPen(QPen(color, 2 if is_active else 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(wr)
        # Zone number in a small tag pinned to the box's top-left, kept inside the frame.
        label = str(index + 1)
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(label)
        th = fm.height()
        bx, by = wr.x(), wr.y() - th - 4
        if by < draw_rect.top():
            by = wr.y() + 4
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawRect(QRect(bx, by, tw + 6, th))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(bx + 3, by + fm.ascent(), label)
        if is_active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            for corner in (wr.topLeft(), wr.topRight(), wr.bottomRight(), wr.bottomLeft()):
                painter.drawRect(QRect(corner.x() - _HANDLE_PX, corner.y() - _HANDLE_PX, 2 * _HANDLE_PX, 2 * _HANDLE_PX))
