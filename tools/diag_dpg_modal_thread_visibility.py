"""Manual diagnostic: DPG modal teardown→create visibility matrix.

Background (Safe Import preview modal swap):
a pass that deletes a SHOWING modal window and then creates the next modal
yields a window that EXISTS (children intact, no exception, no DPG error
anywhere) but never becomes visible. The original repro matrix confounded
"callback thread" with "a modal was deleted in the same pass"; this tool
isolates every cell. Empirical law on dearpygui 1.x (2026-06-11 bench, this
file):

  - The thread does NOT matter.
  - A modal create is eaten whenever another modal was showing in the same
    pass (deleted, hidden, or still up) without a rendered frame between
    the teardown and the create.
  - One rendered frame between teardown (delete OR hide) and create makes
    the new modal visible.
  - Two modals can never SHOW stacked: the second never renders. (DPG's
    modal slot is single-occupancy per rendered frame.)

AppShell's ``_defer_modal_swap`` seam encodes the cure: teardown pass,
rendered frame, create pass. Re-run this matrix after any dearpygui
upgrade to confirm the seam is still load-bearing (or has become
unnecessary).

Phases run EACH IN ITS OWN SUBPROCESS — a poisoned pass wedges the
context's modal state beyond the item itself, so in-process phases would
contaminate each other (empirically confirmed 2026-06-11).

Manual tool only, NOT part of the test suite: the constraint lives in real
DPG's threading internals, needs a real viewport + render loop, and the
suite's headless/patched DPG cannot host or reproduce it.

Usage (run with a Python that has dearpygui installed):
    python tools/diag_dpg_modal_thread_visibility.py             # full matrix
    python tools/diag_dpg_modal_thread_visibility.py --phase main-swap-split
"""

from __future__ import annotations

import subprocess
import sys
import threading

MODAL_A = "diag_modal_a"
MODAL_B = "diag_modal_b"

WARMUP_FRAMES = 30   # let modal A settle on screen first
PHASE_FRAMES = 150   # per-phase drain-pass budget after warmup
SAMPLE_EVERY = 30

RESULT_PREFIX = "PHASE_RESULT"

# name -> (expected_b_visible_mid, expected_a_visible_end_or_None, seam_relevant)
#   B is sampled at frame 90 (well after every phase's create pass);
#   A is sampled at the final frame for the roundtrip phases.
PHASES: dict[str, tuple[bool, bool | None, bool]] = {
    # The live shape: callback thread deletes A + creates B, one pass.
    "thread-same-pass": (False, None, True),
    # The naive "defer to the render thread" fix shape — equally poisoned.
    "main-same-pass": (False, None, False),
    # The production seam shape: teardown pass, rendered frame, create pass.
    "main-swap-split": (True, None, True),
    # Hide instead of delete, same pass — still poisoned.
    "main-hide-same-pass": (False, None, False),
    # Hide, rendered frame, create — the preview→confirm hop shape (the
    # hidden preview keeps its widget state, e.g. the typed profile name).
    "main-hide-split": (True, None, True),
    # Stacked modals (A kept showing, B created over it): B never renders.
    "main-stacked": (False, None, True),
    # Control: a plain create with no other modal ever shown.
    "main-create-only": (True, None, True),
    # Reference: the canonical DPG callback-side idiom (split_frame).
    "thread-split-frame": (True, None, False),
    # Full preview→confirm→cancel roundtrip: hide A, +1 create B,
    # +60 delete B, +1 re-show A. Verdict needs A visible again at the end.
    "main-hide-roundtrip": (True, True, True),
    # Fallback shape if hiding were unavailable: delete A, +1 create B,
    # +60 delete B, +1 re-create A.
    "main-delete-recreate-roundtrip": (True, True, False),
}


