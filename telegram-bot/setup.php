<?php
// Checks the deployment and connects the bot to Telegram. Open it with the secret from .env:
//   setup.php?secret=<TELEGRAM_WEBHOOK_SECRET>                  checks only
//   setup.php?secret=<TELEGRAM_WEBHOOK_SECRET>&action=webhook   checks, then registers webhook.php with Telegram
//   ...&action=delete                                            disconnects the bot from this server
//   ...&image=1                                                  also generates one test image

require __DIR__ . '/lib/bootstrap.php';
require __DIR__ . '/lib/perchance.php';
require __DIR__ . '/lib/telegram.php';

header('Content-Type: text/plain; charset=utf-8');
$secret = cfg('TELEGRAM_WEBHOOK_SECRET');
if ($secret === '' || !hash_equals($secret, isset($_GET['secret']) ? (string) $_GET['secret'] : '')) {
    http_response_code(403);
    echo "Forbidden. Open setup.php?secret=<TELEGRAM_WEBHOOK_SECRET from .env>\n";
    exit;
}
@set_time_limit(300);
$action = isset($_GET['action']) ? $_GET['action'] : 'check';
$problems = 0;

function report($ok, $what, $detail = '')
{
    global $problems;
    if (!$ok) {
        $problems++;
    }
    echo ($ok ? '[ OK ] ' : '[FAIL] ') . $what . ($detail !== '' ? "\n       $detail" : '') . "\n";
}

/** The URL of this folder, as the outside world sees it. */
function folder_url($scheme = null)
{
    $https = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
        || (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https');
    $dir = rtrim(str_replace('\\', '/', dirname($_SERVER['SCRIPT_NAME'])), '/');
    return ($scheme ?: ($https ? 'https' : 'http')) . '://' . $_SERVER['HTTP_HOST'] . $dir;
}

echo "Perchance Telegram bot: setup check\n\n";

// 1. PHP
$phpOk = version_compare(PHP_VERSION, '7.4', '>=');
report($phpOk, 'PHP ' . PHP_VERSION, $phpOk ? '' : 'Choose PHP 8.x in hPanel > Advanced > PHP Configuration.');
report(function_exists('curl_init'), 'PHP cURL extension');

// 2. data/ for conversations
try {
    with_json_file('probe.json', function (array &$data) {
        $data['checked'] = time();
    });
    report(true, 'data/ folder is writable');
} catch (Throwable $e) {
    report(false, 'data/ folder is writable', $e->getMessage());
}

// 3. Secrets must not be downloadable
foreach (['.env' => 'TELEGRAM_', 'data/probe.json' => 'checked'] as $file => $marker) {
    list($status, $body) = http_request('GET', folder_url() . '/' . $file, [], null, 15);
    if ($status === 0) {
        echo "[ ?? ] Could not test whether $file is public ($body). Open " . folder_url() . "/$file yourself: it must not show anything.\n";
    } else {
        $public = $status === 200 && strpos($body, $marker) !== false;
        report(!$public, "$file is not publicly readable (HTTP $status)",
            $public ? 'DANGER: anyone can read it. Make sure the .htaccess files were uploaded (they are hidden files).' : '');
    }
}

// 4. Telegram
$token = cfg('TELEGRAM_BOT_TOKEN');
if ($token === '') {
    report(false, 'TELEGRAM_BOT_TOKEN is set in .env', 'Get a token from @BotFather and put it in .env.');
} else {
    $me = tg_api('getMe');
    report(!empty($me['ok']), 'Telegram bot token works', !empty($me['ok'])
        ? 'Bot: @' . $me['result']['username'] : (isset($me['description']) ? $me['description'] : ''));
}

// 5. Perchance from this server
$started = microtime(true);
try {
    $reply = with_lock('perchance', 60, function () {
        return perchance_generate('Reply with exactly one word: pong', [], 0, 60);
    });
    report(stripos($reply, 'pong') !== false, sprintf('Perchance text generation works from this server (%.1fs)', microtime(true) - $started),
        'Reply: ' . substr(trim($reply), 0, 200));
} catch (Throwable $e) {
    report(false, 'Perchance text generation works from this server', $e->getMessage());
}
if (!empty($_GET['image'])) {
    try {
        $image = perchance_image('a small red apple on a white table');
        report(true, 'Perchance image generation works from this server', strlen($image['data']) . ' bytes');
    } catch (Throwable $e) {
        report(false, 'Perchance image generation works from this server', $e->getMessage());
    }
}

// 6. Webhook
if ($token !== '') {
    if ($action === 'webhook') {
        $url = cfg('WEBHOOK_URL', folder_url('https') . '/webhook.php');
        $result = tg_api('setWebhook', ['url' => $url, 'secret_token' => $secret, 'allowed_updates' => ['message'],
            'drop_pending_updates' => true, 'max_connections' => 10]);
        report(!empty($result['ok']), "Webhook set to $url", isset($result['description']) ? $result['description'] : '');
    } elseif ($action === 'delete') {
        $result = tg_api('deleteWebhook');
        report(!empty($result['ok']), 'Webhook removed', isset($result['description']) ? $result['description'] : '');
    }
    $info = tg_api('getWebhookInfo');
    if (!empty($info['ok'])) {
        $w = $info['result'];
        echo "\nTelegram webhook: " . ($w['url'] !== '' ? $w['url'] : '(not set: add &action=webhook to set it)') . "\n";
        echo 'Updates waiting: ' . $w['pending_update_count'] . "\n";
        if (!empty($w['last_error_message'])) {
            echo 'Last delivery error: ' . $w['last_error_message'] . ' (' . date('Y-m-d H:i:s', $w['last_error_date']) . ")\n";
        }
    }
}

echo $problems ? "\n$problems problem(s) found.\n" : "\nAll checks passed.\n";
