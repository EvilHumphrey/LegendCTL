"""Focused Phase-2 coverage for profile-Apply read-back surfacing."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from tests.r2_shell_test_helpers import empty_snapshot, make_shell
from zd_app import i18n
from zd_app.services._log_entry import ComposedLogEntry, render_log_message
from zd_app.services.settings_apply_coordinator import ApplyFailure, ApplyResult
from zd_app.services.write_verification import (
    all_unverified_outcomes,
    attempted_fields_from_snapshot,
    build_field_outcomes,
    summarize_field_outcomes,
)
from zd_app.storage.restore_point_models import RestoreFieldOutcome
from zd_app.ui.app_shell import AppShell, _profile_apply_readback_field_label


def _outcome(
    field_name: str,
    *,
    verify_matched: bool | None,
    write_succeeded: bool = True,
    write_error: str | None = None,
    expected_value: str | None = None,
    observed_value: str | None = None,
    verify_note: str | None = None,
) -> RestoreFieldOutcome:
    return RestoreFieldOutcome(
        field_name=field_name,
        write_succeeded=write_succeeded,
        write_error=write_error,
        verify_matched=verify_matched,
        verify_note=verify_note,
        expected_value=expected_value,
        observed_value=observed_value,
    )


def _verification(*outcomes: RestoreFieldOutcome):
    return summarize_field_outcomes(outcomes)


class _DpgTextRecorder:
    """Small Restore-style DPG recorder for the deterministic detail renderer."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self._patch = None

    def __enter__(self) -> "_DpgTextRecorder":
        def add_text(*args, **kwargs):
            if args:
                self.texts.append(str(args[0]))
            elif "default_value" in kwargs:
                self.texts.append(str(kwargs["default_value"]))

        self._patch = patch.multiple(
            "zd_app.ui.app_shell.dpg",
            add_text=add_text,
            add_spacer=lambda *_args, **_kwargs: None,
            bind_item_theme=lambda *_args, **_kwargs: None,
            child_window=lambda *_args, **_kwargs: nullcontext("child"),
            does_item_exist=lambda *_args, **_kwargs: False,
        )
        self._patch.__enter__()
        return self

    def __exit__(self, *exc_info) -> bool:
        if self._patch is not None:
            self._patch.__exit__(*exc_info)
        return False


