r"""Secondary HID-interface probe (used only by the opt-in StandaloneTriggerService).

Runs the public identify probe, closes the public handle, then polls for the
controller's secondary HID path and tries a read/write open to confirm
reachability:

1. Reproduce the public `413D:2104` identify probe with the same
   `DeviceIoControl` shape.
2. Close the public handle immediately.
3. Poll `filter_hid_paths(0x20BC, 0x5080)` at a tight interval.
4. If the secondary path becomes visible, test read/write open reachability.
5. If that succeeds, optionally watch for an unsolicited baseline read-back.

State boundary:

- a successful read/write open means `reachable`, not automatically `ready`
- `ready` only means this process observed an unsolicited baseline read-back
- `verified` is out of scope here: this module does not send config packets

Examples:
    python -m zd_app.protocol.trigger_interface
    python -m zd_app.protocol.trigger_interface --send
"""

from __future__ import annotations

import argparse
import ctypes as c
import json
import time
from ctypes import wintypes as w
from dataclasses import asdict, dataclass, replace
from pathlib import Path

try:
    import hid
except ModuleNotFoundError:  # pragma: no cover - runtime-only dependency on Windows
    hid = None

try:
    from zd_app.protocol.hid_transport import (
        FILE_SHARE_READ,
        FILE_SHARE_WRITE,
        GENERIC_READ,
        GENERIC_WRITE,
        INVALID_HANDLE_VALUE,
        OPEN_EXISTING,
        PUBLIC_IDENTIFY_OPEN_PROFILE,
        RETRYABLE_OPEN_ERRORS,
        filter_hid_paths,
        known_hidden_paths_from_logs,
        try_open_hid_path,
    )
except ModuleNotFoundError:  # pragma: no cover - fallback for direct script usage
    from hid_transport import (
        FILE_SHARE_READ,
        FILE_SHARE_WRITE,
        GENERIC_READ,
        GENERIC_WRITE,
        INVALID_HANDLE_VALUE,
        OPEN_EXISTING,
        PUBLIC_IDENTIFY_OPEN_PROFILE,
        RETRYABLE_OPEN_ERRORS,
        filter_hid_paths,
        known_hidden_paths_from_logs,
        try_open_hid_path,
    )


__all__ = [
    "BASELINE_READY_PREFIX",
    "HIDDEN_PRODUCT_ID",
    "HIDDEN_VENDOR_ID",
    "HiddenOpenResult",
    "HiddenReadObservation",
    "OPEN_MODES",
    "PUBLIC_IDENTIFY_EXPECTED_HEX",
    "PUBLIC_IDENTIFY_IOCTL",
    "PUBLIC_IDENTIFY_OUT_LEN",
    "PUBLIC_IDENTIFY_PROBE_BACKOFF_MS_SCHEDULE",
    "PUBLIC_IDENTIFY_PROBE_MAX_ATTEMPTS",
    "PUBLIC_PRODUCT_ID",
    "PUBLIC_VENDOR_ID",
    "PublicProbeResult",
    "TriggerExperimentResult",
    "decode_hid_path",
    "first_read_write_path",
    "main",
    "observe_hidden_reads",
    "ordered_public_paths",
    "parse_args",
    "poll_hidden_paths",
    "preferred_public_path",
    "print_dry_run",
    "print_result",
    "public_hid_paths",
    "public_open_attempts",
    "run_public_identify_probe",
    "run_public_identify_probe_candidates",
    "run_public_identify_probe_candidates_with_retry",
    "summarize_experiment",
    "write_json",
]


PUBLIC_VENDOR_ID = 0x413D
PUBLIC_PRODUCT_ID = 0x2104
HIDDEN_VENDOR_ID = 0x20BC
HIDDEN_PRODUCT_ID = 0x5080

PUBLIC_IDENTIFY_IOCTL = 2147508224
PUBLIC_IDENTIFY_EXPECTED_HEX = "03010100000000003d410421"
PUBLIC_IDENTIFY_OUT_LEN = 12

BASELINE_READY_PREFIX = "3055aad00f"
OPEN_MODES = {
    "metadata_only": 0,
    "read_write": GENERIC_READ | GENERIC_WRITE,
    "read_only": GENERIC_READ,
    "write_only": GENERIC_WRITE,
}

