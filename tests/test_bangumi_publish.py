import pytest
from media_title_renamer import mteam_fill


@pytest.mark.parametrize('category', ['动画', '動畫/Bluray'])
def test_anime_requires_bangumi_before_browser_actions(category):
    with pytest.raises(ValueError, match='bangumi_url'):
        mteam_fill._fill_page(None, {'category': category}, upload=True)


@pytest.mark.parametrize('url', ['https://evil.test/subject/1', 'https://bgm.tv/ep/1', 'https://bgm.tv/subject/1?token=secret'])
def test_bangumi_rejects_non_subject_urls(url):
    with pytest.raises(ValueError, match='subject'):
        mteam_fill._validated_bangumi_url({'category': '动画', 'bangumi_url': url})


def test_movie_does_not_require_bangumi():
    assert mteam_fill._validated_bangumi_url({'category': '电影/HD'}) == ''


def test_english_animation_can_publish_without_bangumi():
    assert mteam_fill._validated_bangumi_url({'category':'动画',
        'tmdb':{'original_language':'en','genre_ids':[16]}}) == ''


def test_bangumi_form_value_must_match_package():
    class Driver:
        def execute_script(self, script):
            return {'category': '动画', 'bangumi_url': '', 'resolution': '1080p',
                    'video_codec': 'H.265', 'audio_codec': 'FLAC', 'screenshot_count': 4,
                    'torrent_file_count': 1, 'mediainfo_length': 1000, 'description_length': 1000}
    with pytest.raises(ValueError, match='bangumi_url'):
        mteam_fill._publish_preflight(Driver(), {'category': '动画', 'bangumi_url': 'https://bgm.tv/subject/266455'})
