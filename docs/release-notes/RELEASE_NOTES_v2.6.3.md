# LegendCTL v2.6.3 — release candidate

This candidate collects controller-state, restore-consent, import, diagnostics,
and build-provenance fixes since v2.6.2. Final installer lifecycle, hardware smoke,
and release-artifact qualification remain pending. These notes do not announce a
published release.

## Controller state and write results

- Asynchronous operations retain the identity of the controller they target,
  including across disconnect/reconnect boundaries. Pending-consent and busy
  guards prevent competing writes, and Last Applied records remain tied to the
  intended device.
- Unconfirmed 8 kHz writes are reported honestly. A successful send alone is not
  proof that the controller accepted or retained a setting.
- Restore approval binds to the snapshot content shown for review. Write-only
  paddle changes identify the exact slots and targets and require explicit
  consent. Attempt summaries are saved separately without rewriting the stored
  snapshot settings.
- Restore and profile verification require successful writer receipts as well as
  matching readable values. A missing receipt cannot count as a verified write,
  even if a subsequent read matches a pre-existing value.
- Settings reads reject malformed percentages, inversion flags, trigger ranges
  and modes, and incomplete replies. Unread categories remain unverified.

Readable changes are checked when possible; write-only paddle bindings remain
reported as sent, never read-back verified. Live Verify still shows XInput output
and does not identify which physical control produced it.

## Import and recovery fixes

- Safe Import hardens local file handling and malformed profile validation, and
  limits classification work before opening the preview. Oversized or overly
  complex inputs are rejected instead of allowing unbounded preview work.
- Malformed Unicode field names are safely escaped for display. New profile
  files are published atomically to avoid leaving partially written profiles.
- The Safe Import preview returns after the file picker completes or is
  cancelled.
- Restore recovery and snapshot handoff handle incomplete or malformed stored
  data more carefully, preserving the distinction between saved state and newly
  read controller values.

## Diagnostics and documentation

- Diagnostic exports sanitize nested JSON, text, clipboard content, and archive
  names. Unsafe or colliding archive entries are rejected before output is opened.
- Documentation and trust wording in English, Simplified Chinese, and Korean
  better describe the existing read-back scope, profile behavior, and official-app
  browser handoffs. Korean was introduced in v2.6.2; it is not new in this release.

## Build and maintenance changes

- Python release dependencies use a transitive version-and-hash lock. The build
  environment installs from a verified wheelhouse offline and audits the same
  environment used for packaging. Build/audit dependencies and pinned CI actions
  were refreshed, and a weekly dependency audit supplements pull-request checks.
- Local builds export immutable committed Git source, stamp the full commit ID,
  and record source and artifact hashes. Existing output is preserved, and
  packaging no longer refreshes local installs or desktop shortcuts. The local
  completion receipt is unsigned; it is distinct from hosted build attestations.
  See [build provenance](../BUILD_PROVENANCE.md).
- Gated post-publication Winget automation checks installer hashes and build
  provenance before submitting a catalog update. This workflow does not publish
  a LegendCTL release, and its presence does not establish that submission is
  enabled or that the catalog already carries this candidate.

The release process still requires qualification of the exact installer and
portable ZIP that would be published, including verification of both artifact
attestations. Earlier source checks or rehearsal builds do not establish those
results for this candidate.
