"""Turning PairResults into tables.

Two views:
  * detail  -- one row per predicted amplicon (plus one row per sequence that
               produced none), with the flanking primer statistics
  * summary -- one row per (pair, target file)

The FASTA header is carried verbatim. Splitting it into accession / species /
group is assay-specific and therefore opt-in via `header_fields`.
"""
from __future__ import annotations

import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, NamedTuple, Sequence

import pandas as pd

from .model import Amplicon, PairResult, PrimerHit, PrimerPair, SequenceResult
from .scoring import CALLS, best_amplicon

# Detail rows past which export writes CSV only: openpyxl grows slow and memory-
# hungry as it approaches Excel's 1,048,576-row ceiling, and detail.csv already
# carries every row. Kept below the ceiling to leave room for the header.
EXCEL_ROW_LIMIT = 900_000


def _flanking(seq: SequenceResult, amp: Amplicon) -> tuple[PrimerHit | None, PrimerHit | None]:
    """The forward hit that opens the amplicon and the reverse hit that closes it."""
    fwd = next((h for h in seq.hits_for("forward") if h.start == amp.start), None)
    rev = next((h for h in seq.hits_for("reverse") if h.end == amp.end), None)
    return fwd, rev


_SCORE_KEYS = ("dg", "bound", "ext3", "efficiency", "mismatch_detail")


def _score_cols(prefix: str, h: PrimerHit | None) -> dict[str, object]:
    """The biological score of one site (scoring.SiteScore), when it has one."""
    sc = getattr(h, "score", None)
    if sc is None:
        return {f"{prefix}_{k}": None for k in _SCORE_KEYS}
    return {
        f"{prefix}_dg": sc.dg,
        f"{prefix}_bound": round(sc.bound, 3),
        f"{prefix}_ext3": round(sc.extension, 3),
        f"{prefix}_efficiency": round(sc.efficiency, 3),
        f"{prefix}_mismatch_detail": sc.mismatches,
    }


def _hit_cols(prefix: str, h: PrimerHit | None) -> dict[str, object]:
    if h is None:
        return {f"{prefix}_{k}": None for k in
                ("primer", "start", "end", "identity", "tm", "mismatches",
                 "hard_mismatches", "min_3prime_dist")}
    offs = h.mismatch_offsets_from_3prime()
    return {
        f"{prefix}_primer": h.primer_name,
        f"{prefix}_start": h.start,
        f"{prefix}_end": h.end,
        f"{prefix}_identity": h.identity,
        f"{prefix}_tm": h.tm,
        f"{prefix}_mismatches": h.mismatches,
        f"{prefix}_hard_mismatches": h.hard_mismatches,
        f"{prefix}_min_3prime_dist": min(offs) if offs else None,
    }


def _split_header(header: str, fields: Sequence[str] | None, sep: str) -> dict[str, object]:
    if not fields:
        return {}
    parts = header.split(sep)
    return {name: (parts[i] if i < len(parts) else None) for i, name in enumerate(fields)}


def detail_table(
    results: Iterable[PairResult],
    *,
    header_fields: Sequence[str] | None = None,
    header_sep: str = "|",
    include_sequence: bool = True,
    include_silent: bool = True,
) -> pd.DataFrame:
    results = list(results)
    tag_engine = _shows_engine(results)
    rows: list[dict[str, object]] = []
    for res in results:
        scored = res.score_ta is not None
        base = {"pair": res.pair.name, "target_file": Path(res.target_file).name}
        if tag_engine:
            base["engine"] = res.engine
        for seq in res.sequences:
            hdr = {"header": seq.header, **_split_header(seq.header, header_fields, header_sep)}
            if not seq.amplicons:
                rows.append({**base, **hdr, "amplified": False, "n_amplicons": 0,
                             "n_forward_hits": len(seq.hits_for("forward")),
                             "n_reverse_hits": len(seq.hits_for("reverse"))})
                continue
            for amp in seq.amplicons:
                f, r = _flanking(seq, amp)
                row: dict[str, object] = {
                    **base, **hdr,
                    "amplified": True,
                    "n_amplicons": seq.total_amplicons,
                    "n_unique_sizes": seq.unique_sizes,
                    "amplicon_start": amp.start,
                    "amplicon_end": amp.end,
                    "amplicon_size": amp.size,
                    "ta": amp.ta,
                    **_hit_cols("f", f), **_hit_cols("r", r),
                }
                if scored:
                    row.update({"delta_cq": amp.delta_cq, "length_delay": amp.length_delay,
                                "call": amp.call or None,
                                **_score_cols("f", f), **_score_cols("r", r)})
                if include_sequence:
                    row["amplicon_seq"] = amp.sequence.upper()
                    row["seq_complete"] = amp.sequence_complete
                rows.append(row)
        if include_silent:
            for h in res.silent_headers:
                rows.append({**base, "header": h,
                             **_split_header(h, header_fields, header_sep),
                             "amplified": False, "n_amplicons": 0,
                             "n_forward_hits": 0, "n_reverse_hits": 0})
    df = pd.DataFrame(rows)
    comp = comparison_table(results)
    if comp is not None and not comp.empty and not df.empty:
        # Each detail row learns whether the engines agree on its sequence.
        agree = {(r.pair, r.target_file, r.header): r.engines_agree
                 for r in comp.itertuples(index=False)}
        df["engines_agree"] = [agree.get(k) for k in
                               zip(df["pair"], df["target_file"], df["header"])]
    return df


