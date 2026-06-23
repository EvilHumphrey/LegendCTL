"""Single source of truth for the Diagnostic Bundle claim-boundary text.

The Diagnostic Bundle aggregates records from three wrapper subsystems
(module passport, Health Report, wear ledger). Each of those subsystems
already carries its own claim-boundary paragraph in its own exporter
(see ``health_report/boundary.py`` and ``restore_points/boundary.py``).
The bundle paragraph below is the umbrella boundary specific to the
aggregated view — it tells a reader what the *combined* artifact can and
cannot prove, distinct from the per-subsystem boundaries which the
embedded records carry on their own.

The text is intentionally English-only. The bundle is a shareable
evidence artifact; translating the claim-boundary would dilute its
shareability with ZD's (most likely English-speaking) technical contact
path. The UI modal that triggers bundle generation IS localized — only
the report content stays English.

Following the same pattern (cf. ``health_report/boundary.py`` §"Report
claim-boundary statement"): verbatim, single source of truth, never
paraphrased, never split.
"""

from __future__ import annotations


# Long-form claim-boundary paragraph rendered at the top of every bundle.
# Mirrors the pattern used by Health Report and Restore Points. Do
# not paraphrase, do not split, do not localize.
CLAIM_BOUNDARY_PARAGRAPH = (
    "This Diagnostic Bundle aggregates records this Windows app produced "
    "during local use of the controller: stick-module characterization "
    "fingerprints, exported Health Reports, and a summary of the wrapper's "
    "wear-ledger lifecycle log. Each record reflects only what the app could "
    "observe through Windows HID input reports and the wrapper's own "
    "operations — operator-assigned module IDs, operator-written service "
    "notes, and event counts the wrapper itself emitted. The bundle cannot "
    "prove true USB bus polling rate, firmware correctness, manufacturing "
    "defects, anti-cheat or tournament approval, controller hardware "
    "lineage, or end-to-end input latency. Numeric measurements were "
    "collected on commodity Windows hardware and include OS scheduling, "
    "HID delivery, and app timestamping overhead, so they should be read "
    "as observed app-layer behavior rather than absolute hardware truth."
)


# Short variant for the UI modal footer / confirmation copy.
CLAIM_BOUNDARY_SHORT = (
    "This bundle aggregates local wrapper records (module passports, "
    "Health Reports, wear-ledger summary). It is observational evidence "
    "from this Windows app, not a hardware test or factory-grade audit."
)


__all__ = [
    "CLAIM_BOUNDARY_PARAGRAPH",
    "CLAIM_BOUNDARY_SHORT",
]
