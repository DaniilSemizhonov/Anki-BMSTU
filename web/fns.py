"""Manual Fast Note Sync exchange; credentials never leave the backend response boundary."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from markdown_io import MAX_BYTES, MAX_FILES, MarkdownError, validate_files


class MissingNote(MarkdownError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Settings:
    def __init__(self, path):
        self.path = Path(path)
        self.values = dict(url="", vault="", folder="Anki", token="")
        if self.path.exists():
            self.values.update(json.loads(self.path.read_text()))

    def public(self):
        return dict(url=self.values["url"], vault=self.values["vault"], folder=self.values["folder"],
                    has_token=bool(self.values["token"]), configured=bool(self.values["url"] and self.values["vault"] and self.values["token"]))

    def save(self, data):
        values = dict(self.values)
        for key in ("url", "vault", "folder"):
            value = data.get(key, values[key])
            if not isinstance(value, str) or len(value) > 500 or any(ord(c) < 32 for c in value):
                raise MarkdownError("Некорректные настройки хранилища.")
            values[key] = value.strip().rstrip("/")
        try:
            url = urlsplit(values["url"])
            url.port
        except ValueError:
            raise MarkdownError("Некорректный адрес или порт Fast Note Sync.") from None
        if values["url"] and (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment):
            raise MarkdownError("Укажите адрес http:// или https:// без пароля, параметров и фрагмента.")
        if any(part in (".", "..", "") for part in values["folder"].split("/")) or "\\" in values["folder"]:
            raise MarkdownError("Укажите относительную папку, например Anki или Учёба/Anki.")
        token = data.get("token", "")
        if not isinstance(token, str) or len(token) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in token):
            raise MarkdownError("Некорректный токен доступа.")
        if data.get("clear_token"):
            values["token"] = ""
        elif token:
            values["token"] = token.strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace + owner-only permissions; same persistent volume as the collection.
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".markdown-settings-")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(values, out, ensure_ascii=False)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.values = values
        return self.public()


class Client:
    def __init__(self, settings):
        self.config = dict(settings.values)
        if not settings.public()["configured"]:
            raise MarkdownError("Сначала сохраните адрес, хранилище и токен Fast Note Sync.")
        self.base = self.config["url"].removesuffix("/api")
        self.opener = build_opener(NoRedirect())
        self.deadline = time.monotonic() + 60

    def request(self, path, data=None, query=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MarkdownError("Обмен занял больше минуты. Повторите операцию или выберите меньшую папку.")
        url = self.base + "/api/" + path
        if query:
            url += "?" + urlencode(dict(vault=self.config["vault"], **query))
        body = json.dumps(dict(vault=self.config["vault"], **data), ensure_ascii=False).encode() if data is not None else None
        headers = {"Authorization": "Bearer " + self.config["token"], "X-Client": "anki-web", "X-Client-Name": "Anki Web",
                   "X-Client-Version": "2", "Content-Type": "application/json"}
        try:
            with self.opener.open(Request(url, data=body, headers=headers), timeout=min(10, remaining)) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise MarkdownError("Ответ Fast Note Sync превышает 10 МБ.")
            result = json.loads(raw)
        except HTTPError as exc:
            exc.close()
            raise MarkdownError(f"Fast Note Sync вернул HTTP {exc.code}. Проверьте адрес, токен и доступ к хранилищу.") from None
        except (URLError, TimeoutError, OSError):
            raise MarkdownError("Не удалось связаться с Fast Note Sync. Проверьте адрес и доступность сервиса.") from None
        except (ValueError, UnicodeError):
            raise MarkdownError("Fast Note Sync вернул некорректный ответ.") from None
        if not isinstance(result, dict):
            raise MarkdownError("Неожиданный формат ответа Fast Note Sync.")
        if result.get("code") == 428:
            raise MissingNote("Файл ещё не создан.")
        if result.get("status") is not True:
            raise MarkdownError("Fast Note Sync отклонил запрос. Проверьте токен, права REST для anki-web и имя хранилища.")
        return result.get("data")

    def check(self):
        self.request("notes", query=dict(page=1, pageSize=1))
        return dict(ok=True)

    def download(self):
        prefix = self.config["folder"] + "/"
        files, page = [], 1
        while True:
            result = self.request("notes", query=dict(page=page, pageSize=100, sortBy="path", sortOrder="asc"))
            if not isinstance(result, dict) or not isinstance(result.get("list"), list):
                raise MarkdownError("Не удалось прочитать список файлов Fast Note Sync.")
            rows = result["list"]
            for row in rows:
                if not isinstance(row, dict):
                    raise MarkdownError("Неожиданный формат списка Fast Note Sync.")
                path = row.get("path", "")
                if not isinstance(path, str) or not path.startswith(prefix) or not path.lower().endswith(".md"):
                    continue
                if len(files) >= MAX_FILES:
                    raise MarkdownError(f"В папке больше {MAX_FILES} Markdown-файлов. Разделите импорт на несколько папок.")
                detail = self.request("note", query=dict(path=path))
                if not isinstance(detail, dict) or not isinstance(detail.get("content"), str):
                    raise MarkdownError("Не удалось прочитать Markdown из хранилища.")
                files.append(dict(name=path[len(prefix):], content=detail["content"]))
                validate_files(files)
            if len(rows) < 100:
                break
            page += 1
            if page > 100:
                raise MarkdownError("Слишком большое хранилище. Выберите отдельное хранилище для карточек Anki.")
        if not files:
            raise MarkdownError("В выбранной папке нет файлов .md.")
        validate_files(files)
        return files

    def upload(self, files):
        if not files:
            raise MarkdownError("В коллекции пока нет заметок для выгрузки.")
        validate_files(files)
        uploaded, unchanged = 0, 0
        for file in files:
            path = self.config["folder"] + "/" + file["name"]
            try:
                try:
                    current = self.request("note", query=dict(path=path))
                except MissingNote:
                    current = None
                if current is not None and (not isinstance(current, dict) or not isinstance(current.get("content"), str)):
                    raise MarkdownError("Неожиданный формат заметки Fast Note Sync.")
                if current and current.get("content") == file["content"]:
                    unchanged += 1
                    continue
                # Existing files at our stable paths must actually belong to this GUID.
                if current:
                    from markdown_io import parse_note
                    old = parse_note(dict(name=file["name"], content=current.get("content", "")), "Default")
                    new = parse_note(file, "Default")
                    if old["guid"] != new["guid"]:
                        raise MarkdownError("В хранилище уже есть другой файл по этому пути.")
                now = int(time.time() * 1000)
                data = dict(path=path, content=file["content"], mtime=now, ctime=current.get("ctime", now) if current else now,
                            createOnly=current is None)
                if current and current.get("contentHash"):
                    data["baseHash"] = current["contentHash"]
                self.request("note", data=data)
                uploaded += 1
            except MarkdownError as exc:
                raise MarkdownError(f"Выгружено {uploaded}, без изменений {unchanged}. {exc} Повторите выгрузку после исправления настроек.") from None
        return dict(uploaded=uploaded, unchanged=unchanged)
