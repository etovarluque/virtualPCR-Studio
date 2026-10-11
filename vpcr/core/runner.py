"""Driving the virtualPCR JAR.

One JAR invocation per (primer pair, target file). That is deliberate: given
several primers at once the JAR pairs *any* forward with *any* reverse and
reports chimeric amplicons across unrelated pairs. Its FRpairs mode avoids
that but stops reporting amplicon sequences and Ta. Running one pair at a time
keeps attribution unambiguous and the report rich; a full mitogenome set costs
a few seconds per pair, and pairs run in parallel.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from .model import PairResult, PrimerPair, RunConfig
from .parser import fasta_headers, finalize, iter_fasta, parse_report
from .primers import write_pair_fasta
from .scoring import Reaction, score_result
from .thermo_engine import run_thermo
from .topology import circular_for

# 'jar' runs virtualPCR, 'thermo' the built-in engine, 'both' runs each pair
# through both so their predictions can be compared, and 'auto' is the JAR
# when a Java on this machine can run it, else the built-in engine.
ENGINES = ("auto", "jar", "thermo", "both")

# Java class file major version -> feature release (45 == Java 1.1)
_CLASS_MAJOR_BASE = 44

# Under pythonw there is no console for java.exe to inherit, so Windows would
# open one per JAR run. Harmless elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# How often the batch looks at should_cancel while JAR runs are in flight.
_CANCEL_POLL_S = 0.2


class JarError(RuntimeError):
    pass


class Cancelled(JarError):
    """The batch was cancelled while this job was queued or running."""


class BatchResults(list):
    """The PairResults of a batch, plus what did not make it into them.

    A plain list to every existing caller; `errors` and `cancelled` let a caller
    tell a complete run from a partial one, which a list alone cannot show.
    """

    def __init__(self, items: Iterable[PairResult] = (), *, total: int = 0,
                 errors: list[tuple[str, str, str]] | None = None,
                 cancelled: bool = False) -> None:
        super().__init__(items)
        self.total = total
        # (pair name, target file name, message) per failed job
        self.errors = errors or []
        self.cancelled = cancelled

    @property
    def complete(self) -> bool:
        return not self.cancelled and not self.errors and len(self) == self.total


class _Processes:
    """JAR processes in flight, so a cancel can stop them rather than wait."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()
        self.cancelled = False

    def run(self, cmd: list[str], cwd: Path) -> tuple[int, str, str]:
        with self._lock:
            # Checked under the lock: a cancel arriving now either sees this
            # process in the set or stops it from starting.
            if self.cancelled:
                raise Cancelled("cancelled")
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    errors="replace", creationflags=_NO_WINDOW)
            self._procs.add(proc)
        try:
            out, err = proc.communicate()
        finally:
            with self._lock:
                self._procs.discard(proc)
        if self.cancelled:
            raise Cancelled("cancelled")
        return proc.returncode, out, err

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            for proc in self._procs:
                try:
                    proc.kill()
                except OSError:
                    pass


@dataclass(frozen=True)
class JavaInfo:
    executable: str
    feature: int          # e.g. 24
    raw: str


def _probe_java(exe: str) -> JavaInfo | None:
    try:
        p = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = (p.stderr or p.stdout).strip()
    # The version line, not whatever comes first: 'Picked up JAVA_TOOL_OPTIONS'
    # and similar notices can precede it.
    line = next((ln for ln in raw.splitlines() if " version " in ln),
                raw.splitlines()[0] if raw else "")
    feature = 0
    for tok in line.replace('"', " ").split():
        parts = tok.split(".")
        if parts[0].isdigit():
            # Java 8 and older report '1.8.0_392': the feature is the second field.
            if parts[0] == "1" and len(parts) > 1 and parts[1].isdigit():
                feature = int(parts[1])
            else:
                feature = int(parts[0])
            break
    return JavaInfo(exe, feature, line)


