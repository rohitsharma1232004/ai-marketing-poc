"""Normalization helpers for publishing performance metrics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SUPPORTED_ANALYTICS_WINDOWS = ("24h", "7d", "30d")


def _to_float(value: Any, *, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_metric_value(value: Any, *, default: float = 0.0) -> float:
    """Convert raw API values into a non-negative numeric metric."""
    amount = _to_float(value, default=default)
    if amount < 0:
        return 0.0
    return amount


def normalize_insights_payload(payload: Any) -> dict[str, float]:
    """Normalize a Meta-like insights payload to a stable dict."""
    if not isinstance(payload, Mapping):
        return {
            "impressions": 0.0,
            "reach": 0.0,
            "clicks": 0.0,
            "saves": 0.0,
            "shares": 0.0,
            "comments": 0.0,
            "reactions": 0.0,
            "engagement_rate": 0.0,
        }

    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return {
            "impressions": 0.0,
            "reach": 0.0,
            "clicks": 0.0,
            "saves": 0.0,
            "shares": 0.0,
            "comments": 0.0,
            "reactions": 0.0,
            "engagement_rate": 0.0,
        }

    values: dict[str, float] = {
        "impressions": 0.0,
        "reach": 0.0,
        "clicks": 0.0,
        "saves": 0.0,
        "shares": 0.0,
        "comments": 0.0,
        "reactions": 0.0,
        "engagement_rate": 0.0,
    }

    for item in data:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        metric_values = item.get("values")
        if isinstance(metric_values, Sequence) and not isinstance(metric_values, (str, bytes)):
            latest = metric_values[-1] if metric_values else {}
            if isinstance(latest, Mapping):
                total = latest.get("value", 0)
            else:
                total = latest
        else:
            total = item.get("value", 0)

        normalized_name = name.lower().replace("post_", "").strip()
        normalized_name = normalized_name.replace("_by_type_total", "")
        normalized_name = normalized_name.replace("_by_type", "")
        normalized_name = normalized_name.replace("_total", "")
        normalized_name = normalized_name.replace("_unique", "")
        normalized_name = normalized_name.strip("_")

        if normalized_name in {"impressions", "reach", "clicks", "saves", "shares", "comments", "reactions"}:
            key = normalized_name
        elif normalized_name == "engaged_users":
            key = "engagement_rate"
        elif normalized_name in {"engagement", "engagement_rate"}:
            key = "engagement_rate"
        else:
            key = None

        if key in values:
            values[key] = normalize_metric_value(total)

    total_engagement = (
        values["clicks"]
        + values["saves"]
        + values["shares"]
        + values["comments"]
        + values["reactions"]
    )
    impressions = values["impressions"] or values["reach"]
    if impressions > 0:
        values["engagement_rate"] = (total_engagement / impressions) * 100.0
    else:
        values["engagement_rate"] = 0.0
    return values


def summarize_performance(metrics: Mapping[str, Any]) -> dict[str, float]:
    """Return a compact, serialization-friendly summary for a single post window."""
    normalized = normalize_insights_payload(metrics)
    engagement = (
        normalized["clicks"]
        + normalized["saves"]
        + normalized["shares"]
        + normalized["comments"]
        + normalized["reactions"]
    )
    return {
        "impressions": float(normalized["impressions"]),
        "reach": float(normalized["reach"]),
        "clicks": float(normalized["clicks"]),
        "saves": float(normalized["saves"]),
        "shares": float(normalized["shares"]),
        "comments": float(normalized["comments"]),
        "reactions": float(normalized["reactions"]),
        "engagement": float(engagement),
        "engagement_rate": float(normalized["engagement_rate"]),
    }


def recommend_next_action(metrics: Mapping[str, Any]) -> str:
    """Provide a simple recommendation based on the normalized post performance."""
    if "data" in metrics:
        summary = summarize_performance(metrics)
    else:
        summary = dict(metrics)
        if "engagement" not in summary or "engagement_rate" not in summary:
            summary = summarize_performance(metrics)
    reach = float(summary.get("reach") or summary.get("impressions") or 0.0)
    if reach <= 0:
        return "No measurable reach yet; wait for the next insight window before changing creative strategy."
    rate = float(summary.get("engagement_rate") or 0.0)
    if rate >= 7.0:
        return "High engagement: keep the current creative pattern and scale similar concepts."
    if rate >= 3.0:
        return "Healthy engagement: maintain the hook and tighten the CTA for stronger conversion."
    if float(summary.get("engagement") or 0.0) > 0:
        return "Low engagement: test a stronger opening hook, clearer CTA, and simpler visual framing."
    return "No engagement observed yet: review the hook, offer clarity, and test a more resonant creative angle."
