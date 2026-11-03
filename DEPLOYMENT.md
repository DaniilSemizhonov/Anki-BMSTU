# Развёртывание Anki BMSTU

Стек состоит из двух контейнеров:

- `backend`: Python и ядро Anki, HTTP API, авторизация и медиа коллекции.
- `web`: статические HTML/CSS/JS и Nginx для передачи `/api/` и `/media/` в backend.

TLS и внешний домен настраиваются в вашем Nginx Proxy Manager.
Backend не публикует порт на хост; web не получает доступ к данным коллекции.

## Первый запуск

На Linux-сервере нужны Docker Engine и Docker Compose v2 или новее.
Предусмотрены Linux amd64/arm64: библиотека Anki устанавливается из готового wheel.

```sh
git clone https://github.com/DaniilSemizhonov/Anki-BMSTU.git
cd Anki-BMSTU
cp .env.example .env
```

Отредактируйте `.env`:

```dotenv
ANKI_PUBLIC_URL=https://anki.your-domain.ru
ANKI_WEB_PASSWORD='your-own-long-private-password'
TZ=Europe/Moscow
WEB_BIND=127.0.0.1
WEB_PORT=8080
```

Пароль должен содержать минимум 12 символов. Это пароль приложения, не пароль
GitHub/Telegram. В `.env` заключайте пароли со знаком `$` в одинарные кавычки,
чтобы Compose не интерполировал их. Настоящий `.env` исключён из Git и build context.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Контейнер web запускается после успешной проверки здоровья backend. Состояние
и логи доступны через `docker compose ps` и `docker compose logs --tail=100`.
Откройте приложение через настроенный HTTPS-домен. При `ANKI_PUBLIC_URL=https://…`
вход использует Secure-cookie; вход по прямому HTTP-адресу сервера не предназначен
для работы с коллекцией.

## Подключение Nginx Proxy Manager

Выберите вариант, соответствующий размещению NPM.

### NPM находится в Docker

Укажите в `.env` имя существующей Docker-сети, к которой подключён NPM:

```dotenv
NPM_NETWORK=your-existing-proxy-network
```

Запустите стек с дополнительным файлом:

```sh
docker compose -f compose.yaml -f compose.npm.yaml config --quiet
docker compose -f compose.yaml -f compose.npm.yaml up -d --build
```

В NPM направьте домен на **`http://anki-bmstu-web:8080`**.
Это alias web-контейнера в общей сети. Внешняя сеть не создаётся Compose:
указанная сеть должна уже существовать. Backend к сети NPM не подключается.
При последующих `up`, `down` и обновлениях используйте те же два файла.

### NPM работает на хосте или другом сервере

Для NPM на том же хосте используйте `http://127.0.0.1:8080` с базовым Compose-файлом.
Если NPM находится на другом сервере, задайте `WEB_BIND` равным нужному LAN-адресу
сервера приложения и направьте NPM на этот адрес и `WEB_PORT`.
`127.0.0.1` внутри другого контейнера указывает на сам этот контейнер.

### Требования к проксированию

Сохраните исходный `Host` домена. `ANKI_PUBLIC_URL` должен точно совпадать
с внешним HTTPS-origin, без пути. Страница, API и медиа доступны с одного домена,
поэтому отдельный CORS не требуется.

Внутренний web-Nginx допускает загрузки до 100 МБ и ожидание API до 300 секунд.
Для крупных `.apkg` задайте соответствующие ограничения и таймауты в NPM.
WebSocket для текущей версии не требуется.

## Коллекция и резервные копии

Коллекция хранится в именованном томе `anki_data`: `collection.anki2`,
`collection.media` и сопутствующие файлы. По умолчанию полное имя тома —
`anki-bmstu_anki_data`. Обычные пересборка и `docker compose down` сохраняют его.
`docker compose down -v` удаляет том и коллекцию.

Первую коллекцию можно загрузить через импорт `.apkg` в интерфейсе. Настольный
профиль на вашем компьютере автоматически в контейнер не переносится.

Для согласованной копии остановите backend, затем архивируйте весь том:

```sh
docker compose stop backend
docker compose run --rm --no-deps -T --entrypoint tar backend -C /data -czf - . > anki-backup.tar.gz
docker compose start backend
```

Для варианта с сетью NPM добавьте к этим командам `-f compose.yaml -f compose.npm.yaml`.
Архив храните отдельно от сервера. Также доступен экспорт `.apkg` через интерфейс.
Одну коллекцию открывает только один backend; несколько реплик не поддерживаются.

## Копии, скачанные до очистки истории

История `main` переинициализирована: старые клоны не обновятся через
`git pull --ff-only`. Для такой копии сохраните `.env`, остановите её стек
командой `docker compose down` (с вашими Compose-файлами), затем клонируйте
репозиторий в другую папку и перенесите `.env`. Не используйте `down -v`.
Имя Compose-проекта осталось `anki-bmstu`, поэтому существующий том коллекции
будет использован при новом запуске. Не запускайте две копии backend одновременно.

## Обновление

```sh
git pull --ff-only
docker compose up -d --build
docker compose ps
```

Если используете `compose.npm.yaml`, передавайте оба файла и при обновлении.
Сессии хранятся в памяти: после перезапуска backend потребуется повторный вход.

## Telegram (необязательно)

В окружении backend предусмотрены:

```dotenv
ANKI_TELEGRAM_BOT_TOKEN=your-bot-token
ANKI_TELEGRAM_USER_ID=your-numeric-user-id
```

Оба значения должны быть заполнены вместе. В настройках своего бота укажите тот
же HTTPS-адрес Mini App. Сервер проверяет подпись `initData`, возраст до пяти минут
и ID владельца. Парольный вход и Telegram открывают одну личную коллекцию.
Токен бота не передаётся web-контейнеру или браузеру.

Проверка с настоящим ботом ещё не выполнена. В Telegram Web браузер может
блокировать сторонние cookie, необходимые для входа внутри iframe.

## Проверки сборки

Конфигурации базового Compose и варианта с сетью NPM проверены официальным
Docker Compose CLI. API и авторизация проверены на временных коллекциях Anki.
В среде разработки нет Docker Engine: сборку образов, `nginx -t` внутри образа
и запуск стека нужно выполнить на сервере.

```sh
docker compose build
docker compose run --rm --no-deps web nginx -t
docker compose up -d
```
