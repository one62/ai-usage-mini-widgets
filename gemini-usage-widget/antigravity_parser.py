"""Data models and multi-schema response parsers for Antigravity quota payloads."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


_SHORT_THIRD_PARTY_RE = re.compile(r"(?<![a-z0-9])(?:3p|o1|o3)(?![a-z0-9])")
_SHORT_GEMINI_RE = re.compile(r"(?<![a-z0-9])1p(?![a-z0-9])")

_RESET_TIME_KEYS = (
    "resetTime",
    "reset_time",
    "resetsAt",
    "resets_at",
    "resetAt",
    "reset_at",
    "nextResetTime",
    "next_reset_time",
    "windowEndTime",
    "window_end_time",
    "endTime",
    "end_time",
    "refreshTime",
    "refresh_time",
    "expiresAt",
    "expires_at",
)
_RELATIVE_RESET_KEYS = (
    "resetInSeconds",
    "reset_in_seconds",
    "secondsUntilReset",
    "seconds_until_reset",
    "resetDuration",
    "reset_duration",
)
_ISO_FRAC_RE = re.compile(r"\.(\d+)")
_NUMERIC_STR_RE = re.compile(r"-?\d+(?:\.\d+)?")
_DURATION_SEC_RE = re.compile(r"^(-?\d+(?:\.\d+)?)s$", re.IGNORECASE)


class UsageError(RuntimeError):
    """Raised when Antigravity usage data cannot be retrieved or parsed."""


class AuthRequiredError(UsageError):
    """Raised when Antigravity authentication is missing or expired."""


@dataclass(frozen=True)
class LimitWindow:
    used_percent: int
    resets_at: int | None


@dataclass(frozen=True)
class UsageSnapshot:
    plan: str
    account_email: str | None
    five_hour: LimitWindow | None
    seven_day: LimitWindow | None
    claude_gpt_five_hour: LimitWindow | None
    claude_gpt_seven_day: LimitWindow | None
    credit_balance: int | None
    fetched_at: float


def _normalize_epoch_seconds(value: float) -> int | None:
    if value <= 0:
        return None
    if value > 10_000_000_000:
        value /= 1000.0
    return int(value)


def _parse_reset_time(raw_reset: Any) -> int | None:
    if raw_reset is None or isinstance(raw_reset, bool):
        return None
    if isinstance(raw_reset, dict):
        for sec_key in ("seconds", "epochSeconds", "unixSeconds"):
            if sec_key in raw_reset and raw_reset[sec_key] is not None and not isinstance(raw_reset[sec_key], bool):
                try:
                    return _normalize_epoch_seconds(float(str(raw_reset[sec_key]).strip()))
                except ValueError:
                    pass
        for nested_key in ("iso", "timestamp", "value"):
            if nested_key in raw_reset:
                parsed = _parse_reset_time(raw_reset[nested_key])
                if parsed is not None:
                    return parsed
        return None
    if isinstance(raw_reset, (int, float)):
        return _normalize_epoch_seconds(float(raw_reset))
    if isinstance(raw_reset, str):
        text = raw_reset.strip()
        if not text:
            return None
        if _NUMERIC_STR_RE.fullmatch(text):
            try:
                return _normalize_epoch_seconds(float(text))
            except ValueError:
                return None
        dur_match = _DURATION_SEC_RE.match(text)
        if dur_match:
            try:
                seconds = float(dur_match.group(1))
                return int(time.time() + max(0.0, seconds))
            except ValueError:
                return None
        try:
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            text = _ISO_FRAC_RE.sub(
                lambda match: "." + match.group(1)[:6].ljust(6, "0"),
                text,
                count=1,
            )
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return _normalize_epoch_seconds(dt.timestamp())
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _extract_reset_time(quota_obj: dict[str, Any]) -> int | None:
    containers = [quota_obj]
    for nested_key in ("remaining", "quotaInfo", "quota_info"):
        nested = quota_obj.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)

    for container in containers:
        for key in _RESET_TIME_KEYS:
            if key in container:
                parsed = _parse_reset_time(container.get(key))
                if parsed is not None:
                    return parsed

    for container in containers:
        for key in _RELATIVE_RESET_KEYS:
            raw_val = container.get(key)
            if raw_val is None or isinstance(raw_val, bool):
                continue
            if isinstance(raw_val, (int, float)):
                return int(time.time() + max(0.0, float(raw_val)))
            if isinstance(raw_val, str):
                text = raw_val.strip()
                dur_match = _DURATION_SEC_RE.match(text)
                num_text = dur_match.group(1) if dur_match else text
                try:
                    return int(time.time() + max(0.0, float(num_text)))
                except ValueError:
                    pass
    return None


def _decimal_to_used_percent(fraction: Decimal) -> int:
    clamped = max(Decimal("0"), min(Decimal("1"), fraction))
    used_percent = int(
        ((Decimal("1") - clamped) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    return max(0, min(100, used_percent))


def _parse_used_percent_from_quota_obj(
    quota_obj: dict[str, Any],
    *,
    has_reset_time: bool | None = None,
) -> int | None:
    if not isinstance(quota_obj, dict) or not quota_obj:
        return None

    source = quota_obj
    for nested_key in ("quotaInfo", "quota_info"):
        nested = source.get(nested_key)
        if isinstance(nested, dict) and nested:
            source = nested
            break

    rem_container = source.get("remaining")
    rem_dict = rem_container if isinstance(rem_container, dict) else {}

    rem_frac_raw = None
    for key in ("remainingFraction", "remaining_fraction"):
        if key in rem_dict:
            rem_frac_raw = rem_dict.get(key)
            break
        if key in source:
            rem_frac_raw = source.get(key)
            break
    if rem_frac_raw is None and isinstance(rem_container, (int, float)) and not isinstance(rem_container, bool):
        rem_frac_raw = rem_container

    if rem_frac_raw is not None and not isinstance(rem_frac_raw, bool):
        try:
            rem = Decimal(str(rem_frac_raw))
            if not rem.is_finite():
                raise ValueError("non-finite fraction")
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise UsageError("Antigravity가 반환한 사용량 데이터가 올바르지 않습니다.") from exc
        return _decimal_to_used_percent(rem)

    for key in ("usedFraction", "used_fraction"):
        raw = rem_dict.get(key) if key in rem_dict else source.get(key)
        if raw is not None and not isinstance(raw, bool):
            try:
                used = Decimal(str(raw))
                if not used.is_finite():
                    raise ValueError("non-finite fraction")
                return _decimal_to_used_percent(Decimal("1") - used)
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise UsageError("Antigravity가 반환한 사용량 데이터가 올바르지 않습니다.") from exc

    for key in ("remainingPercent", "remaining_percent", "remainingPercentage", "remaining_percentage"):
        raw = rem_dict.get(key) if key in rem_dict else source.get(key)
        if raw is not None and not isinstance(raw, bool):
            try:
                rem_pct = Decimal(str(raw)) / Decimal("100")
                if not rem_pct.is_finite():
                    raise ValueError("non-finite percent")
                return _decimal_to_used_percent(rem_pct)
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise UsageError("Antigravity가 반환한 사용량 데이터가 올바르지 않습니다.") from exc

    for key in ("usedPercent", "used_percent", "usedPercentage", "used_percentage"):
        raw = rem_dict.get(key) if key in rem_dict else source.get(key)
        if raw is not None and not isinstance(raw, bool):
            try:
                used_pct = Decimal(str(raw)) / Decimal("100")
                if not used_pct.is_finite():
                    raise ValueError("non-finite percent")
                return _decimal_to_used_percent(Decimal("1") - used_pct)
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise UsageError("Antigravity가 반환한 사용량 데이터가 올바르지 않습니다.") from exc

    rem_amount_raw = (
        rem_dict.get("remainingAmount")
        if "remainingAmount" in rem_dict
        else source.get("remainingAmount", source.get("remaining_amount"))
    )
    rem_amount_zero = (
        not isinstance(rem_amount_raw, bool)
        and (rem_amount_raw == 0 or str(rem_amount_raw) == "0")
    )
    reset_present = (
        has_reset_time if has_reset_time is not None else (_extract_reset_time(quota_obj) is not None)
    )

    if (
        source.get("isExhausted") is True
        or source.get("is_exhausted") is True
        or rem_amount_zero
        or reset_present
        or (isinstance(rem_container, dict) and not rem_container)
    ):
        # Proto3 JSON omits remainingFraction when it is 0.0 (100% used).
        return 100

    return None


def _window_from_quota_obj(quota_obj: Any) -> LimitWindow | None:
    if not isinstance(quota_obj, dict) or not quota_obj:
        return None
    resets_at = _extract_reset_time(quota_obj)
    used_percent = _parse_used_percent_from_quota_obj(quota_obj, has_reset_time=resets_at is not None)
    if used_percent is None:
        return None
    return LimitWindow(used_percent=used_percent, resets_at=resets_at)


def _pick_more_constrained(current: LimitWindow | None, candidate: LimitWindow | None) -> LimitWindow | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    current_key = (current.used_percent, 1 if current.resets_at is not None else 0, -(current.resets_at or 0))
    candidate_key = (candidate.used_percent, 1 if candidate.resets_at is not None else 0, -(candidate.resets_at or 0))
    return candidate if candidate_key > current_key else current


def _safe_int_credit(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        dec = Decimal(str(raw).strip())
        if not dec.is_finite():
            return None
        return int(dec)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def _extract_credits_from_obj(obj: Any) -> int | None:
    if not isinstance(obj, dict):
        return None

    for list_key in ("availableCredits", "credits"):
        raw_list = obj.get(list_key)
        if isinstance(raw_list, list):
            total = 0
            found = False
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                amount = item.get("creditAmount") if "creditAmount" in item else item.get("amount")
                parsed = _safe_int_credit(amount)
                if parsed is not None:
                    total += parsed
                    found = True
            if found:
                return total
        else:
            parsed = _safe_int_credit(raw_list)
            if parsed is not None:
                return parsed

    for num_key in ("creditBalance", "g1Credits", "aiCredits", "availablePromptCredits"):
        parsed = _safe_int_credit(obj.get(num_key))
        if parsed is not None:
            return parsed
    return None


def _collect_tag_text(obj: dict[str, Any]) -> str:
    keys = (
        "groupId",
        "group_id",
        "bucketId",
        "bucket_id",
        "familyId",
        "family_id",
        "id",
        "category",
        "modelFamily",
        "model_family",
        "displayName",
        "display_name",
        "name",
        "title",
        "label",
    )
    parts = [str(obj.get(key) or "") for key in keys]
    return " ".join(part for part in parts if part).lower()


def _classify_model_family(tag: str) -> str | None:
    if _SHORT_THIRD_PARTY_RE.search(tag) or any(
        token in tag
        for token in (
            "third-party",
            "third_party",
            "third party",
            "claude",
            "gpt",
            "anthropic",
            "openai",
            "sonnet",
            "opus",
            "haiku",
            "partner",
            "external",
        )
    ):
        return "third_party"
    if _SHORT_GEMINI_RE.search(tag) or any(
        token in tag for token in ("gemini", "argon", "carbon", "hydrogen", "first-party", "first_party")
    ):
        return "gemini"
    return None


def _classify_bucket_cadence(bucket: dict[str, Any], resets_at: int | None) -> str | None:
    for key in ("window", "windowType", "window_type", "cadence", "period", "limitWindow", "limit_window"):
        raw = bucket.get(key)
        if isinstance(raw, str) and raw.strip():
            norm = raw.strip().lower()
            if any(token in norm for token in ("weekly", "week", "7d", "7-day", "7_day", "seven_day", "168h", "604800")):
                return "weekly"
            if any(token in norm for token in ("5h", "5-hour", "5_hour", "five_hour", "session", "short", "rolling", "18000")):
                return "5h"

    for key in ("windowSeconds", "window_seconds", "durationSeconds", "duration_seconds"):
        raw = bucket.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if raw >= 86400:
                return "weekly"
            if 0 < raw <= 43200:
                return "5h"

    bucket_tag = _collect_tag_text(bucket)
    if any(token in bucket_tag for token in ("weekly", "7d", "7-day", "7_day", "7 day", "week", "주간", "일주일", "7일")):
        return "weekly"
    if any(token in bucket_tag for token in ("5h", "5-hour", "5_hour", "5 hour", "five-hour", "five hour", "short", "session", "5시간")):
        return "5h"

    if resets_at is not None:
        return "weekly" if (resets_at - int(time.time())) > 6 * 3600 else "5h"
    return None


def _unwrap_summary_groups(summary_obj: dict[str, Any]) -> list[dict[str, Any]]:
    queue_objs: list[dict[str, Any]] = [summary_obj]
    visited = 0
    wrapper_keys = (
        "response",
        "quotaSummary",
        "quota_summary",
        "userQuotaSummary",
        "user_quota_summary",
        "summary",
        "data",
        "result",
    )
    group_keys = ("groups", "quotaGroups", "quota_groups", "families", "modelGroups", "model_groups")
    bucket_keys = ("buckets", "quotaBuckets", "quota_buckets")

    while queue_objs and visited < 12:
        current = queue_objs.pop(0)
        visited += 1
        for key in group_keys:
            groups = current.get(key)
            if isinstance(groups, list):
                return [item for item in groups if isinstance(item, dict)]
        for key in bucket_keys:
            buckets = current.get(key)
            if isinstance(buckets, list):
                return [{"buckets": buckets}]
        for wkey in wrapper_keys:
            nested = current.get(wkey)
            if isinstance(nested, dict):
                queue_objs.append(nested)
    return []


def _parse_summary_groups(
    summary_obj: dict[str, Any],
) -> tuple[LimitWindow | None, LimitWindow | None, LimitWindow | None, LimitWindow | None]:
    gemini_5h: LimitWindow | None = None
    gemini_7d: LimitWindow | None = None
    claude_5h: LimitWindow | None = None
    claude_7d: LimitWindow | None = None

    groups = _unwrap_summary_groups(summary_obj)
    if not groups:
        return gemini_5h, gemini_7d, claude_5h, claude_7d

    for group in groups:
        group_family = _classify_model_family(_collect_tag_text(group))
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            buckets = group.get("quotaBuckets")
        if not isinstance(buckets, list):
            buckets = group.get("quota_buckets")
        if not isinstance(buckets, list):
            buckets = group.get("items")
        if not isinstance(buckets, list):
            continue

        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            family = group_family or _classify_model_family(_collect_tag_text(bucket))
            if family is None:
                continue

            window = _window_from_quota_obj(bucket)
            if window is None:
                continue

            cadence = _classify_bucket_cadence(bucket, window.resets_at)
            is_weekly = cadence == "weekly"

            if family == "gemini":
                if is_weekly:
                    gemini_7d = _pick_more_constrained(gemini_7d, window)
                else:
                    gemini_5h = _pick_more_constrained(gemini_5h, window)
            elif family == "third_party":
                if is_weekly:
                    claude_7d = _pick_more_constrained(claude_7d, window)
                else:
                    claude_5h = _pick_more_constrained(claude_5h, window)

    return gemini_5h, gemini_7d, claude_5h, claude_7d


def _parse_client_model_configs(
    configs: list[Any],
) -> tuple[LimitWindow | None, LimitWindow | None, LimitWindow | None, LimitWindow | None]:
    gemini_5h: LimitWindow | None = None
    gemini_7d: LimitWindow | None = None
    claude_5h: LimitWindow | None = None
    claude_7d: LimitWindow | None = None

    now = int(time.time())
    for entry in configs:
        if not isinstance(entry, dict):
            continue

        label_raw = str(entry.get("label") or "")
        model_or_alias = entry.get("modelOrAlias")
        alias_raw = ""
        if isinstance(model_or_alias, dict):
            alias_raw = str(model_or_alias.get("model") or model_or_alias.get("alias") or "")
        elif isinstance(model_or_alias, str):
            alias_raw = model_or_alias
        model_id_raw = str(entry.get("modelId") or "")
        combined = f"{label_raw} {alias_raw} {model_id_raw}".lower()

        if "embedding" in combined or "aqa" in combined:
            continue

        is_gemini = any(token in combined for token in ("gemini", "argon", "carbon", "hydrogen"))
        is_third_party = bool(_SHORT_THIRD_PARTY_RE.search(combined)) or any(
            token in combined for token in ("claude", "gpt", "sonnet", "opus", "haiku")
        )
        if not is_gemini and not is_third_party:
            continue

        short_obj = (
            entry.get("quotaInfo")
            if isinstance(entry.get("quotaInfo"), dict)
            else (entry.get("quota_info") if isinstance(entry.get("quota_info"), dict) else entry)
        )
        weekly_obj = (
            entry.get("weeklyQuotaInfo")
            if isinstance(entry.get("weeklyQuotaInfo"), dict)
            else (entry.get("weekly_quota_info") if isinstance(entry.get("weekly_quota_info"), dict) else None)
        )

        has_inline_quota = any(
            key in entry
            for key in (
                "remainingFraction",
                "remaining_fraction",
                "remainingAmount",
                "resetTime",
                "reset_time",
            )
        )
        short_win = _window_from_quota_obj(short_obj) if short_obj is not entry or has_inline_quota else None
        weekly_win = _window_from_quota_obj(weekly_obj) if weekly_obj else None

        # A 5-hour rolling window cannot reset more than 6 hours in the future;
        # if weeklyQuotaInfo is absent and reset is > 6h out, quotaInfo represents the weekly window.
        if (
            short_win is not None
            and weekly_win is None
            and short_win.resets_at is not None
            and (short_win.resets_at - now) > 6 * 3600
        ):
            weekly_win = short_win
            short_win = None

        if is_gemini:
            gemini_5h = _pick_more_constrained(gemini_5h, short_win)
            gemini_7d = _pick_more_constrained(gemini_7d, weekly_win)
        elif is_third_party:
            claude_5h = _pick_more_constrained(claude_5h, short_win)
            claude_7d = _pick_more_constrained(claude_7d, weekly_win)

    return gemini_5h, gemini_7d, claude_5h, claude_7d


def parse_user_quota(
    status_or_quota: dict[str, Any],
    summary_result: dict[str, Any] | None = None,
    account_email: str | None = None,
) -> UsageSnapshot:
    """Convert Antigravity GetUserStatus and/or RetrieveUserQuotaSummary into a UsageSnapshot."""
    if not isinstance(status_or_quota, dict):
        raise UsageError("Antigravity 응답에 사용량 한도 정보가 없습니다.")

    user_status = (
        status_or_quota.get("userStatus")
        if isinstance(status_or_quota.get("userStatus"), dict)
        else status_or_quota
    )

    # 1. Parse summary groups from RetrieveUserQuotaSummary or status_or_quota if present
    g5_sum, g7_sum, c5_sum, c7_sum = (None, None, None, None)
    if isinstance(summary_result, dict):
        g5_sum, g7_sum, c5_sum, c7_sum = _parse_summary_groups(summary_result)
    if g5_sum is None and g7_sum is None and c5_sum is None and c7_sum is None:
        g5_sum, g7_sum, c5_sum, c7_sum = _parse_summary_groups(user_status)

    # 2. Parse per-model configs from cascadeModelConfigData.clientModelConfigs or buckets
    cascade = (
        user_status.get("cascadeModelConfigData")
        if isinstance(user_status.get("cascadeModelConfigData"), dict)
        else {}
    )
    configs = cascade.get("clientModelConfigs")
    if not isinstance(configs, list):
        configs = user_status.get("buckets") if isinstance(user_status.get("buckets"), list) else []

    g5_cfg, g7_cfg, c5_cfg, c7_cfg = _parse_client_model_configs(configs)

    five_hour = g5_sum or g5_cfg
    seven_day = g7_sum or g7_cfg
    claude_gpt_five_hour = c5_sum or c5_cfg
    claude_gpt_seven_day = c7_sum or c7_cfg

    if (
        five_hour is None
        and seven_day is None
        and claude_gpt_five_hour is None
        and claude_gpt_seven_day is None
    ):
        raise UsageError("Antigravity 응답에 사용량 한도 정보가 없습니다.")

    # 3. Extract Account Email
    email = account_email
    for candidate_email in (
        user_status.get("email"),
        user_status.get("accountEmail"),
        (summary_result or {}).get("email") if isinstance(summary_result, dict) else None,
    ):
        if isinstance(candidate_email, str) and candidate_email.strip():
            email = candidate_email.strip()
            break

    # 4. Extract Plan / Tier Name
    plan = "Antigravity"
    user_tier = user_status.get("userTier") if isinstance(user_status.get("userTier"), dict) else {}
    paid_tier = user_status.get("paidTier") if isinstance(user_status.get("paidTier"), dict) else {}
    current_tier = user_status.get("currentTier") if isinstance(user_status.get("currentTier"), dict) else {}
    plan_status = user_status.get("planStatus") if isinstance(user_status.get("planStatus"), dict) else {}
    plan_info = plan_status.get("planInfo") if isinstance(plan_status.get("planInfo"), dict) else {}

    plan_candidate = (
        user_tier.get("preferredName")
        or user_tier.get("name")
        or paid_tier.get("name")
        or current_tier.get("name")
        or plan_info.get("planName")
        or plan_info.get("teamsTier")
        or user_tier.get("id")
        or paid_tier.get("id")
    )
    if isinstance(plan_candidate, str) and plan_candidate.strip():
        plan = plan_candidate.strip()

    # 5. Extract AI Credits Balance
    credit_balance: int | None = None
    for credit_source in (user_tier, paid_tier, plan_status, user_status, summary_result):
        candidate_credits = _extract_credits_from_obj(credit_source)
        if candidate_credits is not None:
            credit_balance = candidate_credits
            break

    return UsageSnapshot(
        plan=plan,
        account_email=email,
        five_hour=five_hour,
        seven_day=seven_day,
        claude_gpt_five_hour=claude_gpt_five_hour,
        claude_gpt_seven_day=claude_gpt_seven_day,
        credit_balance=credit_balance,
        fetched_at=time.time(),
    )


def _has_weekly_limits(snapshot: UsageSnapshot) -> bool:
    return snapshot.seven_day is not None or snapshot.claude_gpt_seven_day is not None
