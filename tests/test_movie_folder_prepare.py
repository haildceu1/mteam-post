import json
from pathlib import Path
from unittest.mock import patch

import pytest

from media_title_renamer.cli import MediaInfo
from media_title_renamer.prepare import main


def test_movie_folder_with_sample_keeps_full_manifest_and_source(tmp_path):
    root = tmp_path / 'Dark.City.1998.BluRay.2160p.x265-FZHD'
    (root / 'Sample').mkdir(parents=True)
    feature = root / (root.name + '.mkv')
    feature.write_bytes(b'feature' * 100)
    (root / 'Sample' / 'Dark.City-sample.mkv').write_bytes(b'sample')
    (root / 'release.nfo').write_bytes(b'release information')
    before = {p.relative_to(root): (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
              for p in root.rglob('*') if p.is_file()}
    media = MediaInfo(width=3840, height=2160, resolution='2160p', video_format='HEVC',
                      writing_library='x265', video_codec='x265', hdr=(), hfr=None,
                      audio_codec='TrueHD', audio_channels='7.1', audio_tracks=1,
                      audio_bitrate=0, audio_language='en')
    output = tmp_path / 'output'
    with patch('media_title_renamer.prepare.read_mediainfo', return_value=media) as probe, \
         patch('media_title_renamer.prepare.prepare_technical_info',
               return_value=('MediaInfo', 'Video HEVC', output / 'mediainfo.txt', None)):
        package_path = main([str(root), '--remain', '--offline', '--yes',
                             '--skip-screenshots', '--output', str(output)])
    package = json.loads(package_path.read_text())
    assert package['kind'] == 'movie'
    assert package['input_path'] == package['prepared_path'] == str(root)
    assert package['representative_path'] == str(feature)
    assert {r['relative_path'] for r in package['files']} == {p.as_posix() for p in before}
    torrent = Path(package['torrent']['path']).read_bytes()
    assert b'Sample' in torrent and b'release.nfo' in torrent
    assert probe.call_args.args[0] == feature
    assert before == {p.relative_to(root): (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
                      for p in root.rglob('*') if p.is_file()}


def test_tv_season_is_not_routed_to_movie(tmp_path):
    root = tmp_path / 'Show.S01'
    root.mkdir()
    (root / 'Show.S01E01.mkv').write_bytes(b'episode')
    with patch('media_title_renamer.prepare._prepare_folder', return_value=tmp_path / 'tv.json') as tv:
        assert main([str(root), '--remain']) == tmp_path / 'tv.json'
    tv.assert_called_once()


def test_explicit_movie_rejects_multiple_features_before_probe(tmp_path):
    (tmp_path / 'Film.One.1998.mkv').write_bytes(b'one')
    (tmp_path / 'Film.Two.1999.mkv').write_bytes(b'two')
    with patch('media_title_renamer.prepare.read_mediainfo') as probe, pytest.raises(SystemExit):
        main([str(tmp_path), '--remain', '--kind', 'movie'])
    probe.assert_not_called()
