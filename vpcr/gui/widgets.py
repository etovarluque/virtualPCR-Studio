"""Input widgets that do not change value when the wheel rolls over them.

A QSpinBox or QComboBox inside a scroll area is a trap: scrolling the panel
past one silently edits it, and a run then uses a parameter nobody chose. Qt
wires the wheel to the value by default and offers no flag to unwire it.

The wheel is ignored unconditionally, not merely when the widget lacks focus.
Focus is no evidence of intent here: Qt hands it to the first spin box as soon
as the panel is shown, and clicking a field to type in it leaves it focused, so
a later scroll over that same field would edit it again — precisely the accident
this exists to prevent. Set these with the keyboard or the arrows.

An ignored wheel event propagates to the parent, so the panel scrolls as if the
widget were not there. WheelFocus is dropped too, or a passing wheel could give
the widget focus on its way past.

SegmentedControl is the other answer for a small integer: every choice in view
at once, one click to change, and nothing for a wheel to turn.
"""
from __future__ import annotations

from typing import Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QWheelEvent
from PyQt6.QtWidgets import (QButtonGroup, QComboBox, QDoubleSpinBox, QHBoxLayout,
                             QPushButton, QSpinBox, QWidget)


class _NoWheelMixin:
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        event.ignore()


class NoWheelSpinBox(_NoWheelMixin, QSpinBox):
    pass


class NoWheelDoubleSpinBox(_NoWheelMixin, QDoubleSpinBox):
    pass


class NoWheelComboBox(_NoWheelMixin, QComboBox):
    pass


class SegmentedControl(QWidget):
    """Joined buttons for a small range of integers, with QSpinBox's value API.

    The buttons are styled by their `segment` property (first, mid, last) in
    the theme's style sheet. Left and Right step the value from the keyboard.
    """

    valueChanged = pyqtSignal(int)

    def __init__(self, values: Sequence[int], labels: Sequence[str] = (),
                 tips: Sequence[str] = (), parent=None) -> None:
        super().__init__(parent)
        self._values = list(values)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        for i, v in enumerate(self._values):
            b = QPushButton(labels[i] if i < len(labels) else str(v))
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            # The control takes the focus, not the buttons, so the arrow keys
            # reach keyPressEvent whichever segment was clicked last.
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setProperty("segment", "first" if i == 0 else
                          "last" if i == len(self._values) - 1 else "mid")
            if i < len(tips) and tips[i]:
                b.setToolTip(tips[i])
            self._group.addButton(b, i)
            lay.addWidget(b)
        lay.addStretch(1)
        self._group.button(0).setChecked(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setProperty("focused", "false")
        self._group.idToggled.connect(
            lambda i, on: on and self.valueChanged.emit(self._values[i]))

    def value(self) -> int:
        return self._values[max(0, self._group.checkedId())]

    def setValue(self, v: int) -> None:  # noqa: N802
        """The nearest choice to `v`, as a spin box clamps to its range."""
        i = min(range(len(self._values)), key=lambda k: abs(self._values[k] - v))
        b = self._group.button(i)
        if not b.isChecked():
            b.setChecked(True)

    def minimum(self) -> int:
        return min(self._values)

    def maximum(self) -> int:
        return max(self._values)

    def button(self, i: int) -> QPushButton:
        return self._group.button(i)

    def _mark_focus(self, on: bool) -> None:
        self.setProperty("focused", "true" if on else "false")
        for b in self._group.buttons():
            b.style().unpolish(b)
            b.style().polish(b)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._mark_focus(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._mark_focus(False)
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        i = self._group.checkedId()
        if event.key() == Qt.Key.Key_Left and i > 0:
            self._group.button(i - 1).setChecked(True)
        elif event.key() == Qt.Key.Key_Right and i < len(self._values) - 1:
            self._group.button(i + 1).setChecked(True)
        else:
            super().keyPressEvent(event)
