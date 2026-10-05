<?php
// Perchance's free text generator and text-to-image plugin, called the way their own pages do.
// Ported from perchance.py in the parent folder.

class PerchanceError extends RuntimeException
{
}

/** Perchance is still generating an earlier request for the same userKey (it runs one at a time). */
class PerchanceBusy extends PerchanceError
{
}

define('PERCHANCE_HINT', 'Refresh the Perchance values in .env: open https://perchance.org/ai-code-generator in Chrome, '
    . 'generate once, and copy userKey (Network > generate request > Payload) and the cf_clearance cookie.');
define('PERCHANCE_IMAGE_HINT', 'Refresh the image values in .env: open https://perchance.org/text-to-image-plugin in Chrome, '
    . 'generate one image, and copy userKey and adAccessCode (Network > generate request > Payload).');
define('DEFAULT_USER_AGENT', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36');

function perchance_headers($origin, $json = true)
{
    $headers = [
        'User-Agent: ' . cfg('PERCHANCE_USER_AGENT', DEFAULT_USER_AGENT),
        'Accept: */*',
        'Origin: ' . $origin,
        'Referer: ' . $origin . '/embed',
    ];
    if ($json) {
        $headers[] = 'Content-Type: text/plain;charset=UTF-8';
    }
    if (cfg('PERCHANCE_CF_CLEARANCE') !== '') {
        $headers[] = 'Cookie: cf_clearance=' . cfg('PERCHANCE_CF_CLEARANCE');
    }
    return $headers;
}

/** Explains a non-JSON answer; Cloudflare pages get their own message, since that's the usual cause. */
function perchance_http_problem($status, $body)
{
    if ($status === 0) {
        return "Could not reach Perchance: $body";
    }
    if (preg_match('/cloudflare|cf-chl|just a moment|attention required/i', $body)) {
        return "Cloudflare blocked the request (HTTP $status). cf_clearance only works from the IP address and "
            . 'browser it was issued to, so requests from this server may not be accepted.';
    }
    return "Perchance returned HTTP $status: " . substr($body, 0, 300);
}

function approx_tokens($text)
{
    return intdiv(strlen($text) * 2 + 6, 7);
}

function random_digits($n)
{
    $digits = (string) mt_rand(1, 9);
    while (strlen($digits) < $n) {
        $digits .= mt_rand(0, 9);
    }
    return $digits;
}

/**
 * One request to text-generation.perchance.org. The reply comes as lines like  t:"chunk"  and ends
 * with  data:{"text":"","final":true,"stopReason":"natural"}  (the final text is the last token).
 */
function perchance_request($instruction, $startWith, array $stop)
{
    $key = cfg('PERCHANCE_USER_KEY');
    if ($key === '') {
        throw new PerchanceError('PERCHANCE_USER_KEY is not set in .env.');
    }
    $query = http_build_query([
        'browserId' => cfg('PERCHANCE_BROWSER_ID'),
        'userKey' => $key,
        'thread' => 0,
        'requestId' => 'aiTextCompletion' . random_digits(17),
        '__cacheBust' => mt_rand() / mt_getrandmax(),
    ]);
    $body = json_encode([
        'instruction' => $instruction,
        'startWith' => $startWith,
        'stopSequences' => array_values($stop),
        'generatorName' => cfg('PERCHANCE_GENERATOR', 'ai-code-generator'),
        'instructionTokenCount' => approx_tokens($instruction),
        'startWithTokenCount' => approx_tokens($startWith),
    ], JSON_UNESCAPED_UNICODE);
    $base = rtrim(cfg('PERCHANCE_BASE_URL', 'https://text-generation.perchance.org'), '/');
    list($status, $raw, $headers) = http_request('POST', "$base/api/generate?$query",
        perchance_headers('https://text-generation.perchance.org'), $body, 150);
    if ($status !== 200) {
        // HTTP 203 {"status":"waiting_for_prev_request_to_finish",...}: another request is still running.
        if (strpos($raw, 'waiting_for_prev_request_to_finish') !== false) {
            throw new PerchanceBusy($raw);
        }
        throw new PerchanceError(perchance_http_problem($status, $raw));
    }
    $text = '';
    $stopReason = null;
    foreach (preg_split('/\r?\n/', $raw) as $line) {
        if ($line === '') {
            continue;
        }
        if (strncmp($line, 't:', 2) === 0) {
            $chunk = json_decode(substr($line, 2));
            if (!is_string($chunk)) {
                throw new PerchanceError('Unreadable reply from Perchance: ' . substr($line, 0, 200));
            }
            $text .= $chunk;
        } elseif (strncmp($line, 'data:', 5) === 0) {
            $event = json_decode(substr($line, 5), true);
            if (!is_array($event)) {
                throw new PerchanceError('Unreadable reply from Perchance: ' . substr($line, 0, 200));
            }
            if (!empty($event['error'])) {
                throw new PerchanceError('Perchance returned an error: ' . substr(json_encode($event), 0, 300));
            }
            if (isset($event['text']) && is_string($event['text'])) {
                $text .= $event['text'];
            }
            if (!empty($event['final'])) {
                $stopReason = isset($event['stopReason']) ? $event['stopReason'] : null;
                break;
            }
        } else {
            throw new PerchanceError(perchance_http_problem($status, $raw) . ' ' . PERCHANCE_HINT);
        }
    }
    if ($text === '' && !empty($headers['x-should-reverify'])) {
        throw new PerchanceError('Perchance wants the userKey re-verified. ' . PERCHANCE_HINT);
    }
    return ['text' => $text, 'stop_reason' => $stopReason];
}

function ends_in_stop_sequence($text, array $stop)
{
    // Perchance keeps the matched stop sequence (and at most the rest of its last token) in the text.
    foreach ($stop as $s) {
        if ($s !== '' && strpos(substr($text, -(strlen($s) + 20)), $s) !== false) {
            return true;
        }
    }
    return false;
}

/**
 * The whole reply. Perchance stops every reply after 1024 tokens with stopReason "artificial" (also
 * used when a stop sequence matches); such a reply is continued like the site's "continue" button
 * does it, with startWith set to the text so far. $onProgress() runs before each extra request.
 */
function perchance_generate($instruction, array $stop = [], $maxContinues = null, $timeBudget = 120, $onProgress = null)
{
    if ($maxContinues === null) {
        $maxContinues = (int) cfg('PERCHANCE_MAX_CONTINUES', '3');
    }
    $maxInput = (int) cfg('PERCHANCE_MAX_INPUT_CHARS', '46000');
    if (strlen($instruction) > $maxInput) {
        throw new PerchanceError('The conversation is too long for Perchance; send /new to start over.');
    }
    $deadline = time() + $timeBudget;
    $reply = '';
    for ($n = 0; $n <= $maxContinues; $n++) {
        if ($n > 0 && (strlen($instruction) + strlen($reply) > $maxInput || time() > $deadline)) {
            break;
        }
        $before = strlen($reply);
        while (true) {
            try {
                $result = perchance_request($instruction, $reply, $stop);
                break;
            } catch (PerchanceBusy $e) {
                if (time() > $deadline) {
                    throw new PerchanceError('Perchance stayed busy with an earlier request for this key.');
                }
                if ($onProgress) {
                    $onProgress();
                }
                sleep(mt_rand(2, 4));
            } catch (PerchanceError $e) {
                if ($reply === '') {
                    throw $e;
                }
                return $reply;   // keep what arrived before a continuation failed
            }
        }
        $reply .= $result['text'];
        $cut = $result['stop_reason'] === 'artificial' && !ends_in_stop_sequence($reply, $stop);
        if (!$cut || strlen($reply) === $before) {
            break;
        }
        if ($onProgress) {
            $onProgress();
        }
    }
    return $reply;
}

function looks_like_image($data)
{
    return substr($data, 0, 3) === "\xff\xd8\xff" || substr($data, 0, 8) === "\x89PNG\r\n\x1a\n"
        || (substr($data, 0, 4) === 'RIFF' && substr($data, 8, 4) === 'WEBP');
}

/**
 * One image from image-generation.perchance.org, following the site's own client: one requestId,
 * asking again while the queue is busy, then downloading the result from imageDownloadUrl.
 * Returns ['data' => bytes, 'extension' => 'jpeg', 'seed' => int].
 */
function perchance_image($prompt, $resolution = '512x512', $negativePrompt = '')
{
    $key = cfg('PERCHANCE_IMAGE_USER_KEY');
    $code = cfg('PERCHANCE_AD_ACCESS_CODE');
    if ($key === '' || $code === '') {
        throw new PerchanceError('Image generation is not set up: PERCHANCE_IMAGE_USER_KEY and PERCHANCE_AD_ACCESS_CODE are missing in .env.');
    }
    $base = rtrim(cfg('PERCHANCE_IMAGE_BASE_URL', 'https://image-generation.perchance.org'), '/');
    $origin = 'https://image-generation.perchance.org';
    $requestId = (string) (mt_rand() / mt_getrandmax());
    $body = json_encode([
        'prompt' => $prompt, 'negativePrompt' => $negativePrompt, 'seed' => -1, 'resolution' => $resolution,
        'guidanceScale' => 7, 'channel' => 'text-to-image-plugin', 'subChannel' => 'public',
        'userKey' => $key, 'adAccessCode' => $code, 'requestId' => $requestId,
    ], JSON_UNESCAPED_UNICODE);
    $deadline = time() + 120;
    while (true) {
        $query = http_build_query(['userKey' => $key, 'requestId' => $requestId, 'adAccessCode' => $code,
            '__cacheBust' => mt_rand() / mt_getrandmax()]);
        list($status, $raw) = http_request('POST', "$base/api/generate?$query", perchance_headers($origin), $body, 120);
        $result = json_decode($raw, true);
        if (!is_array($result)) {
            throw new PerchanceError(perchance_http_problem($status, $raw));
        }
        $state = isset($result['status']) ? $result['status'] : '';
        if ($state === 'success') {
            break;
        }
        // The site waits 2-4 seconds and asks again in these cases.
        if (in_array($state, ['waiting_for_prev_request_to_finish', 'network_busy'], true) && time() < $deadline) {
            sleep(mt_rand(2, 4));
            continue;
        }
        $hint = in_array($state, ['invalid_key', 'invalid_ad_access_code'], true) ? ' ' . PERCHANCE_IMAGE_HINT : '';
        throw new PerchanceError('Perchance image generation failed: ' . ($state !== '' ? $state : substr($raw, 0, 200)) . $hint);
    }
    if (!empty($result['maybeNsfw']) && !cfg_bool('PERCHANCE_IMAGE_ALLOW_NSFW')) {
        throw new PerchanceError('Perchance flagged this image as possibly not safe for work, so it was not sent.');
    }
    $url = !empty($result['imageDownloadUrl']) ? $result['imageDownloadUrl']
        : '/api/downloadTemporaryImage?imageId=' . urlencode(isset($result['imageId']) ? $result['imageId'] : '');
    if (strpos($url, 'http') !== 0) {
        $url = $base . $url;
    }
    $problem = 'no attempt made';
    for ($attempt = 0; $attempt < 3; $attempt++) {
        if ($attempt > 0) {
            sleep(2);
        }
        list($status, $data) = http_request('GET', $url, perchance_headers($origin, false), null, 60);
        if ($status === 200 && looks_like_image($data)) {
            return ['data' => $data, 'extension' => !empty($result['fileExtension']) ? $result['fileExtension'] : 'jpeg',
                'seed' => isset($result['seed']) ? $result['seed'] : null];
        }
        $problem = $status === 0 ? $data : "HTTP $status, " . strlen($data) . ' bytes';
    }
    throw new PerchanceError("The image was generated but could not be downloaded ($problem).");
}
