"""流程瓶颈分析方法正确性测试。

覆盖业务说明中的反例，而不只是输出结构：
- 并行重叠不得在墙钟历时中重复累加；
- 返工是同步骤再尝试，不计为新业务；
- 撤回/晚到必须可解释，缺失事件不补零；
- 未闭环实例保留并右截尾，不能删除后宣称效率提升；
- 历时分解满足守恒关系，残差必须为 0；
- 证据不足的对比单元格禁止效率归因。
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from response_feedback_ledger.process_insights.analysis import build_process_insights
from response_feedback_ledger.process_insights.intervals import (
    normalize,
    overlap_seconds,
    partition,
    subtract,
    union_seconds,
)
from response_feedback_ledger.process_insights.model import parse_at

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json"
H = 3600.0


def load():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def accounting_by_id(result):
    return {row["case_id"]: row for row in result["duration_accounting"]["per_case"]}


def segments_by_id(result):
    return {row["case_id"]: row for row in result["duration_accounting"]["case_segments"]}


class IntervalTests(unittest.TestCase):
    def test_union_merges_overlap_once(self):
        t0 = datetime(2026, 1, 1, 0, 0)
        t1 = datetime(2026, 1, 1, 2, 0)
        t2 = datetime(2026, 1, 1, 4, 0)
        intervals = [(t0, t2), (t1, t2)]
        self.assertEqual(union_seconds(intervals), 4 * H)
        self.assertEqual(overlap_seconds(intervals), 2 * H)

    def test_adjacent_intervals_have_no_overlap(self):
        t0 = datetime(2026, 1, 1, 0, 0)
        t1 = datetime(2026, 1, 1, 1, 0)
        t2 = datetime(2026, 1, 1, 2, 0)
        merged = normalize([(t0, t1), (t1, t2)])
        self.assertEqual(union_seconds(merged), 2 * H)
        self.assertEqual(overlap_seconds([(t0, t1), (t1, t2)]), 0.0)

    def test_partition_conserves_window(self):
        t0 = datetime(2026, 1, 1, 0, 0)
        t1 = datetime(2026, 1, 1, 3, 0)
        t2 = datetime(2026, 1, 1, 5, 0)
        t3 = datetime(2026, 1, 1, 10, 0)
        window = (t0, t3)
        pieces = partition(window, [[(t0, t1)], [(t1, t2)], [], []])
        total = sum(union_seconds(layer) for layer in pieces)
        self.assertEqual(total, 10 * H)
        # 最后一层兜底剩余窗口
        self.assertEqual(union_seconds(pieces[-1]), 5 * H)

    def test_subtract_clips_blockers(self):
        t0 = datetime(2026, 1, 1, 0, 0)
        t1 = datetime(2026, 1, 1, 4, 0)
        t2 = datetime(2026, 1, 1, 1, 0)
        t3 = datetime(2026, 1, 1, 2, 0)
        remain = subtract([(t0, t1)], [(t2, t3)])
        self.assertEqual(union_seconds(remain), 3 * H)


class ConservationTests(unittest.TestCase):
    def setUp(self):
        self.result = build_process_insights(load())
        self.accounts = accounting_by_id(self.result)

    def test_every_case_satisfies_wall_clock_identity(self):
        for case_id, row in self.accounts.items():
            total = (
                row["processing_hours"] + row["external_wait_hours"]
                + row["queue_hours"] + row["undecomposed_hours"]
            )
            self.assertAlmostEqual(total, row["wall_clock_hours"], places=6, msg=case_id)
            self.assertEqual(row["residual_seconds"], 0.0, case_id)

    def test_overlap_reconciles_work_content_and_processing(self):
        for case_id, row in self.accounts.items():
            # work_content = 各任务毛处理时长之和；并行部分在墙钟只计一次
            self.assertAlmostEqual(
                row["work_content_hours"],
                row["processing_hours"] + row["overlap_hours"],
                places=6,
                msg=case_id,
            )

    def test_segment_timeline_tiles_window_without_gaps_or_double_count(self):
        for case_row in segments_by_id(self.result).values():
            window_start = parse_at(case_row["window"]["start"])
            window_end = parse_at(case_row["window"]["end"])
            cursor = window_start
            total = 0.0
            for segment in case_row["segments"]:
                self.assertEqual(parse_at(segment["start"]), cursor)
                cursor = parse_at(segment["end"])
                total += segment["hours"] * H
            self.assertEqual(cursor, window_end)
            self.assertAlmostEqual(total / H, (window_end - window_start).total_seconds() / H, places=6)


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.result = build_process_insights(load())
        self.accounts = accounting_by_id(self.result)

    def test_parallel_overlap_counted_once(self):
        # case-3/5/7/9/11：field 与 review 并行 2 小时
        for case_id in ("case-3", "case-5", "case-7", "case-9", "case-11"):
            row = self.accounts[case_id]
            self.assertEqual(row["overlap_hours"], 2.0)
            self.assertEqual(row["work_content_hours"], 6.0)
            self.assertEqual(row["wall_clock_hours"], 12.0)

    def test_parallel_pause_covered_by_other_worker_is_processing(self):
        # case-3 的 field 暂停 1 小时，但同期 review 在处理：实例级不是外部等待
        row = self.accounts["case-3"]
        self.assertEqual(row["external_wait_hours"], 0.0)
        task_rows = {t["task_id"]: t for t in segments_by_id(self.result)["case-3"]["task_accounts"]}
        # 任务级暂停仍然可追溯
        self.assertEqual(task_rows["task-3-1"]["external_wait_hours"], 1.0)
        self.assertIn("external_evidence", task_rows["task-3-1"]["pause_reasons"])


class ReworkTests(unittest.TestCase):
    def setUp(self):
        self.result = build_process_insights(load())

    def test_rework_is_variant_not_new_case(self):
        variants = {v["variant_id"]: v for v in self.result["variants"]}
        self.assertIn("parallel-rework", variants)
        rework_variant = variants["parallel-rework"]
        self.assertEqual(rework_variant["case_ids"], ["case-1"])
        loop = rework_variant["rework_loops"][0]
        self.assertEqual(loop["rework_task_id"], "task-rework")
        self.assertEqual(loop["rework_of"], "task-1-1")
        # 返工没有产生新实例
        self.assertEqual(rework_variant["case_count"], 1)

    def test_rework_extra_work_is_traceable(self):
        row = accounting_by_id(self.result)["case-1"]
        # 原始并行工作 6h（含 2h 重叠）+ 返工 3h
        self.assertEqual(row["work_content_hours"], 9.0)
        task_rows = {
            t["task_id"]: t
            for t in segments_by_id(self.result)["case-1"]["task_accounts"]
        }
        self.assertEqual(task_rows["task-rework"]["processing_hours"], 3.0)
        self.assertEqual(task_rows["task-rework"]["rework_of"], "task-1-1")

    def test_rework_critical_path_includes_loop(self):
        seg = segments_by_id(self.result)["case-1"]
        self.assertIn("task-rework", seg["critical_path_task_ids"])


class VisibilityTests(unittest.TestCase):
    def test_withdrawal_removes_finish_and_flags_conflict(self):
        payload = load()
        result = build_process_insights(payload)
        unresolved = {u["code"]: u for u in result["unresolved"]}
        self.assertIn("withdrawn_event", unresolved)
        self.assertEqual(unresolved["withdrawn_event"]["evidence"]["supersedes"], "event-2-2-finish")
        conflict = next(u for u in result["unresolved"] if u["code"] == "closure_evidence_conflict")
        self.assertEqual(conflict["case_id"], "case-2")
        # 撤回后任务无 finish，闭环登记等待被识别为排队而非假装任务完成
        case2 = accounting_by_id(result)["case-2"]
        self.assertEqual(case2["queue_hours"], 8.0)

    def test_historical_snapshot_hides_late_events(self):
        payload = load()
        result = build_process_insights(payload)
        first, second = result["visibility_snapshots"]
        self.assertEqual(first["cutoff"], "2026-08-06T00:00:00+00:00")
        invisible_ids = {e["event_id"] for e in first["not_yet_available_events"]}
        self.assertIn("rework-finish", invisible_ids)
        # 撤回在 8/21 才可见：8/6 快照挂起，8/31 快照生效
        self.assertEqual(len(first["pending_withdrawals"]), 1)
        self.assertEqual(len(second["applied_withdrawals"]), 1)

    def test_late_record_lag_is_explained(self):
        result = build_process_insights(load())
        late = next(u for u in result["unresolved"] if u["code"] == "late_available_records")
        evidence = late["evidence"]
        # 常规 1 小时记账滞后被归为常规带，110 小时返工回执单列异常
        self.assertEqual(evidence["routine_lag_band_hours"], 1.0)
        exceptional_ids = {e["event_id"] for e in evidence["exceptional_late_receipts"]}
        self.assertEqual(exceptional_ids, {"rework-finish"})
        rework_lag = next(e for e in evidence["exceptional_late_receipts"]
                          if e["event_id"] == "rework-finish")
        self.assertEqual(rework_lag["lag_hours"], 110.0)


class RightCensoringTests(unittest.TestCase):
    def test_unclosed_cases_retained_not_deleted(self):
        result = build_process_insights(load())
        accounts = accounting_by_id(result)
        self.assertEqual(len(accounts), 12)  # 全部 12 个实例都在
        for case_id in ("case-0", "case-4", "case-8"):
            row = accounts[case_id]
            self.assertGreater(row["undecomposed_hours"], 0.0)
        codes = {(u["code"], u.get("case_id")) for u in result["unresolved"]}
        for case_id in ("case-0", "case-4", "case-8"):
            self.assertIn(("right_censored_unclosed", case_id), codes)

    def test_unclosed_cases_excluded_from_closed_comparison_but_observed_parts_kept(self):
        result = build_process_insights(load())
        used = set()
        for cell in result["comparisons"]["by_variant"].values():
            used.update(cell.get("serial_cases", []))
        per_case = accounting_by_id(result)
        # 对比单元格的样本量只统计闭环无冲突实例
        total_closed = sum(1 for row in per_case.values() if row["wall_clock_hours"] == 12.0)
        self.assertEqual(total_closed, 9)


class MissingEventTests(unittest.TestCase):
    def _mini_payload(self):
        return {
            "schema_version": 1,
            "as_of": "2026-10-30T00:00:00+00:00",
            "source_versions": {"operations": "t1"},
            "analysis": {"historical_cutoffs": [], "unresolved_policy": "retain"},
            "baseline_case_id": None,
            "cases": [{
                "case_id": "c-orphan", "complexity": "serial",
                "opened_at": "2026-09-01T00:00:00+00:00",
                "available_at": "2026-09-01T00:00:00+00:00",
                "closed_at": "2026-09-01T06:00:00+00:00",
            }],
            "tasks": [
                {"task_id": "t-a", "case_id": "c-orphan", "department": "desk",
                 "depends_on": [], "rework_of": None},
                {"task_id": "t-b", "case_id": "c-orphan", "department": "field",
                 "depends_on": ["t-a"], "rework_of": None},
            ],
            "events": [
                {"event_id": "e-a-start", "task_id": "t-a", "kind": "start",
                 "occurred_at": "2026-09-01T00:00:00+00:00",
                 "available_at": "2026-09-01T00:00:00+00:00"},
                {"event_id": "e-a-finish", "task_id": "t-a", "kind": "finish",
                 "occurred_at": "2026-09-01T02:00:00+00:00",
                 "available_at": "2026-09-01T02:00:00+00:00"},
                # t-b 只有 finish 没有 start
                {"event_id": "e-b-finish", "task_id": "t-b", "kind": "finish",
                 "occurred_at": "2026-09-01T04:00:00+00:00",
                 "available_at": "2026-09-01T04:00:00+00:00"},
            ],
            "event_revisions": [],
            "pauses": [],
        }

    def test_orphan_finish_does_not_become_zero_or_guess(self):
        result = build_process_insights(self._mini_payload())
        issue = next(u for u in result["unresolved"] if u["code"] == "task_evidence_gap")
        self.assertEqual(issue["task_id"], "t-b")
        self.assertIn("finish_without_start", issue["evidence"]["issues"])
        row = accounting_by_id(result)["c-orphan"]
        # 守恒仍然成立，未知时间落入 undecomposed 而不是被猜成处理时长
        self.assertAlmostEqual(
            row["processing_hours"] + row["queue_hours"] + row["undecomposed_hours"],
            row["wall_clock_hours"], places=6,
        )
        self.assertEqual(row["processing_hours"], 2.0)
        self.assertGreater(row["undecomposed_hours"], 0.0)


class AttributionGuardTests(unittest.TestCase):
    def test_small_sample_cell_not_attributed(self):
        result = build_process_insights(load())
        serial_cell = result["comparisons"]["by_complexity"]["serial"]
        self.assertFalse(serial_cell["attribution_safe"])
        self.assertIn("caveat", serial_cell)

    def test_no_personal_ranking_fields(self):
        result = build_process_insights(load())
        # 允许在方法说明中显式声明“不做排名”，但禁止真正的排名字段
        for variant in result["variants"]:
            self.assertNotIn("rank", variant)
            self.assertNotIn("performance_score", variant)
        for row in result["stage_wait_profile"]:
            self.assertNotIn("rank", row)
        # 部门条目只允许是 desk/field/review/closure_registration 这类环节标识
        self.assertEqual(
            {row["stage"] for row in result["stage_wait_profile"]},
            {"desk", "field", "review", "closure_registration"},
        )

    def test_insight_id_deterministic_and_replayable(self):
        payload = load()
        first = build_process_insights(copy.deepcopy(payload))
        second = build_process_insights(copy.deepcopy(payload))
        self.assertEqual(first["insight_id"], second["insight_id"])
        self.assertTrue(first["insight_id"].startswith("insight-"))


class DatabaseLinkTests(unittest.TestCase):
    def test_link_marks_missing_baseline_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "empty.sqlite3"
            result = build_process_insights(load(), database=db)
            self.assertIsNone(result["evidence_link"]["baseline_run"])
            self.assertIn("link_note", result["evidence_link"])
            # 分析专属表幂等写入，不创建既有业务表
            import sqlite3
            with sqlite3.connect(db) as conn:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("process_insight_runs", tables)

    def test_existing_tables_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ops.sqlite3"
            from response_feedback_ledger.service import ApplicationService
            service = ApplicationService(db)
            service.execute({"case_id": "demo-process-20260802"}, request_key="k1")
            result = build_process_insights(load(), database=db)
            linked = result["evidence_link"]["baseline_run"]
            self.assertIsNotNone(linked)
            self.assertTrue(linked["replay_continuous"])
            self.assertEqual(linked["replay_stage_count"], 7)
            self.assertTrue(linked["closure_artifact_present"])


if __name__ == "__main__":
    unittest.main()
