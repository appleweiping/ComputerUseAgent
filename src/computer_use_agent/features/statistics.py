"""Deterministic task-stratified descriptive statistics shared by analyses."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any


def hedges_g(first: Sequence[float], second: Sequence[float]) -> float | None:
    """Return bias-corrected standardized ``first - second`` mean difference."""

    if len(first) < 2 or len(second) < 2:
        return None
    variance_first = statistics.variance(first)
    variance_second = statistics.variance(second)
    degrees = len(first) + len(second) - 2
    pooled_variance = (
        (len(first) - 1) * variance_first + (len(second) - 1) * variance_second
    ) / degrees
    if pooled_variance <= 0:
        return 0.0 if statistics.fmean(first) == statistics.fmean(second) else None
    correction = 1 - 3 / (4 * (len(first) + len(second)) - 9)
    return (
        correction
        * (statistics.fmean(first) - statistics.fmean(second))
        / math.sqrt(pooled_variance)
    )


def task_descriptive_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    feature_names: Sequence[str],
    task_field: str,
    group_field: str,
    group_collection_key: str,
    human_group: str = "human",
    task_aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Summarize each feature within task/group and compare human to all agents.

    The same implementation serves the private ScienceBoard three-actor report
    and the canonical binary detector result. ``task_aliases`` lets a public
    result replace source task identifiers without changing any statistic.
    """

    if not records:
        raise ValueError("Task descriptive statistics require at least one record")
    names = tuple(feature_names)
    if not names or len(names) != len(set(names)):
        raise ValueError("Task descriptive feature names must be unique and non-empty")
    tasks = sorted({str(record[task_field]) for record in records})
    groups = sorted({str(record[group_field]) for record in records})
    aliases = dict(task_aliases or {task: task for task in tasks})
    if set(aliases) != set(tasks) or len(set(aliases.values())) != len(tasks):
        raise ValueError("Task aliases must provide one unique value for every task")

    by_task: dict[str, Any] = {}
    for task in tasks:
        task_records = [record for record in records if str(record[task_field]) == task]
        group_summary: dict[str, Any] = {}
        for group in groups:
            subset = [record for record in task_records if str(record[group_field]) == group]
            group_summary[group] = (
                {
                    name: {
                        "mean": round(
                            statistics.fmean(float(record["features"][name]) for record in subset),
                            6,
                        ),
                        "median": round(
                            statistics.median(float(record["features"][name]) for record in subset),
                            6,
                        ),
                        "n": len(subset),
                    }
                    for name in names
                }
                if subset
                else {}
            )
        human = [record for record in task_records if str(record[group_field]) == human_group]
        agents = [record for record in task_records if str(record[group_field]) != human_group]
        effects: dict[str, float | None] = {}
        for name in names:
            effect = hedges_g(
                [float(record["features"][name]) for record in human],
                [float(record["features"][name]) for record in agents],
            )
            effects[name] = None if effect is None else round(effect, 6)
        by_task[aliases[task]] = {
            group_collection_key: group_summary,
            "human_minus_agent_hedges_g": effects,
        }
    return {"by_task": by_task}
