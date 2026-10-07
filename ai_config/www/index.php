<?php
/**
 * index.php — панель управления MAG-приставками.
 *
 * Возможности:
 *   - таблица устройств: ссылка + тип плеера, комментарий (расположение);
 *   - сохранение -> devices.json + генерация settings.json для приставок;
 *   - действия по кнопкам: перезагрузка, обновление services, проверка
 *     воспроизведения (через Python-скрипты), mute/unmute (через конфиг портала).
 *
 * Авторизация — на уровне веб-сервера (Basic Auth). Здесь только CSRF-защита.
 */

declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

$output = '';
$flash  = '';

function find_device(array $data, string $ip): ?array
{
    foreach ($data['devices'] as $d) {
        if (($d['ip'] ?? '') === $ip) {
            return $d;
        }
    }
    return null;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    csrf_check();
    $action = (string) ($_POST['action'] ?? '');

    if ($action === 'save') {
        $ips = $_POST['ip'] ?? [];
        $new = ['devices' => []];
        $n = is_array($ips) ? count($ips) : 0;
        for ($i = 0; $i < $n; $i++) {
            $new['devices'][] = [
                'ip'       => (string) ($_POST['ip'][$i] ?? ''),
                'mac'      => strtoupper((string) ($_POST['mac'][$i] ?? '')),
                'location' => (string) ($_POST['location'][$i] ?? ''),
                'stream'   => trim((string) ($_POST['stream'][$i] ?? '')),
                'type'     => (string) ($_POST['type'][$i] ?? 'hls'),
                'mute'     => !empty($_POST['mute'][$i]),
                'enabled'  => !empty($_POST['enabled'][$i]),
            ];
        }
        // Защита от случайного стирания: если в форме не оказалось ни одной
        // строки устройств, а список уже есть — не перезаписываем devices.json.
        $existing = devices_load();
        if ($n === 0 && count($existing['devices'] ?? []) > 0) {
            $flash = 'Форма пуста — devices.json НЕ изменён (защита от случайной перезаписи).';
        } else {
            devices_save($new);
            settings_generate($new);
            $flash = 'Сохранено. settings.json перегенерирован.';
        }

    } elseif ($action === 'regen') {
        settings_generate(devices_load());
        $flash = 'settings.json перегенерирован из devices.json.';

    } elseif ($action === 'device') {
        $ip = trim((string) ($_POST['ip'] ?? ''));
        $do = (string) ($_POST['do'] ?? '');
        $dev = find_device(devices_load(), $ip);

        if ($ip === '') {
            $flash = 'Не указан IP.';
        } elseif ($do === 'reboot') {
            [$code, $out] = run_python('mag_ctl.py', ['--hosts', $ip, '--reboot']);
            $output = "Перезагрузка $ip:\n" . $out;
        } elseif ($do === 'update') {
            [$code, $out] = run_python('deploy_services.py', ['--hosts', $ip]);
            $output = "Обновление services на $ip:\n" . $out;
        } elseif ($do === 'check') {
            [$code, $out] = run_python('check_playback.py', ['--hosts', $ip, '--window', '4']);
            $output = "Проверка воспроизведения $ip:\n" . $out;
        } elseif ($do === 'ping') {
            [$ok, $out] = ping_host($ip);
            $output = "Ping $ip: " . ($ok ? 'ДОСТУПЕН' : 'НЕДОСТУПЕН') . "\n\n" . $out;
        } elseif ($do === 'mute_on' || $do === 'mute_off') {
            $want = ($do === 'mute_on');
            $data = devices_load();
            foreach ($data['devices'] as &$d) {
                if (($d['ip'] ?? '') === $ip) {
                    $d['mute'] = $want;
                }
            }
            unset($d);
            devices_save($data);
            settings_generate($data);
            $flash = ($want ? 'Mute ВКЛ' : 'Mute ВЫКЛ') . " для $ip (применится в течение ~1 мин).";
        } elseif ($do === 'toggle_enable' && $dev) {
            $data = devices_load();
            foreach ($data['devices'] as &$d) {
                if (($d['ip'] ?? '') === $ip) {
                    $d['enabled'] = empty($d['enabled']);
                }
            }
            unset($d);
            devices_save($data);
            settings_generate($data);
            $flash = 'Изменён флаг «включено» для ' . $ip;
        } else {
            $flash = 'Неизвестное действие.';
        }
    } elseif ($action === 'all') {
        $do  = (string) ($_POST['do'] ?? '');
        $all = devices_load();
        $ips = [];
        foreach ($all['devices'] as $dd) {
            if (!empty($dd['ip'])) {
                $ips[] = (string) $dd['ip'];
            }
        }
        $joined = implode(',', $ips);
        if ($do === 'ping') {
            $lines = [];
            $okCount = 0;
            foreach ($ips as $one) {
                [$ok] = ping_host($one, 1, 800);
                if ($ok) {
                    $okCount++;
                }
                $lines[] = str_pad($one, 16) . ($ok ? 'ДОСТУПЕН' : 'НЕДОСТУПЕН');
            }
            $output = "Ping всех ($okCount/" . count($ips) . " доступны):\n" . implode("\n", $lines);
        } elseif ($do === 'check') {
            [$c, $o] = run_python('check_playback.py', ['--hosts', $joined, '--window', '4']);
            $output = "Проверка всех устройств:\n" . $o;
        } elseif ($do === 'update') {
            [$c, $o] = run_python('deploy_services.py', ['--hosts', $joined]);
            $output = "Обновление services на всех:\n" . $o;
        } elseif ($do === 'reboot') {
            [$c, $o] = run_python('mag_ctl.py', ['--hosts', $joined, '--reboot']);
            $output = "Перезагрузка всех:\n" . $o;
        } else {
            $flash = 'Неизвестное групповое действие.';
        }
    }
}

