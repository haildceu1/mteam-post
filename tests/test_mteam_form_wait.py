from media_title_renamer.mteam_fill import _wait_for_form_controls


class Driver:
    def __init__(self, ready):
        self.ready = ready
        self.calls = 0
    def execute_script(self, script):
        self.calls += 1
        assert "getElementById('name')" in script
        return self.ready


def test_publish_route_requires_actual_form_controls():
    assert _wait_for_form_controls(Driver(True), 'https://kp.m-team.cc/upload', 0)
    assert not _wait_for_form_controls(Driver(False), 'https://kp.m-team.cc/upload', 0)
