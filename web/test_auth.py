"""Authentication checks with signed Telegram fixtures and a real HTTP server."""
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from auth import Access, AuthenticationError, validate_telegram
from server import Library, Handler, LocalServer

TEST_TOKEN = "123456:test-token-not-a-secret"
TEST_OWNER = 12345
TEST_NOW = 1800000000


def signed_data(**overrides):
    data = dict(auth_date=str(TEST_NOW), user=json.dumps(dict(id=TEST_OWNER,first_name="Owner")), query_id="fixture-query")
    data.update(overrides)
    key = hmac.digest(b"WebAppData", TEST_TOKEN.encode(), "sha256")
    check = "\n".join(f"{k}={v}" for k,v in sorted(data.items()))
    data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class TelegramTests(unittest.TestCase):
    def test_owner_is_accepted_only_with_valid_fresh_signature(self):
        self.assertEqual(validate_telegram(signed_data(), TEST_TOKEN, TEST_OWNER, now=TEST_NOW),TEST_OWNER)
        cases = [
            signed_data().replace("Owner","Attacker"),
            signed_data(auth_date=str(TEST_NOW-301)),
            signed_data(auth_date=str(TEST_NOW+31)),
            signed_data(user=json.dumps(dict(id=99999))),
            signed_data()+"&auth_date="+str(TEST_NOW),
            signed_data(user="[]"),
            "user=missing-hash",
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(AuthenticationError):
                    validate_telegram(payload,TEST_TOKEN,TEST_OWNER,now=TEST_NOW)

    def test_telegram_session_is_restricted_to_owner(self):
        access = Access(bot_token=TEST_TOKEN, telegram_user_id=str(TEST_OWNER),clock=lambda:TEST_NOW)
        cookie = access.login(dict(method="telegram", init_data=signed_data()))
        self.assertIsNotNone(access.session(cookie))
        with self.assertRaises(AuthenticationError):
            access.login(dict(method="telegram",init_data=signed_data(user=json.dumps(dict(id=99999)))))

    def test_configuration_session_expiry_and_throttling(self):
        for kwargs in [dict(public_url="https://study.example"), dict(public_url="http://study.example",password="long-test-password"),dict(bot_token=TEST_TOKEN),dict(password="short")]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Access(**kwargs)
        clock=[TEST_NOW]
        access=Access(public_url="https://study.example",password="long-test-password",clock=lambda:clock[0])
        cookie=access.login(dict(method="password",password="long-test-password"))
        self.assertIn("HttpOnly",cookie)
        self.assertIn("Secure",cookie)
        self.assertIsNotNone(access.session(cookie))
        clock[0]+=86401
        self.assertIsNone(access.session(cookie))
        for _ in range(10):
            with self.assertRaises(AuthenticationError):
                access.login(dict(method="password",password="wrong"))
        with self.assertRaises(AuthenticationError):
            access.login(dict(method="password",password="long-test-password"))
        clock[0]+=61
        self.assertIsNotNone(access.session(access.login(dict(method="password",password="long-test-password"))))


class ProtectedHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.http=LocalServer(("127.0.0.1",0),Handler)
        self.http.library=Library(Path(self.tmp.name)/"collection.anki2")
        self.http.access=Access(password="long-test-password")
        self.thread=Thread(target=self.http.serve_forever,daemon=True)
        self.thread.start()
        self.url=f"http://127.0.0.1:{self.http.server_port}"
        self.cookie=""

    def tearDown(self):
        self.http.shutdown()
        self.thread.join(timeout=5)
        self.http.server_close()
        self.http.library.col.close()
        self.tmp.cleanup()

    def request(self,path,data=None,headers=None):
        headers=dict(headers or {})
        if self.cookie:
            headers["Cookie"]=self.cookie
        if data is not None:
            data=json.dumps(data).encode()
            headers["Content-Type"]="application/json"
        return urlopen(Request(self.url+path,data=data,headers=headers),timeout=5)

    def test_collection_media_and_export_require_login(self):
        for path in ["/api/overview","/api/notes","/api/export","/media/test.png", "/api/settings", "/api/markdown/export", "/api/markdown/example"]:
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as error:
                    self.request(path)
                self.assertEqual(error.exception.code,401)
                error.exception.close()
        with self.request("/api/session") as response:
            self.assertIsNone(json.load(response)["csrf"])
        with self.request("/") as response:
            self.assertEqual(response.status,200)

    def test_api_container_serves_api_but_no_static_files(self):
        self.http.api_only=True
        with self.request("/health") as response:
            self.assertTrue(json.load(response)["ok"])
        for path in ["/", "/app.js", "/style.css"]:
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as error:
                    self.request(path)
                self.assertEqual(error.exception.code,404)
                error.exception.close()
        with self.request("/api/session") as response:
            data=json.load(response)
            self.assertFalse(data["authenticated"])
            self.assertTrue(data["password"])

    def test_reverse_proxy_preserves_the_public_host_and_origin(self):
        self.http.access=Access(public_url="https://anki.test.example",password="long-test-password")
        headers={"Host":"anki.test.example","Origin":"https://anki.test.example","X-Anki-Client":"web"}
        with self.request("/api/login",dict(method="password",password="long-test-password"),headers) as response:
            self.cookie=response.headers["Set-Cookie"].split(";",1)[0]
            self.assertIn("Secure",response.headers["Set-Cookie"])
        with self.request("/api/overview",headers={"Host":"anki.test.example"}) as response:
            csrf=json.load(response)["csrf"]
        headers["X-Anki-Token"]=csrf
        with self.request("/api/decks",dict(name="Behind NPM"),headers) as response:
            self.assertEqual(response.status,200)
        headers["Origin"]="https://other.test.example"
        with self.assertRaises(HTTPError) as error:
            self.request("/api/decks",dict(name="Blocked"),headers)
        self.assertEqual(error.exception.code,403)
        error.exception.close()

    def test_login_csrf_and_logout(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/login",dict(method="password",password="long-test-password"))
        self.assertEqual(error.exception.code,403)
        error.exception.close()
        with self.request("/api/login",dict(method="password",password="long-test-password"),{"X-Anki-Client":"web"}) as response:
            self.cookie=response.headers["Set-Cookie"].split(";",1)[0]
        with self.request("/api/overview") as response:
            csrf=json.load(response)["csrf"]
        with self.assertRaises(HTTPError) as error:
            self.request("/api/decks",dict(name="Blocked"),{"X-Anki-Token":"invalid"})
        self.assertEqual(error.exception.code,403)
        error.exception.close()
        with self.request("/api/decks",dict(name="Private"),{"X-Anki-Token":csrf}) as response:
            self.assertEqual(response.status,200)
        with self.request("/api/logout",{}, {"X-Anki-Token":csrf}) as response:
            self.assertIn("Max-Age=0",response.headers["Set-Cookie"])
        with self.assertRaises(HTTPError) as error:
            self.request("/api/overview")
        self.assertEqual(error.exception.code,401)
        error.exception.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
