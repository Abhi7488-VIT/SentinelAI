"""Hypothesis taxonomy (Phase 4 plan §6): a generic SRE catalogue of failure classes.

Written before any live investigation ran and independent of every testbed scenario (ADR 0003):
the categories and their checks describe failure *classes*, not any particular fault. The checks
listed here are what Verification (T4) runs deterministically; a hypothesis is `confirmed` only if
all of its category's checks pass on direct evidence with nothing contradicting it. `other` has no
checks, so it can never be better than `probable`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Category = Literal[
    "resource_exhaustion",
    "slow_dependency_or_query",
    "dependency_unavailable",
    "bad_deployment",
    "memory_or_resource_leak",
    "upstream_propagation",
    "other",
]


@dataclass(frozen=True, slots=True)
class CategorySpec:
    category: Category
    description: str
    checks: tuple[str, ...]


TAXONOMY: dict[str, CategorySpec] = {
    spec.category: spec
    for spec in (
        CategorySpec(
            "resource_exhaustion",
            "A bounded resource of the subject (connection/thread pool, queue, file handles) ran "
            "out, so requests waited or failed.",
            (
                "saturation_or_timeout_signature_in_subject",
                "subject_onset_not_after_first_downstream_symptom",
                "subject_outbound_or_db_error_rate_rose",
            ),
        ),
        CategorySpec(
            "slow_dependency_or_query",
            "One operation or database statement became slow and dominates request latency.",
            (
                "operation_latency_rose",
                "operation_self_time_dominates_critical_path_increase",
                "no_earlier_error_rate_change_elsewhere",
            ),
        ),
        CategorySpec(
            "dependency_unavailable",
            "A callee or external system stopped answering or refused connections.",
            (
                "connection_errors_on_edge_to_target",
                "target_inbound_traffic_dropped_or_errored",
                "onset_order_consistent",
            ),
        ),
        CategorySpec(
            "bad_deployment",
            "A deployment of the subject introduced the failure.",
            (
                "subject_deployed_within_30_min_before_onset",
                "symptoms_start_after_deployment",
                "no_other_service_changed_first",
            ),
        ),
        CategorySpec(
            "memory_or_resource_leak",
            "A resource of the subject grew steadily until it caused failures.",
            (
                "monotonic_increase_before_onset",
                "restarts_or_oom_if_present",
            ),
        ),
        CategorySpec(
            "upstream_propagation",
            "The subject's errors are explained by an erroring callee.",
            ("subject_errors_explained_by_erroring_callee",),
        ),
        CategorySpec(
            "other",
            "None of the above; cannot be confirmed, at best probable.",
            (),
        ),
    )
}


def describe() -> str:
    """The catalogue as plain text for the analyst prompt (trusted, static)."""
    return "\n".join(f"- {s.category}: {s.description}" for s in TAXONOMY.values())
