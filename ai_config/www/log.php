<?php
/**
 * log.php — приёмник логов от MAG-приставок.
 *
 * Формат запроса от клиента:
 *   GET /log.php?token=XXX&device=AA:BB:CC:DD:EE:FF&count=15&data=...
 *
 * data — это URL-encoded строка, содержащая несколько лог-строк,
 *        разделённых символом "\n".
 *
 * Ответ: text/plain — "OK, received N lines" либо текст ошибки.
 */

ini_set('error_reporting', E_ALL);
ini_set('display_errors', true);

// ============================================================================
// НАСТРОЙКИ
// ============================================================================

// Секретный токен. Должен совпадать с TOKEN в скрипте портала.
// Если не нужен — оставьте пустым, тогда проверка отключается.
$LOG_TOKEN = 'change-me';

// Куда складывать логи. По умолчанию — папка mag_logs рядом со скриптом.
$LOG_DIR = __DIR__ . '/mag_logs';

// Максимальная длина одной лог-строки (обрезаем слишком длинные).
$MAX_LINE_LEN = 4000;

// Ротация логов — целиком внутри log.php (без внешнего аудита/cron):
// файл одного устройства не превышает $MAX_FILE_SIZE, хранится не более
// $MAX_BACKUPS архивов .bak, т.е. суммарно <= MAX_FILE_SIZE*(1+MAX_BACKUPS).
$MAX_FILE_SIZE = 2 * 1024 * 1024;   // 2 МБ на активный файл
$MAX_BACKUPS   = 3;                 // хранить 3 архива .bak на устройство

// Максимальный размер GET-запроса (защита от мусорных запросов).
// При LOG_BATCH_SIZE=15 и средней строке 200 символов — порядка 4–6 КБ.
$MAX_DATA_LEN = 64 * 1024;

// Часовой пояс для метки в файле ротации (не для строк — там своё время).
date_default_timezone_set('Europe/Moscow');

// ============================================================================
// ФУНКЦИИ
// ============================================================================

function fail($code, $message) {
    header('HTTP/1.1 ' . $code);
    header('Content-Type: text/plain; charset=utf-8');
    echo $message . "\n";
    exit;
}

function ok($message) {
    header('Content-Type: text/plain; charset=utf-8');
    echo $message . "\n";
    exit;
}

/**
 * Проверяет токен. Возвращает true, если доступ разрешён.
 */
function checkToken($expected) {
    if ($expected === '') {
        return true; // проверка отключена
    }
    if (!isset($_GET['token'])) {
        return false;
    }
    // Используем hash_equals для защиты от timing-атак (PHP 5.6+).
    // На старых версиях — обычное сравнение.
    if (function_exists('hash_equals')) {
        return hash_equals($expected, $_GET['token']);
    }
    return $_GET['token'] === $expected;
}

/**
 * Санитайзит идентификатор устройства — оставляет только безопасные символы.
 */
function sanitizeDevice($device) {
    $device = preg_replace('/[^A-Za-z0-9:\.\-_]/', '_', $device);
    if ($device === '' || $device === null) {
        $device = 'unknown';
    }
    if (strlen($device) > 100) {
        $device = substr($device, 0, 100);
    }
    return $device;
}

/**
 * Ротация: если файл превысил лимит, переименовываем с датой.
 */
function rotateIfNeeded($file, $maxSize, $maxBackups = 3) {
    if (!file_exists($file)) return;
    if (filesize($file) <= $maxSize) return;

    $bak = $file . '.' . date('Ymd-His') . '.bak';
    @rename($file, $bak);

    // Удаляем самые старые .bak, оставляя не больше $maxBackups на устройство
    $dir  = dirname($file);
    $base = basename($file) . '.';
    $baks = glob($dir . '/' . $base . '*.bak');
    if ($baks && count($baks) > $maxBackups) {
        sort($baks); // имена содержат дату — сортировка по времени
        $toDelete = array_slice($baks, 0, count($baks) - $maxBackups);
        foreach ($toDelete as $old) {
            @unlink($old);
        }
    }
}

/**
 * Пишет строки в файл устройства.
 * Возвращает количество записанных строк или false при ошибке.
 */
function writeLines($file, $lines, $maxLineLen) {
    $fp = @fopen($file, 'a');
    if (!$fp) {
        return false;
    }

    // Блокировка — на случай параллельных запросов от нескольких приставок
    @flock($fp, LOCK_EX);

    $written = 0;
    foreach ($lines as $line) {
        $line = trim($line);
        if ($line === '') continue;

        if (strlen($line) > $maxLineLen) {
            $line = substr($line, 0, $maxLineLen) . ' ...[truncated]';
        }

        if (@fwrite($fp, $line . "\n") !== false) {
            $written++;
        }
    }

    @flock($fp, LOCK_UN);
    @fclose($fp);
    return $written;
}

// ============================================================================
// ОБРАБОТКА ЗАПРОСА
// ============================================================================

// 1) Только GET
if (!isset($_SERVER['REQUEST_METHOD']) || $_SERVER['REQUEST_METHOD'] !== 'GET') {
    fail('405 Method Not Allowed', 'ERROR: only GET is supported');
}

// 2) Проверка токена
if (!checkToken($LOG_TOKEN)) {
    fail('403 Forbidden', 'ERROR: invalid token');
}

// 3) Извлекаем параметры
$device = isset($_GET['device']) ? str_replace(':', '-', $_GET['device']) : 'unknown';
$count  = isset($_GET['count'])  ? intval($_GET['count']) : 0;
$data   = isset($_GET['data'])   ? $_GET['data'] : '';

// Защита от мусорных запросов
if (strlen($data) > $MAX_DATA_LEN) {
    fail('413 Payload Too Large', 'ERROR: data too large');
}

$device = sanitizeDevice($device);

// 4) Пустой запрос — используем как ping
if ($data === '') {
    ok('OK (empty ping from ' . $device . ')');
}

// 5) Готовим папку для логов
if (!is_dir($LOG_DIR)) {
    if (!@mkdir($LOG_DIR, 0775, true)) {
        fail('500 Internal Server Error', 'ERROR: cannot create log dir');
    }
}

if (!is_writable($LOG_DIR)) {
    fail('500 Internal Server Error', 'ERROR: log dir is not writable');
}

// 6) Формируем имя файла
$file = $LOG_DIR . '/' . $device . '.log';

// 7) Ротация, если файл вырос (внутри log.php, без cron)
rotateIfNeeded($file, $MAX_FILE_SIZE, $MAX_BACKUPS);

// 8) Разбиваем data на строки и пишем
$lines = explode("\n", $data);
$written = writeLines($file, $lines, $MAX_LINE_LEN);

if ($written === false) {
    fail('500 Internal Server Error', 'ERROR: cannot write log file');
}

// 9) Отвечаем клиенту
ok('OK, received ' . $written . ' lines (' . $count . ' in request)');