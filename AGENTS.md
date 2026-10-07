# AGENTS.md — контекст проекта для ИИ-агентов

> Файл-«памятка» для работы с репозиторием. Читать в первую очередь.

## Что за проект

Портал и сервисы для IPTV-приставок **MAG (Infomir)**. HTML-портал на
JavaScript-API приставки (`gSTB` + модульные `stbPlayer`/`stbEvent`/…), конфиг
потоков, PHP-приёмник логов и инструменты деплоя на устройства по SSH.

## Структура

| Путь | Назначение |
|------|-----------|
| `ai_config/` | **Актуальный портал**: `portal.template.html`, `services.html`, `log.php` |
| `ai_config/www/` | Docroot веб-сервера: **панель управления** (`index.php`, `logs.php`, `lib.php`, `devices.json`), **единый конфиг** `settings.json`, `log.php`, `mag_logs/` |
| `tools/` | `deploy_services.py` (+ `requirements.txt`, `README.md`) — деплой на приставки |
| `devices.txt` | Список устройств для деплоя (по строке на хост) |
| `network_devices.md` | Пример инвентаря: IP / MAC / VLAN |
| `services.html` (корень) | Простейший пример сервисной страницы (один поток) |
| `API/stbapi.github.io/` | Внешняя документация API (в репозиторий не входит) |
| `API/mag.js.txt`, `API/mag_triks.txt` | Выдержки/хелперы из порталов MAG |

## Ключевые факты (проверено на устройстве)

- **Путь портала на приставке: `/home/web/`** (`services.html`, `settings.json`).
  Это НЕ `/home/default`. Рядом: `services.html~`, `index.html`, `info.json`,
  `rules.js`, `services.min.js`, `public/`, `system/`.
- Приставка: **MAG250**, Linux 2.6.32, прошивка 7105 (2017), arch `sh4`.
- SSH-сервер: **OpenSSH 5.1**. Host keys только `ssh-rsa`/`ssh-dss` →
  нужен **`paramiko >= 3.4, < 4`** (в 4.x поддержка `ssh-rsa` удалена).
- **SFTP-подсистемы нет** (`open_sftp()` → `Channel closed`). Файлы заливаются
  через exec-канал: `cat > файл` (`put_bytes()` в деплой-скрипте).
- Учётка SSH — `root`; пароль задаётся через env `MAG_PASSWORD` (или `--password`).
- MAC в `settings.json` — **верхний регистр через двоеточие** (`00:1A:79:...`);
  именно так его отдаёт `macToString(gSTB.GetDeviceMacAddress())`.
- ⚠️ На прошивке 7105 `gSTB.GetDeviceMacAddress()` возвращает **строку**
  `"00:1a:79:00:00:01"` (не массив байт) → `macToString` обязан принимать оба варианта.
- ⚠️ Браузер приставки — **QtWebKit 4.6, уровень ES3**: `default` как имя свойства
  (`default:`, `CFG.default`) ломает синтаксис и **весь скрипт не выполняется**
  (при этом `window.onerror` для синтаксических ошибок молчит). Писать `"default":` и
  `CFG["default"]`. Валидатор ES3-совместимости — `cscript //E:JScript //NOLOGO <файл.js>`.
- Получение IP: `gSTB.RDir('IPAddress')`. Метода **`gSTB.GetIpAddress()` НЕ
  существует** ни в одной версии API.

## Текущий статус

- На приставку заливается компактный `ai_config/services.html` (см. раздел
  «Воспроизведение на MAG250»); конфиг и логи берёт с ПК (`http://192.168.1.100:8088`).
  Подтверждено: `HEARTBEAT playing=yes buff=100 pos` растёт (RTMP).
- MAC устройств прописаны в `devices.json` (потоки `dev_<MAC>`).
- Данные о прошивках собираются в `network_devices.md` (`tools/collect_devices.py`).

## Воспроизведение на MAG250 (fw 7105) — важные выводы

- ⚠️ **HLS на этой прошивке не декодируется**: `gSTB.Play('http://.../index.m3u8')`
  даёт `IsPlaying=true`, но `GetBufferLoad()=0`, `GetPosTime()=0`, `GetVideoInfo()=0x0`
  — даже эталонным скриптом без обвязки. **Использовать RTMP** (проверено
  `rtmp://192.168.1.200/play/example-tv` → `buff=100`, `pos` растёт, `1280x720@25`).
- ⚠️ **`gSTB.IsPlaying()` ненадёжен** — возвращает `true`, даже когда данные не идут
  (`buff=0`). Реальный критерий — `GetBufferLoad()` + `GetPosTime()`.
- ⚠️ **Тяжёлая обвязка ломает воспроизведение**: полный портал (шимы JSON/localStorage,
  лог-модуль, множество таймеров) RTMP не удерживает (`buff` кратко 40–100 → 0).
  При этом точечно `stbEvent`, localStorage-шим и динамический `gSTB[...]` — невиновны.
- ✅ **Рабочий паттерн** (теперь в `ai_config/services.html`):
  `stb = gSTB; stb.InitPlayer(); stb.SetStereoMode(0);` → затем через паузу (~1.2 с)
  `stb.SetVolume(v); stb.Play(url);` — **прямыми статическими вызовами `stb.*`**,
  из таймера, созданного в `onload` (не из колбэка XHR). Держать минимум кода/таймеров.
