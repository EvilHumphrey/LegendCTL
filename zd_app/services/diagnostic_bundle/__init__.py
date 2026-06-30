"""Diagnostic Bundle — shareable aggregated evidence export.

The bundle is a single Markdown report (plus optional ZIP with raw JSON)
that aggregates already-existing wrapper records into one shareable
artifact: module-passport data (current + archived), recent Health Report
exports, and a privacy-conservative wear-ledger summary.

Operator use case: when a module shows real wear and the
operator wants to share concrete evidence with ZD as something other than
handwavy "it feels off." All data is local; the service writes a file to
disk and the operator chooses to share manually.

Trust posture mirrors the rest of the wrapper:

- **Local only.** No network calls. No upload. No telemetry.
- **Path-safe.** Absolute filesystem paths are rewritten with
  ``<APP_DATA>`` placeholders before the Markdown is written.
- **Privacy-conservative.** The wear-ledger appears in summarized form
  (counts + recent service notes) — never the raw event timing log.
- **Best-effort.** Missing data sources produce graceful placeholders;
  bundle generation never crashes.
"""

from __future__ import annotations

from zd_app.services.diagnostic_bundle.boundary import (
    CLAIM_BOUNDARY_PARAGRAPH,
    CLAIM_BOUNDARY_SHORT,
)
from zd_app.services.diagnostic_bundle.service import (
    APP_DATA_PLACEHOLDER,
    BUNDLE_DIRNAME,
    DiagnosticBundlePreviewItem,
    DiagnosticBundlePreviewManifest,
    DiagnosticBundleService,
)


__all__ = [
    "APP_DATA_PLACEHOLDER",
    "BUNDLE_DIRNAME",
    "CLAIM_BOUNDARY_PARAGRAPH",
    "CLAIM_BOUNDARY_SHORT",
    "DiagnosticBundlePreviewItem",
    "DiagnosticBundlePreviewManifest",
    "DiagnosticBundleService",
]
