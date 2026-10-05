<?php
// Shared setup: settings from .env, logging, HTTP, and small JSON files under data/.

define('BOT_DIR', dirname(__DIR__));
define('DATA_DIR', BOT_DIR . '/data');

function env_values()
{
    static $values = null;
    if ($values !== null) {
        return $values;
    }
    $values = [];
    $file = BOT_DIR . '/.env';
    if (is_readable($file)) {
        foreach (file($file, FILE_IGNORE_NEW_LINES) as $line) {
            $line = trim($line);
            if ($line === '' || $line[0] === '#' || strpos($line, '=') === false) {
                continue;
            }
            list($key, $value) = explode('=', $line, 2);
            $values[trim($key)] = trim(trim($value), "\"'");
        }
    }
    return $values;
}

/** A setting from the environment, else from .env, else the default. */
function cfg($key, $default = '')
{
    $env = getenv($key);
    if ($env !== false && $env !== '') {
        return $env;
    }
    $values = env_values();
    return isset($values[$key]) && $values[$key] !== '' ? $values[$key] : $default;
}

function cfg_bool($key, $default = false)
{
    return in_array(strtolower(cfg($key, $default ? 'true' : 'false')), ['1', 'true', 'yes', 'on'], true);
}

function ensure_data_dir()
{
    if (!is_dir(DATA_DIR)) {
        @mkdir(DATA_DIR, 0700, true);
    }
    // In case the folder was uploaded without its .htaccess: chat histories must never be public.
    if (!file_exists(DATA_DIR . '/.htaccess')) {
        @file_put_contents(DATA_DIR . '/.htaccess', "<IfModule mod_authz_core.c>\n    Require all denied\n</IfModule>\n"
            . "<IfModule !mod_authz_core.c>\n    Order allow,deny\n    Deny from all\n</IfModule>\n");
    }
}

function bot_log($message)
{
    ensure_data_dir();
    $file = DATA_DIR . '/bot.log';
    if (@filesize($file) > 1000000) {
        @rename($file, $file . '.1');
    }
    @file_put_contents($file, date('Y-m-d H:i:s') . ' ' . $message . "\n", FILE_APPEND | LOCK_EX);
}

/**
 * Opens data/<name>, passes its decoded JSON (an array) to $fn by reference and saves the result.
 * The file stays locked meanwhile, so concurrent requests can't lose each other's changes.
 */
function with_json_file($name, callable $fn)
{
    ensure_data_dir();
    $fp = fopen(DATA_DIR . '/' . $name, 'c+');
    if (!$fp) {
        throw new RuntimeException("Cannot open data/$name (is the data folder writable?)");
    }
    flock($fp, LOCK_EX);
    $data = json_decode((string) stream_get_contents($fp), true);
    if (!is_array($data)) {
        $data = [];
    }
    try {
        $result = $fn($data);
        ftruncate($fp, 0);
        rewind($fp);
        fwrite($fp, json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
        fflush($fp);
        return $result;
    } finally {
        flock($fp, LOCK_UN);
        fclose($fp);
    }
}

/** Runs $fn while holding data/<name>.lock, waiting up to $timeout seconds for it. */
function with_lock($name, $timeout, callable $fn)
{
    ensure_data_dir();
    $fp = fopen(DATA_DIR . "/$name.lock", 'c');
    if (!$fp) {
        throw new RuntimeException("Cannot open data/$name.lock (is the data folder writable?)");
    }
    $deadline = time() + $timeout;
    while (!flock($fp, LOCK_EX | LOCK_NB)) {
        if (time() >= $deadline) {
            fclose($fp);
            throw new RuntimeException('The bot is busy with other messages; please try again in a minute.');
        }
        usleep(250000);
    }
    try {
        return $fn();
    } finally {
        flock($fp, LOCK_UN);
        fclose($fp);
    }
}

/**
 * One HTTP request with cURL. $body is a string, an array (multipart form) or null.
 * Returns [status (0 if the request failed), body or error message, lowercase response headers].
 */
function http_request($method, $url, array $headers, $body, $timeout)
{
    $responseHeaders = [];
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CONNECTTIMEOUT => 15,
        CURLOPT_TIMEOUT => $timeout,
        CURLOPT_ENCODING => '',
        CURLOPT_HEADERFUNCTION => function ($ch, $line) use (&$responseHeaders) {
            $parts = explode(':', $line, 2);
            if (count($parts) === 2) {
                $responseHeaders[strtolower(trim($parts[0]))] = trim($parts[1]);
            }
            return strlen($line);
        },
    ]);
    if ($body !== null) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
    }
    $response = curl_exec($ch);
    $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    $error = curl_error($ch);
    curl_close($ch);
    if ($response === false) {
        return [0, $error, []];
    }
    return [$status, $response, $responseHeaders];
}

/** Cuts a UTF-8 string to at most $bytes bytes without splitting a character. */
function utf8_cut($text, $bytes)
{
    if (strlen($text) <= $bytes) {
        return $text;
    }
    while ($bytes > 0 && (ord($text[$bytes]) & 0xC0) === 0x80) {
        $bytes--;
    }
    return substr($text, 0, $bytes);
}

function json_out($status, array $payload)
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
}
