<?php
/**
 * lib.php — общее для панели управления приставками.
 *
 * Панель хранит список устройств в devices.json (ip, mac, location, stream, type,
 * mute, enabled) и ГЕНЕРИРУЕТ из него settings.json — конфиг, который тянут
 * приставки (ссылки + сопоставление MAC + флаги mute).
 *
 * Управляющие действия (перезагрузка, обновление services, проверка) выполняются
 * вызовом Python-скриптов из tools/ (paramiko), авторизация — на уровне веб-сервера
 * (Basic Auth).
 */

declare(strict_types=1);

require_once __DIR__ . '/config.php';

const PANEL_DEVICES  = __DIR__ . '/devices.json';
const PANEL_SETTINGS = __DIR__ . '/settings.json';

/** Корень репозитория (ai_config/www -> ai_config -> repo). */
define('REPO_ROOT', dirname(dirname(__DIR__)));
define('TOOLS_DIR', REPO_ROOT . DIRECTORY_SEPARATOR . 'tools');

/** Интерпретатор Python (при необходимости замените на полный путь). */
define('PYTHON_BIN', 'python');

/** Доступные типы потоков для плеера. */
const STREAM_TYPES = ['hls', 'udp', 'rtsp', 'rtmp', 'radio', 'http'];

/** Значения по умолчанию для settings.json (если файла ещё нет). */
function settings_defaults(): array
{
    return [
        'tv_volume' => 80,
        'radio_streams' => [],
        'volume_schedule' => [
            ['from' => 0,  'to' => 8,  'vol' => 0],
            ['from' => 8,  'to' => 19, 'vol' => 80],
            ['from' => 19, 'to' => 24, 'vol' => 0],
        ],
        'fade_duration_ms' => 3000,
    ];
}

/** Читает devices.json. Если файла нет — пустой список. */
function devices_load(): array
{
    if (!is_file(PANEL_DEVICES)) {
        return ['devices' => []];
    }
    $data = json_decode((string) file_get_contents(PANEL_DEVICES), true);
    if (!is_array($data) || !isset($data['devices']) || !is_array($data['devices'])) {
        return ['devices' => []];
    }
    return $data;
}

/** Сохраняет devices.json. */
function devices_save(array $data): void
{
    file_put_contents(
        PANEL_DEVICES,
        json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . "\n"
    );
}

/** Идентификатор потока для устройства (по MAC, иначе по IP). */
function device_stream_id(array $dev): string
{
    $key = $dev['mac'] ?? '';
    if ($key === '') {
        $key = $dev['ip'] ?? '';
    }
    return 'dev_' . preg_replace('/[^A-Za-z0-9]/', '', $key);
}

/**
 * Генерирует settings.json из списка устройств.
 * Возвращает сгенерированный массив.
 */
