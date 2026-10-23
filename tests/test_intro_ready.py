from unittest.mock import patch
from unittest.mock import patch

from media_title_renamer import mteam_fill
from media_title_renamer.mteam_fill import _fill_description, _wait_for_intro


def test_waits_for_real_intro_not_short_loading_placeholder():
    class Driver:
        calls=0
        def execute_script(self,script):
            self.calls+=1
            return {'text':'loading' if self.calls==1 else 'Verified introduction. '*4,'children':3}
    driver=Driver()
    with patch('media_title_renamer.mteam_fill.time.sleep'):
        assert _wait_for_intro(driver,timeout=1)
    assert driver.calls==2


def test_timeout_is_not_reported_as_intro_ready():
    with patch('media_title_renamer.mteam_fill.time.time',side_effect=[0,61]):
        assert not _wait_for_intro(object(),timeout=60)


def test_explicit_synopsis_fallback_is_written_and_verified():
    class Driver:
        def __init__(self):
            self.editor = Editor()

        def find_element(self, by, selector):
            assert by == 'css selector'
            assert selector == '[contenteditable="true"][data-lexical-editor]'
            return self.editor

        def execute_script(self, _script):
            return {'text': synopsis, 'children': 1}

    class Editor:
        def __init__(self):
            self.keys = []

        def click(self):
            pass

        def send_keys(self, value):
            self.keys.append(value)

    synopsis = 'Gordon Ramsay visits struggling restaurants, examines their food and management, and guides owners through changes intended to help their businesses recover and attract customers.'
    driver = Driver()
    with patch('media_title_renamer.mteam_fill._wait_for_intro', return_value=True) as wait:
        assert _fill_description(driver, synopsis)
    assert driver.editor.keys == ['\ue009a', synopsis]
    wait.assert_called_once_with(driver, timeout=5.0)


def test_explicit_synopsis_fallback_rejects_short_text():
    assert not _fill_description(object(), 'Too short.')


def test_package_synopsis_fallback_skips_site_douban_fetch():
    package = {
        'title': 'Kitchen Nightmares 2007 S09 1080p WEB-DL AV1 DDP 5.1-MeGusta',
        'subtitle': '厨房噩梦 / Kitchen Nightmares',
        'douban_url': 'https://movie.douban.com/subject/38535024/',
        'category': '影剧/综艺/HD',
        'description': 'Gordon Ramsay visits struggling restaurants and helps their owners improve operations. ' * 2,
    }
    with patch.object(mteam_fill, '_fill_field', return_value=True), \
         patch.object(mteam_fill, '_select_category', return_value=True), \
         patch.object(mteam_fill, '_fill_missing_audio_choice', return_value=True), \
         patch.object(mteam_fill, '_fill_description', return_value=True) as fill, \
         patch.object(mteam_fill, '_click_get_intro') as fetch:
        mteam_fill._fill_page(object(), package, upload=False)
    fill.assert_called_once()
    fetch.assert_not_called()
