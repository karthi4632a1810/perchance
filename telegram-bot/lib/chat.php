<?php
// The chat model: conversation history per chat, the prompt sent to Perchance, and the reply.

define('DEFAULT_SYSTEM_PROMPT', 'You are a helpful, friendly assistant chatting with the user on Telegram. '
    . 'Answer clearly and concisely, in the language the user writes in. Use Markdown for code.');
// Stop the model from writing the user's next turn itself.
define('CHAT_STOP', ['=== USER ===', '=== ASSISTANT']);

function conversation_file($conversationId)
{
    return 'chat-' . preg_replace('/[^A-Za-z0-9_-]/', '_', $conversationId) . '.json';
}

/** Drops the oldest messages until the history fits HISTORY_MAX_CHARS and HISTORY_MAX_MESSAGES. */
function trim_history(array $messages)
{
    $maxChars = (int) cfg('HISTORY_MAX_CHARS', '12000');
    $maxMessages = (int) cfg('HISTORY_MAX_MESSAGES', '40');
    $size = 0;
    foreach ($messages as $m) {
        $size += strlen($m['content']);
    }
    while (count($messages) > 1 && ($size > $maxChars || count($messages) > $maxMessages)) {
        $size -= strlen($messages[0]['content']);
        array_shift($messages);
    }
    return $messages;
}

/** Perchance takes one instruction, so the chat becomes a transcript (the format agent.py uses). */
function build_chat_prompt(array $messages)
{
    $parts = ["# Instructions\n" . cfg('BOT_SYSTEM_PROMPT', DEFAULT_SYSTEM_PROMPT), '# Conversation so far'];
    foreach ($messages as $m) {
        $parts[] = ($m['role'] === 'assistant' ? "=== ASSISTANT (you) ===\n" : "=== USER ===\n") . $m['content'];
    }
    $parts[] = 'Reply to the last USER message above. Write only your reply.';
    return implode("\n\n", $parts);
}

/** Removes a leading "=== ASSISTANT ===" header and anything after the model starts another turn. */
function clean_reply($text)
{
    $text = preg_replace('/^\s*=== ASSISTANT[^\n]*===[ \t]*\n/', '', $text);
    if (preg_match('/^=== (USER|ASSISTANT)/m', $text, $m, PREG_OFFSET_CAPTURE)) {
        $text = substr($text, 0, $m[0][1]);
    }
    return trim($text);
}

/**
 * Adds the user's message to the conversation, asks Perchance for the reply and stores both.
 * Runs under one lock, because Perchance only works on one request per key at a time anyway.
 */
function chat_reply($conversationId, $userText, $onProgress = null)
{
    return with_lock('perchance', 240, function () use ($conversationId, $userText, $onProgress) {
        $file = conversation_file($conversationId);
        $messages = with_json_file($file, function (array &$data) {
            return isset($data['messages']) ? $data['messages'] : [];
        });
        $messages[] = ['role' => 'user', 'content' => $userText];
        $messages = trim_history($messages);
        $reply = clean_reply(perchance_generate(build_chat_prompt($messages), CHAT_STOP, null, 150, $onProgress));
        if ($reply === '') {
            $reply = '(Perchance returned an empty reply. Please try again.)';
        }
        $messages[] = ['role' => 'assistant', 'content' => $reply];
        with_json_file($file, function (array &$data) use ($messages) {
            $data['messages'] = trim_history($messages);
            $data['updated'] = time();
        });
        return $reply;
    });
}

function chat_reset($conversationId)
{
    with_json_file(conversation_file($conversationId), function (array &$data) {
        $data = ['messages' => [], 'updated' => time()];
    });
}
