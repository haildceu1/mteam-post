import importlib.util
import json
import sys
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    'batch_tl_publish', Path(__file__).resolve().parents[1] / 'scripts' / 'batch_tl_publish.py')
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


def test_preflight_search_failure_is_skipped_but_submission_ambiguity_stops():
    row = {'status': 'publish_incomplete', 'publication_status': 'preflight',
           'blockers': ['mteam_search_unavailable']}
    assert batch.must_stop_batch(row, {}) is False
    assert batch.must_stop_batch(row, {'submit_attempted_at': 1}) is True
    assert batch.must_stop_batch(row, {'mteam_torrent_id': '123'}) is True
    assert batch.must_stop_batch({**row, 'blockers': ['browser_or_recall_failed']}, {}) is True
    assert batch.must_stop_batch({**row, 'publication_status': 'publishing'}, {}) is True
    assert batch.must_stop_batch({'status': 'pending', 'reason': 'prepare_failed'}, {}) is False


def test_douban_rate_limit_stops_batch_and_preview_has_pacing():
    assert batch.must_stop_for_douban_rate_limit({'blockers':['douban_search_rate_limited']})
    assert not batch.must_stop_for_douban_rate_limit({'blockers':['douban_no_verified_candidates']})
    assert batch.batch_interval_seconds(submit=False, publish_interval=180, preview_interval=0)==0
    assert batch.batch_interval_seconds(submit=True, publish_interval=180, preview_interval=0)==180


def test_ready_from_report_rechecks_current_review(tmp_path):
    task_hash = 'a' * 40
    report = tmp_path / 'batch.json'
    review = tmp_path / 'tasks' / task_hash / 'review.json'
    review.parent.mkdir(parents=True)
    report.write_text(json.dumps({'mode':'preview','resources':[
        {'status':'metadata_ready','hash':task_hash,'fingerprint':'current'}]}))
    review.write_text(json.dumps({'status':'metadata_ready','source_unchanged':True,
                                  'blockers':[],'fingerprint':'current'}))
    assert batch.ready_hashes_from_report(report)=={task_hash}
    review.write_text(json.dumps({'status':'seeded','source_unchanged':True,
                                  'blockers':[],'fingerprint':'current'}))
    assert batch.ready_hashes_from_report(report)==set()


def test_silent_child_has_visible_heartbeat(tmp_path):
    result = batch.run_auto([sys.executable, '-c', 'import time;time.sleep(1.2)'], tmp_path, 5, heartbeat=.1)
    assert result == 0
    assert '[等待] 已运行' in (tmp_path/'auto.log').read_text()
