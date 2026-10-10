# Copyright 2026 The Flutter team. All rights reserved.
# Use of this source code is governed by a BSD-style license in the LICENSE file.
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report


def row(attempt, conclusion, run=10, sha='abc', job='testing_app'):
    return {'run_id': run, 'attempt': attempt, 'sha': sha, 'job': job,
            'test_conclusion': conclusion, 'started_at': '2026-10-05T01:00:00Z',
            'test_seconds': 2.0}


class RecoveryTests(unittest.TestCase):
    def test_failure_to_success(self):
        result = report.recoveries([row(2, 'success'), row(1, 'failure')])
        self.assertEqual([(r['failed_attempt'], r['passed_attempt']) for r in result], [(1, 2)])

    def test_different_runs_commits_jobs_do_not_match(self):
        for replacement in (row(2, 'success', run=11), row(2, 'success', sha='def'), row(2, 'success', job='other')):
            self.assertEqual(report.recoveries([row(1, 'failure'), replacement]), [])

    def test_setup_failure_not_a_test_failure(self):
        failed = {**row(1, 'skipped'), 'conclusion': 'failure'}
        self.assertEqual(report.recoveries([failed, row(2, 'success')]), [])

    def test_cancelled_skipped_and_pending_not_success(self):
        for conclusion in ('cancelled', 'skipped', None):
            self.assertEqual(report.recoveries([row(1, 'failure'), row(2, conclusion)]), [])

    def test_partial_rerun_and_cancelled_intermediate_attempt(self):
        result = report.recoveries([row(1, 'failure'), row(2, 'cancelled'), row(4, 'success')])
        self.assertEqual(result[0]['passed_attempt'], 4)

    def test_latest_failure_and_no_duplicate_recovery(self):
        result = report.recoveries([row(1, 'failure'), row(2, 'failure'), row(3, 'success'), row(4, 'success')])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['failed_attempt'], 2)


class FakeAPI:
    def __init__(self):
        self.calls = []

    def pages(self, path, key):
        self.calls.append(path)
        if key == 'workflow_runs':
            return iter([{'id': 10, 'run_attempt': 2, 'updated_at': datetime.now(timezone.utc).isoformat()}])
        attempt = int(path.split('/attempts/')[1].split('/')[0])
        return iter([{'name': 'testing_app', 'status': 'completed', 'conclusion': 'success',
                      'started_at': '2026-10-05T01:00:00Z', 'completed_at': '2026-10-05T01:00:05Z',
                      'html_url': 'https://github.com/example/repo/actions/runs/10',
                      'steps': [{'name': report.TEST_STEP, 'conclusion': 'failure' if attempt == 1 else 'success',
                                 'started_at': '2026-10-05T01:00:01Z', 'completed_at': '2026-10-05T01:00:04Z'}]}])

    def get(self, path):
        self.calls.append(path)
        return {'head_sha': 'abc', 'head_branch': 'main', 'event': 'push', 'created_at': '2026-10-05T01:00:00Z'}


class CollectionTests(unittest.TestCase):
    def test_fetch_every_attempt_and_idempotent_merge(self):
        history = {'repo': 'example/repo', 'schema_version': 1, 'records': [row(1, None)]}
        api = FakeAPI()
        report.collect(api, history, 365)
        report.collect(api, history, 365)
        self.assertEqual(len(history['records']), 2)
        self.assertEqual(len(report.recoveries(history['records'])), 1)
        self.assertIn('/actions/runs/10/attempts/1/jobs', api.calls)
        self.assertIn('/actions/runs/10/attempts/2/jobs', api.calls)
        self.assertEqual(history['records'][0]['test_seconds'], 3)

    def test_pagination(self):
        class Paged(report.GitHub):
            def get(self, path):
                return {'jobs': list(range(100)) if path.endswith('&page=1') else [100]}
        self.assertEqual(len(list(Paged('example/repo').pages('/jobs', 'jobs'))), 101)

    def test_preserve_old_history_when_no_new_runs(self):
        class Empty:
            def pages(self, *args):
                return iter([])
        history = {'records': [row(1, 'failure')]}
        report.collect(Empty(), history, 1)
        self.assertEqual(len(history['records']), 1)


class OutputTests(unittest.TestCase):
    def test_machine_events_and_hidden_skipped_tests(self):
        events = [{'type': 'suite', 'suite': {'id': 1, 'path': 'test/a.dart'}},
                  {'type': 'testStart', 'test': {'id': 1, 'suiteID': 1, 'name': 'slow'}, 'time': 10},
                  {'type': 'testDone', 'testID': 1, 'time': 2010, 'result': 'success'},
                  {'type': 'testStart', 'test': {'id': 2, 'suiteID': 1, 'name': 'loader'}, 'time': 0},
                  {'type': 'testDone', 'testID': 2, 'time': 9999, 'result': 'success', 'hidden': True},
                  {'type': 'testStart', 'test': {'id': 3, 'suiteID': 1, 'name': 'skip'}, 'time': 0},
                  {'type': 'testDone', 'testID': 3, 'time': 1, 'result': 'success', 'skipped': True}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            path.write_text('non-json Flutter output\n[]\nnull\n' + '\n'.join(json.dumps(e) for e in events))
            self.assertEqual(report.machine_tests(path), [{'suite': 'test/a.dart', 'name': 'slow', 'result': 'success', 'seconds': 2}])

    def test_render_zero_and_nonzero_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            history = {'repo': 'example/repo', 'schema_version': 1, 'records': []}
            report.render(history, output)
            self.assertIn('0 job attempts', (output / 'report.html').read_text())
            history['records'] = [row(1, 'failure'), row(2, 'success')]
            report.render(history, output)
            self.assertIn('attempt 1 → 2', (output / 'report.html').read_text())
            self.assertIn('2026-10-05,1,1,0,1', (output / 'weekly.csv').read_text())


if __name__ == '__main__':
    unittest.main()
