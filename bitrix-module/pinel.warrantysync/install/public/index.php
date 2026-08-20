<?php

define('NO_KEEP_STATISTIC', true);
define('NO_AGENT_STATISTIC', true);
define('DisableEventsCheck', true);
require $_SERVER['DOCUMENT_ROOT'] . '/bitrix/modules/main/include/prolog_before.php';

use Bitrix\Main\Loader;
use Pinel\WarrantySync\Api;

header('Content-Type: application/json; charset=utf-8');
try {
    if (!Loader::includeModule('pinel.warrantysync')) {
        throw new RuntimeException('Модуль синхронизации не установлен.');
    }
    $raw = file_get_contents('php://input');
    $result = Api::handle($raw, $_SERVER);
    echo json_encode(array('ok' => true, 'result' => $result), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
} catch (Exception $exception) {
    http_response_code($exception instanceof InvalidArgumentException ? 400 : 500);
    echo json_encode(array('ok' => false, 'error' => $exception->getMessage()), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
}