def _shows_engine(results: Sequence[PairResult]) -> bool:
    """Whether to name the engine: only once anything but the JAR is involved,
    so a JAR-only run keeps the columns it always had."""
    return any(r.engine != "jar" for r in results)


def _predicted(seq: SequenceResult | None) -> bool:
    """A sequence the engine expects to amplify. For a scored product that
    means 'weak' or better; the JAR's own unscored call is taken as given."""
    if seq is None or not seq.amplicons:
        return False
    calls = [a.call for a in seq.amplicons if a.call]
    return not calls or any(c != "unlikely" for c in calls)


def comparison_table(results: Iterable[PairResult]) -> pd.DataFrame | None:
    """One row per sequence and pair: what each engine predicted, side by side.

    None unless both the JAR and the built-in engine ran. `agreement` is
    'both', 'neither', 'jar only' or 'thermo only'; the last two are the cases
    worth a closer look. A header repeated in the FASTA is matched by order.
    """
    results = list(results)
    if {r.engine for r in results} != {"jar", "thermo"}:
        return None
    by_job: dict[tuple[str, str], dict[str, PairResult]] = {}
    for r in results:
        by_job.setdefault((r.pair.name, Path(r.target_file).name), {})[r.engine] = r

    def entries(res: PairResult) -> dict[str, list[SequenceResult | None]]:
        out: dict[str, list[SequenceResult | None]] = {}
        for s in res.sequences:
            out.setdefault(s.header, []).append(s)
        for h in res.silent_headers:
            out.setdefault(h, []).append(None)
        return out

    def sizes(seq: SequenceResult | None) -> str:
        return ";".join(str(x) for x in sorted({a.size for a in seq.amplicons})) if seq else ""

    rows = []
    for (pair, target), eng in by_job.items():
        if set(eng) != {"jar", "thermo"}:
            continue        # one engine failed for this job: nothing to compare
        jar, thermo = entries(eng["jar"]), entries(eng["thermo"])
        for header in dict.fromkeys([*jar, *thermo]):
            js, ts = jar.get(header, []), thermo.get(header, [])
            for k in range(max(len(js), len(ts))):
                j = js[k] if k < len(js) else None
                t = ts[k] if k < len(ts) else None
                jp, tp = _predicted(j), _predicted(t)
                best_j = best_amplicon(j) if j else None
                best_t = best_amplicon(t) if t else None
                rows.append({
                    "pair": pair, "target_file": target, "header": header,
                    "jar_amplified": jp, "jar_sizes": sizes(j),
                    "jar_call": best_j.call if best_j else None,
                    "thermo_amplified": tp, "thermo_sizes": sizes(t),
                    "thermo_call": best_t.call if best_t else None,
                    "thermo_delta_cq": best_t.delta_cq if best_t else None,
                    "agreement": ("both" if jp and tp else "neither" if not (jp or tp)
                                  else "jar only" if jp else "thermo only"),
                    "engines_agree": jp == tp,
                })
    return pd.DataFrame(rows)


