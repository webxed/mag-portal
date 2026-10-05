# tools/deploy_services.py

Автообновление файлов портала (`services.html`, `settings.json`) на MAG-приставках
по SSH (учётная запись `root`).

## Требования

- Python 3.8+
- `paramiko`

```powershell
pip install -r tools/requirements.txt
```

## Быстрый старт

```powershell
# 1. Проверить SSH-доступность всех устройств из devices.txt
python tools/deploy_services.py --check

# 2. Обновить ОДНО устройство (проверка на одном хосте)
python tools/deploy_services.py --hosts 192.168.1.10

# 3. Обновить все устройства
python tools/deploy_services.py
```

## Что заливается по умолчанию

| Локально | Удалённо |
|----------|----------|
| `ai_config/services.html` | `/home/web/services.html` |
| `ai_config/settings.json` | `/home/web/settings.json` |

Перед перезаписью на устройстве создаётся `<файл>.bak`. После заливки выполняется `sync`.

## Список устройств

Источник — `devices.txt` (один хост в строке, `#` — комментарий) или аргумент `--hosts`:

```powershell
python tools/deploy_services.py --hosts 192.168.1.10,192.168.1.11
```

## Полезные ключи

| Ключ | Назначение |
|------|-----------|
| `--hosts a,b,c` | хосты через запятую (приоритетнее файла) |
| `--devices FILE` | свой файл со списком хостов |
| `--upload LOCAL:REMOTE` | своя пара для заливки (можно несколько раз) |
| `--remote-dir DIR` | удалённая папка для заливок по умолчанию (`/home/web`) |
| `--cmd "..."` | команда после заливки (можно несколько; по умолчанию `sync`) |
| `--no-backup` | не делать `.bak` |
| `--jobs N` | параллельных потоков (по умолчанию 4) |
| `--timeout S` | таймаут подключения/команд, сек (по умолчанию 10) |
| `--user` / `--password` / `--port` | реквизиты SSH |
| `--check` | только проверить доступность |
| `--dry-run` | показать план, ничего не делать |

## Безопасность

Пароль SSH по умолчанию **не задан**. Указывайте его аргументом `--password`
или переменной окружения `MAG_PASSWORD` (скрипт откажется работать без пароля):

```powershell
$env:MAG_PASSWORD = '...'
python tools/deploy_services.py
```

## Важно

- Путь портала — **`/home/web`** (проверено на тестовой MAG250: там лежат
  `services.html` и `settings.json`). Если на других прошивках раскладка иная,
  укажите путь явно:
  ```powershell
  python tools/deploy_services.py --upload ai_config/services.html:/путь/services.html
  ```
- **paramiko должен быть < 4**: старые MAG работают по **OpenSSH 5.1** и принимают
  только host key `ssh-rsa`/`ssh-dss`, а paramiko 4.x удалил `ssh-rsa`. Скрипт сам
  включает legacy-алгоритмы, но только если их поддерживает установленная версия:
  ```powershell
  pip install "paramiko>=3.4,<4"
  ```
- Устройства должны быть доступны с этого ПК по сети и иметь включённый SSH.
- Скрипт использует `AutoAddPolicy` (принимает неизвестные host key).

## Проверка воспроизведения видео

```powershell
python tools/check_playback.py --hosts 192.168.1.10
python tools/check_playback.py --hosts 192.168.1.10 --window 8 --min-kbps 300
```

`gSTB.IsPlaying()` снаружи недоступен (это API внутри портала), поэтому скрипт
смотрит косвенные признаки на уровне ОС:

- процесс портала (`stbapp`) и загруженный URL (`/proc/<pid>/cmdline`);
- ESTABLISHED TCP-соединения, кроме собственного SSH (`/proc/net/tcp`);
- членство в multicast-группах, кроме служебных (`/proc/net/igmp`);
- битрейт на `eth0` (замер `RX` за `--window` секунд);
- активность декодеров (`STVID`/`STAUD` в `ps`).

Вердикт **PLAY**, если есть медиа-multicast-группа **или** битрейт выше
`--min-kbps` (по умолчанию 200 кбит/с); иначе — **IDLE**.

## Сбор данных об устройствах

```powershell
python tools/collect_devices.py                 # все из devices.txt
python tools/collect_devices.py --hosts 192.168.1.10
python tools/collect_devices.py --json          # + JSON
```

По SSH собирает: модель, вендор, версию железа, версию прошивки (`ImageVersion`),
описание (`ImageDescription`), serial, MAC, ядро. Печатает markdown-таблицу —
удобно вставлять в `network_devices.md`. Источники: `/home/default/rdir.cgi <поле>`
и `fw_printenv Image_Version/Image_Desc/Image_Date`.

## Сбор текущих потоков + база

```powershell
python tools/collect_playback.py                    # все из devices.txt
python tools/collect_playback.py --hosts 192.168.1.10
```

Определяет, какой поток сейчас играется на каждой приставке (по SSH, без доступа
к `gSTB` внутри портала):

- multicast-группа из `/proc/net/igmp` → udp-поток (сопоставляется со `settings.json`);
- ESTABLISHED TCP к стрим-серверу → http/hls/rtmp-поток (по `host:port`);
- битрейт на `eth0` → подтверждение активности.

Результат печатается таблицей и **пишется в SQLite `data/mag.db`**
(таблицы `devices` и `playback`, накапливается история). `data/*.db` в `.gitignore`.

## Проверка ссылок в файле портала

```powershell
python tools/check_portal_links.py --out portal_links_report.md
```

Для каждого устройства читает `/home/web/services.html` и показывает: наличие файла,
размер, md5 (сравнение с локальным `ai_config/services.html` — эталон) и **все ссылки**
(URL потоков) внутри. Формирует markdown-отчёт (`--out`).