$data     = devices_load();
$devices  = $data['devices'];
$settings = json_decode((string) @file_get_contents(PANEL_SETTINGS), true);
$token    = csrf_token();
?><!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Панель управления приставками</title>
<style>
  :root { --bd:#d0d7de; --bg:#f6f8fa; --mut:#57606a; }
  body { font: 14px/1.45 -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; background: var(--bg); color: #1f2328; }
  header { background:#24292f; color:#fff; padding:12px 20px; }
  header h1 { margin:0; font-size:18px; }
  .wrap { padding: 16px 20px; }
  .bar { display:flex; gap:8px; align-items:center; margin-bottom:12px; flex-wrap:wrap; }
  table { border-collapse: collapse; width: 100%; background:#fff; }
  th, td { border:1px solid var(--bd); padding:6px 8px; vertical-align:middle; }
  th { background:#eaeef2; text-align:left; font-weight:600; white-space:nowrap; }
  td.actions { white-space:nowrap; }
  input[type=text] { width:100%; box-sizing:border-box; padding:4px 6px; border:1px solid var(--bd); border-radius:4px; }
  input.stream { min-width: 320px; }
  select { padding:4px 6px; border:1px solid var(--bd); border-radius:4px; }
  button { padding:5px 10px; border:1px solid var(--bd); border-radius:6px; background:#f6f8fa; cursor:pointer; }
  button:hover { background:#eaeef2; }
  button.primary { background:#1f883d; border-color:#1f883d; color:#fff; }
  button.danger  { background:#cf222e; border-color:#cf222e; color:#fff; }
  .flash { background:#dafbe1; border:1px solid #aceebb; padding:8px 12px; border-radius:6px; margin-bottom:12px; }
  .out { background:#0d1117; color:#c9d1d9; padding:12px; border-radius:6px; white-space:pre-wrap; overflow:auto; margin-bottom:12px; }
  .mut { color:var(--mut); font-size:12px; }
  b.on { color:#1f883d; } b.off { color:#cf222e; }
  .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; color:#fff; }
  .st-ok { background:#1f883d; } .st-idle { background:#bf8700; }
  .st-off { background:#cf222e; } .st-nd { background:#8b949e; }
  .loglink { margin-left:6px; }
</style>
</head>
<body>
<header><h1>Панель управления приставками MAG</h1></header>
<div class="wrap">

<?php if ($flash !== ''): ?><div class="flash"><?= h($flash) ?></div><?php endif; ?>
<?php if ($output !== ''): ?><pre class="out"><?= h($output) ?></pre><?php endif; ?>

<div class="bar">
  <!-- Кнопка привязана к форме saveform (form="saveform") — там есть строки устройств.
       Раньше это была отдельная форма без полей ip[], и её отправка затирала devices.json. -->
  <button class="primary" type="submit" form="saveform">Сохранить</button>
  <form method="post" style="display:inline">
    <input type="hidden" name="csrf" value="<?= h($token) ?>">
    <input type="hidden" name="action" value="regen">
    <button type="submit">Перегенерировать settings.json</button>
  </form>
  <a href="logs.php" target="_blank"><button type="button">Все логи</button></a>
  <span style="flex:0 0 100%;height:0"></span>
  <form method="post" style="display:inline">
    <input type="hidden" name="csrf" value="<?= h($token) ?>">
    <input type="hidden" name="action" value="all">
    <input type="hidden" name="do" value="ping">
    <button type="submit">Ping всех</button>
  </form>
  <form method="post" style="display:inline">
    <input type="hidden" name="csrf" value="<?= h($token) ?>">
    <input type="hidden" name="action" value="all">
    <input type="hidden" name="do" value="check">
    <button type="submit">Проверить всех</button>
  </form>
  <form method="post" style="display:inline" onsubmit="return confirm('Обновить services на ВСЕХ устройствах?')">
    <input type="hidden" name="csrf" value="<?= h($token) ?>">
    <input type="hidden" name="action" value="all">
    <input type="hidden" name="do" value="update">
    <button type="submit">Обновить на всех</button>
  </form>
  <form method="post" style="display:inline" onsubmit="return confirm('Перезагрузить ВСЕ устройства?')">
    <input type="hidden" name="csrf" value="<?= h($token) ?>">
    <input type="hidden" name="action" value="all">
    <input type="hidden" name="do" value="reboot">
    <button class="danger" type="submit">Перезагрузить всех</button>
  </form>
  <span class="mut">Устройств: <?= count($devices) ?>
    | потоков: <?= is_array($settings['streams'] ?? null) ? count($settings['streams']) : 0 ?>
    | mute: <?= is_array($settings['mute'] ?? null) ? count($settings['mute']) : 0 ?>
    | сервер: <?= h(server_base()) ?>
    | settings.json: <?= is_file(PANEL_SETTINGS) ? date('H:i:s', (int) filemtime(PANEL_SETTINGS)) : 'нет' ?>
  </span>
</div>

<table>
  <thead>
    <tr>
      <th>IP</th><th>MAC</th><th>Статус</th><th>Расположение</th><th>Ссылка</th><th>Тип</th>
      <th>Звук</th><th>Вкл</th><th>Действия</th>
    </tr>
  </thead>
  <tbody>
  <?php foreach ($devices as $i => $d): $st = device_status($d); ?>
    <tr>
      <td><?= h($d['ip'] ?? '') ?></td>
      <td><span class="mut"><?= h($d['mac'] ?? '') ?></span></td>
      <td><?php
        $cls = ['ok' => 'st-ok', 'idle' => 'st-idle', 'offline' => 'st-off', 'nodata' => 'st-nd'][$st['state']] ?? 'st-nd';
        echo '<span class="badge ' . $cls . '">' . h($st['label']) . '</span>';
        echo '<br><span class="mut">' . h(fmt_age($st['age'])) . '</span>';
        if (!empty($st['hb'])) {
            $hb = $st['hb'];
            echo '<br><span class="mut">play=' . (!empty($hb['playing']) ? 'yes' : 'no')
               . ' mute=' . (!empty($hb['mute']) ? 'yes' : 'no');
            if (isset($hb['cc'])) { echo ' err ' . (int) $hb['cc'] . '/' . (int) $hb['rtp'] . '/' . (int) $hb['vdec']; }
            echo '</span>';
        }
      ?></td>
      <td><input type="text" form="saveform" name="location[<?= $i ?>]" value="<?= h($d['location'] ?? '') ?>"></td>
      <td><input type="text" form="saveform" class="stream" name="stream[<?= $i ?>]" value="<?= h($d['stream'] ?? '') ?>"></td>
      <td>
        <select form="saveform" name="type[<?= $i ?>]">
          <?php foreach (STREAM_TYPES as $t): ?>
            <option value="<?= h($t) ?>"<?= (($d['type'] ?? '') === $t) ? ' selected' : '' ?>><?= h($t) ?></option>
          <?php endforeach; ?>
        </select>
      </td>
      <td>
        <input type="checkbox" form="saveform" name="mute[<?= $i ?>]" value="1"<?= !empty($d['mute']) ? ' checked' : '' ?>>
        <b class="<?= !empty($d['mute']) ? 'off' : 'on' ?>"><?= !empty($d['mute']) ? 'выкл' : 'вкл' ?></b>
      </td>
      <td>
        <input type="checkbox" form="saveform" name="enabled[<?= $i ?>]" value="1"<?= !empty($d['enabled']) ? ' checked' : '' ?>>
      </td>
      <td class="actions">
        <?php
          $frm = function (string $do, string $label, string $cls = '') use ($token, $d) {
              echo '<form method="post" style="display:inline">'
                 . '<input type="hidden" name="csrf" value="' . h($token) . '">'
                 . '<input type="hidden" name="action" value="device">'
                 . '<input type="hidden" name="ip" value="' . h($d['ip'] ?? '') . '">'
                 . '<input type="hidden" name="do" value="' . h($do) . '">'
                 . '<button class="' . h($cls) . '" type="submit">' . h($label) . '</button>'
                 . '</form> ';
          };
          $frm('reboot', 'Перезагрузить', 'danger');
          $frm('update', 'Обновить services');
          $frm('check', 'Проверить');
          $frm('ping', 'Ping');
          $frm(!empty($d['mute']) ? 'mute_off' : 'mute_on', !empty($d['mute']) ? 'Вкл. звук' : 'Mute');
          echo '<a class="loglink" href="logs.php?mac=' . h(urlencode((string) ($d['mac'] ?? ''))) . '" target="_blank">Логи</a>';
        ?>
      </td>
    </tr>
  <?php endforeach; ?>
  </tbody>
</table>

<!-- Форма сохранения: поля таблицы привязаны к ней через атрибут form -->
<form id="saveform" method="post">
  <input type="hidden" name="csrf" value="<?= h($token) ?>">
  <input type="hidden" name="action" value="save">
  <?php foreach ($devices as $i => $d): ?>
    <input type="hidden" name="ip[<?= $i ?>]"  value="<?= h($d['ip'] ?? '') ?>">
    <input type="hidden" name="mac[<?= $i ?>]" value="<?= h($d['mac'] ?? '') ?>">
  <?php endforeach; ?>
</form>

</div>
</body>
</html>
