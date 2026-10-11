"""The setup strip: one tile per thing a run needs, and the chip bar.

A tile summarises its section (what is loaded, what is set) and carries a
check once the section is ready. Clicking it opens the section's form in the
drawer below the strip; clicking it again closes it. Tiles that take files
(targets, primers) also accept them dropped straight onto the tile, with the
same accept/refuse feedback as the drop zones in dnd.py.

The drawer floats over the results rather than pushing them down, so the
results keep one height whichever form is open. The Scrim dims what it covers
and closes the drawer when clicked.
"""
from __future__ import annotations

from typing import Sequence

from PyQt6.QtCore import QPoint, QPointF, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter
from PyQt6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QLayout,
                             QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from vpcr.gui import theme
from vpcr.gui.dnd import _DropMixin


def _repolish(w: QWidget) -> None:
    """Re-evaluate the style sheet after a dynamic property changed."""
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


class SetupTile(_DropMixin, QFrame):
    clicked = pyqtSignal()
    filesDropped = pyqtSignal(list)   # list[Path]

    def __init__(self, title: str, suffixes: Sequence[str] = (),
                 dir_suffixes: Sequence[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(150)
        self._suffixes = tuple(suffixes)
        self._dir_suffixes = dir_suffixes
        self.setAcceptDrops(bool(suffixes))

        self.title = QLabel(title.upper())
        self.title.setObjectName("tileTitle")
        self.check = QLabel("✓")
        self.check.setObjectName("tileCheck")
        self.check.setFixedSize(16, 16)
        self.check.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.check.setVisible(False)
        self.main = QLabel("")
        self.main.setObjectName("tileMain")
        self.sub = QLabel("")
        self.sub.setObjectName("tileSub")
        for lab in (self.main, self.sub):
            # Summaries must never widen the strip: they elide via setText.
            lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._texts = ("", "")

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.check)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 9)
        lay.setSpacing(2)
        lay.addLayout(top)
        lay.addWidget(self.main)
        lay.addWidget(self.sub)

    # -- content -------------------------------------------------------------

    def set_summary(self, main: str, sub: str = "", tip: str = "") -> None:
        self._texts = (main, sub)
        self.setToolTip(tip or "\n".join(t for t in (main, sub) if t))
        self._elide()

    def summary_text(self) -> str:
        return " • ".join(t for t in self._texts if t)

    def set_ready(self, ready: bool) -> None:
        self.check.setVisible(ready)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        _repolish(self)

    def _elide(self) -> None:
        for lab, text in zip((self.main, self.sub), self._texts):
            fm = lab.fontMetrics()
            lab.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0, lab.width())))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    # -- interaction -----------------------------------------------------------

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            return
        super().keyPressEvent(event)

    def set_drag_state(self, state: str) -> None:
        self.setProperty("drag", state)
        _repolish(self)

    def dropEvent(self, event) -> None:  # noqa: N802
        self.set_drag_state("none")
        paths = self._paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.filesDropped.emit(paths)


class FlowLayout(QLayout):
    """Lays its items out left to right, wrapping to a new line when full."""

    def __init__(self, parent=None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, dry=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _layout(self, rect: QRect, dry: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_h > 0:
                x, y = rect.x(), y + line_h + self._spacing
                line_h = 0
            if not dry:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class ChipBar(QWidget):
    """Mutually exclusive filter chips, with a QComboBox-like index API."""

    currentIndexChanged = pyqtSignal(int)

    def __init__(self, labels: Sequence[str], tips: Sequence[str] = (), parent=None) -> None:
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        lay = FlowLayout(self, spacing=6)
        for i, text in enumerate(labels):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setProperty("chip", "true")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if i < len(tips) and tips[i]:
                b.setToolTip(tips[i])
            self._group.addButton(b, i)
            lay.addWidget(b)
        if labels:
            self._group.button(0).setChecked(True)
        self._group.idToggled.connect(self._on_toggled)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def _on_toggled(self, idx: int, checked: bool) -> None:
        if checked:
            self.currentIndexChanged.emit(idx)

    def currentIndex(self) -> int:  # noqa: N802
        return self._group.checkedId()

    def setCurrentIndex(self, idx: int) -> None:  # noqa: N802
        b = self._group.button(idx)
        if b is not None and not b.isChecked():
            b.setChecked(True)

    def count(self) -> int:
        return len(self._group.buttons())

    def itemText(self, idx: int) -> str:  # noqa: N802
        b = self._group.button(idx)
        return b.text() if b else ""


class Scrim(QWidget):
    """The veil under the floating drawer: dims the results, closes on a click.

    Its top edge carries a short shadow, which is what tells the eye the
    drawer lies on top of the results rather than beside them. Painted here
    instead of a QGraphicsDropShadowEffect on the drawer: an effect renders
    the drawer and every field in it through an offscreen pixmap, on each
    repaint of any of them.
    """

    clicked = pyqtSignal()
    SHADOW = 14

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.hide()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        dark = theme.scheme() == "dark"
        p.fillRect(self.rect(), QColor(0, 0, 0, 110 if dark else 46))
        g = QLinearGradient(QPointF(0, 0), QPointF(0, self.SHADOW))
        g.setColorAt(0, QColor(0, 0, 0, 120 if dark else 60))
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(0, 0, self.width(), self.SHADOW, g)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.accept()      # the results underneath do not scroll while dimmed
