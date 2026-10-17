"""Machine-enforced enumeration of every modal-construction site in ``zd_app``.

The problem this gate solves
----------------------------
Dear PyGui has a benched law (2026-06-11; matrix in
``tools/diag_dpg_modal_thread_visibility.py``; encoded by
``AppShell._defer_modal_swap``): a modal created in the same render pass in
which another modal was showing -- deleted, hidden, or simply left up -- exists
with all its children but never becomes interactive. Nothing raises, nothing is
logged, and a mocked test cannot see it: the symptom lives in the native
renderer, so only a real-render regression test can prove a single fix.

What no test could do until now is prove the *list* was complete. Sites with
this shape were found and fixed in several separate rounds, and every round
believed the class was closed. The misses were never in the obvious place:

* a ``dpg.popup(`` with no ``modal=`` kwarg on the call at all;
* a construction whose ``modal`` and ``tag`` arrive inside a ``**kwargs`` dict;
* a defect visible only by tracing *callers* (a modal left up by the caller),
  not by inspecting constructors.

So this module converts "did we find them all?" from a human sweep into a gate.
``tests/modal_site_walker.py`` discovers every window-family construction under
``zd_app/`` -- by constructor family, not by one keyword -- and reports anything
it cannot decide statically instead of skipping it. :data:`MODAL_SITE_REGISTRY`
below then has to account for every discovered site, with a human-written
reason for anything that is not on the seam.

Where the registry lives, and why here
--------------------------------------
The registry is a review ledger, not runtime behaviour: bucket labels, reason
strings, and the caller topology a reviewer needs. Nothing in the shipped app
would import it, and adding an unimported module under ``zd_app/`` would ship
dead code to every user to serve a test. It therefore lives in ``tests/``.

The one thing that argues the other way is
``app_shell.CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS`` -- a hand-enumerated
tuple of modal tags that *is* production code and that has drifted in both
directions (see :class:`SweepListDriftTests`). The right end state is for that
tuple to be derived from a production-side registry rather than typed out. That
change belongs in ``app_shell.py``; until it happens, this gate pins the tuple
against this registry so it cannot drift further unnoticed.

Buckets
-------
``SEAM``         the owning function routes its own create through the seam.
``CALLER_SEAM``  the owner is generic; named upstream functions do the seaming.
``DEFECT``       confirmed same-pass shape, tracked, not yet cured.
``EXEMPT``       structurally immune or unreachable; reason required.
``PROBE``        classification needs a live run; reason names the probe.
``NON_MODAL``    proven non-modal, registered so that flipping it is loud.

Every bucket except ``SEAM`` requires a non-empty ``reason``. Every entry is
keyed on ``file :: owning function :: tag`` -- never a line number, because
these files move by tens of lines inside a single working session.
"""

from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass

from tests.modal_site_walker import (
    PACKAGE_ROOT,
    REPO_ROOT,
    Modality,
    TagSource,
    discover,
    function_uses_seam,
    iter_source_files,
    read_string_tuple,
)


APP_SHELL = "zd_app/ui/app_shell.py"
ABOUT = "zd_app/ui/screens/about.py"
BUNDLE_PREVIEW = "zd_app/ui/diagnostic_bundle_preview.py"
MODULES = "zd_app/ui/screens/modules.py"
RESTORE_POINTS = "zd_app/ui/screens/restore_points.py"
SAFE_IMPORT = "zd_app/ui/screens/safe_import.py"
TRUST_RITUAL = "zd_app/ui/widgets/trust_ritual.py"
WEAR_LEDGER = "zd_app/ui/screens/wear_ledger.py"

SEAM = "SEAM"
CALLER_SEAM = "CALLER_SEAM"
DEFECT = "DEFECT"
EXEMPT = "EXEMPT"
PROBE = "PROBE"
NON_MODAL = "NON_MODAL"

_BUCKETS = frozenset({SEAM, CALLER_SEAM, DEFECT, EXEMPT, PROBE, NON_MODAL})


@dataclass(frozen=True)
class RegistryEntry:
    """One accounted-for construction site."""

    relpath: str
    owner: str  # qualified name of the OWNING function (not the closure)
    tag: str | None
    modality: Modality
    tag_source: TagSource
    bucket: str
    reason: str = ""
    # Functions that perform the seam on this site's behalf. Each is
    # ``relpath::qualname`` and each is verified to exist and to mention the
    # seam. Required for CALLER_SEAM.
    seam_via: tuple[str, ...] = ()
    # How many constructions in this owner share this tag. >1 means one
    # function builds the same modal on more than one branch.
    sites: int = 1
    # Registered dead code: asserted to have zero callers anywhere in zd_app.
    dead_code: bool = False

    @property
    def key(self) -> str:
        return f"{self.relpath}::{self.owner}::{self.tag or '<untagged>'}"


