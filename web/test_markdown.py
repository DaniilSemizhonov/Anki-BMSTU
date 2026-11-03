"""Markdown exchange against temporary Anki collections and a local FNS fixture."""
import copy
import io
import json
import stat
import tempfile
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fns import Settings, Client
from markdown_io import MarkdownError, export_notes, zip_notes, parse_note, prepare_import, import_notes
from server import Library


def simple_file(name="question.md", answer="**Ответ** & пример"):
    return dict(name=name, content="## Вопрос\n\nВопрос?\n\n## Ответ\n\n" + answer)


class MarkdownCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = Library(Path(self.tmp.name) / "collection.anki2")
        self.did = str(self.lib.col.decks.add_normal_deck_with_name("Учёба::Тест").id)

    def tearDown(self):
        self.lib.col.close()
        self.tmp.cleanup()

    def test_export_reimport_is_unchanged_for_all_stock_types(self):
        for kind, front, back in [("basic", "<b>Вопрос</b>", "Ответ<br>строка"),
                                  ("reversed", "Hello", "Привет"),
                                  ("cloze", "{{c1::Земля}} и {{c2::Солнце}}", "")]:
            self.lib.add_note(dict(deck_id=self.did, kind=kind, front=front, back=back, format="html", tags="учёба test"))
        files = export_notes(self.lib)
        self.assertEqual(len(files), 3)
        _, counts = prepare_import(self.lib, files, self.did)
        self.assertEqual(counts, dict(added=0, updated=0, unchanged=3))
        self.assertEqual(import_notes(self.lib, files, self.did), counts)
        self.assertEqual(self.lib.col.card_count(), 5)
        with zipfile.ZipFile(io.BytesIO(zip_notes(files))) as archive:
            self.assertEqual(len(archive.namelist()), 3)
            self.assertTrue(all("anki_guid:" in archive.read(name).decode() for name in archive.namelist()))

    def test_markdown_updates_preserve_cards_schedule_and_history(self):
        added = self.lib.add_note(dict(deck_id=self.did, front="Вопрос", back="Ответ", tags="test"))
        card = self.lib.study(self.did)["card"]
        self.lib.answer(dict(token=card["token"], rating=4))
        before = self.lib.col.get_card(int(card["id"]))
        files = export_notes(self.lib)
        files[0]["content"] = files[0]["content"].replace("\nОтвет\n", "\n**Обновлённый ответ**\n")
        result = import_notes(self.lib, files, self.did)
        self.assertEqual(result["updated"], 1)
        self.assertIn("<strong>Обновлённый ответ</strong>", self.lib.col.get_note(int(added["id"])).fields[1])
        after = self.lib.col.get_card(before.id)
        self.assertEqual((after.id, after.due, after.ivl, after.reps, after.type),
                         (before.id, before.due, before.ivl, before.reps, before.type))
        self.assertEqual(self.lib.col.db.scalar("select count(*) from revlog"), 1)
        self.lib.undo({})
        self.assertEqual(self.lib.col.get_note(int(added["id"])).fields[1], "Ответ")

    def test_new_files_are_idempotent_and_batch_is_undoable(self):
        files = [simple_file(), simple_file("second.md", "Второй ответ")]
        _, preview = prepare_import(self.lib, files, self.did)
        self.assertEqual(preview["added"], 2)
        self.assertEqual(self.lib.col.note_count(), 0)
        self.assertEqual(import_notes(self.lib, files, self.did)["added"], 2)
        self.assertEqual(import_notes(self.lib, files, self.did)["unchanged"], 2)
        self.lib.undo({})
        self.assertEqual(self.lib.col.note_count(), 0)

    def test_import_into_another_collection_keeps_guid_and_deck(self):
        self.lib.add_note(dict(deck_id=self.did, kind="cloze", front="{{c1::A}} и {{c2::Б}}", back=""))
        files = export_notes(self.lib)
        other = Library(Path(self.tmp.name) / "other" / "collection.anki2")
        try:
            self.assertEqual(import_notes(other, files, "1")["added"], 1)
            self.assertEqual(other.col.card_count(), 2)
            self.assertEqual(export_notes(other)[0]["name"], files[0]["name"])
            self.assertIsNotNone(other.col.decks.id_for_name("Учёба::Тест"))
        finally:
            other.col.close()

    def test_bad_file_does_not_partially_import_and_duplicate_guid_is_rejected(self):
        with self.assertRaises(MarkdownError):
            import_notes(self.lib, [simple_file(), dict(name="bad.md", content="Нет вопроса и ответа")], self.did)
        self.assertEqual(self.lib.col.note_count(), 0)
        self.lib.add_note(dict(deck_id=self.did, front="Q", back="A"))
        files = export_notes(self.lib)
        duplicate = copy.deepcopy(files[0]); duplicate["name"] = "copy.md"
        with self.assertRaisesRegex(MarkdownError, "идентификатор"):
            import_notes(self.lib, files + [duplicate], self.did)

    def test_failed_batch_rolls_back_earlier_notes(self):
        original = self.lib.add_note
        calls = 0
        def fail_second(data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fixture failure")
            return original(data)
        with patch.object(self.lib, "add_note", side_effect=fail_second), self.assertRaises(RuntimeError):
            import_notes(self.lib, [simple_file(), simple_file("second.md")], self.did)
        self.assertEqual(self.lib.col.note_count(), 0)

    def test_handwritten_files_handle_yaml_markdown_and_cloze(self):
        file = dict(name="title.md", content="---\ntags: [география, test]\n---\n# Столица?\n\n**Париж**\n\n- Франция\n- Европа")
        result = parse_note(file, "Default")
        self.assertEqual(result["tags"], ["test", "география"])
        self.assertIn("<strong>Париж</strong>", result["fields"][1])
        self.assertIn("<ul>", result["fields"][1])
        self.assertEqual(parse_note(dict(name="cloze.md", content="{{c1::Один}} и {{c2::два}}"), "Default")["kind"], "cloze")
        code = dict(name="code.md", content="## Вопрос\n```md\n## Ответ\n```\n\n## Ответ\nНастоящий ответ")
        parsed = parse_note(code, "Default")
        self.assertIn("## Ответ", parsed["fields"][0])
        self.assertEqual(parsed["fields"][1], "<p>Настоящий ответ</p>")
        for content in ["---\n- list\n---\n# Q\nA", "---\ntags: [[bad]]\n---\n# Q\nA", "---\n!!python/object:os.system {}\n---\n# Q\nA"]:
            with self.subTest(content=content), self.assertRaises(MarkdownError):
                parse_note(dict(name="bad.md", content=content), "Default")


class FNSHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data):
        body = json.dumps(data).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Authorization"), self.headers.get("X-Client")))
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        if path.path.startswith("/redirect/"):
            self.send_response(302); self.send_header("Location", "/leaked"); self.end_headers(); return
        if path.path == "/api/notes":
            names = list(self.server.notes)
            page = int(query["page"][0]); size = int(query["pageSize"][0])
            return self.reply(dict(status=True, code=1, data=dict(list=[dict(path=n) for n in names[(page-1)*size:page*size]])))
        if path.path == "/api/note":
            name = query["path"][0]
            if name not in self.server.notes:
                return self.reply(dict(status=False, code=428))
            return self.reply(dict(status=True, code=1, data=dict(content=self.server.notes[name], contentHash="fixture-hash", ctime=1000)))
        return self.reply(dict(status=False, code=507))

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.posts.append(data)
        self.server.notes[data["path"]] = data["content"]
        self.reply(dict(status=True, code=1, data={}))


class FNSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.http = HTTPServer(("127.0.0.1", 0), FNSHandler)
        self.http.notes, self.http.requests, self.http.posts = {}, [], []
        self.thread = Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
        self.settings = Settings(Path(self.tmp.name) / "settings.json")
        self.settings.save(dict(url=f"http://127.0.0.1:{self.http.server_port}", vault="Учёба", folder="Anki", token="fixture-secret"))

    def tearDown(self):
        self.http.shutdown(); self.thread.join(timeout=5); self.http.server_close(); self.tmp.cleanup()

    def test_settings_keep_or_clear_secret_without_returning_it(self):
        public = self.settings.save(dict(vault="Новая"))
        self.assertNotIn("fixture-secret", json.dumps(public))
        self.assertTrue(public["has_token"])
        reloaded = Settings(self.settings.path)
        self.assertEqual(reloaded.values["token"], "fixture-secret")
        self.assertEqual(stat.S_IMODE(self.settings.path.stat().st_mode), 0o600)
        self.settings.save(dict(clear_token=True))
        self.assertFalse(self.settings.public()["has_token"])
        for values in [dict(url="file:///tmp"), dict(url="https://user:secret@example.com"), dict(folder="../escape"), dict(token="bad\ntoken")]:
            with self.subTest(values=values), self.assertRaises(MarkdownError):
                self.settings.save(values)

    def test_upload_is_repeatable_and_uses_authenticated_rest(self):
        content = "---\nanki_guid: test-guid\n---\n# Вопрос\n\nОтвет"
        files = [dict(name="test.md", content=content)]
        client = Client(self.settings)
        self.assertEqual(client.upload(files), dict(uploaded=1, unchanged=0))
        self.assertEqual(client.upload(files), dict(uploaded=0, unchanged=1))
        files[0]["content"] = content.replace("Ответ", "Новый ответ")
        self.assertEqual(client.upload(files)["uploaded"], 1)
        self.assertEqual(self.http.posts[-1]["baseHash"], "fixture-hash")
        self.assertEqual(self.http.posts[-1]["vault"], "Учёба")
        self.assertTrue(all(token == "Bearer fixture-secret" and client == "anki-web" for _, token, client in self.http.requests))

    def test_download_paginates_and_stays_in_selected_folder(self):
        self.http.notes = {f"Other/{i}.md": "unrelated" for i in range(99)}
        self.http.notes.update({"Anki/one.md": "# Q\nA", "Anki/two.md": "# Q2\nA2"})
        files = Client(self.settings).download()
        self.assertEqual([f["name"] for f in files], ["one.md", "two.md"])
        self.assertTrue(any("page=2" in path for path, _, _ in self.http.requests))
        details = [path for path, _, _ in self.http.requests if path.startswith("/api/note?")]
        self.assertEqual(len(details), 2)

    def test_redirect_does_not_forward_credentials(self):
        self.settings.save(dict(url=f"http://127.0.0.1:{self.http.server_port}/redirect"))
        with self.assertRaisesRegex(MarkdownError, "HTTP 302"):
            Client(self.settings).check()
        self.assertEqual(len(self.http.requests), 1)
