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
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox


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
