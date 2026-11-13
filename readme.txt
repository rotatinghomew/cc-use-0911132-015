测试：python3 -m unittest discover -s tests/feedback_cycles -p 'test_*.py'
构建：python3 -m compileall -q response_feedback_ledger tests/feedback_cycles
验收：python3 -m response_feedback_ledger.ledger.cli fixtures/demo.json --database /tmp/response_feedback_ledger-acceptance.sqlite3
