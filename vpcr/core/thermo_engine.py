"""The built-in engine: an in silico PCR independent of the JAR.

It exists to be compared with the JAR. Where both agree, a prediction rests on
two different methods; where they disagree is where a closer look pays off. It
also runs without Java.

Search, per primer and per strand:

  1. Seeds. A primer is extended by the polymerase from its 3' end, so a site
     worth considering matches well there. Two 7-nt seeds are looked up
     exactly: the 3'-terminal seven bases, and the seven before them. A site
     with a mismatch in one seed is still found through the other; one with
     mismatches in both is not, and would hardly prime anyway.
  2. Verification. Each candidate is compared with the whole primer, IUPAC
     codes on both sides, and kept with up to MAX_SITE_MISMATCHES mismatches.
  3. Scoring. Every site is scored as in scoring.py: duplex dG at the
     annealing temperature, degeneracy, and the 3' extension penalty.
  4. Products. Any site on the top strand facing any site on the bottom strand
     within the size range is a product, the same primer on both ends
     included, as in a real tube. On a circular template products may cross
     the origin. A product is reported when its estimated delay puts it at
     'weak' or better; 'unlikely' ones are dropped, so 'amplified' here means
     "expected to give a product".

The result is a PairResult shaped like the parser's, so tables, export and the
Alignment and Primer map tabs treat both engines alike.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .model import Amplicon, PairResult, PrimerHit, PrimerPair, RunConfig, SequenceResult
from .parser import iter_fasta
from .scoring import (CALL_BANDS, IUPAC, Reaction, call_for, delta_cq, resolve_ta,
                      revcomp, score_site)

SEED_LEN = 7
MAX_SITE_MISMATCHES = 6
# Products past this delay are 'unlikely' and not reported.
MAX_REPORTED_DELAY = next(limit for limit, c in CALL_BANDS if c == "weak")

_IUPAC_SETS = {k: frozenset(v) for k, v in IUPAC.items()}
_ANY = frozenset("ACGT")


def _seed_regex(pattern: str) -> re.Pattern[str]:
    """A lookahead regex for an IUPAC pattern, so overlapping hits are all found."""
    cls = "".join(f"[{IUPAC.get(b, 'ACGT')}]" if len(IUPAC.get(b, "ACGT")) > 1
                  else IUPAC.get(b, "ACGT") for b in pattern.upper())
    return re.compile(f"(?=({cls}))")


class _Probe:
    """One primer in one orientation, as it reads on the top strand."""

    def __init__(self, name: str, primer: str, orientation: str) -> None:
        self.name = name
        self.primer = primer.upper()
        self.orientation = orientation
        # What the top strand carries at a perfect site.
        self.site = self.primer if orientation == "forward" else revcomp(self.primer)
        self.allowed = [_IUPAC_SETS.get(b, _ANY) for b in self.site]
        L = len(self.site)
        k = min(SEED_LEN, max(4, L // 3))
        # The primer's 3' end is the right end of a forward site and the left
        # end of a reverse one.
        if orientation == "forward":
            spans = [(L - k, L), (L - 2 * k, L - k)]
        else:
            spans = [(0, k), (k, 2 * k)]
        self.seeds = [(a, _seed_regex(self.site[a:b])) for a, b in spans if a >= 0]

    def find(self, seq: str) -> dict[int, int]:
        """0-based start -> mismatch count, for every acceptable site in `seq`."""
        L = len(self.site)
        found: dict[int, int] = {}
        for offset, rx in self.seeds:
            for m in rx.finditer(seq):
                start = m.start() - offset
                if start < 0 or start in found or start + L > len(seq):
                    continue
                mm = 0
                for allowed, base in zip(self.allowed, seq[start:start + L]):
                    if base not in allowed and not (_IUPAC_SETS.get(base, _ANY) & allowed):
                        mm += 1
                        if mm > MAX_SITE_MISMATCHES:
                            break
                if mm <= MAX_SITE_MISMATCHES:
                    found[start] = mm
        return found


def _match_line(probe: _Probe, top_site: str) -> str:
    """'|' match, ':' match through a degenerate base, ' ' mismatch, in the
    order virtualPCR prints the primer (a reverse primer 3'->5')."""
    out = []
    for allowed, base in zip(probe.allowed, top_site):
        if base in allowed or (_IUPAC_SETS.get(base, _ANY) & allowed):
            out.append("|" if len(allowed) == 1 else ":")
        else:
            out.append(" ")
    # The top-strand site reads 5'->3' for a forward primer, and 3'->5' of
    # the primer for a reverse one: exactly the printed order either way.
    return "".join(out)


_COMPLEMENT = str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN")


def _context(top: str, start: int, L: int, orientation: str) -> str:
    """The template strand under the primer as the Alignment tab draws it: two
    bases of lead, the site, two after; complemented for a forward primer,
    whose template is the bottom strand."""
    a, b = max(0, start - 2), start + L + 2
    lead = "N" * (2 - (start - a))
    ctx = lead + top[a:b]
    return ctx.translate(_COMPLEMENT) if orientation == "forward" else ctx


def scan_sequence(header: str, seq: str, pair: PrimerPair, probes: list[_Probe],
                  config: RunConfig, ta: float, polymerase: str) -> SequenceResult | None:
    """Sites and products of one pair on one template; None if no site at all."""
    seq = seq.upper()
    n = len(seq)
    longest = max(len(p.site) for p in probes)
    # A circle is searched with its start appended, so sites across the
    # origin are found; positions are folded back afterwards.
    view = seq + seq[: longest - 1] if config.circular and n else seq

    hits: list[PrimerHit] = []
    for probe in probes:
        L = len(probe.site)
        primer = probe.primer
        for start, mm in sorted(probe.find(view).items()):
            if start >= n:              # the appended copy: already found at start - n
                continue
            top_site = view[start:start + L]
            site = top_site if probe.orientation == "forward" else revcomp(top_site)
            sc = score_site(primer, site, ta, polymerase)
            end = start + L                         # 1-based inclusive end
            hits.append(PrimerHit(
                primer_name=probe.name, primer_seq=primer,
                start=start + 1, end=end if end <= n else end - n,
                orientation=probe.orientation,
                identity=round(100.0 * (L - mm) / L, 1),
                tm=sc.tm if sc.tm is not None else 0.0,
                match_line=_match_line(probe, top_site),
                target_context=_context(view, start, L, probe.orientation),
                score=sc))
    if not hits:
        return None

    res = SequenceResult(header=header, hits=hits)
    lefts = [h for h in hits if h.orientation == "forward"]
    rights = [h for h in hits if h.orientation == "reverse"]
    for f in lefts:
        for r in rights:
            if r.end >= f.start:
                size = r.end - f.start + 1
            elif config.circular:
                size = n - f.start + 1 + r.end          # across the origin
            else:
                continue
            if not (config.min_len <= size <= config.max_len):
                continue
            delay = round(delta_cq(f.score, r.score), 2)
            if delay > MAX_REPORTED_DELAY:
                continue
            seqtext = ""
            if config.sequence_extract:
                seqtext = (seq[f.start - 1:r.end] if r.end >= f.start
                           else seq[f.start - 1:] + seq[:r.end]).lower()
            res.amplicons.append(Amplicon(start=f.start, end=r.end, size=size,
                                          sequence=seqtext, delta_cq=delay,
                                          call=call_for(delay)))
    res.amplicons.sort(key=lambda a: (a.start, a.end))
    res.total_amplicons = len(res.amplicons)
    res.unique_sizes = len({a.size for a in res.amplicons})
    return res


def run_thermo(pair: PrimerPair, target: Path, config: RunConfig, reaction: Reaction,
               should_stop=None) -> PairResult:
    """Run one pair over one FASTA with the built-in engine."""
    t0 = time.time()
    ta, source = resolve_ta(pair, reaction)
    probes = [_Probe(pair.fwd_name, pair.forward, "forward"),
              _Probe(pair.fwd_name, pair.forward, "reverse"),
              _Probe(pair.rev_name, pair.reverse, "forward"),
              _Probe(pair.rev_name, pair.reverse, "reverse")]

    sequences: list[SequenceResult] = []
    silent: list[str] = []
    for i, (header, seq) in enumerate(iter_fasta(target)):
        if should_stop is not None and i % 200 == 0 and should_stop():
            from .runner import Cancelled
            raise Cancelled("cancelled")
        r = scan_sequence(header, seq, pair, probes, config, ta, reaction.polymerase)
        if r is None:
            silent.append(header)
        else:
            sequences.append(r)
    return PairResult(
        pair=pair, target_file=str(target), sequences=sequences,
        elapsed=f"{time.time() - t0:.0f} seconds", silent_headers=silent,
        score_ta=ta, score_ta_source=source,
        score_polymerase=reaction.profile.label, engine="thermo")