kernel32 = c.windll.kernel32
CreateFileW = kernel32.CreateFileW
CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
CreateFileW.restype = w.HANDLE
DeviceIoControl = kernel32.DeviceIoControl
DeviceIoControl.argtypes = [
    w.HANDLE,
    w.DWORD,
    c.c_void_p,
    w.DWORD,
    c.c_void_p,
    w.DWORD,
    c.POINTER(w.DWORD),
    c.c_void_p,
]
DeviceIoControl.restype = w.BOOL
CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [w.HANDLE]
CloseHandle.restype = w.BOOL


@dataclass(frozen=True)
class PublicProbeResult:
    path: str | None
    attempted: bool
    ok: bool
    last_error: int
    access_mode: str | None
    out_hex: str
    matched_expected: bool
    # Retry-ladder telemetry for the public-identify probe. All
    # default to the no-retry shape so existing callers that construct
    # PublicProbeResult without these fields produce consistent results
    # and existing JSON consumers tolerate the three additional keys.
    retries_fired: int = 0
    retries_converted: int = 0
    retry_errors_observed: tuple[int, ...] = ()


@dataclass(frozen=True)
class HiddenOpenResult:
    path: str | None
    visible: bool
    open_ok: bool
    last_error: int
    visible_after_ms: int | None


@dataclass(frozen=True)
class HiddenReadObservation:
    opened_with_hidapi: bool
    observed_frame_count: int
    observed_frames: tuple[str, ...]
    ready: bool
    message: str


@dataclass(frozen=True)
class TriggerExperimentResult:
    send_attempted: bool
    public_candidates: tuple[str, ...]
    public_probe: PublicProbeResult
    hidden_visible_paths_after_trigger: tuple[str, ...]
    hidden_open: HiddenOpenResult
    hidden_read: HiddenReadObservation
    transport_state: str
    state_ladder: str
    summary: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trigger the public identify probe and watch for hidden-interface visibility."
    )
    parser.add_argument("--send", action="store_true", help="Actually run the public identify probe.")
    parser.add_argument(
        "--public-open-mode",
        choices=["auto", *OPEN_MODES],
        default="read_write",
        help="Desired-access strategy for the public CreateFileW probe.",
    )
    parser.add_argument(
        "--share-mode",
        type=_parse_int,
        default=FILE_SHARE_READ | FILE_SHARE_WRITE,
        help="CreateFileW share mode for both public and hidden open probes.",
    )
    parser.add_argument(
        "--creation-disposition",
        type=_parse_int,
        default=OPEN_EXISTING,
        help="CreateFileW creation disposition for both public and hidden open probes.",
    )
    parser.add_argument(
        "--flags-and-attributes",
        type=_parse_int,
        default=PUBLIC_IDENTIFY_OPEN_PROFILE.flags_and_attributes,
        help="CreateFileW flags and attributes for both public and hidden open probes.",
    )
    parser.add_argument(
        "--watch-seconds",
        type=float,
        default=2.0,
        help="How long to poll for hidden 20BC:5080 visibility after the public trigger.",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=10,
        help="Polling interval for hidden interface visibility after the trigger.",
    )
    parser.add_argument(
        "--read-window-ms",
        type=int,
        default=7000,
        help="How long to watch for unsolicited baseline traffic after a hidden read/write open succeeds.",
    )
    parser.add_argument(
        "--public-path",
        help="Optional explicit public 413D:2104 path override.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Optional JSON output path for the final result.",
    )
    return parser.parse_args(argv)


def _parse_int(value: str) -> int:
    return int(value, 0)


def decode_hid_path(value: object) -> str | None:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", errors="ignore")
    if isinstance(value, str):
        return value
    return None


def public_hid_paths() -> list[str]:
    matches: list[str] = []
    seen: set[str] = set()

    if hid is not None:
        for raw in hid.enumerate(PUBLIC_VENDOR_ID, PUBLIC_PRODUCT_ID):
            path = decode_hid_path(raw.get("path"))
            if not path or path in seen:
                continue
            seen.add(path)
            matches.append(path)

    for path in filter_hid_paths(PUBLIC_VENDOR_ID, PUBLIC_PRODUCT_ID):
        if path in seen:
            continue
        seen.add(path)
        matches.append(path)

    for path in known_hidden_paths_from_logs(vendor_id=PUBLIC_VENDOR_ID, product_id=PUBLIC_PRODUCT_ID):
        if path in seen:
            continue
        seen.add(path)
        matches.append(path)

    return matches