MODAL_SITE_REGISTRY: tuple[RegistryEntry, ...] = (
    # -- app_shell -----------------------------------------------------------
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell._build_main_window",
        tag="main_window",
        modality=Modality.NON_MODAL,
        tag_source=TagSource.LITERAL,
        bucket=NON_MODAL,
        reason=(
            "The shell root window, no_close=True and never modal. Registered so "
            "that flipping it to modal=True fails this gate instead of quietly "
            "joining the modal slot."
        ),
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell._open_no_restore_point_confirm_now",
        tag="apply_no_restore_point_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=EXEMPT,
        reason=(
            "Was DEFECT. Two limbs, cured 2026-10-17 with the front-door rule "
            "(AppShell._modal_open_needs_seam): _open_no_restore_point_confirm "
            "builds inline only when no copy exists, its key is not pending and "
            "no transition holds the create slot, and goes through the seam "
            "otherwise. The live limb was the held slot: a manual Device write "
            "(Controller sliders, the Diagnostics deadzone) made in the tick a "
            "job's outcome modal was requested. The stale-copy branch is "
            "defensive: this modal's on_close deletes it, so the titlebar X "
            "leaves nothing behind. Entries, pinned by "
            "test_front_door_gates_have_their_known_callers: the manual-write "
            "gate (screen-scoped, so it passes owner_screen and gets the drop "
            "notice) and the profile-apply job hop, which names this tag in its "
            "own delete_tags. Not law-safe in every case: that job hop's create "
            "can still land while a user-opened modal whose button no job "
            "disables (the footer's Save As, for one) stands -- the "
            "landed-modal limit in _defer_modal_swap's "
            "docstring. EXEMPT, not SEAM, because a fresh open is not seamed; "
            "seam_via keeps both seamed routes enforced."
        ),
        seam_via=(
            f"{APP_SHELL}::AppShell._open_no_restore_point_confirm",
            f"{APP_SHELL}::AppShell._apply_wrapper_profile_resolved.on_done",
        ),
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell._open_apply_device_confirm_now",
        tag="apply_device_confirm_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=EXEMPT,
        reason=(
            "Was DEFECT: a same-pass delete+create with no on_close, gating a "
            "controller-global write; dismissing the confirm with the X (which "
            "only hides it) and pressing Apply again reached it. Cured "
            "2026-10-17 with the front-door rule: _open_apply_device_confirm "
            "builds inline only when nothing can precede the create and goes "
            "through the seam otherwise, binding the controller presence at the "
            "click and refusing (with the device-changed status) a seamed "
            "create whose controller changed. Its entries are the footer Apply "
            "and the Controller > Profiles row Apply, both via "
            "apply_named_wrapper_profile. Both are disabled while a HID job runs "
            "(footer via _HID_FLOW_BUTTON_TAGS, the row via the disabled "
            "settings editor), and apply_named_wrapper_profile also refuses "
            "while busy, as a backstop. No standing modal lets either button "
            "through. Pinned by "
            "test_front_door_gates_have_their_known_callers. This cures the hop "
            "only; ownership of the whole Apply flow (pre-write confirm, job, "
            "post-write result) is still an open design question, and the slot "
            "check narrows the landed-modal race without closing it."
        ),
        seam_via=(f"{APP_SHELL}::AppShell._open_apply_device_confirm",),
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell._show_crash_review_modal_if_any",
        tag="crash_review_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.LITERAL,
        bucket=EXEMPT,
        reason=(
            "Called once, from run(), against a Dear PyGui context created "
            "moments earlier in the same function -- so its does_item_exist "
            "guard can never find a predecessor, and no other modal can exist "
            "yet. run() also suppresses it entirely when the first-run gate "
            "shows, so the two never share a pass."
        ),
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell._show_first_run_acknowledgment_modal_if_needed",
        tag="first_run_ack_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.LITERAL,
        bucket=CALLER_SEAM,
        reason=(
            "Safe from its startup caller (fresh context, nothing to race), and "
            "that caller stays inline. Its second caller was the defect and was "
            "cured 2026-08-01: _consent_pending_write_allowed_or_refuse re-offers "
            "this gate from write entry points only reachable while another modal "
            "is PROVABLY still up -- the Safe Import confirm modal owns the button "
            "that reaches safe_import_apply -- and the law covers a predecessor "
            "merely left up, so the gate was built into an occupied pass. That "
            "limb was invisible to a delete-adjacency search; it was found by "
            "tracing callers. It now goes through _reoffer_first_run_gate, which "
            "HIDES the standing modals rather than deleting them so a "
            "half-finished import survives, and records which ones so the gate's "
            "exits can settle them: accept re-shows through the seam, the "
            "'How to verify this' link deletes them (navigating away would strand "
            "a hidden modal with no route back), decline exits the app. Gated "
            "fail-on-base by tests/test_browse_and_consent_seam_live.py, including "
            "an explicit precondition assert on the hide -- without it the accept "
            "test passes against the bug. One more direct call: when the seam is "
            "not armed (no render loop yet, or after it exits), "
            "_reoffer_first_run_gate calls this builder inline. No frames run "
            "there, so there is no pass for the law to bite in."
        ),
        seam_via=(f"{APP_SHELL}::AppShell._reoffer_first_run_gate",),
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell._show_apply_failure_modal",
        tag="apply_failure_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.LITERAL,
        bucket=CALLER_SEAM,
        sites=2,
        reason=(
            "Two build branches behind one shared does_item_exist->delete_item "
            "guard, no on_close. The owner stays inline; all three callers are "
            "covered, two of them by the seam. "
            "_finish_safe_import_apply WAS the live limb and was cured "
            "2026-08-01: the Safe Import job-start teardown sweeps the "
            "confirm/preview/result tags but NOT this one, so a stale instance "
            "hidden earlier by the titlebar X was deleted and recreated in the "
            "same completion pass -- an inert dialog reporting a failed "
            "controller write. It now splits the mandatory commit from the "
            "modal and hands only the modal to the seam, naming both openers' "
            "tags. Gated fail-on-base by "
            "tests/test_safe_import_failure_modal_seam_live.py. "
            "The third, _retry_failed_settings.on_done, built inline until "
            "2026-10-17; it now hands its modal to "
            "_open_profile_apply_readback_details_modal like the first apply, "
            "so a user click holding the create slot in that completion tick "
            "is arbitrated instead of landing beside it."
        ),
        seam_via=(
            f"{APP_SHELL}::AppShell._open_profile_apply_readback_details_modal",
            f"{APP_SHELL}::AppShell._finish_safe_import_apply",
        ),
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell.confirm_delete_named_wrapper_profile",
        tag="wrapper_profile_delete_popup",
        modality=Modality.MODAL,
        tag_source=TagSource.LITERAL,
        bucket=SEAM,
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell._open_save_as_modal",
        tag="wrapper_profile_save_as_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.LITERAL,
        bucket=SEAM,
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell.open_rename_profile_modal",
        tag="rename_profile_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.LITERAL,
        bucket=EXEMPT,
        dead_code=True,
        reason=(
            "Unreachable: no caller anywhere in the package, and the screen it "
            "belonged to no longer exists. Kept (not deleted) pending a "
            "maintainer decision; the dead_code flag asserts the caller count "
            "stays zero, so wiring it up without curing the same-pass shape "
            "fails this gate."
        ),
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell.open_import_profile_modal",
        tag="import_profile_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.LITERAL,
        bucket=EXEMPT,
        dead_code=True,
        reason=(
            "Unreachable: no caller anywhere in the package. Same disposition "
            "as rename_profile_modal above."
        ),
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell.open_support_guide",
        tag="support_guide_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.LITERAL,
        bucket=SEAM,
    ),
    RegistryEntry(
        relpath=APP_SHELL,
        owner="AppShell.clear_diagnostic_logs",
        tag="clear_diagnostic_logs_confirm_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=SEAM,
    ),
    # -- shared modal helper -------------------------------------------------
    RegistryEntry(
        relpath=BUNDLE_PREVIEW,
        owner="open_preview_modal",
        tag="diagnostic_bundle_preview_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.KWARGS_DICT,
        bucket=EXEMPT,
        reason=(
            "The only shared modal-construction helper in the tree, and the one "
            "site where both modal=True and the tag arrive through a "
            "locally-built kwargs dict rather than as call keywords -- the shape "
            "a keyword search cannot see. Its own close_preview_modal(tag) runs "
            "immediately before the create, and its two callers diverge: the "
            "Modules export path is on the seam, while "
            "AppShell.export_rich_diagnostic_bundle calls it inline. "
            "EXEMPT rather than DEFECT, and the distinction is the whole point "
            "of this entry: the inline shape is only poison when a stale item "
            "can still exist at create time, and here it cannot. Unlike every "
            "site migrated onto the seam, this modal carries an explicit "
            "on_close (in the preview's window kwargs) routing to _cancel(), "
            "which DELETES via close_preview_modal rather than leaving the "
            "titlebar X to merely hide it -- so the X-dismiss-then-reopen repro "
            "that reaches every other inline site does not reach this one. With "
            "no stale item possible, close_preview_modal at the top of the "
            "helper is always a no-op for the inline caller and the create is "
            "pure. The preview is also modal while up, so its own screen button "
            "cannot be clicked behind it; only a programmatic re-entry while it "
            "is open would reproduce the shape, and no such caller exists. "
            "Re-verify the on_close-deletes property before reclassifying: it, "
            "not the call shape, is what makes this safe. One more case, "
            "covered since 2026-10-17: Export stays enabled during HID jobs, so "
            "a click could land while a job's outcome modal holds the create "
            "slot; export_rich_diagnostic_bundle then takes the seam (the "
            "front-door rule) instead of building inline."
        ),
        seam_via=(
            f"{MODULES}::_open_zip_preview",
            f"{APP_SHELL}::AppShell.export_rich_diagnostic_bundle",
        ),
    ),
    # -- screens -------------------------------------------------------------
    RegistryEntry(
        relpath=ABOUT,
        owner="_open_license_view",
        tag="about_license_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=SEAM,
    ),
    RegistryEntry(
        relpath=MODULES,
        owner="_open_assign_modal",
        tag="modules_assign_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=SEAM,
    ),
    RegistryEntry(
        relpath=MODULES,
        owner="_open_notes_modal",
        tag="modules_notes_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=SEAM,
    ),
    RegistryEntry(
        relpath=MODULES,
        owner="_open_export_modal",
        tag="modules_export_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=SEAM,
    ),
    RegistryEntry(
        relpath=RESTORE_POINTS,
        owner="_build_list_row",
        tag=None,
        modality=Modality.POPUP,
        tag_source=TagSource.POSITIONAL,
        bucket=EXEMPT,
        reason=(
            "The row 'More' menu: dpg.popup anchored to the button, no tag, no "
            "modal= keyword anywhere on the call -- found only by searching the "
            "constructor family, and still the only dpg.popup in the app. "
            "PROBED LIVE 2026-08-01 and the optimistic reading was WRONG: "
            "clicking Delete inside this popup left the popup standing and the "
            "confirm modal never appeared, so a destructive action silently did "
            "nothing. Dear PyGui defaults no_open_over_existing_popup to True, "
            "which ImGui reads as NoOpenOverExistingPopup -- a modal does not "
            "open AT ALL while a popup is up. NOT the same-pass law: the seam "
            "was already in use and the modal still never opened, which is "
            "exactly why the symptoms were indistinguishable and this sat as "
            "PROBE. Cured at the modal being opened, not here -- "
            "_on_open_delete_confirm now passes no_open_over_existing_popup=False, "
            "gated fail-on-base by "
            "tests/test_restore_point_delete_modal_live.py. The popup itself "
            "creates no modal, so it is EXEMPT rather than seamed. "
            "NOTE the probe run only reached Check 1: that the confirm modal now "
            "OPENS is fixed and gated; that its Confirm button actually deletes "
            "was BLOCKED live (the modal never opened, so it could not be "
            "clicked) and rests on mocked coverage. Worth one minute on the next "
            "smoke. Also: that run needed a build carrying the More row "
            "action, and not every build does (see the filter below)."
        ),
    ),
    RegistryEntry(
        relpath=RESTORE_POINTS,
        owner="_on_open_delete_confirm",
        tag="restore_points_delete_confirm_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=SEAM,
    ),
    RegistryEntry(
        relpath=SAFE_IMPORT,
        owner="open_file_select",
        tag="safe_import_file_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=EXEMPT,
        reason=(
            "The front-door design shipped in v2.6.4 (LegendCTL PR #29) and kept on "
            "2026-10-17: the owner builds INLINE only when "
            "nothing can precede the create, and goes through the seam "
            "otherwise -- a stale front door still exists (the titlebar X only "
            "hides it), its own swap key is pending, or another transition "
            "holds the create slot. Registered EXEMPT rather than SEAM because "
            "SEAM would claim every create is seamed, and a fresh open is not. "
            "The inline branch is law-safe: its pass has no teardown, and its "
            "only entry button (Controller > Profiles > Import) sits inside the "
            "settings editor, which cannot be clicked while any modal stands or "
            "while a HID job is in flight. The slot check covers the one "
            "predecessor that could still be coming -- another transition's "
            "create queued for the next pass, typically a threaded job's "
            "outcome modal -- which narrows, but does not close, the window for "
            "the front door to land beside it: a callback that runs after that "
            "create has claimed and released the slot still builds inline (the "
            "seam does not track landed modals; see _defer_modal_swap). seam_via "
            "names this owner itself so the reopen branch's seam stays "
            "enforced. Gated by tests/test_about_safe_import_modal_seam_live.py "
            "(reopen split and navigation, the fresh inline build, and both "
            "slot-held cases) and by tests/test_safe_import_picker_return.py. "
            "The single-entry claim is pinned, not just stated: "
            "test_safe_import_front_door_has_one_entry fails if anything else "
            "names open_file_select or open_safe_import in the ordinary ways -- "
            "called, passed as a callback, imported, or as a literal getattr "
            "string. A tripwire for ordinary wiring, not a proof; its "
            "docstring lists what it misses."
        ),
        seam_via=(f"{SAFE_IMPORT}::open_file_select",),
    ),
    RegistryEntry(
        relpath=SAFE_IMPORT,
        owner="open_browse",
        tag="safe_import_file_dialog",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=SEAM,
        reason=(
            "dpg.file_dialog(modal=True) opened from a button INSIDE the Safe "
            "Import front-door modal, which stays up. PROBED and BENCHED "
            "2026-08-01; the answer was neither of the two the probe expected. "
            "The picker renders and accepts input -- the window-modal law does "
            "NOT transfer to the file dialog's draw path -- but the dialog takes "
            "the standing front door down with it: "
            "a 2026-08-01 bench showed the front door's "
            "visible flips to False PERMANENTLY while it keeps existing, and "
            "closing the dialog never restores it. open_browse never touched "
            "that tag, so it was DPG's doing. Live symptom: Browse -> Cancel "
            "left the user on the bare Profiles tab with the import flow gone. "
            "Cured by re-showing the front door through the seam on BOTH dialog "
            "exits (cancel_callback was absent entirely). Confirmed live on a "
            "build carrying the fix: cancel brings the front door back "
            "immediately and re-entry opens a working picker. "
            "The dialog's own create is deliberately NOT seamed -- it renders "
            "correctly as-is; what rides the seam is the restore. And the front "
            "door being hidden WHILE the dialog is open is not curable here: DPG "
            "never shows two modals stacked, so any expectation that the front "
            "door stays visible behind the picker is unsatisfiable by "
            "construction. Do not re-open the site on those grounds."
        ),
    ),
    RegistryEntry(
        relpath=SAFE_IMPORT,
        owner="open_preview",
        tag="safe_import_preview_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=CALLER_SEAM,
        reason=(
            "Screen-side step function with no shell reference of its own; the "
            "shell owns the hop and tears down the front-door modal first."
        ),
        seam_via=(f"{APP_SHELL}::AppShell.safe_import_scan_path",),
    ),
    RegistryEntry(
        relpath=SAFE_IMPORT,
        owner="open_apply_confirm",
        tag="safe_import_confirm_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=CALLER_SEAM,
        reason=(
            "Same topology as open_preview. The shell's hop HIDES the preview "
            "rather than deleting it, so the typed profile name survives while "
            "the confirm modal is up."
        ),
        seam_via=(f"{APP_SHELL}::AppShell.safe_import_request_apply_to_controller",),
    ),
    RegistryEntry(
        relpath=SAFE_IMPORT,
        owner="open_result",
        tag="safe_import_result_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=CALLER_SEAM,
        reason=(
            "Reached two ways, and the seam is a caller-of-a-caller rather than "
            "the direct caller: the store-only path wraps "
            "_finish_safe_import_apply in the shell's hop, while the apply-job "
            "path tears the import modals down at job start and creates this one "
            "from a later completion drain, with rendered frames in between. "
            "The direct callers therefore correctly do NOT mention the seam. "
            "The fail-closed abort (a missing restore point) also runs from a "
            "job completion and, since 2026-10-17, hands its result modal to "
            "the seam with the success path's key and teardown."
        ),
        seam_via=(
            f"{APP_SHELL}::AppShell.safe_import_apply",
            f"{APP_SHELL}::AppShell._finish_safe_import_abort",
        ),
    ),
    RegistryEntry(
        relpath=WEAR_LEDGER,
        owner="_open_note_modal",
        tag="wear_ledger_note_modal",
        modality=Modality.MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=SEAM,
    ),
    RegistryEntry(
        relpath=TRUST_RITUAL,
        owner="_build_trust_ritual_card",
        tag="trust_ritual_card",
        modality=Modality.NON_MODAL,
        tag_source=TagSource.MODULE_CONST,
        bucket=NON_MODAL,
        reason=(
            "Explicitly modal=False, and it deliberately does not occupy the "
            "modal slot so the first-run gate can show over it. It DOES use the "
            "delete-then-create-inline shape, which is safe only while it stays "
            "non-modal -- registering it here makes a flip to modal=True fail "
            "this gate rather than ship a latent dead card."
        ),
    ),
)

