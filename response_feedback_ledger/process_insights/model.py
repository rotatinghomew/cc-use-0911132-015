"""输入装载、可见性判定与任务生命周期重建。

口径（见 fixtures/analysis/fields.md）：
- occurred_at 是业务发生时刻，决定流程先后；available_at 是系统可知时刻，
  决定某次复盘（给定 cutoff）能采用哪些证据。晚到事实在 available_at
  到达前不可见，但必须在输出中可解释，不允许静默丢弃。
- event_revisions.withdraw 撤回其 supersedes 指向的事件；撤回本身也受
  available_at 约束。
- rework_of 指向被重做的原任务，返工是同一步骤的再次尝试，不是新业务。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


def parse_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def hours(seconds: float) -> float:
    return round(seconds / 3600.0, 6)


@dataclass(slots=True)
class TaskModel:
    task_id: str
    case_id: str
    department: str
    depends_on: tuple[str, ...]
    rework_of: str | None
    start: datetime | None = None
    finish: datetime | None = None
    start_event_id: str | None = None
    finish_event_id: str | None = None
    # 生命周期重建中发现的证据问题（不阻断分析，进入 unresolved）
    issues: list[str] = field(default_factory=list)
    # 撤回/缺失状态标记
    finish_withdrawn: bool = False
    start_withdrawn: bool = False

    @property
    def is_rework(self) -> bool:
        return self.rework_of is not None

    @property
    def is_complete(self) -> bool:
        return self.start is not None and self.finish is not None

    @property
    def is_started_open(self) -> bool:
        return self.start is not None and self.finish is None


@dataclass(slots=True)
class PauseModel:
    task_id: str
    start: datetime
    end: datetime
    reason: str
    visible: bool


@dataclass(slots=True)
class CaseModel:
    case_id: str
    complexity: str
    opened_at: datetime
    available_at: datetime
    closed_at: datetime | None
    tasks: dict[str, TaskModel] = field(default_factory=dict)
    pauses: list[PauseModel] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def has_rework(self) -> bool:
        return any(task.is_rework for task in self.tasks.values())

    @property
    def collaboration(self) -> str:
        """协作结构：存在同一前置下的并行分支即为 parallel，否则 serial。"""
        successors: dict[str, int] = {}
        for task in self.tasks.values():
            for dep in task.depends_on:
                successors[dep] = successors.get(dep, 0) + 1
        return "parallel" if any(count > 1 for count in successors.values()) else "serial"


@dataclass(slots=True)
class VisibilityReport:
    cutoff: datetime
    invisible_events: list[dict[str, Any]] = field(default_factory=list)
    applied_withdrawals: list[dict[str, Any]] = field(default_factory=list)
    pending_withdrawals: list[dict[str, Any]] = field(default_factory=list)
    withdrawn_event_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class ProcessData:
    raw: Mapping[str, Any]
    as_of: datetime
    cutoffs: tuple[datetime, ...]
    unresolved_policy: str
    baseline_case_id: str | None
    source_versions: dict[str, str]
    cases: dict[str, CaseModel]
    all_events: tuple[Mapping[str, Any], ...]
    all_revisions: tuple[Mapping[str, Any], ...]

    def visible_evidence(self, cutoff: datetime) -> VisibilityReport:
        """按 cutoff 重建可见事件集合与撤回状态。"""
        report = VisibilityReport(cutoff=cutoff)
        withdrawn_ids: set[str] = set()
        for revision in self.all_revisions:
            record = {
                "revision_id": revision["revision_id"],
                "supersedes": revision["supersedes"],
                "kind": revision["kind"],
                "available_at": revision["available_at"],
            }
            if parse_at(revision["available_at"]) <= cutoff:
                withdrawn_ids.add(revision["supersedes"])
                report.applied_withdrawals.append(record)
            else:
                report.pending_withdrawals.append(record)
        for event in self.all_events:
            if parse_at(event["available_at"]) > cutoff:
                report.invisible_events.append(
                    {
                        "event_id": event["event_id"],
                        "task_id": event["task_id"],
                        "kind": event["kind"],
                        "occurred_at": event["occurred_at"],
                        "available_at": event["available_at"],
                        "lag_hours": hours(
                            (parse_at(event["available_at"]) - parse_at(event["occurred_at"])).total_seconds()
                        ),
                    }
                )
        report.withdrawn_event_ids = withdrawn_ids
        return report


def load_process_data(payload: Mapping[str, Any]) -> ProcessData:
    as_of = parse_at(payload["as_of"])
    analysis = payload.get("analysis", {})
    cutoffs = tuple(sorted(parse_at(value) for value in analysis.get("historical_cutoffs", ())))
    cases: dict[str, CaseModel] = {}
    for row in payload.get("cases", []):
        cases[row["case_id"]] = CaseModel(
            case_id=row["case_id"],
            complexity=row["complexity"],
            opened_at=parse_at(row["opened_at"]),
            available_at=parse_at(row["available_at"]),
            closed_at=parse_at(row.get("closed_at")),
        )
    for row in payload.get("tasks", []):
        case = cases[row["case_id"]]
        case.tasks[row["task_id"]] = TaskModel(
            task_id=row["task_id"],
            case_id=row["case_id"],
            department=row["department"],
            depends_on=tuple(row.get("depends_on") or ()),
            rework_of=row.get("rework_of"),
        )
    data = ProcessData(
        raw=payload,
        as_of=as_of,
        cutoffs=cutoffs,
        unresolved_policy=analysis.get("unresolved_policy", "retain"),
        baseline_case_id=payload.get("baseline_case_id"),
        source_versions=dict(payload.get("source_versions", {})),
        cases=cases,
        all_events=tuple(payload.get("events", [])),
        all_revisions=tuple(payload.get("event_revisions", [])),
    )
    return data


def reconstruct_tasks(data: ProcessData, visibility: VisibilityReport) -> dict[str, CaseModel]:
    """在给定可见性下重建每个任务的 start/finish 生命周期。

    返回新的 CaseModel 副本（不修改全量模型），并登记缺失、撤回、
    反向时序等证据问题。空值保持未知，绝不转成零。
    """
    withdrawn: set[str] = visibility.withdrawn_event_ids
    visible_events = [
        event
        for event in data.all_events
        if parse_at(event["available_at"]) <= visibility.cutoff
        and event["event_id"] not in withdrawn
    ]
    cases = {
        case_id: CaseModel(
            case_id=case.case_id,
            complexity=case.complexity,
            opened_at=case.opened_at,
            available_at=case.available_at,
            closed_at=case.closed_at,
        )
        for case_id, case in data.cases.items()
    }
    for case in data.cases.values():
        target = cases[case.case_id]
        for task in case.tasks.values():
            target.tasks[task.task_id] = TaskModel(
                task_id=task.task_id,
                case_id=task.case_id,
                department=task.department,
                depends_on=task.depends_on,
                rework_of=task.rework_of,
            )
        for pause in case.pauses:
            pass
    # 暂停记录没有独立 available_at 字段时，作为结构化事实在主分析可见，
    # 并在方法说明中注明；提供 available_at 时同样按 cutoff 过滤。
    task_owner_map: dict[str, CaseModel] = {
        task_id: case
        for case in cases.values()
        for task_id in case.tasks
    }
    for row in data.raw.get("pauses", []):
        pause_available = parse_at(row.get("available_at"))
        visible = pause_available is None or pause_available <= visibility.cutoff
        owner = task_owner_map.get(row["task_id"])
        if owner is not None and visible:
            owner.pauses.append(
                PauseModel(
                    task_id=row["task_id"],
                    start=parse_at(row["start"]),
                    end=parse_at(row["end"]),
                    reason=row.get("reason", "unspecified"),
                    visible=True,
                )
            )

    for event in visible_events:
        target_case = task_owner_map.get(event["task_id"])
        if target_case is None:
            continue
        task = target_case.tasks[event["task_id"]]
        if event["kind"] == "start":
            if task.start is not None:
                task.issues.append(
                    f"duplicate_start:{task.start_event_id}|{event['event_id']}"
                )
            task.start = parse_at(event["occurred_at"])
            task.start_event_id = event["event_id"]
        elif event["kind"] == "finish":
            if task.finish is not None:
                task.issues.append(
                    f"duplicate_finish:{task.finish_event_id}|{event['event_id']}"
                )
            task.finish = parse_at(event["occurred_at"])
            task.finish_event_id = event["event_id"]

    # 撤回事件（即使不可见也要在主模型标注其潜在影响由 visibility 负责）
    for event_id in withdrawn:
        target_case = next(
            (c for c in cases.values() if any(event_id in (t.start_event_id, t.finish_event_id) for t in c.tasks.values())),
            None,
        )
        if target_case is not None:
            for task in target_case.tasks.values():
                if task.finish_event_id == event_id:
                    task.finish = None
                    task.finish_event_id = None
                    task.finish_withdrawn = True
                if task.start_event_id == event_id:
                    task.start = None
                    task.start_event_id = None
                    task.start_withdrawn = True

    for case in cases.values():
        for task in case.tasks.values():
            if task.start is None and task.finish is not None:
                task.issues.append("finish_without_start")
            if task.start is not None and task.finish is not None and task.finish < task.start:
                task.issues.append("finish_before_start")
            for dep in task.depends_on:
                if dep not in case.tasks:
                    task.issues.append(f"unknown_dependency:{dep}")
                else:
                    parent = case.tasks[dep]
                    if task.start is not None and parent.finish is not None and task.start < parent.finish:
                        # 依赖未完成即开工：可能是抢跑，也可能是并行定义问题，保留并上报
                        task.issues.append(f"started_before_dependency_finish:{dep}")
        if case.closed_at is not None:
            unfinished = [t.task_id for t in case.tasks.values() if not t.is_complete]
            if unfinished:
                case.issues.append("closure_registered_with_open_tasks:" + ",".join(unfinished))
    return cases


def variant_key(case: CaseModel) -> str:
    """流程变体 = 协作结构 × 是否含返工回路。"""
    return f"{case.collaboration}{'-rework' if case.has_rework else ''}"
