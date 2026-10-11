"""PyQt6 front end for in silico PCR.

Layout: a navigation rail down the left edge (the results views, and the
theme toggle at its foot), a strip of setup tiles across the top (targets,
primers, parameters, output, and Run), a drawer that opens the clicked tile's
form over the results, and the results below, at one height whatever is open. News goes to toasts in the corner
of the results (gui/toast.py); message boxes are kept for questions. The JAR runs in a worker thread; the
batch itself is parallel, so the GUI thread only marshals progress.

Choosing primers happens in its own window (gui/primer_dialog.py), so the
Primers drawer holds a summary and a drop zone rather than three tables.
Colours come from gui/theme.py, light or dark.
"""
from __future__ import annotations

import sys
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

import pandas as pd
from PyQt6 import sip
from PyQt6.QtCore import (QAbstractTableModel, QModelIndex, QPoint, QSettings, QSize,
                          Qt, QThread, QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                             QComboBox, QDialog, QFileDialog, QFormLayout,
                             QFrame, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMainWindow, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton,
                             QScrollArea, QSizePolicy, QStackedWidget, QTabWidget,
                             QTableView, QToolButton, QVBoxLayout, QWidget)

from vpcr import APP_NAME, APP_ROOT, JAR_DIR
from vpcr.gui import theme
from vpcr.gui.alignment_view import AlignmentTab
from vpcr.gui.primer_map import PrimerMapTab
from vpcr.gui.dnd import (FASTA_DIR_SUFFIXES, FASTA_SUFFIXES, JAR_SUFFIXES,
                          PRIMER_SUFFIXES, DropDirLineEdit, DropLineEdit,
                          DropListWidget, DropZone)
from vpcr.gui.primer_dialog import PrimerDialog, PrimerState
from vpcr.gui.primer_io import load_pairs_interactive
from vpcr.gui.setup_strip import ChipBar, Scrim, SetupTile
from vpcr.gui.nav_rail import NavRail, compact_count
from vpcr.gui.summary_view import SummaryPage
from vpcr.gui.toast import ToastHost
from vpcr.gui.widgets import (NoWheelComboBox, NoWheelDoubleSpinBox,
                             NoWheelSpinBox, SegmentedControl)
from vpcr.core.export import (EXCEL_ROW_LIMIT, OutputCheck, check_output_dir,
                              detail_table, export_run, run_folder_name,
                              summary_table, unique_dir, write_tables)
from vpcr.core.model import STUDIO_DEFAULTS, PrimerPair, RunConfig
from vpcr.core.primers import MIN_PRIMER_LEN
from vpcr.core import runner as _runner
from vpcr.core.runner import (JarError, discover_javas, jar_class_version,
                              run_batch, select_java)
from vpcr.core.scoring import (DEFAULT_POLYMERASE, POLYMERASES, Reaction,
                               resolve_ta)

# The results views, in tab order: rail key, rail label, page title, tooltip.
VIEWS = (
    ("summary", "Summary", "Summary", "Headline numbers, one row per pair and file, and the virtual gel"),
    ("detail", "Detail", "Detail", "One row per sequence and product, with filters"),
    ("align", "Align", "Alignment", "Primers drawn on the amplified region of a sequence"),
    ("map", "Map", "Primer map", "Every primer's footprint on a chosen target"),
    ("log", "Log", "Log", "What each run did, as archived in run_log.txt"),
)

# The target list in its drawer: five rows, then it scrolls.
TARGET_LIST_HEIGHT = 140

# The Annealing Ta spin box shows "auto" at its minimum: each pair then uses its
# own Ta from the primer file, else its lower Tm plus the polymerase's offset.
TA_AUTO = 39.0

# Engine choices: label shown, run_batch's key, and what each one is for.
ENGINE_CHOICES = (
    ("virtualPCR JAR", "jar",
     "The JAR's in silico PCR, with the biological score on top."),
    ("Built-in (thermodynamic)", "thermo",
     "An independent engine: seed-and-extend search scored by duplex\n"
     "thermodynamics and 3' mismatches. Needs no Java."),
    ("Both (compare)", "both",
     "Runs every pair through both engines and writes\n"
     "engine_comparison.csv; filter Detail on 'Engines disagree'."),
)

# The single source of truth for the Parameters section: the widgets are built
# empty and filled from here, so "Reset to defaults" cannot drift from what the
# app starts with. The run parameters come from STUDIO_DEFAULTS, which the CLI
# shares, so both routes give the same result for the same inputs.
PARAM_DEFAULTS: dict[str, object] = {
    "min_len": STUDIO_DEFAULTS["min_len"],
    "max_len": STUDIO_DEFAULTS["max_len"],
    "err3": STUDIO_DEFAULTS["n_3prime_errors"],
    "workers": STUDIO_DEFAULTS["workers"],
    "heap": "",
    "circular": STUDIO_DEFAULTS["circular"],
    "engine": ENGINE_CHOICES[0][0],
    "polymerase": POLYMERASES[DEFAULT_POLYMERASE].label,
    "fixed_ta": TA_AUTO,
    "probe": False,
    "seqext": True,
    "keep": False,
}

_PARAM_LABELS = {
    "min_len": "Min amplicon", "max_len": "Max amplicon", "err3": "3' mismatches",
    "workers": "Parallel jobs", "heap": "JVM heap", "circular": "Circular template",
    "probe": "Probe search", "seqext": "Extract amplicon sequences",
    "keep": "Keep raw .out reports",
    "engine": "Engine", "polymerase": "Polymerase", "fixed_ta": "Annealing Ta",
}


def _fmt_default(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value == TA_AUTO or value == "":
        return "auto"
    return str(value)


def _defaults_tooltip() -> str:
    lines = "\n".join(f"  {_PARAM_LABELS[k]}: {_fmt_default(v)}"
                      for k, v in PARAM_DEFAULTS.items())
    return f"Restore every parameter below to:\n{lines}"


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """A column as a NaN-safe numeric/boolean series, absent columns as False."""
    if name not in df:
        return pd.Series(False, index=df.index)
    return df[name].fillna(0)


# Detail rows the table shows at once. Past this the view holds only a prefix:
# QTableView bogs down on very large models and no one reads 50k rows by eye. The
# full table always reaches the CSV/Excel export, and the count line says so.
DETAIL_VIEW_CAP = 50_000


class DetailFilter(NamedTuple):
    label: str
    apply: Callable[[pd.DataFrame], pd.DataFrame]
    chip: str = ""   # the short text on the filter chip; label is its tooltip


# 'Multi-amplicon' = more than one predicted product. 'Multiband' = products of
# more than one distinct size, i.e. what a gel would actually resolve.
DETAIL_FILTERS: tuple[DetailFilter, ...] = (
    DetailFilter("All rows", lambda d: d, "All"),
    DetailFilter("Amplified", lambda d: d[_col(d, "amplified") == True],  # noqa: E712
                 "Amplified"),
    DetailFilter("Not amplified", lambda d: d[_col(d, "amplified") != True],  # noqa: E712
                 "Not amplified"),
    DetailFilter("Multi-amplicon (>1 product)", lambda d: d[_col(d, "n_amplicons") > 1],
                 "Multi-product"),
    DetailFilter("Multiband (>1 size)", lambda d: d[_col(d, "n_unique_sizes") > 1],
                 "Multiband"),
    # From the biological score (absent when a run was not scored).
    DetailFilter("Likely (strong or probable)",
                 lambda d: d[_col(d, "call").isin(["strong", "probable"])], "Likely"),
    DetailFilter("Doubtful (weak or unlikely)",
                 lambda d: d[_col(d, "call").isin(["weak", "unlikely"])], "Doubtful"),
    # Compared with == False, not via _col: a missing value must not count.
    DetailFilter("Engines disagree",
                 lambda d: d[d["engines_agree"] == False] if "engines_agree" in d  # noqa: E712
                 else d.iloc[0:0], "Engines disagree"),
)

# Cell backgrounds, by theme token: green for good news, amber, red.
_CALL_TOKENS = {"strong": "good_bg", "probable": "good_bg2",
                "weak": "warn_bg", "unlikely": "bad_bg"}


# ---------------------------------------------------------------------------
# table model
# ---------------------------------------------------------------------------

class DataFrameModel(QAbstractTableModel):
    """Read-only view over a DataFrame; cheap for large result sets.

    Cells are read from a NumPy copy made once per table, not through
    `df.iat`: a view sized to its contents asks for every cell, in several
    roles, whenever the style changes, and at ~20 µs a pandas lookup a theme
    switch on a few hundred rows took fifteen seconds.
    """

    _ROLES = (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.BackgroundRole)

    def __init__(self, df: pd.DataFrame | None = None) -> None:
        super().__init__()
        self._set(df if df is not None else pd.DataFrame())

    def _set(self, df: pd.DataFrame) -> None:
        self._df = df
        self._values = df.to_numpy(dtype=object)
        self._token_cols = {i: c for i, c in enumerate(df.columns) if c in ("amplified", "call")}

    def set_dataframe(self, df: pd.DataFrame) -> None:
        self.beginResetModel()
        self._set(df)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._df)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._df.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role not in self._ROLES or not index.isValid():
            return None
        value = self._values[index.row(), index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            if pd.isna(value):
                return ""
            if isinstance(value, float):
                return f"{value:g}"
            return str(value)
        if role == Qt.ItemDataRole.BackgroundRole:
            key = self._cell_token(index.column(), value)
            return theme.color(key) if key else None
        return None

    def _cell_token(self, column: int, value) -> str | None:
        col = self._token_cols.get(column)
        if col is None:
            return None
        if col == "amplified":
            return "good_bg" if bool(value) else "bad_bg"
        if col == "call":
            return _CALL_TOKENS.get(value)
        return None

    def repaint_colors(self) -> None:
        """Re-query the backgrounds after a theme change."""
        if len(self._df):
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._df) - 1, len(self._df.columns) - 1),
                                  [Qt.ItemDataRole.BackgroundRole])

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return str(self._df.columns[section])
        return str(section + 1)


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------