# The restore-row ``More`` menu is not in every build of this tree. Where its
# label key is absent from the English locale, the popup is absent too, so its
# entry does not apply there. Keyed on the locale rather than on the source, so
# a build that keeps the key but loses the popup still fails the gate.
_EN_LOCALE = REPO_ROOT / "zd_app/i18n/locales/en.json"
if '"restore_points.list.row.more"' not in _EN_LOCALE.read_text(encoding="utf-8"):
    MODAL_SITE_REGISTRY = tuple(
        entry
        for entry in MODAL_SITE_REGISTRY
        if not (entry.relpath == RESTORE_POINTS and entry.owner == "_build_list_row")
    )


# Discovery holes the walker reports but cannot analyse. Each must be
# acknowledged by hand; an unacknowledged hole fails the gate.
ACKNOWLEDGED_DISCOVERY_HOLES: dict[str, str] = {
    f"{APP_SHELL}::dynamic_dpg_dispatch::getattr(dpg, ...) in "
    "AppShell._viewport_client_width": (
        "Reads a viewport dimension through a getter name chosen at runtime. "
        "Dynamic attribute access on the Dear PyGui module is opaque to static "
        "analysis, so the walker reports it; this one resolves to size getters "
        "only and constructs nothing."
    ),
}


# ---------------------------------------------------------------------------
# The hand-enumerated sweep list in app_shell, and its known drift.
# ---------------------------------------------------------------------------

