"""The Summary tab: headline numbers, the summary table, and a virtual gel.

The gel answers at a glance what the table answers in columns: which pairs
amplify, at what size, and whether they give more than one band. One lane per
pair (two, side by side, when both engines ran), a size ladder on a log scale
as on a real agarose gel, and every product size drawn with a brightness set
by the share of sequences that yield it. A size most sequences give is a
bright band; one a handful give is a faint one; sizes that vary between
species spread into a short smear, as they would.

The gel is drawn dark in both themes, like a gel on a transilluminator.
The panel copy is small; Expand (or a double click) opens it in a window of its
own, with zoom, a size range to resolve close bands, and Copy / Save as PNG.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from statistics import median

import pandas as pd
from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QPainterPath, QShortcut
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QFrame,
                             QHBoxLayout, QLabel, QProgressBar, QPushButton,
                             QScrollArea, QSizePolicy, QSpinBox, QToolTip,
                             QVBoxLayout, QWidget)

from vpcr.core.model import PairResult

LADDER = (100, 200, 300, 400, 500, 700, 1000, 1500, 2000, 3000)
LADDER_LABELS = {100, 300, 500, 1000, 2000, 3000}

_GEL_BG = QColor("#05080b")
_LANE_BG = QColor("#0b1116")
_INK = QColor("#8494a3")
_BAND = {"jar": QColor("#bff7e4"), "thermo": QColor("#c9ddff")}
_GLOW = {"jar": QColor(54, 208, 165), "thermo": QColor(110, 160, 255)}
_LADDER_BAND = QColor("#c8d4df")


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

@dataclass
class Lane:
    pair: str
    engine: str
    n_sequences: int = 0
    n_amplified: int = 0
    # product size -> number of sequences giving it (each sequence counted
    # once per distinct size, so two equal products in one genome are one band)
    sizes: Counter = field(default_factory=Counter)

    @property
    def pct(self) -> float:
        return 100.0 * self.n_amplified / self.n_sequences if self.n_sequences else 0.0


def gel_lanes(results: list[PairResult]) -> list[Lane]:
    """One lane per (pair, engine), summed over target files, in result order."""
    lanes: dict[tuple[str, str], Lane] = {}
    for r in results:
        lane = lanes.setdefault((r.pair.name, r.engine), Lane(r.pair.name, r.engine))
        lane.n_sequences += len(r.sequences) + len(r.silent_headers)
        for s in r.sequences:
            sizes = {a.size for a in s.amplicons if a.size > 0}
            if sizes:
                lane.n_amplified += 1
            lane.sizes.update(sizes)
    return list(lanes.values())


@dataclass
class Kpi:
    title: str
    value: str
    caption: str = ""
    bar: float | None = None     # 0-100, drawn as a meter
    warn: bool = False


def kpis(summary: pd.DataFrame, results: list[PairResult]) -> list[Kpi]:
    """The four headline numbers for a finished run."""
    if summary is None or summary.empty:
        return []
    engines = {r.engine for r in results}
    main = summary[summary["engine"] == "jar"] if "engine" in summary and "jar" in engines \
        else summary
    # Sequences: per target file, counted once whatever the number of pairs.
    n_seq = int(main.groupby("target_file")["n_sequences"].max().sum())
    n_files = main["target_file"].nunique()
    out = [Kpi("Sequences", f"{n_seq:,}",
               f"in {n_files} file{'s' if n_files != 1 else ''}")]

    metric = "pct_likely" if "pct_likely" in main else "pct_amplified"
    best = main.loc[main[metric].idxmax()]
    out.append(Kpi("Best pair" if metric == "pct_amplified" else "Best pair (likely)",
                   f"{best[metric]:g}%", str(best["pair"]), bar=float(best[metric])))

    # Per pair, not summed: a sequence counts once for each pair it is run
    # with, so a total could exceed the number of sequences.
    worst = main.loc[main["n_multiband"].idxmax()]
    n_mb = int(worst["n_multiband"])
    out.append(Kpi("Multiband", f"{n_mb:,}",
                   f"sequences with >1 band size · {worst['pair']}" if n_mb
                   else "no pair gives >1 band size", warn=n_mb > 0))

    if "pct_engine_agreement" in summary:
        agree = summary.drop_duplicates(["pair", "target_file"])["pct_engine_agreement"]
        mean = float(agree.mean())
        out.append(Kpi("Engines agree", f"{mean:.0f}%", "JAR vs built-in, per sequence",
                       bar=mean, warn=mean < 90))
    elif "score_ta" in main and main["score_ta"].notna().any():
        tas = sorted(main["score_ta"].dropna().unique())
        value = f"{tas[0]:g} °C" if len(tas) == 1 else f"{tas[0]:g}–{tas[-1]:g} °C"
        out.append(Kpi("Annealing Ta", value,
                       f"{len(tas)} value{'s' if len(tas) != 1 else ''} across pairs"))
    elif main["ta_median"].notna().any():
        out.append(Kpi("Median Ta", f"{median(main['ta_median'].dropna()):g} °C",
                       "as reported by the JAR"))
    return out


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------

class KpiCard(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("kpi")
        self.title = QLabel()
        self.title.setObjectName("tileTitle")
        self.value = QLabel()
        self.value.setObjectName("kpiValue")
        self.caption = QLabel()
        self.caption.setObjectName("tileSub")
        self.caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.meter = QProgressBar()
        self.meter.setRange(0, 1000)
        self.meter.setTextVisible(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 9)
        lay.setSpacing(2)
        for w in (self.title, self.value, self.caption, self.meter):
            lay.addWidget(w)

    def set_kpi(self, k: Kpi) -> None:
        self.title.setText(k.title.upper())
        self.value.setText(k.value)
        self.value.setProperty("warn", "true" if k.warn else "false")
        self.value.style().unpolish(self.value)
        self.value.style().polish(self.value)
        self.caption.setText(k.caption)
        self.caption.setToolTip(k.caption)
        self.meter.setVisible(k.bar is not None)
        if k.bar is not None:
            self.meter.setValue(round(10 * max(0.0, min(100.0, k.bar))))


class GelView(QWidget):
    """Draws the lanes. Hover a lane for its numbers; click it to pick its row.

    `zoom` widens the lanes and, through the minimum height, stretches the size
    axis; `set_range` pins that axis to a window of sizes, so bands a few bp
    apart separate. Without a range the axis spans every product, as before.
    """

    laneClicked = pyqtSignal(str, str)    # pair, engine
    doubleClicked = pyqtSignal()
    zoomRequested = pyqtSignal(int)       # +1 / -1, from Ctrl + wheel

    LEFT, TOP, BOTTOM = 40, 30, 14
    LANE_W, GAP, PAIR_GAP = 26, 6, 14
    BASE_HEIGHT = 140

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.lanes: list[Lane] = []
        self.lo, self.hi = 80, 3000
        self._auto = (80, 3000)
        self._range: tuple[float, float] | None = None
        self.zoom = 1.0
        self.setMouseTracking(True)
        self.setMinimumHeight(self.BASE_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._xs: list[float] = []

    def set_lanes(self, lanes: list[Lane]) -> None:
        self.lanes = lanes
        sizes = [s for ln in lanes for s in ln.sizes]
        self._auto = (min([80, *(s * 0.8 for s in sizes)]),
                      max([3000, *(s * 1.25 for s in sizes)]))
        self._apply_range()

    def auto_range(self) -> tuple[float, float]:
        """The span the gel takes with no range set: every product, padded."""
        return self._auto

    def set_range(self, lo: float | None, hi: float | None) -> None:
        """Pin the size axis to [lo, hi] bp; None for either restores auto."""
        self._range = (lo, hi) if lo is not None and hi is not None and hi > lo else None
        self._apply_range()

    def _apply_range(self) -> None:
        self.lo, self.hi = self._range or self._auto
        self._layout_lanes()
        self.updateGeometry()
        self.update()

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.5, min(6.0, zoom))
        self.setMinimumHeight(round(self.BASE_HEIGHT * self.zoom))
        self._layout_lanes()
        self.updateGeometry()
        self.update()

    # Lane geometry scales with the zoom; the ladder labels' margin does not.
    @property
    def lane_w(self) -> float:
        return self.LANE_W * self.zoom

    def _layout_lanes(self) -> None:
        z = self.zoom
        x = self.LEFT + self.lane_w + self.PAIR_GAP * z      # after the ladder
        self._xs = []
        prev = None
        for ln in self.lanes:
            if prev is not None:
                x += (self.GAP if ln.pair == prev else self.PAIR_GAP) * z
            self._xs.append(x)
            x += self.lane_w
            prev = ln.pair
        self._width = int(x + 12)

    def sizeHint(self) -> QSize:  # noqa: N802
        self._layout_lanes()
        return QSize(max(220, self._width), max(300, self.minimumHeight()))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    # -- geometry ------------------------------------------------------------

    def _y(self, bp: float) -> float:
        h = self.height() - self.TOP - self.BOTTOM
        lo, hi = math.log10(self.lo), math.log10(self.hi)
        return self.TOP + (hi - math.log10(bp)) / (hi - lo) * h

    def _lane_at(self, x: float) -> int | None:
        for i, lx in enumerate(self._xs):
            if lx <= x <= lx + self.lane_w:
                return i
        return None

    # -- painting ------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 8, 8)
        p.fillPath(path, _GEL_BG)

        small = QFont(self.font())
        small.setPointSizeF(max(6.5, small.pointSizeF() - 2))
        p.setFont(small)
        # Bands thicken a little with the zoom, never in step with it: a thick
        # band would cover its neighbours, which is what zooming is for.
        bw = math.sqrt(self.zoom)

        # ladder: with a narrow range, label every rung that is on screen
        lx = self.LEFT
        self._lane_bg(p, lx, "L")
        rungs = [bp for bp in LADDER if self.lo <= bp <= self.hi]
        labelled = set(rungs) if len(rungs) <= 6 else LADDER_LABELS
        for bp in rungs:
            y = self._y(bp)
            p.fillRect(QRectF(lx + 3, y - bw, self.lane_w - 6, 2 * bw), _LADDER_BAND)
            if bp in labelled:
                p.setPen(_INK)
                p.drawText(QRectF(2, y - 7, self.LEFT - 6, 14),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           f"{bp:,}")

        # A narrow range leaves one or two rungs; finer ticks on the axis keep
        # sizes readable. They are only marks, not bands: a ladder is a fixed set.
        if len(rungs) < 4:
            span = self.hi - self.lo
            step = next((st for st in (5, 10, 20, 25, 50, 100, 200, 250, 500)
                         if span / st <= 10), 1000)
            dim = QColor(_INK)
            dim.setAlphaF(0.7)
            p.setPen(dim)
            t = math.ceil(self.lo / step) * step
            while t <= self.hi:
                y = self._y(t)
                if all(abs(y - self._y(bp)) > 12 for bp in rungs):
                    p.fillRect(QRectF(self.LEFT - 4, y - 0.5, 4, 1), dim)
                    p.drawText(QRectF(2, y - 7, self.LEFT - 8, 14),
                               Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                               f"{t:,}")
                t += step

        if not self.lanes:
            p.setPen(_INK)
            p.drawText(QRectF(self.LEFT + self.LANE_W, 0, 200, self.height()),
                       Qt.AlignmentFlag.AlignCenter, "Run to see the gel")
            return

        engines = {ln.engine for ln in self.lanes}
        for i, (ln, x) in enumerate(zip(self.lanes, self._xs)):
            tag = str(self._pair_number(i))
            if len(engines) > 1:
                tag += "J" if ln.engine == "jar" else "B"
            self._lane_bg(p, x, tag)
            if not ln.n_sequences:
                continue
            band, glow = _BAND.get(ln.engine, _BAND["jar"]), _GLOW.get(ln.engine, _GLOW["jar"])
            for size, n in sorted(ln.sizes.items(), key=lambda kv: kv[1]):
                share = n / ln.n_sequences
                # Square-root brightness: a 1% band stays visible next to a 90% one.
                a = 0.12 + 0.88 * math.sqrt(share)
                y = self._y(size)
                if not (self.lo <= size <= self.hi):
                    continue
                g = QColor(glow)
                g.setAlphaF(0.35 * a)
                p.fillRect(QRectF(x + 1, y - 3.5 * bw, self.lane_w - 2, 7 * bw), g)
                c = QColor(band)
                c.setAlphaF(a)
                p.fillRect(QRectF(x + 3, y - 1.5 * bw, self.lane_w - 6, 3 * bw), c)

    def _pair_number(self, i: int) -> int:
        n, prev = 0, None
        for ln in self.lanes[: i + 1]:
            if ln.pair != prev:
                n += 1
            prev = ln.pair
        return n

    def _lane_bg(self, p: QPainter, x: float, label: str) -> None:
        w = self.lane_w
        p.fillRect(QRectF(x, self.TOP - 6, w, self.height() - self.TOP - 2), _LANE_BG)
        p.fillRect(QRectF(x + 3, self.TOP - 10, w - 6, 4), QColor("#1a232b"))  # well
        p.setPen(_INK)
        p.drawText(QRectF(x - 4, 2, w + 8, 14), Qt.AlignmentFlag.AlignCenter, label)

    # -- interaction ---------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        i = self._lane_at(event.position().x())
        if i is None:
            QToolTip.hideText()
            return
        ln = self.lanes[i]
        top = ", ".join(f"{s} bp ({100 * n / ln.n_sequences:.0f}%)"
                        for s, n in ln.sizes.most_common(3)) if ln.n_sequences else ""
        engine = "virtualPCR JAR" if ln.engine == "jar" else "built-in engine"
        QToolTip.showText(event.globalPosition().toPoint(),
                          f"{ln.pair}\n{engine}\n{ln.pct:.1f}% of {ln.n_sequences:,} "
                          f"sequences amplify" + (f"\nmain sizes: {top}" if top else ""), self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        i = self._lane_at(event.position().x())
        if i is not None and event.button() == Qt.MouseButton.LeftButton:
            self.laneClicked.emit(self.lanes[i].pair, self.lanes[i].engine)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoomRequested.emit(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
        else:
            super().wheelEvent(event)


def _legend_text(lanes: list[Lane]) -> str:
    names = list(dict.fromkeys(ln.pair for ln in lanes))
    text = "   ".join(f"{i}  {n}" for i, n in enumerate(names, 1))
    if len({ln.engine for ln in lanes}) > 1:
        text += "\nJ = virtualPCR JAR · B = built-in engine"
    return text


class GelWindow(QDialog):
    """The gel in a window of its own: resizable, zoomable, and exportable.

    Not modal, so it can sit on a second screen beside the results while the
    table is used; it follows every new run until it is closed.
    """

    laneClicked = pyqtSignal(str, str)
    ZOOMS = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Virtual gel")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.resize(760, 640)

        self.gel = GelView()
        self.gel.laneClicked.connect(self.laneClicked)
        self.gel.zoomRequested.connect(self._step_zoom)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.gel)
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.zoom_out = self._button("−", "Zoom out (Ctrl + wheel, Ctrl+−)", lambda: self._step_zoom(-1))
        self.zoom_label = QLabel()
        self.zoom_label.setMinimumWidth(44)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_in = self._button("+", "Zoom in (Ctrl + wheel, Ctrl++)", lambda: self._step_zoom(1))
        self.fit_btn = self._button("Fit", "Fit the gel to the window (Ctrl+0)", self.fit)
        for w in (self.zoom_out, self.zoom_label, self.zoom_in, self.fit_btn):
            bar.addWidget(w)
        bar.addSpacing(16)

        self.range_box = QCheckBox("Size range")
        self.range_box.setToolTip("Show only this window of product sizes, so bands a few bp apart separate")
        self.lo_spin, self.hi_spin = QSpinBox(), QSpinBox()
        for sp, tip in ((self.lo_spin, "Smallest size shown"), (self.hi_spin, "Largest size shown")):
            sp.setRange(10, 100_000)
            sp.setSuffix(" bp")
            sp.setSingleStep(50)
            sp.setToolTip(tip)
            sp.setEnabled(False)
            sp.valueChanged.connect(self._apply_range)
        self.range_box.toggled.connect(self._on_range_toggled)
        bar.addWidget(self.range_box)
        bar.addWidget(self.lo_spin)
        bar.addWidget(QLabel("to"))
        bar.addWidget(self.hi_spin)
        bar.addStretch(1)
        bar.addWidget(self._button("Copy", "Copy the gel image to the clipboard", self.copy_image))
        bar.addWidget(self._button("Save PNG…", "Save the gel as a PNG image", self.save_image))

        self.legend = QLabel()
        self.legend.setObjectName("tileSub")
        self.legend.setWordWrap(True)
        self.legend.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addLayout(bar)
        lay.addWidget(self.scroll, 1)
        lay.addWidget(self.legend)

        for keys, fn in ((QKeySequence.StandardKey.ZoomIn, lambda: self._step_zoom(1)),
                         (QKeySequence.StandardKey.ZoomOut, lambda: self._step_zoom(-1)),
                         (QKeySequence("Ctrl+0"), self.fit)):
            QShortcut(QKeySequence(keys), self, activated=fn)
        self._update_zoom_label()

    @staticmethod
    def _button(text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setProperty("compact", "true")
        b.setToolTip(tip)
        b.setAutoDefault(False)
        b.clicked.connect(slot)
        return b

    # -- content --------------------------------------------------------------

    def set_lanes(self, lanes: list[Lane]) -> None:
        self.gel.set_lanes(lanes)
        lo, hi = self.gel.auto_range()
        if not self.range_box.isChecked():
            # Seed the fields with the auto span, so ticking the box starts
            # from what is on screen rather than from arbitrary numbers.
            for sp, v in ((self.lo_spin, lo), (self.hi_spin, hi)):
                sp.blockSignals(True)
                sp.setValue(round(v))
                sp.blockSignals(False)
        self.legend.setText(_legend_text(lanes))

    # -- zoom -----------------------------------------------------------------

    def set_zoom(self, z: float) -> None:
        self.gel.set_zoom(z)
        self._update_zoom_label()

    def _step_zoom(self, step: int) -> None:
        z = self.gel.zoom
        if step > 0:
            nxt = next((v for v in self.ZOOMS if v > z + 1e-6), self.ZOOMS[-1])
        else:
            nxt = next((v for v in reversed(self.ZOOMS) if v < z - 1e-6), self.ZOOMS[0])
        self.set_zoom(nxt)

    def fit(self) -> None:
        """The largest zoom at which every lane fits the window's width."""
        self.gel.set_zoom(1.0)
        natural = self.gel.sizeHint().width()
        room = self.scroll.viewport().width() - 4
        z = 1.0 if natural <= 0 else room / natural
        self.set_zoom(max(self.ZOOMS[0], min(self.ZOOMS[-1], z)))

    def _update_zoom_label(self) -> None:
        self.zoom_label.setText(f"{self.gel.zoom * 100:.0f}%")
        self.zoom_out.setEnabled(self.gel.zoom > self.ZOOMS[0] + 1e-6)
        self.zoom_in.setEnabled(self.gel.zoom < self.ZOOMS[-1] - 1e-6)

    # -- range ----------------------------------------------------------------

    def _on_range_toggled(self, on: bool) -> None:
        self.lo_spin.setEnabled(on)
        self.hi_spin.setEnabled(on)
        self._apply_range()

    def _apply_range(self) -> None:
        if self.range_box.isChecked():
            self.gel.set_range(self.lo_spin.value(), self.hi_spin.value())
        else:
            self.gel.set_range(None, None)

    # -- export ---------------------------------------------------------------

    def image(self):
        return self.gel.grab()

    def copy_image(self) -> None:
        QApplication.clipboard().setPixmap(self.image())

    def save_image(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save gel image", "virtual_gel.png",
                                              "PNG image (*.png)")
        if path:
            self.image().save(path, "PNG")


