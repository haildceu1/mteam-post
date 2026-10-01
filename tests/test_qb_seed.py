import json
from types import SimpleNamespace

import pytest

from media_title_renamer import qb_seed, publish
from media_title_renamer.prepare import _bencode


@pytest.fixture
def setup_seed(tmp_path, monkeypatch):
    source = tmp_path / "Series"
    source.mkdir()
    (source / "one.mkv").write_bytes(b"abc")
    (source / "two.mkv").write_bytes(b"def")
    torrent = tmp_path / "official.torrent"
    torrent.write_bytes(_bencode({b"info": {b"private": 1, b"name": b"Series", b"piece length": 16,
        b"pieces": b"x" * 20, b"files": [{b"path": [b"one.mkv"], b"length": 3},
                                            {b"path": [b"two.mkv"], b"length": 3}]}}))
    monkeypatch.setattr(qb_seed, "mapped_source", lambda *args: __import__('pathlib').Path('/downloads/Series'))
    stats = [p.stat() for p in (source / "one.mkv", source / "two.mkv")]
    monkeypatch.setattr(qb_seed.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="\n".join(
        f"{s.st_dev}:{s.st_ino}:{s.st_size}" for s in stats)))
    monkeypatch.setattr(qb_seed, "_qb_config_key", lambda *a: "not-a-real-key")
    monkeypatch.setattr(qb_seed.time, "sleep", lambda *a: None)
    return source, torrent


def test_multifile_paused_skipchecking_then_verify_start(setup_seed, monkeypatch):
    source, torrent = setup_seed
    calls = []
    task = {"state": "stoppedUP", "size": 6, "save_path": "/downloads", "content_path": "/downloads/Series",
            "progress": 1, "amount_left": 0, "tags": ""}
    exists = [False]
    class Session:
        def get(self, url, **kwargs):
            if url.endswith('version'):
                return SimpleNamespace(text='v5.2.3')
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
                {"name": "Series/one.mkv", "size": 3}, {"name": "Series/two.mkv", "size": 3}])
        def post(self, url, data=None, **kwargs):
            endpoint = url.split('/')[-1]
            calls.append((endpoint, data))
            if endpoint == 'add': exists[0] = True
            if endpoint == 'addTags': task['tags'] = 'Mteam'
            if endpoint == 'start': task['state'] = 'stalledUP'
            return SimpleNamespace(status_code=200, text='Ok.')
        def close(self): pass
    monkeypatch.setattr(qb_seed, '_qb_api', lambda *a: Session())
    monkeypatch.setattr(qb_seed, '_qb_task', lambda *a: task if exists[0] else None)
    result = qb_seed.seed(torrent, source)
    assert result['status'] == 'seeded'
    assert calls[0][1]['stopped'] == calls[0][1]['skip_checking'] == 'true'
    assert [c[0] for c in calls] == ['add', 'createTags', 'addTags', 'start']
    calls.clear()
    assert qb_seed.seed(torrent, source)['reused']
    assert all(c[0] not in ('add', 'start') for c in calls)


def test_publish_default_seeds_and_no_qb_optout(tmp_path, monkeypatch):
    package = tmp_path / 'mteam-prepare.json'
    package.write_text(json.dumps({'prepared_path': str(tmp_path / 'Series')}))
    calls = []
    def fill(argv):
        calls.append(argv)
        (tmp_path / 'publish-result.json').write_text(json.dumps({
            'mteam_torrent_id': '1261825', 'official_torrent': {'manifest_verified': True}}))
    seeded = []
    monkeypatch.setattr(publish, 'mteam_fill_main', fill)
    monkeypatch.setattr(qb_seed, 'seed', lambda *a, **k: seeded.append(a) or {'status': 'seeded'})
    publish.main([str(package), '--submit', '--yes'])
    assert '--recall-official' in calls[-1] and len(seeded) == 1
    publish.main([str(package), '--submit', '--no-qb'])
    assert '--recall-official' not in calls[-1] and len(seeded) == 1
    publish.main([str(package)])
    assert '--submit' not in calls[-1] and len(seeded) == 1


def test_container_identity_mismatch_rejected_before_qb(setup_seed, monkeypatch):
    source, torrent = setup_seed
    monkeypatch.setattr(qb_seed.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='wrong inode'))
    monkeypatch.setattr(qb_seed, '_qb_api', lambda *a: pytest.fail('must not contact qB'))
    with pytest.raises(ValueError, match='inode'):
        qb_seed.seed(torrent, source)
