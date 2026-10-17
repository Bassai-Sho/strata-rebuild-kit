import importlib.util, json, os, unittest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent", "strata-front.py")
spec = importlib.util.spec_from_file_location("front", SRC); front = importlib.util.module_from_spec(spec); spec.loader.exec_module(front)

def body(last, meta=None, schema=True):
    b = {"input": [{"content": [{"text": "x"}]}, {"content": [{"text": last}]}]}
    if schema: b["text"] = {"format": {"schema": {"properties": {"title": {}}}}}
    if meta is not None: b["client_metadata"] = {"x-codex-turn-metadata": json.dumps(meta)}
    return json.dumps(b).encode()

class TitleRequest(unittest.TestCase):
    def test_the_shape_codex_0162_and_0163_send(self):
        b = body(front.TITLE_MARK + " of at most 36 characters.\n\nUser prompt:\nbuild a snake game", {"thread_source": "thread_title"})
        self.assertEqual(front.is_title_request(b), "build a snake game")
    def test_the_metadata_alone_is_enough_if_codex_rewords_the_prompt(self):
        b = body("Name this task in a few words.\n\nUser prompt:\nbuild a snake game", {"thread_source": "thread_title"}, schema=False)
        self.assertEqual(front.is_title_request(b), "build a snake game")
    def test_the_old_shape_still_works_without_metadata(self):
        self.assertEqual(front.is_title_request(body(front.TITLE_MARK + ".\n\nUser prompt:\nfix the bug")), "fix the bug")
    def test_ordinary_requests_are_not_titles(self):
        self.assertIsNone(front.is_title_request(body("build a snake game", {"thread_source": "user"}, schema=False)))
        self.assertIsNone(front.is_title_request(body("mentions " + front.TITLE_MARK + " in passing", None, schema=False)))
        self.assertIsNone(front.is_title_request(b"not json")); self.assertIsNone(front.is_title_request(b"{}"))
    def test_make_title(self):
        self.assertEqual(front.make_title("`fix` the **bug** in app.js now please"), "Fix the bug in app.js")
        self.assertEqual(front.make_title("   "), "New task")

if __name__ == "__main__":
    unittest.main(verbosity=2)