function settings_generate(array $data): array
{
    $cur = [];
    if (is_file(PANEL_SETTINGS)) {
        $tmp = json_decode((string) file_get_contents(PANEL_SETTINGS), true);
        if (is_array($tmp)) {
            $cur = $tmp;
        }
    }
    $def = settings_defaults();

    $streams = [];
    $mac = [];
    $mute = [];
    $radio = [];
    $first_id = '';

    foreach ($data['devices'] as $dev) {
        if (empty($dev['enabled'])) {
            continue;
        }
        $url = trim((string) ($dev['stream'] ?? ''));
        $macAddr = strtoupper(trim((string) ($dev['mac'] ?? '')));
        if ($macAddr === '') {
            continue;
        }
        $id = device_stream_id($dev);
        if ($url !== '') {
            $streams[$id] = $url;
            $mac[$macAddr] = $id;
            if (($dev['type'] ?? '') === 'radio') {
                $radio[] = $id;
            }
            if ($first_id === '') {
                $first_id = $id;
            }
        }
        if (!empty($dev['mute'])) {
            $mute[$macAddr] = true;
        }
    }

    $settings = [
        'version' => time(),
        'streams' => $streams,
        'mac' => $mac,
        'mute' => $mute,
        'default' => $first_id !== '' ? $first_id : 'main',
        'radio_streams' => array_values(array_unique($radio)),
        'tv_volume' => $cur['tv_volume'] ?? $def['tv_volume'],
        'volume_schedule' => $cur['volume_schedule'] ?? $def['volume_schedule'],
        'fade_duration_ms' => $cur['fade_duration_ms'] ?? $def['fade_duration_ms'],
    ];

    file_put_contents(
        PANEL_SETTINGS,
        json_encode($settings, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . "\n"
    );
    return $settings;
}

/**
 * Запускает Python-скрипт из tools/. Возвращает [код, вывод].
 */
function run_python(string $script, array $args): array
{
    $cmd = escapeshellarg(PYTHON_BIN) . ' ' . escapeshellarg(TOOLS_DIR . DIRECTORY_SEPARATOR . $script);
    foreach ($args as $a) {
        $cmd .= ' ' . escapeshellarg((string) $a);
    }
    $cmd .= ' 2>&1';

    $out = shell_exec($cmd);
    return [$out === null ? 127 : 0, (string) $out];
}

/** Экранирование для HTML. */
function h($s): string
{
    return htmlspecialchars((string) $s, ENT_QUOTES, 'UTF-8');
}

/** CSRF-токен (сессия). */
function csrf_token(): string
{
    if (empty($_SESSION['csrf'])) {
        $_SESSION['csrf'] = bin2hex(random_bytes(16));
    }
    return $_SESSION['csrf'];
}

function csrf_check(): void
{
    $t = $_POST['csrf'] ?? '';
    if (!is_string($t) || !hash_equals($_SESSION['csrf'] ?? '', $t)) {
        http_response_code(400);
        exit('CSRF check failed');
    }
}

// ============================================================================
// Логи и статус (по строкам HEARTBEAT из mag_logs)
// ============================================================================

/** Путь к лог-файлу устройства: log.php заменяет ':' на '-' в имени. */
function device_log_path(string $mac): string
{
    $name = strtoupper(str_replace(':', '-', trim($mac)));
    return __DIR__ . '/mag_logs/' . $name . '.log';
}

/** Последняя строка HEARTBEAT из лог-файла (читаем только хвост файла). */
function heartbeat_last(string $mac): ?array
{
    $file = device_log_path($mac);
    if (!is_file($file)) {
        return null;
    }
    $fp = @fopen($file, 'rb');
    if (!$fp) {
        return null;
    }
    $size = filesize($file);
    $tail = 65536;
    if ($size > $tail) {
        fseek($fp, -$tail, SEEK_END);
    }
    $data = (string) stream_get_contents($fp);
    fclose($fp);

    $lines = preg_split('/\r\n|\n/', $data);
    $raw = null;
    for ($i = count($lines) - 1; $i >= 0; $i--) {
        if (strpos($lines[$i], 'HEARTBEAT') !== false) {
            $raw = $lines[$i];
            break;
        }
    }
    if ($raw === null) {
        return null;
    }

    $hb = ['raw' => $raw];
    if (preg_match('/playing=(\w+)/', $raw, $m)) { $hb['playing'] = ($m[1] === 'yes'); }
    if (preg_match('/mute=(\w+)/', $raw, $m))    { $hb['mute']    = ($m[1] === 'yes'); }
    if (preg_match('/vol=(\d+)/', $raw, $m))     { $hb['vol']     = (int) $m[1]; }
    if (preg_match('/stream=(\S+)/', $raw, $m))  { $hb['stream']  = $m[1]; }
    if (preg_match('/stats\[cc=(\d+),rtp=(\d+),vdec=(\d+)\]/', $raw, $m)) {
        $hb['cc'] = (int) $m[1]; $hb['rtp'] = (int) $m[2]; $hb['vdec'] = (int) $m[3];
    }
    if (preg_match('/^\[(.*?)\]/', $raw, $m)) {
        $ts = preg_replace('/\s*\(.*\)\s*$/', '', $m[1]); // убрать " (EEST)"
        $t = strtotime($ts);
        if ($t !== false) {
            $hb['ts'] = $t;
        }
    }
    if (!isset($hb['ts'])) {
        $hb['ts'] = (int) @filemtime($file);
    }
    return $hb;
}

/**
 * Статус устройства по последнему heartbeat:
 *   ok      — есть свежий heartbeat, playing=yes;
 *   idle    — есть свежий heartbeat, но не играет;
 *   offline — heartbeat устарел (> 180 c);
 *   nodata  — heartbeat ещё не приходил.
 */
function device_status(array $dev): array
{
    $mac = (string) ($dev['mac'] ?? '');
    if ($mac === '') {
        return ['state' => 'nodata', 'label' => 'нет MAC', 'age' => null, 'hb' => null];
    }
    $hb = heartbeat_last($mac);
    if ($hb === null) {
        return ['state' => 'nodata', 'label' => 'нет данных', 'age' => null, 'hb' => null];
    }
    $age = time() - (int) $hb['ts'];
    if ($age > 180) {
        return ['state' => 'offline', 'label' => 'нет связи', 'age' => $age, 'hb' => $hb];
    }
    if (!empty($hb['playing'])) {
        return ['state' => 'ok', 'label' => 'работает', 'age' => $age, 'hb' => $hb];
    }
    return ['state' => 'idle', 'label' => 'не играет', 'age' => $age, 'hb' => $hb];
}

/** Человекочитаемый возраст в секундах. */
function fmt_age(?int $age): string
{
    if ($age === null) {
        return '—';
    }
    if ($age < 60) {
        return $age . ' с';
    }
    if ($age < 3600) {
        return (int) floor($age / 60) . ' мин';
    }
    return (int) floor($age / 3600) . ' ч';
}

/** Хвост файла: последние $n строк. */
function tail_lines(string $file, int $n): array
{
    if (!is_file($file)) {
        return [];
    }
    $fp = @fopen($file, 'rb');
    if (!$fp) {
        return [];
    }
    $size = filesize($file);
    $buf = 262144;
    if ($size > $buf) {
        fseek($fp, -$buf, SEEK_END);
    }
    $data = (string) stream_get_contents($fp);
    fclose($fp);
    $lines = preg_split('/\r\n|\n/', $data);
    if ($lines && end($lines) === '') {
        array_pop($lines);
    }
    return array_slice($lines, -$n);
}

/**
 * Проверка доступности хоста через ping. Возвращает [доступен, вывод].
 * Работает и на Windows, и на Linux (по PHP_OS_FAMILY).
 */
function ping_host(string $ip, int $count = 2, int $waitMs = 1000): array
{
    $ip = trim($ip);
    if (filter_var($ip, FILTER_VALIDATE_IP) === false) {
        return [false, 'некорректный IP: ' . $ip];
    }
    $isWin = stripos(PHP_OS_FAMILY, 'Windows') !== false;
    if ($isWin) {
        $cmd = 'ping -n ' . $count . ' -w ' . $waitMs . ' ' . escapeshellarg($ip);
    } else {
        $sec = max(1, (int) ceil($waitMs / 1000));
        $cmd = 'ping -c ' . $count . ' -W ' . $sec . ' ' . escapeshellarg($ip);
    }
    $lines = [];
    $code = 1;
    @exec($cmd, $lines, $code);
    return [$code === 0, implode("\n", $lines)];
}

/** Быстрая проверка списка хостов. Возвращает [ip => bool]. */
function ping_all(array $ips): array
{
    $res = [];
    foreach ($ips as $ip) {
        [$ok] = ping_host((string) $ip, 1, 800);
        $res[(string) $ip] = $ok;
    }
    return $res;
}