def summary_table(results: Iterable[PairResult]) -> pd.DataFrame:
    results = list(results)
    tag_engine = _shows_engine(results)
    comp = comparison_table(results)
    agreement: dict[tuple[str, str], float] = {}
    if comp is not None and not comp.empty:
        for (pair, target), g in comp.groupby(["pair", "target_file"]):
            agreement[(pair, target)] = round(100 * g["engines_agree"].mean(), 2)
    rows = []
    for res in results:
        n_seen = len(res.sequences)
        n_total = n_seen + len(res.silent_headers)
        amplified = [s for s in res.sequences if s.amplified]
        sizes = [a.size for s in amplified for a in s.amplicons]
        tas = [a.ta for s in amplified for a in s.amplicons if a.ta is not None]
        # Two products of equal length run as one band on a gel; only distinct
        # sizes are resolvable, so the two counts answer different questions.
        multi_amplicon = sum(1 for s in amplified if s.total_amplicons > 1)
        multiband = sum(1 for s in amplified if s.unique_sizes > 1)
        row: dict[str, object] = {
            **({"engine": res.engine} if tag_engine else {}),
            "pair": res.pair.name,
            "forward": res.pair.forward,
            "reverse": res.pair.reverse,
            "target_file": Path(res.target_file).name,
            "n_sequences": n_total,
            "n_with_hits": n_seen,
            "n_amplified": len(amplified),
            "pct_amplified": round(100 * len(amplified) / n_total, 2) if n_total else 0.0,
            "n_multi_amplicon": multi_amplicon,
            "n_multiband": multiband,
            "size_min": min(sizes) if sizes else None,
            "size_median": int(pd.Series(sizes).median()) if sizes else None,
            "size_max": max(sizes) if sizes else None,
            "ta_median": round(float(pd.Series(tas).median()), 1) if tas else None,
            "jar_time": res.elapsed,
        }
        if res.score_ta is not None:
            # One call per sequence: its most likely product decides.
            calls = Counter(b.call for s in amplified
                            if (b := best_amplicon(s)) is not None and b.call)
            likely = calls["strong"] + calls["probable"]
            row.update({
                "score_polymerase": res.score_polymerase,
                "score_ta": res.score_ta,
                "score_ta_source": res.score_ta_source,
                **{f"n_{c}": calls[c] for c in CALLS},
                "pct_likely": round(100 * likely / n_total, 2) if n_total else 0.0,
            })
        if agreement:
            row["pct_engine_agreement"] = agreement.get(
                (res.pair.name, Path(res.target_file).name))
        rows.append(row)
    if not rows:   # e.g. a run cancelled before any job finished
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if tag_engine:
        # A pair's engines stay adjacent; pairs ordered by the best of them.
        best = df.groupby(["pair", "target_file"])["pct_amplified"].transform("max")
        return (df.assign(_best=best)
                  .sort_values(["_best", "pair", "target_file", "engine"],
                               ascending=[False, True, True, True], ignore_index=True)
                  .drop(columns="_best"))
    return df.sort_values("pct_amplified", ascending=False, ignore_index=True)


def write_excel(
    path: str | Path,
    summary: pd.DataFrame,
    detail: pd.DataFrame | None = None,
    max_detail_rows: int = 1_000_000,
    comparison: pd.DataFrame | None = None,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="Summary", index=False)
        if detail is not None and not detail.empty:
            detail.head(max_detail_rows).to_excel(xl, sheet_name="Detail", index=False)
        if comparison is not None and not comparison.empty:
            comparison.head(max_detail_rows).to_excel(xl, sheet_name="Engine comparison",
                                                      index=False)
        for name in xl.sheets:
            ws = xl.sheets[name]
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col[:200] if c.value is not None),
                            default=8)
                ws.column_dimensions[col[0].column_letter].width = min(width + 2, 60)


_WIN_FORBIDDEN = '<>:"/\\|?*'


def slugify(name: str) -> str:
    """A pair name made safe for a folder name. '|' reads best as '-'."""
    out = []
    for c in name:
        if c == "|":
            out.append("-")
        elif c in _WIN_FORBIDDEN or c.isspace():
            out.append("_")
        else:
            out.append(c)
    return "".join(out).strip("._ ") or "run"


