import json
import hashlib
import os
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
    torrent.write_bytes(_bencode({b"announce": b"https://tracker.m-team.io:8999/announce?passkey=test-secret",
        b"announce-list": [[b"https://tracker.m-team.io:8999/announce?passkey=test-secret"]],
        b"info": {b"private": 1, b"name": b"Series", b"piece length": 16,
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
    trackers = [{"url": "https://tracker.m-team.io:8999/announce?passkey=test-secret"}]
    class Session:
        def get(self, url, **kwargs):
            if url.endswith('version'):
                return SimpleNamespace(text='v5.2.3')
            if url.endswith('/trackers'):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: list(trackers))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
                {"name": "Series/one.mkv", "size": 3}, {"name": "Series/two.mkv", "size": 3}])
        def post(self, url, data=None, **kwargs):
            endpoint = url.split('/')[-1]
            calls.append((endpoint, data))
            if endpoint == 'add': exists[0] = True
            if endpoint == 'addTrackers':
                trackers.extend({"url": item} for item in data["urls"].splitlines())
            if endpoint == 'removeTrackers':
                remove = data["urls"].split('|')
                trackers[:] = [row for row in trackers if row['url'] not in remove]
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


def test_existing_hash_replaces_tl_tracker_with_official_mteam(setup_seed, monkeypatch, capsys):
    source, torrent = setup_seed
    calls = []
    mteam = "https://tracker.m-team.io:8999/announce?passkey=test-secret"
    tl = "https://tracker.torrentleech.org/announce?passkey=tl-test-secret"
    public = "udp://tracker.example.org:6969/announce"
    trackers = [{"url": tl}, {"url": public}]
    task = {"state": "stalledUP", "size": 6, "save_path": "/downloads", "content_path": "/downloads/Series",
            "progress": 1, "amount_left": 0, "tags": "TorrentLeech"}

    class Session:
        def get(self, url, **kwargs):
            if url.endswith('/version'):
                return SimpleNamespace(text='v5.2.3')
            if url.endswith('/trackers'):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: list(trackers))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
                {"name": "Series/one.mkv", "size": 3}, {"name": "Series/two.mkv", "size": 3}])

        def post(self, url, data=None, **kwargs):
            endpoint = url.split('/')[-1]
            calls.append((endpoint, data))
            if endpoint == 'addTrackers':
                trackers.extend({"url": item} for item in data['urls'].splitlines())
            elif endpoint == 'removeTrackers':
                removed = data['urls'].split('|')
                trackers[:] = [row for row in trackers if row['url'] not in removed]
            elif endpoint == 'addTags':
                task['tags'] = 'TorrentLeech,Mteam'
            status = 204 if endpoint == 'addTrackers' else 200
            return SimpleNamespace(status_code=status, text='' if status == 204 else 'Ok.')

        def close(self):
            pass

    monkeypatch.setattr(qb_seed, '_qb_api', lambda *a: Session())
    monkeypatch.setattr(qb_seed, '_qb_task', lambda *a: task)
    result = qb_seed.seed(torrent, source)
    assert result['status'] == 'seeded'
    assert [name for name, _ in calls] == ['addTrackers', 'removeTrackers', 'createTags', 'addTags']
    assert {row['url'] for row in trackers} == {mteam, public}
    assert 'tl-test-secret' not in capsys.readouterr().out


def test_export_qb_torrent_strips_tracker_secret_and_preserves_infohash(tmp_path, monkeypatch):
    source = tmp_path / 'film.mkv'
    source.write_bytes(b'abc')
    info = {b'private': 1, b'name': b'film.mkv', b'length': 3,
            b'piece length': 16, b'pieces': hashlib.sha1(b'abc').digest()}
    info_hash = hashlib.sha1(_bencode(info)).hexdigest()
    raw = _bencode({b'announce': b'https://tracker.torrentleech.org/announce?passkey=must-not-leak',
                    b'announce-list': [[b'https://tracker.torrentleech.org/announce?passkey=must-not-leak']],
                    b'url-list': b'https://private.example/seed?token=must-not-leak', b'info': info})
    task = {'hash': info_hash, 'content_path': '/downloads/film.mkv', 'tags': 'TorrentLeech',
            'progress': 1, 'amount_left': 0, 'size': 3}

    class Session:
        def get(self, url, **kwargs):
            if url.endswith('/torrents/info'):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [task])
            return SimpleNamespace(raise_for_status=lambda: None, content=raw)
        def close(self):
            pass

    monkeypatch.setattr(qb_seed, 'mapped_source', lambda *_: __import__('pathlib').Path('/downloads/film.mkv'))
    monkeypatch.setattr(qb_seed, '_qb_config_key', lambda *_: 'private-test-key')
    monkeypatch.setattr(qb_seed, '_qb_api', lambda *_: Session())
    destination = tmp_path / 'run' / 'qb-upload.torrent'
    result = qb_seed.export_qb_torrent(source, destination, config=tmp_path / 'unused.conf')
    exported = destination.read_bytes()
    meta = __import__('media_title_renamer.prepare', fromlist=['_bdecode'])._bdecode(exported)
    assert result['info_hash'] == info_hash and result['manifest_verified']
    assert not any(key in meta for key in (b'announce', b'announce-list', b'url-list'))
    assert b'must-not-leak' not in exported
    assert os.stat(destination).st_mode & 0o777 == 0o600


