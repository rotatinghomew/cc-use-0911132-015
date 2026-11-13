from __future__ import annotations
from typing import Any
from response_feedback_ledger.commands.runtime import RunState, SQLiteLedger, fingerprint, number, sequence

class ReplayCoordinator:
    artifact_key = 'replay_chain'

    def execute(self, state: RunState, stages: list[Any], ledger: SQLiteLedger) -> None:
        replay = []
        previous_hash = "genesis"
        for stage in stages:
            stage.apply(state)
            record = {"sequence": len(replay) + 1, "stage": stage.name, "revision": state.revision, "previous_hash": previous_hash, "artifact_hash": fingerprint(state.artifacts[stage.name])}
            record["event_hash"] = fingerprint(record)
            previous_hash = record["event_hash"]
            replay.append(record)
            ledger.save(state)
        continuity = all(item["previous_hash"] == ("genesis" if index == 0 else replay[index - 1]["event_hash"]) for index, item in enumerate(replay))
        state.artifacts[self.artifact_key] = {"events": replay, "head": previous_hash, "continuous": continuity, "signature": fingerprint(replay)}
        state.emit(self.artifact_key, "replay_chain_completed", continuous=continuity)