def _run_phase_in_this_process(phase: str) -> None:
    """One phase against a fresh DPG context; prints a PHASE_RESULT line."""

    import dearpygui.dearpygui as dpg

    def create_a() -> None:
        with dpg.window(tag=MODAL_A, label="Modal A (start)", modal=True,
                        no_resize=True, width=420, height=160):
            dpg.add_text("Diagnostic modal A.")
            dpg.add_button(label="(inert)")

    def delete_a() -> None:
        if dpg.does_item_exist(MODAL_A):
            dpg.delete_item(MODAL_A)

    def hide_a() -> None:
        dpg.configure_item(MODAL_A, show=False)

    def show_a() -> None:
        dpg.configure_item(MODAL_A, show=True)

    def create_b() -> None:
        with dpg.window(tag=MODAL_B, label=f"Modal B via {phase}", modal=True,
                        no_resize=True, width=460, height=200):
            dpg.add_text(f"Diagnostic modal B — created via: {phase}")
            dpg.add_text("If you can read this on screen, the pass was safe.")
            dpg.add_button(label="(inert)")

    def delete_b() -> None:
        if dpg.does_item_exist(MODAL_B):
            dpg.delete_item(MODAL_B)

    # drain-pass offset (0 = first pass after warmup) -> main-thread actions.
    main_actions: dict[int, list] = {}
    thread_target = None
    with_modal_a = phase != "main-create-only"

    if phase == "thread-same-pass":
        thread_target = lambda: (delete_a(), create_b())  # noqa: E731
    elif phase == "thread-split-frame":
        def thread_target() -> None:
            delete_a()
            dpg.split_frame()
            create_b()
    elif phase == "main-same-pass":
        main_actions[0] = [delete_a, create_b]
    elif phase == "main-swap-split":
        main_actions[0] = [delete_a]
        main_actions[1] = [create_b]
    elif phase == "main-hide-same-pass":
        main_actions[0] = [hide_a, create_b]
    elif phase == "main-hide-split":
        main_actions[0] = [hide_a]
        main_actions[1] = [create_b]
    elif phase == "main-stacked":
        main_actions[0] = [create_b]  # A stays showing underneath
    elif phase == "main-create-only":
        main_actions[0] = [create_b]
    elif phase == "main-hide-roundtrip":
        main_actions[0] = [hide_a]
        main_actions[1] = [create_b]
        main_actions[100] = [delete_b]
        main_actions[101] = [show_a]
    elif phase == "main-delete-recreate-roundtrip":
        main_actions[0] = [delete_a]
        main_actions[1] = [create_b]
        main_actions[100] = [delete_b]
        main_actions[101] = [create_a]
    else:
        raise SystemExit(f"unknown phase {phase!r}")

    dpg.create_context()
    dpg.create_viewport(
        title=f"DPG modal diagnostic — {phase}", width=900, height=600
    )
    dpg.setup_dearpygui()
    if with_modal_a:
        create_a()
    dpg.show_viewport()

    def visible(tag: str):
        if not dpg.does_item_exist(tag):
            return None
        return bool(dpg.get_item_state(tag).get("visible"))

    print(f"--- phase: {phase} ---", flush=True)
    for _ in range(WARMUP_FRAMES):
        dpg.render_dearpygui_frame()
    if thread_target is not None:
        threading.Thread(
            target=thread_target, name=f"fake-dpg-callback-{phase}", daemon=True
        ).start()

    b_visible_mid = None
    for frame in range(PHASE_FRAMES):
        # The drain point: top of the loop pass, before this frame renders —
        # where AppShell._tick runs _drain_deferred_ui_calls.
        for action in main_actions.pop(frame, []):
            action()
        dpg.render_dearpygui_frame()
        if (frame + 1) % SAMPLE_EVERY == 0:
            print(f"  {phase} frame+{frame + 1}: B visible={visible(MODAL_B)} "
                  f"| A visible={visible(MODAL_A)}", flush=True)
        if frame + 1 == 90:
            b_visible_mid = bool(visible(MODAL_B))
    a_visible_end = visible(MODAL_A)

    dpg.destroy_context()
    print(f"{RESULT_PREFIX} {phase} b_mid={b_visible_mid} a_end={a_visible_end}",
          flush=True)


def _run_phase_subprocess(phase: str) -> tuple[bool, bool | None]:
    """Run one phase isolated in a child process; relay output; parse result."""

    proc = subprocess.run(
        [sys.executable, __file__, "--phase", phase],
        capture_output=True,
        text=True,
        timeout=120,
    )
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"{phase} phase subprocess failed ({proc.returncode})")
    for line in proc.stdout.splitlines():
        if line.startswith(f"{RESULT_PREFIX} {phase} "):
            fields = dict(
                part.split("=", 1) for part in line.split()[2:]
            )
            b_mid = fields.get("b_mid") == "True"
            a_end = None if fields.get("a_end") == "None" else fields.get("a_end") == "True"
            return b_mid, a_end
    raise RuntimeError(f"{phase} phase printed no {RESULT_PREFIX} line")


def main() -> None:
    if "--phase" in sys.argv:
        _run_phase_in_this_process(sys.argv[sys.argv.index("--phase") + 1])
        return

    seam_ok = True
    rows = []
    for phase, (want_b, want_a, seam_relevant) in PHASES.items():
        b_mid, a_end = _run_phase_subprocess(phase)
        ok = b_mid == want_b and (want_a is None or a_end == want_a)
        if seam_relevant and not ok:
            seam_ok = False
        a_part = "" if want_a is None else f" a_end={a_end} (want {want_a})"
        tag = ("seam" if seam_relevant else "info") + (": OK" if ok else ": VIOLATED")
        rows.append(f"  {phase:<32} b={b_mid!s:<5} (want {want_b!s:<5}){a_part}  [{tag}]")

    print("\n=== matrix ===")
    for row in rows:
        print(row)
    if seam_ok:
        print("Seam-relevant cells hold; the AppShell _defer_modal_swap design "
              "is sound on this dearpygui build.")
    else:
        print("Seam-relevant cells VIOLATED — re-bench before trusting any "
              "modal flow on this dearpygui build.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
