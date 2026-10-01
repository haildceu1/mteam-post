from media_title_renamer.episode_mapping import season_number_hint, sample_or_extra
from media_title_renamer.cli import _canonical_source
from media_title_renamer.batch_metadata import _missing_fields
from media_title_renamer.prepare import DoubanMatch, _choose_douban


def test_season_is_preserved_in_ai_episode_and_ordinal_release_names():
    assert season_number_hint('S02E01') == 2
    assert season_number_hint('S03E01') == 3
    assert season_number_hint('[sam] Fruits Basket (2019) 2nd Season') == 2
    assert season_number_hint('Fruits Basket 1st Season') == 1


def test_season_fallback_cannot_select_old_anime_remake():
    old = DoubanMatch(id='1465390', url='https://movie.douban.com/subject/1465390/',
        title='水果篮子', original_title='フルーツバスケット', year='2001', score=100,
        season_number=None, source='suggest')
    assert _choose_douban([old], expected_season=2, expected_titles=['水果篮子'], expected_year='2019') is None


def test_creditless_opening_and_ending_are_not_main_episodes():
    assert sample_or_extra('NC/[YURI] Soul Eater Not! (Creditless ED).mkv')
    assert sample_or_extra('NC/[YURI] Soul Eater Not! (Creditless OP).mkv')
    assert not sample_or_extra('[YURI] Soul Eater Not! S01E01.mkv')


def test_ai_bluray_spelling_normalizes_to_site_convention():
    assert _canonical_source('Blu-ray') == 'BluRay'
    assert _canonical_source('UHD Blu-ray REMUX') == 'UHD BluRay REMUX'


def test_final_pack_does_not_validate_default_first_season():
    missing = _missing_fields({'kind':'tv', 'input_path':'/TL/Fruits Basket The Final',
        'episode':'S01', 'episode_mapping':{'source':'inferred_single_season'}})
    assert any('Final' in field for field in missing)


def test_declared_ordinal_season_mismatch_is_pending():
    missing = _missing_fields({'kind':'tv', 'input_path':'/TL/Fruits Basket 2nd Season', 'episode':'S01'})
    assert any('源目录声明季号' in field for field in missing)
