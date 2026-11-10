"""Shared gold-query SQL builder for the generator and validator.

build_group_where produces the WHERE fragment that matches
recall_components rows to a component group, including sub-component
exclusions.  Used by make_generated_questions.py and validate_golden.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from recall_assistant.evals.mappings import ExcludedSub


def build_group_where(
    categories: tuple[str, ...],
    excluded_subs: tuple[ExcludedSub, ...] = (),
    *,
    column: str = "rc.component",
) -> tuple[str, list]:
    """Build the WHERE clause and params for a component group gold query.

    Returns (where_fragment, params) that filter whichever table the
    *column* belongs to (``recall_components`` by default, via
    ``rc.component``).  Pass e.g. ``column='r.component'`` to filter
    the ``recalls`` table instead.
    """
    placeholders = ",".join(["%s"] * len(categories))
    clause = f"TRIM(SPLIT_PART({column}, ':', 1)) IN ({placeholders})"
    params: list = list(categories)

    if excluded_subs:
        parts: list[str] = []
        for ex in excluded_subs:
            if ex.level3:
                parts.append(
                    f"(TRIM(SPLIT_PART({column}, ':', 2)) = %s"
                    f" AND TRIM(SPLIT_PART({column}, ':', 3)) = %s)"
                )
                params.extend([ex.level2, ex.level3])
            else:
                parts.append(f"TRIM(SPLIT_PART({column}, ':', 2)) = %s")
                params.append(ex.level2)
        clause += " AND NOT (" + " OR ".join(parts) + ")"

    return clause, params