- `gSTB.GetStatistics()` во время воспроизведения не вызывать (сбрасывает поток).
- Портал остаётся на устройстве (`file:///home/web/services.html`), конфиг/логи — с ПК.

## Частые команды

```powershell
# Проверка SSH-доступности
python tools/deploy_services.py --check

# Деплой на одну приставку (обкатка)
python tools/deploy_services.py --hosts 192.168.1.10

# Показать план без изменений
python tools/deploy_services.py --dry-run

# Проверка, воспроизводится ли поток (косвенно, по SSH)
python tools/check_playback.py --hosts 192.168.1.10

# Сбор версий прошивок/данных со всех устройств (markdown-таблица)
python tools/collect_devices.py

# Сбор текущих потоков на устройствах + запись в SQLite data/mag.db
python tools/collect_playback.py

# Проверка ссылок в /home/web/services.html на всех устройствах (markdown-отчёт)
python tools/check_portal_links.py --out portal_links_report.md

# Валидация PHP
php -l ai_config/log.php

# Валидация JS внутри services.html (Node нет; используем JScript-движок)
#   извлечь блок <script>...</script> в файл и:
cscript //E:JScript //NOLOGO <файл.js>
#   ВНИМАНИЕ: ES3-движок ругается на `default:`/`.default` — это ложное
#   срабатывание, в ES5+ (WebKit приставки) это допустимо.
```

## Тестовый сервер на ПК (отладка логов и конфига)

Docroot — `ai_config/www` (`log.php`, `settings.json`, `services.html`, `test.html`).

```powershell
php -S 192.168.1.100:8088 -t ai_config/www
```

Приставка, загруженная по `file://`, может делать XHR на `http://` — поэтому портал
остаётся на устройстве, а конфиг и логи берёт с ПК:

- `CONFIG_URL   = http://192.168.1.100:8088/settings.json`
- `LOG_ENDPOINT = http://192.168.1.100:8088/log.php`

Логи принимаются в `ai_config/www/mag_logs/<MAC через дефис>.log`.
`test.html` — мини-проверка: приставка шлёт в лог `JS_RUNS ... gSTB=... XHR=...`.

### Панель управления (`ai_config/www/index.php`)

- Таблица устройств (из `devices.json`): ссылка + тип плеера, комментарий (расположение),
  статус из `HEARTBEAT`, кнопки: **Сохранить**, **Перезагрузить** (`mag_ctl.py --reboot`),
  **Обновить services** (`deploy_services.py`), **Проверить** (`check_playback.py`),
  **Ping** (нативный), **Mute/Вкл. звук** (флаг в `settings.json`), **Логи**.
- `logs.php` — список логов и просмотр хвоста с автообновлением.
- `devices.json` — источник истины панели; из него генерируется `settings.json`
  (потоки `dev_<MAC>` + `mac`-мэппинг + карта `mute`).
- `log.php` сам ротирует логи (2 МБ на файл, 3 архива `.bak`), без внешнего cron.
- Авторизация — Basic Auth на веб-сервере; в PHP только CSRF.

## Соглашения по правкам

- `ai_config/services.html` — легаси-портал под **старые MAG (Gecko)**:
  совместимость важна → только `var`, без `const`/`let`, без стрелочных функций
  и ES5+ возможностей, где это критично.
- Кодировка файлов — **UTF-8** (в HTML указан `charset=utf-8`).
  Переводы строк — **LF** (см. `.gitattributes`).
- ⚠️ **Правки `services.html` делать ПОСЛЕДОВАТЕЛЬНО, по одной.** Параллельные
  правки одного файла приводили к тому, что блоки вставлялись не туда —
  после каждого изменения перечитывать участок.
- После правок: перечитать изменённый участок, проверить `get_errors`, при
  возможности — валидировать JS/PHP.
- Не коммитить сторонние клоны (`API/stbapi.github.io/`) и `__pycache__/` — они в `.gitignore`.

## Документация API

- Официальная справка: `https://stbapi.github.io/v343/gSTB.html` (можно опираться
  на v343). Локальная копия версий v342–v348 в этот репозиторий не входит.
- Модульные пространства имён: `stbPlayer`, `stbPlayerManager`, `stbEvent`,
  `stbStorage`, `stbWindowMgr`, `stbWebWindow`, `stbUpdate`, `timeShift`,
  `pvrManager`, `stbEpgManager`, `stbDvb*`, `stbSmb`, `stbNfs`, `stbBrowser`,
  `stbAudio*`, `stbBluetooth*`, `stbDisplay*`, `stbSurface*`, `stbDownloadManager`.
- Модель событий: `window.stbEvent = { onEvent: function(event, info){…}, event: 0 }`.
- Плеер (легаси): `gSTB.InitPlayer()` → `SetStereoMode(0)` → `SetVolume(N)` →
  `gSTB.Play('<hls|udp|rtsp>')`; `Stop/Pause/Continue`.

## Что считать источником истины

- Потоки и маппинг MAC/IP — `ai_config/www/devices.json` (источник) → из него генерируется
  единый `ai_config/www/settings.json` (+ копия на устройстве). Отдельного `ai_config/settings.json` нет.
- Список устройств для деплоя — `devices.txt`.
- Инвентарь/сеть — `network_devices.md`.