# Registered modal tags that the Clear-Logs sweep tuple does NOT carry today.
# Each is a real gap, recorded so the gate can pin the tuple exactly without
# pretending the tuple is correct. Removing a tag from this map is the required
# second half of adding it to the tuple.
SWEEP_LIST_KNOWN_GAPS: dict[str, str] = {
    "apply_failure_modal": (
        "The only modal that reports a failed controller write, and the one "
        "whose stale instance drives the live Safe Import defect limb."
    ),
    "about_license_modal": "About -> Licenses; never added when the screen gained it.",
    "modules_assign_modal": (
        "Now on the seam, but a stale instance still survives a Clear Logs sweep."
    ),
    "modules_notes_modal": "Same as modules_assign_modal.",
    "safe_import_file_dialog": (
        "The Browse file dialog; a dialog item outlives the sweep like any other."
    ),
    "wear_ledger_note_modal": "Wear Ledger service note; never added.",
}

# Tags the sweep tuple carries whose modal cannot be opened by any code path.
# Deliberately left in place; the gate asserts they stay unreachable.
SWEEP_LIST_UNREACHABLE_ENTRIES: frozenset[str] = frozenset(
    {"rename_profile_modal", "import_profile_modal"}
)


# Every seam call site's drop-notice setting, keyed ``file::function::key``
# (a variable key shows as ``<name>``; "-" means the kwarg is absent, i.e.
# False; a bare name means the caller passes its own flag through). The rule:
# a user click asks for the notice; a request made right after the caller
# wrote its own result line (job completions, the consent re-offer and its
# restore, the store-only Safe Import save) does not, because "try again"
# over a failure report invites a repeat write. A new seam site must be
# classified here, deliberately.
MODAL_SWAP_NOTIFY_FLAGS: dict[str, str] = {
    "zd_app/ui/app_shell.py::AppShell._apply_wrapper_profile_resolved.on_done::apply_profile_no_restore_point_confirm": "-",
    "zd_app/ui/app_shell.py::AppShell._finish_safe_import_abort::safe_import_open_result": "-",
    "zd_app/ui/app_shell.py::AppShell._finish_safe_import_apply::safe_import_open_result": "-",
    "zd_app/ui/app_shell.py::AppShell._open_apply_device_confirm::<swap_key>": "True",
    "zd_app/ui/app_shell.py::AppShell._open_no_restore_point_confirm::<swap_key>": "user_request",
    "zd_app/ui/app_shell.py::AppShell._open_profile_apply_readback_details_modal::profile_apply_details": "user_request",
    "zd_app/ui/app_shell.py::AppShell._open_save_as_modal::wrapper_profile_save_as": "True",
    "zd_app/ui/app_shell.py::AppShell._reoffer_first_run_gate::first_run_consent_reoffer": "-",
    "zd_app/ui/app_shell.py::AppShell._settle_consent_reoffer::first_run_consent_restore": "-",
    "zd_app/ui/app_shell.py::AppShell.clear_diagnostic_logs::clear_diagnostic_logs_confirm": "True",
    "zd_app/ui/app_shell.py::AppShell.confirm_delete_named_wrapper_profile::wrapper_profile_delete": "True",
    "zd_app/ui/app_shell.py::AppShell.export_rich_diagnostic_bundle::<swap_key>": "True",
    "zd_app/ui/app_shell.py::AppShell.open_support_guide::support_guide": "True",
    "zd_app/ui/app_shell.py::AppShell.safe_import_apply::safe_import_open_result": "-",
    "zd_app/ui/app_shell.py::AppShell.safe_import_cancel_apply_confirm::safe_import_cancel_confirm": "True",
    "zd_app/ui/app_shell.py::AppShell.safe_import_request_apply_to_controller::safe_import_open_confirm": "True",
    "zd_app/ui/app_shell.py::AppShell.safe_import_scan_path::safe_import_open_preview": "True",
    "zd_app/ui/screens/about.py::_open_license_view::about_license_view": "True",
    "zd_app/ui/screens/modules.py::_open_assign_modal::modules_assign_modal": "True",
    "zd_app/ui/screens/modules.py::_open_export_modal::modules_export_modal": "True",
    "zd_app/ui/screens/modules.py::_open_notes_modal::modules_notes_modal": "True",
    "zd_app/ui/screens/modules.py::_open_zip_preview::modules_export_zip_preview": "True",
    "zd_app/ui/screens/restore_points.py::_on_open_delete_confirm::restore_points_delete_confirm": "True",
    "zd_app/ui/screens/safe_import.py::open_browse._close_and_restore::safe_import_browse_close": "True",
    "zd_app/ui/screens/safe_import.py::open_file_select::<swap_key>": "True",
    "zd_app/ui/screens/wear_ledger.py::_open_note_modal::wear_ledger_note_modal": "True",
}