def ordered_public_paths(paths: list[str]) -> list[str]:
    def sort_key(path: str) -> tuple[int, int, int, str]:
        lower = path.lower()
        return (
            0 if "mi_00" in lower else 1,
            0 if lower.startswith("\\\\?\\usb#") else 1,
            0 if "vid_413d" in lower and "pid_2104" in lower else 1,
            len(path),
            lower,
        )

    return sorted(paths, key=sort_key)


def preferred_public_path(paths: list[str]) -> str | None:
    ordered = ordered_public_paths(paths)
    return ordered[0] if ordered else None


def public_open_attempts(mode_name: str) -> tuple[tuple[str, int], ...]:
    if mode_name == "auto":
        return (
            ("metadata_only", OPEN_MODES["metadata_only"]),
            ("read_write", OPEN_MODES["read_write"]),
            ("read_only", OPEN_MODES["read_only"]),
            ("write_only", OPEN_MODES["write_only"]),
        )
    return ((mode_name, OPEN_MODES[mode_name]),)


def _open_public_handle_once(
    path: str,
    desired_access: int,
    share_mode: int,
    creation_disposition: int,
    flags_and_attributes: int,
) -> tuple[int | None, int]:
    handle = CreateFileW(
        path,
        desired_access,
        share_mode,
        None,
        creation_disposition,
        flags_and_attributes,
        None,
    )
    if handle != INVALID_HANDLE_VALUE:
        return handle, 0
    return None, kernel32.GetLastError()


def run_public_identify_probe(
    path: str | None,
    mode_name: str,
    share_mode: int,
    creation_disposition: int,
    flags_and_attributes: int,
) -> PublicProbeResult:
    if not path:
        return PublicProbeResult(
            path=None,
            attempted=False,
            ok=False,
            last_error=0,
            access_mode=None,
            out_hex="",
            matched_expected=False,
        )

    best_failure: PublicProbeResult | None = None
    for access_mode, desired_access in public_open_attempts(mode_name):
        handle, last_error = _open_public_handle_once(
            path,
            desired_access=desired_access,
            share_mode=share_mode,
            creation_disposition=creation_disposition,
            flags_and_attributes=flags_and_attributes,
        )
        if handle is None:
            best_failure = PublicProbeResult(
                path=path,
                attempted=True,
                ok=False,
                last_error=last_error,
                access_mode=access_mode,
                out_hex="",
                matched_expected=False,
            )
            continue

        buffer = c.create_string_buffer(PUBLIC_IDENTIFY_OUT_LEN)
        bytes_returned = w.DWORD()
        try:
            ok = bool(
                DeviceIoControl(
                    handle,
                    PUBLIC_IDENTIFY_IOCTL,
                    None,
                    0,
                    buffer,
                    PUBLIC_IDENTIFY_OUT_LEN,
                    c.byref(bytes_returned),
                    None,
                )
            )
            out_hex = bytes(buffer[:PUBLIC_IDENTIFY_OUT_LEN]).hex()
            last_error = 0 if ok else kernel32.GetLastError()
            result = PublicProbeResult(
                path=path,
                attempted=True,
                ok=ok,
                last_error=last_error,
                access_mode=access_mode,
                out_hex=out_hex,
                matched_expected=out_hex == PUBLIC_IDENTIFY_EXPECTED_HEX,
            )
        finally:
            CloseHandle(handle)

        if result.ok and result.matched_expected:
            return result
        best_failure = result

    return best_failure or PublicProbeResult(
        path=path,
        attempted=True,
        ok=False,
        last_error=0,
        access_mode=None,
        out_hex="",
        matched_expected=False,
    )