class SummaryPage(QWidget):
    """KPI cards over the summary table, with the gel at its side."""

    laneClicked = pyqtSignal(str, str)

    def __init__(self, table: QWidget, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        self.cards_host = QWidget()
        cards = QHBoxLayout(self.cards_host)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setSpacing(8)
        self.cards = [KpiCard() for _ in range(4)]
        for c in self.cards:
            cards.addWidget(c, 1)
        self.cards_host.setVisible(False)
        lay.addWidget(self.cards_host)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(table, 1)

        gel_card = QFrame()
        gel_card.setObjectName("kpi")
        gl = QVBoxLayout(gel_card)
        gl.setContentsMargins(10, 8, 10, 10)
        head = QLabel("VIRTUAL GEL")
        head.setObjectName("tileTitle")
        head.setToolTip("One lane per pair (J = JAR, B = built-in when comparing).\n"
                        "Band brightness = share of sequences giving that size.\n"
                        "Hover a lane for its numbers; click it to select its row.")
        self.expand_btn = QPushButton("Expand")
        self.expand_btn.setProperty("compact", "true")
        self.expand_btn.setToolTip("Open the gel in its own window, with zoom and export "
                                   "(or double-click the gel)")
        self.expand_btn.clicked.connect(self.open_gel_window)
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.addWidget(head)
        head_row.addStretch(1)
        head_row.addWidget(self.expand_btn)
        gl.addLayout(head_row)
        self.gel = GelView()
        self.gel.laneClicked.connect(self.laneClicked)
        self.gel.doubleClicked.connect(self.open_gel_window)
        self.gel_window: GelWindow | None = None
        self._lanes: list[Lane] = []
        scroll = QScrollArea()
        scroll.setWidget(self.gel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(240)
        scroll.setMaximumWidth(420)
        self._gel_scroll = scroll
        gl.addWidget(scroll, 1)
        self.legend = QLabel("")
        self.legend.setObjectName("tileSub")
        self.legend.setWordWrap(True)
        self.legend.setMaximumWidth(420)
        gl.addWidget(self.legend)
        row.addWidget(gel_card)
        lay.addLayout(row, 1)

    def set_results(self, summary: pd.DataFrame | None, results: list[PairResult]) -> None:
        ks = kpis(summary, results) if results else []
        self.cards_host.setVisible(bool(ks))
        for card, k in zip(self.cards, ks):
            card.set_kpi(k)
            card.setVisible(True)
        for card in self.cards[len(ks):]:
            card.setVisible(False)
        lanes = gel_lanes(results) if results else []
        self._lanes = lanes
        self.gel.set_lanes(lanes)
        self._gel_scroll.setFixedWidth(min(420, self.gel.sizeHint().width() + 4))
        self.legend.setText(_legend_text(lanes))
        if self.gel_window is not None:
            self.gel_window.set_lanes(lanes)

    def open_gel_window(self) -> GelWindow:
        """Show the floating gel, creating it on first use; raise it after."""
        if self.gel_window is None:
            self.gel_window = GelWindow(self.window())
            self.gel_window.laneClicked.connect(self.laneClicked)
            self.gel_window.set_lanes(self._lanes)
        self.gel_window.show()
        self.gel_window.raise_()
        self.gel_window.activateWindow()
        return self.gel_window
