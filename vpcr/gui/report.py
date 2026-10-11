"""The PDF run report: a few pages to file or share, with the virtual gel.

What a reader needs to judge a run without the app: the headline numbers,
the gel with a key to its lanes, the result per pair, the primers, and how
the run was set up — engine, JAR build and its self-test, Java, parameters.
The full tables stay in the CSV / Excel files beside it.

Laid out by QTextDocument from HTML and printed through QPdfWriter, so it
needs nothing beyond Qt. US Letter, the standard where the app is used.
The gel is the app's own GelView, rendered offscreen at print resolution.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QMarginsF, QSize, Qt, QUrl
from PyQt6.QtGui import QFont, QImage, QPageLayout, QPageSize, QPdfWriter, QTextDocument

from vpcr import APP_NAME, __version__
from vpcr.core.export import summary_table
from vpcr.core.model import PairResult
from vpcr.gui.summary_view import GelView, Lane, gel_lanes, kpis

PAGE = QPageSize.PageSizeId.Letter
# Gel width on the page (document px, ~96 per inch): beside its key when it
# is narrow, across the page with the key below when many lanes widen it.
_GEL_SIDE_W, _GEL_FULL_W = 250, 640
_GEL_H = 380
_SCALE = 3          # rendered at 3x for print

_CSS = """
body { font-size: 9pt; color: #1d2329; }
h1 { font-size: 17pt; font-weight: 600; margin: 0; color: #0f6e56; }
h2 { font-size: 11.5pt; font-weight: 600; margin-top: 16px; margin-bottom: 4px;
     color: #0f6e56; }
.sub { color: #5b6670; font-size: 8.5pt; }
table.grid { border-collapse: collapse; }
table.grid th { background: #e8efed; font-weight: 600; text-align: left; padding: 3px 6px; }
table.grid td { padding: 3px 6px; border-bottom: 1px solid #e3e7ea; }
td.num { text-align: right; }
table.kv td { padding: 2px 10px 2px 0; }
.k { color: #5b6670; }
.kpi { font-size: 15pt; font-weight: 600; }
.note { color: #5b6670; font-size: 8pt; }
.mono { font-family: Consolas, 'Courier New', monospace; font-size: 8pt; }
.warn { background: #fdf1e3; color: #7a4b0c; padding: 6px; }
"""


@dataclass
class RunInfo:
    """How a run was set up, captured when Run was pressed."""
    started: datetime
    label: str = ""
    engine: str = ""                 # as shown, e.g. "virtualPCR JAR"
    jar: str = ""                    # path, when the JAR ran
    java: str = ""                   # e.g. "Java 25"
    circular_via_jar: bool = False   # circular templates went through the JAR
    params: list[tuple[str, str]] = field(default_factory=list)


def _esc(v) -> str:
    return html.escape(str(v), quote=False)


def _num(v) -> bool:
    return v is not None and v == v      # not None, not NaN


def gel_image(lanes: list[Lane], width_cap: int) -> tuple[QImage, int, int]:
    """The gel at print resolution, and its size on the page."""
    gel = GelView()
    gel.set_lanes(lanes)
    gel.set_zoom(1.8 if len(lanes) <= 6 else 1.2)
    size = QSize(gel.sizeHint().width(), _GEL_H)
    gel.resize(size)
    img = QImage(size * _SCALE, QImage.Format.Format_ARGB32)
    img.setDevicePixelRatio(_SCALE)
    img.fill(Qt.GlobalColor.transparent)
    gel.render(img)
    w = min(size.width(), width_cap)
    return img, w, round(size.height() * w / size.width())


def _jar_lines(jar: str) -> list[tuple[str, str]]:
    from vpcr.core import jar_check
    try:
        sha = jar_check.jar_sha256(jar)
    except OSError:
        return [("JAR", Path(jar).name)]
    verdict = jar_check.cached(sha)
    test = ("not run" if verdict is None else
            f"passed ({verdict['date']})" if verdict["ok"] else
            f"FAILED ({verdict['date']})")
    return [("JAR build", jar_check.describe(sha)), ("JAR SHA-256", sha[:16] + "…"),
            ("Self-test", test)]


def build_html(results: list[PairResult], info: RunInfo) -> tuple[str, QImage | None]:
    summary = summary_table(results)
    lanes = gel_lanes(results)
    both = len({ln.engine for ln in lanes}) > 1
    H = [f"<html><head><style>{_CSS}</style></head><body>"]
    H.append("<h1>In silico PCR report</h1><div class='sub'>"
             + (f"<b>{_esc(info.label)}</b> · " if info.label else "")
             + f"run {info.started:%Y-%m-%d %H:%M} · {APP_NAME} {__version__}</div>")

    # What makes the numbers below partial, said first.
    warns = []
    if getattr(results, "cancelled", False):
        warns.append("The run was cancelled: these results cover the jobs finished by then.")
    if errs := getattr(results, "errors", []):
        shown = "; ".join(f"{p} / {t}" for p, t, _ in errs[:5])
        warns.append(f"{len(errs)} job(s) failed and are missing: {_esc(shown)}"
                     + (" …" if len(errs) > 5 else "") + ".")
    for w in warns:
        H.append(f"<p class='warn'>{w}</p>")

    if summary.empty:
        H.append("<p>No results.</p></body></html>")
        return "".join(H), None

    H.append("<h2>Overview</h2><table width='100%'><tr>")
    for k in kpis(summary, results):
        H.append(f"<td width='25%' valign='top'><div class='k'>{_esc(k.title.upper())}</div>"
                 f"<div class='kpi'>{_esc(k.value)}</div>"
                 f"<div class='note'>{_esc(k.caption)}</div></td>")
    H.append("</tr></table>")

    # The gel and its key.
    names = list(dict.fromkeys(ln.pair for ln in lanes))
    img, w, h = gel_image(lanes, _GEL_FULL_W)
    # Beside the key while shrinking it to fit keeps the lanes readable.
    side = w <= 1.4 * _GEL_SIDE_W and len(names) <= 12
    if side:
        w, h = min(w, _GEL_SIDE_W), round(h * min(w, _GEL_SIDE_W) / w)
    key = ["<table class='grid'><tr><th>Lane</th><th>Pair</th>"
           f"<th>Amplify{' (JAR / built-in)' if both else ''}</th>"
           "<th>Main sizes (bp)</th></tr>"]
    for i, n in enumerate(names, 1):
        mine = [x for x in lanes if x.pair == n]
        pct = " / ".join(f"{x.pct:.0f}%" for x in mine)
        top = ", ".join(str(s) for s, _ in mine[0].sizes.most_common(3)) or "—"
        # The JAR lane's sizes when both ran: the built-in lane's sit beside it.
        key.append(f"<tr><td class='num'>{i}</td><td>{_esc(n)}</td>"
                   f"<td class='num'>{pct}</td><td>{top}</td></tr>")
    key.append("</table><p class='note'>L = size ladder (bp, log scale). One lane per pair"
               + ("; J = virtualPCR JAR, B = built-in engine" if both else "")
               + ". A band's brightness is the share of sequences giving that size, so a "
               "faint band is a minority product. Sizes are predicted, not measured: the "
               "gel shows what the templates allow, not how well each band would "
               "amplify.</p>")
    gel = f"<img src='gel.png' width='{w}' height='{h}'>"
    H.append("<h2>Virtual gel</h2>")
    if side:
        H.append(f"<table width='100%'><tr><td width='{w + 12}' valign='top'>{gel}</td>"
                 f"<td valign='top'>{''.join(key)}</td></tr></table>")
    else:
        H.append(f"<p style='margin-bottom: 10px'>{gel}</p>{''.join(key)}")

    # Result per pair.
    one_file = summary["target_file"].nunique() == 1
    scored = "pct_likely" in summary
    has_engine = "engine" in summary
    H.append("<h2>Results by pair</h2>")
    if one_file:
        H.append(f"<p class='sub'>Target: {_esc(summary['target_file'].iloc[0])}</p>")
    cols = (["Pair"] + (["Engine"] if has_engine else []) + ([] if one_file else ["Target file"])
            + ["Sequences", "Amplified"] + (["Likely"] if scored else [])
            + [">1 size", "Size, median (range)", "Ta"]
            + (["Engines agree"] if "pct_engine_agreement" in summary else []))
    H.append("<table class='grid' width='100%'><tr>"
             + "".join(f"<th>{_esc(c)}</th>" for c in cols) + "</tr>")
    for r in summary.to_dict("records"):
        size = (f"{int(r['size_median'])} ({int(r['size_min'])}–{int(r['size_max'])})"
                if _num(r.get("size_median")) else "—")
        ta = r.get("score_ta") if scored else r.get("ta_median")
        cells = ([_esc(r["pair"])]
                 + ([("JAR" if r["engine"] == "jar" else "built-in")] if has_engine else [])
                 + ([] if one_file else [_esc(r["target_file"])]))
        nums = ([f"{r['n_sequences']:,}", f"{r['pct_amplified']:g}%"]
                + ([f"{r['pct_likely']:g}%"] if scored else [])
                + [f"{r['n_multiband']:,}", size, f"{ta:g} °C" if _num(ta) else "—"]
                + ([f"{r['pct_engine_agreement']:g}%" if _num(r.get('pct_engine_agreement'))
                    else "—"] if "pct_engine_agreement" in summary else []))
        H.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells)
                 + "".join(f"<td class='num'>{c}</td>" for c in nums) + "</tr>")
    H.append("</table><p class='note'><i>Amplified</i>: the engine predicts a product."
             + (" <i>Likely</i>: its biological score calls it strong or probable."
                if scored else "")
             + " <i>&gt;1 size</i>: sequences giving bands of more than one size.</p>")

    # Primers.
    pairs = list({r.pair.name: r.pair for r in results}.values())
    H.append("<h2>Primers</h2><table class='grid' width='100%'><tr><th>Pair</th>"
             "<th>Forward 5′→3′</th><th>Reverse 5′→3′</th></tr>")
    for p in pairs:
        H.append(f"<tr><td>{_esc(p.name)}</td><td class='mono'>{_esc(p.forward)}</td>"
                 f"<td class='mono'>{_esc(p.reverse)}</td></tr>")
    H.append("</table>")

    # Setup: two key/value columns per row, so a page break falls between
    # rows and never splits the section into two half-tables.
    targets = (summary.drop_duplicates("target_file")[["target_file", "n_sequences"]]
               .itertuples(index=False))
    left = [("Target", f"{t} ({n:,} sequences)") for t, n in targets]
    left.append(("Engine", info.engine))
    if info.jar:
        left += _jar_lines(info.jar)
    if info.java:
        left.append(("Java", info.java))
    right = list(info.params)
    H.append("<h2>Run setup</h2><table class='kv' width='100%'>")
    for i in range(max(len(left), len(right))):
        row = ""
        for col in (left, right):
            k, v = col[i] if i < len(col) else ("", "")
            row += f"<td class='k' valign='top'>{_esc(k)}</td><td valign='top'>{_esc(v)}</td>"
        H.append(f"<tr>{row}</tr>")
    H.append("</table>")
    notes = []
    if info.circular_via_jar:
        notes.append("Circular templates were searched through the JAR's linear mode, on "
                     "copies with their first bases appended, and mapped back onto the "
                     "circle: the JAR's own circular mode misreports products across the "
                     "origin (see the user guide).")
    notes.append("Full tables: summary.csv, detail.csv and vpcr_results.xlsx in the run "
                 "folder, when the run was exported.")
    H.append(f"<p class='note'>{' '.join(notes)}</p></body></html>")
    return "".join(H), img


def write_report(path: str | Path, results: list[PairResult], info: RunInfo) -> Path:
    """Write the PDF report for `results` to `path`; returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body, img = build_html(results, info)
    doc = QTextDocument()
    doc.setDefaultFont(QFont("Segoe UI", 9))
    if img is not None:
        doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl("gel.png"), img)
    doc.setHtml(body)
    writer = QPdfWriter(str(path))
    # No margins here: QTextDocument.print adds its own 2 cm, and page numbers.
    writer.setPageLayout(QPageLayout(QPageSize(PAGE), QPageLayout.Orientation.Portrait,
                                     QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter))
    writer.setResolution(300)
    writer.setTitle("In silico PCR report" + (f" — {info.label}" if info.label else ""))
    writer.setCreator(f"{APP_NAME} {__version__}")
    doc.print(writer)
    del writer          # flushes and closes the file
    return path