def poll_hidden_paths(timeout_s: float, poll_interval_ms: int) -> tuple[list[str], int | None]:
    deadline = time.monotonic() + max(timeout_s, 0.0)
    poll_interval_s = max(poll_interval_ms, 0) / 1000.0
    first_seen_ms: int | None = None
    start = time.monotonic()
    while True:
        paths = filter_hid_paths(HIDDEN_VENDOR_ID, HIDDEN_PRODUCT_ID)
        if paths:
            if first_seen_ms is None:
                first_seen_ms = round((time.monotonic() - start) * 1000)
            return paths, first_seen_ms
        if time.monotonic() >= deadline:
            return [], first_seen_ms
        time.sleep(poll_interval_s)


def first_read_write_path(
    paths: list[str],
    share_mode: int,
    creation_disposition: int,
    flags_and_attributes: int,
) -> HiddenOpenResult:
    if not paths:
        return HiddenOpenResult(
            path=None,
            visible=False,
            open_ok=False,
            last_error=0,
            visible_after_ms=None,
        )

    last_error = 0
    for path in paths:
        result = try_open_hid_path(
            path,
            GENERIC_READ | GENERIC_WRITE,
            share_mode=share_mode,
            creation_disposition=creation_disposition,
            flags_and_attributes=flags_and_attributes,
        )
        if result.get("ok"):
            return HiddenOpenResult(
                path=path,
                visible=True,
                open_ok=True,
                last_error=0,
                visible_after_ms=None,
            )
        last_error = int(result.get("last_error") or 0)

    return HiddenOpenResult(
        path=paths[0],
        visible=True,
        open_ok=False,
        last_error=last_error,
        visible_after_ms=None,
        )


def run_public_identify_probe_candidates(
    paths: list[str],
    mode_name: str,
    share_mode: int,
    creation_disposition: int,
    flags_and_attributes: int,
) -> PublicProbeResult:
    preferred_path = preferred_public_path(paths)
    if preferred_path:
        return run_public_identify_probe(
            preferred_path,
            mode_name=mode_name,
            share_mode=share_mode,
            creation_disposition=creation_disposition,
            flags_and_attributes=flags_and_attributes,
        )
    return PublicProbeResult(
        path=None,
        attempted=False,
        ok=False,
        last_error=0,
        access_mode=None,
        out_hex="",
        matched_expected=False,
    )


# Default retry ladder policy for the public identify probe. Three-attempt
# ladder with 100 ms + 250 ms time-anchored backoffs (deliberate divergence
# from Layer 1's event-anchored approach — the public-path failure mode is
# enumeration-visible-but-open-fails, which is not the transient-
# deregistration pattern an ARRIVAL event would catch). Worst-case added
# latency 350 ms on a genuinely failing probe; zero added latency on the
# common healthy-session path.
PUBLIC_IDENTIFY_PROBE_MAX_ATTEMPTS = 3
PUBLIC_IDENTIFY_PROBE_BACKOFF_MS_SCHEDULE: tuple[int, ...] = (100, 250)


