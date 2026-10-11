"""Is a template a circle? Decided per sequence for the 'auto' topology.

A circular search joins a sequence's end to its start, so a product can cross
the origin. That is right for a complete mitogenome, plastome or bacterial
genome, and wrong for a fragment: a partial gene from GenBank or BOLD searched
as a circle can yield a product made of its last and first bases, which exists
in no tube. A run often holds both kinds, so one switch for the whole run is
wrong for some of them; 'auto' decides record by record:

* a header that says *partial* -> linear;
* a numbered eukaryotic chromosome ("chromosome 1, complete sequence") ->
  linear, complete or not;
* a header that says *complete genome / sequence*, *mitogenome*, *plastome* ->
  circular;
* otherwise, a length typical of an animal mitogenome (14-20 kb) -> circular,
  so complete mitogenomes under custom headers ("MT100001|Species_1") are
  still searched as circles;
* anything else -> linear.
"""
from __future__ import annotations

import re

MITO_RANGE = (14_000, 20_000)

_PARTIAL = re.compile(r"\bpartial\b", re.I)
_EUK_CHROMOSOME = re.compile(r"\bchromosome\s+(\d+[A-Za-z]?|[IVX]+|[XYZW])\b", re.I)
_COMPLETE = re.compile(
    r"\bcomplete\s+(genome|sequence|mitochondrial\s+genome|mitogenome|plastome)\b"
    r"|\bmitogenome\b|\bplastome\b", re.I)

TOPOLOGIES = ("auto", "circular", "linear")


def is_circular(header: str, length: int) -> bool:
    """Whether a record should be searched as a circle (see the module notes)."""
    if _PARTIAL.search(header) or _EUK_CHROMOSOME.search(header):
        return False
    if _COMPLETE.search(header):
        return True
    return MITO_RANGE[0] <= length <= MITO_RANGE[1]


def circular_for(setting: bool | str, header: str, length: int) -> bool:
    """A run's topology setting (True, False or 'auto') applied to one record."""
    if setting == "auto":
        return is_circular(header, length)
    if setting in ("circular", "linear"):
        return setting == "circular"
    return bool(setting)
