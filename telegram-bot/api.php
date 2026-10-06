<?php
// JSON chat API over the same chat model as the Telegram bot.
//
//   POST api.php   Header: X-API-Key: <API_KEY from .env>   (or Authorization: Bearer <API_KEY>)
//   Body: {"message": "Hi!", "conversation_id": "optional-id", "reset": false, "knowledge": false}
//   "knowledge": true adds your notes from knowledge/ (see knowledge/README.md).
//   Answer: {"reply": "...", "conversation_id": "..."}
// Send the conversation_id from the answer with the next message to continue the same conversation.

require __DIR__ . '/lib/bootstrap.php';
require __DIR__ . '/lib/perchance.php';
require __DIR__ . '/lib/chat.php';

function given_api_key()
{
    foreach (['HTTP_X_API_KEY', 'HTTP_AUTHORIZATION', 'REDIRECT_HTTP_AUTHORIZATION'] as $name) {
        if (!empty($_SERVER[$name])) {
            return preg_replace('/^Bearer\s+/i', '', trim($_SERVER[$name]));
        }
    }
    return '';
}

$key = cfg('API_KEY');
if ($key === '' || !hash_equals($key, given_api_key())) {
    json_out(401, ['error' => $key === '' ? 'The API is turned off (API_KEY is empty in .env).' : 'Missing or wrong API key.']);
    exit;
}
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    json_out(405, ['error' => 'Use POST with a JSON body like {"message": "Hi!"}.']);
    exit;
}
$input = json_decode((string) file_get_contents('php://input'), true);
if (!is_array($input)) {
    $input = $_POST;
}
$message = trim(isset($input['message']) ? (string) $input['message'] : '');
$conversation = isset($input['conversation_id']) ? (string) $input['conversation_id'] : bin2hex(random_bytes(8));
if (!preg_match('/^[A-Za-z0-9_-]{1,64}$/', $conversation)) {
    json_out(400, ['error' => 'conversation_id may only contain letters, digits, _ and - (at most 64).']);
    exit;
}
if (!empty($input['reset'])) {
    chat_reset("api-$conversation");
}
if ($message === '') {
    json_out(empty($input['reset']) ? 400 : 200, empty($input['reset'])
        ? ['error' => 'message is empty.'] : ['reply' => '', 'conversation_id' => $conversation]);
    exit;
}

@set_time_limit(300);
try {
    $knowledge = !empty($input['knowledge']) ? knowledge_text() : '';   // your notes, only when asked for
    json_out(200, ['reply' => chat_reply("api-$conversation", $message, null, $knowledge), 'conversation_id' => $conversation]);
} catch (Throwable $e) {
    bot_log("API $conversation: " . $e->getMessage());
    json_out(502, ['error' => $e->getMessage(), 'conversation_id' => $conversation]);
}