def _run_public_identify_probe_with_retry(
    path: str | None,
    mode_name: str,
    share_mode: int,
    creation_disposition: int,
    flags_and_attributes: int,
    max_attempts: int = PUBLIC_IDENTIFY_PROBE_MAX_ATTEMPTS,
    backoff_ms_schedule: tuple[int, ...] = PUBLIC_IDENTIFY_PROBE_BACKOFF_MS_SCHEDULE,
    sleep_fn=None,
    probe_fn=None,
) -> PublicProbeResult:
    """Retry-aware wrapper over ``run_public_identify_probe``.

    Retries on ``RETRYABLE_OPEN_ERRORS`` membership only. Returns
    immediately on success (``ok=True and matched_expected=True``) or on
    any non-retryable ``last_error``. Populates the ``retries_fired`` /
    ``retries_converted`` / ``retry_errors_observed`` telemetry fields on
    the returned ``PublicProbeResult``. ``sleep_fn`` and ``probe_fn``
    are dependency-injection hooks for unit tests; when left as ``None``
    they resolve to ``time.sleep`` and ``run_public_identify_probe``
    respectively at call time (so ``mock.patch`` against those module
    attributes takes effect).

    ``retry_errors_observed`` records only the errors that *triggered* a
    retry — that is, failures on attempts that were followed by another
    attempt. The final attempt's error (if the ladder exhausts) is not
    counted because it did not trigger a retry.

    This wrapper is time-anchored (not event-anchored): it sleeps for
    ``backoff_ms_schedule[attempt_index - 1]`` before each retry attempt
    (100 ms before attempt 2, 250 ms before attempt 3, with the last
    schedule entry reused for any further attempts if ``max_attempts``
    is raised). This is a deliberate divergence from Layer 1's
    event-anchored retry.
    """
    # Resolve dependency-injected defaults at call time so
    # mock.patch against the module-level attributes takes effect.
    resolved_sleep_fn = sleep_fn if sleep_fn is not None else time.sleep
    resolved_probe_fn = probe_fn if probe_fn is not None else run_public_identify_probe

    errors_seen: list[int] = []
    last_result: PublicProbeResult | None = None

    if max_attempts < 1:
        max_attempts = 1

    for attempt_index in range(max_attempts):
        if attempt_index > 0:
            if backoff_ms_schedule:
                backoff_ms = backoff_ms_schedule[
                    min(attempt_index - 1, len(backoff_ms_schedule) - 1)
                ]
            else:
                backoff_ms = 0
            if backoff_ms > 0:
                resolved_sleep_fn(backoff_ms / 1000.0)

        result = resolved_probe_fn(
            path,
            mode_name,
            share_mode,
            creation_disposition,
            flags_and_attributes,
        )
        last_result = result

        if result.ok and result.matched_expected:
            return replace(
                result,
                retries_fired=attempt_index,
                retries_converted=1 if attempt_index > 0 else 0,
                retry_errors_observed=tuple(errors_seen),
            )

        if result.last_error not in RETRYABLE_OPEN_ERRORS:
            return replace(
                result,
                retries_fired=attempt_index,
                retries_converted=0,
                retry_errors_observed=tuple(errors_seen),
            )

        # Only record this error if it will trigger a retry (i.e., there
        # is at least one more attempt in the ladder).
        if attempt_index < max_attempts - 1:
            errors_seen.append(result.last_error)

    # Exhausted all attempts on retryable errors. last_result is
    # guaranteed non-None because max_attempts >= 1.
    assert last_result is not None
    return replace(
        last_result,
        retries_fired=max_attempts - 1,
        retries_converted=0,
        retry_errors_observed=tuple(errors_seen),
    )


def run_public_identify_probe_candidates_with_retry(
    paths: list[str],
    mode_name: str,
    share_mode: int,
    creation_disposition: int,
    flags_and_attributes: int,
    max_attempts: int = PUBLIC_IDENTIFY_PROBE_MAX_ATTEMPTS,
    backoff_ms_schedule: tuple[int, ...] = PUBLIC_IDENTIFY_PROBE_BACKOFF_MS_SCHEDULE,
    sleep_fn=None,
    probe_fn=None,
) -> PublicProbeResult:
    """Candidate-list entry point that threads the retry ladder.

    Same selection rule as ``run_public_identify_probe_candidates``
    (picks the preferred path via ``preferred_public_path``), then wraps
    the per-path probe in the retry ladder. When no candidate path is
    supplied, returns the same no-path ``PublicProbeResult`` shape as
    the non-retry variant with the retry telemetry fields zeroed out.
    """
    preferred_path = preferred_public_path(paths)
    if preferred_path:
        return _run_public_identify_probe_with_retry(
            preferred_path,
            mode_name=mode_name,
            share_mode=share_mode,
            creation_disposition=creation_disposition,
            flags_and_attributes=flags_and_attributes,
            max_attempts=max_attempts,
            backoff_ms_schedule=backoff_ms_schedule,
            sleep_fn=sleep_fn,
            probe_fn=probe_fn,
        )
    return PublicProbeResult(
        path=None,
        attempted=False,
        ok=False,
        last_error=0,
        access_mode=None,
        out_hex="",
        matched_expected=False,
    )


