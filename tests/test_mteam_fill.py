import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from media_title_renamer.mteam_fill import (
    _append_editor_image_spacing,
    _configure_chrome_options,
    _fill_page,
    _fill_missing_audio_choice,
    _fill_missing_video_choice,
    _has_mteam_auth,
    _is_login_url,
    _parser,
    _publish_preflight,
    _select_category,
    _wait_for_editor_images,
    _wait_for_mteam_auth,
    load_mteam_session,
)


class MTeamFillTests(unittest.TestCase):
    def test_audio_choice_overrides_wrong_media_info_selection(self):
        option = object()

        class Element:
            clicked = False

            def click(self):
                self.clicked = True

            def send_keys(self, _value):
                raise AssertionError("the exact measured audio option should be selected")

        class Driver:
            def __init__(self):
                self.element = Element()
                self.script_args = []

            def execute_script(self, script, *args):
                self.script_args.append((script, args))
                if "getElementById('audioCodec')" in script:
                    return "LPCM/PCM"
                if "const canon=s" in script:
                    return option if args == ("TrueHD Atmos",) else None
                return None

            def find_element(self, _by, selector):
                self.assert_selector = selector
                return self.element

        driver = Driver()
        self.assertTrue(_fill_missing_audio_choice(driver, {
            "title": "The Last Boy Scout 1991 UHD BluRay TrueHD Atmos 7.1-MTeam",
            "media": {"audio_codec": "TrueHD Atmos"},
        }))
        self.assertTrue(driver.element.clicked)
        self.assertEqual(driver.assert_selector, "#audioCodec")
        self.assertEqual(driver.script_args[-1][1], (option,))

    def test_audio_choice_keeps_matching_site_selection(self):
        class Driver:
            def execute_script(self, _script, *_args):
                return "TrueHD Atmos"

            def find_element(self, *_args):
                raise AssertionError("matching site selection should be kept")

        self.assertTrue(_fill_missing_audio_choice(Driver(), {"media": {"audio_codec": "TrueHD Atmos"}}))

    def test_missing_video_codec_uses_exact_mediainfo_vc1_option(self):
        option = object()

        class Element:
            clicked = False

            def click(self):
                self.clicked = True

            def send_keys(self, _value):
                raise AssertionError("a selected option should not be dismissed")

        class Driver:
            def __init__(self):
                self.element = Element()
                self.script_args = []

            def execute_script(self, script, *args):
                self.script_args.append((script, args))
                if "document.getElementById('videoCodec')" in script:
                    return ""
                if "const canon=s=>" in script:
                    return option if args == ("VC-1",) else None
                return None

            def find_element(self, _by, selector):
                self.assert_selector = selector
                return self.element

        driver = Driver()
        self.assertTrue(_fill_missing_video_choice(driver, {"media": {"video_codec": "VC-1"}}))
        self.assertTrue(driver.element.clicked)
        self.assertEqual(driver.assert_selector, "#videoCodec")
        self.assertEqual(driver.script_args[-1][1], (option,))

    def test_missing_video_codec_preserves_site_media_info_selection(self):
        class Element:
            clicked = False

            def click(self):
                self.clicked = True

        class Driver:
            def __init__(self):
                self.element = Element()

            def execute_script(self, _script, *_args):
                return "H.264(x264/AVC)"

            def find_element(self, *_args):
                return self.element

        driver = Driver()
        self.assertTrue(_fill_missing_video_choice(driver, {"media": {"video_codec": "VC-1"}}))
        self.assertFalse(driver.element.clicked)

    def test_missing_video_codec_does_not_guess_unknown_mediainfo(self):
        class Driver:
            def execute_script(self, *_args):
                raise AssertionError("unsupported codecs must not touch the form")

        self.assertFalse(_fill_missing_video_choice(Driver(), {"media": {"video_codec": "VVC"}}))

    def test_publish_preflight_rejects_numeric_fps_even_if_stale_package_reaches_form(self):
        title = "Blue Lights 2023 S04 1080p WEB-DL 50Fps H.264 AAC 2.0-NoGrp"
        package = {
            "title": title,
            "subtitle": "Blue Lights Season 4 / 警之光 第四季",
            "category": "影剧/综艺/HD",
            "media": {"hfr": "50Fps"},
        }
        form = {
            "title": title,
            "subtitle": package["subtitle"],
            "douban_url": "",
            "imdb_url": "",
            "bangumi_url": "",
            "category": "影剧/综艺/HD",
            "resolution": "1080p",
            "video_codec": "H.264",
            "audio_codec": "AAC",
            "mediainfo_length": 100,
            "description_length": 50,
            "screenshot_count": 4,
            "screenshot_loaded_count": 4,
            "torrent_file_count": 1,
            "invalid_controls": 0,
            "errors": [],
        }

        class Driver:
            def execute_script(self, _script):
                return form

        with self.assertRaisesRegex(ValueError, "title_hfr_spelling"):
            _publish_preflight(Driver(), package)

    def test_publish_preflight_accepts_canonical_hfr_title(self):
        title = "Blue Lights 2023 S04 1080p WEB-DL HFR H.264 AAC 2.0-NoGrp"
        package = {
            "title": title,
            "subtitle": "Blue Lights Season 4 / 警之光 第四季",
            "category": "影剧/综艺/HD",
            "media": {"hfr": "HFR"},
        }
        form = {
            "title": title,
            "subtitle": package["subtitle"],
            "douban_url": "",
            "imdb_url": "",
            "bangumi_url": "",
            "category": "影剧/综艺/HD",
            "resolution": "1080p",
            "video_codec": "H.264",
            "audio_codec": "AAC",
            "mediainfo_length": 100,
            "description_length": 50,
            "screenshot_count": 4,
            "screenshot_loaded_count": 4,
            "torrent_file_count": 1,
            "invalid_controls": 0,
            "errors": [],
        }

        class Driver:
            def execute_script(self, _script):
                return form

        result = _publish_preflight(Driver(), package)
        self.assertEqual(result["title"], title)

    def test_sports_publish_allows_screenshot_only_description(self):
        package = {
            "title": "NASCAR Americana 2026 S01 1080p WEB-DL H.264 AC3 2.0-NOGRP",
            "subtitle": "NASCAR Americana",
            "category": "運動",
            "kind": "tv",
        }
        form = {
            "title": package["title"], "subtitle": package["subtitle"],
            "douban_url": "", "imdb_url": "", "bangumi_url": "",
            "category": "運動", "resolution": "1080p", "video_codec": "H.264",
            "audio_codec": "AC3", "mediainfo_length": 100,
            "description_length": 0, "screenshot_count": 4, "screenshot_loaded_count": 4,
            "torrent_file_count": 1, "invalid_controls": 1,
            "invalid_control_labels": ["簡介"], "form_error_labels": ["簡介"],
            "errors": ["required"],
        }

        class Driver:
            def execute_script(self, _script):
                return form

        result = _publish_preflight(Driver(), package)
        self.assertEqual(result["description_length"], 0)
        self.assertTrue(result["sports_screenshot_only_description"])

    def test_sports_screenshot_exception_does_not_hide_other_invalid_fields(self):
        package = {
            "title": "NASCAR Americana 2026 S01 1080p WEB-DL H.264 AC3 2.0-NOGRP",
            "subtitle": "NASCAR Americana", "category": "運動", "kind": "tv",
        }
        form = {
            "title": package["title"], "subtitle": package["subtitle"],
            "douban_url": "", "imdb_url": "", "bangumi_url": "",
            "category": "運動", "resolution": "1080p", "video_codec": "H.264",
            "audio_codec": "AC3", "mediainfo_length": 100,
            "description_length": 0, "screenshot_count": 4, "screenshot_loaded_count": 4,
            "torrent_file_count": 1, "invalid_controls": 1,
            "invalid_control_labels": ["地区"], "form_error_labels": ["简介"],
            "errors": ["required"],
        }

        class Driver:
            def execute_script(self, _script):
                return form

        with self.assertRaisesRegex(ValueError, "required_controls"):
            _publish_preflight(Driver(), package)

    def test_non_sports_still_requires_description_text(self):
        package = {
            "title": "Example Movie 2026 1080p WEB-DL H.264 AC3 2.0-NOGRP",
            "subtitle": "Example Movie", "category": "電影/HD", "kind": "movie",
        }
        form = {
            "title": package["title"], "subtitle": package["subtitle"],
            "douban_url": "", "imdb_url": "", "bangumi_url": "",
            "category": "電影/HD", "resolution": "1080p", "video_codec": "H.264",
            "audio_codec": "AC3", "mediainfo_length": 100,
            "description_length": 0, "screenshot_count": 4, "screenshot_loaded_count": 4,
            "torrent_file_count": 1, "invalid_controls": 0, "errors": [],
        }

        class Driver:
            def execute_script(self, _script):
                return form

        with self.assertRaisesRegex(ValueError, "description"):
            _publish_preflight(Driver(), package)

    def test_publish_preflight_rejects_audio_choice_different_from_measured_track(self):
        package = {
            "title": "The Last Boy Scout 1991 UHD BluRay 2160p HDR10 HEVC TrueHD Atmos7.1-MTeam",
            "subtitle": "The Last Boy Scout", "category": "电影/BluRay",
            "media": {"audio_codec": "TrueHD Atmos"},
            "files": [{"relative_path": "The.Last.Boy.Scout.iso"}],
        }
        form = {
            "title": package["title"], "subtitle": package["subtitle"],
            "douban_url": "", "imdb_url": "", "bangumi_url": "",
            "category": "电影/BluRay", "resolution": "2160p", "video_codec": "HEVC",
            "audio_codec": "LPCM/PCM", "mediainfo_length": 100,
            "description_length": 50, "screenshot_count": 4, "screenshot_loaded_count": 4,
            "torrent_file_count": 1, "invalid_controls": 0, "errors": [],
        }

        class Driver:
            def execute_script(self, _script):
                return form

        with self.assertRaisesRegex(ValueError, "audio_codec_mismatch"):
            _publish_preflight(Driver(), package)

    def test_required_control_preflight_identifies_static_field_label(self):
        package = {
            "title": "NASCAR Americana 2026 S01 1080p WEB-DL H.264 AC3 2.0-NOGRP",
            "subtitle": "NASCAR Americana", "category": "運動", "kind": "tv",
        }
        form = {
            "title": package["title"], "subtitle": package["subtitle"],
            "douban_url": "", "imdb_url": "", "bangumi_url": "",
            "category": "運動", "resolution": "1080p", "video_codec": "H.264",
            "audio_codec": "AC3", "mediainfo_length": 100,
            "description_length": 0, "screenshot_count": 4, "screenshot_loaded_count": 4,
            "torrent_file_count": 1, "invalid_controls": 1,
            "invalid_control_labels": ["赛事分类"], "errors": [],
        }

        class Driver:
            def execute_script(self, _script):
                return form

        with self.assertRaisesRegex(ValueError, "required_controls:赛事分类"):
            _publish_preflight(Driver(), package)

    def test_sports_screenshot_exception_accepts_editor_only_form_error(self):
        package = {
            "title": "NASCAR Americana 2026 S01 1080p WEB-DL H.264 AC3 2.0-NOGRP",
            "subtitle": "NASCAR Americana", "category": "運動", "kind": "tv",
        }
        form = {
            "title": package["title"], "subtitle": package["subtitle"],
            "douban_url": "", "imdb_url": "", "bangumi_url": "",
            "category": "運動", "resolution": "1080p", "video_codec": "H.264",
            "audio_codec": "AC3", "mediainfo_length": 100,
            "description_length": 0, "screenshot_count": 4, "screenshot_loaded_count": 4,
            "torrent_file_count": 1, "invalid_controls": 0,
            "invalid_control_labels": [], "form_error_labels": ["简介"], "errors": ["invalid"],
        }

        class Driver:
            def execute_script(self, _script):
                return form

        result = _publish_preflight(Driver(), package)
        self.assertTrue(result["sports_screenshot_only_description"])

    def test_preflight_rejects_four_image_nodes_when_one_is_not_loaded(self):
        package = {
            "title": "Asian Games Beach Volleyball 2026 1080p HDTV H.264 MP2 2.0-NOGRP",
            "subtitle": "Asian Games Beach Volleyball",
            "category": "運動",
            "kind": "tv",
        }
        form = {
            "title": package["title"], "subtitle": package["subtitle"],
            "douban_url": "", "imdb_url": "", "bangumi_url": "",
            "category": "運動", "resolution": "1080p", "video_codec": "H.264",
            "audio_codec": "MP2/3", "mediainfo_length": 100,
            "description_length": 0, "screenshot_count": 4,
            "screenshot_loaded_count": 3, "torrent_file_count": 1,
            "invalid_controls": 0, "errors": [],
        }

        class Driver:
            def execute_script(self, _script):
                return form

        with self.assertRaisesRegex(ValueError, "screenshots_loaded>=4"):
            _publish_preflight(Driver(), package)

    def test_wait_for_editor_images_requires_loaded_image_dimensions(self):
        class Driver:
            def __init__(self, loaded):
                self.loaded = loaded

            def execute_script(self, _script):
                return {"count": 4, "loaded_count": self.loaded}

        self.assertTrue(_wait_for_editor_images(Driver(4), 4, timeout=1))
        with patch("media_title_renamer.mteam_fill.time.sleep"):
            self.assertFalse(_wait_for_editor_images(Driver(3), 4, timeout=0.01))

    def test_sports_does_not_fetch_douban_synopsis_before_screenshot_upload(self):
        package = {
            "title": "NASCAR Americana 2026 S01 1080p WEB-DL H.264 AC3 2.0-NOGRP",
            "subtitle": "NASCAR Americana", "category": "運動", "kind": "tv",
            "douban_url": "https://movie.douban.com/subject/123456/",
            "description": "An automatically collected synopsis that must not be inserted.",
        }
        with patch("media_title_renamer.mteam_fill._fill_field"), \
             patch("media_title_renamer.mteam_fill._select_category"), \
             patch("media_title_renamer.mteam_fill._fill_missing_audio_choice"), \
             patch("media_title_renamer.mteam_fill._click_get_intro") as get_intro:
            _fill_page(object(), package, upload=False)
        get_intro.assert_not_called()

    def test_fill_page_canonicalizes_legacy_numeric_fps_title(self):
        package = {
            "title": "Blue Lights 2023 S04 1080p WEB-DL 50Fps H.264 AAC 2.0-NoGrp",
            "media": {"hfr": "50Fps"},
            "category": "影剧/综艺/HD",
        }
        entered = []

        def capture_field(_driver, _labels, value, label):
            if label == "标题":
                entered.append(value)

        with patch("media_title_renamer.mteam_fill._fill_field", side_effect=capture_field), \
             patch("media_title_renamer.mteam_fill._select_category"), \
             patch("media_title_renamer.mteam_fill._fill_missing_audio_choice"):
            _fill_page(object(), package, upload=False)

        expected = "Blue Lights 2023 S04 1080p WEB-DL HFR H.264 AAC 2.0-NoGrp"
        self.assertEqual(entered, [expected])
        self.assertEqual(package["title"], expected)
        self.assertEqual(package["media"]["hfr"], "HFR")

    def test_chrome_options_add_server_compatibility_flags(self):
        class Options:
            def __init__(self):
                self.arguments = []

            def add_argument(self, value):
                self.arguments.append(value)

        with patch("media_title_renamer.mteam_fill.os.geteuid", return_value=0, create=True), patch(
            "media_title_renamer.mteam_fill.os.name", "posix"
        ), patch.dict("media_title_renamer.mteam_fill.os.environ", {}, clear=True):
            options = _configure_chrome_options(Options())
        self.assertIn("--no-sandbox", options.arguments)
        self.assertIn("--disable-dev-shm-usage", options.arguments)
        self.assertIn("--headless=new", options.arguments)

    def test_chrome_options_accept_explicit_proxy(self):
        class Options:
            def __init__(self):
                self.arguments = []

            def add_argument(self, value):
                self.arguments.append(value)

        with patch("media_title_renamer.mteam_fill.os.environ", {"MTEAM_CHROME_PROXY": "http://127.0.0.1:7890"}):
            options = _configure_chrome_options(Options())
        self.assertIn("--proxy-server=http://127.0.0.1:7890", options.arguments)

    def test_chrome_options_accept_explicit_proxy_pac(self):
        class Options:
            def __init__(self):
                self.arguments = []

            def add_argument(self, value):
                self.arguments.append(value)

        with patch("media_title_renamer.mteam_fill.os.environ", {"MTEAM_CHROME_PAC_URL": "http://127.0.0.1:8765/proxy.pac"}):
            options = _configure_chrome_options(Options())
        self.assertIn("--proxy-pac-url=http://127.0.0.1:8765/proxy.pac", options.arguments)

    def test_two_enters_are_inserted_before_editor_images(self):
        class Editor:
            def __init__(self):
                self.keys = ""

            def send_keys(self, keys):
                self.keys += keys

        class Driver:
            def __init__(self):
                self.editor = Editor()

            def execute_script(self, _script):
                return True

            def find_element(self, by, selector):
                self.assertion = (by, selector)
                return self.editor

        driver = Driver()
        self.assertTrue(_append_editor_image_spacing(driver))
        self.assertEqual(driver.editor.keys, "\ue007\ue007")
        self.assertEqual(
            driver.assertion,
            ("css selector", '[contenteditable="true"][data-lexical-editor]'),
        )

    def test_default_url_is_upload_page(self):
        self.assertEqual(_parser().parse_args([]).url, "https://kp.m-team.cc/upload")

    def test_login_url_detection_ignores_query_and_trailing_slash(self):
        self.assertTrue(_is_login_url("https://kp.m-team.cc/login/?redirect=%2Fupload"))
        self.assertFalse(_is_login_url("https://kp.m-team.cc/upload"))

    def test_login_wait_accepts_existing_local_storage_auth(self):
        class Driver:
            def execute_script(self, _script):
                return "saved-token"

        driver = Driver()
        self.assertTrue(_has_mteam_auth(driver))
        self.assertTrue(_wait_for_mteam_auth(driver, 0))

    def test_parses_devtools_request_header_dump_as_local_storage_session(self):
        text = "\n".join(
            [
                "请求网址",
                "https://api.m-team.cc/api/member/bases",
                "authorization",
                "eyJfake-token",
                "did",
                "device-id",
                "visitorid",
                "visitor-id",
                "version",
                "1.1.7",
                "webversion",
                "1170",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.txt"
            path.write_text(text, encoding="utf-8")
            session = load_mteam_session(path)
        self.assertTrue(session.is_auth_dump)
        self.assertEqual(session.authorization, "eyJfake-token")
        self.assertEqual(session.did, "device-id")
        self.assertEqual(session.visitor_id, "visitor-id")
        self.assertEqual(session.version, "1.1.7")
        self.assertEqual(session.web_version, "1170")

    def test_parses_only_mteam_netscape_cookies(self):
        text = "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".example.com\tTRUE\t/\tFALSE\t2000000000\tother\tignored",
                ".kp.m-team.cc\tTRUE\t/\tTRUE\t2000000000\tauth\tsecret-value",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text(text, encoding="utf-8")
            session = load_mteam_session(path)
        self.assertFalse(session.is_auth_dump)
        self.assertEqual(len(session.cookies), 1)
        self.assertEqual(session.cookies[0]["domain"], ".kp.m-team.cc")
        self.assertEqual(session.cookies[0]["name"], "auth")

    def test_category_selection_retries_after_scrolling_virtualized_dropdown(self):
        class Select:
            def __init__(self):
                self.clicked = False

            def click(self):
                self.clicked = True

        class Driver:
            def __init__(self):
                self.select = Select()
                self.calls = 0
                self.clicked_option = False

            def execute_script(self, script, *args):
                self.calls += 1
                if self.calls == 1:
                    return self.select
                if "arguments[0].click()" in script:
                    self.clicked_option = True
                    return None
                # First lookup represents the visible top of the list; the
                # second succeeds after the JS scroll step.
                return None if self.calls == 2 else object()

        driver = Driver()
        self.assertTrue(_select_category(driver, "动画/Bluray"))
        self.assertTrue(driver.select.clicked)
        self.assertTrue(driver.clicked_option)


if __name__ == "__main__":
    unittest.main()