def find_java(explicit: str | None = None) -> JavaInfo:
    exe = explicit or os.environ.get("JAVA_HOME_BIN") or shutil.which("java")
    if not exe:
        raise JarError("java not found on PATH; install a JDK or set the path explicitly")
    info = _probe_java(exe)
    if info is None:
        raise JarError(f"could not run {exe}")
    return info


def discover_javas() -> list[JavaInfo]:
    """Every runnable JRE we can find, newest first."""
    seen: dict[str, JavaInfo] = {}
    cands: list[str] = []
    if (w := shutil.which("java")):
        cands.append(w)
    for root in (r"C:\Program Files\Java", r"C:\Program Files\Eclipse Adoptium",
                 "/usr/lib/jvm", "/Library/Java/JavaVirtualMachines"):
        p = Path(root)
        if not p.is_dir():
            continue
        for jdk in p.iterdir():
            for rel in ("bin/java.exe", "bin/java", "Contents/Home/bin/java"):
                exe = jdk / rel
                if exe.exists():
                    cands.append(str(exe))
    for exe in cands:
        if exe in seen:
            continue
        if (info := _probe_java(exe)):
            seen[exe] = info
    return sorted(seen.values(), key=lambda j: j.feature, reverse=True)


def select_java(jar: str | Path, explicit: str | None = None) -> JavaInfo:
    """A JRE new enough to run this JAR, preferring an explicit choice."""
    if explicit:
        java = find_java(explicit)
        check_compatibility(jar, java)
        return java
    need = jar_class_version(jar)
    # PATH first, but its absence is not the end: a JDK installed without
    # touching PATH is still found by discover_javas().
    try:
        java: JavaInfo | None = find_java()
    except JarError:
        java = None
    if java is not None and java.feature >= need:
        return java
    for cand in discover_javas():
        if cand.feature >= need:
            return cand
    if java is None:
        raise JarError(
            f"{Path(jar).name} needs Java {need}+ but no Java was found on PATH or "
            f"in the usual install folders. Install a JDK {need} or newer."
        )
    raise JarError(
        f"{Path(jar).name} needs Java {need}+ but only Java {java.feature} was found. "
        f"Install a newer JDK, or use a JAR built for Java {java.feature}."
    )


def jar_class_version(jar: str | Path) -> int:
    """Feature release the JAR was compiled for, from its main class."""
    with zipfile.ZipFile(jar) as z:
        name = next((n for n in z.namelist() if n.endswith("virtualpcr.class")), None)
        if name is None:
            raise JarError(f"{jar} does not contain virtualpcr.class")
        head = z.read(name)[:8]
    if head[:4] != b"\xca\xfe\xba\xbe":
        raise JarError("not a Java class file")
    (major,) = struct.unpack(">H", head[6:8])
    return major - _CLASS_MAJOR_BASE


def check_compatibility(jar: str | Path, java: JavaInfo) -> None:
    need = jar_class_version(jar)
    if java.feature and java.feature < need:
        raise JarError(
            f"{Path(jar).name} needs Java {need}+ but '{java.executable}' is Java "
            f"{java.feature}. Install a newer JDK or use a JAR built for Java {java.feature}."
        )


def resolve_engine(engine: str, jar: str | Path | None,
                   java_path: str | None = None) -> tuple[str, str]:
    """`engine` with 'auto' settled, and why: the JAR when it can run here,
    else the built-in engine. The reason is empty for any other choice."""
    if engine != "auto":
        return engine, ""
    if jar is None or not Path(jar).is_file():
        return "thermo", "no virtualPCR.jar"
    from .jar_check import cached, jar_sha256
    verdict = cached(jar_sha256(jar))
    if verdict is not None and not verdict["ok"]:
        return "thermo", "this virtualPCR.jar build failed its self-test"
    try:
        select_java(jar, java_path)
    except JarError as e:
        return "thermo", str(e)
    return "jar", ""


