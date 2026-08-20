<?php
// Read-only exporter. It only opens a DB connection and runs SELECT queries.
$settings = include '/var/www/pinel_ru_usr/data/www/pinel.ru/bitrix/.settings.php';
$connection = $settings['connections']['value']['default'];
$db = new mysqli($connection['host'], $connection['login'], $connection['password'], $connection['database']);
if ($db->connect_errno) {
    fwrite(STDERR, "Bitrix DB connection failed\n");
    exit(2);
}
$db->set_charset('utf8mb4');

function rows(mysqli $db, string $query): array {
    $result = $db->query($query);
    if (!$result) {
        fwrite(STDERR, "Read-only export query failed\n");
        exit(3);
    }
    return $result->fetch_all(MYSQLI_ASSOC);
}

$claims = rows($db, 'SELECT w.*, CONCAT_WS(" ", u.NAME, u.LAST_NAME) CREATED_BY_NAME FROM warranty w LEFT JOIN b_user u ON u.ID = w.UF_CREATE_BY ORDER BY w.ID');
$history = rows($db, 'SELECT l.*, CONCAT_WS(" ", u.NAME, u.LAST_NAME) ACTOR_NAME FROM warranty_log l LEFT JOIN b_user u ON u.ID = l.UF_USER_ID ORDER BY l.ID');
$historyByClaim = [];
foreach ($history as $event) {
    $historyByClaim[(string)$event['UF_WARRANTY_ID']][] = $event;
}

$fileIds = [];
foreach ($claims as $claim) {
    foreach (array_filter(explode('/', (string)($claim['UF_OTHER_FILES'] ?? ''))) as $fileId) {
        if (ctype_digit((string)$fileId)) $fileIds[(int)$fileId] = (int)$fileId;
    }
}
$filesById = [];
if ($fileIds) {
    $idList = implode(',', $fileIds);
    foreach (rows($db, "SELECT ID,ORIGINAL_NAME,CONTENT_TYPE,FILE_SIZE,SUBDIR,FILE_NAME FROM b_file WHERE ID IN ($idList)") as $file) {
        $file['SRC'] = '/upload/' . trim($file['SUBDIR'], '/') . '/' . $file['FILE_NAME'];
        $file['LOCAL_PATH'] = trim($file['SUBDIR'], '/') . '/' . $file['FILE_NAME'];
        $filesById[(int)$file['ID']] = $file;
    }
}

foreach ($claims as &$claim) {
    $claim['HISTORY'] = $historyByClaim[(string)$claim['ID']] ?? [];
    $claim['FILES'] = [];
    foreach (array_filter(explode('/', (string)($claim['UF_OTHER_FILES'] ?? ''))) as $fileId) {
        if (isset($filesById[(int)$fileId])) $claim['FILES'][] = $filesById[(int)$fileId];
    }
}
unset($claim);
echo json_encode(['schema' => 1, 'claims' => $claims], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);