def observe_hidden_reads(path: str | None, read_window_ms: int) -> HiddenReadObservation:
    if not path:
        return HiddenReadObservation(
            opened_with_hidapi=False,
            observed_frame_count=0,
            observed_frames=(),
            ready=False,
            message="No hidden path was available to read.",
        )

    if hid is None:
        return HiddenReadObservation(
            opened_with_hidapi=False,
            observed_frame_count=0,
            observed_frames=(),
            ready=False,
            message="The hid package is unavailable, so hidden-path read watching could not run.",
        )

    device = hid.device()
    observed_frames: list[str] = []
    deadline = time.time() + max(read_window_ms, 0) / 1000.0
    try:
        device.open_path(path.encode("utf-8"))
        device.set_nonblocking(True)
        while time.time() < deadline:
            raw = device.read(64)
            if raw:
                frame_hex = bytes(raw).hex()
                observed_frames.append(frame_hex)
                if frame_hex.startswith(BASELINE_READY_PREFIX):
                    return HiddenReadObservation(
                        opened_with_hidapi=True,
                        observed_frame_count=len(observed_frames),
                        observed_frames=tuple(observed_frames[:12]),
                        ready=True,
                        message="Observed unsolicited baseline traffic on the hidden path.",
                    )
            time.sleep(0.02)
        return HiddenReadObservation(
            opened_with_hidapi=True,
            observed_frame_count=len(observed_frames),
            observed_frames=tuple(observed_frames[:12]),
            ready=False,
            message="No unsolicited baseline traffic was observed within the read window.",
        )
    except OSError:
        return HiddenReadObservation(
            opened_with_hidapi=False,
            observed_frame_count=0,
            observed_frames=(),
            ready=False,
            message="Read/write open succeeded through CreateFileW, but hidapi could not watch the hidden path.",
        )
    finally:
        try:
            device.close()
        except OSError:
            pass


def summarize_experiment(
    send_attempted: bool,
    public_candidates: list[str],
    public_probe: PublicProbeResult,
    hidden_visible_paths: list[str],
    hidden_open: HiddenOpenResult,
    hidden_read: HiddenReadObservation,
) -> TriggerExperimentResult:
    if not send_attempted:
        transport_state = "Dry Run"
        state_ladder = "candidate only"
        summary = (
            "Dry run only. No public trigger was sent, so hidden-interface visibility and reachability "
            "were not tested."
        )
    elif not public_probe.ok:
        transport_state = "Trigger Failed"
        state_ladder = "candidate only"
        summary = (
            "The public identify probe did not complete successfully, so the hidden-interface trigger "
            "hypothesis was not actually tested."
        )
    elif not hidden_open.visible:
        transport_state = "Candidate Only (No Hidden Visibility)"
        state_ladder = "candidate only"
        summary = (
            "The public identify probe completed, but no hidden 20BC:5080 path became visible through "
            "SetupAPI within the watch window."
        )
    elif not hidden_open.open_ok:
        transport_state = "Candidate Only (Visible, Not Reachable)"
        state_ladder = "candidate only"
        summary = (
            "The hidden path became visible after the public identify probe, but read/write open did not "
            "succeed from this process."
        )
    elif hidden_read.ready:
        transport_state = "Ready (Unsolicited Baseline Observed)"
        state_ladder = "candidate -> reachable -> ready"
        summary = (
            "The hidden path became visible, read/write open succeeded, and this process observed unsolicited "
            "baseline traffic on its own handle."
        )
    else:
        transport_state = "Reachable (Read/Write Open)"
        state_ladder = "candidate -> reachable"
        summary = (
            "The hidden path became visible and read/write open succeeded. Treat this as reachable only, "
            "not yet ready or verified."
        )

    return TriggerExperimentResult(
        send_attempted=send_attempted,
        public_candidates=tuple(public_candidates),
        public_probe=public_probe,
        hidden_visible_paths_after_trigger=tuple(hidden_visible_paths),
        hidden_open=hidden_open,
        hidden_read=hidden_read,
        transport_state=transport_state,
        state_ladder=state_ladder,
        summary=summary,
    )


def print_dry_run(public_candidates: list[str]) -> None:
    preferred = preferred_public_path(public_candidates)
    print("Trigger Hidden Interface")
    print()
    print("Dry run only. No packet was sent.")
    print(f"Preferred public path: {preferred or 'none'}")
    print(f"Public candidate count: {len(public_candidates)}")
    for path in public_candidates:
        print(f"  {path}")
    print()
    print(f"Public identify IOCTL: {PUBLIC_IDENTIFY_IOCTL}")
    print(f"Expected output hex: {PUBLIC_IDENTIFY_EXPECTED_HEX}")
    print("Public open mode choices: metadata_only, read_write, read_only, write_only")
    print(
        "Default public send shape: "
        f"{PUBLIC_IDENTIFY_OPEN_PROFILE.name} "
        f"(access=read_write share=0x{PUBLIC_IDENTIFY_OPEN_PROFILE.share_mode:x} "
        f"creation=0x{PUBLIC_IDENTIFY_OPEN_PROFILE.creation_disposition:x} "
        f"flags=0x{PUBLIC_IDENTIFY_OPEN_PROFILE.flags_and_attributes:x})"
    )
    print(
        "When run with --send, this script will issue the public identify probe, close the public handle, "
        "poll visible hidden paths, then test read/write reachability."
    )


