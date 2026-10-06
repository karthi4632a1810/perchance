<?php
// Endpoint for the Perchance Bridge Chrome extension (see ../perchance-bridge-extension/).
// The extension asks here for work, runs it with Perchance in your browser, and sends the result back.
//   POST bridge.php?action=poll    {"wait": 20}            -> {"job": {...} or null}   (waits up to 25 s)
//   POST bridge.php?action=start   {"id": "..."}           -> {"ok": true/false}
//   POST bridge.php?action=result  {"id": "...", "text": "...", "stop_reason": "..."}  or  {"id": "...", "error": "..."}
// Every request needs the header  X-Bridge-Key: <BRIDGE_KEY from .env>.

require __DIR__ . '/lib/bootstrap.php';
require __DIR__ . '/lib/perchance.php';

$key = cfg('BRIDGE_KEY');
$given = isset($_SERVER['HTTP_X_BRIDGE_KEY']) ? $_SERVER['HTTP_X_BRIDGE_KEY'] : '';
if ($key === '' || !hash_equals($key, $given)) {
    json_out(401, ['error' => $key === '' ? 'BRIDGE_KEY is not set in .env.' : 'Missing or wrong bridge key.']);
    exit;
}
$input = json_decode((string) file_get_contents('php://input'), true);
if (!is_array($input)) {
    $input = [];
}
$action = isset($_GET['action']) ? $_GET['action'] : '';

if ($action === 'poll') {
    @set_time_limit(60);
    $state = isset($input['state']) && is_array($input['state']) ? $input['state'] : [];
    $deadline = microtime(true) + min(25, max(0, isset($input['wait']) ? (int) $input['wait'] : 20));
    bridge_heartbeat($state);
    while (true) {
        $job = bridge_next_job();
        if ($job || microtime(true) >= $deadline) {
            break;
        }
        usleep(500000);
    }
    json_out(200, ['job' => $job]);
} elseif ($action === 'start') {
    json_out(200, ['ok' => bridge_start(isset($input['id']) ? (string) $input['id'] : '')]);
} elseif ($action === 'result') {
    $id = isset($input['id']) ? (string) $input['id'] : '';
    unset($input['id']);
    json_out(200, ['ok' => bridge_complete($id, $input)]);
} else {
    json_out(400, ['error' => 'Unknown action; use poll, start or result.']);
}
