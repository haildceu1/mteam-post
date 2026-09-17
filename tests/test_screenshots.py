import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path

from random_video_screenshots.cli import _base_command, _filter_option, _is_hdr, _probe_video


class ScreenshotTests(unittest.TestCase):
    def test_hdr_detection_supports_pq_hlg_and_dolby_vision_side_data(self):
        self.assertTrue(_is_hdr({"color_transfer": "smpte2084"}))
        self.assertTrue(_is_hdr({"color_transfer": "arib-std-b67"}))
        self.assertTrue(
            _is_hdr(
                {
                    "color_transfer": "unknown",
                    "side_data_types": ("DOVI configuration record",),
                }
            )
        )
        self.assertTrue(_is_hdr({"profile": "Dolby Vision 8.1"}))
        self.assertFalse(_is_hdr({"color_transfer": "bt709"}))

    def test_probe_falls_back_when_ffprobe_lacks_side_data_section(self):
        failed = subprocess.CompletedProcess(
            [],
            1,
            "",
            "No match for section 'stream_side_data'\nInvalid argument",
        )
        succeeded = subprocess.CompletedProcess(
            [],
            0,
            '{"streams":[{"duration":"120","width":1920,"height":1080,"profile":"High","color_transfer":"bt709"}],"format":{}}',
            "",
        )
        with patch("random_video_screenshots.cli._run", side_effect=[failed, succeeded]) as run:
            info = _probe_video("ffprobe", Path("movie.mkv"))
        self.assertEqual(info["duration"], 120.0)
        self.assertEqual(run.call_count, 2)
        fallback_command = run.call_args_list[1].args[0]
        self.assertNotIn("stream_side_data", " ".join(fallback_command))

    def test_hdr_jpg_uses_linear_float_tone_mapping(self):
        options = _filter_option(
            1920,
            "jpg",
            {"color_transfer": "smpte2084"},
        )
        filters = options[1].split(",")
        self.assertEqual(filters[0], "zscale=transfer=linear:npl=100")
        self.assertEqual(filters[1], "format=gbrpf32le")
        self.assertIn("tonemap=tonemap=mobius:param=0.3:desat=2", filters)
        self.assertIn("zscale=transfer=bt709:matrix=bt709:range=tv", filters)

    def test_sdr_jpg_does_not_apply_hdr_tone_mapping(self):
        options = _filter_option(1920, "jpg", {"color_transfer": "bt709"})
        self.assertNotIn("tonemap", options[1])
        self.assertIn("scale=1920:-2", options[1])

    def test_seek_decodes_three_second_preroll_before_capture(self):
        command = _base_command("ffmpeg", Path("movie.m2ts"), 571.1)
        first_seek = command.index("-ss")
        input_index = command.index("-i")
        second_seek = command.index("-ss", first_seek + 1)
        self.assertLess(first_seek, input_index)
        self.assertGreater(second_seek, input_index)
        self.assertEqual(command[first_seek + 1], "568.100")
        self.assertEqual(command[second_seek + 1], "3.000")


if __name__ == "__main__":
    unittest.main()
