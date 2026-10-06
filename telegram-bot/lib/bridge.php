<?php
// Bridge mode (PERCHANCE_VIA=bridge): Perchance requests are made by the "Perchance Bridge" Chrome
// extension in your own browser instead of this server, because Perchance only accepts a key from the
// browser and IP address that verified it. Jobs wait in data/bridge/ until the extension picks them up.

define('BRIDGE_ONLINE_SECONDS', 90);    // the extension asks for work at least every ~25 seconds
define('BRIDGE_LOST_JOB_SECONDS', 150); // a started job with no result by then is offered again

function bridge_enabled()
{
    return strtolower(cfg('PERCHANCE_VIA', 'direct')) === 'bridge';
}

function bridge_dir()
{
    ensure_data_dir();
    if (!is_dir(DATA_DIR . '/bridge')) {
        @mkdir(DATA_DIR . '/bridge', 0700, true);
    }
    return DATA_DIR . '/bridge';
}

function bridge_heartbeat(array $state)
{
    @file_put_contents(bridge_dir() . '/heartbeat.json', json_encode(['time' => time(), 'state' => $state]), LOCK_EX);
}

/** Seconds since the extension last asked for work, or null if it never has. */
function bridge_last_seen()
{
    $data = json_decode((string) @file_get_contents(bridge_dir() . '/heartbeat.json'), true);
    return is_array($data) && isset($data['time']) ? time() - (int) $data['time'] : null;
}

function bridge_submit(array $payload)
{
    $id = sprintf('%d%03d%s', time(), mt_rand(0, 999), bin2hex(random_bytes(3)));
    with_json_file("bridge/job-$id.json", function (array &$job) use ($id, $payload) {
        $job = ['id' => $id, 'status' => 'pending', 'created' => time(), 'payload' => $payload];
    });
    return $id;
}

/** The oldest job waiting for the extension (left pending until the extension calls bridge_start). */
function bridge_next_job()
{
    $files = glob(bridge_dir() . '/job-*.json');
    sort($files);
    foreach ($files as $file) {
        $job = json_decode((string) @file_get_contents($file), true);
        if (!is_array($job) || !isset($job['status'])) {
            continue;
        }
        $age = time() - (int) $job['created'];
        if ($age > 3600) {
            @unlink($file);   // leftovers
        } elseif ($job['status'] === 'pending' && $age < 600
            || $job['status'] === 'started' && time() - (int) $job['started'] > BRIDGE_LOST_JOB_SECONDS) {
            return ['id' => $job['id']] + $job['payload'];
        }
    }
    return null;
}

/** Marks a job as taken; false if another tab took it or it no longer waits. */
function bridge_start($id)
{
    if (!preg_match('/^\d+[0-9a-f]+$/', $id) || !is_file(bridge_dir() . "/job-$id.json")) {
        return false;
    }
    return with_json_file("bridge/job-$id.json", function (array &$job) {
        $lost = isset($job['status']) && $job['status'] === 'started' && time() - (int) $job['started'] > BRIDGE_LOST_JOB_SECONDS;
        if (!isset($job['status']) || ($job['status'] !== 'pending' && !$lost)) {
            return false;
        }
        $job['status'] = 'started';
        $job['started'] = time();
        return true;
    });
}

function bridge_complete($id, array $result)
{
    if (!preg_match('/^\d+[0-9a-f]+$/', $id) || !is_file(bridge_dir() . "/job-$id.json")) {
        return false;
    }
    return with_json_file("bridge/job-$id.json", function (array &$job) use ($result) {
        if (!isset($job['status']) || $job['status'] === 'done') {
            return false;
        }
        $job['status'] = 'done';
        $job['result'] = $result;
        return true;
    });
}

/** Waits for the extension's answer to a job; null if none came in time. */
function bridge_wait($id, $timeout)
{
    $file = bridge_dir() . "/job-$id.json";
    $deadline = microtime(true) + $timeout;
    while (microtime(true) < $deadline) {
        $job = json_decode((string) @file_get_contents($file), true);   // a half-written file just fails to decode
        if (is_array($job) && isset($job['status']) && $job['status'] === 'done') {
            @unlink($file);
            return $job['result'];
        }
        usleep(300000);
    }
    @unlink($file);   // too late: make sure the extension doesn't run it any more
    return null;
}

/** One Perchance request, made by the extension in your browser. Same answer as perchance_request(). */
function perchance_bridge_request($instruction, $startWith, array $stop)
{
    $seen = bridge_last_seen();
    if ($seen === null || $seen > BRIDGE_ONLINE_SECONDS) {
        throw new PerchanceError('The Perchance Bridge is offline: open Chrome on the computer with the Perchance Bridge '
            . 'extension' . ($seen === null ? '.' : ' (last seen ' . round($seen / 60) . ' minutes ago).'));
    }
    $id = bridge_submit(['kind' => 'text', 'instruction' => $instruction, 'startWith' => $startWith,
        'stopSequences' => array_values($stop), 'generatorName' => cfg('PERCHANCE_GENERATOR', 'ai-code-generator')]);
    $result = bridge_wait($id, (int) cfg('BRIDGE_TIMEOUT', '180'));
    if ($result === null) {
        throw new PerchanceError('The Perchance Bridge did not answer in time. Is Chrome asleep, or the Perchance tab closed?');
    }
    if (!empty($result['error'])) {
        throw new PerchanceError('Perchance (in your browser) failed: ' . substr((string) $result['error'], 0, 300));
    }
    return ['text' => isset($result['text']) ? (string) $result['text'] : '',
        'stop_reason' => isset($result['stop_reason']) ? $result['stop_reason'] : null];
}
