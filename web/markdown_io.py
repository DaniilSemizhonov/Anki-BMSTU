"""Portable Markdown notes. Scheduling stays in Anki, matched by note GUID."""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

import markdown
from markdownify import markdownify
import yaml

MAX_FILES = 500
MAX_BYTES = 10 * 1024 * 1024
LABELS = {"Front": "Вопрос", "Back": "Ответ", "Text": "Текст", "Back Extra": "Пояснение"}
KINDS = {"Anki Web Basic": "basic", "Anki Web Reversed": "reversed", "Anki Web Cloze": "cloze"}
MARKER = re.compile(r"^<!-- anki-field:(\d+) -->\n## [^\n]*(?:\n|$)", re.MULTILINE)


class MarkdownError(ValueError):
    pass


def outside_fences(content):
    """Offsets of lines outside code blocks, where field headings can delimit notes."""
    fence, offset = None, 0
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\n"))
        if marker and fence is None:
            fence = marker[1]
        elif marker and fence and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
            fence = None
        elif fence is None:
            yield offset, line
        offset += len(line)


def export_notes(lib):
    ids = lib.col.find_notes("")
    if len(ids) > MAX_FILES:
        raise MarkdownError(f"За один раз можно выгрузить до {MAX_FILES} заметок.")
    files = []
    for nid in ids:
        note = lib.col.get_note(nid)
        model = note.note_type()
        cards = note.cards()
        deck = lib.col.decks.get_legacy(cards[0].odid or cards[0].did)["name"] if cards else "Default"
        meta = dict(anki_format=1, anki_guid=note.guid, anki_kind=KINDS.get(model["name"], "custom"),
                    anki_notetype=model["name"], anki_deck=deck, tags=list(note.tags),
                    anki_fields=list(note.keys()))
        header = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items())
        sections = []
        for i, (name, value) in enumerate(note.items()):
            body = markdownify(value, heading_style="ATX", bullets="-").strip()
            sections.append(f"<!-- anki-field:{i} -->\n## {LABELS.get(name, name)}\n\n{body}")
        # Hex-encoding keeps imported/custom GUIDs out of filesystem paths.
        filename = note.guid.encode().hex() + ".md"
        files.append(dict(name=filename, content="---\n" + header + "\n---\n\n" + "\n\n".join(sections) + "\n"))
    validate_files(files, allow_empty=True)
    return files


def zip_notes(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.writestr(file["name"], file["content"])
    return out.getvalue()


def validate_files(files, allow_empty=False):
    if not isinstance(files, list) or len(files) > MAX_FILES or (not files and not allow_empty):
        raise MarkdownError(f"Выберите от 1 до {MAX_FILES} файлов Markdown.")
    total = 0
    names = set()
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get("name"), str) or not isinstance(file.get("content"), str):
            raise MarkdownError("Некорректный файл Markdown.")
        name = file["name"]
        if not name.lower().endswith(".md") or len(name) > 500 or name in names:
            raise MarkdownError("Нужны файлы .md с уникальными именами (до 500 символов).")
        names.add(name)
        total += len(file["content"].encode())
    if total > MAX_BYTES:
        raise MarkdownError("Общий размер Markdown — не более 10 МБ.")


