from __future__ import annotations
import json
import uuid
from pathlib import Path
from typing import Any, Mapping
from response_feedback_ledger.commands.action_order import ActionOrder
from response_feedback_ledger.receipts.level_receipt import LevelReceipt
from response_feedback_ledger.actions.field_event import FieldEvent
from response_feedback_ledger.evidence.custody_chain import CustodyChain
from response_feedback_ledger.exceptions.rework_queue import ReworkQueue
from response_feedback_ledger.closure.outcome_check import OutcomeCheck
from response_feedback_ledger.replay.incident_timeline import IncidentTimeline
from response_feedback_ledger.commands.runtime import DomainError, RunState, SQLiteLedger, fingerprint, number, stable_json
from response_feedback_ledger.replay.replay_coordinator import ReplayCoordinator

class ApplicationService:
    def __init__(self, database: str | Path):
        self.ledger = SQLiteLedger(database)
        self.stages = [ActionOrder(), LevelReceipt(), FieldEvent(), CustodyChain(), ReworkQueue(), OutcomeCheck(), IncidentTimeline()]
        self.workflow = ReplayCoordinator()

    def execute(self, payload: Mapping[str, Any], request_key: str | None = None) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise DomainError("payload must be an object")
        case_id = str(payload.get("case_id", "")).strip()
        if not case_id:
            self.ledger.fail("", "intake", "case_id is required", dict(payload))
            raise DomainError("case_id is required")
        key = request_key or fingerprint(payload)
        recalled = self.ledger.recalled(key)
        if recalled is not None:
            return recalled
        state = RunState(uuid.uuid4().hex, case_id, dict(payload))
        state.emit("received", "run_started", project='response_feedback_ledger', architecture='命令事件、证据保全与可重放闭环')
        try:
            self.workflow.execute(state, self.stages, self.ledger)
            state.advance("completed")
            state.artifacts["operational_summary"] = self._summary(state)
            self.ledger.save(state)
        except Exception as exc:
            self.ledger.fail(case_id, state.state, str(exc), dict(payload))
            raise
        response = {
            "project": 'response_feedback_ledger', "architecture": '命令事件、证据保全与可重放闭环', "run_id": state.run_id,
            "case_id": state.case_id, "state": state.state, "revision": state.revision,
            "finding_count": len(state.findings), "findings": [item.__dict__ if hasattr(item, "__dict__") else {"rule": item.rule, "stage": item.stage, "severity": item.severity, "message": item.message, "observed": item.observed, "expected": item.expected} for item in state.findings],
            "metrics": state.metrics, "artifacts": state.artifacts, "event_count": len(state.events),
            "storage": self.ledger.stats(),
        }
        self.ledger.remember(key, state.run_id, response)
        return response

    def _summary(self, state: RunState) -> dict[str, Any]:
        severities: dict[str, int] = {}
        for finding in state.findings:
            severities[finding.severity] = severities.get(finding.severity, 0) + 1
        return {
            "case_id": state.case_id,
            "completed_stages": [item.stage for item in state.findings] + list(state.artifacts),
            "severity_counts": severities,
            "metric_checksum": fingerprint(state.metrics)[:16],
            "artifact_checksum": fingerprint(state.artifacts)[:16],
            "priority": round(number(state.payload.get("attributes", {}).get("risk_score")), 3),
        }

    def execute_batch(self, records: list[Mapping[str, Any]]) -> dict[str, Any]:
        completed, failed = [], []
        for position, record in enumerate(records):
            try:
                completed.append(self.execute(record, f"batch:{position}:{fingerprint(record)}"))
            except Exception as exc:
                failed.append({"position": position, "case_id": str(record.get("case_id", "")), "error": str(exc)})
        return {"completed": completed, "failed": failed, "storage": self.ledger.stats()}

def run_fixture(path: str | Path, database: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    service = ApplicationService(database)
    if isinstance(payload, list):
        return service.execute_batch(payload)
    return service.execute(payload, "acceptance:" + fingerprint(payload))
