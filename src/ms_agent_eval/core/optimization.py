from __future__ import annotations

from collections.abc import Mapping, Sequence

from .errors import IntegrityError


COPRO_PROPOSAL_ACCOUNTING_FIELDS = frozenset(
    {
        "requested_count",
        "received_count",
        "accepted_count",
        "unchanged_dropped_count",
        "duplicate_dropped_count",
        "excess_dropped_count",
        "shortfall_count",
        "degraded",
    }
)


def normalize_copro_proposals(
    raw_candidates: Sequence[object],
    *,
    requested_count: int,
    target_kind: str,
    current_text: str,
    current_output_prefix: str,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Normalize a generative COPRO proposal without treating exhaustion as corruption."""

    if requested_count < 0:
        raise IntegrityError("optimizer candidate_count must not be negative")
    if target_kind not in {"solver_prompt", "skill"}:
        raise IntegrityError(f"unsupported COPRO target kind: {target_kind!r}")

    original_identity = (
        _non_empty_text(current_text, "optimizer current_text"),
        current_output_prefix.strip() if target_kind == "solver_prompt" else "",
    )
    if target_kind == "solver_prompt" and not original_identity[1]:
        raise IntegrityError("optimizer current_output_prefix must not be empty")

    accepted: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    unchanged_count = 0
    duplicate_count = 0
    excess_count = 0

    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, Mapping):
            raise IntegrityError("optimizer candidate must be an object")
        unknown = set(raw_candidate) - {"instruction", "output_prefix"}
        if unknown:
            raise IntegrityError(
                f"optimizer candidate contains unknown fields: {sorted(unknown)}"
            )
        instruction = _non_empty_text(
            raw_candidate.get("instruction"), "candidate instruction"
        )
        raw_prefix = raw_candidate.get("output_prefix", "")
        if not isinstance(raw_prefix, str):
            raise IntegrityError("candidate output_prefix must be a string")
        output_prefix = raw_prefix.strip() if target_kind == "solver_prompt" else ""
        if target_kind == "solver_prompt" and not output_prefix:
            raise IntegrityError("solver prompt candidates require output_prefix")

        identity = (instruction, output_prefix)
        if identity == original_identity:
            unchanged_count += 1
            continue
        if identity in identities:
            duplicate_count += 1
            continue
        if len(accepted) >= requested_count:
            excess_count += 1
            continue
        identities.add(identity)
        accepted.append({"instruction": instruction, "output_prefix": output_prefix})

    shortfall_count = max(0, requested_count - len(accepted))
    accounting: dict[str, object] = {
        "requested_count": requested_count,
        "received_count": len(raw_candidates),
        "accepted_count": len(accepted),
        "unchanged_dropped_count": unchanged_count,
        "duplicate_dropped_count": duplicate_count,
        "excess_dropped_count": excess_count,
        "shortfall_count": shortfall_count,
        "degraded": shortfall_count > 0,
    }
    return accepted, accounting


def validate_copro_proposal_accounting(
    value: object,
    *,
    expected_count: int,
    accepted_count: int,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityError("COPRO proposal_accounting must be an object")
    if set(value) != COPRO_PROPOSAL_ACCOUNTING_FIELDS:
        raise IntegrityError("COPRO proposal_accounting has the wrong fields")

    counts: dict[str, int] = {}
    for field in COPRO_PROPOSAL_ACCOUNTING_FIELDS - {"degraded"}:
        raw = value.get(field)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise IntegrityError(f"COPRO proposal_accounting {field} must be non-negative")
        counts[field] = raw
    degraded = value.get("degraded")
    if not isinstance(degraded, bool):
        raise IntegrityError("COPRO proposal_accounting degraded must be boolean")
    if counts["requested_count"] != expected_count:
        raise IntegrityError("COPRO proposal_accounting requested_count is invalid")
    if counts["accepted_count"] != accepted_count:
        raise IntegrityError("COPRO proposal_accounting accepted_count is invalid")
    if accepted_count > expected_count:
        raise IntegrityError("COPRO proposal_accounting exceeds requested_count")
    dropped = (
        counts["unchanged_dropped_count"]
        + counts["duplicate_dropped_count"]
        + counts["excess_dropped_count"]
    )
    if counts["received_count"] != accepted_count + dropped:
        raise IntegrityError("COPRO proposal_accounting received_count is inconsistent")
    shortfall = max(0, expected_count - accepted_count)
    if counts["shortfall_count"] != shortfall or degraded is not (shortfall > 0):
        raise IntegrityError("COPRO proposal_accounting shortfall is inconsistent")
    return value


def _non_empty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntegrityError(f"{label} must be a non-empty string")
    return value.strip()


__all__ = ["normalize_copro_proposals", "validate_copro_proposal_accounting"]
