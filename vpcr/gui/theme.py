"""Light and dark themes, from one set of colour tokens.

Every colour the app paints comes from TOKENS: the Qt palette (which Fusion
and the drop zones draw from), the application style sheet, the result-table
highlights, and the HTML of the Alignment tab. Switching theme re-applies all
of them and emits `bus().changed`, so views that render their own content
(Alignment, Primer map) can redraw.

The mode is 'system', 'light' or 'dark'. 'system' follows Windows' app
setting through Qt's style hints (Qt 6.5+), and keeps following it live.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QObject, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QStyleFactory

MODES = ("system", "light", "dark")

TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#f5f6f4",        # window
        "panel": "#ffffff",     # strip, drawers, status bar
        "card": "#ffffff",      # tiles, inputs, tables
        "alt": "#f8f9f7",       # alternate table rows
        "line": "#e1e5e1",
        "ink": "#1d2421",
        "muted": "#66716b",
        "accent": "#0f7a5c",
        "accent_ink": "#ffffff",
        "accent_soft": "#e2f2eb",
        "chip": "#eef0ed",
        "good": "#1f8a4c", "good_bg": "#dcf2e3", "good_bg2": "#ecf7e1",
        "warn": "#9a5b00", "warn_bg": "#fcefd6",
        "bad": "#b42318", "bad_bg": "#fbe3e1",
        "fwd_bg": "#cde7d3", "fwd_ink": "#10331b",
        "rev_bg": "#cfe0f5", "rev_ink": "#0e2a45",
        "mark_bg": "#ffe08a",
    },
    "dark": {
        "bg": "#0f1417",
        "panel": "#151c20",
        "card": "#1a2227",
        "alt": "#1e272c",
        "line": "#2b363c",
        "ink": "#e3eae7",
        "muted": "#8c9a95",
        "accent": "#3fcf9f",
        "accent_ink": "#062519",
        "accent_soft": "#173a2f",
        "chip": "#222c32",
        "good": "#5fd3a3", "good_bg": "#173a2c", "good_bg2": "#1f3626",
        "warn": "#f2c160", "warn_bg": "#3b301a",
        "bad": "#ff8a80", "bad_bg": "#43231f",
        "fwd_bg": "#1f4a2e", "fwd_ink": "#d4f3dd",
        "rev_bg": "#1e3a5c", "rev_ink": "#d6e6fa",
        "mark_bg": "#7a5c00",
    },
}


class _Bus(QObject):
    changed = pyqtSignal(str)   # the resolved scheme: 'light' or 'dark'


_bus: _Bus | None = None
_mode = "system"
_scheme = "light"


def bus() -> _Bus:
    global _bus
    if _bus is None:
        _bus = _Bus()
    return _bus


def mode() -> str:
    return _mode


def scheme() -> str:
    """The theme in effect: 'light' or 'dark'."""
    return _scheme


def tok(name: str) -> str:
    """A token's hex value in the theme in effect."""
    return TOKENS[_scheme][name]


def color(name: str) -> QColor:
    return QColor(tok(name))


