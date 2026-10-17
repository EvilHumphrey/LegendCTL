# LegendCTL v2.6.4

This release fixes controller setting readback and profile saves, makes step-size
edits explicit, keeps the footer status current after a restore, and strengthens
restore consent, imports and release packaging. A v2.6.3 build was prepared but
never published; all of its changes are included here.

## Controller state and write results

- Restore Points replaces the previous manual-setting footer message when a
  restore starts and reports the actual outcome afterward. Refreshing or
  navigating away no longer brings back the earlier message.
- Controller reconnects replace the loading screen with freshly read controls
  and the supported sensitivity-curve editor. Disconnected settings are hidden,
  and unread deadzones cannot be applied.
- Manual deadzone, step-size, and polling-rate changes update the values used
  by Controller and saved profiles after read-back. Saving waits for pending
  writes and confirmation, so an older cached value cannot silently replace a
  confirmed change. Failed or unavailable reads remain explicit.
- Step size is now a numeric draft with an Apply Step Size button. Typing a
  partial number or using the plus/minus buttons does not write to the
  controller. Saving an unapplied draft asks you to apply it first.
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

- Installer builds verify the reviewed Inno Setup installer and compiler inputs.
  The application installer enforces its managed Program Files destination and
  refuses redirected locations or conflicting prior registrations. It relies on
  normal protected Program Files permissions; it does not repair weakened ACLs.
- Python release dependencies use a transitive version-and-hash lock. The build
  environment installs from a verified wheelhouse offline and audits the same
  environment used for packaging. Build/audit dependencies and pinned CI actions
  were refreshed, and a weekly dependency audit supplements pull-request checks.
- Local builds export immutable committed Git source, stamp the full commit ID,
  and record source and artifact hashes. Existing output is preserved, and
  packaging no longer refreshes local installs or desktop shortcuts. The local
  completion receipt is unsigned; it is distinct from hosted build attestations.
  See [build provenance](https://github.com/EvilHumphrey/LegendCTL/blob/v2.6.4/docs/BUILD_PROVENANCE.md).
- Gated post-publication Winget automation checks installer hashes and build
  provenance before submitting a catalog update. This workflow does not publish
  a LegendCTL release, and its presence does not establish that submission is
  enabled or that the catalog already carries this version.

## Release verification

The tagged Windows build passed its full automated suite (3,588 tests; 37 skipped),
21 installer policy cases and nine installer lifecycle cases. The installer and
portable ZIP were reconciled against the tagged build, and both GitHub artifact
attestations were verified against the release workflow and commit.

The installed v2.6.4 build was checked with a USB-connected ZD Ultimate Legend:
a one-step step-size change was applied and read back, then Restore Points
returned the controller to a freshly captured baseline, with all 12 readable
restore groups matched on read-back. The footer showed the restore in progress,
then its result, and kept showing that result after the banner expired and after
changing screens.

The broader controller checks ran on the installed v2.6.3 build. On top of it,
v2.6.4 changes only Restore Points: how it reports progress and outcome in the
footer and status bar, and how it discards a restore and stale cached settings
if the controller changes during it. No controller read/write, protocol or
settings-service code changed. The v2.6.3 checks covered deadzone, step-size and
polling-rate profile saves without a manual Read, plus profile-only Apply, full
Apply, Cancel and Restore Points. In both sessions, a fresh snapshot taken after
the final restore matched the original settings, and the tester then confirmed
normal inputs. These checks do not measure the actual USB polling frequency or
verify unreadable paddle bindings.

LegendCTL is a standalone, unofficial configurator for the ZD Ultimate Legend — no official ZD app
required, local, no telemetry, no drivers, and honest about what it can and can't verify.
