import json
from media_title_renamer import prepare
from media_title_renamer.cli import inspect_media


def test_remain_iso_uses_actual_playlist_audio_not_missing_initial_probe(tmp_path, monkeypatch):
    source = tmp_path / 'The.World.Will.Tremble.2025.DDP.5.1-ISO.iso'
    source.write_bytes(b'unchanged-test-iso')
    initial = inspect_media({'media': {'track': [
        {'@type': 'Video', 'Format': 'AVC', 'Width': '1920', 'Height': '1080'}]}}, source='BluRay')
    monkeypatch.setattr(prepare, 'read_mediainfo', lambda *a: initial)
    report = ('VIDEO:\nMPEG-4 AVC Video 27,712 kbps 1080p / 24 fps\n'
              'AUDIO:\nDolby Digital Audio English 640 kbps 5.1 / 48 kHz\nSUBTITLES:\n')
    output = tmp_path / 'preview'
    monkeypatch.setattr(prepare, 'prepare_technical_info', lambda *a, **k:
                        ('BDInfo', report, output / 'bdinfo.txt', '00000'))
    monkeypatch.setattr(prepare, '_douban_for_release', lambda *a, **k: None)
    path = prepare.main([str(source), '--remain', '--offline', '--title', 'The World Will Tremble',
                         '--year', '2025', '--source', 'BluRay', '--group', 'ISO',
                         '--skip-screenshots', '--skip-torrent', '--output', str(output)])
    package = json.loads(path.read_text())
    assert package['media']['audio_codec'] == 'DD'
    assert package['media']['audio_channels'] == '5.1'
    assert 'DD5.1' in package['title'] and 'DDP' not in package['title']
    assert package['prepared_path'] == str(source)
    assert source.read_bytes() == b'unchanged-test-iso'
