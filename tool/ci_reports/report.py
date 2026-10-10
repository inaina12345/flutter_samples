# Copyright 2026 The Flutter team. All rights reserved.
# Use of this source code is governed by a BSD-style license in the LICENSE file.
"""Read-only GitHub Actions attempt collection and cumulative Plotly reports."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import html
import io
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

WORKFLOW = 'flutter-test-report.yml'
TEST_STEP = 'Run Flutter tests'


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def seconds(start, end):
    return max(0, (timestamp(end) - timestamp(start)).total_seconds()) if start and end else None


class GitHub:
    def __init__(self, repo):
        self.base = f'https://api.github.com/repos/{repo}'
        self.token = os.environ.get('GITHUB_TOKEN')

    def request(self, path):
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'flutter-ci-reports'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        # Never forward GitHub credentials to artifact storage on redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(
                    urllib.request.Request(self.base + path, headers=headers), timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 302:
                location = error.headers['Location']
                if urllib.parse.urlparse(location).scheme != 'https':
                    raise ValueError('Artifact redirect must use HTTPS')
                with urllib.request.urlopen(location, timeout=30) as response:
                    return response.read()
            raise RuntimeError(f'GitHub API {error.code} for {path}; check Actions read permission/rate limit') from error

    def get(self, path):
        return json.loads(self.request(path))

    def pages(self, path, key):
        page = 1
        while True:
            separator = '&' if '?' in path else '?'
            data = self.get(f'{path}{separator}per_page=100&page={page}')[key]
            yield from data
            if len(data) < 100:
                break
            page += 1

    def archive_json(self, artifact):
        with zipfile.ZipFile(io.BytesIO(self.request(f"/actions/artifacts/{artifact['id']}/zip"))) as archive:
            return json.loads(archive.read('history.json'))


def collect(api, history, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records = {(r['run_id'], r['attempt'], r['job']): r for r in history['records']}
    # No created filter: avoids GitHub's 1000-result search cap and includes old reruns.
    for run in api.pages(f'/actions/workflows/{WORKFLOW}/runs', 'workflow_runs'):
        if timestamp(run['updated_at']) < cutoff:
            continue
        for attempt in range(1, run['run_attempt'] + 1):
            detail = api.get(f"/actions/runs/{run['id']}/attempts/{attempt}")
            for job in api.pages(f"/actions/runs/{run['id']}/attempts/{attempt}/jobs", 'jobs'):
                step = next((s for s in job.get('steps', []) if s['name'] == TEST_STEP), {})
                record = {
                    'run_id': run['id'], 'attempt': attempt, 'sha': detail['head_sha'],
                    'branch': detail.get('head_branch'), 'event': detail['event'],
                    'job': job['name'], 'status': job['status'], 'conclusion': job['conclusion'],
                    'test_conclusion': step.get('conclusion'),
                    'started_at': job.get('started_at') or detail['created_at'],
                    'duration_seconds': seconds(job.get('started_at'), job.get('completed_at')),
                    'test_seconds': seconds(step.get('started_at'), step.get('completed_at')),
                    'url': job['html_url'],
                }
                records[(run['id'], attempt, job['name'])] = record
    history['records'] = sorted(records.values(), key=lambda r: (r['run_id'], r['attempt'], r['job']))
    history['collected_at'] = datetime.now(timezone.utc).isoformat()
    return history


def recoveries(records):
    """Compare actual executions of the same run, SHA and job, including partial reruns."""
    groups = {}
    for r in records:
        groups.setdefault((r['run_id'], r['sha'], r['job']), []).append(r)
    result = []
    for rows in groups.values():
        failed = None
        for row in sorted(rows, key=lambda r: r['attempt']):
            if row['test_conclusion'] == 'failure':
                failed = row
            elif row['test_conclusion'] == 'success' and failed is not None:
                result.append({**row, 'failed_attempt': failed['attempt'], 'passed_attempt': row['attempt']})
                failed = None
    return result


def machine_tests(path):
    """Reporter wall times, not CPU time. Ignore hidden loader tests and skipped tests."""
    tests, suites, completed = {}, {}, []
    for line in Path(path).read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind == 'suite':
            suites[event['suite']['id']] = event['suite'].get('path', '')
        elif kind == 'testStart':
            tests[event['test']['id']] = (event['test'], event['time'])
        elif kind == 'testDone' and event.get('testID') in tests:
            test, start = tests[event['testID']]
            if event.get('hidden') or event.get('skipped'):
                continue
            completed.append({'suite': suites.get(test['suiteID'], ''), 'name': test['name'],
                              'result': event['result'], 'seconds': max(0, event['time'] - start) / 1000})
    return sorted(completed, key=lambda t: t['seconds'], reverse=True)


def write_csv(path, rows):
    if not rows:
        path.write_text('')
        return
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def render(history, output, tests=None):
    import plotly.graph_objects as go
    output.mkdir(parents=True, exist_ok=True)
    records = history['records']
    candidates = recoveries(records)
    (output / 'history.json').write_text(json.dumps(history, indent=2) + '\n')
    write_csv(output / 'attempts.csv', records)
    write_csv(output / 'recoveries.csv', candidates)
    weeks = {}
    for row in records:
        day = timestamp(row['started_at']).astimezone(timezone.utc).date()
        week = str(day - timedelta(days=day.weekday()))
        counts = weeks.setdefault(week, {'success': 0, 'failure': 0, 'other': 0, 'recoveries': 0})
        conclusion = row['test_conclusion']
        counts[conclusion if conclusion in ('success', 'failure') else 'other'] += 1
    for row in candidates:
        day = timestamp(row['started_at']).astimezone(timezone.utc).date()
        weeks[str(day - timedelta(days=day.weekday()))]['recoveries'] += 1
    weekly = [{'week_utc': week, **weeks[week]} for week in sorted(weeks)]
    write_csv(output / 'weekly.csv', weekly)
    figure = go.Figure()
    for label in ('success', 'failure', 'other', 'recoveries'):
        figure.add_trace(go.Bar(name=label, x=[r['week_utc'] for r in weekly], y=[r[label] for r in weekly]))
    figure.update_layout(title='Weekly test-step outcomes and FAIL → PASS candidates (UTC)', barmode='group')
    trend = go.Figure()
    for job in sorted({r['job'] for r in records}):
        rows = sorted((r for r in records if r['job'] == job and r['test_seconds'] is not None),
                      key=lambda r: r['started_at'])
        trend.add_trace(go.Scatter(name=job, x=[r['started_at'] for r in rows],
                                  y=[r['test_seconds'] for r in rows], mode='lines+markers'))
    trend.update_layout(title='Test-step wall time by attempt', yaxis_title='Seconds')
    def escaped(value):
        return html.escape(str(value), quote=True)
    items = ''.join(f"<li>Run {r['run_id']}: {escaped(r['job'])}, attempt {r['failed_attempt']} → {r['passed_attempt']}</li>"
                    for r in candidates)
    slow = ''
    if tests is not None:
        write_csv(output / 'test-durations.csv', tests)
        slow = '<h2>Slow tests from supplied machine events</h2><ol>' + ''.join(
            f"<li>{escaped(t['suite'])} / {escaped(t['name'])}: {t['seconds']:.3f}s ({escaped(t['result'])})</li>"
            for t in tests[:20]) + '</ol>'
    body = f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Flutter CI report</title>
<h1>Flutter CI report: {escaped(history['repo'])}</h1>
<p>{len(records)} job attempts; {len(candidates)} test-step recovery candidates.</p>
<p>FAIL → PASS in the same run, commit and job is a candidate, not proof of a flaky individual test.
Setup failures, skipped tests and cancelled test steps are not counted as test failures.
Weeks start Monday in UTC. Durations include runner overhead.</p>
<p>Collected: {escaped(history.get('collected_at', 'offline'))}. History contains only collected/retained data;
missing or deleted runs are not evidence of success. No runs means no observations.</p>
{figure.to_html(full_html=False, include_plotlyjs=True)}
{trend.to_html(full_html=False, include_plotlyjs=False)}
<h2>Recovery candidates</h2><ul>{items}</ul>{slow}</html>'''
    (output / 'report.html').write_text(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--days', type=int, default=365)
    parser.add_argument('--output', type=Path, default=Path('build/ci-report'))
    parser.add_argument('--history', type=Path, help='Merge previously collected history')
    parser.add_argument('--offline', action='store_true', help='Render history without GitHub calls')
    parser.add_argument('--restore-latest', action='store_true', help='Restore previous weekly report artifact')
    parser.add_argument('--machine', type=Path, help='Optional downloaded tests.jsonl for slow-test ranking')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo) or args.days < 1:
        parser.error('Use owner/repository and --days >= 1')
    history = {'schema_version': 1, 'repo': args.repo, 'records': []}
    existing = args.history or args.output / 'history.json'
    if existing.exists():
        history = json.loads(existing.read_text())
    if not args.offline:
        api = GitHub(args.repo)
        if args.restore_latest and not existing.exists():
            # Only restore successful report-workflow artifacts from the default branch.
            branch = api.get('')['default_branch']
            for run in api.pages('/actions/workflows/flutter-ci-analysis.yml/runs?status=success', 'workflow_runs'):
                if run['head_branch'] != branch or run['event'] not in ('schedule', 'workflow_dispatch'):
                    continue
                artifacts = list(api.pages(f"/actions/runs/{run['id']}/artifacts", 'artifacts'))
                artifact = next((a for a in artifacts if not a['expired'] and a['name'].startswith('flutter-ci-report-')), None)
                if artifact:
                    history = api.archive_json(artifact)
                    break
        if history['repo'] != args.repo or history.get('schema_version') != 1:
            parser.error('History repository or schema mismatch')
        history = collect(api, history, args.days)
    elif history['repo'] != args.repo or history.get('schema_version') != 1:
        parser.error('History repository or schema mismatch')
    render(history, args.output, machine_tests(args.machine) if args.machine else None)
    print(f"Report: {args.output / 'report.html'} ({len(history['records'])} job attempts)")


if __name__ == '__main__':
    main()
