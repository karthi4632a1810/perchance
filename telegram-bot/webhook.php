<?php
// Telegram webhook: Telegram POSTs every message here; the reply comes from Perchance.

require __DIR__ . '/lib/bootstrap.php';
require __DIR__ . '/lib/perchance.php';
require __DIR__ . '/lib/telegram.php';
require __DIR__ . '/lib/chat.php';
require __DIR__ . '/lib/bot.php';

// Only Telegram knows the secret we gave it in setWebhook.
$secret = cfg('TELEGRAM_WEBHOOK_SECRET');
$given = isset($_SERVER['HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN']) ? $_SERVER['HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN'] : '';
if ($secret === '' || !hash_equals($secret, $given)) {
    http_response_code(403);
    exit;
}

$update = json_decode((string) file_get_contents('php://input'), true);
respond_now();
if (!is_array($update) || !first_delivery($update)) {
    exit;
}
try {
    handle_update($update);
} catch (Throwable $e) {
    bot_log('Unhandled error: ' . $e->getMessage());
}

/**
 * Answers Telegram right away and keeps running: generating a reply can take longer than Telegram
 * waits, and an unanswered update would be delivered again.
 */
function respond_now()
{
    ignore_user_abort(true);
    @set_time_limit(300);
    http_response_code(200);
    if (function_exists('fastcgi_finish_request')) {
        fastcgi_finish_request();
    } elseif (function_exists('litespeed_finish_request')) {   // Hostinger runs LiteSpeed
        litespeed_finish_request();
    } else {
        header('Content-Type: text/plain');
        header('Content-Length: 2');
        header('Connection: close');
        echo 'ok';
        while (ob_get_level() > 0) {
            ob_end_flush();
        }
        flush();
    }
}
