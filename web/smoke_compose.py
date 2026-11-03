"""Exercise the actual two-container deployment using disposable CI data. AGPL-3.0-or-later."""
import argparse
import base64
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description="Smoke test for a disposable Compose deployment")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    origin = os.environ["ANKI_PUBLIC_URL"]
    password = os.environ["ANKI_WEB_PASSWORD"]
    base = f"http://127.0.0.1:{os.environ.get('WEB_PORT', '8080')}"
    cookie = ""
    csrf = ""

    def request(path, data=None, *, authenticated=False, raw=False, filename=None):
        headers = {"Host": urlsplit(origin).netloc, "Origin": origin}
        if authenticated:
            headers.update({"Cookie": cookie, "X-Anki-Token": csrf})
        if data is not None:
            if isinstance(data, dict):
                data = json.dumps(data).encode()
                headers["Content-Type"] = "application/json"
            else:
                headers["Content-Type"] = "application/octet-stream"
            headers["X-Anki-Client"] = "web"
        if filename:
            headers["X-Filename"] = filename
        with urlopen(Request(base+path, data=data, headers=headers), timeout=30) as response:
            content = response.read()
            result = content if raw else json.loads(content)
            return result, response.headers

    def expect_unauthorized(path):
        try:
            request(path)
        except HTTPError as error:
            code = error.code
            error.close()
            assert code == 401, (path, code)
        else:
            raise AssertionError(f"Unauthorized access was accepted: {path}")

    page, _ = request("/", raw=True)
    assert b'/app.js' in page
    for path in ("/app.js", "/style.css", "/health"):
        content, _ = request(path, raw=True)
        assert content, path
    expect_unauthorized("/api/overview")
    _, headers = request("/api/login", dict(method="password", password=password))
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert "Secure" in headers["Set-Cookie"]
    overview, _ = request("/api/overview", authenticated=True)
    csrf = overview["csrf"]
    if args.verify_only:
        assert overview["total"] == 1, overview["total"]
        assert overview["reviewed"] == 1, overview["reviewed"]
        print("Saved card and review survived the backend restart.")
        return

    assert overview["total"] == 0, "Run this test only against a disposable, empty collection."
    deck, _ = request("/api/decks", dict(name="Compose smoke test"), authenticated=True)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jVvsAAAAASUVORK5CYII=")
    media, _ = request("/api/media", png, authenticated=True, filename="fixture.png")
    image, _ = request("/media/"+media["filename"], authenticated=True, raw=True)
    assert image == png
    expect_unauthorized("/media/"+media["filename"])
    request("/api/notes", dict(deck_id=deck["id"],front=f'<img src="{media["filename"]}">Smoke question',back="Smoke answer",format="html"), authenticated=True)
    review, _ = request("/api/study", dict(deck_id=deck["id"]), authenticated=True)
    assert "Smoke question" in review["card"]["question"]
    request("/api/answer", dict(token=review["card"]["token"],rating=4), authenticated=True)
    archive, _ = request("/api/export", authenticated=True, raw=True)
    assert archive.startswith(b"PK")
    print("Web assets, authentication, media, review and APKG export passed through Nginx.")


if __name__ == "__main__":
    main()
