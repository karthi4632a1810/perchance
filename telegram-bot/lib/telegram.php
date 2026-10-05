<?php
// Telegram Bot API calls and message formatting.

function tg_api($method, array $params = [], $multipart = false)
{
    $token = cfg('TELEGRAM_BOT_TOKEN');
    if ($token === '') {
        throw new RuntimeException('TELEGRAM_BOT_TOKEN is not set in .env.');
    }
    $url = rtrim(cfg('TELEGRAM_API_BASE', 'https://api.telegram.org'), '/') . "/bot$token/$method";
    if ($multipart) {
        list($status, $raw) = http_request('POST', $url, [], $params, 120);
    } else {
        list($status, $raw) = http_request('POST', $url, ['Content-Type: application/json'],
            json_encode($params, JSON_UNESCAPED_UNICODE), 60);
    }
    $result = json_decode($raw, true);
    return is_array($result) ? $result : ['ok' => false, 'description' => "HTTP $status: " . substr($raw, 0, 200)];
}

/** The bot's own user id is the number before the colon in its token. */
function tg_bot_id()
{
    return (int) explode(':', cfg('TELEGRAM_BOT_TOKEN'))[0];
}

/**
 * Splits text into Telegram-sized messages at line breaks. A ``` block cut in two is closed at the
 * end of one message and reopened at the start of the next.
 */
function split_message($text, $limit = 3500)
{
    $chunks = [];
    $current = '';
    foreach (explode("\n", $text) as $line) {
        while (strlen($line) > $limit) {
            $piece = utf8_cut($line, $limit);
            if ($current !== '') {
                $chunks[] = $current;
                $current = '';
            }
            $chunks[] = $piece;
            $line = substr($line, strlen($piece));
        }
        if ($current !== '' && strlen($current) + strlen($line) + 1 > $limit) {
            $chunks[] = $current;
            $current = '';
        }
        $current = $current === '' ? $line : "$current\n$line";
    }
    if (trim($current) !== '') {
        $chunks[] = $current;
    }
    $open = false;
    foreach ($chunks as $i => $chunk) {
        if ($open) {
            $chunk = "```\n" . $chunk;
        }
        $open = preg_match_all('/^```/m', $chunk) % 2 === 1;
        $chunks[$i] = $open ? "$chunk\n```" : $chunk;
    }
    return $chunks;
}

/**
 * The Markdown the model writes, as Telegram HTML: ``` blocks, `code`, **bold**, headings, links and
 * bullets. Everything else is escaped, so the result is always valid HTML for Telegram.
 */
/** HTML-escapes text; invalid UTF-8 is replaced instead of blanking the whole string. */
function h($text, $flags = ENT_NOQUOTES)
{
    return htmlspecialchars($text, $flags | ENT_SUBSTITUTE, 'UTF-8');
}

function markdown_to_telegram_html($text)
{
    $out = '';
    $parts = preg_split('/^```[ \t]*([\w+#.-]*)[ \t]*\n(.*?)^```[ \t]*$/ms', $text, -1, PREG_SPLIT_DELIM_CAPTURE);
    for ($i = 0; $i < count($parts); $i += 3) {
        $out .= inline_markdown_to_html($parts[$i]);
        if (isset($parts[$i + 2])) {
            $lang = $parts[$i + 1] !== '' ? ' class="language-' . h($parts[$i + 1], ENT_QUOTES) . '"' : '';
            $out .= "<pre><code$lang>" . h(rtrim($parts[$i + 2], "\n")) . '</code></pre>';
        }
    }
    return trim($out);
}

function inline_markdown_to_html($text)
{
    // Inline code first, so nothing inside it is touched.
    $codes = [];
    $text = preg_replace_callback('/`([^`\n]+)`/', function ($m) use (&$codes) {
        $codes[] = '<code>' . h($m[1]) . '</code>';
        return "\x01" . (count($codes) - 1) . "\x02";
    }, $text);
    $text = h($text);
    $text = preg_replace_callback('/^#{1,6}[ \t]+(.+?)[ \t#]*$/m', function ($m) {
        return '<b>' . str_replace('**', '', $m[1]) . '</b>';
    }, $text);
    $text = preg_replace('/\*\*(?=\S)(.+?)(?<=\S)\*\*/', '<b>$1</b>', $text);
    $text = preg_replace('/^([ \t]*)[*-][ \t]+/m', '$1• ', $text);
    $text = preg_replace_callback('/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/', function ($m) {
        return '<a href="' . str_replace('"', '&quot;', $m[2]) . '">' . $m[1] . '</a>';
    }, $text);
    return preg_replace_callback("/\x01(\\d+)\x02/", function ($m) use ($codes) {
        return $codes[(int) $m[1]];
    }, $text);
}

/** Sends a reply, split into several messages if needed. Falls back to plain text if Telegram rejects the HTML. */
function tg_send_text($chatId, $text, $replyTo = null)
{
    foreach (split_message($text) as $i => $chunk) {
        $params = ['chat_id' => $chatId, 'text' => markdown_to_telegram_html($chunk), 'parse_mode' => 'HTML',
            'link_preview_options' => ['is_disabled' => true]];
        if ($replyTo && $i === 0) {
            $params['reply_parameters'] = ['message_id' => $replyTo, 'allow_sending_without_reply' => true];
        }
        $result = tg_api('sendMessage', $params);
        if (empty($result['ok'])) {
            unset($params['parse_mode']);
            $params['text'] = $chunk;
            $result = tg_api('sendMessage', $params);
            if (empty($result['ok'])) {
                bot_log('sendMessage failed: ' . (isset($result['description']) ? $result['description'] : '?'));
            }
        }
    }
}

function tg_send_photo($chatId, $data, $extension, $caption, $replyTo = null)
{
    ensure_data_dir();
    $file = tempnam(DATA_DIR, 'img');
    file_put_contents($file, $data);
    try {
        $params = ['chat_id' => $chatId, 'caption' => utf8_cut($caption, 1000),
            'photo' => new CURLFile($file, $extension === 'png' ? 'image/png' : 'image/jpeg', "image.$extension")];
        if ($replyTo) {
            $params['reply_parameters'] = json_encode(['message_id' => $replyTo, 'allow_sending_without_reply' => true]);
        }
        $result = tg_api('sendPhoto', $params, true);
        if (empty($result['ok'])) {
            bot_log('sendPhoto failed: ' . (isset($result['description']) ? $result['description'] : '?'));
            tg_send_text($chatId, 'The image was made but Telegram would not accept it. Please try again.');
        }
    } finally {
        @unlink($file);
    }
}

/** Shows "typing..." (or "sending photo...") for about five seconds. */
function tg_action($chatId, $action = 'typing')
{
    tg_api('sendChatAction', ['chat_id' => $chatId, 'action' => $action]);
}