def _linearize_circles(target: Path, setting: bool | str, max_len: int,
                       workdir: Path) -> tuple[Path, dict[str, int]]:
    """The file the JAR reads, and its circular records (header -> length).

    The JAR's own circular mode is not used: run on the 2026-07-08 build, it
    reports every product a second time at its size plus the template length
    (a turn of the circle no polymerase makes), gives products across the
    origin no coordinates and a size one base short, misplaces a site that
    starts at base 1, and scores a site cut by the origin at a fraction of
    its identity. Its linear mode has none of these faults. So each circular
    record is written with its first bases appended, as many as the longest
    product needs, and searched as a line; _fold_circular maps the result
    back onto the circle. With no circular record the target is used as is.
    """
    circ: dict[str, int] = {}
    if setting is False or setting == "linear":
        return target, circ
    out = workdir / (target.name or "target.fasta")
    with open(out, "w", encoding="utf-8") as fh:
        for header, seq in iter_fasta(target):
            n = len(seq)
            if n and circular_for(setting, header, n):
                circ[header] = n
                seq = seq + seq[:min(n, max_len) - 1]
            fh.write(f">{header}\n")
            for i in range(0, len(seq), 80):
                fh.write(seq[i:i + 80] + "\n")
    if not circ:
        out.unlink(missing_ok=True)
        return target, circ
    return out, circ


def _fold_circular(result: PairResult, circ: dict[str, int]) -> None:
    """Map hits and products on an extended record back onto its circle.

    A site or product that starts in the appended copy is the same one found
    at start - n, and is dropped; one that starts before the end and runs
    into the copy crosses the origin, and its end is folded (end < start).
    A product longer than the circle would need a full turn: dropped.
    """
    for seq in result.sequences:
        n = circ.get(seq.header)
        if not n:
            continue
        hits = []
        for h in seq.hits:
            if h.start > n:
                continue
            if h.end > n:
                h.end -= n
            hits.append(h)
        seq.hits = hits
        amps = []
        for a in seq.amplicons:
            if a.start > n or a.size > n:
                continue
            if a.end > n:
                a.end -= n
            amps.append(a)
        seq.amplicons = amps
        seq.total_amplicons = len(amps)
        seq.unique_sizes = len({a.size for a in amps})
        seq.reported_sizes = [(z, ta) for z, ta in seq.reported_sizes if z <= n]


def _run_jar_task(task, prepared: tuple[Path, dict[str, int]], headers: list[str],
                  jar, java, config, heap, keep, repair, processes, reaction) -> PairResult:
    """One JAR job: always in linear mode, circles linearized beforehand."""
    path, circ = prepared
    result = run_one(RunTask(task.pair, path, "jar"), jar, java,
                     replace(config, circular=False), heap, keep, headers, repair,
                     processes, reaction)
    if circ:
        _fold_circular(result, circ)
    result.target_file = str(task.target)
    return result


def _link_or_copy(src: Path, dst: Path) -> None:
    """Give the JAR its own target path without duplicating a large FASTA."""
    try:
        os.link(src, dst)
        return
    except (OSError, NotImplementedError):
        pass
    try:
        os.symlink(src, dst)
        return
    except (OSError, NotImplementedError, AttributeError):
        pass
    shutil.copy2(src, dst)


@dataclass
class RunTask:
    pair: PrimerPair
    target: Path
    engine: str = "jar"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "pair"


# The JAR announces failures two ways: an uncaught exception on stderr, or a
# plain 'Failed to ...' line on stdout while still exiting 0. Echoing the raw
# tail of stdout instead would bury the cause under the config it just parsed.
_ERROR_PREFIXES = ("Error", "Failed", "Incorrect", "Warning", "Skipped")


def _diagnose(stderr: str | None, stdout: str | None) -> str:
    for text in (stderr or "", stdout or ""):
        for line in text.splitlines():
            s = line.strip()
            if "Exception" in s or s.startswith(_ERROR_PREFIXES):
                return s
    tail = (stderr or stdout or "").strip()
    return tail[-200:] if tail else "no output"


