# Перенос PHP-части (панель + логи + конфиг) на другой компьютер

«PHP-часть» — это тестовый веб-сервер, который:
- отдаёт приставкам `settings.json` (конфиг потоков);
- принимает логи от приставок (`log.php`);
- показывает панель управления (`index.php`, `logs.php`).

Приставки обращаются к нему по адресу, «зашитому» в портал (`ai_config/services.html`),
поэтому при переезде нужно сменить адрес в ОДНОМ месте и пересобрать портал.

---

## 1. Что нужно установить на новом ПК

| Компонент | Зачем |
|-----------|-------|
| **PHP 8+** | панель, `log.php`, генерация `settings.json` |
| **Python 3.8+** | скрипты `tools/*.py` (кнопки панели: reboot/update/check) |
| **paramiko** | `pip install -r tools/requirements.txt` (нужна версия `<4` для старых MAG) |

## 2. Что скопировать

Вся PHP-часть — папка **`ai_config/www/`** (docroot) плюс **`tools/`** (их вызывают кнопки панели)
и данные в репозитории:

```
ai_config/www/            # docroot веб-сервера (панель, log.php, devices.json, settings.json, mag_logs/)
ai_config/portal.template.html   # шаблон портала (для сборки)
ai_config/services.html   # собранный портал (можно пересобрать на месте)
tools/                    # deploy_services.py, mag_ctl.py, check_playback.py, collect_*.py, build_portal.py, requirements.txt
devices.txt               # список устройств для py-скриптов
```

Проще всего скопировать весь репозиторий целиком.

## 3. Настроить адрес сервера (один файл)

`ai_config/www/config.php`:

```php
define('SERVER_HOST', getenv('MAG_SERVER_HOST') ?: 'НОВЫЙ-IP');   // <— сюда
define('SERVER_PORT', (int) (getenv('MAG_SERVER_PORT') ?: 8088));
```

Либо без правки файла — через переменные окружения при запуске:

```powershell
$env:MAG_SERVER_HOST = 'НОВЫЙ-IP'
$env:MAG_SERVER_PORT = '8088'
```

## 4. Пересобрать портал для приставок

```powershell
python tools/build_portal.py
```

Скрипт подставит новый адрес в `ai_config/services.html` и `ai_config/www/services.html`.
После этого портал нужно **разложить на приставки**:

```powershell
python tools/deploy_services.py --hosts <ip1,ip2,...>
```

## 5. Запустить веб-сервер

```powershell
# слушать на всех интерфейсах, docroot — ai_config/www
php -S 0.0.0.0:8088 -t ai_config/www
```

- Разрешить входящий TCP-порт 8088 в брандмауэре.
- Приставки должны доставать новый ПК по сети (тот же/маршрутизируемый сегмент).

Проверка с другой машины:

```powershell
python tools/collect_playback.py --hosts <ip-приставки>   # или
python tools/check_playback.py  --hosts <ip-приставки>
```

## 6. Проверка, что всё переехало

1. Панель: `http://НОВЫЙ-IP:8088/index.php` (Basic Auth браузера).
2. Логи приставок пишутся в `ai_config/www/mag_logs/<MAC через дефис>.log`.
3. В панели виден статус из `HEARTBEAT` и кнопки работают (они вызывают `tools/*.py`).
4. Приставки получают конфиг: в логах сервера (`php -S` печатает запросы) видны
   `GET /settings.json` и `GET /log.php` от IP приставок.

---

## Замечания

- Папка `ai_config/www/mag_logs/` и `data/*.db` — рабочие данные (в git не хранятся),
  переносить их не обязательно (создадутся заново).
- `servers` в `config.php` влияет и на панель (отображается вверху: «сервер: …»).
- Если адрес сервера часто меняется — заведите на сети DNS-имя (например, `mag-server`)
  и укажите его в `config.php`; тогда пересборка портала не нужна.
