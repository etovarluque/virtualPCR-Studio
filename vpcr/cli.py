"""Command-line front end. The GUI drives the same vpcr.core functions.

    python -m vpcr.cli --primers pairs.tsv \
                       --targets a.fasta b.fasta --out results/ --circular
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from vpcr import JAR_DIR
from vpcr.core.export import export_run, write_tables
from vpcr.core.model import STUDIO_DEFAULTS, RunConfig
from vpcr.core.primers import combine, load_pairs, primer_set_from_pairs
from vpcr.core.runner import JarError, run_batch


def _default_jar() -> Path | None:
    """The JAR shipped in jar/, newest first if several were dropped there."""
    jars = sorted(JAR_DIR.glob("*.jar")) if JAR_DIR.is_dir() else []
    return jars[-1] if jars else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="In silico PCR over FASTA targets.")
    ap.add_argument("--jar", type=Path, default=None,
                    help="virtualPCR.jar (default: the one in jar/)")
    ap.add_argument("--primers", required=True, type=Path,
                    help="FASTA (F,R alternating), TSV/CSV or XLSX of pairs")
    ap.add_argument("--sheet", default="0",
                    help="worksheet name, or 0-based index, for XLSX primers")
    ap.add_argument("--targets", required=True, nargs="+", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    # Same defaults as the GUI, so both routes give the same result.
    d = STUDIO_DEFAULTS
    ap.add_argument("--minlen", type=int, default=d["min_len"])
    ap.add_argument("--maxlen", type=int, default=d["max_len"])
    ap.add_argument("--errors3", type=int, default=d["n_3prime_errors"],
                    help="mismatches near the 3' end")
    topo = ap.add_mutually_exclusive_group()
    topo.add_argument("--circular", dest="circular", action="store_true",
                      default=d["circular"],
                      help="circular template" + (" (default)" if d["circular"] else ""))
    topo.add_argument("--linear", dest="circular", action="store_false",
                      help="linear template" + ("" if d["circular"] else " (default)"))
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--no-sequence", action="store_true", help="skip amplicon sequences")
    ap.add_argument("--no-repair", action="store_true",
                    help="keep the JAR's amplicon sequences verbatim (1 base short)")
    ap.add_argument("--workers", type=int, default=d["workers"])
    ap.add_argument("--heap", default=None, help="JVM max heap, e.g. 8g")
    ap.add_argument("--keep-reports", action="store_true", help="save raw .out files")
    ap.add_argument("--subfolder", action="store_true",
                    help="write into out/<primers>_<timestamp>/ instead of out/")
    ap.add_argument("--label", default="",
                    help="names the --subfolder instead of the primer pairs")
    ap.add_argument("--combine", action="store_true",
                    help="run every forward against every reverse, ignoring the "
                         "file's own pairing")
    ap.add_argument("--java", default=None)
    ap.add_argument("--header-fields", default=None,
                    help="comma-separated names to split the FASTA header into, "
                         "e.g. 'accession,species,moltype,topology,length,group'")
    ap.add_argument("--header-sep", default="|")
    args = ap.parse_args(argv)

    if args.jar is None:
        args.jar = _default_jar()
        if args.jar is None:
            print(f"error: no .jar in {JAR_DIR}; pass --jar", file=sys.stderr)
            return 2
        print(f"using {args.jar}")

    # Everything echoed to the console is also archived as run_log.txt.
    log: list[str] = []

    def say(msg: str = "") -> None:
        print(msg, flush=True)
        log.append(msg)

    if args.minlen > args.maxlen:
        print(f"error: --minlen {args.minlen} exceeds --maxlen {args.maxlen}", file=sys.stderr)
        return 2

    # argparse hands over a string; pandas reads "1" as a sheet *named* "1".
    sheet: str | int = int(args.sheet) if str(args.sheet).isdigit() else args.sheet
    try:
        pairs = load_pairs(args.primers, sheet=sheet)
        if args.combine:
            pset = primer_set_from_pairs(pairs)
            pairs = combine(pset.forwards, pset.reverses)
    except Exception as e:
        print(f"error reading primers: {e}", file=sys.stderr)
        return 2

    cfg = RunConfig(
        min_len=args.minlen, max_len=args.maxlen, n_3prime_errors=args.errors3,
        circular=args.circular, probe=args.probe,
        sequence_extract=not args.no_sequence,
    )

    say(f"virtualPCR run — {datetime.now():%Y-%m-%d %H:%M:%S}")
    say(f"JAR       : {args.jar}")
    say(f"Amplicon  : {cfg.min_len}-{cfg.max_len} bp, 3' mismatches {cfg.n_3prime_errors}")
    say(f"Template  : {'circular' if cfg.circular else 'linear'}"
        f"{', probe search' if cfg.probe else ''}")
    for t in args.targets:
        say(f"Target    : {t}")
    for p in pairs:
        say(f"Primer    : {p.name}  {p.forward} / {p.reverse}")

    total = len(pairs) * len(args.targets)
    t0 = time.time()

    def progress(done: int, n: int, label: str) -> None:
        say(f"  [{done}/{n}] {label}")

    say(f"\nrunning {total} job(s)...")
    try:
        results = run_batch(
            pairs, args.targets, args.jar, cfg,
            java_path=args.java, workers=args.workers, heap=args.heap,
            reports_dir=(args.out / "reports") if args.keep_reports else None,
            repair=not args.no_repair, progress=progress,
        )
    except JarError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # A failed job leaves its pair out of every table; say so where the tables
    # are archived, not only in the scrollback.
    if results.errors:
        say(f"\nWARNING: {len(results.errors)} of {results.total} job(s) failed; "
            f"their pairs are missing from the tables:")
        for pair, target, msg in results.errors:
            say(f"  {pair} / {target}: {msg}")

    if not results:
        print("no results produced", file=sys.stderr)
        return 1

    fields = args.header_fields.split(",") if args.header_fields else None
    kw = dict(header_fields=fields, header_sep=args.header_sep,
              include_sequence=not args.no_sequence)
    if args.subfolder:
        outdir, paths = export_run(args.out, results, log_text="\n".join(log),
                                   label=args.label, **kw)
        print(f"\ndone in {time.time() - t0:.1f}s -> {outdir}")
    else:
        outdir, paths = args.out, write_tables(args.out, results, "\n".join(log), **kw)
        print(f"\ndone in {time.time() - t0:.1f}s")
    for k, v in paths.items():
        print(f"  {k}: {v}")
    # Tables were written, but a partial run must not look like a clean one
    # to a script checking the exit status.
    return 0 if results.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
