"""User guidance for support-heavy ZD workflows."""

from __future__ import annotations

from dataclasses import dataclass

from zd_app.i18n import t


@dataclass(frozen=True)
class SupportGuide:
    key: str
    title: str
    summary: str
    bullets: tuple[str, ...]
    evidence_note: str


CONFIG_TERMS_GUIDE = SupportGuide(
    key="config_terms",
    title="Config Terms",
    summary="Ultimate Legend uses \u914d\u7f6e as the safest umbrella term for editable controller setups.",
    bullets=(
        "Strong Ultimate Legend labels to preserve: \u677f\u8f7d\u914d\u7f6e\u5207\u6362, \u672c\u673a\u914d\u7f6e, \u5b58\u68631-4.",
        "Use Config as the primary English family term so Windows summary labels and our app stay aligned.",
        "Cross-line aliases such as \u677f\u8f7d\u8bbe\u7f6e\u5207\u6362 remain useful for search and support, but they do not replace stronger Ultimate Legend wording.",
    ),
    evidence_note="Reflects Ultimate Legend's own on-controller Config terminology.",
)


CONNECTION_GUIDE = SupportGuide(
    key="connection",
    title="Configuration Connection",
    summary="Configuring the controller in the app is a settings channel, not the same thing as gameplay pairing.",
    bullets=(
        "Open the app after the controller is powered on and connected to the device you actually want to play on.",
        "Official add-device flow: \u6dfb\u52a0\u8bbe\u5907 -> \u7ec8\u6781\u4f20\u5947 -> \u626b\u63cf -> \u786e\u8ba4 -> \u8fde\u63a5.",
        "Official mobile guidance says the app needs Bluetooth and location permissions, but settings traffic does not require phone Bluetooth pairing.",
        "Current app branding is \u81f4\u52a8\u6e38\u620f\u5730\u5e26. Older manuals and videos may still say \u81f4\u52a8Z\u5730\u5e26.",
    ),
    evidence_note="Based on official ZD support materials and documented connection workflows, with workflow wording preserved as guidance rather than a definitive specification.",
)


CALIBRATION_GUIDE = SupportGuide(
    key="calibration",
    title="Calibration Targets",
    summary="Choose the symptom first so you run the right calibration flow for the right part of the controller.",
    bullets=(
        "Stick drift or centering issues map to \u6447\u6746\u6821\u51c6.",
        "Trigger actuation, trigger lock feel, or trigger travel issues map to \u6263\u673a\u6821\u51c6 / \u6263\u673a\u9501.",
        "Motion drift maps to \u4f53\u611f\u6821\u51c6.",
        "Some ZD lines combine trigger and stick calibration. Follow the model-specific flow instead of calibrating only one part.",
        "Official Windows Home evidence also shows \u5f00\u59cb\u6821\u51c6, \u7ed3\u675f\u6821\u51c6, and \u6821\u51c6\u8bbe\u7f6e as top-level actions.",
    ),
    evidence_note="Based on official Windows summary evidence plus the reconciled Chinese manual and documented video walkthroughs.",
)

CALIBRATION_BULLET_KEYS = (
    "support.calibration.bullet.stick",
    "support.calibration.bullet.trigger",
    "support.calibration.bullet.motion",
    "support.calibration.bullet.combined",
)


FIRMWARE_GUIDE = SupportGuide(
    key="firmware",
    title="Firmware Targets",
    summary="Treat updates as separate target lanes so the app can guide users to the right recovery path without pretending it can flash directly.",
    bullets=(
        "Choose a target first: Controller, Receiver, Left Joystick (L3), and Right Joystick (R3) should not be collapsed into one flat Update Firmware action.",
        "Nearby Windows bundles keep the same three-way split across Receiver, L3, and R3, so the update surface should stay target-aware instead of flattening everything into generic firmware.",
        "Official/public wording strongly supports separate \u624b\u67c4\u56fa\u4ef6, \u63a5\u6536\u5668\u56fa\u4ef6, and \u5de6/\u53f3\u6447\u6746\u56fa\u4ef6 lanes, while current Windows bundles expose the side lanes as L3 and R3.",
        "The strongest current local naming clue is Dongle (Receiver) Upgrade Instructions, which makes Receiver the safest user-facing alias for the dongle lane.",
        "Receiver recovery and receiver firmware flows are distinct from ordinary controller recovery and can involve the receiver button while inserting into USB.",
        "Current local package evidence keeps L3 and R3 as separate maintenance lanes instead of one flat side-update path.",
        "Ultimate Legend 8K firmware guidance expects the matching PC client version at the same time.",
        "Safer status labels are Official Steps Available, Recovery Instructions Available, Version Unknown, and Direct App Flashing Not Available.",
        "Public official downloads are fragmented, and some advanced upgrades still route through QQ-group support rather than one clean public desktop flow.",
    ),
    evidence_note="Based on comparison of official Windows update bundles and official ZD firmware/download references.",
)


