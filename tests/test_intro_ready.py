from unittest.mock import patch
from media_title_renamer.mteam_fill import _wait_for_intro


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