def _modal_swap_notify_flags() -> dict[str, str]:
    found: dict[str, str] = {}

    def walk(node: ast.AST, relpath: str, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, relpath, scope + [child.name])
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else None
                )
                if name in ("_defer_modal_swap", "modal_swap"):
                    kwargs = {kw.arg: kw.value for kw in child.keywords}
                    key, flag = kwargs.get("key"), kwargs.get("notify_if_dropped")
                    key_text = (
                        key.value
                        if isinstance(key, ast.Constant)
                        else f"<{key.id}>" if isinstance(key, ast.Name) else "-"
                    )
                    flag_text = (
                        repr(flag.value)
                        if isinstance(flag, ast.Constant)
                        else flag.id if isinstance(flag, ast.Name) else "-"
                    )
                    found[f"{relpath}::{'.'.join(scope)}::{key_text}"] = flag_text
            walk(child, relpath, scope)

    for path in iter_source_files(PACKAGE_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        walk(tree, path.relative_to(REPO_ROOT).as_posix(), [])
    return found


def _references_to(name: str) -> list[str]:
    """``relpath::qualname`` of every function under ``zd_app`` naming ``name``.

    Unlike ``find_callers`` this counts any use of the name -- loads and
    assignments alike, call or not -- so a bound method handed over as a
    callback is seen too. It also counts the name as a literal string argument
    to a call, positional or keyword (``getattr(shell, "name")``), and as an
    imported name (``from ... import name as alias``). Walks ``zd_app`` and
    ``main_zd.py``. Sorted and de-duplicated by function.

    A tripwire for ordinary wiring, not a proof. It misses indirection: the name
    held in a constant or variable, looked up through ``vars()`` or a dict,
    built in an f-string, or given as a parameter's default. It can also
    over-report: ``hasattr(shell, "name")`` or a log call whose argument is
    exactly the name counts as a reference.
    """

    hits: set[str] = set()

    def walk(node: ast.AST, relpath: str, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, relpath, scope + [child.name])
                continue
            named = (
                (isinstance(child, ast.Attribute) and child.attr == name)
                or (isinstance(child, ast.Name) and child.id == name)
                or (
                    isinstance(child, ast.ImportFrom)
                    and any(alias.name == name for alias in child.names)
                )
                or (
                    isinstance(child, ast.Call)
                    and any(
                        isinstance(arg, ast.Constant) and arg.value == name
                        for arg in [
                            *child.args,
                            *(keyword.value for keyword in child.keywords),
                        ]
                    )
                )
            )
            if named:
                hits.add(f"{relpath}::{'.'.join(scope) or '<module>'}")
            walk(child, relpath, scope)

    for path in [*iter_source_files(PACKAGE_ROOT), REPO_ROOT / "main_zd.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        walk(tree, path.relative_to(REPO_ROOT).as_posix(), [])
    return sorted(hits)


def _registry_by_key() -> dict[str, RegistryEntry]:
    return {entry.key: entry for entry in MODAL_SITE_REGISTRY}


def _group(sites) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for site in sites:
        grouped.setdefault(site.key, []).append(site)
    return grouped


def _discovered_by_key():
    sites, holes = discover()
    return _group(sites), holes


# -- the two predicates the gate turns on ------------------------------------
# Extracted as plain functions so GateSelfCheckTests can run the SAME logic
# against a deliberately-broken synthetic tree. A gate whose failure path is
# never executed is a gate nobody has checked.


def unregistered_keys(discovered: dict[str, list], registered: dict[str, RegistryEntry]) -> list[str]:
    """Discovered construction sites with no registry entry."""

    return sorted(set(discovered) - set(registered))


def seam_regressions(
    discovered: dict[str, list], entries: tuple[RegistryEntry, ...]
) -> list[str]:
    """Sites registered as SEAM whose owner no longer references the seam."""

    broken: list[str] = []
    for entry in entries:
        if entry.bucket != SEAM:
            continue
        for site in discovered.get(entry.key, []):
            if not site.owner_uses_seam:
                broken.append(entry.key)
    return sorted(set(broken))


class RegistryShapeTests(unittest.TestCase):
    """The registry itself must be well-formed before it can gate anything."""

    def test_keys_are_unique(self) -> None:
        keys = [entry.key for entry in MODAL_SITE_REGISTRY]
        self.assertEqual(len(keys), len(set(keys)), "duplicate registry keys")

    def test_buckets_are_known(self) -> None:
        for entry in MODAL_SITE_REGISTRY:
            self.assertIn(entry.bucket, _BUCKETS, f"{entry.key}: unknown bucket")

    def test_non_seam_entries_carry_a_human_reason(self) -> None:
        """A bucket label without a reason is decoration, not a decision."""

        for entry in MODAL_SITE_REGISTRY:
            if entry.bucket == SEAM:
                continue
            self.assertTrue(
                entry.reason.strip(),
                f"{entry.key}: bucket {entry.bucket} requires a written reason",
            )
            self.assertGreaterEqual(
                len(entry.reason.split()),
                8,
                f"{entry.key}: reason is too thin to be a real justification",
            )

    def test_caller_seam_entries_name_their_seam_owner(self) -> None:
        for entry in MODAL_SITE_REGISTRY:
            if entry.bucket != CALLER_SEAM:
                continue
            self.assertTrue(
                entry.seam_via,
                f"{entry.key}: CALLER_SEAM must name the function that seams it",
            )


class ModalSiteEnumerationTests(unittest.TestCase):
    """Every discovered construction is accounted for, and vice versa."""

    def test_every_discovered_site_is_registered(self) -> None:
        """The gate that fires when someone adds a modal.

        This is the whole point of the module: a new ``dpg.window(modal=True)``
        anywhere under ``zd_app/`` lands here as an unregistered key, and the
        author has to classify it -- seam it, or say in writing why not.
        """

        discovered, _ = _discovered_by_key()
        registered = _registry_by_key()
        unregistered = unregistered_keys(discovered, registered)
        self.assertFalse(
            unregistered,
            "Unregistered Dear PyGui window-family construction(s):\n  "
            + "\n  ".join(
                f"{key}  (line {discovered[key][0].lineno}, "
                f"{discovered[key][0].modality.value})"
                for key in unregistered
            )
            + "\n\nEvery modal construction must appear in MODAL_SITE_REGISTRY in "
            "tests/test_modal_site_registry.py, classified, with a written reason "
            "if it is not routed through AppShell._defer_modal_swap. See the "
            "module docstring for why.",
        )

    def test_every_registered_site_still_exists(self) -> None:
        """Catches a stale registry after a rename, move or deletion."""

        discovered, _ = _discovered_by_key()
        registered = _registry_by_key()
        missing = sorted(set(registered) - set(discovered))
        self.assertFalse(
            missing,
            "Registry entries with no matching construction in the tree "
            "(renamed, moved or removed?):\n  " + "\n  ".join(missing),
        )

    def test_site_counts_match(self) -> None:
        """A second construction slipped into an already-registered function."""

        discovered, _ = _discovered_by_key()
        for entry in MODAL_SITE_REGISTRY:
            found = discovered.get(entry.key, [])
            self.assertEqual(
                len(found),
                entry.sites,
                f"{entry.key}: registry expects {entry.sites} construction(s), "
                f"found {len(found)} at line(s) {[s.lineno for s in found]}",
            )

    def test_modality_and_tag_source_match(self) -> None:
        """Pins HOW the walker resolved each site, not just that it did.

        A site that used to spell ``modal=True`` inline and now takes it from a
        splatted dict is a different auditing problem even if the tag is
        unchanged, so the resolution path is part of the contract.
        """

        discovered, _ = _discovered_by_key()
        for entry in MODAL_SITE_REGISTRY:
            for site in discovered.get(entry.key, []):
                self.assertEqual(
                    site.modality,
                    entry.modality,
                    f"{entry.key}: modality changed to {site.modality.value}",
                )
                self.assertEqual(
                    site.tag_source,
                    entry.tag_source,
                    f"{entry.key}: tag now resolves via {site.tag_source.value}",
                )

    def test_no_unresolved_or_undecidable_site_is_unregistered(self) -> None:
        """Undecidable sites fail loudly; they are never skipped.

        Covered by the enumeration test above, but asserted separately so the
        failure message says what is actually wrong when a construction becomes
        statically opaque.
        """

        discovered, _ = _discovered_by_key()
        registered = _registry_by_key()
        opaque = [
            site
            for sites in discovered.values()
            for site in sites
            if site.modality is Modality.UNDECIDABLE
            or site.tag_source is TagSource.UNRESOLVED
        ]
        for site in opaque:
            self.assertIn(
                site.key,
                registered,
                f"{site.relpath}:{site.lineno}: the walker cannot decide this "
                "construction statically. It must be registered by hand -- "
                "silently skipping it would defeat the gate.",
            )

    def test_discovery_holes_are_acknowledged(self) -> None:
        _, holes = _discovered_by_key()
        unacknowledged = [h for h in holes if h.key not in ACKNOWLEDGED_DISCOVERY_HOLES]
        self.assertFalse(
            unacknowledged,
            "Unacknowledged discovery hole(s) -- code the walker cannot read:\n  "
            + "\n  ".join(f"{h.relpath}:{h.lineno} {h.kind}: {h.detail}" for h in unacknowledged),
        )
        for reason in ACKNOWLEDGED_DISCOVERY_HOLES.values():
            self.assertTrue(reason.strip(), "acknowledged hole needs a reason")


class SeamClaimTests(unittest.TestCase):
    """Registry claims about the seam are verified against source, not trusted."""

    def test_seam_entries_actually_route_through_the_seam(self) -> None:
        """The gate that fires when a seamed site regresses to inline.

        Deleting the ``_defer_modal_swap`` hop from a function registered as
        ``SEAM`` -- reverting to ``delete_item`` then an inline ``dpg.window``
        -- makes this fail, which is the second half of the enumeration
        guarantee: the list stays complete AND the cures stay in place.
        """

        discovered, _ = _discovered_by_key()
        broken = seam_regressions(discovered, MODAL_SITE_REGISTRY)
        self.assertFalse(
            broken,
            "Site(s) registered SEAM whose owning function no longer mentions "
            "AppShell._defer_modal_swap:\n  " + "\n  ".join(broken) + "\n\n"
            "Either the hop was removed -- a regression to the same-pass "
            "teardown+create shape the seam exists to prevent -- or the site "
            "moved to a different owner and the registry needs updating.",
        )

    def test_non_seam_entries_have_not_been_quietly_fixed(self) -> None:
        """Keeps the ledger honest in the other direction.

        A DEFECT entry whose owner has gained the seam is stale: the finding is
        cured and the bucket should say so, otherwise the report keeps claiming
        an outstanding defect that no longer exists.
        """

        discovered, _ = _discovered_by_key()
        for entry in MODAL_SITE_REGISTRY:
            if entry.bucket not in {DEFECT, CALLER_SEAM}:
                continue
            for site in discovered.get(entry.key, []):
                self.assertFalse(
                    site.owner_uses_seam,
                    f"{entry.key}: registered {entry.bucket}, but {entry.owner} "
                    "now routes through the seam. Reclassify it as SEAM.",
                )

    def test_named_seam_owners_exist_and_use_the_seam(self) -> None:
        for entry in MODAL_SITE_REGISTRY:
            for reference in entry.seam_via:
                relpath, qualname = reference.split("::")
                uses = function_uses_seam(relpath, qualname)
                self.assertIsNotNone(
                    uses,
                    f"{entry.key}: seam_via names {reference}, which does not exist",
                )
                self.assertTrue(
                    uses,
                    f"{entry.key}: seam_via names {reference}, which no longer "
                    "mentions AppShell._defer_modal_swap",
                )

    def test_dead_code_entries_still_have_no_callers(self) -> None:
        """Registered-dead modals must stay unreachable while they survive.

        They are kept rather than deleted pending a maintainer decision. If
        anything ever wires one up, it becomes a live modal with an uncured
        same-pass shape, and this fails. ``_references_to`` rather than
        ``find_callers``, because the likeliest wiring is a button's
        ``callback=shell.fn``, which is a reference with no call.
        """

        for entry in MODAL_SITE_REGISTRY:
            if not entry.dead_code:
                continue
            function_name = entry.owner.split(".")[-1]
            callers = _references_to(function_name)
            self.assertEqual(
                callers,
                [],
                f"{entry.key}: registered as dead code but now called from "
                f"{callers}. Route it through the seam before wiring it up.",
            )

    def test_inline_builders_have_one_entry(self) -> None:
        """The two front-door builders are reached only through their gate.

        ``_open_*_now`` builds inline with no seam check of its own; the check
        lives in the wrapper. A second caller wired straight to the builder
        would skip the front-door rule, so every reference is pinned.
        """

        self.assertEqual(
            _references_to("_open_no_restore_point_confirm_now"),
            [f"{APP_SHELL}::AppShell._open_no_restore_point_confirm"],
        )
        self.assertEqual(
            _references_to("_open_apply_device_confirm_now"),
            [
                f"{APP_SHELL}::AppShell._open_apply_device_confirm",
                f"{APP_SHELL}::AppShell._open_apply_device_confirm.open_if_still_current",
            ],
        )

    def test_every_seam_site_has_a_deliberate_drop_notice_setting(self) -> None:
        self.assertEqual(_modal_swap_notify_flags(), MODAL_SWAP_NOTIFY_FLAGS)

    def test_readback_details_callers_pass_the_right_notice_setting(self) -> None:
        """The details hop passes its caller's ``user_request`` through.

        The flag table above records only the pass-through, so the callers are
        pinned here: the footer button is a click and asks for the notice; the
        apply and retry completions have just written their own result line
        and must not.
        """

        found: dict[str, str] = {}
        tree = ast.parse((PACKAGE_ROOT / "ui" / "app_shell.py").read_text(encoding="utf-8"))

        def walk(node: ast.AST, scope: list[str]) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, scope + [child.name])
                    continue
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "_open_profile_apply_readback_details_modal"
                ):
                    flag = {kw.arg: kw.value for kw in child.keywords}.get("user_request")
                    found[".".join(scope)] = (
                        repr(flag.value) if isinstance(flag, ast.Constant) else "-"
                    )
                walk(child, scope)

        walk(tree, [])
        self.assertEqual(
            found,
            {
                "AppShell._build_footer": "True",
                "AppShell._apply_wrapper_profile_snapshot.on_done": "-",
                "AppShell._retry_failed_settings.on_done": "-",
            },
        )

    def test_apply_failure_modal_is_built_only_behind_the_seam(self) -> None:
        """No completion path may build the failure/Retry modal inline again.

        Both references are seamed routes: the first apply's details hop (which
        the retry completion now reuses) and Safe Import's commit, whose
        returned opener the caller hands to the seam.
        """

        self.assertEqual(
            _references_to("_show_apply_failure_modal"),
            [
                f"{APP_SHELL}::AppShell._commit_safe_import_apply_result",
                f"{APP_SHELL}::AppShell._open_profile_apply_readback_details_modal",
            ],
        )

    def test_front_door_gates_have_their_known_callers(self) -> None:
        """The EXEMPT reasons for the two Apply confirms argue from callers.

        Their inline branch is law-safe because each entry is a button no
        standing modal lets through (or, for the job hop, a create already
        paced by the seam). A new caller -- a button inside a modal, a timer
        -- would need its own argument, so the caller sets are pinned.
        """

        self.assertEqual(
            _references_to("_open_no_restore_point_confirm"),
            [
                f"{APP_SHELL}::AppShell._apply_wrapper_profile_resolved.on_done"
                ".open_no_restore_point_if_current",
                f"{APP_SHELL}::AppShell._manual_device_write_gate",
            ],
        )
        self.assertEqual(
            _references_to("_manual_device_write_gate"),
            [
                f"{APP_SHELL}::AppShell.apply_deadzone_settings",
                f"{APP_SHELL}::AppShell.apply_diagnostics_deadzone",
                f"{APP_SHELL}::AppShell.apply_polling_rate",
                f"{APP_SHELL}::AppShell.apply_step_size",
            ],
        )
        self.assertEqual(
            _references_to("_open_apply_device_confirm"),
            [f"{APP_SHELL}::AppShell.apply_named_wrapper_profile"],
        )
        self.assertEqual(
            _references_to("apply_named_wrapper_profile"),
            [
                f"{APP_SHELL}::AppShell.apply_selected_wrapper_profile",
                "zd_app/ui/screens/controller.py::_render_profiles_tab",
            ],
        )

    def test_safe_import_front_door_has_one_entry(self) -> None:
        """The EXEMPT reason for ``open_file_select`` rests on caller topology.

        Its inline branch is law-safe only because the one way in is the
        Controller > Profiles > Import button, inside an editor that cannot be
        clicked while a modal stands. Wiring the front door to a button inside
        a modal (an "Import another" in the result modal, say) would run that
        inline branch over a standing modal, and neither SEAM nor EXEMPT would
        notice. So every reference counts here, calls and bare callbacks alike:
        ``callback=shell.open_safe_import`` is a reference with no call.
        """

        self.assertEqual(
            _references_to("open_file_select"),
            [f"{APP_SHELL}::AppShell.open_safe_import"],
        )
        self.assertEqual(
            _references_to("open_safe_import"),
            ["zd_app/ui/screens/controller.py::_render_profiles_tab"],
        )