WINDOWS_COMPONENT_GUIDE = SupportGuide(
    key="windows_component_model",
    title="Windows Support Stack",
    summary="Ultimate Legend Windows support is a multi-target system with multiple PC packages, not one single controller-settings object.",
    bullets=(
        "The main maintenance targets are controller main firmware, receiver firmware, Left Joystick (L3), and Right Joystick (R3).",
        "The January and March Windows update bundles keep the same Receiver, L3, and R3 split, so treat each one as its own maintenance path.",
        "ZD Game Zone 3.7 and Z Center are both official ZD apps, so use the official listings to find the right package.",
        "Receiver is the clearest user-facing name for the dongle, so look for Receiver (Dongle) wording when you need to update it.",
        "Receiver and side-target maintenance use distinct entry actions, so runtime configuration, normal pairing, and firmware updates are handled separately.",
        "The side targets stay as separate L3 and R3 maintenance lanes rather than one self-contained side updater.",
        "Some deeper local package terms and the exact L3/R3 split are not fully documented, so follow the target-specific flow when you are unsure.",
    ),
    evidence_note="Based on the official Windows update bundles and ZD package listings.",
)


FIRMWARE_ROUTING_GUIDE = SupportGuide(
    key="firmware_routing",
    title="Choose the Right Target",
    summary="If you do not already know the target, route by symptom first instead of guessing which firmware lane to use.",
    bullets=(
        "If the controller still works over cable but not through the dongle, start with the Receiver lane.",
        "If the issue is broad after an update and not isolated to one side, start with the Controller lane.",
        "If only the left-side or left-stick path feels wrong, start with Left Joystick (L3).",
        "If only the right-side or right-stick path feels wrong, start with Right Joystick (R3).",
        "If you are unsure, begin with symptom checks instead of choosing a firmware file or updater package.",
        "This app can route the recovery flow, but it does not flash these targets directly yet.",
    ),
    evidence_note="Based on the firmware-target routing model derived from official ZD support flows and labeling.",
)


FEATURE_ACCESS_GUIDE = SupportGuide(
    key="feature_access",
    title="Feature Access Tiers",
    summary="The app has a strong public-only foundation, while a narrow set of advanced controller actions still depends on the official Controller Settings session.",
    bullets=(
        "Public features are safe without the hidden session: controller detection, diagnostics, local drafts, import/export, and the official-summary bridge.",
        "Session features are real but currently official-session-dependent: Activate Config and guarded Lighting Mode actions.",
        "Some actions stay out of scope by design: this app routes firmware and update recovery to official tools instead of flashing the controller directly.",
        "The safest current product posture is: public foundation first, with advanced controller actions clearly labeled as official-session-dependent.",
    ),
    evidence_note="Reflects which actions work on their own and which still need the official Controller Settings session.",
)


CONFIG_SLOTS_GUIDE = SupportGuide(
    key="config_slots",
    title="Config Slots",
    summary="Treat the controller config, the local draft, and the target onboard slot as three separate things.",
    bullets=(
        "\u914d\u7f6e is the safest umbrella term for editable controller setups.",
        "Strong Ultimate Legend labels to preserve: \u677f\u8f7d\u914d\u7f6e\u5207\u6362, \u672c\u673a\u914d\u7f6e, \u5b58\u68631-4.",
        "The app should always show whether the current draft is read from controller, inferred from the active slot, or purely local.",
        "A write action should highlight the exact target slot before overwrite.",
    ),
    evidence_note="Keeps the controller config, your local draft, and the onboard slot as three distinct things so writes always show their exact target.",
)


BLUETOOTH_PAIRING_GUIDE = SupportGuide(
    key="bluetooth_pairing",
    title="Bluetooth Pairing",
    summary="Use this guide when the user wants Bluetooth specifically and pairing is not completing cleanly.",
    bullets=(
        "Confirm that the user really wants the Bluetooth path, not receiver or wired.",
        "Official support language ties pairing help to \u6a21\u5f0f\u5207\u6362, \u914d\u5bf9\u72b6\u6001, \u914d\u5bf9\u6210\u529f, and \u84dd\u7259\u8fde\u63a5.",
        "If Bluetooth pairing fails, remove stale pairing data from the host first, then retry.",
        "Do not blur app-side configuration traffic with gameplay pairing on the current host.",
    ),
    evidence_note="Based on official ZD support tasks plus documented workflow findings that configuration and host pairing are related but not identical tasks.",
)


