"""Single source of truth for the Health Report claim-boundary paragraph.

The paragraph states only what the report can actually show — stick rest
noise, range coverage, and trigger travel — and the things it cannot prove
(USB polling rate, firmware, anti-cheat/tournament, end-to-end latency). It
deliberately makes no claim about timing or cadence. Every exported Health
Report — Markdown and JSON — must include this text unchanged. The
forbidden-phrase tests assert its presence in every Markdown export.

The shorter quote and footer line are reused in UI copy. Centralizing them
here lets the tests assert exact equality without duplicating the strings.
"""

from __future__ import annotations


# The claim-boundary paragraph. Do not paraphrase, do not split, do not
# localize — this is the single load-bearing honesty boundary for the whole
# feature.
CLAIM_BOUNDARY_PARAGRAPH = (
    "This Controller Health Report is based on HID input reports received by "
    "this Windows app during a guided local test. It can show observed stick "
    "rest noise, approximate range coverage, and trigger travel. It cannot "
    "prove true USB bus polling rate, firmware correctness, "
    "anti-cheat/tournament approval, or end-to-end input latency."
)


# A short summary line reused in UI copy.
SHORT_REVIEWER_QUOTE = (
    "The report measures app-observed HID behavior, not USB-bus truth or "
    "input latency."
)


# The shortest UI footer line.
UI_FOOTER_LINE = "Local HID report snapshot. Not a latency test or USB analyzer."


# The opaque machine-readable identifier for the claim boundary, used as the
# JSON ``claim_boundary`` field.
CLAIM_BOUNDARY_ID = "application_layer_hid_report_snapshot"


# Human-readable summary of what the measurement *is*, used as the JSON
# ``measurement_boundary`` field. Distinct from ``claim_boundary`` so the
# JSON schema can carry both an opaque identifier and a one-line gloss.
# The wording avoids the forbidden-phrase vocabulary (see HR9 / the forbidden-phrase policy);
# the verbatim claim-boundary paragraph carries the denial language.
MEASUREMENT_BOUNDARY_SUMMARY = (
    "Windows application-layer HID input report snapshot. Includes OS "
    "scheduling, HID delivery, and app timestamping overhead. Not a USB bus "
    "analyzer or end-to-end timing measurement."
)