def run_one(
    task: RunTask,
    jar: Path,
    java: JavaInfo,
    config: RunConfig,
    heap: str | None = None,
    keep_report: Path | None = None,
    headers: list[str] | None = None,
    repair: bool = True,
    processes: _Processes | None = None,
    reaction: Reaction | None = None,
) -> PairResult:
    """Run the JAR for one pair against one FASTA and parse the report."""
    # Preferably on the target's volume, so the hard link succeeds instead of
    # copying a large FASTA. A read-only folder (a network share, a mounted
    # archive) cannot hold it, so fall back to the system temp dir and copy.
    try:
        tmp = Path(tempfile.mkdtemp(prefix=".vpcr_", dir=task.target.parent))
    except OSError:
        tmp = Path(tempfile.mkdtemp(prefix="vpcr_"))
    try:
        stem = _safe(task.pair.name)
        target_link = tmp / f"{stem}{task.target.suffix or '.fasta'}"
        _link_or_copy(task.target, target_link)

        primer_file = tmp / f"{stem}.primers.txt"
        write_pair_fasta(task.pair, primer_file)

        out_file = tmp / f"{stem}.out"
        cfg_file = tmp / f"{stem}.config"
        cfg_file.write_text(
            config.to_config_text(str(target_link), str(primer_file), str(out_file)),
            encoding="utf-8",
        )

        cmd = [java.executable, "-jar"]
        if heap:
            cmd[1:1] = [f"-Xmx{heap}"]
        cmd += [str(jar), str(cfg_file)]
        returncode, stdout, stderr = (processes or _Processes()).run(cmd, tmp)

        # Older builds ignore output_path and write <target>.out next to it.
        report = out_file if out_file.exists() else target_link.with_suffix(
            target_link.suffix + ".out"
        )
        if not report.exists():
            raise JarError(
                f"no report produced for pair {task.pair.name!r} "
                f"(exit {returncode}): {_diagnose(stderr, stdout)}"
            )

        result = parse_report(report, task.pair, target_file=str(task.target),
                              all_headers=headers)
        finalize(result, task.target, repair=repair)
        if reaction is not None:
            score_result(result, task.target, reaction)
        if keep_report:
            keep_report.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(report, keep_report)
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_batch(
    pairs: Iterable[PrimerPair],
    targets: Iterable[str | Path],
    jar: str | Path | None,
    config: RunConfig | None = None,
    *,
    java_path: str | None = None,
    workers: int = 4,
    heap: str | None = None,
    reports_dir: str | Path | None = None,
    repair: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    reaction: Reaction | None = None,
    engine: str = "jar",
) -> BatchResults:
    """Run every pair against every target file, in parallel.

    `progress(done, total, label)` is called from the calling thread. When
    `should_cancel` turns true, queued jobs are dropped and running JAR
    processes are killed; the jobs finished by then are returned. A job that
    fails is recorded in the result's `errors` and does not stop the others.

    With a `reaction`, every predicted amplicon is also scored biologically
    (see scoring.py); without one, the results are the JAR's alone.

    `engine` picks what predicts the products (see ENGINES). The built-in
    engine always scores, with `reaction` or the default one, and needs
    neither Java nor the JAR. With 'both', each pair yields two results,
    told apart by PairResult.engine.

    With `config.circular == 'auto'` each record is searched as a circle or a
    line on its own (topology.py). The built-in engine handles circles
    itself; the JAR is given them linearized (see _linearize_circles).
    """
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, not {engine!r}")
    engine, _ = resolve_engine(engine, jar, java_path)
    engines = ["jar", "thermo"] if engine == "both" else [engine]
    if "thermo" in engines and reaction is None:
        reaction = Reaction()
    java = None
    if "jar" in engines:
        if jar is None:
            raise JarError("no JAR given")
        # Resolved: workers run the JAR with cwd set to a temp dir, so a
        # relative path would be interpreted against the wrong directory.
        jar = Path(jar).resolve()
        if not jar.exists():
            raise JarError(f"JAR not found: {jar}")
        java = select_java(jar, java_path)
    config = config or RunConfig()

    targets = [Path(t).resolve() for t in targets]
    for t in targets:
        if not t.exists():
            raise JarError(f"target file not found: {t}")
    pairs = list(pairs)
    if not pairs:
        raise JarError("no primer pairs given")
    if reaction is not None:
        try:
            import primer3  # noqa: F401
        except ImportError as e:
            raise JarError("scoring needs the primer3-py package: "
                           "pip install primer3-py") from e

    header_cache = {t: fasta_headers(t) for t in targets} if "jar" in engines else {}
    # What the JAR reads per target: the file itself, or a copy with its
    # circular records linearized, written once for every pair.
    split_dir = None
    prepared: dict[Path, tuple[Path, dict[str, int]]] = {}
    if "jar" in engines:
        for t in targets:
            if config.circular is not False and config.circular != "linear" \
                    and split_dir is None:
                split_dir = Path(tempfile.mkdtemp(prefix="vpcr_circles_"))
            folder = None
            if split_dir is not None:
                folder = split_dir / str(len(prepared))
                folder.mkdir()
            prepared[t] = _linearize_circles(t, config.circular, config.max_len, folder) \
                if folder is not None else (t, {})
    # Engines adjacent per pair, so a pair's two results sit side by side.
    tasks = [RunTask(p, t, e) for t in targets for p in pairs for e in engines]
    total = len(tasks)
    reports_dir = Path(reports_dir) if reports_dir else None
    # One reports subfolder per target. Two targets can share a stem when they
    # come from different folders; suffix the later ones so neither overwrites.
    report_folder: dict[Path, str] = {}
    for t in targets:
        name, k = t.stem, 2
        while name in report_folder.values():
            name, k = f"{t.stem}_{k}", k + 1
        report_folder[t] = name

    processes = _Processes()
    errors: list[tuple[str, str, str]] = []
    cancelled = False
    # Keyed by task index so results can be restored to submission order:
    # completion order depends on thread scheduling, and callers derive folder
    # names and row order from this list.
    collected: dict[int, PairResult] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {}
        for i, t in enumerate(tasks):
            if t.engine == "thermo":
                fut = pool.submit(run_thermo, t.pair, t.target, config, reaction,
                                  lambda: processes.cancelled)
            else:
                keep = (reports_dir / report_folder[t.target] / f"{_safe(t.pair.name)}.out"
                        if reports_dir else None)
                fut = pool.submit(
                    _run_jar_task, t, prepared[t.target], header_cache[t.target], jar,
                    java, config, heap, keep, repair, processes, reaction)
            futures[fut] = (i, t)

        # Polled rather than blocked on as_completed: a single JAR run can take
        # minutes, and a cancel has to act while it is still going.
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=_CANCEL_POLL_S,
                                     return_when=FIRST_COMPLETED)
            if not cancelled and should_cancel and should_cancel():
                cancelled = True
                processes.cancel()
                for fut in pending:
                    fut.cancel()
                if progress:
                    progress(done, total, "Cancelling: stopping running jobs...")
            for fut in finished:
                i, t = futures[fut]
                if fut.cancelled():
                    continue
                try:
                    collected[i] = fut.result()
                except Cancelled:
                    continue
                except Exception as e:  # one bad pair must not kill the batch
                    done += 1
                    where = f"{t.target.name} [{t.engine}]" if len(engines) > 1 else t.target.name
                    errors.append((t.pair.name, where, str(e)))
                    if progress:
                        progress(done, total, f"ERROR {t.pair.name} / {where}: {e}")
                    continue
                done += 1
                if progress:
                    tag = f" [{t.engine}]" if len(engines) > 1 or t.engine != "jar" else ""
                    progress(done, total, f"{t.pair.name} / {t.target.name}{tag}")
    if split_dir is not None:
        shutil.rmtree(split_dir, ignore_errors=True)
    return BatchResults((collected[i] for i in sorted(collected)), total=total,
                        errors=errors, cancelled=cancelled)
