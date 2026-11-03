"""Local browser frontend for Anki. License: GNU AGPL-3.0-or-later."""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
from datetime import datetime
import secrets
import signal
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, unquote, urlsplit

from anki._backend import RustBackend
from anki.collection import Collection
from anki.cards import Card
from anki.errors import NotFoundError, SearchError, InvalidInput
from anki import import_export_pb2, generic_pb2, notetypes_pb2, deck_config_pb2
from anki.utils import from_json_bytes
from auth import Access, AuthenticationError

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 100 * 1024 * 1024


class InvalidRequest(Exception):
    pass


def integer(value):
    if isinstance(value, bool):
        raise InvalidRequest("Ожидалось целое число.")
    try:
        return int(str(value))
    except (ValueError, TypeError):
        raise InvalidRequest("Ожидалось целое число.") from None


def text(value, limit=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InvalidRequest(f"Введите текст длиной от 1 до {limit} символов.")
    return value.strip()


class Library:
    def __init__(self, path):
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.col = Collection(str(path), backend=RustBackend(langs=["ru"]))
        self.pending = None
        self.csrf = secrets.token_urlsafe(32)
        self.last_answer_step = None

    def deck(self, value):
        did = integer(value)
        if not self.col.decks.get_legacy(did):
            raise InvalidRequest("Колода не найдена.")
        return did

    def overview(self):
        col = self.col
        tree = col.sched.deck_due_tree()
        counts = {}
        def walk(node):
            counts[node.deck_id] = dict(new=node.new_count, learning=node.learn_count, review=node.review_count)
            for child in node.children:
                walk(child)
        walk(tree)
        decks = []
        names = list(col.decks.all_names_and_ids())
        totals = dict(col.db.all("select did, count(*) from cards group by did"))
        for deck in names:
            decks.append(dict(id=str(deck.id), name=deck.name,
                total=sum(totals.get(d.id, 0) for d in names if d.id == deck.id or d.name.startswith(deck.name + "::")),
                **counts.get(deck.id, dict(new=0, learning=0, review=0))))
        cutoff = (col.sched.day_cutoff - 86400) * 1000
        reviewed, millis = col.db.first("select count(*), coalesce(sum(time),0) from revlog where ease>0 and id>=?", cutoff)
        return dict(decks=decks, total=col.card_count(), reviewed=reviewed,
                    minutes=round(millis / 60000, 1), csrf=self.csrf, undo=col.undo_status().undo)

    def notes(self, query):
        search = query.get("search", [""])[0][:500]
        if query.get("deck_id", [""])[0]:
            did = self.deck(query["deck_id"][0])
            name = self.col.decks.get_legacy(did)["name"].replace("\\", "\\\\").replace('"', '\\"')
            search = f'deck:"{name}"' + (f" ({search})" if search else "")
        ids = self.col.find_notes(search, order="n.id desc")
        notes = []
        for nid in ids[:200]:
            note = self.col.get_note(nid)
            notes.append(dict(id=str(nid), fields=[dict(name=k, value=v) for k,v in note.items()], tags=note.tags,
                              cards=[dict(id=str(c.id), suspended=c.queue == -1) for c in note.cards()]))
        return dict(notes=notes, total=len(ids))

    def study(self, did):
        self.col.decks.select(self.deck(did))
        self.pending = None
        return self.next_card()

    def next_card(self):
        queued = self.col.sched.get_queued_cards()
        counts = dict(new=queued.new_count, learning=queued.learning_count, review=queued.review_count)
        can_undo = self.last_answer_step is not None and self.last_answer_step == self.col.undo_status().last_step
        if not queued.cards:
            self.pending = None
            return dict(card=None, counts=counts, can_undo=can_undo)
        item = queued.cards[0]
        card = Card(self.col, backend_card=item.card)
        card.start_timer()
        states = item.states
        token = secrets.token_urlsafe(24)
        self.pending = (token, card, states)
        from anki.sound import SoundOrVideoTag, strip_av_refs
        def media(tags):
            return [tag.filename for tag in tags if isinstance(tag, SoundOrVideoTag)]
        return dict(card=dict(question_media=media(card.question_av_tags()), answer_media=media(card.answer_av_tags()), id=str(card.id), token=token, question=strip_av_refs(card.question()), answer=strip_av_refs(card.answer()),
                             intervals=list(self.col.sched.describe_next_states(states))), counts=counts, can_undo=can_undo)

    def answer(self, data):
        rating = integer(data.get("rating"))
        if rating not in (1,2,3,4):
            raise InvalidRequest("Оценка должна быть от 1 до 4.")
        if not self.pending or data.get("token") != self.pending[0]:
            raise InvalidRequest("Эта карточка уже изменилась. Начните повторение заново.")
        _, card, states = self.pending
        answer = self.col.sched.build_answer(card=card, states=states, rating=rating-1)
        self.col.sched.answer_card(answer)
        self.last_answer_step = self.col.undo_status().last_step
        self.pending = None
        return self.next_card()

    def note_types(self):
        models = [dict(id=str(m.id), name=m.name) for m in self.col.models.all_names_and_ids()]
        return dict(models=models, stock=[dict(id=k, name=n) for k,n in
                    [("basic", "Вопрос — ответ"), ("reversed", "Прямая и обратная"), ("cloze", "Пропуски (cloze)")]])

    def add_note(self, data):
        did = self.deck(data.get("deck_id"))
        kind = data.get("kind", "basic")
        specs = {"basic": (notetypes_pb2.StockNotetype.KIND_BASIC, "Anki Web Basic"),
                 "reversed": (notetypes_pb2.StockNotetype.KIND_BASIC_AND_REVERSED, "Anki Web Reversed"),
                 "cloze": (notetypes_pb2.StockNotetype.KIND_CLOZE, "Anki Web Cloze")}
        if kind not in specs:
            raise InvalidRequest("Неизвестный тип карточки.")
        front = text(data.get("front"))
        back = data.get("back", "")
        if not isinstance(back, str) or len(back) > 20000 or (kind != "cloze" and not back.strip()):
            raise InvalidRequest("Заполните ответ (до 20000 символов).")
        if kind == "cloze" and not re.search(r"\{\{c[1-9][0-9]*::.+?\}\}", front, re.DOTALL):
            raise InvalidRequest("Выделите пропуск: {{c1::текст}}, {{c2::другой текст}}.")
        tags = self.tags(data.get("tags", ""))
        stock, name = specs[kind]
        model = self.col.models.by_name(name)
        if model is None:
            model = from_json_bytes(self.col._backend.get_stock_notetype_legacy(stock))
            model["name"] = name
            self.col.models.add(model)
        note = self.col.new_note(model)
        values = [front, back]
        note.fields = values if data.get("format") == "html" else [html.escape(v).replace("\n", "<br>") for v in values]
        note.tags = tags
        self.col.add_note(note, did)
        self.pending = None
        return dict(id=str(note.id), cards=len(note.cards()))

    @staticmethod
    def tags(value):
        if not isinstance(value, str) or len(value) > 1000:
            raise InvalidRequest("Некорректные метки (максимум 1000 символов).")
        return value.split()

    def edit_note(self, data):
        note = self.col.get_note(integer(data.get("id")))
        fields = data.get("fields")
        if not isinstance(fields, list) or len(fields) != len(note.fields):
            raise InvalidRequest("Неверное количество полей.")
        if any(not isinstance(v, str) or len(v) > 100000 for v in fields) or not fields[0].strip():
            raise InvalidRequest("Заполните первое поле. Максимальная длина поля — 100000 символов.")
        tags = self.tags(data["tags"]) if "tags" in data else note.tags
        note.fields = fields
        note.tags = tags
        self.col.update_note(note)
        self.pending = None
        return dict(ok=True)

    def undo(self, data):
        if not self.col.undo_status().undo:
            raise InvalidRequest("Нечего отменять.")
        if data.get("review_only") and self.last_answer_step != self.col.undo_status().last_step:
            raise InvalidRequest("Последнее действие уже изменилось.")
        self.col.undo()
        self.pending = None
        self.last_answer_step = None
        return dict(ok=True)

    def card_action(self, data):
        if "note_id" in data:
            note = self.col.get_note(integer(data["note_id"]))
            ids = [card.id for card in note.cards()]
        else:
            cid = integer(data.get("card_id"))
            self.col.get_card(cid)
            ids = [cid]
        action = data.get("action")
        if action == "suspend":
            self.col.sched.suspend_cards(ids)
        elif action == "resume":
            self.col.sched.unsuspend_cards(ids)
        elif action == "bury":
            self.col.sched.bury_cards(ids)
        else:
            raise InvalidRequest("Неизвестное действие.")
        self.pending = None
        return dict(ok=True)

    def remove_note(self, data):
        nid = integer(data.get("id"))
        self.col.get_note(nid)
        self.col.remove_notes([nid])
        self.pending = None
        return dict(ok=True)

    def upload_media(self, filename, content):
        suffix = Path(filename).suffix.lower()
        allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp3", ".m4a", ".ogg", ".wav", ".mp4", ".webm"}
        if suffix not in allowed or not content or len(content) > 10*1024*1024:
            raise InvalidRequest("Выберите изображение, звук или видео размером до 10 МБ.")
        # Anki handles collisions. Never accept a path or executable HTML/SVG.
        name = "web-" + secrets.token_hex(8) + suffix
        stored = self.col.media.write_data(name, content)
        return dict(filename=stored, image=suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"})

    def statistics(self):
        start = self.col.sched.day_cutoff - 30 * 86400
        rows = self.col.db.all("select cast((id/1000-?)/86400 as integer), count(*), sum(time), sum(case when ease=1 then 1 else 0 end) from revlog where ease>0 and id>=? and id<? group by 1", start, start*1000, self.col.sched.day_cutoff*1000)
        by_day = {row[0]:row[1:] for row in rows}
        days = [dict(date=datetime.fromtimestamp(start+i*86400).strftime("%d.%m"), reviews=by_day.get(i,(0,0,0))[0]) for i in range(30)]
        distribution = {str(k):v for k,v in self.col.db.all("select case when queue=-1 then -1 else type end, count(*) from cards group by 1")}
        return dict(days=days, reviews=sum(r[1] for r in rows), minutes=round(sum(r[2] for r in rows)/60000,1),
                    again=sum(r[3] for r in rows), distribution=distribution)

    def deck_settings(self, value):
        did = self.deck(value)
        info = self.col.decks.get_deck_configs_for_update(did)
        conf = next(e.config for e in info.all_config if e.config.id == info.current_deck.config_id)
        limits = info.current_deck.limits
        return dict(id=str(did), name=info.current_deck.name, fsrs=info.fsrs,
                    new=limits.new if limits.HasField("new") else conf.config.new_per_day,
                    review=limits.review if limits.HasField("review") else conf.config.reviews_per_day)

    def save_deck_settings(self, data):
        did = self.deck(data.get("id"))
        new, review = integer(data.get("new")), integer(data.get("review"))
        if not (0 <= new <= 9999 and 0 <= review <= 9999):
            raise InvalidRequest("Дневные лимиты должны быть от 0 до 9999.")
        info = self.col.decks.get_deck_configs_for_update(did)
        limits = info.current_deck.limits
        setattr(limits, "new", new)
        limits.review = review
        selected = next(e.config for e in info.all_config if e.config.id == info.current_deck.config_id)
        request = deck_config_pb2.UpdateDeckConfigsRequest(target_deck_id=did, configs=[selected], limits=limits,
            fsrs=info.fsrs, fsrs_health_check=info.fsrs_health_check,
            card_state_customizer=info.card_state_customizer,
            new_cards_ignore_review_limit=info.new_cards_ignore_review_limit,
            apply_all_parent_limits=info.apply_all_parent_limits)
        self.col.decks.update_deck_configs(request)
        self.pending = None
        return self.deck_settings(did)

    def import_package(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "import.apkg"
            path.write_bytes(content)
            result = self.col.import_anki_package(import_export_pb2.ImportAnkiPackageRequest(
                package_path=str(path), options=import_export_pb2.ImportAnkiPackageOptions(
                    with_scheduling=True, with_deck_configs=True)))
        self.pending = None
        return dict(added=len(result.log.new), updated=len(result.log.updated))

    def export_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "anki-web.apkg"
            self.col.export_anki_package(out_path=str(path),
                options=import_export_pb2.ExportAnkiPackageOptions(with_scheduling=True, with_deck_configs=True, with_media=True),
                limit=import_export_pb2.ExportLimit(whole_collection=generic_pb2.Empty()))
            return path.read_bytes()


class LocalServer(HTTPServer):
    def server_bind(self):
        # HTTPServer normally reverse-resolves 127.0.0.1 here. No DNS is needed
        # for this loopback-only service, and offline resolvers can stall startup.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server_version = "AnkiWeb/2.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    @property
    def library(self):
        return self.server.library

    @property
    def access(self):
        if not hasattr(self.server, "access"):
            self.server.access = Access()
        return self.server.access

    def respond(self, status, content, mime="application/json; charset=utf-8", extra=None):
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False).encode()
        elif isinstance(content, str):
            content = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        sdk = " https://telegram.org" if self.access.bot_token else ""
        ancestors = " https://web.telegram.org" if self.access.bot_token else ""
        policy = (f"default-src 'self'; script-src 'self'{sdk}; style-src 'self' 'unsafe-inline'; "
                  f"frame-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'{ancestors}")
        if extra and "Content-Security-Policy" in extra:
            policy = extra["Content-Security-Policy"]
        self.send_header("Content-Security-Policy", policy)
        for k,v in (extra or {}).items():
            if k != "Content-Security-Policy":
                self.send_header(k,v)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, write):
        try:
            access = self.access
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origins = {f"http://{h}" for h in allowed}
            if access.public_url:
                allowed.add(urlsplit(access.public_url).netloc)
                origins.add(access.public_url)
            if self.headers.get("Host") not in allowed:
                return self.respond(403, dict(error="Недопустимый адрес сервера."))
            path = urlsplit(self.path)
            session = access.session(self.headers.get("Cookie")) if access.required else None
            authenticated = not access.required or session is not None
            csrf = session["csrf"] if session else self.library.csrf
            if not write and path.path == "/api/session":
                return self.respond(200, dict(authenticated=authenticated, password=bool(access.password_hash),
                    telegram=bool(access.bot_token), remote=bool(access.public_url), csrf=csrf if authenticated else None))
            if not write and path.path == "/health":
                return self.respond(200, dict(ok=True))
            private = path.path.startswith(("/api/", "/media/"))
            if private and not authenticated and path.path != "/api/login":
                return self.respond(401, dict(error="Войдите, чтобы открыть коллекцию."))
            if write:
                origin = self.headers.get("Origin")
                if origin and origin not in origins:
                    return self.respond(403, dict(error="Недопустимый источник запроса."))
                if path.path == "/api/login":
                    if self.headers.get("X-Anki-Client") != "web":
                        return self.respond(403, dict(error="Недопустимый запрос входа."))
                elif not secrets.compare_digest(self.headers.get("X-Anki-Token", ""), csrf):
                    return self.respond(403, dict(error="Обновите страницу перед сохранением."))
                length = integer(self.headers.get("Content-Length", 0))
                limit = MAX_UPLOAD if path.path == "/api/import" else 10*1024*1024 if path.path == "/api/media" else 1024*1024
                if self.headers.get("Transfer-Encoding") or length < 0 or length > limit:
                    return self.respond(413, dict(error="Превышен допустимый размер запроса."))
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise InvalidRequest("Запрос получен не полностью.")
                if path.path == "/api/import":
                    return self.respond(200, self.library.import_package(raw))
                if path.path == "/api/media":
                    return self.respond(200, self.library.upload_media(unquote(self.headers.get("X-Filename", "")), raw))
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise InvalidRequest("Некорректный JSON.") from None
                if not isinstance(data, dict):
                    raise InvalidRequest("Ожидался JSON-объект.")
                if path.path == "/api/login":
                    cookie = access.login(data)
                    return self.respond(200, dict(ok=True), extra={"Set-Cookie":cookie})
                if path.path == "/api/logout":
                    return self.respond(200, dict(ok=True), extra={"Set-Cookie":access.logout(self.headers.get("Cookie"))})
                lib = self.library
                if path.path == "/api/decks":
                    name = text(data.get("name"), 200)
                    if lib.col.decks.id_for_name(name):
                        raise InvalidRequest("Такая колода уже существует.")
                    result = dict(id=str(lib.col.decks.add_normal_deck_with_name(name).id))
                    lib.pending = None
                elif path.path == "/api/decks/rename":
                    did, name = lib.deck(data.get("id")), text(data.get("name"), 200)
                    existing = lib.col.decks.id_for_name(name)
                    if existing and existing != did:
                        raise InvalidRequest("Такая колода уже существует.")
                    lib.col.decks.rename(did, name)
                    lib.pending = None
                    result = dict(ok=True)
                else:
                    methods = {"/api/notes": lib.add_note, "/api/notes/edit":lib.edit_note,
                               "/api/notes/remove":lib.remove_note, "/api/answer":lib.answer,
                               "/api/cards/action":lib.card_action, "/api/undo":lib.undo,
                               "/api/decks/settings":lib.save_deck_settings}
                    if path.path == "/api/study":
                        result = lib.study(data.get("deck_id"))
                    elif path.path in methods:
                        result = methods[path.path](data)
                    else:
                        return self.respond(404, dict(error="Не найдено."))
                return self.respond(200, result)
            lib = self.library
            if path.path == "/api/overview":
                result = lib.overview()
                result["csrf"] = csrf
                return self.respond(200, result)
            if path.path == "/api/notes":
                return self.respond(200, lib.notes(parse_qs(path.query)))
            if path.path == "/api/notetypes":
                return self.respond(200, lib.note_types())
            if path.path == "/api/statistics":
                return self.respond(200, lib.statistics())
            if path.path == "/api/decks/settings":
                return self.respond(200, lib.deck_settings(parse_qs(path.query).get("id", [None])[0]))
            if path.path == "/api/export":
                return self.respond(200, lib.export_package(), "application/octet-stream",
                                    {"Content-Disposition": 'attachment; filename="anki-web.apkg"'})
            if path.path.startswith("/media/"):
                base = Path(lib.col.media.dir()).resolve()
                target = (base / unquote(path.path[7:])).resolve()
                if target.parent != base or not target.is_file():
                    return self.respond(404, dict(error="Файл не найден."))
                return self.respond(200, target.read_bytes(), mimetypes.guess_type(str(target))[0] or "application/octet-stream",
                                    {"Content-Security-Policy": "default-src 'none'; sandbox"})
            if getattr(self.server, "api_only", False):
                return self.respond(404, dict(error="Статику обслуживает контейнер web."))
            routes = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
            if path.path not in routes:
                return self.respond(404, dict(error="Не найдено."))
            file = ROOT / "static" / routes[path.path]
            content = file.read_bytes()
            return self.respond(200, content, mimetypes.guess_type(str(file))[0] + "; charset=utf-8")
        except AuthenticationError as exc:
            self.respond(401, dict(error=str(exc)))
        except (InvalidRequest, NotFoundError, SearchError, InvalidInput) as exc:
            self.respond(400, dict(error=str(exc)))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.respond(500, dict(error="Не удалось выполнить операцию. Подробности — в терминале сервера."))


def main():
    parser = argparse.ArgumentParser(description="Локальная веб-версия Anki")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-only", action="store_true", help="Статика обслуживается отдельным web-контейнером")
    parser.add_argument("--collection", type=Path, default=ROOT / "data" / "collection.anki2")
    args = parser.parse_args()
    try:
        access = Access(public_url=os.environ.get("ANKI_PUBLIC_URL", ""), password=os.environ.get("ANKI_WEB_PASSWORD", ""),
            bot_token=os.environ.get("ANKI_TELEGRAM_BOT_TOKEN", ""), telegram_user_id=os.environ.get("ANKI_TELEGRAM_USER_ID", ""))
    except ValueError as exc:
        parser.error(str(exc))
    if args.host not in ("127.0.0.1", "localhost") and not (access.required and access.public_url):
        parser.error("Для внешнего интерфейса нужны ANKI_PUBLIC_URL и авторизация.")
    server = LocalServer((args.host, args.port), Handler)
    server.access = access
    server.api_only = args.api_only
    # Docker/Compose sends SIGTERM on stop unless overridden. Close Anki cleanly.
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.library = Library(args.collection)
        print(f"Anki Web: http://127.0.0.1:{server.server_port}", flush=True)
        print(f"Коллекция: {args.collection.resolve()}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if hasattr(server, "library"):
            server.library.col.close()


if __name__ == "__main__":
    main()
