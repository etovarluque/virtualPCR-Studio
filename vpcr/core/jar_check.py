"""Is this virtualPCR.jar one the app reads correctly? Fingerprint and self-test.

The JAR carries no version: its manifest names only the Java that built it.
A build is identified by its SHA-256, and judged by what it does. A new build
can change the report format, or the search itself, without any error, and
the tables would then be wrong in silence. So, before a build is first used on
a machine, it runs a self-test: templates whose products are known by
construction, searched with the app's own settings, compared base for base.

The cases cover what the parser and runner rely on: products on either strand,
two products in one record, sites at the very ends of a sequence, a record
with no site, products across the origin of a circle (including a site cut by
it and one at base 1), a circle shorter than the longest product, and a
degenerate primer. The result is kept per build, so the test runs once.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .model import PrimerPair, RunConfig
from .scoring import revcomp

# Builds the app was written and checked against: SHA-256 -> description.
KNOWN_BUILDS: dict[str, str] = {
    "44a010c019827974270c77e4e7bf92ddba0784e4db7dd42b143ce315c06e0d73":
        "virtualPCR, build of 2026-07-08 (Java 25)",
}


def jar_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def describe(sha: str) -> str:
    return KNOWN_BUILDS.get(sha, f"unrecognised build {sha[:12]}…")


# ---------------------------------------------------------------------------
# results kept per machine
# ---------------------------------------------------------------------------

def cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or str(Path.home() / ".cache")
    return Path(base) / "vpcr" / "verified_jars.json"


def _load() -> dict:
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cached(sha: str) -> dict | None:
    """The stored verdict for a build: {'ok', 'failures', 'date'}, or None."""
    return _load().get(sha)


def remember(sha: str, result: "SelfTest") -> None:
    data = _load()
    data[sha] = {"ok": result.ok, "failures": result.failures,
                 "date": datetime.now().isoformat(timespec="seconds")}
    try:
        cache_path().parent.mkdir(parents=True, exist_ok=True)
        cache_path().write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass        # unwritable profile: the test simply runs again next time


# ---------------------------------------------------------------------------
# the self-test
# ---------------------------------------------------------------------------

@dataclass
class SelfTest:
    ok: bool
    failures: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        if self.ok:
            return "passed"
        more = f" (+{len(self.failures) - 3} more)" if len(self.failures) > 3 else ""
        return "; ".join(self.failures[:3]) + more


F = "GTCGGTAAAACTCGTGCCAGC"            # MiFish-U-F
R = "CATAGTGGGGTATCTAATCCCAGTTTG"      # MiFish-U-R
# A degenerate forward primer (Y at 5, R at 12), like the lab's L15411F.
F_DEG = "CTCCYAGTCTTRGCAGCCATC"


def _cases() -> tuple[list[tuple[str, str]], dict[tuple[str, str], list[tuple[int, int]]]]:
    """Records, and the products each pair must give on each: (start, end),
    1-based, end < start across the origin. Sizes and sequences follow."""
    rng = random.Random(11)
    bg = lambda n: "".join(rng.choice("ACGT") for _ in range(n))  # noqa: E731
    rcR = revcomp(R)
    L = len(F) + 120 + len(R)
    recs: list[tuple[str, str]] = []
    want: dict[tuple[str, str], list[tuple[int, int]]] = {}

    def add(h, seq, prods, pair="std"):
        recs.append((h, seq))
        want[(pair, h)] = prods

    add("lin_fwd", bg(150) + F + bg(120) + rcR + bg(100), [(151, 150 + L)])
    s = bg(150) + F + bg(120) + rcR + bg(100)
    add("lin_rev", revcomp(s), [(101, 100 + L)])
    add("two_products", bg(80) + F + bg(100) + rcR + bg(150) + rcR + bg(60),
        [(81, 80 + 148), (81, 80 + 148 + 150 + len(R))])
    add("edge_both_ends", F + bg(120) + rcR, [(1, L)])
    add("none", bg(600), [])
    head = bg(40) + rcR + bg(300)
    body = bg(300) + F + bg(50)
    add("wrap_inside complete genome", head + body,
        [(len(head) + 301, 40 + len(rcR))])
    cut = 10
    w2 = F[cut:] + bg(200) + rcR + bg(300) + F[:cut]
    add("wrap_cuts_primer complete genome", w2,
        [(len(w2) - cut + 1, len(F) - cut + 200 + len(rcR))])
    w3 = rcR + bg(700) + F + bg(60)
    add("reverse_at_base1 complete genome", w3, [(len(rcR) + 701, len(rcR))])
    add("short_circle complete genome", bg(30) + F + bg(80) + rcR + bg(40),
        [(31, 30 + len(F) + 80 + len(R))])
    # the degenerate primer, on a template carrying one of its variants
    variant = F_DEG.replace("Y", "T").replace("R", "G")
    add("degenerate", bg(90) + variant + bg(150) + rcR + bg(70),
        [(91, 90 + len(variant) + 150 + len(R))], pair="deg")
    return recs, want


def _product(template: str, start: int, end: int) -> str:
    if end >= start:
        return template[start - 1:end]
    return template[start - 1:] + template[:end]


def self_test(jar: str | Path, java_path: str | None = None) -> SelfTest:
    """Run `jar` on the known cases; ok only if every product is exact."""
    from .runner import run_batch      # runner imports this module's users

    t0 = time.time()
    recs, want = _cases()
    templates = dict(recs)
    pairs = {"std": PrimerPair("selftest", F, R), "deg": PrimerPair("selftest-deg", F_DEG, R)}
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="vpcr_selftest_") as tmp:
        fa = Path(tmp) / "selftest.fasta"
        fa.write_text("".join(f">{h}\n{s}\n" for h, s in recs), encoding="utf-8")
        cfg = RunConfig(min_len=40, max_len=2200, circular="auto", sequence_extract=True)
        try:
            res = run_batch(list(pairs.values()), [fa], jar, cfg, java_path=java_path,
                            workers=2, engine="jar")
        except Exception as e:
            return SelfTest(False, [f"the JAR did not run: {e}"], time.time() - t0)
        for pair, target, msg in res.errors:
            failures.append(f"{pair}: {msg}")
        by_pair = {r.pair.name: {s.header: s for s in r.sequences} for r in res}
        for (key, header), prods in want.items():
            got = by_pair.get(pairs[key].name, {}).get(header)
            amps = sorted(got.amplicons, key=lambda a: a.size) if got else []
            exp = sorted(((st, en, _product(templates[header], st, en)) for st, en in prods),
                         key=lambda x: len(x[2]))
            if len(amps) != len(exp):
                found = ", ".join(f"{a.size} bp" for a in amps) or "none"
                failures.append(f"{header}: {len(amps)} product(s), expected {len(exp)} "
                                f"(found: {found})")
                continue
            for a, (st, en, seq) in zip(amps, exp):
                if (a.start, a.end, a.size) != (st, en, len(seq)):
                    failures.append(f"{header}: product {a.start}-{a.end} ({a.size} bp), "
                                    f"expected {st}-{en} ({len(seq)} bp)")
                elif a.sequence.upper() != seq:
                    failures.append(f"{header}: product sequence differs from the template")
        # a site cut by the origin must read as one perfect hit
        cutrec = by_pair.get("selftest", {}).get("wrap_cuts_primer complete genome")
        fw = [h for h in cutrec.hits if h.orientation == "forward"] if cutrec else []
        if not (len(fw) == 1 and fw[0].identity == 100.0):
            failures.append("wrap_cuts_primer: the site across the origin is not one "
                            f"100% hit ({[(h.start, h.end, h.identity) for h in fw]})")
    return SelfTest(not failures, failures, time.time() - t0)


def verify(jar: str | Path, java_path: str | None = None,
           force: bool = False) -> tuple[SelfTest, bool]:
    """The build's verdict, from the cache or by running the self-test.
    Returns (result, ran) — ran is True when the test was run just now."""
    sha = jar_sha256(jar)
    hit = None if force else cached(sha)
    if hit is not None:
        return SelfTest(hit["ok"], hit.get("failures", [])), False
    result = self_test(jar, java_path)
    # A JAR that could not start says nothing about the build: not remembered.
    if result.ok or not any(f.startswith("the JAR did not run") for f in result.failures):
        remember(sha, result)
    return result, True
