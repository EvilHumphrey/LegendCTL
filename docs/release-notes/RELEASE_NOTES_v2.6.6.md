# LegendCTL v2.6.6

This security and privacy maintenance release hardens local file handling,
support exports, controller-import filtering, and the release toolchain.

## Security and privacy

- Crash-report discovery now accepts only the expected report names, rejects
  redirected or non-regular files and directories, bounds preview reads, and
  applies the share-safe path and device-identifier sanitizer to final reports.
- Diagnostic bundles exclude service-note text by default. They retain counts
  without placing private note prose in Markdown, JSON, ZIP members, filenames,
  or previews.
- Wear-ledger failure telemetry no longer echoes operator-authored event types
  or summaries into the support log.
- Safe Import applies one field-level classification to preview, filtering,
  saving, and applying. Trigger mode cannot pass through when its Layout
  category is excluded, and malformed or hostile local inputs fail closed.
- Development-environment setup no longer recursively deletes an existing or
  failed environment. It retains the path for explicit inspection and cleanup.
- Download guidance now distinguishes same-channel SHA-256 mismatch detection
  from authenticated publisher or build-origin verification.
- The release lock moves `filelock`, `setuptools`, and `msgpack` to their
  advisory-fixed releases and records their exact hashes.

## Controller-write boundary

- Standalone hidden-interface configuration writes are disabled. The app keeps
  read-only diagnostics and the normal public settings path, but it will not
  send hidden-interface configuration packets without a trustworthy binding
  between that interface and the controller selected by the operator.
- Disabled operations report that no write was attempted; they do not claim a
  successful or verified controller change.

## Release verification

- The `v2.6.5` tag was not published: its raw-source inventory gate stopped
  before any draft release was created when current PyInstaller emitted a
  bytecode-cache directory. Version 2.6.6 is the first distributable version
  of this maintenance set.
- The isolated release worker now explicitly disables Python bytecode-cache
  writes for its entire process tree, preserving the raw-source inventory
  boundary with current PyInstaller releases.
- The tagged build is produced by the public release workflow. That workflow
  requires the full Windows test suite, dependency audit, installer-policy and
  disposable-runner lifecycle gates before it creates an unpublished draft.
  The installer and portable ZIP are published with SHA-256 checksums and
  GitHub build-provenance attestations.

LegendCTL remains a standalone, unofficial configurator for the ZD Ultimate
Legend: local, no telemetry, no bundled drivers, and explicit about what it can
and cannot verify.