def test_one_file_multifile_source_uses_torrent_root_as_qb_save_path(tmp_path, monkeypatch):
    source_dir = tmp_path / "Film"
    source_dir.mkdir()
    source = source_dir / "Film.mkv"
    source.write_bytes(b"abc")
    tracker = "https://tracker.m-team.io:8999/announce?passkey=test-secret"
    torrent = tmp_path / "official.torrent"
    torrent.write_bytes(_bencode({
        b"announce": tracker.encode(),
        b"info": {b"private": 1, b"name": b"Film", b"piece length": 16,
                  b"pieces": b"x" * 20,
                  b"files": [{b"path": [b"Film.mkv"], b"length": 3}]},
    }))
    mapped_file = __import__('pathlib').Path('/downloads/TL/Film/Film.mkv')
    mapped_base = __import__('pathlib').Path('/downloads/TL')
    monkeypatch.setattr(qb_seed, 'mapped_source', lambda *_: mapped_file)
    monkeypatch.setattr(qb_seed, '_qb_config_key', lambda *_: 'not-a-real-key')
    monkeypatch.setattr(qb_seed.time, 'sleep', lambda *_: None)
    stat_calls = []
    monkeypatch.setattr(qb_seed.subprocess, 'run', lambda args, **kwargs: (
        stat_calls.append(args) or SimpleNamespace(stdout=f"{source.stat().st_dev}:{source.stat().st_ino}:{source.stat().st_size}")))

    task = {"state": "uploading", "size": 3, "save_path": "/downloads/TL",
            "content_path": "/downloads/TL/Film/Film.mkv", "progress": 1,
            "amount_left": 0, "tags": "TorrentLeech"}
    trackers = [{"url": "https://tracker.torrentleech.org/announce?passkey=tl-secret"}]
    calls = []

    class Session:
        def get(self, url, **kwargs):
            if url.endswith('/version'):
                return SimpleNamespace(text='v5.2.3')
            if url.endswith('/trackers'):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: list(trackers))
            if url.endswith('/torrents/files'):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
                    {"name": "Film/Film.mkv", "size": 3}])
            raise AssertionError(url)

        def post(self, url, data=None, **kwargs):
            endpoint = url.split('/')[-1]
            calls.append((endpoint, data))
            if endpoint == 'addTrackers':
                trackers.extend({"url": value} for value in data['urls'].splitlines())
            elif endpoint == 'removeTrackers':
                removed = data['urls'].split('|')
                trackers[:] = [row for row in trackers if row['url'] not in removed]
            elif endpoint == 'addTags':
                task['tags'] += ',Mteam'
            return SimpleNamespace(status_code=200, text='Ok.')

        def close(self):
            pass

    monkeypatch.setattr(qb_seed, '_qb_api', lambda *_: Session())
    monkeypatch.setattr(qb_seed, '_qb_task', lambda *_: task)
    result = qb_seed.seed(torrent, source)
    assert result['status'] == 'seeded'
    assert result['qb_save_path'] == '/downloads/TL'
    assert stat_calls[0][-1] == '/downloads/TL/Film/Film.mkv'
    assert not any(endpoint == 'add' for endpoint, _data in calls)
    assert [row['url'] for row in trackers] == [tracker]


def test_one_file_directory_accepts_qb_file_content_path(tmp_path, monkeypatch):
    source = tmp_path / "Bullet.Release"
    source.mkdir()
    media = source / "disc.iso"
    media.write_bytes(b"abc")
    tracker = "https://tracker.m-team.io:8999/announce"
    torrent = tmp_path / "official.torrent"
    torrent.write_bytes(_bencode({
        b"announce": tracker.encode(),
        b"info": {b"private": 1, b"name": b"Bullet.Release", b"piece length": 16,
                  b"pieces": b"x" * 20,
                  b"files": [{b"path": [b"disc.iso"], b"length": 3}]},
    }))
    mapped = __import__('pathlib').Path('/downloads/Bullet.Release')
    monkeypatch.setattr(qb_seed, 'mapped_source', lambda *_: mapped)
    monkeypatch.setattr(qb_seed, '_qb_config_key', lambda *_: 'not-a-real-key')
    monkeypatch.setattr(qb_seed.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(qb_seed.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        stdout=f"{media.stat().st_dev}:{media.stat().st_ino}:{media.stat().st_size}"))

    task = {"state": "stoppedUP", "size": 3, "save_path": "/downloads",
            "content_path": "/downloads/Bullet.Release/disc.iso", "progress": 1,
            "amount_left": 0, "tags": ""}
    trackers = [{"url": tracker}]
    calls = []

    class Session:
        def get(self, url, **kwargs):
            if url.endswith('/version'):
                return SimpleNamespace(text='v5.2.3')
            if url.endswith('/trackers'):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: list(trackers))
            if url.endswith('/torrents/files'):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
                    {"name": "Bullet.Release/disc.iso", "size": 3}])
            raise AssertionError(url)

        def post(self, url, data=None, **kwargs):
            endpoint = url.split('/')[-1]
            calls.append(endpoint)
            if endpoint == 'addTags':
                task['tags'] = 'Mteam'
            elif endpoint == 'start':
                task['state'] = 'stalledUP'
            return SimpleNamespace(status_code=200, text='Ok.')

        def close(self):
            pass

    monkeypatch.setattr(qb_seed, '_qb_api', lambda *_: Session())
    monkeypatch.setattr(qb_seed, '_qb_task', lambda *_: task)
    result = qb_seed.seed(torrent, source)
    assert result['status'] == 'seeded'
    assert result['qb_save_path'] == '/downloads'
    assert calls == ['createTags', 'addTags', 'start']


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
