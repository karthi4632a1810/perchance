<?php
// What the bot does with one Telegram update; used by webhook.php and poll.php.

define('HELP_TEXT', "Hi! I'm a chat bot powered by Perchance's free AI. Just send me a message.\n\n"
    . "/new - start a new conversation\n"
    . "/image <description> - make a picture (add \"wide\" or \"tall\" at the start for other shapes)\n"
    . "/id - show your Telegram user ID\n\n"
    . "In groups, use /ask <question> or reply to one of my messages.");

/** False if this update was handled before (Telegram re-sends updates it isn't sure arrived). */
function first_delivery(array $update)
{
    if (!isset($update['update_id'])) {
        return true;
    }
    $id = (int) $update['update_id'];
    return with_json_file('updates.json', function (array &$data) use ($id) {
        $seen = isset($data['ids']) ? $data['ids'] : [];
        if (in_array($id, $seen, true)) {
            return false;
        }
        $seen[] = $id;
        $data['ids'] = array_slice($seen, -300);
        return true;
    });
}

/**
 * TELEGRAM_ALLOWED_USERS lists user IDs or @usernames (or * for everyone). If it is empty, the first
 * person to message the bot becomes its owner and everyone else is refused.
 */
function user_allowed(array $from)
{
    $id = isset($from['id']) ? (string) $from['id'] : '';
    $username = isset($from['username']) ? strtolower($from['username']) : '';
    $allowed = trim(cfg('TELEGRAM_ALLOWED_USERS'));
    if ($allowed === '*') {
        return true;
    }
    if ($allowed !== '') {
        foreach (explode(',', $allowed) as $entry) {
            $entry = strtolower(trim($entry));
            if ($entry !== '' && ($entry === $id || ($username !== '' && ltrim($entry, '@') === $username))) {
                return true;
            }
        }
        return false;
    }
    return with_json_file('owner.json', function (array &$data) use ($id) {
        if (empty($data['owner'])) {
            $data['owner'] = $id;
            bot_log("Owner is now user $id (the first person to message the bot)");
        }
        return $data['owner'] === $id;
    });
}

/**
 * The bot's owner: the first user in TELEGRAM_ALLOWED_USERS, or whoever claimed the bot (data/owner.json).
 * Only the owner's private chat gets the notes in knowledge/.
 */
function is_owner(array $from)
{
    $id = isset($from['id']) ? (string) $from['id'] : '';
    $username = isset($from['username']) ? strtolower($from['username']) : '';
    $allowed = trim(cfg('TELEGRAM_ALLOWED_USERS'));
    if ($allowed !== '' && $allowed !== '*') {
        $first = strtolower(trim(explode(',', $allowed)[0]));
        return $id !== '' && ($first === $id || ($username !== '' && ltrim($first, '@') === $username));
    }
    $owner = json_decode((string) @file_get_contents(DATA_DIR . '/owner.json'), true);
    return $id !== '' && is_array($owner) && isset($owner['owner']) && (string) $owner['owner'] === $id;
}

/** "/image@MyBot wide a castle" -> ["/image", "wide a castle"]. */
function parse_command($text)
{
    if (!preg_match('~^/(\w+)(?:@\w+)?(?:\s+(.*))?$~s', $text, $m)) {
        return ['', $text];
    }
    return ['/' . strtolower($m[1]), isset($m[2]) ? trim($m[2]) : ''];
}

function handle_update(array $update)
{
    if (empty($update['message'])) {
        return;   // edits, channel posts, etc.
    }
    $message = $update['message'];
    $chatId = $message['chat']['id'];
    $messageId = $message['message_id'];
    $from = isset($message['from']) ? $message['from'] : [];
    $text = trim(isset($message['text']) ? $message['text'] : (isset($message['caption']) ? $message['caption'] : ''));
    $isGroup = in_array($message['chat']['type'], ['group', 'supergroup'], true);
    list($command, $args) = parse_command($text);

    // In groups, only commands and replies to the bot are meant for it.
    $repliesToBot = isset($message['reply_to_message']['from']['id'])
        && (int) $message['reply_to_message']['from']['id'] === tg_bot_id();
    if ($isGroup && $command === '' && !$repliesToBot) {
        return;
    }
    if (!user_allowed($from)) {
        $id = isset($from['id']) ? $from['id'] : '?';
        tg_send_text($chatId, "Sorry, this bot is private. Your Telegram user ID is $id; the owner can add it to "
            . 'TELEGRAM_ALLOWED_USERS.');
        return;
    }
    $replyTo = $isGroup ? $messageId : null;

    switch ($command) {
        case '/start':
        case '/help':
            tg_send_text($chatId, HELP_TEXT);
            return;
        case '/new':
        case '/reset':
            chat_reset("tg-$chatId");
            tg_send_text($chatId, 'Started a new conversation.');
            return;
        case '/id':
            tg_send_text($chatId, "Your user ID: {$from['id']}\nThis chat's ID: $chatId");
            return;
        case '/image':
            send_image($chatId, $args, $replyTo);
            return;
        case '/ask':
            $text = $args;
            break;
        case '':
            break;
        default:
            tg_send_text($chatId, "I don't know $command.\n\n" . HELP_TEXT);
            return;
    }
    if ($text === '') {
        tg_send_text($chatId, $command === '/ask' ? 'Usage: /ask <your question>' : 'I can only read text messages.');
        return;
    }

    tg_action($chatId);
    $lastAction = time();
    $keepTyping = function () use ($chatId, &$lastAction) {
        if (time() - $lastAction >= 4) {
            tg_action($chatId);
            $lastAction = time();
        }
    };
    try {
        $knowledge = !$isGroup && is_owner($from) ? knowledge_text() : '';
        $reply = chat_reply("tg-$chatId", $text, $keepTyping, $knowledge);
    } catch (Throwable $e) {
        bot_log("Chat $chatId: " . $e->getMessage());
        $reply = '⚠️ ' . $e->getMessage();
    }
    tg_send_text($chatId, $reply, $replyTo);
}

function send_image($chatId, $prompt, $replyTo)
{
    $resolution = '512x512';
    if (preg_match('/^(wide|landscape|tall|portrait)\b\s*(.*)$/is', $prompt, $m)) {
        $resolution = in_array(strtolower($m[1]), ['wide', 'landscape'], true) ? '768x512' : '512x768';
        $prompt = trim($m[2]);
    }
    if ($prompt === '') {
        tg_send_text($chatId, 'Usage: /image <description>, for example: /image wide a lighthouse at sunset');
        return;
    }
    tg_action($chatId, 'upload_photo');
    try {
        $image = with_lock('perchance-image', 180, function () use ($prompt, $resolution) {
            return perchance_image($prompt, $resolution);
        });
        tg_send_photo($chatId, $image['data'], $image['extension'], $prompt, $replyTo);
    } catch (Throwable $e) {
        bot_log("Image for chat $chatId: " . $e->getMessage());
        tg_send_text($chatId, '⚠️ ' . $e->getMessage(), $replyTo);
    }
}
