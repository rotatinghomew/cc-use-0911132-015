import json
import unittest
from pathlib import Path


class AnalysisInputsTests(unittest.TestCase):
    def load_data(self):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json").read_text())

    def test_parallel_tasks(self):
        data = self.load_data()
        self.assertTrue(sum(row['depends_on'] == ['task-1-0'] for row in data['tasks']) == 2)

    def test_unfinished_cases(self):
        data = self.load_data()
        self.assertTrue(any(row['closed_at'] is None for row in data['cases']))

    def test_rework_link(self):
        data = self.load_data()
        self.assertTrue(any(row['rework_of'] for row in data['tasks']))

    def test_source_versions_and_cutoff(self):
        data = self.load_data()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["source_versions"])
        self.assertIn("+00:00", data["as_of"])