def print_result(result: TriggerExperimentResult) -> None:
    print("Trigger Hidden Interface")
    print()
    print(f"Send attempted: {result.send_attempted}")
    print(f"Public candidates: {len(result.public_candidates)}")
    if result.public_probe.path:
        print(f"Public path: {result.public_probe.path}")
    print(f"Public probe ok: {result.public_probe.ok}")
    if result.public_probe.access_mode:
        print(f"Public access mode: {result.public_probe.access_mode}")
    if result.public_probe.out_hex:
        print(f"Public output hex: {result.public_probe.out_hex}")
        print(f"Matched expected identify hex: {result.public_probe.matched_expected}")
    if result.public_probe.last_error:
        print(f"Public last error: {result.public_probe.last_error}")
    print(f"Hidden visible paths after trigger: {len(result.hidden_visible_paths_after_trigger)}")
    for path in result.hidden_visible_paths_after_trigger:
        print(f"  {path}")
    if result.hidden_open.path:
        print(f"Hidden open candidate: {result.hidden_open.path}")
    print(f"Hidden read/write open ok: {result.hidden_open.open_ok}")
    if result.hidden_open.last_error:
        print(f"Hidden open last error: {result.hidden_open.last_error}")
    print(f"Hidden read watcher opened: {result.hidden_read.opened_with_hidapi}")
    print(f"Hidden observed frames: {result.hidden_read.observed_frame_count}")
    if result.hidden_read.observed_frames:
        print("Observed frame sample:")
        for frame_hex in result.hidden_read.observed_frames[:4]:
            print(f"  {frame_hex}")
    print(f"Transport state: {result.transport_state}")
    print(f"State ladder: {result.state_ladder}")
    print(f"Summary: {result.summary}")
    print(f"Read observation: {result.hidden_read.message}")


def write_json(path: Path, result: TriggerExperimentResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    public_candidates = public_hid_paths()
    if not args.send:
        print_dry_run(public_candidates)
        return 0

    if args.public_path:
        public_probe = run_public_identify_probe(
            args.public_path,
            mode_name=args.public_open_mode,
            share_mode=args.share_mode,
            creation_disposition=args.creation_disposition,
            flags_and_attributes=args.flags_and_attributes,
        )
    else:
        public_probe = run_public_identify_probe_candidates(
            public_candidates,
            mode_name=args.public_open_mode,
            share_mode=args.share_mode,
            creation_disposition=args.creation_disposition,
            flags_and_attributes=args.flags_and_attributes,
        )
    hidden_visible_paths, first_visible_ms = poll_hidden_paths(args.watch_seconds, args.poll_interval_ms)
    hidden_open = first_read_write_path(
        hidden_visible_paths,
        share_mode=args.share_mode,
        creation_disposition=args.creation_disposition,
        flags_and_attributes=args.flags_and_attributes,
    )
    hidden_open = HiddenOpenResult(
        path=hidden_open.path,
        visible=hidden_open.visible,
        open_ok=hidden_open.open_ok,
        last_error=hidden_open.last_error,
        visible_after_ms=first_visible_ms,
    )
    hidden_read = observe_hidden_reads(hidden_open.path if hidden_open.open_ok else None, args.read_window_ms)
    result = summarize_experiment(
        send_attempted=True,
        public_candidates=public_candidates,
        public_probe=public_probe,
        hidden_visible_paths=hidden_visible_paths,
        hidden_open=hidden_open,
        hidden_read=hidden_read,
    )
    print_result(result)
    if args.json_out:
        write_json(args.json_out.expanduser().resolve(), result)
    return 0 if result.transport_state != "Trigger Failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