class SweepListDriftTests(unittest.TestCase):
    """Pins the hand-enumerated Clear-Logs teardown tuple to this registry.

    ``CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS`` lists the modal tags the
    Clear-Logs confirm hop tears down before it opens. It is typed out by hand
    and has drifted in both directions: it omits live user-facing modals and
    carries tags for modals no code path can open. Nothing forced a new modal
    onto it -- until this test.
    """

    def _sweep_tags(self) -> tuple[str, ...]:
        return read_string_tuple(APP_SHELL, "CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS")

    def _registered_modal_tags(self) -> set[str]:
        return {
            entry.tag
            for entry in MODAL_SITE_REGISTRY
            if entry.tag is not None and entry.modality is Modality.MODAL
        }

    def test_sweep_list_matches_registry_minus_known_gaps(self) -> None:
        """Exact set equality, so drift in either direction fails.

        A new modal that is not added to the tuple fails here (it is registered
        but neither swept nor listed as a known gap). Closing a known gap also
        fails until the tag is removed from :data:`SWEEP_LIST_KNOWN_GAPS` --
        cheap, and it keeps the gap list from rotting into fiction.
        """

        expected = self._registered_modal_tags() - set(SWEEP_LIST_KNOWN_GAPS)
        actual = set(self._sweep_tags())
        self.assertEqual(
            actual,
            expected,
            "CLEAR_DIAGNOSTIC_LOGS_MODAL_SWAP_DELETE_TAGS has drifted.\n"
            f"  in the tuple but not registered: {sorted(actual - expected)}\n"
            f"  registered but not swept:        {sorted(expected - actual)}\n"
            "Add the tag to the tuple, or record it in SWEEP_LIST_KNOWN_GAPS "
            "with a reason.",
        )

    def test_known_gaps_are_really_absent_from_the_tuple(self) -> None:
        actual = set(self._sweep_tags())
        for tag, reason in SWEEP_LIST_KNOWN_GAPS.items():
            self.assertTrue(reason.strip(), f"{tag}: known gap needs a reason")
            self.assertIn(
                tag,
                self._registered_modal_tags(),
                f"{tag}: listed as a sweep gap but is not a registered modal tag",
            )
            self.assertNotIn(
                tag,
                actual,
                f"{tag}: now present in the sweep tuple -- remove it from "
                "SWEEP_LIST_KNOWN_GAPS",
            )

    def test_unreachable_entries_are_still_listed_and_still_unreachable(self) -> None:
        actual = set(self._sweep_tags())
        registered = {entry.tag: entry for entry in MODAL_SITE_REGISTRY}
        for tag in SWEEP_LIST_UNREACHABLE_ENTRIES:
            self.assertIn(tag, actual, f"{tag}: expected in the sweep tuple")
            entry = registered.get(tag)
            self.assertIsNotNone(entry, f"{tag}: not in the registry")
            self.assertTrue(
                entry.dead_code,
                f"{tag}: swept but no longer registered as dead code",
            )

    def test_every_swept_tag_maps_to_a_real_construction(self) -> None:
        """A tag in the tuple that names no modal is either dead or a typo."""

        registered = self._registered_modal_tags()
        for tag in self._sweep_tags():
            self.assertIn(
                tag,
                registered,
                f"{tag!r} is swept by the Clear-Logs hop but no registered modal "
                "construction produces it.",
            )


