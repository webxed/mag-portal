<?php
/**
 * config.php — настройки СЕРВЕРА (панель, приём логов, раздача конфига приставкам).
 *
 * При переносе на другой компьютер меняется ТОЛЬКО здесь (или через переменные
 * окружения MAG_SERVER_HOST / MAG_SERVER_PORT).
 *
 * ВАЖНО: этот адрес «зашит» в портал на приставках (ai_config/services.html),
 * потому что приставки открывают его по file:// и должны знать, куда обращаться
 * за settings.json и логами. После смены адреса пересоберите портал:
 *     python tools/build_portal.py
 * и разложите на приставки.
 */

declare(strict_types=1);

define('SERVER_HOST', getenv('MAG_SERVER_HOST') ?: '192.168.1.100');
define('SERVER_PORT', (int) (getenv('MAG_SERVER_PORT') ?: 8088));

/** Базовый адрес сервера, например http://192.168.1.100:8088 */
function server_base(): string
{
    return 'http://' . SERVER_HOST . ':' . SERVER_PORT;
}