def toggle_icon(show_sun: bool) -> QIcon:
    """A sun (switch to light) or a moon (switch to dark), drawn in the ink colour.

    Drawn rather than taken from a font: the U+2600/U+263E glyphs fall back to
    a colour emoji on Windows, or to nothing at all.
    """
    px = QPixmap(40, 40)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    ink = color("ink")
    c = QPointF(20, 20)
    if show_sun:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawEllipse(c, 7, 7)
        pen = QPen(ink, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for k in range(8):
            a = k * math.pi / 4
            p.drawLine(QPointF(20 + 11 * math.cos(a), 20 + 11 * math.sin(a)),
                       QPointF(20 + 16 * math.cos(a), 20 + 16 * math.sin(a)))
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawEllipse(c, 14, 14)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.drawEllipse(QPointF(28, 13), 12, 12)
    p.end()
    return QIcon(px)


def _system_scheme() -> str:
    hints = QGuiApplication.styleHints()
    try:
        return "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
    except AttributeError:      # Qt older than 6.5
        return "light"


def _palette(t: dict[str, str]) -> QPalette:
    c = {k: QColor(v) for k, v in t.items()}
    p = QPalette()
    R = QPalette.ColorRole
    for role, key in ((R.Window, "bg"), (R.WindowText, "ink"), (R.Base, "card"),
                      (R.AlternateBase, "alt"), (R.Text, "ink"), (R.Button, "card"),
                      (R.ButtonText, "ink"), (R.Highlight, "accent"),
                      (R.HighlightedText, "accent_ink"), (R.ToolTipBase, "card"),
                      (R.ToolTipText, "ink"), (R.PlaceholderText, "muted"),
                      (R.Link, "accent"), (R.BrightText, "accent_ink")):
        p.setColor(role, c[key])
    # Bevel roles, which Fusion uses for frames and separators.
    p.setColor(R.Light, c["card"])
    p.setColor(R.Midlight, c["alt"])
    p.setColor(R.Mid, c["line"])
    p.setColor(R.Dark, c["line"].darker(130) if _scheme == "light" else c["line"].lighter(140))
    p.setColor(R.Shadow, QColor(0, 0, 0, 60))
    dis = QPalette.ColorGroup.Disabled
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(dis, role, c["muted"])
    p.setColor(dis, R.Button, c["chip"])
    p.setColor(dis, R.Base, c["bg"])
    return p


def stylesheet(t: dict[str, str]) -> str:
    """Application-wide QSS. Spin boxes and combo boxes are left to Fusion:
    styling their frames in QSS makes Qt drop their native arrows."""
    return f"""
    QMainWindow, QDialog {{ background: {t['bg']}; }}
    QToolTip {{ background: {t['card']}; color: {t['ink']}; border: 1px solid {t['line']};
               padding: 4px 6px; }}
    QMenuBar {{ background: {t['panel']}; border-bottom: 1px solid {t['line']}; }}
    QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
    QMenuBar::item:selected {{ background: {t['chip']}; border-radius: 4px; }}
    QMenu {{ background: {t['card']}; border: 1px solid {t['line']}; padding: 4px; }}
    QMenu::item {{ padding: 5px 22px 5px 22px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {t['accent_soft']}; color: {t['ink']}; }}
    QStatusBar {{ background: {t['panel']}; border-top: 1px solid {t['line']};
                 color: {t['muted']}; }}

    QPushButton {{ background: {t['card']}; color: {t['ink']}; border: 1px solid {t['line']};
                  border-radius: 6px; padding: 5px 14px; }}
    QPushButton:hover {{ border-color: {t['accent']}; }}
    QPushButton:pressed {{ background: {t['accent_soft']}; }}
    QPushButton:disabled {{ color: {t['muted']}; background: {t['bg']}; }}
    QPushButton[primary="true"] {{ background: {t['accent']}; color: {t['accent_ink']};
                  border: 1px solid {t['accent']}; font-weight: 600; }}
    QPushButton[primary="true"]:hover {{ background: {QColor(t['accent']).lighter(110).name()}; }}
    QPushButton[primary="true"]:disabled {{ background: {t['chip']}; color: {t['muted']};
                  border-color: {t['line']}; }}
    QPushButton[compact="true"] {{ padding: 2px 12px; }}
    QPushButton[chip="true"] {{ border-radius: 12px; padding: 3px 11px; color: {t['muted']}; }}
    QPushButton[chip="true"]:checked {{ background: {t['ink']}; color: {t['bg']};
                  border-color: {t['ink']}; }}
    QToolButton#themeToggle {{ border: 1px solid {t['line']}; border-radius: 6px;
                  padding: 3px; background: {t['card']}; }}
    QToolButton#themeToggle:hover {{ border-color: {t['accent']}; }}

    QLineEdit, QPlainTextEdit, QTextBrowser, QListWidget {{ background: {t['card']};
                  border: 1px solid {t['line']}; border-radius: 6px; padding: 3px 6px;
                  selection-background-color: {t['accent']}; selection-color: {t['accent_ink']}; }}
    QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {t['accent']}; }}
    QListWidget {{ padding: 2px; }}

    QFrame#strip {{ background: {t['panel']}; border-bottom: 1px solid {t['line']}; }}
    QFrame#drawer {{ background: {t['panel']}; border-bottom: 1px solid {t['line']}; }}
    QFrame#tile {{ background: {t['card']}; border: 1px solid {t['line']}; border-radius: 8px; }}
    QFrame#tile:hover {{ border-color: {t['accent']}; }}
    QFrame#tile[active="true"] {{ border: 2px solid {t['accent']}; }}
    QFrame#tile[drag="accept"] {{ border: 2px dashed {t['accent']}; background: {t['accent_soft']}; }}
    QFrame#tile[drag="reject"] {{ border: 2px dashed {t['bad']}; }}
    QFrame#kpi {{ background: {t['card']}; border: 1px solid {t['line']}; border-radius: 8px; }}
    QLabel#kpiValue {{ font-size: 20px; font-weight: 700; color: {t['ink']}; }}
    QLabel#kpiValue[warn="true"] {{ color: {t['warn']}; }}
    QLabel#tileTitle {{ color: {t['muted']}; font-size: 10px; font-weight: 600;
                  letter-spacing: 1px; }}
    QLabel#tileMain {{ font-size: 13px; font-weight: 600; }}
    QLabel#tileSub, QLabel#muted {{ color: {t['muted']}; }}
    QLabel#tileCheck {{ background: {t['accent']}; color: {t['accent_ink']}; border-radius: 8px;
                  font-size: 10px; font-weight: 700; }}
    QLabel#groupTitle {{ font-weight: 600; color: {t['ink']}; }}

    QTabWidget::pane {{ border: 1px solid {t['line']}; border-radius: 8px; top: -1px;
                  background: {t['card']}; }}
    QTabBar::tab {{ background: transparent; color: {t['muted']}; padding: 5px 14px;
                  margin-right: 2px; border-radius: 6px; }}
    QTabBar::tab:selected {{ background: {t['card']}; color: {t['ink']}; font-weight: 600;
                  border: 1px solid {t['line']}; }}
    QTabBar::tab:hover:!selected {{ color: {t['ink']}; }}

    QTableView {{ background: {t['card']}; alternate-background-color: {t['alt']};
                  gridline-color: {t['line']}; border: none;
                  selection-background-color: {t['accent_soft']}; selection-color: {t['ink']}; }}
    QHeaderView::section {{ background: {t['panel']}; color: {t['muted']}; border: none;
                  border-bottom: 1px solid {t['line']}; border-right: 1px solid {t['line']};
                  padding: 4px 6px; font-weight: 600; }}
    QTableCornerButton::section {{ background: {t['panel']}; border: none; }}

    QProgressBar {{ background: {t['chip']}; border: none; border-radius: 2px; max-height: 4px; }}
    QProgressBar::chunk {{ background: {t['accent']}; border-radius: 2px; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
    QScrollBar::handle {{ background: {t['line']}; border-radius: 4px; min-height: 24px;
                  min-width: 24px; margin: 2px; }}
    QScrollBar::handle:hover {{ background: {t['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QSplitter::handle {{ background: {t['line']}; }}
    """


def apply(app: QApplication, new_mode: str | None = None) -> str:
    """Apply `new_mode` (or the current one again). Returns the scheme in effect."""
    global _mode, _scheme
    if new_mode is not None:
        _mode = new_mode if new_mode in MODES else "system"
    _scheme = _system_scheme() if _mode == "system" else _mode
    t = TOKENS[_scheme]
    if app.style().name().lower() != "fusion":
        app.setStyle(QStyleFactory.create("Fusion"))
    app.setPalette(_palette(t))
    app.setStyleSheet(stylesheet(t))
    bus().changed.emit(_scheme)
    return _scheme


def follow_system(app: QApplication) -> None:
    """Re-apply when Windows switches between light and dark, in 'system' mode."""
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda *_: apply(app) if _mode == "system" else None)