class GateSelfCheckTests(unittest.TestCase):
    """Proves the gate can fail.

    An enumeration gate is only worth its runtime if its failure path actually
    fires. These two cases build a throwaway source tree, run the REAL walker
    over it, and feed the result to the REAL predicates the tests above use --
    so a refactor that quietly neuters discovery (an import form the alias
    resolver stops recognising, say) shows up here as a self-check that can no
    longer produce a failure.
    """

    def _build_tree(self, root) -> None:
        screens = root / "zd_app" / "ui" / "screens"
        screens.mkdir(parents=True)
        for package in (root / "zd_app", root / "zd_app" / "ui", screens):
            (package / "__init__.py").write_text("", encoding="utf-8")

        # A faithful miniature of a seam-routed site, at the real path and with
        # the real owner name and tag, so its key matches the registry entry.
        (screens / "restore_points.py").write_text(
            'import dearpygui.dearpygui as dpg\n'
            '\n'
            'TAG_DELETE_CONFIRM_MODAL = "restore_points_delete_confirm_modal"\n'
            '\n'
            '\n'
            'def _on_open_delete_confirm(shell, rp_id, title):\n'
            '    def _open():\n'
            '        with dpg.window(tag=TAG_DELETE_CONFIRM_MODAL, modal=True):\n'
            '            pass\n'
            '\n'
            '    modal_swap = getattr(shell, "_defer_modal_swap", None)\n'
            '    modal_swap(_open, delete_tags=(TAG_DELETE_CONFIRM_MODAL,))\n',
            encoding="utf-8",
        )

    def test_a_new_unregistered_modal_fails_the_gate(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_tree(root)
            (root / "zd_app" / "ui" / "screens" / "new_screen.py").write_text(
                'import dearpygui.dearpygui as dpg\n'
                '\n'
                'TAG_SOMETHING_MODAL = "something_modal"\n'
                '\n'
                '\n'
                'def open_something(shell):\n'
                '    if dpg.does_item_exist(TAG_SOMETHING_MODAL):\n'
                '        dpg.delete_item(TAG_SOMETHING_MODAL)\n'
                '    with dpg.window(tag=TAG_SOMETHING_MODAL, modal=True):\n'
                '        pass\n',
                encoding="utf-8",
            )
            sites, _ = discover(root=root / "zd_app", base=root)
            unregistered = unregistered_keys(_group(sites), _registry_by_key())

        self.assertEqual(
            unregistered,
            ["zd_app/ui/screens/new_screen.py::open_something::something_modal"],
            "the gate did not flag a brand-new unregistered modal construction",
        )

    def test_a_seam_site_reverting_to_inline_fails_the_gate(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_tree(root)
            # Same file, same owner, same tag -- but the hop is gone and the
            # create is inline again, immediately after the teardown.
            (root / "zd_app" / "ui" / "screens" / "restore_points.py").write_text(
                'import dearpygui.dearpygui as dpg\n'
                '\n'
                'TAG_DELETE_CONFIRM_MODAL = "restore_points_delete_confirm_modal"\n'
                '\n'
                '\n'
                'def _on_open_delete_confirm(shell, rp_id, title):\n'
                '    if dpg.does_item_exist(TAG_DELETE_CONFIRM_MODAL):\n'
                '        dpg.delete_item(TAG_DELETE_CONFIRM_MODAL)\n'
                '    with dpg.window(tag=TAG_DELETE_CONFIRM_MODAL, modal=True):\n'
                '        pass\n',
                encoding="utf-8",
            )
            sites, _ = discover(root=root / "zd_app", base=root)
            broken = seam_regressions(_group(sites), MODAL_SITE_REGISTRY)

        self.assertEqual(
            broken,
            [
                "zd_app/ui/screens/restore_points.py::_on_open_delete_confirm"
                "::restore_points_delete_confirm_modal"
            ],
            "the gate did not flag a seam-routed site reverting to an inline "
            "teardown-then-create",
        )

    def test_a_dead_seam_getattr_next_to_an_inline_create_fails_the_gate(self) -> None:
        """The mutation the seam check used to wave through.

        The old predicate accepted a *mention*: any constant equal to
        ``"_defer_modal_swap"`` made the owning function count as seamed. So the
        shape below -- fetch the seam, never call it, then delete and rebuild
        the modal inline in one pass -- passed as SEAM while being exactly the
        defect the registry exists to surface. Neither self-check above
        exercised it, because both removed the mention entirely.
        """

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_tree(root)
            (root / "zd_app" / "ui" / "screens" / "restore_points.py").write_text(
                'import dearpygui.dearpygui as dpg\n'
                '\n'
                'TAG_DELETE_CONFIRM_MODAL = "restore_points_delete_confirm_modal"\n'
                '\n'
                '\n'
                'def _on_open_delete_confirm(shell, rp_id, title):\n'
                '    def _open():\n'
                '        with dpg.window(tag=TAG_DELETE_CONFIRM_MODAL, modal=True):\n'
                '            pass\n'
                '\n'
                '    modal_swap = getattr(shell, "_defer_modal_swap", None)\n'
                '    if dpg.does_item_exist(TAG_DELETE_CONFIRM_MODAL):\n'
                '        dpg.delete_item(TAG_DELETE_CONFIRM_MODAL)\n'
                '    _open()\n',
                encoding="utf-8",
            )
            sites, _ = discover(root=root / "zd_app", base=root)
            broken = seam_regressions(_group(sites), MODAL_SITE_REGISTRY)

        self.assertEqual(
            broken,
            [
                "zd_app/ui/screens/restore_points.py::_on_open_delete_confirm"
                "::restore_points_delete_confirm_modal"
            ],
            "a seam lookup that is never called must not classify as SEAM",
        )

    def test_a_superseded_seam_alias_fails_the_gate(self) -> None:
        """The mutation the hardened seam check still waved through.

        Requiring a real call was not enough while the alias bookkeeping was
        flow-insensitive: it collected every name ever bound to a seam lookup
        and then accepted a call through that name from anywhere. So fetching
        the seam, immediately rebinding the same name to something that runs
        the builder inline, and calling that classified as SEAM -- while the
        modal was in fact torn down and rebuilt in one pass.
        """

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_tree(root)
            (root / "zd_app" / "ui" / "screens" / "restore_points.py").write_text(
                'import dearpygui.dearpygui as dpg\n'
                '\n'
                'TAG_DELETE_CONFIRM_MODAL = "restore_points_delete_confirm_modal"\n'
                '\n'
                '\n'
                'def _on_open_delete_confirm(shell, rp_id, title):\n'
                '    def _open():\n'
                '        if dpg.does_item_exist(TAG_DELETE_CONFIRM_MODAL):\n'
                '            dpg.delete_item(TAG_DELETE_CONFIRM_MODAL)\n'
                '        with dpg.window(tag=TAG_DELETE_CONFIRM_MODAL, modal=True):\n'
                '            pass\n'
                '\n'
                '    modal_swap = getattr(shell, "_defer_modal_swap", None)\n'
                '    modal_swap = lambda fn, **kwargs: fn()\n'
                '    modal_swap(_open, delete_tags=(TAG_DELETE_CONFIRM_MODAL,))\n',
                encoding="utf-8",
            )
            sites, _ = discover(root=root / "zd_app", base=root)
            broken = seam_regressions(_group(sites), MODAL_SITE_REGISTRY)

        self.assertEqual(
            broken,
            [
                "zd_app/ui/screens/restore_points.py::_on_open_delete_confirm"
                "::restore_points_delete_confirm_modal"
            ],
            "a seam lookup superseded before the call must not classify as SEAM",
        )

    def test_a_seam_alias_bound_only_in_dead_code_fails_the_gate(self) -> None:
        """The same hole reached through a branch that cannot execute.

        ``if False:`` is folded away by the compiler, so the lookup inside it
        never runs and the name the call uses is whatever the live code bound.
        The old alias set did not care where the binding sat.
        """

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_tree(root)
            (root / "zd_app" / "ui" / "screens" / "restore_points.py").write_text(
                'import dearpygui.dearpygui as dpg\n'
                '\n'
                'TAG_DELETE_CONFIRM_MODAL = "restore_points_delete_confirm_modal"\n'
                '\n'
                '\n'
                'def _on_open_delete_confirm(shell, rp_id, title):\n'
                '    def _open():\n'
                '        if dpg.does_item_exist(TAG_DELETE_CONFIRM_MODAL):\n'
                '            dpg.delete_item(TAG_DELETE_CONFIRM_MODAL)\n'
                '        with dpg.window(tag=TAG_DELETE_CONFIRM_MODAL, modal=True):\n'
                '            pass\n'
                '\n'
                '    if False:\n'
                '        modal_swap = getattr(shell, "_defer_modal_swap", None)\n'
                '    modal_swap = lambda fn, **kwargs: fn()\n'
                '    modal_swap(_open, delete_tags=(TAG_DELETE_CONFIRM_MODAL,))\n',
                encoding="utf-8",
            )
            sites, _ = discover(root=root / "zd_app", base=root)
            broken = seam_regressions(_group(sites), MODAL_SITE_REGISTRY)

        self.assertEqual(
            broken,
            [
                "zd_app/ui/screens/restore_points.py::_on_open_delete_confirm"
                "::restore_points_delete_confirm_modal"
            ],
            "a seam lookup that only executes in dead code must not classify as SEAM",
        )

    def test_a_seam_call_with_no_builder_argument_fails_the_gate(self) -> None:
        """Calling the seam is not enough; it has to be given the builder.

        ``modal_swap()`` with no ``open_fn`` cannot be what opens the modal, so
        an inline create beside it is still the uncured shape.
        """

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_tree(root)
            (root / "zd_app" / "ui" / "screens" / "restore_points.py").write_text(
                'import dearpygui.dearpygui as dpg\n'
                '\n'
                'TAG_DELETE_CONFIRM_MODAL = "restore_points_delete_confirm_modal"\n'
                '\n'
                '\n'
                'def _on_open_delete_confirm(shell, rp_id, title):\n'
                '    modal_swap = getattr(shell, "_defer_modal_swap", None)\n'
                '    modal_swap(delete_tags=(TAG_DELETE_CONFIRM_MODAL,))\n'
                '    if dpg.does_item_exist(TAG_DELETE_CONFIRM_MODAL):\n'
                '        dpg.delete_item(TAG_DELETE_CONFIRM_MODAL)\n'
                '    with dpg.window(tag=TAG_DELETE_CONFIRM_MODAL, modal=True):\n'
                '        pass\n',
                encoding="utf-8",
            )
            sites, _ = discover(root=root / "zd_app", base=root)
            broken = seam_regressions(_group(sites), MODAL_SITE_REGISTRY)

        self.assertEqual(
            broken,
            [
                "zd_app/ui/screens/restore_points.py::_on_open_delete_confirm"
                "::restore_points_delete_confirm_modal"
            ],
        )

    def test_the_unmutated_miniature_is_clean(self) -> None:
        """Control: the seam predicate is not simply always failing."""

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_tree(root)
            sites, _ = discover(root=root / "zd_app", base=root)
            broken = seam_regressions(_group(sites), MODAL_SITE_REGISTRY)

        self.assertEqual(broken, [])


class SeamDetectorCoverageTests(unittest.TestCase):
    """The other half of the hardened detector: it must not UNDER-count.

    Requiring a real call is only safe if the guarded-``getattr`` form still
    resolves, because that is how every screen-module hop reaches the seam (so
    the mocked suite can pass a fake shell with no seam on it). A detector that
    only understood ``self._defer_modal_swap(...)`` would report all of these
    as inline and fail the whole gate -- which is a mistake worth pinning
    rather than re-deriving.
    """

    GETATTR_HOPS = (
        (MODULES, "_open_assign_modal"),
        (MODULES, "_open_notes_modal"),
        (MODULES, "_open_export_modal"),
        (MODULES, "_open_zip_preview"),
        (WEAR_LEDGER, "_open_note_modal"),
        (SAFE_IMPORT, "open_file_select"),
        (RESTORE_POINTS, "_on_open_delete_confirm"),
        (ABOUT, "_open_license_view"),
    )

    def test_every_getattr_routed_hop_is_recognised(self) -> None:
        for relpath, qualname in self.GETATTR_HOPS:
            with self.subTest(hop=f"{relpath}::{qualname}"):
                self.assertIs(
                    function_uses_seam(relpath, qualname),
                    True,
                    f"{relpath}::{qualname} reaches the seam through "
                    "getattr(shell, ...) and must still classify as SEAM",
                )

    def test_an_attribute_call_hop_is_recognised(self) -> None:
        # The in-shell shape, so the two forms are pinned side by side.
        self.assertIs(
            function_uses_seam(APP_SHELL, "AppShell._open_save_as_modal"), True
        )


if __name__ == "__main__":
    unittest.main()
