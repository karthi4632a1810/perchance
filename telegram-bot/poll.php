<?php
// Runs the bot without a webhook: asks Telegram for new messages (long polling) and answers them.
// Use it on a computer where Perchance accepts the key (the one whose browser the key came from):
//   php poll.php           keeps running and answers messages as they arrive
//   php poll.php --once    answers the messages that are waiting, then exits

if (PHP_SAPI !== 'cli') {
    http_response_code(403);
    exit;
}
require __DIR__ . '/lib/bootstrap.php';
require __DIR__ . '/lib/perchance.php';
require __DIR__ . '/lib/telegram.php';
require __DIR__ . '/lib/chat.php';
require __DIR__ . '/lib/bot.php';

$once = in_array('--once', $argv, true);
$me = tg_api('getMe');
if (empty($me['ok'])) {
    fwrite(STDERR, 'Telegram rejected the bot token: ' . (isset($me['description']) ? $me['description'] : '?') . "\n");
    exit(1);
}
$hook = tg_api('getWebhookInfo');
if (!empty($hook['result']['url'])) {
    fwrite(STDERR, "A webhook is set ({$hook['result']['url']}), and Telegram doesn't allow polling at the same time.\n"
        . "Remove it first: open setup.php?secret=...&action=delete\n");
    exit(1);
}
echo date('H:i:s') . " Answering messages to @{$me['result']['username']}" . ($once ? ' that are waiting' : '; press Ctrl+C to stop') . "\n";

$offset = 0;
while (true) {
    // timeout=50: Telegram holds the request open until a message arrives (long polling).
    $result = tg_api('getUpdates', ['offset' => $offset, 'timeout' => $once ? 0 : 50, 'allowed_updates' => ['message']]);
    if (empty($result['ok'])) {
        bot_log('getUpdates failed: ' . (isset($result['description']) ? $result['description'] : '?'));
        sleep(5);
        continue;
    }
    foreach ($result['result'] as $update) {
        $offset = $update['update_id'] + 1;
        if (!first_delivery($update)) {
            continue;
        }
        $from = isset($update['message']['from']['id']) ? $update['message']['from']['id'] : '?';
        echo date('H:i:s') . " Message from user $from\n";
        try {
            handle_update($update);
        } catch (Throwable $e) {
            bot_log('Unhandled error: ' . $e->getMessage());
            echo date('H:i:s') . ' Error: ' . $e->getMessage() . "\n";
        }
    }
    if ($once) {
        if ($offset) {
            tg_api('getUpdates', ['offset' => $offset, 'timeout' => 0]);   // tells Telegram these were handled
        }
        break;
    }
}
