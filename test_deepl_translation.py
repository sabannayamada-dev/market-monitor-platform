import json
import os
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from monitor_core.deepl_translation import translate_to_japanese


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class DeepLTranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "DEEPL_API_KEY": "test-key:fx",
            "DEEPL_CACHE_FILE": os.path.join(self.temp.name, "cache.sqlite3"),
            "DEEPL_DAILY_CHARACTER_LIMIT": "16000",
        }, clear=False)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    @patch("monitor_core.deepl_translation.urllib.request.urlopen")
    def test_translates_once_then_uses_cache_and_skips_japanese(self, urlopen):
        urlopen.return_value = _Response({"translations": [{"text": "市場が再開"}]})
        expected = {"Markets reopen": "市場が再開", "日本語です": "日本語です"}
        self.assertEqual(translate_to_japanese(expected), expected)
        self.assertEqual(translate_to_japanese(expected), expected)
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api-free.deepl.com/v2/translate")
        self.assertNotIn("test-key", request.full_url)

    @patch("monitor_core.deepl_translation.urllib.request.urlopen")
    def test_api_failure_returns_original(self, urlopen):
        urlopen.side_effect = urllib.error.URLError("offline")
        self.assertEqual(translate_to_japanese(["Original"]), {"Original": "Original"})

    @patch("monitor_core.deepl_translation.urllib.request.urlopen")
    def test_daily_limit_returns_original_without_request(self, urlopen):
        os.environ["DEEPL_DAILY_CHARACTER_LIMIT"] = "3"
        self.assertEqual(translate_to_japanese(["Too long"]), {"Too long": "Too long"})
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