class RunWorker(QThread):
    progress = pyqtSignal(int, int, str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, pairs, targets, jar, config, workers, heap, reports_dir,
                 reaction=None, engine="jar"):
        super().__init__()
        self._args = (pairs, targets, jar, config, workers, heap, reports_dir,
                      reaction, engine)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        (pairs, targets, jar, config, workers, heap, reports_dir,
         reaction, engine) = self._args
        try:
            results = run_batch(
                pairs, targets, jar, config,
                workers=workers, heap=heap or None, reports_dir=reports_dir,
                progress=lambda d, t, l: self.progress.emit(d, t, l),
                should_cancel=lambda: self._cancel,
                reaction=reaction, engine=engine,
            )
            self.finished_ok.emit(results)
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.results: list = []
        self.worker: RunWorker | None = None
        self._detail_df: pd.DataFrame | None = None
        # Folder chosen once per run so the .out reports and the export share it.
        self._run_outdir: Path | None = None
        self._run_include_sequence = True
        # The primer tables live in a dialog that is rebuilt on every open, so
        # the window, not a widget, owns what was loaded.
        self.primers = PrimerState()
        self._settings = QSettings("IAvH", APP_NAME)
        # Set before any widget exists: resizeEvent fires during construction.
        self._restoring = True
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_ui_state)
        if (app := QApplication.instance()) is not None:
            theme.apply(app, str(self._settings.value("ui/theme", "system")))
        theme.bus().changed.connect(self._on_theme_changed)

        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._build_setup())
        column.addWidget(self._build_results(), 1)
        outer.addWidget(self._build_rail())
        outer.addLayout(column, 1)
        self.setCentralWidget(central)
        # Wide enough for the rail, the strip at its tiles' minimum width, and
        # the three Parameters columns: narrower, the right edge is clipped.
        self.setMinimumSize(960, 560)
        # The drawer floats over the results instead of taking a slot in the
        # column: whichever form is open, the results keep their height. The
        # scrim dims them meanwhile, and a click on it closes the drawer.
        self.scrim = Scrim(central)
        self.scrim.clicked.connect(lambda: self.open_section(None))
        self.drawer.setParent(central)
        self.drawer.hide()
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self,
                  activated=lambda: self.open_section(None))
        # Notices land in the corner of the results, clear of the Run button,
        # and above the scrim, so a toast is never dimmed by an open drawer.
        self.toasts = ToastHost(central, self._results_host)

        self._build_menu()
        self._autodetect_jar()
        self._wire_summaries()
        self._restore_ui_state()   # clears _restoring on the way out
        self.statusBar().showMessage("Ready")

    # -- parameters --------------------------------------------------------

    def parameters_are_default(self) -> bool:
        return all(self._param_value(k) == v for k, v in PARAM_DEFAULTS.items())

    def _param_value(self, key: str) -> object:
        w = getattr(self, key)
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentText()
        return w.value()

    def reset_parameters(self, confirm: bool = False) -> None:
        """Restore every Parameters widget to PARAM_DEFAULTS.

        From the button (`confirm`), the values replaced are kept and a toast
        offers Undo: cheaper than asking first, and a slip costs one click.
        """
        if confirm and not self.parameters_are_default():
            before = {k: self._param_value(k) for k in PARAM_DEFAULTS}
            self._set_parameters(PARAM_DEFAULTS)
            self.toasts.show("info", "Parameters restored to their defaults.",
                             [("Undo", lambda: self._set_parameters(before))])
            return
        self._set_parameters(PARAM_DEFAULTS)

    def _set_parameters(self, values: dict[str, object]) -> None:
        for key, value in values.items():
            w = getattr(self, key)
            if isinstance(w, QCheckBox):
                w.setChecked(bool(value))
            elif isinstance(w, QComboBox):
                w.setCurrentText(str(value))
            else:
                w.setValue(value)

    # -- the drawer ----------------------------------------------------------

    def toggle_section(self, key: str) -> None:
        """Open `key`'s form in the drawer, or close the drawer if it is open."""
        self.open_section(None if self._open_section == key else key)

    def open_section(self, key: str | None) -> None:
        self._open_section = key
        for k, tile in self.sections.items():
            tile.set_active(k == key)
        if key is None:
            self.drawer.setVisible(False)
            self.scrim.setVisible(False)
            return
        page = self._drawer_pages[key]
        # Only the shown page may claim height, or the drawer is as tall as
        # the tallest form whichever one is open.
        for i in range(self._drawer_stack.count()):
            w = self._drawer_stack.widget(i)
            w.setSizePolicy(QSizePolicy.Policy.Preferred,
                            QSizePolicy.Policy.Preferred if w is page
                            else QSizePolicy.Policy.Ignored)
        self._drawer_stack.setCurrentWidget(page)
        # Exactly the height the form asks for: any less and the longest
        # column (Amplicon) is the one squeezed, so the columns lose their
        # common row rhythm. The visible part is capped, though, so a strip
        # of dimmed results always shows below; past the cap the drawer
        # scrolls rather than hiding the form's last rows.
        self._drawer_stack.setFixedHeight(page.sizeHint().height())
        self._fit_drawer()
        self.drawer.setVisible(True)
        self.scrim.setVisible(True)
        self.drawer.layout().activate()
        self._place_drawer()

    def _place_drawer(self) -> None:
        """Lay the drawer over the results, flush under the strip and as wide
        as it, with the scrim over the rest of the results below it."""
        central = self.centralWidget()
        if central is None or not self.drawer.isVisible():
            return
        top_left = self._strip.mapTo(central, QPoint(0, self._strip.height()))
        m = self.drawer.layout().contentsMargins()
        h = self._drawer_scroll.height() + m.top() + m.bottom()
        self.drawer.setGeometry(top_left.x(), top_left.y(), self._strip.width(), h)
        self.scrim.setGeometry(top_left.x(), top_left.y() + h, self._strip.width(),
                               max(0, central.height() - top_left.y() - h))
        self.scrim.raise_()
        self.drawer.raise_()
        self.toasts.raise_all()

    # -- section summaries -------------------------------------------------

    def _wire_summaries(self) -> None:
        """Keep each header's right-hand hint current, folded or not."""
        refresh = self._refresh_summaries
        self.jar_edit.textChanged.connect(refresh)
        self.run_label.textChanged.connect(refresh)
        self._out_check = check_output_dir(self.out_edit.text())
        self._out_timer = QTimer(self)
        self._out_timer.setSingleShot(True)
        self._out_timer.setInterval(300)
        self._out_timer.timeout.connect(self._check_output)
        self.out_edit.textChanged.connect(lambda *_: self._out_timer.start())
        self.out_edit.textChanged.connect(refresh)
        self.auto_export.toggled.connect(refresh)
        self.keep.toggled.connect(refresh)
        # Every PARAM_DEFAULTS widget, or "modified" and Reset go stale.
        for w in (self.min_len, self.max_len, self.err3, self.workers):
            w.valueChanged.connect(refresh)
        self.heap.currentTextChanged.connect(refresh)
        self.polymerase.currentTextChanged.connect(refresh)
        self.engine.currentTextChanged.connect(refresh)
        self.fixed_ta.valueChanged.connect(refresh)
        for w in (self.circular, self.probe, self.seqext, self.keep):
            w.toggled.connect(refresh)
        m = self.target_list.model()
        m.rowsInserted.connect(refresh)
        m.rowsRemoved.connect(refresh)
        m.modelReset.connect(refresh)
        refresh()

    def _dim_primer_names(self) -> None:
        """The pair list is a hint, not a control: dimmed, from the palette."""
        self.primer_names.setStyleSheet(
            f"color: {self.palette().placeholderText().color().name()};")

    def changeEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtCore import QEvent
        # Qt can deliver this before the setup strip has been built.
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "primer_names"):
            self._dim_primer_names()
        super().changeEvent(event)

    def _refresh_primer_labels(self) -> None:
        """The panel's stand-in for the tables: how many pairs, and which."""
        st = self.primers
        if st.is_empty:
            self.primer_count.setText("No primers loaded")
            self.primer_names.setText("")
            self.primer_names.setToolTip("")
            return

        chosen = st.selected
        noun = "pair" if len(st.pairs) == 1 else "pairs"
        self.primer_count.setText(
            f"<b>{len(chosen)}</b> of {len(st.pairs)} {noun} to run, from "
            f"{len(st.forwards)} forward and {len(st.reverses)} reverse primers")
        if not chosen:
            self.primer_names.setText("no pairs ticked")
            self.primer_names.setToolTip("")
            return
        shown = ", ".join(p.name for p in chosen[:2])
        if len(chosen) > 2:
            shown += f", +{len(chosen) - 2} more"
        self.primer_names.setText(shown)
        self.primer_names.setToolTip("\n".join(p.name for p in chosen))

    def _refresh_summaries(self, *_) -> None:
        # Reached from target_list's model signals (rowsInserted/Removed/
        # modelReset). On window teardown the model is cleared and emits
        # modelReset after the C++ widgets are gone; the surviving Python
        # wrapper would then touch dead objects and raise. Bail out, as
        # DropListWidget._place_caption does for the same signal.
        if sip.isdeleted(self):
            return
        paths = self.target_list.paths()
        n = len(paths)
        names = [Path(x).name for x in paths]
        self.sections["targets"].set_summary(
            f"{n} FASTA file{'s' if n != 1 else ''}" if n else "No FASTA files",
            (", ".join(names[:2]) + (f" +{n - 2}" if n > 2 else "")) if n
            else "drop files or a folder here",
            "\n".join(paths) if n else "Drop FASTA files or a folder here, or click to add")
        self.sections["targets"].set_ready(n > 0)

        st = self.primers
        if st.is_empty:
            self.sections["primers"].set_summary(
                "No primers", "drop FASTA, TSV, CSV or XLSX",
                "Drop a primer file here, or click to load one")
        else:
            chosen = st.selected
            self.sections["primers"].set_summary(
                f"{len(chosen)} of {len(st.pairs)} pair{'s' if len(st.pairs) != 1 else ''}",
                f"{len(st.forwards)} F · {len(st.reverses)} R",
                "\n".join(p.name for p in chosen) or "no pairs ticked")
        self.sections["primers"].set_ready(bool(st.selected))
        self._refresh_primer_labels()

        engine = self.engine.currentData() or "jar"
        sub = ["circular" if self.circular.isChecked() else "linear"]
        if self.probe.isChecked():
            sub.append("probe")
        if engine != "jar":
            sub.append({"thermo": "built-in", "both": "JAR + built-in"}[engine])
        sub.append(self.polymerase.currentText().split(" (")[0])
        sub.append("auto Ta" if self.fixed_ta.value() == TA_AUTO
                   else f"Ta {self.fixed_ta.value():g} °C")
        at_default = self.parameters_are_default()
        if not at_default:
            sub.append("modified")
        main = f"{self.min_len.value()}–{self.max_len.value()} bp · {self.err3.value()} mm"
        self.sections["parameters"].set_summary(main, " · ".join(sub),
                                                f"{main}\n" + "\n".join(sub))
        jar = self.jar_edit.text().strip()
        self.sections["parameters"].set_ready(
            engine == "thermo" or bool(jar and Path(jar).exists()))
        self.reset_btn.setEnabled(not at_default)

        label = self.run_label.text().strip()
        folder = Path(self.out_edit.text().strip() or ".").name
        oc = self._out_check
        mode = "auto-export" if self.auto_export.isChecked() else "manual export"
        self.sections["output"].set_summary(
            label or folder, mode if oc.ok else oc.message,
            f"{self.out_edit.text().strip()}\n{oc.message}")
        if oc.ok:
            self.sections["output"].set_ready(True)
        else:
            self.sections["output"].set_problem(oc.message)
        self.out_status.setText(("✓ " if oc.ok else "⚠ ") + oc.message[:1].upper() + oc.message[1:])
        self.out_status.setProperty("ok", "true" if oc.ok else "false")
        self.out_status.style().unpolish(self.out_status)
        self.out_status.style().polish(self.out_status)

        jobs = len(self.primers.selected) * n * (2 if engine == "both" else 1)
        self.jobs_label.setText(f"{jobs} job{'s' if jobs != 1 else ''}" if jobs
                                else "add targets and primers")

    # -- persisted layout --------------------------------------------------

    def _save_ui_state(self) -> None:
        if self._restoring:
            return
        s = self._settings
        # saveGeometry already carries the maximised/fullscreen flag.
        s.setValue("geometry", self.saveGeometry())
        s.setValue("ui/theme", theme.mode())
        # Closing the console that launched the app kills the process without a
        # closeEvent, so nothing may flush the settings later. Do it now.
        s.sync()

    def _schedule_save(self) -> None:
        """Coalesce the flood of events a drag produces into one write."""
        if not self._restoring:
            self._save_timer.start()

    def _restore_ui_state(self) -> None:
        self._restoring = True
        try:
            if (geom := self._settings.value("geometry")) is not None:
                self.restoreGeometry(geom)
            else:
                self.resize(1280, 800)
        finally:
            self._restoring = False

    # -- theme ---------------------------------------------------------------

    def set_theme(self, mode: str) -> None:
        if (app := QApplication.instance()) is not None:
            theme.apply(app, mode)
        self._save_ui_state()

    def toggle_theme(self) -> None:
        """The toolbar button flips between light and dark explicitly."""
        self.set_theme("light" if theme.scheme() == "dark" else "dark")

    def _on_theme_changed(self, scheme: str) -> None:
        if not hasattr(self, "theme_btn") or sip.isdeleted(self):
            return
        dark = scheme == "dark"
        self.theme_btn.setIcon(theme.toggle_icon(show_sun=dark))
        self.theme_btn.setToolTip("Switch to light theme" if dark else "Switch to dark theme")
        for m, act in self._theme_actions.items():
            act.setChecked(m == theme.mode())
        self.summary_model.repaint_colors()
        self.detail_model.repaint_colors()

    # Share of the window height the open drawer may take. It covers the
    # results rather than shrinking them, so it can take more than it could
    # when it pushed them down; what is left shows dimmed below it.
    DRAWER_MAX_SHARE = 0.7
    # The least of the dimmed results kept in view under the drawer, so it
    # still reads as a sheet over them and has somewhere to click to close.
    DRAWER_GAP = 60

    def _fit_drawer(self) -> None:
        if self._open_section is None:
            return
        need = self._drawer_stack.minimumHeight()   # fixed to the form's height
        cap = int(self.height() * self.DRAWER_MAX_SHARE)
        if (central := self.centralWidget()) is not None:
            margins = self.drawer.layout().contentsMargins()
            room = (central.height() - self._strip.sizeHint().height()
                    - self.DRAWER_GAP - margins.top() - margins.bottom())
            cap = min(cap, room)
        self._drawer_scroll.setFixedHeight(max(90, min(need, cap)))
        self._place_drawer()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_drawer_scroll"):
            self._fit_drawer()
        self._schedule_save()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._schedule_save()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Destroying a QThread that is still running aborts the process, and
        # the JAR runs it started would be orphaned. Stop the run first.
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self, APP_NAME, "A run is in progress. Stop it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # Disconnected first: the partial result would otherwise arrive
            # after the window is gone and try to build tables and export.
            # PyQt raises on a signal with no connections, and an exception
            # escaping closeEvent aborts the process.
            for sig in (self.worker.progress, self.worker.finished_ok, self.worker.failed):
                try:
                    sig.disconnect()
                except TypeError:
                    pass
            self.worker.cancel()
            with self._busy("Stopping the run…"):
                self.worker.wait()
        self._save_timer.stop()
        self._save_ui_state()
        super().closeEvent(event)

    # -- construction ------------------------------------------------------

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&File")
        for text, slot in (("Add FASTA targets...", self.add_targets),
                           ("Load primer pairs...", lambda: self.load_primers()),
                           ("Manage primers...", self.manage_primers),
                           ("Export results...", self.export_results)):
            a = QAction(text, self)
            a.triggered.connect(slot)
            m.addAction(a)
        m.addSeparator()
        quit_a = QAction("Quit", self)
        quit_a.triggered.connect(self.close)
        m.addAction(quit_a)

        v = self.menuBar().addMenu("&View")
        tm = v.addMenu("Theme")
        group = QActionGroup(self)
        self._theme_actions: dict[str, QAction] = {}
        for m, text in (("system", "Follow Windows"), ("light", "Light"), ("dark", "Dark")):
            act = QAction(text, self, checkable=True)
            act.setChecked(m == theme.mode())
            act.triggered.connect(lambda _=False, m=m: self.set_theme(m))
            group.addAction(act)
            tm.addAction(act)
            self._theme_actions[m] = act
        v.addSeparator()
        for key, text in (("targets", "Targets"), ("primers", "Primers"),
                          ("parameters", "Parameters"), ("output", "Output")):
            act = QAction(f"{text} settings", self)
            act.triggered.connect(lambda _=False, k=key: self.open_section(k))
            v.addAction(act)

        h = self.menuBar().addMenu("&Help")
        a = QAction("Java / JAR info", self)
        a.triggered.connect(self.show_env)
        h.addAction(a)

        # The theme toggle itself sits at the foot of the rail.
        self._on_theme_changed(theme.scheme())

    def _build_setup(self) -> QWidget:
        """The tile strip, and the drawer that opens a tile's form under it."""
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        host.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        strip = QFrame()
        self._strip = strip
        strip.setObjectName("strip")
        strip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(strip)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(10)

        # Parented to the central widget once that exists (see __init__).
        self.drawer = QFrame()
        self.drawer.setObjectName("drawer")
        self._drawer_stack = QStackedWidget()
        self._drawer_scroll = QScrollArea()
        self._drawer_scroll.setObjectName("drawerScroll")
        self._drawer_stack.setObjectName("drawerStack")
        self._drawer_scroll.setWidget(self._drawer_stack)
        self._drawer_scroll.setWidgetResizable(True)
        self._drawer_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._drawer_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        dl = QVBoxLayout(self.drawer)
        dl.setContentsMargins(14, 12, 14, 14)
        dl.addWidget(self._drawer_scroll)
        self.drawer.setVisible(False)
        self._open_section: str | None = None
        self._drawer_pages: dict[str, QWidget] = {}
        self.sections: dict[str, SetupTile] = {}

        def tile(key: str, title: str, page: QWidget, suffixes=(),
                 dir_suffixes=None) -> SetupTile:
            t = SetupTile(title, suffixes, dir_suffixes)
            t.clicked.connect(lambda k=key: self.toggle_section(k))
            self.sections[key] = t
            self._drawer_pages[key] = page
            self._drawer_stack.addWidget(page)
            row.addWidget(t, 1)
            return t

        targets = tile("targets", "Targets", self._targets_page(),
                       FASTA_SUFFIXES, FASTA_DIR_SUFFIXES)
        targets.filesDropped.connect(self._on_targets_dropped)
        primers = tile("primers", "Primers", self._primers_page(), PRIMER_SUFFIXES)
        primers.filesDropped.connect(lambda ps: self.load_primers(str(ps[0])))
        tile("parameters", "Parameters", self._parameters_page())
        tile("output", "Output", self._output_page())
        row.addWidget(self._run_box())

        v.addWidget(strip)
        return host

    def _targets_page(self) -> QWidget:
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.target_list = DropListWidget(FASTA_SUFFIXES)
        self.target_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.target_list.setToolTip("Drop FASTA files or a folder of them here")
        self.target_list.setMinimumHeight(90)
        self.target_list.setMaximumHeight(TARGET_LIST_HEIGHT)
        self.target_list.filesDropped.connect(
            lambda ps: self._log(f"Added {len(ps)} target file(s)"))
        lay.addWidget(self.target_list, 1)
        col = QVBoxLayout()
        for text, slot in (("Add files...", self.add_targets), ("Remove", self.remove_targets),
                           ("Clear", lambda: self.target_list.clear())):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            col.addWidget(btn)
        col.addStretch(1)
        lay.addLayout(col)
        return page

    def _primers_page(self) -> QWidget:
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        self.primer_count = QLabel()
        self.primer_count.setWordWrap(True)
        self.primer_names = QLabel()
        self.primer_names.setObjectName("primerNames")
        self.primer_names.setWordWrap(True)
        self._dim_primer_names()
        left.addWidget(self.primer_count)
        left.addWidget(self.primer_names)
        left.addStretch(1)
        # Slots with optional arguments are wrapped: clicked(bool) would
        # otherwise arrive as the first parameter.
        btns = QHBoxLayout()
        for text, tip, slot in (
                ("Load file...", "Load primers from FASTA, TSV, CSV or XLSX",
                 lambda: self.load_primers()),
                ("View/Select primers...", "Choose primers, recombine them, edit the pairs",
                 self.manage_primers),
                ("Clear", "Discard the loaded primers, so another file can be dropped",
                 self.clear_primers)):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            btns.addWidget(btn)
        btns.addStretch(1)
        left.addLayout(btns)
        lay.addLayout(left, 1)

        self.primer_drop = DropZone()
        self.primer_drop.setMinimumWidth(300)
        self.primer_drop.fileDropped.connect(self.load_primers)
        self.primer_drop.clicked.connect(lambda: self.load_primers())
        lay.addWidget(self.primer_drop, 1)
        return page

    @staticmethod
    def _group(title: str, form: QFormLayout, extra: QWidget | None = None) -> QWidget:
        """A titled column. Every title row has one fixed height, so a button
        beside one title does not push that column's rows out of line."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        head = QWidget()
        head.setFixedHeight(30)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        label = QLabel(title)
        label.setObjectName("groupTitle")
        hl.addWidget(label)
        hl.addStretch(1)
        if extra is not None:
            hl.addWidget(extra)
        v.addWidget(head)
        v.addLayout(form)
        v.addStretch(1)
        return w

    def _parameters_page(self) -> QWidget:
        """Three groups side by side: the amplicon, the biological score, the run.

        The spin boxes and combos ignore the mouse wheel, so scrolling past one
        never edits it.
        """
        self.min_len = NoWheelSpinBox(); self.min_len.setRange(20, 50000)
        self.max_len = NoWheelSpinBox(); self.max_len.setRange(20, 50000)
        self.err3 = SegmentedControl(
            (0, 1, 2, 3), tips=[f"Allow {n} mismatch{'es' if n != 1 else ''} in the "
                                f"primers' 3' end" for n in range(4)])
        self.err3.setToolTip("Mismatches allowed in the 3' end of each primer")
        self.workers = NoWheelSpinBox(); self.workers.setRange(1, 32)
        self.heap = NoWheelComboBox(); self.heap.addItems(["", "2g", "4g", "8g", "16g"])
        self.circular = QCheckBox("Circular template")
        self.probe = QCheckBox("Probe search")
        self.seqext = QCheckBox("Extract amplicon sequences")
        self.keep = QCheckBox("Keep raw .out reports")

        # Biological score: what the bench protocol is, so each predicted
        # product can be graded rather than only counted.
        self.engine = NoWheelComboBox()
        for label, key, tip in ENGINE_CHOICES:
            self.engine.addItem(label, key)
            self.engine.setItemData(self.engine.count() - 1, tip,
                                    Qt.ItemDataRole.ToolTipRole)
        self.engine.setToolTip("What predicts the products. 'Both' compares them.")
        self.polymerase = NoWheelComboBox()
        for key, prof in POLYMERASES.items():
            self.polymerase.addItem(prof.label, key)
            self.polymerase.setItemData(self.polymerase.count() - 1, prof.note,
                                        Qt.ItemDataRole.ToolTipRole)
        self.polymerase.setToolTip(
            "Polymerase used at the bench. It sets the buffer assumed for the\n"
            "thermodynamics, how much a 3' mismatch costs, and the automatic Ta.")
        self.fixed_ta = NoWheelDoubleSpinBox()
        self.fixed_ta.setRange(TA_AUTO, 75.0)
        self.fixed_ta.setDecimals(1)
        self.fixed_ta.setSingleStep(0.5)
        self.fixed_ta.setSuffix(" °C")
        self.fixed_ta.setSpecialValueText("auto (per pair)")
        self.fixed_ta.setToolTip(
            "auto: each pair uses its own Ta (from the primer file or the\n"
            "View/Select primers table), else its lower primer Tm - 5 °C\n"
            "(+3 °C for high-fidelity polymerases).\n"
            "A value here applies to pairs that have no Ta of their own.")

        # JAR: autodetected; only the JAR engine needs it.
        self.jar_edit = DropLineEdit(JAR_SUFFIXES)
        self.jar_edit.setMinimumWidth(120)
        self.jar_edit.setPlaceholderText("virtualPCR.jar")
        self.jar_edit.setToolTip("Path to virtualPCR.jar — or drop the file here")
        self.jar_edit.fileDropped.connect(lambda p: self._log(f"JAR: {p}"))
        jar_row = QHBoxLayout()
        jar_row.addWidget(self.jar_edit, 1)
        browse = QPushButton("Browse")
        browse.clicked.connect(self.pick_jar)
        jar_row.addWidget(browse)

        amp = QFormLayout()
        amp.addRow("Min amplicon (bp)", self.min_len)
        amp.addRow("Max amplicon (bp)", self.max_len)
        amp.addRow("3' mismatches", self.err3)
        amp.addRow(self.circular)
        amp.addRow(self.probe)
        amp.addRow(self.seqext)
        score = QFormLayout()
        score.addRow("Engine", self.engine)
        score.addRow("Polymerase", self.polymerase)
        score.addRow("Annealing Ta", self.fixed_ta)
        run = QFormLayout()
        run.addRow("virtualPCR JAR", jar_row)
        run.addRow("Parallel jobs", self.workers)
        run.addRow("JVM heap", self.heap)
        run.addRow(self.keep)

        self.reset_btn = QPushButton("Reset to defaults")
        self.reset_btn.setProperty("compact", "true")
        self.reset_btn.setToolTip(_defaults_tooltip())
        self.reset_btn.clicked.connect(lambda: self.reset_parameters(confirm=True))
        self.reset_parameters()   # the widgets have no values until now

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        cols = QHBoxLayout()
        cols.setSpacing(28)
        cols.addWidget(self._group("Amplicon", amp), 1)
        cols.addWidget(self._group("Biological score", score), 1)
        # Reset sits on the title row, where it is seen before any field: at
        # the foot of the form it was the first thing a short window hid.
        cols.addWidget(self._group("Run", run, self.reset_btn), 1)
        lay.addLayout(cols)
        return page

    def _output_page(self) -> QWidget:
        self.out_edit = DropDirLineEdit()
        self.out_edit.setText(str(APP_ROOT / "output"))
        self.out_edit.setMinimumWidth(240)
        self.out_edit.setToolTip(
            "Each run is written to its own <primers>_<timestamp> subfolder here.\n"
            "Drop a folder to change it.")
        orow = QHBoxLayout()
        orow.addWidget(self.out_edit, 1)
        ob = QPushButton("Browse")
        ob.clicked.connect(self.pick_output_dir)
        orow.addWidget(ob)
        self.run_label = QLineEdit()
        self.run_label.setPlaceholderText("optional")
        self.run_label.setToolTip("Names the output subfolder instead of the primer pairs")
        self.auto_export = QCheckBox("Auto-export when the run finishes")
        self.auto_export.setToolTip(
            "Write summary.csv, detail.csv, vpcr_results.xlsx and run_log.txt\n"
            "into a new subfolder as soon as the run completes")
        self.auto_export.setChecked(True)
        form = QFormLayout()
        form.addRow("Folder", orow)
        # Whether a run can write there, tested on disk (check_output_dir).
        self.out_status = QLabel("")
        self.out_status.setObjectName("outputStatus")
        form.addRow("", self.out_status)
        form.addRow("Run label", self.run_label)
        form.addRow(self.auto_export)
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(form, 2)
        lay.addStretch(1)
        return page

    def _run_box(self) -> QWidget:
        """Run, Cancel and progress: always in view, at the end of the strip."""
        box = QWidget()
        box.setFixedWidth(210)
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        self.run_btn = QPushButton("▶  Run in silico PCR")
        self.run_btn.setProperty("primary", "true")
        self.run_btn.setMinimumHeight(38)
        self.run_btn.clicked.connect(self.start_run)
        v.addWidget(self.run_btn)
        row = QHBoxLayout()
        self.jobs_label = QLabel("")
        self.jobs_label.setObjectName("muted")
        row.addWidget(self.jobs_label, 1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_run)
        row.addWidget(self.cancel_btn)
        v.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        v.addWidget(self.progress)
        return box

    def _on_targets_dropped(self, paths: list) -> None:
        added = self.target_list.add_paths(paths)
        if added:
            self._log(f"Added {len(added)} target file(s)")

    def _build_results(self) -> QWidget:
        """Tabs across the full width, with the filter chips and Export above."""
        w = QWidget()
        self._results_host = w
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)

        self.summary_model = DataFrameModel()
        self.detail_model = DataFrameModel()
        views = []
        for model in (self.summary_model, self.detail_model):
            view = QTableView()
            view.setModel(model)
            view.setSortingEnabled(False)
            view.setAlternatingRowColors(True)
            view.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.ResizeToContents)
            # Columns are sized from a sample of rows, not all of them: the
            # widths settle within a hundred rows, and every style change
            # (a theme switch) measures them again.
            view.horizontalHeader().setResizeContentsPrecision(150)
            views.append(view)
        self.summary_view = views[0]
        # Headline numbers and the virtual gel around the summary table.
        self.summary_page = SummaryPage(self.summary_view)
        self.summary_page.laneClicked.connect(self._select_summary_row)
        self.tabs.addTab(self.summary_page, "Summary")

        # The filters belong to the Detail table, so they sit on its page.
        detail_page = QWidget()
        dv = QVBoxLayout(detail_page)
        dv.setContentsMargins(8, 8, 8, 0)
        dv.setSpacing(6)
        bar = QHBoxLayout()
        self.detail_filter = ChipBar([f.chip or f.label for f in DETAIL_FILTERS],
                                     [f.label for f in DETAIL_FILTERS])
        self.detail_filter.currentIndexChanged.connect(self._apply_detail_filter)
        self.detail_filter.setEnabled(False)
        bar.addWidget(self.detail_filter, 1)
        self.filter_count = QLabel("")
        self.filter_count.setObjectName("muted")
        bar.addWidget(self.filter_count, 0, Qt.AlignmentFlag.AlignTop)
        dv.addLayout(bar)
        dv.addWidget(views[1], 1)
        self.tabs.addTab(detail_page, "Detail")

        # Primers drawn onto the amplified region, for a chosen pair/sequence.
        self.alignment_tab = AlignmentTab()
        self.tabs.addTab(self.alignment_tab, "Alignment")

        # Every primer's footprint on a chosen target, to see what overlaps.
        self.primer_map_tab = PrimerMapTab()
        self.tabs.addTab(self.primer_map_tab, "Primer map")

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.tabs.addTab(self.log, "Log")
        # The rail chooses the view, so the tab bar would only repeat it.
        self.tabs.tabBar().hide()
        self.tabs.currentChanged.connect(self._on_view_changed)

        # The view's name, and Export, which applies to every view.
        head = QHBoxLayout()
        head.setContentsMargins(2, 0, 0, 0)
        self.page_title = QLabel(VIEWS[0][2])
        self.page_title.setObjectName("pageTitle")
        head.addWidget(self.page_title)
        head.addStretch(1)
        self.export_btn = QPushButton("Export Excel + CSV")
        self.export_btn.setProperty("compact", "true")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_results)
        head.addWidget(self.export_btn)
        lay.addLayout(head)
        lay.addWidget(self.tabs, 1)
        return w

    def _build_rail(self) -> QWidget:
        """The rail: one button per view, Ctrl+1..5, and the theme toggle."""
        self.rail = NavRail([(key, label, f"{tip}  (Ctrl+{i})")
                             for i, (key, label, _t, tip) in enumerate(VIEWS, 1)])
        self.rail.currentChanged.connect(self.tabs.setCurrentIndex)
        for i in range(len(VIEWS)):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self,
                      activated=lambda i=i: self.tabs.setCurrentIndex(i))
        self.theme_btn = QToolButton()
        self.theme_btn.setObjectName("themeToggle")
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.setIconSize(QSize(18, 18))
        self.theme_btn.setFixedSize(40, 36)
        self.theme_btn.clicked.connect(self.toggle_theme)
        self.rail.add_bottom(self.theme_btn)
        return self.rail

    def _on_view_changed(self, i: int) -> None:
        self.rail.set_current(i)
        self.page_title.setText(VIEWS[i][2])
        # The Log badge counts problems not yet seen; seeing them clears it.
        if VIEWS[i][0] == "log":
            self.rail.set_badge("log")

    def show_view(self, key: str) -> None:
        self.tabs.setCurrentIndex([v[0] for v in VIEWS].index(key))

    def _select_summary_row(self, pair: str, engine: str) -> None:
        """A click on a gel lane selects that pair's row in the table."""
        df = self.summary_model._df
        if df.empty or "pair" not in df:
            return
        hit = df["pair"] == pair
        if "engine" in df:
            hit &= df["engine"] == engine
        rows = list(df.index[hit])
        if rows:
            self.summary_view.selectRow(rows[0])
            self.summary_view.scrollTo(self.summary_model.index(rows[0], 0))

    def _apply_detail_filter(self) -> None:
        if self._detail_df is None:
            return
        try:
            with self._busy("Filtering detail rows…"):
                f = DETAIL_FILTERS[self.detail_filter.currentIndex()]
                shown = f.apply(self._detail_df)
                matched = len(shown)
                if matched > DETAIL_VIEW_CAP:
                    self.detail_model.set_dataframe(shown.iloc[:DETAIL_VIEW_CAP])
                    self.filter_count.setText(
                        f"showing first {DETAIL_VIEW_CAP:,} of {matched:,} rows "
                        f"— export for the full table")
                else:
                    self.detail_model.set_dataframe(shown)
                    self.filter_count.setText(f"{matched:,} of {len(self._detail_df):,} rows")
            self.tabs.setCurrentIndex(1)
        except MemoryError:
            self._log("OUT OF MEMORY applying the detail filter; showing nothing.")
            self.detail_model.set_dataframe(pd.DataFrame())
            self.filter_count.setText("out of memory — use a narrower filter or export")

    # -- helpers -----------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    @contextmanager
    def _busy(self, message: str):
        """Show a wait cursor + status while a blocking step runs on the GUI thread.

        Building the result tables and writing the export are CPU/IO bound and
        cannot be interrupted; without this the window paints as 'Not responding'
        and Windows offers to kill it. processEvents once lets the cursor and
        message paint before the blocking call starts.
        """
        before = self.statusBar().currentMessage()
        self.statusBar().showMessage(message)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            yield
        finally:
            QApplication.restoreOverrideCursor()
            # Put back what was there, unless the step said something new.
            if self.statusBar().currentMessage() == message:
                self.statusBar().showMessage(before)

    # Where the JAR always lives; anywhere else only when the user says so.
    DEFAULT_JAR = JAR_DIR / "virtualPCR.jar"

    def _autodetect_jar(self) -> None:
        """jar/virtualPCR.jar, else the JAR chosen last time, else nothing yet.

        The JAR is taken whether or not a Java to run it is installed: that
        is a separate problem, reported as such at Run, rather than an empty
        JAR field that reads as "the JAR is missing".
        """
        saved = str(self._settings.value("jar/path", "") or "")
        jar = self.DEFAULT_JAR if self.DEFAULT_JAR.is_file() else \
            Path(saved) if saved and Path(saved).is_file() else None
        if jar is None:
            self._log(f"No virtualPCR.jar in {JAR_DIR}; you will be asked for it at Run.")
            return
        self.jar_edit.setText(str(jar))
        self._log(f"JAR: {jar}")
        try:
            need = jar_class_version(jar)
        except Exception as e:
            self._log(f"WARNING: cannot read {jar.name}: {e}")
            return
        best = max((j.feature for j in discover_javas()), default=0)
        if best < need:
            self._log(f"WARNING: {jar.name} needs Java {need}+, "
                      + (f"newest found is Java {best}." if best else "and no Java was found.")
                      + " The built-in engine needs no Java.")

    def pick_jar(self, prompt: str = "Select virtualPCR.jar") -> bool:
        """Ask for the JAR; remembered for the next session. True if one was chosen."""
        p, _ = QFileDialog.getOpenFileName(self, prompt, str(JAR_DIR), "JAR files (*.jar)")
        if not p:
            return False
        self.jar_edit.setText(p)
        self._settings.setValue("jar/path", p)
        return True

    def add_targets(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select FASTA targets", "",
            "FASTA (*.fasta *.fa *.fas *.fna *.txt);;All files (*)")
        self.target_list.add_paths(files)

    def remove_targets(self) -> None:
        for item in self.target_list.selectedItems():
            self.target_list.takeItem(self.target_list.row(item))

    def _set_primers(self, state: PrimerState) -> None:
        self.primers = state
        self._refresh_summaries()

    def load_primers(self, path: str = "") -> None:
        """Load pairs from `path`, or ask for a file when called with none.

        The file's own pairing seeds the run, with every pair ticked; Manage
        primers is where it gets recombined.
        """
        pairs = load_pairs_interactive(self, path, log=self._log)
        if pairs is None:
            return
        state = PrimerState.from_pairs(pairs)
        self._set_primers(state)
        self._log(f"Loaded {len(state.forwards)} forward and {len(state.reverses)} "
                  f"reverse primer(s) from {path or 'file'}, paired as in the file "
                  f"({len(pairs)} pair(s))")
        source = f" from {Path(path).name}" if path else ""
        self.toasts.show("success", f"Loaded {len(state.forwards)} forward and "
                         f"{len(state.reverses)} reverse primers{source}, "
                         f"{len(pairs)} pair{'s' if len(pairs) != 1 else ''}.",
                         [("View/Select primers", self.manage_primers)])

    def clear_primers(self) -> None:
        """Forget the loaded file, so a different one can be dropped."""
        if self.primers.is_empty:
            return
        self._set_primers(PrimerState())
        self._log("Cleared the loaded primers")

    def manage_primers(self) -> None:
        dlg = PrimerDialog(self.primers, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        before = len(self.primers.pairs)
        self._set_primers(dlg.state)
        after = len(self.primers.pairs)
        if after != before:
            self._log(f"Primer pairs: {before} -> {after}")

    def collect_pairs(self) -> list[PrimerPair]:
        return self.primers.selected

    def show_env(self) -> None:
        lines = ["Java runtimes found:"]
        javas = discover_javas()
        lines += [f"  Java {j.feature:<3} {j.executable}" for j in javas] or ["  (none)"]

        jar = self.jar_edit.text().strip()
        if jar and Path(jar).exists():
            try:
                need = jar_class_version(jar)
                lines.append(f"\nJAR: {jar}\nRequires Java {need}+")
                chosen = select_java(jar)
                lines.append(f"Will run with: Java {chosen.feature} ({chosen.executable})")
            except JarError as e:
                lines.append(f"\n{e}")
            except Exception as e:
                lines.append(f"\nJAR: {e}")
        QMessageBox.information(self, "Environment", "\n".join(lines))

    # -- run ---------------------------------------------------------------

    def start_run(self) -> None:
        jar = self.jar_edit.text().strip()
        targets = self.target_list.paths()
        pairs = self.collect_pairs()

        engine = self.engine.currentData() or "jar"
        problem = self._run_problem(targets, pairs, jar, engine)
        if problem is not None and problem[0] == "jar":
            # Not in jar/: ask for it now, and carry on with the run if given.
            if self.pick_jar(f"virtualPCR.jar is not in {JAR_DIR.name}/ — select it"):
                self.start_run()
                return
            problem = ("parameters", problem[1])
        if problem is not None:
            section, message = problem
            # The form that fixes it opens, and a toast says what to fix.
            self.open_section(section)
            actions = [("View/Select primers", self.manage_primers)] if section == "primers" \
                and self.primers.pairs else []
            self.toasts.show("warning", message, actions)
            return
        self.open_section(None)

        ta = self.fixed_ta.value()
        reaction = Reaction(self.polymerase.currentData() or DEFAULT_POLYMERASE,
                            None if ta == TA_AUTO else ta)
        cfg = RunConfig(
            min_len=self.min_len.value(), max_len=self.max_len.value(),
            n_3prime_errors=self.err3.value(), circular=self.circular.isChecked(),
            probe=self.probe.isChecked(), sequence_extract=self.seqext.isChecked(),
        )
        # Exports describe the run that produced the results, not whatever the
        # checkbox says by the time someone presses Export.
        self._run_include_sequence = cfg.sequence_extract
        # One folder per run, named once here so its timestamp is stable between
        # the worker (which writes the .out reports as it goes) and the export at
        # the end. With both options on, 'Keep raw .out' and auto-export land
        # side by side; the .out files go in a 'reports' subfolder of it.
        self._run_outdir = None
        if self.keep.isChecked() or self.auto_export.isChecked():
            self._run_outdir = unique_dir(
                self._output_base(),
                run_folder_name(pairs, datetime.now(), self.run_label.text()))
        reports = (self._run_outdir / "reports") if self.keep.isChecked() else None
        if reports is not None:
            self._log(f"Raw .out reports: {reports}")

        self.progress.setRange(0, len(pairs) * len(targets) * (2 if engine == "both" else 1))
        self.progress.setValue(0)
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.export_btn.setEnabled(False)
        self.alignment_tab.set_results([])
        self.primer_map_tab.set_results([])
        self.summary_page.set_results(None, [])
        for key in ("summary", "detail", "log"):
            self.rail.set_badge(key)
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.log))

        # The log is archived next to the results, so it records the run.
        self._log(f"\n{'=' * 62}\nvirtualPCR Studio run — {datetime.now():%Y-%m-%d %H:%M:%S}")
        self._log(f"JAR       : {jar}")
        self._log(f"Amplicon  : {cfg.min_len}-{cfg.max_len} bp, "
                  f"3' mismatches {cfg.n_3prime_errors}")
        self._log(f"Template  : {'circular' if cfg.circular else 'linear'}"
                  f"{', probe search' if cfg.probe else ''}"
                  f"{'' if cfg.sequence_extract else ', no amplicon sequences'}")
        # Cap the echo: thousands of per-item lines stall the log widget at
        # start. The full lists reach summary.csv / the export regardless.
        if len(targets) <= 50:
            for t in targets:
                self._log(f"Target    : {t}")
        else:
            self._log(f"Targets   : {len(targets)} files")
        if engine != "jar":
            self._log(f"Engine    : {self.engine.currentText()}")
        self._log(f"Score     : {reaction.profile.label}")
        if len(pairs) <= 50:
            for p in pairs:
                try:
                    t, src = resolve_ta(p, reaction)
                    ta_note = f"  Ta {t:g} °C ({src})"
                except ImportError:   # primer3 missing: run_batch says so
                    ta_note = ""
                self._log(f"Primer    : {p.name}  {p.forward} / {p.reverse}{ta_note}")
        else:
            self._log(f"Primers   : {len(pairs)} pairs (see summary.csv for the full list)")
        self._log(f"Running {len(pairs)} pair(s) x {len(targets)} file(s)...")

        self.worker = RunWorker(pairs, targets, jar, cfg,
                                self.workers.value(), self.heap.currentText(), reports,
                                reaction, engine)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def _run_problem(self, targets: list[str], pairs: list[PrimerPair], jar: str,
                     engine: str) -> tuple[str, str] | None:
        """The first thing that stops a run, as (section, message), or None.

        Checked in the order the setup is filled in, left to right along the
        strip: targets, primers, parameters, output. The first problem is
        the one reported, so the message always names the next thing to do.
        """
        # 1. Targets
        if not targets:
            return "targets", "Add at least one FASTA target."
        gone = [Path(t).name for t in targets if not Path(t).is_file()]
        if gone:
            more = f" and {len(gone) - 2} more" if len(gone) > 2 else ""
            return "targets", (f"{len(gone)} target file(s) no longer exist: "
                               f"{', '.join(gone[:2])}{more}. Remove or re-add them.")

        # 2. Primers
        if self.primers.is_empty:
            return "primers", "Load a primer file."
        if not pairs:
            return "primers", "Tick at least one primer pair in View/Select primers."
        # The JAR silently discards primers shorter than this and then fails
        # with 'Failed to open primer's file', which explains nothing.
        short = [f"{p.name}: {p.forward} / {p.reverse}" for p in pairs
                 if min(len(p.forward), len(p.reverse)) < MIN_PRIMER_LEN]
        if short:
            more = f"\n…and {len(short) - 3} more" if len(short) > 3 else ""
            return "primers", (f"These primers are shorter than {MIN_PRIMER_LEN} nt and "
                               f"cannot be searched:\n" + "\n".join(short[:3]) + more)

        # 3. Parameters
        lo, hi = self.min_len.value(), self.max_len.value()
        if lo > hi:
            return "parameters", f"Min amplicon ({lo} bp) is larger than max ({hi} bp)."
        # A product spans both primers, so a pair longer than the maximum
        # can never be found, and the run would report it as not amplifying.
        too_long = [p.name for p in pairs if len(p.forward) + len(p.reverse) > hi]
        if too_long:
            return "parameters", (f"Max amplicon ({hi} bp) is shorter than the primers of "
                                  f"{', '.join(too_long[:2])} together; no product could fit.")
        if engine != "thermo":
            if not jar or not Path(jar).is_file():
                return "jar", ("No virtualPCR.jar found. Select it under Run, or choose "
                               "the built-in engine.")
            try:
                _runner.select_java(jar)
            except JarError as e:
                return "parameters", f"{e} Or choose the built-in engine, which needs no Java."
            except Exception as e:      # an unreadable JAR
                return "parameters", f"{Path(jar).name} cannot be used: {e}"

        # 4. Output: only when the run will write there. Checked afresh: a
        # network drive may have gone since the folder was chosen.
        if self.keep.isChecked() or self.auto_export.isChecked():
            oc = self._check_output()
            if not oc.ok:
                return "output", f"The output folder cannot be used: {oc.message}."
        return None

    def cancel_run(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self._log("Cancelling: queued jobs dropped, running JAR processes stopped...")

    def on_progress(self, done: int, total: int, label: str) -> None:
        self.progress.setValue(done)
        self._log(f"[{done}/{total}] {label}")
        self.statusBar().showMessage(label)

    def _log_incomplete(self, results: list) -> None:
        """Put a partial run on record, in the log that run_log.txt archives."""
        total = getattr(results, "total", len(results))
        if getattr(results, "cancelled", False):
            self._log(f"CANCELLED: {len(results)} of {total} job(s) finished; "
                      f"the tables below are partial.")
        errors = getattr(results, "errors", [])
        if errors:
            self._log(f"WARNING: {len(errors)} of {total} job(s) failed; their pairs "
                      f"are missing from the tables:")
            for pair, target, msg in errors:
                self._log(f"  {pair} / {target}: {msg}")

    def _warn_failed_jobs(self, results: list) -> None:
        errors = getattr(results, "errors", [])
        if not errors:
            return
        total = getattr(results, "total", len(results))
        shown = "\n".join(f"• {p} / {t}: {m.splitlines()[0] if m else ''}"
                          for p, t, m in errors[:3])
        more = f"\n…and {len(errors) - 3} more" if len(errors) > 3 else ""
        self.rail.set_badge("log", str(len(errors)), "bad")
        # Stays until closed: a partial result must not pass unnoticed.
        self.toasts.show(
            "warning",
            f"{len(errors)} of {total} job(s) failed and are missing from the "
            f"results:\n{shown}{more}",
            [("Show log", lambda: self.show_view("log"))], timeout=0)

    def on_finished(self, results: list) -> None:
        self.results = results
        self.cancel_btn.setEnabled(False)
        cancelled = getattr(results, "cancelled", False)
        incomplete = cancelled or bool(getattr(results, "errors", []))
        self._log_incomplete(results)
        try:
            if not results:
                self._log("Run cancelled before any job finished." if cancelled
                          else "No results.")
                self.statusBar().showMessage("Cancelled" if cancelled else "No results")
                self._warn_failed_jobs(results)
                return
            # Building the detail table materialises one row per amplicon; on a
            # huge result set this is where the app would freeze or run out of
            # memory. Do it under a wait cursor and survive a MemoryError.
            with self._busy("Building result tables…"):
                summary = summary_table(results)
                detail = detail_table(results, include_sequence=self._run_include_sequence)
                self.summary_model.set_dataframe(summary)
                self.summary_page.set_results(summary, results)
                # Sequences are long; hide them on screen but keep them for export.
                self._detail_df = detail.drop(columns=["amplicon_seq"], errors="ignore")
            self.alignment_tab.set_results(results)
            self.primer_map_tab.set_results(results)
            self.detail_filter.setEnabled(True)
            self._apply_detail_filter()
            self.export_btn.setEnabled(True)
            self.tabs.setCurrentIndex(0)
            self.rail.set_badge("summary", compact_count(len(summary)))
            self.rail.set_badge("detail", compact_count(len(detail)))
            multiband = int((summary["n_multiband"]).sum())
            multi = int((summary["n_multi_amplicon"]).sum())
            self._log(f"Done. {len(summary)} pair/file combination(s), {len(detail)} detail rows. "
                      f"{multi} sequence(s) with >1 product, {multiband} with >1 distinct size.")
            if "pct_engine_agreement" in summary:
                agree = summary.drop_duplicates(["pair", "target_file"])
                for r in agree.itertuples(index=False):
                    self._log(f"Engines agree on {r.pct_engine_agreement:g}% of sequences "
                              f"for {r.pair} / {r.target_file}")
            self.statusBar().showMessage("Cancelled — partial results" if cancelled
                                         else "Done, with failed jobs" if incomplete
                                         else "Done")

            # A cancelled run is not exported on its own: the user stopped it,
            # and a folder that looks like any other would hide that it is
            # partial. Export results still writes it on request.
            if self.auto_export.isChecked() and cancelled:
                self._log("Auto-export skipped because the run was cancelled; "
                          "use Export results to save the finished jobs.")
            elif self.auto_export.isChecked():
                with self._busy("Exporting results…"):
                    outdir = self._export(outdir=self._run_outdir)
                if outdir:
                    self.statusBar().showMessage(f"Exported to {outdir}")
            self._warn_failed_jobs(results)
        except MemoryError:
            self._detail_df = None
            self.export_btn.setEnabled(bool(results))
            self._log("OUT OF MEMORY while building the result tables. The run itself "
                      "finished; try fewer pairs/targets per run or turn off "
                      "'Extract amplicon sequences'.")
            self.toasts.show(
                "error",
                "Ran out of memory building the result tables. The run finished, "
                "but the result set is too large to display here. Split the targets "
                "across smaller runs, or turn off 'Extract amplicon sequences'.")
        except Exception as e:
            self._log(f"ERROR building results: {e}\n{traceback.format_exc()}")
            self.rail.set_badge("log", "!", "bad")
            self.toasts.show("error", "Results failed: "
                             + (str(e).splitlines()[0] if str(e) else repr(e)),
                             [("Show log", lambda: self.show_view("log"))])
        finally:
            # Whatever happened, the window must be usable again.
            self.run_btn.setEnabled(True)

    def on_failed(self, msg: str) -> None:
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._log(f"FAILED: {msg}")
        self.rail.set_badge("log", "!", "bad")
        self.toasts.show("error", f"Run failed: {msg.splitlines()[0] if msg else ''}",
                         [("Show log", lambda: self.show_view("log"))])

    def _check_output(self) -> OutputCheck:
        """Test the output folder now, and show the answer on the tile."""
        self._out_check = check_output_dir(self.out_edit.text())
        self._refresh_summaries()
        return self._out_check

    def pick_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _output_base(self) -> Path:
        return Path(self.out_edit.text().strip() or (APP_ROOT / "output"))

    def _export(self, outdir: Path | None = None) -> Path | None:
        """Write the current results into the run's folder, or a fresh one.

        `outdir` is the folder chosen at run start (shared with the .out
        reports). When None — a manual export of a run that kept neither — a
        fresh <label>_<timestamp> folder is created instead.
        """
        base = self._output_base()
        try:
            base.mkdir(parents=True, exist_ok=True)
            if outdir is None:
                outdir, paths = export_run(
                    base, self.results, log_text=self.log.toPlainText(),
                    label=self.run_label.text(),
                    include_sequence=self._run_include_sequence)
            else:
                paths = write_tables(
                    outdir, self.results, self.log.toPlainText(),
                    include_sequence=self._run_include_sequence)
        except PermissionError as e:
            # The usual cause is the workbook still open in Excel.
            self._log(f"EXPORT FAILED: {e}")
            self.toasts.show("error", f"Export failed: {e}\nIs the workbook open in Excel?")
            return None
        except Exception as e:
            self._log(f"EXPORT FAILED: {e}")
            self.toasts.show("error", f"Export failed: {e}")
            return None

        self._log(f"Exported to {outdir}")
        for k, v in paths.items():
            self._log(f"  {k}: {v.name}")
        skipped_xlsx = "xlsx" not in paths
        if skipped_xlsx:
            self._log(f"  xlsx: skipped (> {EXCEL_ROW_LIMIT:,} detail rows; "
                      f"the full table is in detail.csv)")
        # The log file was written before these lines existed; refresh it.
        if "log" in paths:
            paths["log"].write_text(self.log.toPlainText(), encoding="utf-8")
        note = ("" if not skipped_xlsx else
                f"\nExcel skipped: over {EXCEL_ROW_LIMIT:,} rows; the full table "
                f"is in detail.csv.")
        folder = outdir
        self.toasts.show("success", f"Exported to {outdir.name}{note}",
                         [("Open folder",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))))])
        return outdir

    def export_results(self) -> None:
        if not self.results:
            self.toasts.show("info", "Nothing to export yet: run first."); return
        # The run's folder is reused while it holds no tables yet (auto-export
        # off, raw .out reports kept), so the export lands beside the reports.
        # Once it has tables, a manual export gets a fresh folder rather than
        # overwriting them: pressing Export twice gives two folders.
        outdir = self._run_outdir
        if outdir is not None and (outdir / "summary.csv").exists():
            outdir = None
        self._export(outdir=outdir)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    theme.follow_system(app)
    win = MainWindow()   # applies the saved theme before building anything
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
