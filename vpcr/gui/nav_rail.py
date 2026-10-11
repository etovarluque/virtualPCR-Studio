"""The navigation rail: one button per results view, down the window's left edge.

Each button is an outline icon over a short label, with an optional counter
badge (pairs run, detail rows, failed jobs). Everything is painted from the
theme tokens at paint time, so a theme switch needs only a repaint, and the
icons are drawn as paths rather than taken from a font, where Windows would
substitute colour emoji or nothing.
"""
from __future__ import annotations

from typing import Callable, Sequence

from PyQt6 import sip
from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QVBoxLayout,
                             QWidget)

from vpcr.gui import theme

RAIL_WIDTH = 72


# Icons on a 24 x 24 grid, stroked, never filled.
def _summary(p: QPainterPath) -> None:
    p.addRoundedRect(QRectF(3.5, 4.5, 17, 15), 2, 2)
    p.moveTo(3.5, 9.5); p.lineTo(20.5, 9.5)
    p.moveTo(3.5, 14.5); p.lineTo(20.5, 14.5)
    p.moveTo(9.5, 9.5); p.lineTo(9.5, 19.5)


def _detail(p: QPainterPath) -> None:
    for y in (6.5, 12, 17.5):
        p.addEllipse(QPointF(5, y), 1.1, 1.1)
        p.moveTo(9, y); p.lineTo(20.5, y)


def _align(p: QPainterPath) -> None:
    p.moveTo(4, 7); p.lineTo(20, 7)
    p.moveTo(4, 12); p.lineTo(14, 12)
    p.moveTo(4, 17); p.lineTo(13, 17)
    p.moveTo(17, 10.5); p.lineTo(20, 12); p.lineTo(17, 13.5)


def _map(p: QPainterPath) -> None:
    p.moveTo(3, 18.5); p.lineTo(21, 18.5)
    p.moveTo(6, 14.5); p.lineTo(11, 14.5)
    p.moveTo(13, 10.5); p.lineTo(18, 10.5)
    p.moveTo(8, 6.5); p.lineTo(14, 6.5)


def _log(p: QPainterPath) -> None:
    p.moveTo(7, 4.5); p.lineTo(15, 4.5); p.lineTo(18.5, 8)
    p.lineTo(18.5, 19.5); p.lineTo(7, 19.5); p.closeSubpath()
    p.moveTo(10, 11); p.lineTo(15.5, 11)
    p.moveTo(10, 14.5); p.lineTo(15.5, 14.5)


ICONS: dict[str, Callable[[QPainterPath], None]] = {
    "summary": _summary, "detail": _detail, "align": _align, "map": _map, "log": _log,
}


def compact_count(n: int) -> str:
    """A badge-sized number: 7, 980, 1.2k, 35k, 1.4M."""
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


class RailButton(QAbstractButton):
    """An icon over a label, a selection bar on the left, a badge top right."""

    def __init__(self, key: str, label: str, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self.setText(label)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(RAIL_WIDTH, 54)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.badge = ""
        self.badge_kind = "accent"     # or "bad"
        self._key_focus = False

    # The focus ring is for keyboard users: a window opening hands the focus
    # to the first button, and a ring around it then reads as a second state.
    def focusInEvent(self, event) -> None:  # noqa: N802
        self._key_focus = event.reason() in (Qt.FocusReason.TabFocusReason,
                                             Qt.FocusReason.BacktabFocusReason)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._key_focus = False
        super().focusOutEvent(event)

    def set_badge(self, text: str, kind: str = "accent") -> None:
        self.badge, self.badge_kind = text, kind
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(RAIL_WIDTH, 54)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        hover = self.underMouse()
        box = QRectF(8, 3, self.width() - 16, self.height() - 6)

        if on or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color("accent_soft" if on else "chip"))
            p.drawRoundedRect(box, 8, 8)
        if on:
            p.setBrush(theme.color("accent"))
            p.drawRoundedRect(QRectF(2, 14, 3, self.height() - 28), 1.5, 1.5)
        if self.hasFocus() and self._key_focus:
            p.setPen(QPen(theme.color("accent"), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), 8, 8)

        ink = theme.color("accent" if on else "ink" if hover else "muted")
        # icon: 20 px, centred above the label
        path = QPainterPath()
        ICONS[self.key](path)
        p.save()
        s = 20 / 24
        p.translate(self.width() / 2 - 10, 9)
        p.scale(s, s)
        p.setPen(QPen(ink, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.restore()

        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        if on:
            f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(ink)
        p.drawText(QRectF(0, 31, self.width(), 16), Qt.AlignmentFlag.AlignCenter, self.text())

        if self.badge:
            bf = QFont(self.font())
            bf.setPointSizeF(max(6.5, bf.pointSizeF() - 2))
            bf.setWeight(QFont.Weight.Bold)
            p.setFont(bf)
            w = max(16.0, p.fontMetrics().horizontalAdvance(self.badge) + 9.0)
            r = QRectF(self.width() / 2 + 3, 4, w, 15)
            bg, fg = (("bad", "accent_ink") if self.badge_kind == "bad"
                      else ("accent", "accent_ink"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color(bg))
            p.drawRoundedRect(r, 7.5, 7.5)
            p.setPen(QColor("#ffffff") if self.badge_kind == "bad" else theme.color(fg))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.badge)
        p.end()


class NavRail(QFrame):
    """The rail itself: the view buttons at the top, `bottom` widgets at the foot."""

    currentChanged = pyqtSignal(int)

    def __init__(self, items: Sequence[tuple[str, str, str]], parent=None) -> None:
        """`items` are (key, label, tooltip), one per view, in view order."""
        super().__init__(parent)
        self.setObjectName("rail")
        self.setFixedWidth(RAIL_WIDTH)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons: dict[str, RailButton] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 10, 0, 10)
        lay.setSpacing(2)
        for i, (key, label, tip) in enumerate(items):
            b = RailButton(key, label)
            b.setToolTip(tip)
            self._group.addButton(b, i)
            self.buttons[key] = b
            lay.addWidget(b)
        lay.addStretch(1)
        self._bottom = lay
        if items:
            self._group.button(0).setChecked(True)
        self._group.idToggled.connect(lambda i, on: on and self.currentChanged.emit(i))
        theme.bus().changed.connect(self._on_theme)

    def add_bottom(self, w: QWidget) -> None:
        self._bottom.addWidget(w, 0, Qt.AlignmentFlag.AlignHCenter)

    def _on_theme(self, _scheme: str) -> None:
        if not sip.isdeleted(self):
            self.update_all()

    def update_all(self) -> None:
        for b in self.buttons.values():
            b.update()

    def current(self) -> int:
        return self._group.checkedId()

    def set_current(self, i: int) -> None:
        b = self._group.button(i)
        if b is not None and not b.isChecked():
            b.setChecked(True)

    def set_badge(self, key: str, text: str = "", kind: str = "accent") -> None:
        self.buttons[key].set_badge(text, kind)

    def badge(self, key: str) -> str:
        return self.buttons[key].badge
