"""单实例墙钟历时的守恒分解。

分解层级（按优先级互斥切分同一窗口 [opened_at, 窗口右端)）：
1. processing 实际处理：任务 start→finish 区间并集，扣除落在其中的暂停；
2. external_wait 外部等待：暂停区间（如等待外部证据），原因随区间保留；
3. queue 排队/交接等待：前置任务完成到后继任务开工之间的空窗、
   实例开启到首个任务开工的空窗、末任务完成到闭环登记的空窗；
4. undecomposed 无法分解：无足够事件证据的剩余时间（含右截尾的
   在办尾巴、只有 finish 没有 start 的孤儿区间、撤回后的证据空洞）。

守恒关系（每个实例都可对账到原始事件）：
    wall_clock_seconds
      = processing + external_wait + queue + undecomposed
    work_content_seconds（含返工的实际投入，并行不重复计）
      = processing（处理并集） + 并行重叠秒数 overlap_seconds
即 work_content = 各任务处理时长之和；wall_clock 与 work_content 的差
就是并行重叠，重叠在 wall_clock 里只计一次，绝不靠平均值相加凑总数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from response_feedback_ledger.process_insights.intervals import (
    Interval,
    normalize,
    overlap_seconds,
    partition,
    subtract,
    union_seconds,
)
from response_feedback_ledger.process_insights.model import CaseModel, TaskModel

CLOSURE_QUEUE_KEY = "closure_registration"
INTAKE_QUEUE_KEY = "intake"
OPEN_TAIL_REASON = "right_censored_open_tail"
ORPHAN_REASON = "orphan_finish_without_start"
WITHDRAWN_REASON = "evidence_withdrawn"


@dataclass(slots=True)
class TaskAccount:
    task_id: str
    department: str
    rework_of: str | None
    start: datetime | None
    finish: datetime | None
    start_event_id: str | None
    finish_event_id: str | None
    processing_seconds: float
    external_wait_seconds: float
    pause_reasons: list[str]
    status: str
    issues: list[str]


@dataclass(slots=True)
class Segment:
    label: str
    category: str
    start: datetime
    end: datetime
    seconds: float
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CaseAccounting:
    case_id: str
    variant: str
    complexity: str
    window_start: datetime
    window_end: datetime
    window_basis: str
    is_closed: bool
    is_right_censored: bool
    wall_clock_seconds: float
    processing_seconds: float
    external_wait_seconds: float
    queue_seconds: float
    undecomposed_seconds: float
    overlap_seconds: float
    work_content_seconds: float
    conservation_residual_seconds: float
    queue_breakdown: dict[str, float]
    external_wait_breakdown: dict[str, float]
    undecomposed_reasons: dict[str, float]
    task_accounts: list[TaskAccount]
    segments: list[Segment]
    critical_path_task_ids: list[str]
    critical_path_seconds: float
    evidence_refs: dict[str, Any]


def _task_processing_intervals(task: TaskModel, pauses: list) -> tuple[list[Interval], list[Interval], list[str]]:
    """返回（净处理区间, 落在处理区间内的暂停区间, 暂停原因）。"""
    if task.start is None or task.finish is None:
        return [], [], []
    raw = [(task.start, task.finish)]
    pause_intervals = [
        (max(pause.start, task.start), min(pause.end, task.finish))
        for pause in pauses
        if pause.task_id == task.task_id and pause.end > task.start and pause.start < task.finish
    ]
    pause_intervals = [(s, e) for s, e in pause_intervals if e > s]
    reasons = sorted({pause.reason for pause in pauses if pause.task_id == task.task_id})
    net = subtract(raw, pause_intervals)
    return net, normalize(pause_intervals), reasons


def _resolve_pause_reason(interval: Interval, pause_records: list[tuple[Interval, str]]) -> str:
    """按区间中点落在哪个原始暂停区间来解析原因（合并区间也适用）。"""
    midpoint = interval[0] + (interval[1] - interval[0]) / 2
    for (start, end), reason in pause_records:
        if start <= midpoint < end:
            return reason
    overlapping = [reason for (start, end), reason in pause_records if end > interval[0] and start < interval[1]]
    return overlapping[0] if overlapping else "unspecified"


def _critical_path(case: CaseModel) -> tuple[list[str], float]:
    """在任务依赖 DAG 上求最长处理路径（返工尝试各自计入）。

    处理时长取 start→finish 毛时长（含任务内暂停），与墙钟关键路径
    口径一致；证据不足的任务按 0 时长参与但仍保留在路径候选中。
    """
    memo: dict[str, tuple[float, list[str]]] = {}

    def weight(task: TaskModel) -> float:
        if task.start is not None and task.finish is not None:
            return max(0.0, (task.finish - task.start).total_seconds())
        return 0.0

    def visit(task_id: str) -> tuple[float, list[str]]:
        if task_id in memo:
            return memo[task_id]
        task = case.tasks[task_id]
        best_duration = 0.0
        best_path: list[str] = []
        for dep in task.depends_on:
            if dep in case.tasks:
                dep_duration, dep_path = visit(dep)
                if dep_duration > best_duration:
                    best_duration, best_path = dep_duration, dep_path
        result = (best_duration + weight(task), best_path + [task_id])
        memo[task_id] = result
        return result

    overall_duration = 0.0
    overall_path: list[str] = []
    for task_id in case.tasks:
        duration, path = visit(task_id)
        if duration > overall_duration:
            overall_duration, overall_path = duration, path
    return overall_path, overall_duration


def account_case(case: CaseModel, variant: str, analysis_cutoff: datetime) -> CaseAccounting:
    """对单个实例做可追溯的历时分解。"""
    task_intervals: list[Interval] = []
    external_intervals: list[Interval] = []
    pause_records: list[tuple[Interval, str]] = []
    task_accounts: list[TaskAccount] = []
    processing_by_task: dict[str, list[Interval]] = {}

    for task_id in sorted(case.tasks):
        task = case.tasks[task_id]
        net, pauses_on_task, reasons = _task_processing_intervals(task, case.pauses)
        processing_by_task[task_id] = net
        task_intervals.extend(net)
        for interval in pauses_on_task:
            external_intervals.append(interval)
        for pause in case.pauses:
            if pause.task_id == task_id:
                pause_records.append(
                    ((max(pause.start, task.start or pause.start),
                      min(pause.end, task.finish or pause.end)), pause.reason)
                )
        task_accounts.append(
            TaskAccount(
                task_id=task.task_id,
                department=task.department,
                rework_of=task.rework_of,
                start=task.start,
                finish=task.finish,
                start_event_id=task.start_event_id,
                finish_event_id=task.finish_event_id,
                processing_seconds=union_seconds(net),
                external_wait_seconds=union_seconds(pauses_on_task),
                pause_reasons=reasons,
                status=(
                    "complete" if task.is_complete
                    else "started_open" if task.is_started_open
                    else "finish_without_start" if task.finish is not None
                    else "not_started"
                ),
                issues=list(task.issues),
            )
        )

    processing_union = normalize(task_intervals)
    # 实例级外部等待：暂停区间中没有任何其他任务在处理的部分。
    # 并行任务仍在干活的时段，实例墙钟上属于有效处理而非外部等待。
    external_union = normalize(subtract(external_intervals, processing_union))

    # 排队/交接空窗：逐任务根据依赖完成时刻与自身开工时刻推导
    queue_segments: list[Segment] = []
    orphan_finish_task_ids: list[str] = []
    for task_id in sorted(case.tasks):
        task = case.tasks[task_id]
        if task.start is None:
            if task.finish is not None:
                # 只有 finish 没有 start：时长无法重建，标记为孤儿证据，
                # 相关窗口时间由 undecomposed 兜底，绝不猜测处理时长。
                orphan_finish_task_ids.append(task_id)
            continue
        if not task.depends_on:
            ready_at = case.opened_at
            wait_key = INTAKE_QUEUE_KEY
        else:
            finishes = [case.tasks[d].finish for d in task.depends_on if case.tasks[d].finish is not None]
            missing = [d for d in task.depends_on if case.tasks[d].finish is None]
            if finishes:
                ready_at = max(finishes)
            else:
                continue  # 前置均无完成证据，空窗无法可靠归因，留给 undecomposed 兜底
            wait_key = "handoff:" + "|".join(sorted(set(
                case.tasks[d].department for d in task.depends_on if d in case.tasks
            )))
            if missing:
                wait_key += "|missing:" + ",".join(missing)
        if task.start > ready_at:
            queue_segments.append(
                Segment(wait_key, "queue", ready_at, task.start,
                        (task.start - ready_at).total_seconds(),
                        {"task_id": task_id, "department": task.department})
            )

    # 闭环登记空窗（末个完成事件 → closed_at）
    closure_segment: Segment | None = None
    all_finishes = [t.finish for t in case.tasks.values() if t.finish is not None]
    if case.closed_at is not None and all_finishes:
        last_finish = max(all_finishes)
        if case.closed_at > last_finish:
            closure_segment = Segment(
                CLOSURE_QUEUE_KEY, "queue", last_finish, case.closed_at,
                (case.closed_at - last_finish).total_seconds(),
                {"last_finish_event_id": next(
                    t.finish_event_id for t in case.tasks.values() if t.finish == last_finish
                )},
            )
    queue_intervals = normalize([(s.start, s.end) for s in queue_segments])
    if closure_segment is not None:
        queue_segments.append(closure_segment)
        queue_intervals = normalize(queue_intervals + [(closure_segment.start, closure_segment.end)])

    # 窗口右端：闭环实例取 closed_at；未闭环取分析 cutoff（右截尾，不删实例）
    observed_end = max(
        [t.finish for t in case.tasks.values() if t.finish is not None]
        + [t.start for t in case.tasks.values() if t.start is not None]
        + [case.opened_at]
    )
    is_closed = case.closed_at is not None
    if is_closed:
        window_end = case.closed_at
        window_basis = "case.closed_at"
    else:
        window_end = max(analysis_cutoff, observed_end)
        window_basis = "analysis_cutoff_right_censored"
    window: Interval = (case.opened_at, window_end)

    # 撤回导致的证据空洞：登记为零长度标记，实际空洞由 undecomposed 兜底
    withdrawn_tasks = [t for t in case.tasks.values() if t.finish_withdrawn or t.start_withdrawn]

    # 守恒切分：处理 → 外部等待 → 排队 → 剩余全部 undecomposed（末层兜底）
    layers = [processing_union, external_union, queue_intervals, []]
    pieces = partition(window, layers)
    proc_pieces, ext_pieces, queue_pieces, undecomposed_pieces = pieces

    segments: list[Segment] = []
    for start, end in proc_pieces:
        owners = sorted({
            task.task_id for task in case.tasks.values()
            for s, e in processing_by_task.get(task.task_id, [])
            if e > start and s < end
        })
        segments.append(Segment("processing", "processing", start, end,
                                (end - start).total_seconds(), {"task_ids": owners}))
    for start, end in ext_pieces:
        reason = _resolve_pause_reason((start, end), pause_records)
        segments.append(Segment("external_wait", "external_wait", start, end,
                                (end - start).total_seconds(), {"reason": reason}))
    queue_lookup = {(s.start, s.end): s.label for s in queue_segments}
    for start, end in queue_pieces:
        label = queue_lookup.get((start, end), "queue")
        segments.append(Segment(label, "queue", start, end,
                                (end - start).total_seconds(), {}))
    for start, end in undecomposed_pieces:
        reason = OPEN_TAIL_REASON if not is_closed else "unattributed_gap"
        detail: dict[str, Any] = {}
        if not is_closed:
            open_tasks = [t.task_id for t in case.tasks.values() if t.is_started_open]
            not_started = [t.task_id for t in case.tasks.values()
                           if t.start is None and t.finish is None]
            detail = {"open_tasks": open_tasks, "not_started_tasks": not_started}
        segments.append(Segment(reason, "undecomposed", start, end,
                                (end - start).total_seconds(), detail))
    segments.sort(key=lambda item: item.start)

    processing_seconds = union_seconds(proc_pieces)
    external_seconds = union_seconds(ext_pieces)
    queue_seconds = union_seconds(queue_pieces)
    undecomposed_seconds = union_seconds(undecomposed_pieces)
    wall_clock_seconds = (window_end - case.opened_at).total_seconds()

    # 并行重叠：毛工作投入与墙钟处理并集之差
    raw_work = sum(
        max(0.0, (t.finish - t.start).total_seconds())
        for t in case.tasks.values() if t.start is not None and t.finish is not None
    )
    overlap = overlap_seconds(
        [(t.start, t.finish) for t in case.tasks.values()
         if t.start is not None and t.finish is not None]
    )

    queue_breakdown: dict[str, float] = {}
    for segment in queue_segments:
        queue_breakdown[segment.label] = round(
            queue_breakdown.get(segment.label, 0.0) + segment.seconds, 6
        )
    external_breakdown: dict[str, float] = {}
    for interval in external_union:
        reason = _resolve_pause_reason(interval, pause_records)
        external_breakdown[reason] = round(
            external_breakdown.get(reason, 0.0) + (interval[1] - interval[0]).total_seconds(), 6
        )
    undecomposed_reasons: dict[str, float] = {}
    for segment in segments:
        if segment.category == "undecomposed":
            undecomposed_reasons[segment.label] = round(
                undecomposed_reasons.get(segment.label, 0.0) + segment.seconds, 6
            )
    # 孤儿 finish 与撤回是证据问题标记：对应时间已计入上面的 undecomposed 段，
    # 这里保留零秒原因键，保证输出能显式说明空洞来源。
    if orphan_finish_task_ids:
        undecomposed_reasons.setdefault(ORPHAN_REASON, 0.0)
    if withdrawn_tasks:
        undecomposed_reasons.setdefault(WITHDRAWN_REASON, 0.0)

    critical_path, critical_seconds = _critical_path(case)

    residual = wall_clock_seconds - (
        processing_seconds + external_seconds + queue_seconds + undecomposed_seconds
    )

    return CaseAccounting(
        case_id=case.case_id,
        variant=variant,
        complexity=case.complexity,
        window_start=case.opened_at,
        window_end=window_end,
        window_basis=window_basis,
        is_closed=is_closed,
        is_right_censored=not is_closed,
        wall_clock_seconds=wall_clock_seconds,
        processing_seconds=processing_seconds,
        external_wait_seconds=external_seconds,
        queue_seconds=queue_seconds,
        undecomposed_seconds=undecomposed_seconds,
        overlap_seconds=overlap,
        work_content_seconds=raw_work,
        conservation_residual_seconds=residual,
        queue_breakdown=queue_breakdown,
        external_wait_breakdown=external_breakdown,
        undecomposed_reasons=undecomposed_reasons,
        task_accounts=task_accounts,
        segments=segments,
        critical_path_task_ids=critical_path,
        critical_path_seconds=critical_seconds,
        evidence_refs={
            "event_ids": sorted(
                ref for task in case.tasks.values()
                for ref in (task.start_event_id, task.finish_event_id) if ref
            ),
            "pause_count": len(case.pauses),
            "withdrawn_task_ids": [t.task_id for t in withdrawn_tasks],
            "orphan_finish_task_ids": orphan_finish_task_ids,
            "task_issues": {t.task_id: t.issues for t in case.tasks.values() if t.issues},
            "case_issues": list(case.issues),
        },
    )
