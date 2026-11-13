"""流程瓶颈分析主流程与输出装配。

设计原则：
- 不生成个人绩效排名，不改变指令下发流程，只读输入与既有 SQLite 证据；
- 未闭环实例一律保留（unresolved_policy=retain），以分析 cutoff 右截尾，
  绝不通过删除未闭环实例来“改善”效率数字；
- 并行用区间并集表达，重叠单独披露；返工保留 rework_of 链路，作为同一
  步骤的再次尝试而非新业务；
- 晚到与撤回按 available_at 解释，并提供历史 cutoff 快照证明可知性；
- 证据不足的对比只描述结构，不下效率归因结论。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from response_feedback_ledger.commands.runtime import fingerprint, stable_json
from response_feedback_ledger.process_insights.accounting import (
    CLOSURE_QUEUE_KEY,
    CaseAccounting,
    account_case,
)
from response_feedback_ledger.process_insights.intervals import hours
from response_feedback_ledger.process_insights.model import (
    CaseModel,
    ProcessData,
    VisibilityReport,
    iso,
    load_process_data,
    parse_at,
    reconstruct_tasks,
    variant_key,
)

# 每个对比单元格少于该样本量时，只给结构描述、不做效率归因
MIN_COMPARISON_SAMPLE = 3
INSIGHTS_TABLE = "process_insight_runs"


def _median_hours(values: Sequence[float]) -> float | None:
    return round(statistics.median(values) / 3600.0, 6) if values else None


def _summarize_durations(accounts: Sequence[CaseAccounting]) -> dict[str, Any]:
    if not accounts:
        return {"sample_size": 0}
    fields = (
        "wall_clock_seconds", "processing_seconds", "external_wait_seconds",
        "queue_seconds", "undecomposed_seconds", "overlap_seconds",
        "work_content_seconds", "critical_path_seconds",
    )
    summary: dict[str, Any] = {"sample_size": len(accounts)}
    for name in fields:
        values = [getattr(item, name) for item in accounts]
        summary[name.replace("_seconds", "_hours")] = {
            "median_hours": _median_hours(values),
            "max_hours": round(max(values) / 3600.0, 6),
            "mean_hours": round(statistics.fmean(values) / 3600.0, 6),
        }
    return summary


def _snapshot_at(data: ProcessData, cutoff: datetime) -> dict[str, Any]:
    """历史 cutoff 快照：证明晚到记录/撤回在当时为何不可见或已生效。"""
    visibility = data.visible_evidence(cutoff)
    cases = reconstruct_tasks(data, visibility)
    complete_tasks = sum(
        1 for case in cases.values() for task in case.tasks.values() if task.is_complete
    )
    open_tasks = sum(
        1 for case in cases.values() for task in case.tasks.values() if task.is_started_open
    )
    closed_cases = sum(1 for case in cases.values() if case.closed_at is not None)
    return {
        "cutoff": iso(cutoff),
        "visible_event_count": len(data.all_events) - len(visibility.invisible_events),
        "not_yet_available_events": [
            {
                "event_id": item["event_id"],
                "task_id": item["task_id"],
                "kind": item["kind"],
                "occurred_at": item["occurred_at"],
                "available_at": item["available_at"],
                "lag_hours": item["lag_hours"],
            }
            for item in visibility.invisible_events
        ],
        "applied_withdrawals": visibility.applied_withdrawals,
        "pending_withdrawals": visibility.pending_withdrawals,
        "reconstructed": {
            "closed_cases": closed_cases,
            "complete_tasks": complete_tasks,
            "started_open_tasks": open_tasks,
        },
    }


def _build_variants(cases: Mapping[str, CaseModel], accounts: Mapping[str, CaseAccounting]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for case_id, case in cases.items():
        grouped.setdefault(variant_key(case), []).append(case_id)
    variants = []
    for key in sorted(grouped):
        member_ids = sorted(grouped[key])
        member_accounts = [accounts[case_id] for case_id in member_ids]
        closed = [item for item in member_accounts if item.is_closed]
        rework_task_ids = sorted(
            task.task_id
            for case_id in member_ids
            for task in cases[case_id].tasks.values()
            if task.is_rework
        )
        rework_loops = [
            {
                "case_id": case_id,
                "rework_task_id": task.task_id,
                "rework_of": task.rework_of,
                "department": task.department,
                "start_event_id": task.start_event_id,
                "finish_event_id": task.finish_event_id,
            }
            for case_id in member_ids
            for task in cases[case_id].tasks.values()
            if task.is_rework
        ]
        variants.append({
            "variant_id": key,
            "collaboration_structure": key.split("-")[0],
            "has_rework_loop": bool(rework_task_ids),
            "rework_task_ids": rework_task_ids,
            "rework_loops": rework_loops,
            "case_ids": member_ids,
            "case_count": len(member_ids),
            "closed_case_count": len(closed),
            "right_censored_case_ids": [item.case_id for item in member_accounts if item.is_right_censored],
            "complexity_mix": _complexity_mix(member_accounts),
            "duration_summary_hours": _summarize_durations(closed),
        })
    return variants


def _complexity_mix(accounts: Sequence[CaseAccounting]) -> dict[str, int]:
    mix: dict[str, int] = {}
    for item in accounts:
        mix[item.complexity] = mix.get(item.complexity, 0) + 1
    return dict(sorted(mix.items()))


def _stage_bottlenecks(cases: Mapping[str, CaseModel], accounts: Mapping[str, CaseAccounting]) -> list[dict[str, Any]]:
    """环节等待画像（结构指标，不是部门绩效）。

    排队空窗按“等待哪个环节开工”归因到下游部门；闭环登记等待单列。
    只在闭环且无证据冲突的实例上统计，右截尾实例单列计数。
    """
    eligible = [a for a in accounts.values() if a.is_closed and not a.evidence_refs["case_issues"]]
    wait_by_stage: dict[str, list[float]] = {}
    closure_wait: list[float] = []
    critical_hits: dict[str, int] = {}
    for account in eligible:
        for label, seconds in account.queue_breakdown.items():
            if label == CLOSURE_QUEUE_KEY:
                closure_wait.append(seconds)
            else:
                department = label.split(":", 1)[1] if ":" in label else label
                wait_by_stage.setdefault(department, []).append(seconds)
        # 关键路径上的部门参与度（结构画像，非绩效）
        for task_id in account.critical_path_task_ids:
            department = cases[account.case_id].tasks[task_id].department
            critical_hits[department] = critical_hits.get(department, 0) + 1
    rows = []
    for stage in sorted(set(wait_by_stage) | {"closure_registration"}):
        if stage == "closure_registration":
            values = closure_wait
            attribution = "business_registration"
        else:
            values = wait_by_stage.get(stage, [])
            attribution = "waiting_for_department_to_start"
        total = sum(values)
        rows.append({
            "stage": stage,
            "attribution": attribution,
            "observed_case_count": len(values),
            "total_wait_hours": hours(total),
            "median_wait_hours_per_case": _median_hours(values),
            "critical_path_appearances": critical_hits.get(stage, 0),
            "note": "结构等待画像，不构成部门或个人绩效排名",
        })
    rows.sort(key=lambda row: row["total_wait_hours"], reverse=True)
    return rows


def _bottlenecks(cases: Mapping[str, CaseModel], accounts: Mapping[str, CaseAccounting]) -> list[dict[str, Any]]:
    findings = []
    stage_rows = _stage_bottlenecks(cases, accounts)
    closed_accounts = [a for a in accounts.values() if a.is_closed]

    # 1. 闭环登记延迟：墙钟的最大组成是否为登记空窗
    closure_total = sum(
        sum(seconds for label, seconds in a.queue_breakdown.items() if label == CLOSURE_QUEUE_KEY)
        for a in closed_accounts
    )
    if closure_total > 0:
        findings.append({
            "bottleneck_id": "closure-registration-lag",
            "kind": "queue",
            "scope": "closure_registration",
            "evidence": {
                "total_hours": hours(closure_total),
                "affected_cases": [
                    a.case_id for a in closed_accounts
                    if CLOSURE_QUEUE_KEY in a.queue_breakdown
                ],
            },
            "interpretation": "末任务完成到 closed_at 登记之间的等待是墙钟历时的最大可归因组成，属于业务登记滞后而非任一处置环节处理慢。",
            "confidence": "high" if closure_total > 0 else "low",
        })

    # 2. 外部等待（暂停原因可追溯）
    external_total = sum(a.external_wait_seconds for a in closed_accounts)
    if external_total > 0:
        findings.append({
            "bottleneck_id": "external-evidence-wait",
            "kind": "external_wait",
            "scope": "field",
            "evidence": {
                "total_hours": hours(external_total),
                "reasons": sorted({
                    reason for a in closed_accounts for reason in a.external_wait_breakdown
                }),
                "cases": [
                    {"case_id": a.case_id, "reasons": a.external_wait_breakdown}
                    for a in closed_accounts if a.external_wait_seconds > 0
                ],
            },
            "interpretation": "暂停区间对应外部证据等待，处置部门在该区间无处理动作，不能计入处理耗时。",
            "confidence": "high",
        })

    # 3. 返工回路：对比同协作结构无返工变体的墙钟
    rework_accounts = [a for a in closed_accounts if cases[a.case_id].has_rework]
    plain_parallel = [
        a for a in closed_accounts
        if not cases[a.case_id].has_rework and cases[a.case_id].collaboration == "parallel"
    ]
    if rework_accounts and plain_parallel:
        extra = statistics.fmean(a.wall_clock_seconds for a in rework_accounts) - statistics.fmean(
            a.wall_clock_seconds for a in plain_parallel
        )
        findings.append({
            "bottleneck_id": "rework-loop-extension",
            "kind": "rework",
            "scope": "field",
            "evidence": {
                "rework_cases": [a.case_id for a in rework_accounts],
                "comparison_variant": "parallel",
                "mean_wall_clock_delta_hours": hours(extra),
                "rework_work_content_hours": {
                    a.case_id: hours(sum(
                        t.processing_seconds for t in a.task_accounts if t.rework_of
                    ))
                    for a in rework_accounts
                },
            },
            "interpretation": "返工是同一步骤的第二次尝试，额外处理投入与回路等待可单独追溯；当前返工样本仅 1 例，方向性提示而非统计结论。",
            "confidence": "low",
        })

    # 4. 环节等待排序（结构）
    for row in stage_rows:
        if row["stage"] != "closure_registration" and row["total_wait_hours"] > 0:
            findings.append({
                "bottleneck_id": f"stage-wait:{row['stage']}",
                "kind": "queue",
                "scope": row["stage"],
                "evidence": row,
                "interpretation": "环节交接等待画像；受样本量与案件复杂度限制，不作为效率归因或排名。",
                "confidence": "medium" if row["observed_case_count"] >= MIN_COMPARISON_SAMPLE else "low",
            })
    return findings


def _comparisons(accounts: Mapping[str, CaseAccounting]) -> dict[str, Any]:
    closed = [a for a in accounts.values() if a.is_closed and not a.evidence_refs["case_issues"]]

    def cell(group_filter) -> dict[str, Any]:
        group = [a for a in closed if group_filter(a)]
        summary = _summarize_durations(group)
        summary["attribution_safe"] = len(group) >= MIN_COMPARISON_SAMPLE
        if not summary["attribution_safe"]:
            summary["caveat"] = f"闭环有效样本 {len(group)} < {MIN_COMPARISON_SAMPLE}，仅描述结构，不能做效率归因。"
        return summary

    by_complexity = {
        complexity: cell(lambda a, value=complexity: a.complexity == value)
        for complexity in sorted({a.complexity for a in closed})
    }
    by_collaboration = {
        structure: cell(lambda a, value=structure: a.variant.startswith(value))
        for structure in sorted({a.variant.split("-")[0] for a in closed})
    }
    by_variant = {
        variant: cell(lambda a, value=variant: a.variant == value)
        for variant in sorted({a.variant for a in closed})
    }
    return {
        "basis": "仅使用闭环且无证据冲突的实例；按任务复杂度与协作结构分层，不做部门绩效排名。",
        "min_attribution_sample": MIN_COMPARISON_SAMPLE,
        "by_complexity": by_complexity,
        "by_collaboration_structure": by_collaboration,
        "by_variant": by_variant,
        "parallelism_effect": _parallelism_effect(closed),
    }


def _parallelism_effect(closed: Sequence[CaseAccounting]) -> dict[str, Any]:
    serial = [a for a in closed if a.variant == "serial"]
    parallel = [a for a in closed if a.variant == "parallel"]
    effect: dict[str, Any] = {
                "serial_cases": [a.case_id for a in serial],
                "parallel_cases": [a.case_id for a in parallel],
            }
    if serial and parallel:
        overlap_share = statistics.fmean(
            a.overlap_seconds / a.wall_clock_seconds for a in parallel
        )
        effect.update({
            "mean_parallel_overlap_hours": hours(statistics.fmean(a.overlap_seconds for a in parallel)),
            "overlap_share_of_wall_clock": round(overlap_share, 6),
            "interpretation": "并行分支的重叠时间已在墙钟历时中只计一次；若把各任务时长相加会高估墙钟，高估量即 overlap_hours。",
        })
    return effect


def _unresolved(
    data: ProcessData,
    visibility: VisibilityReport,
    cases: Mapping[str, CaseModel],
    accounts: Mapping[str, CaseAccounting],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    # 右截尾未闭环
    for account in accounts.values():
        if account.is_right_censored:
            items.append({
                "code": "right_censored_unclosed",
                "case_id": account.case_id,
                "detail": "实例无 closed_at，窗口右端取分析 cutoff；已观察部分照常分解，在办尾巴计入 undecomposed 并标注，不参与闭环效率统计。",
                "evidence": {
                    "window_basis": account.window_basis,
                    "window_end": iso(account.window_end),
                    "open_tasks": [
                        t.task_id for t in account.task_accounts if t.status == "started_open"
                    ],
                    "not_started_tasks": [
                        t.task_id for t in account.task_accounts if t.status == "not_started"
                    ],
                    "undecomposed_hours": hours(account.undecomposed_seconds),
                },
            })

    # 闭环登记与任务证据冲突（撤回 finish 等）
    for case_id, case in cases.items():
        if case.issues:
            items.append({
                "code": "closure_evidence_conflict",
                "case_id": case_id,
                "detail": "case.closed_at 是业务登记，与任务事件证据冲突，保留为未解决状态，不把登记时刻当作真实完成时刻。",
                "evidence": {"issues": case.issues, "closed_at": iso(case.closed_at)},
            })

    # 任务级证据问题
    for case_id, case in cases.items():
        for task in case.tasks.values():
            if task.issues:
                items.append({
                    "code": "task_evidence_gap",
                    "case_id": case_id,
                    "task_id": task.task_id,
                    "detail": "任务生命周期事件缺失或时序异常，相关时间不补零、不猜测，交由 undecomposed 兜底。",
                    "evidence": {"issues": task.issues, "status": (
                        "complete" if task.is_complete else "started_open" if task.is_started_open
                        else "finish_without_start" if task.finish is not None else "not_started"
                    )},
                })

    # 晚到记录：业务先发生、系统后记账；主分析按 occurred_at 重建流程，
    # 可知性由历史 cutoff 快照证明。区分常规记账滞后与异常晚回执，避免
    # 大量 1 小时常规滞后淹没真正的长滞后证据。
    late_events = []
    for event in data.all_events:
        lag = (parse_at(event["available_at"]) - parse_at(event["occurred_at"])).total_seconds()
        if lag > 0:
            late_events.append({
                "event_id": event["event_id"],
                "task_id": event["task_id"],
                "occurred_at": event["occurred_at"],
                "available_at": event["available_at"],
                "lag_hours": hours(lag),
                "explanation": "业务先发生、系统后记账；主分析按 occurred_at 重建流程，可知性由历史 cutoff 快照证明。",
            })
    if late_events:
        lags = [item["lag_hours"] for item in late_events]
        routine_band = max(lags) if len(set(lags)) == 1 else statistics.median(lags)
        exceptional = [item for item in late_events if item["lag_hours"] > routine_band]
        items.append({
            "code": "late_available_records",
            "detail": "晚到记录在 available_at 之后才可采用，历史快照展示其在早期 cutoff 不可见。",
            "evidence": {
                "total_late_events": len(late_events),
                "routine_lag_band_hours": routine_band,
                "routine_event_count": sum(1 for value in lags if value <= routine_band),
                "exceptional_late_receipts": exceptional,
                "all_lag_hours": sorted(lags),
            },
        })

    # 撤回
    for revision in visibility.applied_withdrawals:
        items.append({
            "code": "withdrawn_event",
            "detail": "撤回在 available_at 生效，被撤回事件不参与生命周期重建，留下的证据空洞进入 undecomposed。",
            "evidence": revision,
        })
    for revision in visibility.pending_withdrawals:
        items.append({
            "code": "pending_withdrawal",
            "detail": "撤回指令在分析 cutoff 尚不可见，当前分析仍采用原事件；后续重放会改变结论，已标注。",
            "evidence": revision,
        })
    return items


def _link_database(database: str | Path, baseline_case_id: str | None, insight_id: str) -> dict[str, Any]:
    """只读关联同库既有 replay 与 closure 证据，并幂等登记分析记录。"""
    db_path = str(database)
    link: dict[str, Any] = {"database": db_path, "baseline_case_id": baseline_case_id}
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if {"runs", "events"}.issubset(tables):
            row = connection.execute(
                "SELECT run_id, state, artifacts, updated_at FROM runs WHERE case_id=? ORDER BY updated_at DESC LIMIT 1",
                (baseline_case_id,),
            ).fetchone() if baseline_case_id else None
            if row is not None:
                artifacts = json.loads(row["artifacts"])
                replay_chain = artifacts.get("replay_chain", {})
                closure = artifacts.get("outcome_check", {})
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE run_id=?", (row["run_id"],)
                ).fetchone()[0]
                link["baseline_run"] = {
                    "run_id": row["run_id"],
                    "state": row["state"],
                    "replay_head": replay_chain.get("head"),
                    "replay_continuous": replay_chain.get("continuous"),
                    "replay_stage_count": len(replay_chain.get("events", [])),
                    "closure_artifact_present": bool(closure),
                    "closure_score": closure.get("score"),
                    "event_count": int(event_count),
                }
            else:
                link["baseline_run"] = None
                link["link_note"] = "库中不存在 baseline_case_id 对应运行，证据关联标记为缺失而非伪造。"
        else:
            link["baseline_run"] = None
            link["link_note"] = "数据库缺少既有账本表，跳过 replay/closure 关联。"
        # 幂等登记（只新增分析专属表，不触碰既有表与处理器）
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {INSIGHTS_TABLE}("
            "insight_id TEXT PRIMARY KEY, input_fingerprint TEXT NOT NULL, "
            "as_of TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        connection.execute(
            f"INSERT OR IGNORE INTO {INSIGHTS_TABLE}(insight_id,input_fingerprint,as_of,created_at,payload) "
            "VALUES(?,?,?,?,?)",
            (insight_id, link.get("input_fingerprint", ""), link.get("as_of", ""),
             datetime.now().isoformat(timespec="seconds"), stable_json({"insight_id": insight_id})),
        )
    return link


def build_process_insights(payload: Mapping[str, Any], database: str | Path | None = None) -> dict[str, Any]:
    data = load_process_data(payload)
    input_fingerprint = fingerprint(payload)
    insight_id = "insight-" + input_fingerprint[:16]

    # 主分析视角：as_of（可知信息截止时刻，不是业务时间上界）
    main_visibility = data.visible_evidence(data.as_of)
    cases = reconstruct_tasks(data, main_visibility)
    accounts = {
        case_id: account_case(case, variant_key(case), data.as_of)
        for case_id, case in cases.items()
    }

    # 守恒校验：每个实例的分层之和必须等于墙钟窗口
    conservation = []
    for account in accounts.values():
        conservation.append({
            "case_id": account.case_id,
            "wall_clock_hours": hours(account.wall_clock_seconds),
            "processing_hours": hours(account.processing_seconds),
            "external_wait_hours": hours(account.external_wait_seconds),
            "queue_hours": hours(account.queue_seconds),
            "undecomposed_hours": hours(account.undecomposed_seconds),
            "overlap_hours": hours(account.overlap_seconds),
            "work_content_hours": hours(account.work_content_seconds),
            "residual_seconds": round(account.conservation_residual_seconds, 9),
            "identity": "wall_clock = processing + external_wait + queue + undecomposed; "
                        "work_content = processing_union_equiv + overlap",
        })
    max_residual = max(abs(a.conservation_residual_seconds) for a in accounts.values())

    result = {
        "insight_id": insight_id,
        "schema_version": 1,
        "generated_for": {
            "as_of": iso(data.as_of),
            "source_versions": data.source_versions,
            "unresolved_policy": data.unresolved_policy,
            "input_fingerprint": input_fingerprint,
        },
        "methodology": {
            "time_basis": "occurred_at 决定流程顺序，available_at 决定证据可知性；空值保持未知不转零。",
            "parallelism": "同一实例并行任务按区间并集计时，重叠秒数单独披露，不重复累加。",
            "rework": "rework_of 标记同步骤的再次尝试，保留回路证据，不计为新业务实例。",
            "right_censoring": "未闭环实例保留并以 as_of 右截尾，不删除、不假装完成。",
            "ranking_policy": "不产出个人或部门绩效排名，环节等待仅作结构画像。",
            "duration_unit": "hours（内部以秒计算，输出四舍五入到 6 位小数）。",
        },
        "variants": _build_variants(cases, accounts),
        "duration_accounting": {
            "conservation_identity": conservation[0]["identity"] if conservation else "",
            "max_conservation_residual_seconds": round(max_residual, 9),
            "per_case": conservation,
            "case_segments": [
                {
                    "case_id": account.case_id,
                    "variant": account.variant,
                    "window": {"start": iso(account.window_start), "end": iso(account.window_end),
                              "basis": account.window_basis},
                    "queue_breakdown_hours": {k: hours(v) for k, v in account.queue_breakdown.items()},
                    "external_wait_breakdown_hours": {
                        k: hours(v) for k, v in account.external_wait_breakdown.items()
                    },
                    "undecomposed_reasons_hours": {
                        k: hours(v) for k, v in account.undecomposed_reasons.items()
                    },
                    "critical_path_task_ids": account.critical_path_task_ids,
                    "critical_path_hours": hours(account.critical_path_seconds),
                    "segments": [
                        {
                            "label": segment.label,
                            "category": segment.category,
                            "start": iso(segment.start),
                            "end": iso(segment.end),
                            "hours": hours(segment.seconds),
                            "detail": segment.detail,
                        }
                        for segment in account.segments
                    ],
                    "task_accounts": [
                        {
                            "task_id": task.task_id,
                            "department": task.department,
                            "rework_of": task.rework_of,
                            "status": task.status,
                            "start_event_id": task.start_event_id,
                            "finish_event_id": task.finish_event_id,
                            "start": iso(task.start),
                            "finish": iso(task.finish),
                            "processing_hours": hours(task.processing_seconds),
                            "external_wait_hours": hours(task.external_wait_seconds),
                            "pause_reasons": task.pause_reasons,
                            "issues": task.issues,
                        }
                        for task in account.task_accounts
                    ],
                    "evidence_refs": account.evidence_refs,
                }
                for account in sorted(accounts.values(), key=lambda item: item.case_id)
            ],
        },
        "bottlenecks": _bottlenecks(cases, accounts),
        "stage_wait_profile": _stage_bottlenecks(cases, accounts),
        "comparisons": _comparisons(accounts),
        "unresolved": _unresolved(data, main_visibility, cases, accounts),
        "visibility_snapshots": [_snapshot_at(data, cutoff) for cutoff in data.cutoffs],
        "evidence_link": {},
    }

    if database is not None:
        link = _link_database(database, data.baseline_case_id, insight_id)
        link["input_fingerprint"] = input_fingerprint
        link["as_of"] = iso(data.as_of)
        result["evidence_link"] = link
    else:
        result["evidence_link"] = {
            "database": None,
            "baseline_case_id": data.baseline_case_id,
            "link_note": "未提供数据库，输出仅关联输入事件证据。",
            "input_fingerprint": input_fingerprint,
            "as_of": iso(data.as_of),
        }
    return result