def parse_note(file, fallback_deck):
    content = file["content"].lstrip("\ufeff").replace("\r\n", "\n")
    meta = {}
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end < 0 or end > 16000:
            raise MarkdownError("Не закрыт блок свойств YAML или он превышает 16 КБ.")
        try:
            meta = yaml.safe_load(content[4:end]) or {}
        except (yaml.YAMLError, RecursionError):
            raise MarkdownError("Не удалось прочитать свойства YAML.") from None
        if not isinstance(meta, dict):
            raise MarkdownError("Свойства YAML должны быть списком ключей и значений.")
        content = content[end + 5:].strip()
    if "anki_format" in meta:
        if type(meta["anki_format"]) is not int or meta["anki_format"] != 1:
            raise MarkdownError("Неизвестная версия формата Anki Markdown.")
        names = meta.get("anki_fields")
        offsets = {offset for offset, _ in outside_fences(content)}
        matches = [match for match in MARKER.finditer(content) if match.start() in offsets]
        if not isinstance(names, list) or not 1 <= len(names) <= 64 or any(not isinstance(n, str) or "\n" in n for n in names):
            raise MarkdownError("Некорректный список полей заметки.")
        if [int(m[1]) for m in matches] != list(range(len(names))):
            raise MarkdownError("В файле отсутствует поле или изменены разделители полей Anki.")
        values = [content[m.end():matches[i+1].start() if i+1 < len(matches) else len(content)].strip()
                  for i, m in enumerate(matches)]
    else:
        # Simple hand-written files use a question/answer pair, or a title + body.
        headings = [(offset, line) for offset, line in outside_fences(content) if re.fullmatch(r"## (?:Вопрос|Question|Ответ|Answer)\s*", line)]
        pair = len(headings) >= 2 and headings[0][1].strip() in ("## Вопрос", "## Question") and headings[1][1].strip() in ("## Ответ", "## Answer")
        title = re.fullmatch(r"\s*# ([^\n]+)\n+(.*)", content, re.DOTALL)
        if pair:
            question, answer = headings[:2]
            values = [content[question[0] + len(question[1]):answer[0]].strip(), content[answer[0] + len(answer[1]):].strip()]
        elif title:
            values = [title[1].strip(), title[2].strip()]
        elif re.search(r"\{\{c[1-9][0-9]*::.+?\}\}", content, re.DOTALL):
            values = [content.strip(), ""]
        else:
            raise MarkdownError("Используйте «## Вопрос» и «## Ответ» либо заголовок «# Вопрос» с ответом ниже.")
        names = None
    if not values[0] or any(len(v) > 100000 for v in values):
        raise MarkdownError("Заполните первое поле; каждое поле — не более 100000 символов.")
    kind = meta.get("anki_kind", "cloze" if re.search(r"\{\{c[1-9][0-9]*::.+?\}\}", values[0], re.DOTALL) else "basic")
    if not isinstance(kind, str) or kind not in ("basic", "reversed", "cloze", "custom"):
        raise MarkdownError("Неизвестный тип карточки Markdown.")
    if kind == "cloze" and not re.search(r"\{\{c[1-9][0-9]*::.+?\}\}", values[0], re.DOTALL):
        raise MarkdownError("Для cloze нужен пропуск {{c1::текст}}.")
    if kind in ("basic", "reversed", "cloze") and len(values) != 2:
        raise MarkdownError("Для стандартной карточки нужны два поля.")
    if kind in ("basic", "reversed") and (len(values) != 2 or not values[1]):
        raise MarkdownError("Для обычной и обратной карточки нужны вопрос и ответ.")
    deck = meta.get("anki_deck", fallback_deck)
    guid = meta.get("anki_guid")
    tags = meta.get("tags", [])
    if not isinstance(deck, str) or not deck.strip() or len(deck) > 200:
        raise MarkdownError("Название колоды — от 1 до 200 символов.")
    if guid is None:
        guid = hashlib.sha256((fallback_deck + "/" + file["name"]).encode()).hexdigest()[:20]
    if not isinstance(guid, str) or not guid or len(guid) > 100:
        raise MarkdownError("Некорректный идентификатор заметки.")
    if isinstance(tags, str):
        tags = tags.split()
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t or re.search(r"\s", t) for t in tags) or len(" ".join(tags)) > 1000:
        raise MarkdownError("Метки должны быть словами без пробелов (всего до 1000 символов).")
    model_name = meta.get("anki_notetype", "")
    if not isinstance(model_name, str) or len(model_name) > 200:
        raise MarkdownError("Некорректное название типа заметки.")
    return dict(guid=guid, kind=kind, model_name=model_name, names=names, deck=deck.strip(), tags=sorted(set(tags)),
                field_md=values, fields=[markdown.markdown(v, extensions=["fenced_code", "tables", "sane_lists"]) for v in values])


def prepare_import(lib, files, deck_id):
    validate_files(files)
    fallback = lib.col.decks.get_legacy(lib.deck(deck_id))["name"]
    records, seen = [], set()
    counts = dict(added=0, updated=0, unchanged=0)
    for file in files:
        try:
            record = parse_note(file, fallback)
            if record["guid"] in seen:
                raise MarkdownError("Две заметки имеют одинаковый идентификатор.")
            seen.add(record["guid"])
            nid = lib.col.db.scalar("select id from notes where guid=?", record["guid"])
            note = lib.col.get_note(nid) if nid else None
            model = note.note_type() if note else lib.col.models.by_name(record["model_name"]) if record["kind"] == "custom" else None
            if record["kind"] == "custom" and model is None:
                raise MarkdownError("Сначала импортируйте тип заметки через .apkg: " + record["model_name"])
            if model and (len(model["flds"]) != len(record["fields"]) or (record["names"] and record["names"] != [f["name"] for f in model["flds"]])):
                raise MarkdownError("Поля файла не совпадают с типом существующей заметки.")
            if note and record["kind"] != KINDS.get(model["name"], "custom"):
                raise MarkdownError("Markdown не может менять тип существующей заметки.")
            record["nid"] = nid
            same_fields = note and [markdownify(v, heading_style="ATX", bullets="-").strip() for v in note.fields] == record["field_md"]
            record["action"] = "unchanged" if same_fields and sorted(note.tags) == record["tags"] else "updated" if note else "added"
            counts[record["action"]] += 1
            records.append(record)
        except MarkdownError as exc:
            raise MarkdownError(f"{file['name']}: {exc}") from None
    return records, counts


def import_notes(lib, files, deck_id):
    records, counts = prepare_import(lib, files, deck_id)
    if not counts["added"] and not counts["updated"]:
        return counts
    undo = lib.col.add_custom_undo_entry("Импорт Markdown")
    try:
        for record in records:
            if record["action"] == "unchanged":
                continue
            if record["nid"]:
                note = lib.col.get_note(record["nid"])
                note.fields, note.tags = record["fields"], record["tags"]
                lib.col.update_note(note)
            else:
                did = lib.col.decks.id_for_name(record["deck"]) or lib.col.decks.add_normal_deck_with_name(record["deck"]).id
                if record["kind"] == "custom":
                    model = lib.col.models.by_name(record["model_name"])
                    note = lib.col.new_note(model)
                    note.guid, note.fields, note.tags = record["guid"], record["fields"], record["tags"]
                    lib.col.add_note(note, did)
                else:
                    added = lib.add_note(dict(deck_id=str(did), kind=record["kind"], front=record["fields"][0],
                                              back=record["fields"][1], format="html", tags=" ".join(record["tags"])))
                    note = lib.col.get_note(int(added["id"]))
                    note.guid = record["guid"]
                    lib.col.update_note(note)
        lib.col.merge_undo_entries(undo)
    except Exception:
        # Roll back the entire batch, including decks and note types, on failure.
        lib.col.merge_undo_entries(undo)
        lib.col.undo()
        raise
    finally:
        lib.pending = None
    return counts
