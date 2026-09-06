import unittest
from pathlib import Path

from random_video_screenshots.cli import _base_command, _filter_option, _is_hdr


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
        self.assertFalse(_is_hdr({"color_transfer": "bt709"}))

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
