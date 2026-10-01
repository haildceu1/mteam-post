import pytest
from media_title_renamer.mteam_fill import _fill_missing_audio_choice


def test_preserves_site_mediainfo_selection():
    class Driver:
        def execute_script(self, script, *args):
            return 'DTS-HD MA'
        def find_element(self, *args):
            raise AssertionError('must not override MediaInfo')
    assert _fill_missing_audio_choice(Driver(), {'media': {'audio_codec':'FLAC'}})


@pytest.mark.parametrize('codec,label',[('DTS-HD MA','DTS-HD MA'),('Opus','Other')])
def test_empty_choice_uses_measured_audio_exact_label(codec,label):
    class Element:
        def click(self):
            pass
    class Driver:
        def __init__(self):
            self.selected = None
        def execute_script(self, script, *args):
            if not args:
                return ''
            if 'const canon' in script:
                assert args == (label,)
                return Element()
            self.selected = args[0]
        def find_element(self, *args):
            assert args == ('css selector','#audioCodec')
            return Element()
    driver = Driver()
    assert _fill_missing_audio_choice(driver, {'media':{'audio_codec':codec}})
    assert driver.selected is not None
