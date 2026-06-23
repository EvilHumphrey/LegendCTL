"""Structured risk classifier for Safe Import.

``classify_import`` takes a candidate wrapper-profile payload (already-parsed
JSON) and buckets its settings into risk categories for the Safe-Import preview UI,
while surfacing unknown / blocked / automation-looking foreign keys. It rides
the validating snapshot codec (via ``WrapperProfile.from_dict``) for schema and
range validation and never raises on hostile input — every failure mode is
reported in the returned ``ImportResult``.

No UI here: the Safe-Import UI consumes ``ImportResult`` and owns all i18n copy.
The policy seams were resolved 2026-05-22 by the import-policy design:
polling = DEVICE, benign unknowns are ignored-with-warning, and automation keys
**block + discard** exactly like safety-sensitive ones. The remaining knob
(``fail_on_unknown``) and the category map stay as clearly-marked seams below.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from zd_app.models import WrapperProfile
from zd_app.storage.snapshot_codec import snapshot_to_dict
from zd_app.storage.wrapper_profile_store import slugify, unique_display_name


class RiskCategory(Enum):
    """Risk buckets surfaced in the import preview."""

    FEEL = "feel"
    LAYOUT = "layout"
    COSMETIC = "cosmetic"
    DEVICE = "device"
    AUTOMATION = "automation"
    BLOCKED = "blocked"


# --- Risk categorization seam (resolved 2026-05-22) ------------------------
# This dict is the single seam: editing it re-buckets fields without touching
# logic. Keys are "preview field" keys (mostly snapshot keys; the trigger frames
# split into ``.range`` (FEEL) and ``.mode`` (LAYOUT) because one trigger frame
# carries both a feel value and a layout-ish mode). Kept as a seam for the badges.
_CATEGORY_MAP: dict[str, RiskCategory] = {
    "polling_rate": RiskCategory.DEVICE,  # Resolved: Device (not Feel).
    "step_size": RiskCategory.DEVICE,  # Device: global stick step-size, like polling.
    "vibration": RiskCategory.COSMETIC,
    "deadzones": RiskCategory.FEEL,
    # Inversion is a FEEL setting everywhere — this aligns
    # the classifier with the Safe-Import UI map and the restore-point registry
    # (agreement pinned by tests/test_field_registry_drift.py).
    "axis_inversion_left": RiskCategory.FEEL,
    "axis_inversion_right": RiskCategory.FEEL,
    "sensitivity_left": RiskCategory.FEEL,
    "sensitivity_right": RiskCategory.FEEL,
    # 1.2.9 / fw-1.24 8-point curves (HID cat 0x86): same per-stick "feel"
    # surface as the 3-point curves above, so they share the FEEL bucket.
    # Registered here because the codec now serializes them, and
    # KNOWN_SNAPSHOT_KEYS derives from this map (CategoryMapDriftTests guards
    # the codec<->map sync).
    "sensitivity_left_8point": RiskCategory.FEEL,
    "sensitivity_right_8point": RiskCategory.FEEL,
    "trigger_left.range": RiskCategory.FEEL,
    "trigger_left.mode": RiskCategory.LAYOUT,
    "trigger_right.range": RiskCategory.FEEL,
    "trigger_right.mode": RiskCategory.LAYOUT,
    "button_bindings": RiskCategory.LAYOUT,
    "lighting_zones": RiskCategory.COSMETIC,
    "motion_settings": RiskCategory.FEEL,  # motion sensitivity; read-only on apply.
    "back_paddle_bindings": RiskCategory.LAYOUT,
}

# Device-level settings: global controller state (not per-profile feel/layout).
# Single source of truth shared by Save As (opt-in), the profile-card badge, and
# the apply confirm. The DEVICE entries of ``_CATEGORY_MAP`` must agree with this
# (guarded by ``CategoryMapDriftTests``).
DEVICE_SETTING_KEYS: frozenset[str] = frozenset({"polling_rate", "step_size"})

# Top-level keys of a valid WrapperProfile payload (mirrors WrapperProfile.to_dict).
KNOWN_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"schema_version", "name", "description", "created_at", "last_modified_at", "snapshot"}
)

# Snapshot keys, in a stable order, derived from the category map so the two can
# never drift. A key the codec does not serialize (e.g. the flagged ``step_size``
# gap) is therefore treated as unknown until both the codec and this map gain it.
_SNAPSHOT_KEY_ORDER: tuple[str, ...] = tuple(
    dict.fromkeys(key.split(".", 1)[0] for key in _CATEGORY_MAP)
)
KNOWN_SNAPSHOT_KEYS: frozenset[str] = frozenset(_SNAPSHOT_KEY_ORDER)

# Foreign keys that look like rapid-fire / macro-timing automation, including
# the dormant v1 ``turbo_enabled`` / ``macro_binding`` field names. Our schema
# has no automation fields, so these only ever match foreign keys. The design
# (2026-05-22): automation keys block + discard, same as safety-sensitive keys.
_AUTOMATION_KEY_PATTERNS: tuple[str, ...] = (
    "automation", "turbo", "rapidfire", "rapid_fire", "autofire", "auto_fire",
    "macro", "repeat", "count", "delay", "interval", "timing", "sequence",
)

# Foreign keys in safety-sensitive namespaces: raw-HID byte injection, arbitrary
# filesystem paths, shell/script/command execution, plugins, and device/firmware
# overrides are surfaces Safe Import must never silently carry into a profile.
# Covers the always-blocked list; deliberately broad (these only ever
# match foreign keys).
_BLOCKED_KEY_PATTERNS: tuple[str, ...] = (
    "hid", "raw", "report", "script", "command", "cmd", "exec", "shell",
    "path", "dll", "payload", "file", "url", "registry",
    "plugin", "reserved", "override",
)

# Single-token patterns matched as a WHOLE token rather than by token-prefix.
# ``count`` is too short/common as a prefix — it tripped ``country_code``
# (``country`` startswith ``count``) — so it fires only on a literal ``count``
# token. Other single-token patterns stay prefix-matched (``macro`` still
# catches ``macros``; a split ``rawReport`` still hits ``raw`` / ``report``).
_EXACT_TOKEN_PATTERNS: frozenset[str] = frozenset({"count"})

# Compact (de-separated) high-risk fragments. The token scan (``_matches``)
# anchors at token boundaries, so a glued compound with no separator or
# camelCase hump — ``runshellcmd``, ``evilexec`` — never splits and slips past
# it. We re-test the JOINED compact key for these fragments, but anchored at the
# compact key's START or END rather than a bare substring ``in``: a benign word
# that merely embeds a fragment mid-string must not match (``description`` /
# ``subscription`` both contain ``script`` but neither starts nor ends with it),
# preserving the token scan's no-mid-word-false-positive contract while still
# catching a boundary-glued danger word. Safety beats automation, matching the
# precedence in ``_scan_dangerous_keys``.
_COMPACT_BLOCKED_SUBSTRINGS: tuple[str, ...] = (
    "shell", "cmd", "exec", "script", "command", "payload", "report", "hid",
)
_COMPACT_AUTOMATION_SUBSTRINGS: tuple[str, ...] = (
    "turbo", "macro", "rapidfire", "autofire",
)


@dataclass(frozen=True)
class ImportPolicy:
    """Policy for *benign* unknown foreign keys (a policy seam).

    Automation and safety-sensitive keys always block + discard (a design
    decision 2026-05-22) regardless of this policy.
    """

    fail_on_unknown: bool = False  # Default: ignore benign unknowns with a warning.


DEFAULT_IMPORT_POLICY = ImportPolicy()


@dataclass(frozen=True)
class FieldChange:
    """One setting carried by an import.

    For recognized fields, ``imported_value`` is the codec-dict representation
    (what would be applied); the Safe-Import UI fills ``current_value`` against the
    live/active profile for the diff. For BLOCKED / AUTOMATION entries it is
    ``None`` — those keys are name-only (never echo a hostile payload).
    """

    category: RiskCategory
    key: str
    label_key: str
    imported_value: Any


def _empty_categories() -> dict[RiskCategory, list[FieldChange]]:
    return {category: [] for category in RiskCategory}


@dataclass
class ImportResult:
    """UI-ready classification of a candidate import (valid or hostile)."""

    profile: WrapperProfile | None = None
    categories: dict[RiskCategory, list[FieldChange]] = field(default_factory=_empty_categories)
    warnings: list[str] = field(default_factory=list)
    blocked_fields: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    generated_name: str = ""
    ok: bool = False

    @property
    def blocked_automation_count(self) -> int:
        """Automation keys blocked + discarded (feeds the Safe-Import ImportAudit)."""

        return len(self.categories[RiskCategory.AUTOMATION])

    @property
    def blocked_safety_count(self) -> int:
        """Safety-sensitive keys blocked + discarded (feeds the Safe-Import ImportAudit)."""

        return len(self.categories[RiskCategory.BLOCKED])


def _label_key(preview_key: str) -> str:
    return f"import.field.{preview_key}"


# Tokenizer for danger-key matching. A key is lower-cased and split into
# alphanumeric tokens on (a) any run of non-alphanumeric chars and (b) camelCase
# humps. So ``rapid-fire`` / ``rapid fire`` / ``rapid.fire`` / ``rapid_fire`` /
# ``rapidFire`` all tokenize to ``["rapid", "fire"]`` — separator and case
# variants collapse onto one canonical token sequence — while ``subscription``
# stays a single token (it can't false-match ``script``).
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    # NFKC-normalize first so compatibility forms fold to their ASCII canon
    # (fullwidth ``rapidＦire`` -> ``rapidFire`` -> ``["rapid", "fire"]``),
    # closing a homoglyph evasion of the danger scan. Confusables that NFKC does
    # NOT fold (Cyrillic/Greek look-alikes) are caught separately by the
    # fail-closed non-ASCII check in ``_scan_dangerous_keys``.
    text = unicodedata.normalize("NFKC", text)
    spaced = _CAMEL_BOUNDARY.sub(" ", text)
    return [token for token in _NON_ALNUM.split(spaced.lower()) if token]


def _matches(key: str, patterns: tuple[str, ...]) -> bool:
    """True if any pattern matches ``key`` as a whole token or token-prefix.

    Both ``key`` and each pattern are tokenized on separator and camelCase
    boundaries (see :func:`_tokenize`). A single-token pattern matches when it is
    a prefix of any key token (so ``macro`` catches ``macros`` and a split
    ``rawReport`` hits ``raw`` / ``report``) — except patterns in
    :data:`_EXACT_TOKEN_PATTERNS` (``count``), which must equal a whole token so
    a common prefix can't over-match (``country_code`` ⊅ ``count``). A
    multi-token pattern such as ``rapid_fire`` matches when its tokens appear as
    a contiguous run of key tokens (the last allowed to match by prefix), so
    every separator spelling of ``rapid fire`` is caught. Because matching is
    anchored at token starts, a benign word that merely contains a pattern
    mid-token never matches (``description`` ⊅ ``script``, ``account`` ⊅
    ``count``, ``subscription`` ⊅ ``script``). ``key`` is always a ``str`` here
    (the danger scan passes ``str(raw_key)``), so the never-raises
    ``classify_import`` contract holds.
    """

    tokens = _tokenize(key)
    if not tokens:
        return False
    for pattern in patterns:
        pat_tokens = _tokenize(pattern)
        if not pat_tokens:
            continue
        if len(pat_tokens) == 1:
            needle = pat_tokens[0]
            if needle in _EXACT_TOKEN_PATTERNS:
                if any(token == needle for token in tokens):
                    return True
            elif any(token.startswith(needle) for token in tokens):
                return True
            continue
        last = len(pat_tokens) - 1
        for start in range(len(tokens) - last):
            window = tokens[start : start + last + 1]
            if all(window[i] == pat_tokens[i] for i in range(last)) and window[
                last
            ].startswith(pat_tokens[last]):
                return True
    return False


def _iter_preview_fields(snapshot: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """Yield (preview_key, imported_value) for each recognized, set field.

    Skips ``None`` and empty collections (an export always emits every key, so
    an empty ``button_bindings`` is "nothing set", not a change). Splits the
    trigger frames into ``.range`` and ``.mode`` to match the category map.
    """

    for key in _SNAPSHOT_KEY_ORDER:
        if key not in snapshot:
            continue
        value = snapshot[key]
        if value is None:
            continue
        if isinstance(value, (dict, list)) and not value:
            continue
        if key in ("trigger_left", "trigger_right") and isinstance(value, dict):
            range_part = {k: value[k] for k in ("range_min", "range_max") if k in value}
            if range_part:
                yield f"{key}.range", range_part
            if "mode" in value:
                yield f"{key}.mode", value["mode"]
        else:
            yield key, value


def _record_blocked(result: ImportResult, qualified: str, category: RiskCategory, reason: str) -> None:
    """Block + discard a dangerous key: record the name only (never the
    payload) and force ``ok=False``. The codec already discards the value (it
    preserves only known fields), so the key is reported but never written back.
    Idempotent on ``qualified`` so a key is recorded once.
    """

    if qualified in result.blocked_fields:
        return
    result.blocked_fields.append(qualified)
    result.categories[category].append(
        FieldChange(category, qualified, _label_key(qualified), None)
    )
    result.warnings.append(f"{reason}: {qualified}")


def _has_non_ascii_letter(key: str) -> bool:
    """True if ``key`` carries a non-ASCII *letter* after NFKC folding.

    NFKC (applied here and in :func:`_tokenize`) folds compatibility forms such
    as fullwidth ``Ｆ`` to ASCII, so anything still non-ASCII-alphabetic is a
    Cyrillic/Greek/etc. homoglyph (``mаcro``, ``turbοMode``, ``scrіpt``) — a
    confusable-character evasion of the danger scan. The wrapper-profile schema
    has zero legitimate non-ASCII keys, so the scan fails closed on these.
    """

    normalized = unicodedata.normalize("NFKC", key)
    return any(ch.isalpha() and ord(ch) > 0x7F for ch in normalized)


def _compact_matches(key: str, substrings: tuple[str, ...]) -> bool:
    """True if the de-separated compact key starts or ends with a fragment.

    Catches a danger word glued into a single separator-less token
    (``runshellcmd`` -> ends with ``cmd``; ``evilexec`` -> ends with ``exec``)
    that the token-anchored :func:`_matches` misses. Anchoring at the compact
    key's ends — not a bare ``in`` — is deliberate: it preserves the
    no-mid-word-false-positive contract (``description`` / ``subscription`` both
    embed ``script`` mid-string and must not match). See the
    :data:`_COMPACT_BLOCKED_SUBSTRINGS` comment.
    """

    compact = "".join(_tokenize(key))
    if not compact:
        return False
    return any(
        compact.startswith(sub) or compact.endswith(sub) for sub in substrings
    )


def _scan_dangerous_keys(root: Any, *, result: ImportResult) -> None:
    """Flag automation / safety-sensitive key NAMES at any depth.

    An iterative walk (no recursion-limit risk) over the parsed payload. Safety
    patterns take precedence over automation. Known wrapper-profile keys never
    match these patterns, so only foreign keys flag — this also catches a
    dangerous key nested inside a known field (e.g. a ``turbo_enabled`` smuggled
    into a button binding) that the structural unknown scan would not reach.

    Three layers, safety-first: (1) a key with a non-ASCII letter after NFKC is
    a homoglyph evasion and fails closed to BLOCKED (the schema has no legitimate
    non-ASCII keys); (2) the token-anchored ``_matches`` scan; (3) a compact-key
    fragment scan for danger words glued into a single separator-less token.
    """

    stack: list[tuple[str, Any]] = [("", root)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key)
                qualified = f"{path}.{key}" if path else key
                if _has_non_ascii_letter(key):
                    _record_blocked(
                        result, qualified, RiskCategory.BLOCKED,
                        "Blocked non-ASCII key in safety scan",
                    )
                elif _matches(key, _BLOCKED_KEY_PATTERNS) or _compact_matches(
                    key, _COMPACT_BLOCKED_SUBSTRINGS
                ):
                    _record_blocked(result, qualified, RiskCategory.BLOCKED, "Blocked safety-sensitive key")
                elif _matches(key, _AUTOMATION_KEY_PATTERNS) or _compact_matches(
                    key, _COMPACT_AUTOMATION_SUBSTRINGS
                ):
                    _record_blocked(result, qualified, RiskCategory.AUTOMATION, "Blocked automation key")
                stack.append((qualified, child))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                stack.append((f"{path}[{index}]", item))


def _inventory_unknown(result: ImportResult, qualified: str, policy: ImportPolicy) -> None:
    """Record a structural unknown (top-level / snapshot) key.

    Dangerous keys were already blocked by the danger scan; benign unknowns are
    ignored-with-warning unless ``fail_on_unknown`` flips them to blocking.
    """

    result.unknown_fields.append(qualified)
    if qualified in result.blocked_fields:
        return
    result.warnings.append(f"Ignoring unknown key: {qualified}")
    if policy.fail_on_unknown:
        result.blocked_fields.append(qualified)


def classify_import(
    raw_payload: Any,
    *,
    existing_names: set[str],
    policy: ImportPolicy = DEFAULT_IMPORT_POLICY,
) -> ImportResult:
    """Bucket a candidate import into risk categories without ever raising.

    Detects foreign keys the codec would silently ignore, validates the profile
    via the snapshot codec, and returns a structured ``ImportResult`` for both
    valid and hostile inputs.
    """

    existing_slugs = {slugify(name) for name in existing_names}
    result = ImportResult()

    if not isinstance(raw_payload, dict):
        result.warnings.append("Import payload is not a JSON object.")
        result.generated_name = unique_display_name(None, existing_slugs)
        return result

    result.generated_name = unique_display_name(raw_payload.get("name"), existing_slugs)

    # Validate via the codec (schema_version + per-field ranges). A failure
    # leaves profile=None but we still surface foreign-key risks below.
    try:
        result.profile = WrapperProfile.from_dict(raw_payload)
    except (KeyError, ValueError, TypeError) as exc:
        result.warnings.append(f"Profile failed validation: {exc}")

    # Pass 1: block + discard automation / safety-sensitive keys at any depth.
    _scan_dangerous_keys(raw_payload, result=result)

    # Pass 2: inventory structural unknowns (top-level + snapshot). Benign
    # unknowns are ignored-with-warning by default; dangerous ones were already
    # blocked in Pass 1.
    for key in raw_payload:
        if key not in KNOWN_TOP_LEVEL_KEYS:
            _inventory_unknown(result, str(key), policy)
    snapshot = raw_payload.get("snapshot")
    if isinstance(snapshot, dict):
        for key in snapshot:
            if key not in KNOWN_SNAPSHOT_KEYS:
                _inventory_unknown(result, f"snapshot.{key}", policy)

    # Pass 3: categorize recognized, set fields from the codec-normalized
    # snapshot — foreign nested keys are already dropped, so no payload leaks
    # into a FieldChange and the preview reflects exactly what would be applied.
    if result.profile is not None:
        normalized = snapshot_to_dict(result.profile.snapshot)
        for preview_key, imported_value in _iter_preview_fields(normalized):
            category = _CATEGORY_MAP[preview_key]
            result.categories[category].append(
                FieldChange(category, preview_key, _label_key(preview_key), imported_value)
            )

    result.ok = result.profile is not None and not result.blocked_fields
    return result
