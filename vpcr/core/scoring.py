"""Biological scoring of the amplicons the JAR predicts.

The JAR decides "amplified or not" from mismatch counts. This module asks a
graded question of every predicted product: how much later than a perfectly
matched assay would it show up? It does not search for sites; it scores the
sites the JAR found, so the two layers stay separable and comparable.

Per primer binding site:

  * **Binding** (thermodynamics). The nearest-neighbour duplex free energy of
    primer and template at the annealing temperature, mismatches included
    (SantaLucia & Hicks 2004 parameters, via primer3's thermodynamic
    alignment). From it, the fraction of template sites occupied by primer at
    equilibrium, primer in excess: bound = 1 / (1 + e^(dG/RT) / C).
  * **Degeneracy.** A primer with degeneracy D is a mix of D oligos; only the
    variant matching this site binds it well, at concentration C / D.
  * **Extension** (polymerase kinetics). A mismatch near the 3' end hinders
    extension beyond what it costs in binding, and how much depends on its
    position and its type (primer base . template base). Purine.purine and
    C.C mismatches at the 3' terminus are the worst for Taq; T-containing
    ones are mild (Kwok et al. 1990, Nucleic Acids Res 18:999; Stadhouders et
    al. 2010, J Mol Diagn 12:109). Polymerases with 3'->5' exonuclease can
    remove a terminal mismatch, so they tolerate these better.

Per amplicon, the two sites combine into an estimated cycle delay,
delta_cq = -log2(efficiency_F) - log2(efficiency_R). Mismatches only matter
in the first cycles: after that the primer itself is part of the template.

The extension weights below are literature-informed starting values, not a
calibrated model. Calibrating them against wet-lab outcomes is the next step;
until then, read the calls as a ranking of risk, not as a probability.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from statistics import median

from .model import Amplicon, PairResult, PrimerHit, PrimerPair, SequenceResult

R_CAL = 1.98720425864083  # gas constant, cal / (mol K)

IUPAC: dict[str, str] = {
    "A": "A", "C": "C", "G": "G", "T": "T", "U": "T",
    "R": "AG", "Y": "CT", "S": "CG", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
    # Inosine pairs with every base: a universal position, not a mixture.
    "I": "ACGT",
}
_UNIVERSAL = {"I"}
_COMP = str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN")


def revcomp(seq: str) -> str:
    return seq.upper().translate(_COMP)[::-1]


# ---------------------------------------------------------------------------
# reaction conditions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Polymerase:
    key: str
    label: str
    # Exponent applied to every 3' extension penalty: 1 = plain Taq,
    # smaller = a terminal mismatch costs less.
    mismatch_tolerance: float
    # Automatic Ta = lower primer Tm + this offset.
    ta_offset: float
    # Buffer: monovalent cations, Mg2+, total dNTP (mM) and primer (nM).
    mv_mM: float
    dv_mM: float
    dntp_mM: float
    primer_nM: float
    note: str
    # Extension rate, kb per minute: what one cycle's extension can copy.
    kb_per_min: float = 1.0


POLYMERASES: dict[str, Polymerase] = {p.key: p for p in (
    Polymerase(
        "onetaq", "OneTaq (NEB)", 0.6, -5.0,
        # OneTaq Standard Reaction Buffer: 22 mM KCl + 22 mM NH4Cl, 1.8 mM
        # MgCl2; 200 uM of each dNTP; 0.2 uM of each primer.
        44.0, 1.8, 0.8, 200.0,
        "Taq + Deep Vent blend: the 3'->5' exonuclease can trim a 3' mismatch, "
        "so terminal mismatches cost less than with Taq alone.",
        # NEB: 1 min/kb for OneTaq.
        1.0),
    Polymerase(
        "taq", "Taq (no proofreading)", 1.0, -5.0,
        50.0, 1.5, 0.8, 200.0,
        "No 3'->5' exonuclease: the most sensitive to 3' mismatches.", 1.0),
    Polymerase(
        "hifi", "High-fidelity (Q5, Phusion)", 0.4, 3.0,
        50.0, 2.0, 0.8, 500.0,
        "Strong 3'->5' exonuclease. NEB anneals these at the lower Tm + 3 C.",
        # NEB: 20-30 s/kb for Q5; the slower end, to stay conservative.
        2.0),
)}
DEFAULT_POLYMERASE = "onetaq"


@dataclass(frozen=True)
class Reaction:
    """What the scoring assumes about the bench protocol."""
    polymerase: str = DEFAULT_POLYMERASE
    # One annealing temperature for every pair, or None to use each pair's own
    # (PrimerPair.ta) and fall back to lower Tm + the polymerase's offset.
    ta: float | None = None
    # Extension time per cycle, in seconds; None leaves length out of the score.
    extension_s: float | None = 60.0

    @property
    def profile(self) -> Polymerase:
        return POLYMERASES[self.polymerase]


def _variants(primer: str, cap: int = 256) -> list[str]:
    """Concrete oligos of a degenerate primer; a fixed sample when there are many."""
    choices = [IUPAC.get(b, "ACGT") for b in primer.upper()]
    total = math.prod(len(c) for c in choices)
    if total <= cap:
        out = [""]
        for c in choices:
            out = [o + b for o in out for b in c]
        return out
    rng = random.Random(primer)   # deterministic per primer
    return ["".join(rng.choice(c) for c in choices) for _ in range(cap)]


def degeneracy(primer: str) -> int:
    return math.prod(1 if b in _UNIVERSAL else len(IUPAC.get(b, "ACGT"))
                     for b in primer.upper())


@lru_cache(maxsize=4096)
def primer_tm(primer: str, polymerase: str = DEFAULT_POLYMERASE) -> float:
    """Melting temperature of a primer against its perfect complement, in C.

    For a degenerate primer, the median over its variants: the mix melts as a
    spread, and the median is what a Tm calculator's single number stands for.
    """
    import primer3
    p = POLYMERASES[polymerase]
    tms = [primer3.calc_tm(v, mv_conc=p.mv_mM, dv_conc=p.dv_mM,
                           dntp_conc=p.dntp_mM, dna_conc=p.primer_nM)
           for v in _variants(primer)]
    return float(median(tms))


def resolve_ta(pair: PrimerPair, reaction: Reaction) -> tuple[float, str]:
    """The annealing temperature to score `pair` at, and where it came from."""
    if pair.ta is not None:
        return float(pair.ta), "pair"
    if reaction.ta is not None:
        return float(reaction.ta), "fixed"
    low = min(primer_tm(pair.forward, reaction.polymerase),
              primer_tm(pair.reverse, reaction.polymerase))
    return round(low + reaction.profile.ta_offset, 1), "auto"


# ---------------------------------------------------------------------------
# one binding site
# ---------------------------------------------------------------------------

# Extension penalty for a mismatch at the 3' terminus, by primer.template
# bases, for Taq. Starting values to be calibrated, chosen so the classes
# cost roughly 6-7, 3 and 1 cycles respectively.
_SEVERE, _MODERATE, _MILD = 0.01, 0.1, 0.5
_TERMINAL_PENALTY: dict[tuple[str, str], float] = {
    ("A", "A"): _SEVERE, ("A", "G"): _SEVERE, ("G", "A"): _SEVERE,
    ("G", "G"): _SEVERE, ("C", "C"): _SEVERE,
    ("A", "C"): _MODERATE, ("C", "A"): _MODERATE, ("C", "T"): _MODERATE,
    ("T", "C"): _MILD, ("T", "G"): _MILD, ("T", "T"): _MILD, ("G", "T"): _MILD,
}
# How much of the terminal penalty applies at each distance from the 3' end
# (as an exponent). Past the fifth base, extension is barely affected and the
# mismatch acts through binding only, which the dG already accounts for.
_POSITION_WEIGHT = (1.0, 0.5, 0.3, 0.15, 0.1)

# Cycle-delay bands for the call. A 5-cycle delay is ~30x less product, still
# a visible band after 35 cycles; past 10 cycles (~1000x) it is unlikely.
CALL_BANDS: tuple[tuple[float, str], ...] = (
    (2.0, "strong"), (5.0, "probable"), (10.0, "weak"), (math.inf, "unlikely"))
CALLS = tuple(c for _, c in CALL_BANDS)
_MAX_DELAY = 40.0   # cap: beyond any real cycling program


@dataclass(frozen=True)
class SiteScore:
    dg: float            # kcal/mol at Ta, the best-matching variant on this site
    bound: float         # fraction of sites occupied at Ta, 0-1
    extension: float     # relative extension efficiency from 3' mismatches, 0-1
    efficiency: float    # bound * extension
    mismatches: str      # "3'+0 A.G; 3'+7 T.C" (primer.template), 3' end first
    n_mismatches: int
    degeneracy: int
    tm: float | None = None  # C, this variant on this site, at the buffer's salt


def _align(primer: str, site: str) -> tuple[str, str, list[tuple[int, str, str]]]:
    """Best-matching primer variant for `site`, and its mismatches.

    `site` is written like the primer: the sequence a perfect primer would
    have. Returns the variant, the site with ambiguous bases resolved, and
    (distance from 3' end, primer base, template base) per mismatch.
    Ambiguous template bases count as a match when the primer can pair with
    any of their readings: an N in a mitogenome is a gap in the sequencing,
    not evidence of a mismatch.
    """
    L = len(primer)
    variant: list[str] = []
    resolved: list[str] = []
    mism: list[tuple[int, str, str]] = []
    for i, (pb, sb) in enumerate(zip(primer.upper(), site.upper())):
        allowed = IUPAC.get(pb, "ACGT")
        readings = IUPAC.get(sb, "ACGT")
        common = [b for b in readings if b in allowed]
        if common:
            variant.append(common[0])
            resolved.append(common[0])
            continue
        use = allowed[0]
        variant.append(use)
        resolved.append(readings[0])
        # The template base faces the primer: the complement of the site base.
        mism.append((L - 1 - i, use, readings[0].translate(_COMP)))
    return "".join(variant), "".join(resolved), mism


def _extension(mism: list[tuple[int, str, str]], tolerance: float) -> float:
    ext = 1.0
    for d, pb, tb in mism:
        if d < len(_POSITION_WEIGHT):
            ext *= _TERMINAL_PENALTY.get((pb, tb), _MODERATE) ** (
                _POSITION_WEIGHT[d] * tolerance)
    return ext


def _duplex(variant: str, template: str, ta: float, polymerase: str) -> tuple[float, float | None]:
    """Duplex dG (kcal/mol) at `ta`, and Tm, of `variant` on `template` (5'->3')."""
    import primer3
    p = POLYMERASES[polymerase]
    r = primer3.calc_heterodimer(variant, template, mv_conc=p.mv_mM, dv_conc=p.dv_mM,
                                 dntp_conc=p.dntp_mM, dna_conc=p.primer_nM, temp_c=ta)
    if not r.structure_found:
        return 0.0, None
    return r.dg / 1000.0, round(r.tm, 1)


@lru_cache(maxsize=200_000)
def score_site(primer: str, site: str, ta: float,
               polymerase: str = DEFAULT_POLYMERASE) -> SiteScore:
    """Score one binding site. `site` is written 5'->3' like the primer.

    Cached: across a set of related genomes the same site recurs thousands of
    times, and the thermodynamics are the expensive part.
    """
    prof = POLYMERASES[polymerase]
    variant, resolved, mism = _align(primer, site)
    D = degeneracy(primer)
    # The primer anneals to the strand complementary to the site.
    dg, tm = _duplex(variant, revcomp(resolved), round(ta, 2), polymerase)
    conc = prof.primer_nM * 1e-9 / D
    x = (dg * 1000.0) / (R_CAL * (ta + 273.15)) - math.log(conc)
    bound = 0.0 if x > 700 else 1.0 / (1.0 + math.exp(x))
    ext = _extension(mism, prof.mismatch_tolerance)
    text = "; ".join(f"3'+{d} {pb}.{tb}" for d, pb, tb in sorted(mism))
    return SiteScore(round(dg, 2), bound, ext, bound * ext, text, len(mism), D, tm)


# Extension goes on through the ramp from the extension to the denaturation
# temperature, so a cycle copies a little more than its hold time alone allows.
_RAMP_S = 10.0
# The cycle at which a well amplifying product crosses the threshold, used to
# turn a per-cycle loss into a delay. A starting value, as the weights above.
_REF_CQ = 25.0


def extension_capacity(reaction: Reaction) -> float | None:
    """Bases the polymerase copies in one cycle's extension; None if not set."""
    if reaction.extension_s is None:
        return None
    return reaction.profile.kb_per_min * 1000.0 * (reaction.extension_s + _RAMP_S) / 60.0


def length_delay(size: int, reaction: Reaction | None) -> float:
    """Cycle delay of a product too long to finish in the extension time.

    Unlike a 3' mismatch, which costs only until the first copies carry the
    primer's own sequence, length costs in every cycle: if a share e of the
    strands is completed per cycle, the product grows (1 + e)-fold instead of
    2-fold, and reaches the threshold at Cq * ln 2 / ln(1 + e) instead of Cq.
    e is the capacity over the length. A product 10% too long costs ~2
    cycles; twice too long, ~18, i.e. unlikely.
    """
    cap = extension_capacity(reaction) if reaction is not None else None
    if cap is None or size <= 0 or size <= cap:
        return 0.0
    e = cap / size
    return min(_MAX_DELAY, _REF_CQ * (math.log(2) / math.log1p(e) - 1.0))


def delta_cq(f: SiteScore, r: SiteScore) -> float:
    eff = f.efficiency * r.efficiency
    return _MAX_DELAY if eff <= 0 else min(_MAX_DELAY, -math.log2(eff))


def call_for(delay: float) -> str:
    return next(c for limit, c in CALL_BANDS if delay <= limit)


# ---------------------------------------------------------------------------
# a whole PairResult
# ---------------------------------------------------------------------------

def _site_from_template(template: str, hit: PrimerHit, primer: str) -> str | None:
    """The 5'->3' site sequence for `primer` at `hit`, or None if it cannot be read.

    A forward hit means the primer reads like the top strand at start..end; a
    reverse hit means its reverse complement does. Coordinates are 1-based and
    inclusive. A span that differs from the primer length (an indel) or that
    runs off the template is not scored.
    """
    L = len(primer)
    if hit.end - hit.start + 1 != L or hit.start < 1 or hit.end > len(template):
        return None
    top = template[hit.start - 1: hit.end].upper()
    return top if hit.orientation == "forward" else revcomp(top)


def _primer_for(hit: PrimerHit, pair: PrimerPair) -> str:
    """The pair's own primer behind a hit. The report's copy may be case- or
    U/T-altered; the pair holds the sequence as ordered."""
    seq = hit.primer_seq.upper().replace("U", "T")
    for cand in (pair.forward, pair.reverse):
        if cand.upper().replace("U", "T") == seq:
            return cand.upper()
    if hit.primer_name and hit.primer_name == pair.rev_name:
        return pair.reverse.upper()
    return pair.forward.upper() if hit.primer_name == pair.fwd_name else seq


def _flanking(seq: SequenceResult, amp: Amplicon) -> tuple[PrimerHit | None, PrimerHit | None]:
    fwd = next((h for h in seq.hits_for("forward") if h.start == amp.start), None)
    rev = next((h for h in seq.hits_for("reverse") if h.end == amp.end), None)
    return fwd, rev


def score_sequence(seq: SequenceResult, template: str, pair: PrimerPair,
                   ta: float, polymerase: str, reaction: Reaction | None = None) -> int:
    """Score every amplicon of one sequence. Returns how many were scored."""
    n = 0
    for amp in seq.amplicons:
        f, r = _flanking(seq, amp)
        if f is None or r is None:
            continue
        scores = []
        for hit in (f, r):
            if hit.score is None:
                primer = _primer_for(hit, pair)
                site = _site_from_template(template, hit, primer)
                if site is not None:
                    hit.score = score_site(primer, site, ta, polymerase)
            scores.append(hit.score)
        if all(scores):
            amp.length_delay = round(length_delay(amp.size, reaction), 2)
            amp.delta_cq = round(min(_MAX_DELAY, delta_cq(*scores) + amp.length_delay), 2)
            amp.call = call_for(amp.delta_cq)
            n += 1
    return n


def score_result(result: PairResult, fasta_path: str | Path, reaction: Reaction) -> int:
    """Score every amplicon in `result` against its template. Returns the count.

    Reads the FASTA once, and only the records that hold an amplicon. A FASTA
    that repeats a header is matched record by record, as in parser.finalize.
    """
    from .parser import _belongs_to, iter_fasta

    ta, source = resolve_ta(result.pair, reaction)
    result.score_ta, result.score_ta_source = ta, source
    result.score_polymerase = reaction.profile.label

    pending: dict[str, list[SequenceResult]] = {}
    for s in result.sequences:
        if s.amplicons:
            pending.setdefault(s.header, []).append(s)
    if not pending:
        return 0
    scored = 0
    for header, template in iter_fasta(fasta_path):
        cands = pending.get(header)
        if not cands:
            continue
        for k, s in enumerate(cands):
            if _belongs_to(s, template):
                scored += score_sequence(s, template, result.pair, ta, reaction.polymerase,
                                         reaction)
                del cands[k]
                break
        if not cands:
            del pending[header]
            if not pending:
                break
    return scored


def best_amplicon(seq: SequenceResult) -> Amplicon | None:
    """The product most likely to show: the one with the smallest delay."""
    scored = [a for a in seq.amplicons if a.delta_cq is not None]
    return min(scored, key=lambda a: a.delta_cq) if scored else None