class ProfileApplyReadbackUiTests(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_locale("en")

    def test_disclosure_uses_sweep_priority_and_legacy_fallback(self) -> None:
        shell = make_shell()
        cases = (
            (
                "mismatch wins even when old writes are empty",
                ApplyResult(
                    readback_verification=_verification(
                        _outcome("step_size", verify_matched=False),
                        _outcome(
                            "back_paddle_bindings",
                            verify_matched=None,
                            verify_note="write-only",
                        ),
                    ),
                    unverified_writes=(),
                ),
                "apply.result.readback_mismatch",
                1,
                (),
            ),
            (
                "could-not-verify without mismatch",
                ApplyResult(
                    readback_verification=_verification(
                        _outcome(
                            "back_paddle_bindings",
                            verify_matched=None,
                            verify_note="write-only",
                        ),
                    ),
                ),
                "apply.result.readback_unconfirmed",
                1,
                (),
            ),
            (
                "all-readable-fields-matched",
                ApplyResult(
                    readback_verification=_verification(
                        _outcome("step_size", verify_matched=True),
                    ),
                ),
                "apply.result.readback_all_verified",
                None,
                (),
            ),
            (
                "no-sweep-keeps-legacy-unverified-note",
                ApplyResult(unverified_writes=("step_size",)),
                None,
                None,
                ("apply.result.write_unverified",),
            ),
        )

        for name, result, expected_entry_key, expected_n, expected_keys in cases:
            with self.subTest(name=name):
                message = shell._with_sensitivity_downgrade_notice("base", result)
                self.assertIsInstance(message, ComposedLogEntry)
                self.assertEqual(message.note_keys, expected_keys)
                if expected_entry_key is None:
                    self.assertEqual(message.note_entries, ())
                    self.assertIn(i18n.t("apply.result.write_unverified"), render_log_message(message))
                    continue
                self.assertEqual(len(message.note_entries), 1)
                entry = message.note_entries[0]
                self.assertEqual(entry.key, expected_entry_key)
                self.assertEqual(entry.fmt_args, {} if expected_n is None else {"n": expected_n})
                if expected_n is None:
                    self.assertIn(i18n.t(expected_entry_key), render_log_message(message))
                else:
                    self.assertIn(
                        i18n.t(expected_entry_key, n=expected_n),
                        render_log_message(message),
                    )

    def test_whole_sweep_failure_synthesis_marks_all_could_not_verify(self) -> None:
        # Review blocker 2026-07-17: a verifier that raises before it can read
        # the controller must not leave readback_verification=None (silent clean
        # success). all_unverified_outcomes synthesizes could-not-verify for every
        # attempted field, and the footer's three-valued disclosure must then fire.
        sent = empty_snapshot(
            vibration=object(),
            deadzones=object(),
            trigger_left=object(),
        )
        attempted = attempted_fields_from_snapshot(sent)
        self.assertEqual(set(attempted), {"vibration", "deadzones", "trigger_left"})

        outcomes = all_unverified_outcomes(
            attempted,
            sent,
            ApplyResult(),  # no failed writes: writes succeeded, reads did not run
            reason="post-apply read-back sweep failed",
        )
        self.assertTrue(all(o.verify_matched is None for o in outcomes))
        self.assertTrue(all(o.write_succeeded for o in outcomes))
        self.assertTrue(
            all("post-apply read-back sweep failed" in o.verify_note for o in outcomes)
        )

        verification = summarize_field_outcomes(outcomes)
        self.assertEqual(verification.attempted, len(attempted))
        self.assertEqual(verification.could_not_verify, len(attempted))
        self.assertEqual(verification.verified_matched, 0)
        self.assertEqual(verification.mismatched, 0)
        self.assertEqual(verification.wrote_succeeded, len(attempted))

        # The footer note must disclose "could not verify N", not stay silent.
        shell = make_shell()
        message = shell._with_sensitivity_downgrade_notice(
            "base", ApplyResult(readback_verification=verification)
        )
        self.assertIsInstance(message, ComposedLogEntry)
        self.assertEqual(len(message.note_entries), 1)
        entry = message.note_entries[0]
        self.assertEqual(entry.key, "apply.result.readback_unconfirmed")
        self.assertEqual(entry.fmt_args, {"n": len(attempted)})
        self.assertIn(
            i18n.t("apply.result.readback_unconfirmed", n=len(attempted)),
            render_log_message(message),
        )

    def test_write_failed_field_excluded_from_could_not_verify_count(self) -> None:
        # Review fix 2026-07-17: a whole-sweep failure marks every
        # attempted field verify_matched=None, but a field whose WRITE failed was
        # not written. It must NOT be counted under could_not_verify, whose footer
        # copy says "{n} change(s) were written but couldn't be confirmed". The
        # per-field detail must still show the write failure.
        sent = empty_snapshot(vibration=object(), deadzones=object())
        attempted = attempted_fields_from_snapshot(sent)
        self.assertEqual(set(attempted), {"vibration", "deadzones"})

        apply_result = ApplyResult(
            total_attempted=2,
            succeeded=1,
            failed=[ApplyFailure("deadzones", "HID write rejected", True)],
        )
        outcomes = all_unverified_outcomes(
            attempted,
            sent,
            apply_result,
            reason="post-apply read-back sweep failed",
        )
        by_name = {o.field_name: o for o in outcomes}
        # Per-field: both present and unverified, but write status is preserved.
        self.assertTrue(by_name["vibration"].write_succeeded)
        self.assertFalse(by_name["deadzones"].write_succeeded)
        self.assertIsNone(by_name["vibration"].verify_matched)
        self.assertIsNone(by_name["deadzones"].verify_matched)

        verification = summarize_field_outcomes(outcomes)
        self.assertEqual(verification.attempted, 2)
        self.assertEqual(verification.wrote_succeeded, 1)
        self.assertEqual(verification.write_failed, 1)
        self.assertEqual(verification.mismatched, 0)
        # THE FIX: only the written-but-unconfirmed field (vibration) counts.
        self.assertEqual(verification.could_not_verify, 1)

        # Footer note discloses "1 ... were written", never 2.
        shell = make_shell()
        message = shell._with_sensitivity_downgrade_notice(
            "base", ApplyResult(readback_verification=verification)
        )
        self.assertEqual(len(message.note_entries), 1)
        entry = message.note_entries[0]
        self.assertEqual(entry.key, "apply.result.readback_unconfirmed")
        self.assertEqual(entry.fmt_args, {"n": 1})

    def test_all_writes_failed_sweep_failure_does_not_claim_all_verified(self) -> None:
        # With could_not_verify excluding write-failed fields, an Apply whose
        # writes ALL failed reaches the note logic with could_not_verify=0,
        # mismatched=0, verified_matched=0. It must NOT fire
        # "All readable changes were confirmed by read-back."
        sent = empty_snapshot(vibration=object(), deadzones=object())
        attempted = attempted_fields_from_snapshot(sent)
        apply_result = ApplyResult(
            total_attempted=2,
            succeeded=0,
            failed=[
                ApplyFailure("vibration", "HID write rejected", True),
                ApplyFailure("deadzones", "HID write rejected", True),
            ],
        )
        verification = summarize_field_outcomes(
            all_unverified_outcomes(
                attempted, sent, apply_result, reason="post-apply read-back sweep failed"
            )
        )
        self.assertEqual(verification.attempted, 2)
        self.assertEqual(verification.write_failed, 2)
        self.assertEqual(verification.could_not_verify, 0)
        self.assertEqual(verification.verified_matched, 0)
        self.assertEqual(verification.mismatched, 0)

        shell = make_shell()
        message = shell._with_sensitivity_downgrade_notice(
            "base", ApplyResult(readback_verification=verification)
        )
        # No note is appended at all — the base message is returned unchanged
        # (not a ComposedLogEntry), so no false confirmation claim can surface.
        self.assertEqual(message, "base")
        self.assertNotIn(
            i18n.t("apply.result.readback_all_verified"),
            render_log_message(message),
        )

    def test_all_writes_failed_with_matching_readback_does_not_claim_all_verified(
        self,
    ) -> None:
        # Review round-3 fix 2026-07-17: verification is computed
        # independently of write success, so an Apply whose writes ALL failed can
        # still read back PRE-EXISTING values that coincidentally equal the
        # requested ones (verify_matched=True on write-failed fields). That must
        # NOT emit "All readable changes were confirmed by read-back" right after
        # the all-failed partial result — any write failure gates the note off.
        vib, dz = object(), object()
        sent = empty_snapshot(vibration=vib, deadzones=dz)
        attempted = attempted_fields_from_snapshot(sent)
        apply_result = ApplyResult(
            total_attempted=2,
            succeeded=0,
            failed=[
                ApplyFailure("vibration", "HID write rejected", True),
                ApplyFailure("deadzones", "HID write rejected", True),
            ],
        )
        # Read-back returns the same values as sent: a completed sweep with
        # coincidental matches, not a sweep failure.
        verification = summarize_field_outcomes(
            build_field_outcomes(attempted, sent, sent, apply_result, {})
        )
        self.assertEqual(verification.write_failed, 2)
        self.assertEqual(verification.verified_matched, 2)
        self.assertEqual(verification.mismatched, 0)
        self.assertEqual(verification.could_not_verify, 0)

        shell = make_shell()
        message = shell._with_sensitivity_downgrade_notice(
            "base", ApplyResult(readback_verification=verification)
        )
        self.assertEqual(message, "base")
        self.assertNotIn(
            i18n.t("apply.result.readback_all_verified"),
            render_log_message(message),
        )

    def test_mixed_write_failure_with_matching_readback_does_not_claim_all_verified(
        self,
    ) -> None:
        # Same gate for the mixed case: one write succeeded and verified, one
        # write failed but reads back matching (pre-existing value). The
        # all-verified note must stay silent — the partial-apply path owns the
        # failure disclosure, and "all changes confirmed" would contradict it.
        vib, dz = object(), object()
        sent = empty_snapshot(vibration=vib, deadzones=dz)
        attempted = attempted_fields_from_snapshot(sent)
        apply_result = ApplyResult(
            total_attempted=2,
            succeeded=1,
            failed=[ApplyFailure("deadzones", "HID write rejected", True)],
        )
        verification = summarize_field_outcomes(
            build_field_outcomes(attempted, sent, sent, apply_result, {})
        )
        self.assertEqual(verification.write_failed, 1)
        self.assertEqual(verification.verified_matched, 2)
        self.assertEqual(verification.mismatched, 0)
        self.assertEqual(verification.could_not_verify, 0)

        shell = make_shell()
        message = shell._with_sensitivity_downgrade_notice(
            "base", ApplyResult(readback_verification=verification)
        )
        self.assertEqual(message, "base")
        self.assertNotIn(
            i18n.t("apply.result.readback_all_verified"),
            render_log_message(message),
        )

    def test_every_attempted_field_has_a_human_label_and_unknowns_fall_back(self) -> None:
        common = dict(
            polling_rate=object(),
            step_size=1,
            vibration=object(),
            deadzones=object(),
            axis_inversion_left=object(),
            axis_inversion_right=object(),
            sensitivity_left=object(),
            sensitivity_right=object(),
            trigger_left=object(),
            trigger_right=object(),
            button_bindings={"A": object()},
            back_paddle_bindings={"M1": object()},
            lighting_zones={"home": object()},
        )
        base_fields = attempted_fields_from_snapshot(empty_snapshot(**common))
        rider_fields = attempted_fields_from_snapshot(
            empty_snapshot(
                **common,
                sensitivity_left_8point=object(),
                sensitivity_right_8point=object(),
            )
        )
        emitted = set(base_fields) | set(rider_fields)

        self.assertEqual(
            emitted,
            {
                "polling_rate",
                "step_size",
                "vibration",
                "deadzones",
                "axis_inversion_left",
                "axis_inversion_right",
                "sensitivity_left",
                "sensitivity_right",
                "sensitivity_left_8point",
                "sensitivity_right_8point",
                "trigger_left",
                "trigger_right",
                "button_bindings",
                "back_paddle_bindings",
                "lighting_zones",
            },
        )
        for field_name in emitted:
            with self.subTest(field_name=field_name):
                self.assertNotEqual(
                    _profile_apply_readback_field_label(field_name),
                    field_name,
                )
        self.assertEqual(
            _profile_apply_readback_field_label("sensitivity_left_8point"),
            "Left stick sensitivity (8-point)",
        )
        self.assertEqual(
            _profile_apply_readback_field_label("future_setting"),
            "future_setting",
        )

    def test_detail_renderer_surfaces_all_three_verify_markers_and_mismatch_values(self) -> None:
        verification = _verification(
            _outcome("step_size", verify_matched=True),
            _outcome(
                "vibration",
                verify_matched=False,
                expected_value="131",
                observed_value="146",
            ),
            _outcome(
                "back_paddle_bindings",
                verify_matched=None,
                verify_note="write-only",
            ),
        )
        shell = SimpleNamespace(COLORS={"text": (1, 1, 1), "muted": (2, 2, 2)})

        with _DpgTextRecorder() as recorder:
            AppShell._render_profile_apply_readback_details(shell, verification)

        self.assertIn(i18n.t("restore_points.result.counts.attempted", n=3), recorder.texts)
        self.assertIn(
            i18n.t("restore_points.result.counts.verified_matched", n=1),
            recorder.texts,
        )
        self.assertIn(
            i18n.t("restore_points.result.counts.could_not_verify", n=1),
            recorder.texts,
        )
        self.assertIn(i18n.t("restore_points.result.counts.mismatched", n=1), recorder.texts)
        rendered = "\n".join(recorder.texts)
        self.assertIn("Step size: write=ok verify=matched", rendered)
        self.assertIn("Vibration: write=ok verify=mismatch", rendered)
        self.assertIn("Back paddle bindings: write=ok verify=unverified", rendered)
        self.assertIn("expected: 131, observed: 146", rendered)
        self.assertIn("(write-only)", rendered)

    def test_detail_renderer_rerenders_shared_state_in_zh_and_ko(self) -> None:
        verification = _verification(
            _outcome("step_size", verify_matched=True),
            _outcome(
                "vibration",
                verify_matched=False,
                expected_value="RAW_EXPECTED",
                observed_value="RAW_OBSERVED",
            ),
            _outcome(
                "back_paddle_bindings",
                verify_matched=None,
                verify_note="verify-read failed: OS_ERROR_121",
            ),
            _outcome(
                "deadzones",
                verify_matched=None,
                write_succeeded=False,
                write_error="RAW_WRITE_ERROR",
            ),
        )
        shell = SimpleNamespace(COLORS={"text": (1, 1, 1), "muted": (2, 2, 2)})
        expected_by_locale = {
            "zh-CN": (
                "摇杆步进：写入=成功 验证=匹配",
                "振动：写入=成功 验证=不匹配",
                "预期：RAW_EXPECTED，实际：RAW_OBSERVED",
                "后置侧键绑定：写入=成功 验证=未验证",
                "验证回读失败：OS_ERROR_121",
                "死区：写入=失败 验证=未验证",
            ),
            "ko": (
                "스텝 크기: 쓰기=성공 검증=일치",
                "진동: 쓰기=성공 검증=불일치",
                "예상: RAW_EXPECTED, 관찰값: RAW_OBSERVED",
                "후면 패들 바인딩: 쓰기=성공 검증=검증되지 않음",
                "검증 읽기 실패: OS_ERROR_121",
                "데드존: 쓰기=실패 검증=검증되지 않음",
            ),
        }

        for locale, expected_fragments in expected_by_locale.items():
            with self.subTest(locale=locale):
                i18n.set_locale(locale)
                with _DpgTextRecorder() as recorder:
                    AppShell._render_profile_apply_readback_details(shell, verification)
                rendered = "\n".join(recorder.texts)
                for fragment in expected_fragments:
                    self.assertIn(fragment, rendered)
                self.assertIn("RAW_WRITE_ERROR", rendered)
                self.assertNotIn("write=ok", rendered)
                self.assertNotIn("verify=matched", rendered)

    def test_write_error_translation_allowlist_preserves_i18n_key_collisions(self) -> None:
        verification = _verification(
            _outcome(
                "step_size",
                verify_matched=None,
                write_succeeded=False,
                write_error="actions.cancel",
            ),
            _outcome(
                "vibration",
                verify_matched=None,
                write_succeeded=False,
                write_error="apply.failure.error.not_available",
            ),
        )
        shell = SimpleNamespace(COLORS={"text": (1, 1, 1), "muted": (2, 2, 2)})
        expected_sentinel_by_locale = {
            "en": "not available",
            "zh-CN": "不可用",
            "ko": "사용할 수 없음",
        }

        for locale, expected_sentinel in expected_sentinel_by_locale.items():
            with self.subTest(locale=locale):
                i18n.set_locale(locale)
                with _DpgTextRecorder() as recorder:
                    AppShell._render_profile_apply_readback_details(shell, verification)
                rendered = "\n".join(recorder.texts)
                self.assertIn("actions.cancel", rendered)
                self.assertIn(expected_sentinel, rendered)
                self.assertNotIn(i18n.t("actions.cancel"), rendered)

    def test_status_action_tracks_sweep_and_opens_through_the_modal_swap(self) -> None:
        shell = make_shell()
        result = ApplyResult(
            readback_verification=_verification(
                _outcome("step_size", verify_matched=True),
            )
        )
        shell._last_apply_result = result
        shell._dpg_context_ready = True

        with patch("zd_app.ui.app_shell.dpg.does_item_exist", return_value=True), patch(
            "zd_app.ui.app_shell.dpg.configure_item"
        ) as configure_item:
            shell._sync_profile_apply_readback_details_action()
            shell._last_apply_result = ApplyResult()
            shell._sync_profile_apply_readback_details_action()

        configure_item.assert_has_calls(
            [
                call("footer_apply_readback_details_button", show=True),
                call("footer_apply_readback_details_button", show=False),
            ]
        )

        shell._last_apply_result = result
        with patch.object(shell, "_defer_modal_swap") as defer_modal_swap:
            shell._open_profile_apply_readback_details_modal()

        defer_modal_swap.assert_called_once()
        open_fn = defer_modal_swap.call_args.args[0]
        self.assertEqual(
            defer_modal_swap.call_args.kwargs["delete_tags"],
            ("apply_failure_modal",),
        )
        self.assertEqual(defer_modal_swap.call_args.kwargs["key"], "profile_apply_details")
        with patch.object(shell, "_show_apply_failure_modal") as show_modal:
            open_fn()
        show_modal.assert_called_once_with(result)