def run_folder_name(
    pairs: Sequence[PrimerPair],
    when: datetime | None = None,
    label: str = "",
    max_names: int = 3,
    max_len: int = 70,
) -> str:
    """`<label>_<timestamp>`, e.g. 'L15411F-H15546R_20260709-143205'.

    A user-supplied `label` wins. Otherwise the pair names are used, and beyond
    `max_names` pairs they stop being a useful label so the count is used
    instead; the pairs are listed in summary.csv and run_log.txt anyway.
    """
    ts = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    if label.strip():
        return f"{slugify(label)[:max_len].rstrip('._-')}_{ts}"
    names = [slugify(p.name) for p in pairs]
    if not names:
        return ts
    stem = "__".join(names) if len(names) <= max_names else f"{len(names)}-pairs"
    if len(stem) > max_len:
        stem = f"{len(names)}-pairs" if len(names) > 1 else stem[:max_len].rstrip("._-")
    return f"{stem}_{ts}"


class OutputCheck(NamedTuple):
    ok: bool
    message: str     # what the tile shows: "writable", "will be created", or the problem


def check_output_dir(path: str | Path) -> OutputCheck:
    """Can a run's folder be written under `path`? Tried, not assumed.

    An existing folder is tested by creating and removing a temporary file in
    it: on Windows, os.access answers from the read-only attribute and ignores
    ACLs and share permissions, so it says yes to folders that refuse writes.
    A folder that does not exist yet is judged by its nearest existing parent,
    which is where it would be created; none at all means the drive is gone.
    """
    text = str(path).strip()
    if not text:
        return OutputCheck(False, "no folder set")
    p = Path(text).expanduser()
    if p.exists() and not p.is_dir():
        return OutputCheck(False, "a file, not a folder")
    probe = next((a for a in (p, *p.parents) if a.exists()), None)
    if probe is None or (p.anchor and not Path(p.anchor).exists()):
        drive = p.drive or p.anchor or text
        return OutputCheck(False, f"{drive} is not available")
    if not probe.is_dir():
        return OutputCheck(False, f"{probe.name} is a file, not a folder")
    try:
        with tempfile.TemporaryFile(dir=probe):
            pass
    except OSError:
        return OutputCheck(False, "no permission to write here" if probe == p
                           else f"no permission to create it in {probe}")
    return OutputCheck(True, "writable" if probe == p else "will be created")


def unique_dir(base: str | Path, name: str) -> Path:
    """`base/name`, suffixed if a run in the same second already claimed it."""
    base = Path(base)
    target = base / name
    i = 2
    while target.exists():
        target = base / f"{name}_{i}"
        i += 1
    return target


def write_tables(
    outdir: str | Path,
    results: Iterable[PairResult],
    log_text: str = "",
    *,
    xlsx_row_limit: int = EXCEL_ROW_LIMIT,
    **kw,
) -> dict[str, Path]:
    """Write summary + detail as CSV, a combined workbook, and the run log.

    The .xlsx is skipped when the detail table exceeds `xlsx_row_limit`: near
    Excel's row ceiling openpyxl is slow and memory-hungry, and detail.csv holds
    the full table anyway. Its absence from the returned paths is the signal.
    """
    results = list(results)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    summary = summary_table(results)
    detail = detail_table(results, **kw)
    paths = {
        "summary_csv": outdir / "summary.csv",
        "detail_csv": outdir / "detail.csv",
    }
    summary.to_csv(paths["summary_csv"], index=False)
    detail.to_csv(paths["detail_csv"], index=False)
    comparison = comparison_table(results)
    if comparison is not None:
        paths["comparison_csv"] = outdir / "engine_comparison.csv"
        comparison.to_csv(paths["comparison_csv"], index=False)
    if len(detail) <= xlsx_row_limit:
        paths["xlsx"] = outdir / "vpcr_results.xlsx"
        write_excel(paths["xlsx"], summary, detail, comparison=comparison)
    if log_text:
        paths["log"] = outdir / "run_log.txt"
        paths["log"].write_text(log_text, encoding="utf-8")
    return paths


def export_run(
    base_dir: str | Path,
    results: Iterable[PairResult],
    log_text: str = "",
    when: datetime | None = None,
    label: str = "",
    **kw,
) -> tuple[Path, dict[str, Path]]:
    """Write one run into its own `<label>_<timestamp>` folder under base_dir.

    Returns the folder and the files written inside it.
    """
    results = list(results)
    pairs = list({r.pair.name: r.pair for r in results}.values())
    outdir = unique_dir(base_dir, run_folder_name(pairs, when, label))
    return outdir, write_tables(outdir, results, log_text, **kw)
