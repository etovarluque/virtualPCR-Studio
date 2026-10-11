"""Toasts: short notices in the corner of the results, in place of message boxes.

A message box stops everything until it is dismissed, which suits a question
but not news: "exported", "loaded", "3 jobs failed". A toast says it without
taking the focus, goes away on its own, and can carry the next step as a
button (Open folder, Show log, Undo). Errors stay until closed, so one is never
missed; hovering a toast holds it open.

`notify` is the entry point for code that may run with or without a host: a
window that has a `toasts` attribute gets a toast, anything else (a modal
dialog, a test with no window) a message box, which is then the only thing
that can show above the dialog anyway.
"""
from __future__ import annotations

from typing import Callable, Sequence

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                             QMessageBox, QPushButton, QToolButton, QVBoxLayout,
                             QWidget, QFrame)

from vpcr.gui import theme

KINDS = ("success", "info", "warning", "error")
# Milliseconds on screen; 0 = until closed.
TIMEOUTS = {"success": 5000, "info": 5000, "warning": 9000, "error": 0}
_TOKEN = {"success": "good", "info": "accent", "warning": "warn", "error": "bad"}
MAX_VISIBLE = 4
WIDTH = 420
MARGIN = 14


def _glyph(kind: str, size: int = 20) -> QPixmap:
    """A filled circle in the kind's colour with a white mark: ✓ i ! ×."""
    dpr = 2
    px = QPixmap(size * dpr, size * dpr)
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(theme.color(_TOKEN[kind]))
    p.drawEllipse(QRectF(0, 0, size, size))
    ink = QColor(theme.tok("card"))
    pen = QPen(ink, size * 0.12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    n = size
    path = QPainterPath()
    if kind == "success":
        path.moveTo(n * 0.28, n * 0.52); path.lineTo(n * 0.44, n * 0.67)
        path.lineTo(n * 0.73, n * 0.36)
        p.drawPath(path)
    elif kind == "error":
        p.drawLine(QPointF(n * 0.34, n * 0.34), QPointF(n * 0.66, n * 0.66))
        p.drawLine(QPointF(n * 0.66, n * 0.34), QPointF(n * 0.34, n * 0.66))
    else:
        dot_y, (a, b) = ((0.30, (0.45, 0.72)) if kind == "info" else (0.72, (0.27, 0.55)))
        p.drawLine(QPointF(n / 2, n * a), QPointF(n / 2, n * b))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawEllipse(QPointF(n / 2, n * dot_y), n * 0.07, n * 0.07)
    p.end()
    return px


def _close_icon() -> QIcon:
    """A drawn ✕: the glyph is missing from some fonts and shows as a box."""
    dpr = 2
    px = QPixmap(12 * dpr, 12 * dpr)
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(theme.color("muted"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(2.5, 2.5), QPointF(9.5, 9.5))
    p.drawLine(QPointF(9.5, 2.5), QPointF(2.5, 9.5))
    p.end()
    return QIcon(px)


class Toast(QFrame):
    """One notice: glyph, text, optional action buttons, and a close button."""

    closed = pyqtSignal()

    def __init__(self, kind: str, text: str,
                 actions: Sequence[tuple[str, Callable[[], None]]] = (),
                 timeout: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self.kind = kind if kind in KINDS else "info"
        self.setObjectName("toast")
        self.setProperty("kind", self.kind)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

        self.icon = QLabel()
        self.icon.setFixedSize(20, 20)
        self.icon.setPixmap(_glyph(self.kind))
        self.label = QLabel(text)
        self.label.setObjectName("toastText")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.close_btn = QToolButton()
        self.close_btn.setObjectName("toastClose")
        self.close_btn.setIcon(_close_icon())
        self.close_btn.setIconSize(QSize(12, 12))
        self.close_btn.setToolTip("Dismiss")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.dismiss)

        body = QVBoxLayout()
        body.setSpacing(6)
        body.addWidget(self.label)
        self.action_buttons: list[QPushButton] = []
        if actions:
            row = QHBoxLayout()
            row.setSpacing(6)
            for text_, fn in actions:
                b = QPushButton(text_)
                b.setProperty("toastAction", "true")
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda _=False, fn=fn: self._run(fn))
                row.addWidget(b)
                self.action_buttons.append(b)
            row.addStretch(1)
            body.addLayout(row)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 8, 10)
        lay.setSpacing(10)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(body, 1)
        lay.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 60))
        self.setGraphicsEffect(shadow)

        self.timeout = TIMEOUTS[self.kind] if timeout is None else timeout
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        if self.timeout:
            self._timer.start(self.timeout)
        theme.bus().changed.connect(self._on_theme)

    def text(self) -> str:
        return self.label.text()

    def _run(self, fn: Callable[[], None]) -> None:
        self.dismiss()
        fn()

    def _on_theme(self, _scheme: str) -> None:
        if not sip.isdeleted(self):
            self.icon.setPixmap(_glyph(self.kind))
            self.close_btn.setIcon(_close_icon())

    # Hovering holds the toast: it should not vanish while being read.
    def enterEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self.timeout:
            self._timer.start(max(1500, self.timeout // 2))
        super().leaveEvent(event)

    def dismiss(self) -> None:
        if sip.isdeleted(self) or not self.isVisible():
            return
        self._timer.stop()
        self.hide()
        self.closed.emit()
        self.deleteLater()


class ToastHost(QObject):
    """Stacks toasts in the bottom-right corner of `anchor`, newest lowest.

    The bottom, not the top: the top of the results holds Export, the
    headline cards and the gel's Expand, while the bottom holds the end of a
    table, which a passing notice can cover at no cost.
    """

    def __init__(self, anchor: QWidget) -> None:
        super().__init__(anchor)
        self.anchor = anchor
        self.toasts: list[Toast] = []
        # Everything shown, oldest first: for the log of a session and tests.
        self.history: list[tuple[str, str]] = []
        anchor.installEventFilter(self)

    def show(self, kind: str, text: str,
             actions: Sequence[tuple[str, Callable[[], None]]] = (),
             timeout: int | None = None) -> Toast:
        t = Toast(kind, text, actions, timeout, self.anchor)
        t.closed.connect(lambda t=t: self._forget(t))
        self.history.append((t.kind, text))
        self.toasts.insert(0, t)
        # The oldest go first when the stack is full; errors are kept longest.
        while len(self.toasts) > MAX_VISIBLE:
            victim = next((x for x in reversed(self.toasts) if x.kind != "error"),
                          self.toasts[-1])
            victim.dismiss()
        t.show()
        t.raise_()
        self._layout()
        return t

    def visible(self) -> list[Toast]:
        return [t for t in self.toasts if not sip.isdeleted(t) and t.isVisible()]

    def clear(self) -> None:
        for t in list(self.toasts):
            t.dismiss()

    def _forget(self, t: Toast) -> None:
        if t in self.toasts:
            self.toasts.remove(t)
        self._layout()

    def _layout(self) -> None:
        if sip.isdeleted(self.anchor):
            return
        w = min(WIDTH, max(200, self.anchor.width() - 2 * MARGIN))
        bottom = self.anchor.height() - MARGIN
        for t in self.toasts:
            if sip.isdeleted(t):
                continue
            t.setFixedWidth(w)
            h = t.heightForWidth(w) if t.hasHeightForWidth() else t.sizeHint().height()
            h = max(h, t.minimumSizeHint().height())
            t.setGeometry(self.anchor.width() - w - MARGIN, bottom - h, w, h)
            t.raise_()
            bottom -= h + 8

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.anchor and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._layout()
        return False


_BOXES = {"success": "information", "info": "information",
          "warning": "warning", "error": "critical"}


def notify(parent: QWidget | None, kind: str, text: str, title: str = "",
           actions: Sequence[tuple[str, Callable[[], None]]] = ()) -> None:
    """A toast on `parent`'s window if it has a host, else a message box."""
    host = getattr(parent.window(), "toasts", None) if parent is not None else None
    if isinstance(host, ToastHost):
        host.show(kind, f"{title}: {text}" if title and kind == "error" else text, actions)
        return
    from vpcr import APP_NAME
    getattr(QMessageBox, _BOXES.get(kind, "information"))(parent, title or APP_NAME, text)
