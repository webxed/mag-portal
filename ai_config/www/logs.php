<?php
/**
 * logs.php — быстрый просмотр логов приставок.
 *
 *   logs.php                 — список устройств/лог-файлов + статус
 *   logs.php?mac=AA:BB:...&n=300[&r=10]  — последние N строк лога (r — автообновление, сек)
 */

declare(strict_types=1);
require __DIR__ . '/lib.php';

$mac     = (string) ($_GET['mac'] ?? '');
$n       = max(20, min(5000, (int) ($_GET['n'] ?? 300)));
$refresh = max(0, min(600, (int) ($_GET['r'] ?? 0)));
$logDir  = __DIR__ . '/mag_logs';

function status_badge(array $st): string
{
    $cls = ['ok' => 'st-ok', 'idle' => 'st-idle', 'offline' => 'st-off', 'nodata' => 'st-nd'][$st['state']] ?? 'st-nd';
    return '<span class="badge ' . $cls . '">' . h($st['label']) . '</span> '
         . '<span class="mut">' . h(fmt_age($st['age'])) . '</span>';
}
?><!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<?php if ($refresh > 0 && $mac !== ''): ?>
<meta http-equiv="refresh" content="<?= $refresh ?>">
<?php endif; ?>
<title>Логи приставок</title>
<style>
  body { font: 13px/1.4 ui-monospace, Consolas, monospace; margin:0; background:#0d1117; color:#c9d1d9; }
  header { background:#161b22; padding:10px 16px; border-bottom:1px solid #30363d; font-family:-apple-system,Segoe UI,Arial,sans-serif; }
  header a { color:#58a6ff; text-decoration:none; margin-right:14px; }
  .wrap { padding: 14px 16px; }
  table { border-collapse: collapse; width:100%; font-family:-apple-system,Segoe UI,Arial,sans-serif; }
  th, td { border:1px solid #30363d; padding:5px 8px; text-align:left; }
  th { background:#161b22; }
  a { color:#58a6ff; }
  .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; color:#fff; font-family:-apple-system,Segoe UI,Arial,sans-serif; }
  .st-ok { background:#1f883d; } .st-idle { background:#bf8700; }
  .st-off { background:#cf222e; } .st-nd { background:#8b949e; }
  .mut { color:#8b949e; }
  pre { background:#010409; border:1px solid #30363d; border-radius:6px; padding:10px; overflow:auto; max-height:75vh; }
  .hb { color:#3fb950; }
  .bar { margin-bottom:10px; font-family:-apple-system,Segoe UI,Arial,sans-serif; }
</style>
</head>
<body>
<header>
  <a href="index.php">&larr; Панель</a>
  <a href="logs.php">Все логи</a>
</header>
<div class="wrap">
<?php if ($mac === ''): ?>
  <h3>Логи устройств</h3>
  <table>
    <tr><th>MAC</th><th>Файл</th><th>Размер</th><th>Изменён</th><th>Статус</th><th></th></tr>
    <?php
    $data = devices_load();
    $known = [];
    foreach ($data['devices'] as $d) {
        $known[strtoupper((string) ($d['mac'] ?? ''))] = $d;
    }
    $files = glob($logDir . '/*.log') ?: [];
    usort($files, fn($a, $b) => filemtime($b) <=> filemtime($a));
    if (!$files) {
        echo '<tr><td colspan="6" class="mut">Логов пока нет.</td></tr>';
    }
    foreach ($files as $file) {
        $base = basename($file, '.log');
        $macD = strtoupper(str_replace('-', ':', $base));
        $dev  = $known[$macD] ?? ['mac' => $macD, 'ip' => ''];
        $st   = device_status($dev);
        $sz   = filesize($file);
        echo '<tr>'
           . '<td>' . h($macD) . '</td>'
           . '<td>' . h(basename($file)) . '</td>'
           . '<td>' . number_format($sz / 1024, 1, '.', ' ') . ' КБ</td>'
           . '<td>' . h(date('Y-m-d H:i:s', (int) filemtime($file))) . '</td>'
           . '<td>' . status_badge($st) . '</td>'
           . '<td><a href="logs.php?mac=' . h(urlencode($macD)) . '">смотреть</a></td>'
           . '</tr>';
    }
    ?>
  </table>
<?php else: ?>
  <?php
    $data = devices_load();
    $dev = ['mac' => $mac, 'ip' => ''];
    foreach ($data['devices'] as $d) {
        if (strcasecmp((string) ($d['mac'] ?? ''), $mac) === 0) {
            $dev = $d; break;
        }
    }
    $st   = device_status($dev);
    $file = device_log_path($mac);
  ?>
  <div class="bar">
    <b><?= h($mac) ?></b>
    <?= $dev['ip'] ? '— ' . h($dev['ip']) : '' ?>
    &nbsp; <?= status_badge($st) ?>
    <?php if (!empty($st['hb'])): ?>
      &nbsp; <span class="mut">playing=<?= !empty($st['hb']['playing']) ? 'yes' : 'no' ?>
      mute=<?= !empty($st['hb']['mute']) ? 'yes' : 'no' ?>
      vol=<?= (int) ($st['hb']['vol'] ?? 0) ?></span>
    <?php endif; ?>
    &nbsp; | &nbsp;<a href="logs.php?mac=<?= h(urlencode($mac)) ?>&n=<?= $n ?>&r=10">автообновление 10с</a>
    &nbsp; <a href="logs.php?mac=<?= h(urlencode($mac)) ?>&n=<?= $n ?>">выкл</a>
  </div>
  <?php if (!is_file($file)): ?>
    <p class="mut">Файл лога не найден: <?= h(basename($file)) ?></p>
  <?php else: ?>
    <div class="bar mut"><?= h(basename($file)) ?> — последние <?= $n ?> строк, обновлён <?= h(date('Y-m-d H:i:s', (int) filemtime($file))) ?></div>
    <pre><?php
      foreach (tail_lines($file, $n) as $line) {
          $cls = strpos($line, 'HEARTBEAT') !== false ? ' class="hb"' : '';
          echo '<span' . $cls . '>' . h($line) . '</span>' . "\n";
      }
    ?></pre>
  <?php endif; ?>
<?php endif; ?>
</div>
</body>
</html>
