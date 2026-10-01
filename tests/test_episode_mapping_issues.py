from pathlib import Path
from media_title_renamer.episode_mapping import episode_mapping_issues, season_folder_map, season_number_hint


def test_french_season_folder_does_not_hide_duplicate_or_unnumbered_episodes():
    assert season_number_hint('Saison 2') == 2
    text=episode_mapping_issues([Path('07- La Mort.mp4'),Path('07- La mort.mp4'),Path('Danton.mp4')])
    assert '重复集号：07（2 个文件）' in text
    assert '无集号文件：Danton.mp4' in text


def test_remain_mode_can_identify_seasons_without_inventing_episodes():
    root = Path('/media/La camera')
    first = root / 'Saison 1' / '05- one.mp4'
    duplicate = root / 'Saison 1' / '05- two.mp4'
    unnumbered = root / 'Saison 2' / 'Danton.mp4'
    assert season_folder_map([first, duplicate, unnumbered], root) == {
        first: 'S01', duplicate: 'S01', unnumbered: 'S02'
    }
    assert season_folder_map([root / 'misc.mp4'], root) is None
    assert season_folder_map([Path('/media/Show S01/unnumbered.mkv')], Path('/media/Show S01')) == {
        Path('/media/Show S01/unnumbered.mkv'): 'S01'
    }
    assert season_folder_map([Path('/media/Show S01-S06/unnumbered.mkv')], Path('/media/Show S01-S06')) is None