RECEIVER_PAIRING_GUIDE = SupportGuide(
    key="receiver_pairing",
    title="Receiver Pairing",
    summary="Treat receiver pairing as a distinct transport flow rather than a variant of Bluetooth help.",
    bullets=(
        "Use receiver-specific wording such as \u63a5\u6536\u5668 and \u914d\u5bf9\u952e.",
        "Confirm the receiver is inserted before guiding the pairing step.",
        "Keep the physical pairing action and the transport verification step separate.",
        "If pairing still fails, route to receiver repair instead of looping on generic retry.",
    ),
    evidence_note="Based on official ZD support tasks and the official receiver-support pattern described across Ultimate Legend and broader ZD-family materials.",
)


RESTORE_DEFAULTS_GUIDE = SupportGuide(
    key="restore_defaults",
    title="Restore Defaults",
    summary="Use restore-defaults as a supported recovery path when the controller state feels wrong or migration left the user unsure what changed.",
    bullets=(
        "Official recovery wording centers on \u6062\u590d\u9ed8\u8ba4 and \u91cd\u7f6e.",
        "This should be presented as a safe recovery lane, not as a hidden panic button.",
        "Restore-defaults is especially useful after migration confusion, unreadable state, or unexpected behavior after an update.",
        "After restore-defaults, the app should encourage a follow-up read or diagnostics check.",
    ),
    evidence_note="Based on official ZD support flows and the repeated recovery pattern across the official Chinese support surfaces.",
)


MODE_SWITCHING_GUIDE = SupportGuide(
    key="mode_switching",
    title="Mode Switching",
    summary="Mode problems usually come from a transport-plus-protocol mismatch, not from one flat global toggle.",
    bullets=(
        "Official guidance consistently uses \u6a21\u5f0f\u5207\u6362 as a real support task.",
        "The app should help users think in terms of transport plus mode, such as wired, receiver, or Bluetooth combined with Xinput or Switch.",
        "If a feature behaves strangely, confirm transport and mode before changing advanced settings.",
        "Mode help should sit near connection help, not only inside advanced settings.",
    ),
    evidence_note="Based on official ZD support tasks and the broader official workflow pattern around pairing and configuration.",
)


RECEIVER_REPAIR_GUIDE = SupportGuide(
    key="receiver_repair",
    title="Receiver Repair",
    summary="Receiver repair belongs in a recovery lane that is clearly separate from normal receiver pairing.",
    bullets=(
        "Use \u63a5\u6536\u5668\u56fa\u4ef6 when the task is repair or firmware, not normal pairing.",
        "Receiver recovery can require a manual USB re-entry step with the receiver triangle button.",
        "The app should explain when the user is entering recovery mode rather than ordinary connection mode.",
        "Repair success should not be described as the same thing as verified gameplay pairing success.",
    ),
    evidence_note="Based on official ZD support tasks and the official receiver-firmware/tutorial pattern.",
)


SUPPORT_GUIDES = {
    guide.key: guide
    for guide in (
        CONFIG_TERMS_GUIDE,
        CONNECTION_GUIDE,
        CALIBRATION_GUIDE,
        FIRMWARE_GUIDE,
        WINDOWS_COMPONENT_GUIDE,
        FIRMWARE_ROUTING_GUIDE,
        FEATURE_ACCESS_GUIDE,
        CONFIG_SLOTS_GUIDE,
        BLUETOOTH_PAIRING_GUIDE,
        RECEIVER_PAIRING_GUIDE,
        RESTORE_DEFAULTS_GUIDE,
        MODE_SWITCHING_GUIDE,
        RECEIVER_REPAIR_GUIDE,
    )
}


def get_guide(key: str) -> SupportGuide:
    return SUPPORT_GUIDES[key]


def localized_summary(guide: SupportGuide) -> str:
    if guide.key == "calibration":
        return t("support.calibration.summary")
    return guide.summary


def localized_bullets(guide: SupportGuide) -> tuple[str, ...]:
    if guide.key == "calibration":
        return tuple(t(key) for key in CALIBRATION_BULLET_KEYS)
    return guide.bullets
