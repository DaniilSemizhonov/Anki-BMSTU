"""Integration checks against real Anki collections and the HTTP API."""
import json
from pathlib import Path
from threading import Thread
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from anki.errors import SearchError

from server import Library, InvalidRequest, Handler, LocalServer


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "collection.anki2"
        self.lib = Library(self.path)
        self.did = str(self.lib.col.decks.add_normal_deck_with_name('Languages::English "A"').id)

    def tearDown(self):
        self.lib.col.close()
        self.tmp.cleanup()

    def add(self, **kwargs):
        return self.lib.add_note(dict(deck_id=self.did, front="Hello <world>", back="Привет\nмир", tags="test language", **kwargs))

    def test_collection_messages_are_in_russian(self):
        self.add()
        self.assertRegex(self.lib.overview()["undo"], "[А-Яа-я]")
        with self.assertRaises(SearchError) as caught:
            self.lib.notes({"search": ['"']})
        self.assertRegex(str(caught.exception), "[А-Яа-я]")

    def test_real_scheduler_and_stale_answer(self):
        self.add()
        card = self.lib.study(self.did)["card"]
        self.assertIn("Hello &lt;world&gt;", card["question"])
        self.assertEqual(len(card["intervals"]), 4)
        with self.assertRaises(InvalidRequest):
            self.lib.answer(dict(token=card["token"], rating=9))
        self.lib.answer(dict(token=card["token"], rating=3))
        self.assertEqual(self.lib.col.db.scalar("select count(*) from revlog"), 1)
        with self.assertRaises(InvalidRequest):
            self.lib.answer(dict(token=card["token"], rating=3))
        self.assertEqual(self.lib.overview()["reviewed"], 1)

    def test_search_edit_and_persistence(self):
        nid = self.add()["id"]
        result = self.lib.notes({"deck_id": [self.did], "search": ["Hello"]})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["notes"][0]["tags"], ["language", "test"])
        self.lib.edit_note(dict(id=nid, fields=["Changed", "Updated"]))
        self.lib.col.close()
        self.lib = Library(self.path)
        self.assertEqual(self.lib.notes({"search":["Changed"]})["total"], 1)
        self.assertEqual(self.lib.overview()["total"], 1)
        parent = next(d for d in self.lib.overview()["decks"] if d["name"] == "Languages")
        self.assertEqual(parent["total"], 1)

    def test_apkg_roundtrip_with_media_and_schedule(self):
        self.add()
        media = Path(self.lib.col.media.dir()) / "sample.txt"
        media.write_text("test media")
        note = self.lib.col.get_note(int(self.lib.notes({})["notes"][0]["id"]))
        note.fields[1] += '<img src="sample.txt">'
        self.lib.col.update_note(note)
        card = self.lib.study(self.did)["card"]
        self.lib.answer(dict(token=card["token"], rating=4))
        package = self.lib.export_package()
        other = Library(Path(self.tmp.name) / "other" / "collection.anki2")
        try:
            imported = other.import_package(package)
            self.assertEqual(imported["added"], 1)
            self.assertEqual(other.overview()["total"], 1)
            self.assertEqual(other.col.db.scalar("select count(*) from revlog"), 1)
            self.assertEqual((Path(other.col.media.dir()) / "sample.txt").read_text(), "test media")
        finally:
            other.col.close()

    def test_add_validation_and_pending_invalidation(self):
        with self.assertRaises(InvalidRequest):
            self.lib.add_note(dict(deck_id=self.did, front="", back="answer"))
        self.add()
        card = self.lib.study(self.did)["card"]
        self.add()
        with self.assertRaises(InvalidRequest):
            self.lib.answer(dict(token=card["token"], rating=3))
        self.assertEqual(self.lib.col.db.scalar("select count(*) from revlog"), 0)

    def test_stock_note_types_generate_real_cards(self):
        reverse = self.lib.add_note(dict(deck_id=self.did, kind="reversed", front="One", back="Один"))
        cloze = self.lib.add_note(dict(deck_id=self.did, kind="cloze", front="{{c1::one}} and {{c2::two}}", back=""))
        self.assertEqual(reverse["cards"], 2)
        self.assertEqual(cloze["cards"], 2)
        self.lib.card_action(dict(note_id=reverse["id"],action="suspend"))
        self.assertTrue(all(c.queue == -1 for c in self.lib.col.get_note(int(reverse["id"])).cards()))
        self.lib.undo({})
        self.assertTrue(all(c.queue == 0 for c in self.lib.col.get_note(int(reverse["id"])).cards()))
        self.assertEqual(self.lib.overview()["total"], 4)
        with self.assertRaises(InvalidRequest):
            self.lib.add_note(dict(deck_id=self.did, kind="cloze", front="No deletions", back=""))
        self.assertEqual(self.lib.overview()["total"], 4)

    def test_suspend_resume_delete_and_undo(self):
        nid = int(self.add()["id"])
        card = self.lib.col.get_note(nid).cards()[0]
        self.lib.card_action(dict(card_id=str(card.id), action="suspend"))
        self.assertIsNone(self.lib.study(self.did)["card"])
        self.lib.card_action(dict(card_id=str(card.id), action="resume"))
        self.assertIsNotNone(self.lib.study(self.did)["card"])
        self.lib.remove_note(dict(id=str(nid)))
        self.assertEqual(self.lib.overview()["total"], 0)
        self.lib.undo({})
        self.assertEqual(self.lib.overview()["total"], 1)
        self.assertEqual(self.lib.col.get_note(nid).fields[0], "Hello &lt;world&gt;")

    def test_undo_review_restores_queue_and_rejects_stale_answer(self):
        self.add()
        original = self.lib.study(self.did)["card"]
        after = self.lib.answer(dict(token=original["token"],rating=4))
        self.assertTrue(after["can_undo"])
        self.lib.undo(dict(review_only=True))
        restored = self.lib.study(self.did)
        self.assertEqual(restored["card"]["id"], original["id"])
        self.assertFalse(restored["can_undo"])
        self.assertEqual(self.lib.col.db.scalar("select count(*) from revlog"), 0)
        with self.assertRaises(InvalidRequest):
            self.lib.answer(dict(token=original["token"], rating=3))

    def test_media_html_and_tags_are_stored(self):
        media = self.lib.upload_media("../unsafe.png", b"fake image bytes")
        self.assertNotIn("/", media["filename"])
        with self.assertRaises(InvalidRequest):
            self.lib.upload_media("executable.svg", b"<svg></svg>")
        nid = self.lib.add_note(dict(deck_id=self.did, front=f'<img src="{media["filename"]}">', back="Answer", format="html", tags="math"))["id"]
        note = self.lib.notes({})["notes"][0]
        self.assertIn('<img src=', note["fields"][0]["value"])
        self.lib.edit_note(dict(id=nid,fields=["Updated", "Answer"],tags="calculus semester1"))
        self.assertEqual(self.lib.col.get_note(int(nid)).tags, ["calculus", "semester1"])
        self.assertTrue((Path(self.lib.col.media.dir()) / media["filename"]).is_file())

    def test_deck_limits_are_independent_and_persist(self):
        other = self.lib.col.decks.add_normal_deck_with_name("Other").id
        defaults = self.lib.deck_settings(other)
        saved = self.lib.save_deck_settings(dict(id=self.did, new=2, review=80))
        self.assertEqual((saved["new"], saved["review"]), (2,80))
        self.assertEqual(self.lib.deck_settings(other), defaults)
        with self.assertRaises(InvalidRequest):
            self.lib.save_deck_settings(dict(id=self.did,new=-1,review=80))
        for index in range(4):
            self.lib.add_note(dict(deck_id=self.did,front=f"Question {index}",back="Answer"))
        self.assertEqual(self.lib.study(self.did)["counts"]["new"], 2)
        self.lib.col.close()
        self.lib = Library(self.path)
        self.assertEqual(self.lib.deck_settings(self.did)["new"], 2)

    def test_statistics_use_real_review_history(self):
        self.add()
        before = self.lib.statistics()
        self.assertEqual(before["reviews"],0)
        card = self.lib.study(self.did)["card"]
        self.lib.answer(dict(token=card["token"],rating=1))
        stats = self.lib.statistics()
        self.assertEqual(len(stats["days"]),30)
        self.assertEqual(stats["reviews"],1)
        self.assertEqual(stats["again"],1)
        self.assertEqual(sum(d["reviews"] for d in stats["days"]),1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.http = LocalServer(("127.0.0.1", 0), Handler)
        cls.http.library = Library(Path(cls.tmp.name) / "collection.anki2")
        cls.thread = Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.http.server_port}"
        with urlopen(cls.url + "/api/overview", timeout=5) as response:
            cls.token = json.load(response)["csrf"]

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.thread.join(timeout=5)
        cls.http.server_close()
        cls.http.library.col.close()
        cls.tmp.cleanup()

    def request(self, path, data=None, token=True, headers=None):
        h = headers or {}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
            if token:
                h["X-Anki-Token"] = self.token
        return urlopen(Request(self.url + path, data=body, headers=h), timeout=5)

    def test_http_create_review_edit_export(self):
        with self.request("/api/decks", dict(name="HTTP integration")) as r:
            did = json.load(r)["id"]
        with self.request("/api/notes", dict(deck_id=did, front="HTTP question", back="HTTP answer")) as r:
            nid = json.load(r)["id"]
        with self.request("/api/study", dict(deck_id=did)) as r:
            card = json.load(r)["card"]
        with self.request("/api/answer", dict(token=card["token"], rating=4)) as r:
            self.assertIsNone(json.load(r)["card"])
        with self.request("/api/notes/edit", dict(id=nid, fields=["Edited", "Answer"])) as r:
            self.assertTrue(json.load(r)["ok"])
        with self.request("/api/export") as r:
            package = r.read()
            self.assertEqual(package[:2], b"PK")
        request = Request(self.url+"/api/import", data=package, headers={"X-Anki-Token":self.token, "Content-Type":"application/octet-stream"})
        with urlopen(request, timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_markdown_preview_import_and_private_settings(self):
        with self.request("/api/decks", dict(name="Markdown HTTP")) as response:
            did = json.load(response)["id"]
        files = [dict(name="http-card.md", content="## Вопрос\nHTTP Markdown?\n\n## Ответ\n**Работает**")]
        with self.request("/api/markdown/preview", dict(deck_id=did, files=files)) as response:
            self.assertEqual(json.load(response)["added"], 1)
        with self.request("/api/markdown/import", dict(deck_id=did, files=files)) as response:
            self.assertEqual(json.load(response)["added"], 1)
        with self.request("/api/markdown/import", dict(deck_id=did, files=files)) as response:
            self.assertEqual(json.load(response)["unchanged"], 1)
        with self.request("/api/markdown/export") as response:
            self.assertEqual(response.read()[:2], b"PK")
            self.assertEqual(response.headers.get_content_type(), "application/zip")
        with self.request("/api/settings", dict(url="https://notes.example", vault="Test", folder="Anki", token="http-test-secret")) as response:
            public = json.load(response)
            self.assertTrue(public["has_token"])
            self.assertNotIn("http-test-secret", str(public))
        with self.request("/api/settings") as response:
            self.assertNotIn("http-test-secret", response.read().decode())
        with self.assertRaises(HTTPError) as error:
            self.request("/api/settings", dict(token="changed"), token=False)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_request_guards(self):
        cases = [
            ("/api/decks", dict(name="blocked"), False, {}, 403),
            ("/api/decks", dict(name="blocked"), True, {"Origin":"https://example.invalid"}, 403),
            ("/api/overview", None, True, {"Host":"example.invalid"}, 403),
            ("/api/notes", dict(deck_id="bad", front="q", back="a"), True, {}, 400),
            ("/media/%2e%2e%2fcollection.anki2", None, True, {}, 404),
            ("/missing", None, True, {}, 404),
        ]
        for path, data, token, headers, status in cases:
            with self.subTest(path=path, headers=headers):
                with self.assertRaises(HTTPError) as caught:
                    self.request(path, data, token, headers)
                self.assertEqual(caught.exception.code, status)
                caught.exception.close()

    def test_static_assets(self):
        for path in ["/", "/app.js", "/style.css"]:
            with self.request(path) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)
                self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
