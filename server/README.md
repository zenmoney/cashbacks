# Cashback service

Документация HTTP-сервиса для данных кешбэка. О правилах и добавлении категорий см. [README корня репозитория](../README.md).

Cashback-data HTTP service documentation. For data rules and contributing categories, see the [repository-root README](../README.md).

[Русский](#russian) | [English](#english)

<a id="russian"></a>
## Русский
### Переменные окружения

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `CHECKOUT_DIR` | — | обязательный непустой путь к выделенному удаляемому checkout; при запуске всё содержимое каталога уничтожается |
| `REPOSITORY_URL` | — | обязательный непустой URL или путь Git-репозитория; удалённый репозиторий должен быть доступен при каждом запуске |
| `GITHUB_TOKEN` | — | необязательный токен GitHub; из значения удаляются начальные и конечные пробелы, а пустое значение считается не заданным |
| `HOST` | `0.0.0.0` | адрес прослушивания; при отсутствующем или пустом значении используется значение по умолчанию |
| `PORT` | `8080` | порт от `0` до `65535`; при отсутствующем или пустом значении используется значение по умолчанию |
| `REMOTE` | `origin` | удалённый репозиторий Git для начальной загрузки и `POST /sync`; при отсутствующем или пустом значении используется значение по умолчанию |
| `REF` | `main` | одна проверяемая ветка или тег; при отсутствующем или пустом значении используется значение по умолчанию |
| `GIT_TIMEOUT_SECONDS` | `300` | положительный тайм-аут в секундах для каждой команды Git; при отсутствующем или пустом значении используется значение по умолчанию |

### Безопасность токена GitHub

Для сетевых Git-команд начальной загрузки и `POST /sync` учётные данные, сформированные из `GITHUB_TOKEN` для URL без учётных данных, начинающегося ровно с `https://github.com/`, передаются через встроенное правило Git, действующее только для этой команды: `url.https://x-access-token:<URL-кодированный-токен>@github.com/.insteadOf=https://github.com/`. Исходный URL без учётных данных остаётся аргументом `clone` и URL настроенного удалённого репозитория (`REMOTE`, по умолчанию `origin`), поэтому удалённый репозиторий сохраняется без учётных данных. Для других URL и при отсутствующем токене правило не добавляется. Сервис не устанавливает и не настраивает ни askpass, ни помощник учётных данных, но Git может использовать уже имеющуюся конфигурацию.

Сервис не удаляет и не очищает часть URL с данными пользователя, переданную непосредственно в `REPOSITORY_URL`; не используйте URL с учётными данными. Указанные выше маскирование и отсутствие сохранения относятся к учётным данным, сформированным из `GITHUB_TOKEN` для GitHub по HTTPS с URL без учётных данных. Исходный токен остаётся в окружении, предоставленном системой развёртывания, и в памяти сервиса на всё время его работы и наследуется Git-подпроцессами; его URL-кодированная форма также ненадолго попадает в аргументы Git-процесса. При отображении команд и ошибок исходная и URL-кодированная формы токена маскируются, в том числе в стандартном выводе и выводе ошибок.

### Docker

Соберите образ из корня репозитория:

```shell
docker build -f server/Dockerfile -t cashbacks-service .
```

Образ уже содержит `CHECKOUT_DIR=/checkout`. Монтируйте в `/checkout` только отдельный том для удаляемых данных checkout: сервис уничтожает всё его содержимое при каждом запуске. Не монтируйте туда исходный репозиторий, файлы оператора или другие долговечные данные. На каждом запуске передавайте доступный URL репозитория без учётных данных и, при необходимости, токен отдельными переменными окружения:

```shell
docker run --rm --publish 8080:8080 \
  --volume cashbacks-checkout:/checkout \
  -e REPOSITORY_URL=https://github.com/owner/repo.git \
  -e GITHUB_TOKEN \
  cashbacks-service
```

Среда развёртывания создаёт выделенный том и предоставляет фактическим UID/GID права на проход по каталогам, создание, запись и удаление внутри checkout, а также передаёт стандартные секреты Git/SSH и остальные переменные окружения. Менеджер секретов может передать секрет как `GITHUB_TOKEN` в окружение контейнера; при такой передаче используйте `REPOSITORY_URL` без учётных данных, поскольку сервис не очищает часть URL с данными пользователя. Удалённый репозиторий обязан быть доступен при каждом старте: существующий checkout не используется как автономный резервный источник. Образ запускается от имени непривилегированного пользователя с UID/GID `10001:10001`, не исправляет права или владельца тома и полагается на Docker при завершении процесса и Git-подпроцессов.

Секреты Git/SSH, включая `GITHUB_TOKEN`, используются только для сетевого доступа Git при начальной загрузке и `POST /sync`; HTTP-маршруты остаются неаутентифицированными: заголовки авторизации не требуются и не интерпретируются.

### API и синхронизация данных

- `GET /banks`
- `GET /banks/{positive-id}/categories`
- `POST /banks/{positive-id}/categories/match` с JSON-массивом строк
- `POST /sync` без тела
- `GET /openapi.json`
- `GET /docs`
- `GET /redoc`

`GET /banks` возвращает JSON-массив верхнего уровня с положительными числовыми идентификаторами компаний всех активных банков текущего снимка. Если активных банков нет, сервис возвращает `[]`; банки в ожидании не включаются. Порядок элементов не определён, и клиентам нельзя на него полагаться.

FastAPI и Uvicorn отвечают за маршрутизацию, разбор HTTP и проверку объявленных path/body-параметров. Некорректный ID банка или тело запроса возвращает стандартный `422` с непустым массивом `detail`; неизвестный путь — `404`; неподдерживаемый метод — `405` с `Allow`; завершающий `/` перенаправляется на канонический маршрут. Отсутствующий банк возвращает `404 {"detail":"bank_not_found"}`, а ошибка синхронизации — `503 {"detail":"sync_failed"}`. OpenAPI и обе интерактивные страницы документации генерируются FastAPI. Все маршруты неаутентифицированы.

Для `POST /banks/{positive-id}/categories/match` сервис и строгая проверка источника используют общий нормализатор для каждого сохранённого скалярного или массивного alias и каждой строки запроса: `unicodedata.normalize("NFC", title)`; `re.sub(r"[^\w\s]", "", title, flags=re.UNICODE)`; `re.sub(r"[_\s]+", " ", title)`; `strip()`; `lower()`. Каждый сохранённый alias обязан давать непустой ключ; источник с псевдонимом только из удаляемых знаков отклоняется до построения снимка. Внутри одного банка совпадающие ключи, включая элементы одного массива и разных файлов, отклоняются при загрузке источника; у разных банков ключи независимы. Строка запроса может нормализоваться в пустую строку, но сохранённого пустого ключа нет, поэтому такая позиция возвращает `null`. Нормализация применяется только к ключам: исходные значения и форма `category`, объекты исходных правил, тела `GET` и объекты правил в ответах остаются без изменений. Она не выполняет `casefold()`, замену `ё` на `е`, транслитерацию, удаление диакритических знаков, нечёткое сопоставление или перестановку слов. Ответ — bare JSON-массив той же длины и в том же порядке, что запрос: для совпадения возвращается полный необработанный объект правила, для отсутствующего — `null`.


`POST /sync` — единственный явный способ обновить checkout данных; сервис не планирует синхронизацию, не выполняет повторных попыток и не запускает её в фоне. Периодичность синхронизации задаёт среда развёртывания. Синхронизация не обновляет исполняемый код: после изменений в `server/` или `scripts/cashbacks.py` пересоберите образ и перезапустите процесс в среде развёртывания.

Ответ `503 {"detail":"sync_failed"}` сообщает только о сбое синхронизации. Конкретная причина записывается в stderr с префиксом `cashbacks-service: sync failed:`; в Docker смотрите журнал контейнера: `docker logs <container>`.

### Проверка сервера

Из корня репозитория выполните:

```shell
python3 -m pip install -r server/requirements.txt
python3 -m unittest tests.test_cashbacks_service
```

<a id="english"></a>
## English

### Environment configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHECKOUT_DIR` | — | required non-empty path to a dedicated disposable checkout; startup destroys every entry beneath the directory |
| `REPOSITORY_URL` | — | required non-empty Git repository URL or path; the remote must be available on every start |
| `GITHUB_TOKEN` | — | optional GitHub token; the value is trimmed, and an empty value is the same as absent |
| `HOST` | `0.0.0.0` | listening address; an absent or empty value uses the default |
| `PORT` | `8080` | port from `0` through `65535`; an absent or empty value uses the default |
| `REMOTE` | `origin` | Git remote for bootstrap and `POST /sync`; an absent or empty value uses the default |
| `REF` | `main` | one validated branch or tag; an absent or empty value uses the default |
| `GIT_TIMEOUT_SECONDS` | `300` | positive timeout in seconds for every Git command; an absent or empty value uses the default |

### GitHub token security

For bootstrap and `POST /sync` Git network commands, credentials generated from `GITHUB_TOKEN` for a clean URL starting exactly with `https://github.com/` are passed through a native command-scoped URL rewrite: `url.https://x-access-token:<URL-encoded-token>@github.com/.insteadOf=https://github.com/`. The clean URL remains the `clone` argument and the URL of the configured remote (`REMOTE`, default `origin`), so that remote is stored without credentials. No rewrite is added for other URLs or an absent token. The service itself does not install or configure askpass or a credential helper, but Git may use ambient configuration.

The service does not remove or sanitize userinfo supplied directly in `REPOSITORY_URL`; do not use a credential-bearing URL. The redaction and non-persistence guarantees above apply to credentials generated from `GITHUB_TOKEN` for clean GitHub HTTPS transport. The raw token remains in the backend-provided service environment and process memory for the service lifetime and is inherited by Git subprocesses; its URL-encoded form also appears briefly in Git-process argv. Raw and URL-encoded token forms are redacted from rendered commands and stdout/stderr-based errors.

### Docker

Build the image from the repository root:

```shell
docker build -f server/Dockerfile -t cashbacks-service .
```

The image includes `CHECKOUT_DIR=/checkout`. Mount only a dedicated disposable-checkout volume at `/checkout`: the service destroys every entry in it on every start. Never mount the source repository, operator-owned files, or other durable data there. Supply an available clean repository URL on every start and, when needed, inject the token separately:

```shell
docker run --rm --publish 8080:8080 \
  --volume cashbacks-checkout:/checkout \
  -e REPOSITORY_URL=https://github.com/owner/repo.git \
  -e GITHUB_TOKEN \
  cashbacks-service
```

The backend deployment creates the dedicated volume, provisions traversal, creation, write, and removal access inside the checkout for the effective UID/GID, and injects standard Git/SSH secrets together with the other environment variables. A secret manager can inject the secret as `GITHUB_TOKEN` into the container environment; use a clean `REPOSITORY_URL` for that token transport because the service does not sanitize its userinfo. The remote must be available on every start; an existing checkout is not an offline fallback. The image runs as non-root UID/GID `10001:10001`, does not repair volume ownership or permissions, and relies on Docker to terminate the service and Git subprocesses.

Git/SSH secrets, including `GITHUB_TOKEN`, serve only Git transport for bootstrap and `POST /sync`; HTTP routes remain unauthenticated: authorization headers are neither required nor interpreted.

### API and data synchronization

- `GET /banks`
- `GET /banks/{positive-id}/categories`
- `POST /banks/{positive-id}/categories/match` with a JSON string array
- `POST /sync` with no body
- `GET /openapi.json`
- `GET /docs`
- `GET /redoc`

`GET /banks` returns a bare JSON array containing the positive numeric company IDs of all active banks in the current snapshot. It returns `[]` when there are no active banks and excludes pending banks. Element order is unspecified and clients must not rely on it.

FastAPI and Uvicorn own routing, HTTP parsing, and declared path/body validation. An invalid bank ID or request body returns the standard `422` response with a non-empty `detail` array; an unmatched path returns `404`; an unsupported method returns `405` with `Allow`; and a trailing slash redirects to the canonical route. An absent bank returns `404 {"detail":"bank_not_found"}`, while a synchronization failure returns `503 {"detail":"sync_failed"}`. FastAPI generates OpenAPI and both interactive documentation pages. Every route is unauthenticated.

For `POST /banks/{positive-id}/categories/match`, the service and strict source validation use one shared normalizer for each stored scalar or array alias and each request string: `unicodedata.normalize("NFC", title)`; `re.sub(r"[^\w\s]", "", title, flags=re.UNICODE)`; `re.sub(r"[_\s]+", " ", title)`; `strip()`; `lower()`. Every stored alias must produce a non-empty key; a source alias made only of removed punctuation is rejected before snapshot construction. Within one bank, matching keys—including entries in one array and separate files—are rejected while loading the source; different banks have independent keys. A request string may normalize to the empty string, but no empty stored key exists, so that position returns `null`. Normalization applies only to keys: source `category` values and shape, source rule objects, `GET` bodies, and returned rule objects remain unchanged. It does not use `casefold()`, map `ё` to `е`, transliterate, remove diacritics, fuzzy-match, or reorder words. The response is a bare JSON array with the request's exact length and order: a complete raw rule object for each match and `null` for each miss.


`POST /sync` is the only explicit data-checkout refresh; the service does not schedule, retry, or run synchronization in the background. The backend deployment chooses the sync cadence. Sync does not update executable code: after changing `server/` or `scripts/cashbacks.py`, rebuild the image and restart the process in the deployment.

The `503 {"detail":"sync_failed"}` response only indicates that synchronization failed. The specific cause is written to stderr with the `cashbacks-service: sync failed:` prefix; in Docker, inspect the container logs with `docker logs <container>`.

### Server verification

From the repository root, run:

```shell
python3 -m pip install -r server/requirements.txt
python3 -m unittest tests.test_cashbacks_service
```
