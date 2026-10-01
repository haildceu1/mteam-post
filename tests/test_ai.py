import json
import unittest
from unittest.mock import patch

from media_title_renamer.ai import CliproxyAssistant


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.payload


class CliproxyTests(unittest.TestCase):
    @patch("media_title_renamer.ai.urllib.request.urlopen")
    def test_identify_reads_structured_json_without_logging_key(self, urlopen):
        urlopen.return_value = _Response(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                '{"search_title":"Westworld","title":"Westworld",'
                                '"year":"2016","kind":"tv","episode":"S03",'
                                '"source":"BluRay","group":"Stelks",'
                                '"confidence":0.97}\n```'
                            )
                        }
                    }
                ]
            }
        )
        assistant = CliproxyAssistant(
            base_url="http://example.invalid/v1",
            api_key="local-test-key",
            model="gpt-test",
        )

        identity = assistant.identify(
            filename="Westworld.S03.2160p.BluRay.x265-Stelks.mkv",
            parent_name="Westworld S03",
            hints={"title": "Westworld", "year": "2016"},
            media={"resolution": "2160p", "video_codec": "HEVC"},
        )

        self.assertEqual(identity.search_title, "Westworld")
        self.assertEqual(identity.episode, "S03")
        self.assertEqual(identity.group, "Stelks")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.headers.get("Authorization"), "Bearer local-test-key")
        self.assertNotIn("local-test-key", request.data.decode("utf-8"))

    @patch("media_title_renamer.ai.urllib.request.urlopen")
    def test_choose_douban_returns_candidate_index(self, urlopen):
        urlopen.return_value = _Response(
            {
                "choices": [
                    {"message": {"content": '{"candidate": 2, "confidence": 0.91}'}}
                ]
            }
        )
        assistant = CliproxyAssistant(
            base_url="http://example.invalid/v1",
            api_key="local-test-key",
            model="gpt-test",
        )
        candidates = [
            {"id": "1", "title": "Wrong", "year": "1991"},
            {"id": "2", "title": "Brides of Christ", "year": "1991"},
        ]

        self.assertEqual(assistant.choose_douban("Brides of Christ 1991", candidates), 2)
        self.assertEqual(assistant.last_douban_choice_confidence, 0.91)
        self.assertEqual(assistant.last_douban_selection["id"], "2")

    @patch("media_title_renamer.ai.urllib.request.urlopen")
    def test_choose_douban_rejects_low_confidence_choice(self, urlopen):
        urlopen.return_value = _Response(
            {"choices": [{"message": {"content": '{"candidate": 1, "confidence": 0.42}'}}]}
        )
        assistant = CliproxyAssistant(
            base_url="http://example.invalid/v1",
            api_key="local-test-key",
            model="gpt-test",
        )

        self.assertIsNone(
            assistant.choose_douban(
                "Death on the Nile 1978",
                [
                    {"id": "1302100", "title": "Death on the Nile", "year": "1978"},
                    {"id": "27203644", "title": "Death on the Nile", "year": "2022"},
                ],
            )
        )
        self.assertEqual(assistant.last_douban_choice_confidence, 0.42)

    @patch("media_title_renamer.ai.urllib.request.urlopen")
    def test_web_search_uses_responses_tool_and_parses_output(self, urlopen):
        urlopen.return_value = _Response(
            {
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_local_test",
                        "status": "completed",
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '{"search_title":"Westworld", "title":"Westworld",'
                                    '"year":"2016", "kind":"tv", "confidence":0.99}'
                                ),
                            }
                        ],
                    },
                ]
            }
        )
        assistant = CliproxyAssistant(
            base_url="http://example.invalid/v1",
            api_key="local-test-key",
            model="gpt-test",
            web_search=True,
        )

        identity = assistant.identify(
            filename="Westworld.S03.2160p.BluRay.x265-Stelks.mkv",
            parent_name="Westworld S03",
            hints={"title": "Westworld", "year": "2016"},
            media={"resolution": "2160p", "video_codec": "HEVC"},
        )

        self.assertEqual(identity.search_title, "Westworld")
        self.assertTrue(assistant.last_web_search_used)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/v1/responses"))
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(body["tool_choice"], "required")
        self.assertNotIn("local-test-key", request.data.decode("utf-8"))

    @patch("media_title_renamer.ai.urllib.request.urlopen")
    def test_web_search_can_have_shorter_timeout_than_plain_chat(self, urlopen):
        urlopen.return_value = _Response({"output_text": '{"answer": true}'})
        with patch.dict('os.environ', {'CLIPROXY_WEB_TIMEOUT': '30'}):
            assistant = CliproxyAssistant(base_url='http://example.invalid/v1',
                                         api_key='test-key', model='test-model', timeout=45)
            assistant._complete_responses('system', 'user')
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 30)


if __name__ == "__main__":
    unittest.main()
