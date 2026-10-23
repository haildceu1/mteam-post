from types import SimpleNamespace

import pytest

from media_title_renamer import mteam_fill


def test_anamorphic_1080p_corrects_the_sites_sd_guess():
    state = {'selected': 'SD', 'opened': False}
    option = object()
    class Driver:
        def execute_script(self, script, *args):
            if "getElementById('standard')" in script:
                return state['selected']
            if 'const wanted=' in script:
                assert state['opened'] and args == ('1080p',)
                return option
            assert args == (option,)
            state['selected'] = '1080p'
        def find_element(self, _by, selector):
            assert selector == '#standard'
            return SimpleNamespace(click=lambda: state.update(opened=True))
    package = {'media': {'width':1440, 'height':1080, 'resolution':'1080p'}}
    assert mteam_fill._fill_resolution_choice(Driver(), package)
    assert state['selected'] == '1080p'


def test_preflight_blocks_a_nonempty_but_incorrect_resolution(monkeypatch):
    monkeypatch.setattr(mteam_fill, '_validate_release_package_metadata', lambda _: None)
    package = {'title':'Scary Movie 3 2003 HDTV 1080p H.264 DD5.1-Danishbits',
               'category':'电影/HD', 'media':{'resolution':'1080p'}, 'kind':'movie'}
    values = {'title':package['title'], 'category':'電影/HD', 'resolution':'SD',
              'video_codec':'H.264(x264/AVC)', 'audio_codec':'AC3(DD)',
              'mediainfo_length':200, 'description_length':100,
              'screenshot_count':4, 'screenshot_loaded_count':4, 'torrent_file_count':1,
              'invalid_controls':0, 'errors':[], 'form_error_labels':[]}
    driver = SimpleNamespace(execute_script=lambda *_: dict(values))
    with pytest.raises(ValueError, match='resolution_mismatch'):
        mteam_fill._publish_preflight(driver, package)
    values['resolution'] = '1080p'
    assert mteam_fill._publish_preflight(driver, package)['resolution'] == '1080p'
