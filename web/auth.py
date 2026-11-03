"""Personal access control for browser and Telegram clients. AGPL-3.0-or-later."""
import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from http.cookies import SimpleCookie, CookieError
from urllib.parse import parse_qsl, urlsplit


class AuthenticationError(Exception):
    pass


def validate_telegram(init_data, bot_token, user_id, now=None):
    """Verify Telegram initData before reading identity. Never trust initDataUnsafe."""
    try:
        if not isinstance(init_data, str) or len(init_data) > 16384:
            raise ValueError()
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(pairs) != len(data):
            raise ValueError()
        signature = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        key = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        age = (time.time() if now is None else now) - int(data["auth_date"])
        if age < -30 or age > 300:
            raise ValueError()
        user = json.loads(data["user"])
        if type(user["id"]) is not int or user["id"] != user_id:
            raise ValueError()
        return user["id"]
    except (ValueError, KeyError, TypeError):
        raise AuthenticationError("Вход не подтверждён. Откройте приложение заново из своего Telegram.") from None


class Access:
    """One owner's collection. Sessions are memory-only and expire after 24 hours."""
    def __init__(self, public_url="", password="", bot_token="", telegram_user_id="", clock=time.time):
        self.clock = clock
        self.public_url = public_url.rstrip("/")
        if self.public_url:
            parsed = urlsplit(self.public_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("ANKI_PUBLIC_URL должен быть HTTPS-адресом без пути и параметров.")
        if password and len(password) < 12:
            raise ValueError("Пароль ANKI_WEB_PASSWORD должен содержать минимум 12 символов.")
        if bool(bot_token) != bool(telegram_user_id):
            raise ValueError("Для Telegram нужны ANKI_TELEGRAM_BOT_TOKEN и ANKI_TELEGRAM_USER_ID.")
        self.bot_token = bot_token
        self.telegram_user_id = int(telegram_user_id) if telegram_user_id else None
        if self.telegram_user_id is not None and self.telegram_user_id <= 0:
            raise ValueError("Некорректный ANKI_TELEGRAM_USER_ID.")
        if self.public_url and not (password or bot_token):
            raise ValueError("Для внешнего адреса настройте пароль или Telegram-вход.")
        self.salt = secrets.token_bytes(16)
        self.password_hash = self.hash_password(password) if password else None
        self.required = bool(password or bot_token)
        self.sessions = {}
        self.failures = deque(maxlen=10)

    def hash_password(self, password):
        return hashlib.pbkdf2_hmac("sha256", password.encode(), self.salt, 200000)

    def session(self, cookie):
        try:
            cookies = SimpleCookie()
            cookies.load(cookie or "")
            token = cookies["anki_session"].value
        except (KeyError, ValueError, CookieError):
            return None
        session = self.sessions.get(token)
        if session and session["expires"] > self.clock():
            return session
        self.sessions.pop(token, None)
        return None

    def login(self, data):
        now = self.clock()
        if len(self.failures) == 10 and now - self.failures[0] < 60:
            raise AuthenticationError("Слишком много попыток. Подождите минуту.")
        try:
            if data.get("method") == "telegram" and self.bot_token:
                validate_telegram(data.get("init_data"), self.bot_token, self.telegram_user_id, now=now)
            elif data.get("method") == "password" and self.password_hash:
                password = data.get("password")
                if not isinstance(password, str) or len(password) > 1024 or not hmac.compare_digest(self.hash_password(password), self.password_hash):
                    raise AuthenticationError("Неверный пароль.")
            else:
                raise AuthenticationError("Этот способ входа не настроен.")
        except AuthenticationError:
            self.failures.append(now)
            raise
        self.sessions = {k:v for k,v in self.sessions.items() if v["expires"] > now}
        if len(self.sessions) >= 50:
            self.sessions.pop(next(iter(self.sessions)))
        token = secrets.token_urlsafe(32)
        self.sessions[token] = dict(csrf=secrets.token_urlsafe(32), expires=now + 86400)
        return self.cookie(token)

    def cookie(self, token, logout=False):
        secure = "; Secure" if self.public_url else ""
        same_site = "None" if self.public_url and self.bot_token else "Lax"
        return f"anki_session={token}; Path=/; HttpOnly; SameSite={same_site}; Max-Age={0 if logout else 86400}{secure}"

    def logout(self, cookie):
        try:
            parsed = SimpleCookie(cookie or "")
            self.sessions.pop(parsed["anki_session"].value, None)
        except (ValueError, KeyError, CookieError):
            pass
        return self.cookie("", logout=True)
