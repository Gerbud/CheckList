<?php

namespace Pinel\WarrantySync;

use Bitrix\Main\Application;

final class Api
{
    private const MODULE_ID = 'pinel.warrantysync';
    private const ALLOWED_FIELDS = array('UF_STATUS', 'UF_COMMENT');
    private const CREATE_FIELDS = array('UF_FIO', 'UF_PHONE', 'UF_TYPE', 'UF_PRODUCT_NAME', 'UF_ARTICLE', 'UF_SERIAL_NUMBER', 'UF_DATE_OF_PURCHASE', 'UF_COMMENT');

    public static function handle($raw, array $server)
    {
        if (\COption::GetOptionString(self::MODULE_ID, 'active', 'N') !== 'Y') {
            throw new \RuntimeException('API синхронизации выключен.');
        }
        self::verifySignature($raw, $server);
        $request = json_decode($raw, true);
        if (!is_array($request)) {
            throw new \InvalidArgumentException('Некорректный JSON.');
        }
        $action = (string)(isset($request['action']) ? $request['action'] : '');
        $payload = isset($request['payload']) && is_array($request['payload']) ? $request['payload'] : array();
        switch ($action) {
            case 'health':
                self::assertSchema();
                return array('module' => self::MODULE_ID, 'version' => '1.2.0');
            case 'claims.list':
                return self::listClaims($payload);
            case 'claims.update':
                return self::updateClaim($payload);
            case 'claims.files.add':
                return self::addClaimFile($payload);
            case 'claims.create':
                return self::createClaim($payload);
            case 'claims.blank':
                return self::claimBlank($payload);
            default:
                throw new \InvalidArgumentException('Неизвестное действие.');
        }
    }

    private static function verifySignature($raw, array $server)
    {
        $secret = \COption::GetOptionString(self::MODULE_ID, 'secret', '');
        if (strlen($secret) < 32) {
            throw new \RuntimeException('Общий секрет не настроен.');
        }
        $timestamp = (string)(isset($server['HTTP_X_WARRANTY_TIMESTAMP']) ? $server['HTTP_X_WARRANTY_TIMESTAMP'] : '');
        $signature = (string)(isset($server['HTTP_X_WARRANTY_SIGNATURE']) ? $server['HTTP_X_WARRANTY_SIGNATURE'] : '');
        if (!ctype_digit($timestamp) || abs(time() - (int)$timestamp) > 300) {
            throw new \InvalidArgumentException('Запрос просрочен.');
        }
        $expected = hash_hmac('sha256', $timestamp . '.' . $raw, $secret);
        if (!hash_equals($expected, $signature)) {
            throw new \InvalidArgumentException('Неверная подпись запроса.');
        }
    }

    private static function connection()
    {
        return Application::getConnection();
    }

    private static function columns($table)
    {
        $columns = array();
        $result = self::connection()->query('SHOW COLUMNS FROM `' . $table . '`');
        while ($row = $result->fetch()) {
            $columns[$row['Field']] = true;
        }
        return $columns;
    }

    private static function assertSchema()
    {
        $claimColumns = self::columns('warranty');
        $historyColumns = self::columns('warranty_log');
        foreach (array('ID', 'UF_STATUS', 'UF_COMMENT') as $required) {
            if (!isset($claimColumns[$required])) {
                throw new \RuntimeException('В таблице warranty отсутствует поле ' . $required . '.');
            }
        }
        foreach (array('ID', 'UF_WARRANTY_ID') as $required) {
            if (!isset($historyColumns[$required])) {
                throw new \RuntimeException('В таблице warranty_log отсутствует поле ' . $required . '.');
            }
        }
    }

    private static function listClaims(array $payload)
    {
        self::assertSchema();
        $sinceClaimId = max(0, (int)(isset($payload['sinceClaimId']) ? $payload['sinceClaimId'] : 0));
        $sinceHistoryId = max(0, (int)(isset($payload['sinceHistoryId']) ? $payload['sinceHistoryId'] : 0));
        $limit = max(1, min(500, (int)(isset($payload['limit']) ? $payload['limit'] : 100)));
        $connection = self::connection();
        $sql = 'SELECT w.*, CONCAT_WS(" ", u.NAME, u.LAST_NAME) CREATED_BY_NAME '
            . 'FROM warranty w LEFT JOIN b_user u ON u.ID = w.UF_CREATE_BY '
            . 'WHERE w.ID > ' . $sinceClaimId
            . ' OR w.ID IN (SELECT DISTINCT UF_WARRANTY_ID FROM warranty_log WHERE ID > ' . $sinceHistoryId . ') '
            . 'ORDER BY w.ID ASC LIMIT ' . $limit;
        $claims = array();
        $ids = array();
        $result = $connection->query($sql);
        while ($row = $result->fetch()) {
            $row['HISTORY'] = array();
            $row['FILES'] = array();
            $claims[(string)$row['ID']] = $row;
            $ids[] = (int)$row['ID'];
        }
        if ($ids) {
            $history = $connection->query(
                'SELECT l.*, CONCAT_WS(" ", u.NAME, u.LAST_NAME) ACTOR_NAME FROM warranty_log l '
                . 'LEFT JOIN b_user u ON u.ID = l.UF_USER_ID WHERE l.UF_WARRANTY_ID IN (' . implode(',', $ids) . ') ORDER BY l.ID'
            );
            while ($event = $history->fetch()) {
                $key = (string)$event['UF_WARRANTY_ID'];
                if (isset($claims[$key])) {
                    $claims[$key]['HISTORY'][] = $event;
                }
            }
            foreach ($claims as &$claim) {
                foreach (array_filter(explode('/', (string)(isset($claim['UF_OTHER_FILES']) ? $claim['UF_OTHER_FILES'] : ''))) as $fileId) {
                    $file = \CFile::GetFileArray((int)$fileId);
                    if ($file) {
                        $claim['FILES'][] = array(
                            'ID' => $file['ID'], 'ORIGINAL_NAME' => $file['ORIGINAL_NAME'],
                            'CONTENT_TYPE' => $file['CONTENT_TYPE'], 'FILE_SIZE' => $file['FILE_SIZE'], 'SRC' => $file['SRC'],
                        );
                    }
                }
            }
            unset($claim);
        }
        $claimCursor = $sinceClaimId;
        foreach ($ids as $id) {
            $claimCursor = max($claimCursor, $id);
        }
        $historyCursor = (int)$connection->queryScalar('SELECT COALESCE(MAX(ID), 0) FROM warranty_log');
        return array('claims' => array_values($claims), 'claimCursor' => $claimCursor, 'historyCursor' => $historyCursor);
    }

    private static function updateClaim(array $payload)
    {
        self::assertSchema();
        $id = (int)(isset($payload['id']) ? $payload['id'] : 0);
        $fields = isset($payload['fields']) && is_array($payload['fields']) ? $payload['fields'] : array();
        if ($id < 1 || !$fields) {
            throw new \InvalidArgumentException('Нужны id и fields.');
        }
        $helper = self::connection()->getSqlHelper();
        $sets = array();
        foreach (self::ALLOWED_FIELDS as $field) {
            if (array_key_exists($field, $fields)) {
                $sets[] = '`' . $field . '`=\'' . $helper->forSql((string)$fields[$field]) . '\'';
            }
        }
        if (!$sets) {
            throw new \InvalidArgumentException('Нет разрешённых для изменения полей.');
        }
        $connection = self::connection();
        $connection->startTransaction();
        try {
            $connection->queryExecute('UPDATE warranty SET ' . implode(',', $sets) . ' WHERE ID=' . $id);
            $columns = self::columns('warranty_log');
            $insert = array('UF_WARRANTY_ID' => (string)$id);
            if (isset($columns['UF_CHANGES'])) {
                $insert['UF_CHANGES'] = 'Изменено через модуль синхронизации: ' . implode(', ', array_keys($fields));
            }
            if (isset($columns['UF_DATE'])) {
                $insert['UF_DATE'] = date('Y-m-d H:i:s');
            }
            if (isset($columns['UF_USER_ID'])) {
                $insert['UF_USER_ID'] = '0';
            }
            $names = array();
            $values = array();
            foreach ($insert as $name => $value) {
                $names[] = '`' . $name . '`';
                $values[] = '\'' . $helper->forSql($value) . '\'';
            }
            $connection->queryExecute('INSERT INTO warranty_log (' . implode(',', $names) . ') VALUES (' . implode(',', $values) . ')');
            $connection->commitTransaction();
        } catch (\Exception $exception) {
            $connection->rollbackTransaction();
            throw $exception;
        }
        return array('id' => $id, 'updated' => array_keys($fields));
    }

    private static function addClaimFile(array $payload)
    {
        self::assertSchema();
        $id = (int)(isset($payload['id']) ? $payload['id'] : 0);
        $file = isset($payload['file']) && is_array($payload['file']) ? $payload['file'] : array();
        $name = basename((string)(isset($file['name']) ? $file['name'] : ''));
        $contentType = (string)(isset($file['contentType']) ? $file['contentType'] : 'application/octet-stream');
        $encoded = (string)(isset($file['contentBase64']) ? $file['contentBase64'] : '');
        $checksum = strtolower((string)(isset($file['checksumSha256']) ? $file['checksumSha256'] : ''));
        if ($id < 1 || $name === '' || !preg_match('/^[a-f0-9]{64}$/', $checksum)) {
            throw new \InvalidArgumentException('Некорректные параметры файла.');
        }
        $content = base64_decode($encoded, true);
        if ($content === false || strlen($content) > 20 * 1024 * 1024 || hash('sha256', $content) !== $checksum) {
            throw new \InvalidArgumentException('Некорректное содержимое файла.');
        }
        if ($contentType === '' || $contentType === 'application/octet-stream') {
            $detectedType = '';
            if (class_exists('finfo')) {
                $finfo = new \finfo(FILEINFO_MIME_TYPE);
                $detectedType = (string)$finfo->buffer($content);
            }
            if ($detectedType !== '' && $detectedType !== 'application/octet-stream') {
                $contentType = $detectedType;
            }
        }
        $connection = self::connection();
        $row = $connection->query('SELECT UF_OTHER_FILES FROM warranty WHERE ID=' . $id)->fetch();
        if (!$row) {
            throw new \InvalidArgumentException('Обращение не найдено.');
        }
        $fileIds = array_values(array_filter(explode('/', (string)$row['UF_OTHER_FILES'])));
        foreach ($fileIds as $fileId) {
            $existing = \CFile::GetFileArray((int)$fileId);
            if (!$existing || (int)$existing['FILE_SIZE'] !== strlen($content)) {
                continue;
            }
            $path = $_SERVER['DOCUMENT_ROOT'] . (string)$existing['SRC'];
            if (is_file($path) && hash_file('sha256', $path) === $checksum) {
                return array('id' => $id, 'fileId' => (int)$fileId, 'duplicate' => true);
            }
        }
        $tmp = tempnam(sys_get_temp_dir(), 'warranty-sync-');
        if ($tmp === false || file_put_contents($tmp, $content) === false) {
            throw new \RuntimeException('Не удалось подготовить файл к сохранению.');
        }
        try {
            $fileId = (int)\CFile::SaveFile(array(
                'name' => $name,
                'type' => $contentType,
                'tmp_name' => $tmp,
                'size' => strlen($content),
                'MODULE_ID' => self::MODULE_ID,
            ), 'warranty');
        } finally {
            if (is_file($tmp)) {
                unlink($tmp);
            }
        }
        if ($fileId < 1) {
            throw new \RuntimeException('Bitrix не сохранил файл.');
        }
        $fileIds[] = (string)$fileId;
        $helper = $connection->getSqlHelper();
        $connection->queryExecute(
            "UPDATE warranty SET UF_OTHER_FILES='" . $helper->forSql(implode('/', $fileIds)) . "' WHERE ID=" . $id
        );
        return array('id' => $id, 'fileId' => $fileId, 'duplicate' => false);
    }

    private static function createClaim(array $payload)
    {
        self::assertSchema();
        $fields = isset($payload['fields']) && is_array($payload['fields']) ? $payload['fields'] : array();
        foreach (array('UF_FIO', 'UF_PHONE', 'UF_PRODUCT_NAME', 'UF_SERIAL_NUMBER', 'UF_DATE_OF_PURCHASE') as $required) {
            if (trim((string)(isset($fields[$required]) ? $fields[$required] : '')) === '') {
                throw new \InvalidArgumentException('Не заполнено обязательное поле ' . $required . '.');
            }
        }
        if (!preg_match('/^\+?[0-9]{10,15}$/', (string)$fields['UF_PHONE'])) {
            throw new \InvalidArgumentException('Некорректный телефон.');
        }
        $columns = self::columns('warranty');
        $helper = self::connection()->getSqlHelper();
        $insert = array('UF_STATUS' => '1', 'UF_CREATE_BY' => '0', 'UF_USER_ID' => '0');
        foreach (self::CREATE_FIELDS as $field) {
            if (isset($columns[$field]) && array_key_exists($field, $fields)) {
                $insert[$field] = trim((string)$fields[$field]);
            }
        }
        if (isset($columns['UF_CREATE_DATE'])) {
            $insert['UF_CREATE_DATE'] = date('Y-m-d H:i:s');
        }
        $names = array(); $values = array();
        foreach ($insert as $name => $value) {
            if (!isset($columns[$name])) continue;
            $names[] = '`' . $name . '`';
            $values[] = "'" . $helper->forSql($value) . "'";
        }
        $connection = self::connection();
        $connection->queryExecute('INSERT INTO warranty (' . implode(',', $names) . ') VALUES (' . implode(',', $values) . ')');
        $id = (int)$connection->getInsertedId();
        if ($id < 1) throw new \RuntimeException('Bitrix не создал обращение.');
        return array('id' => $id);
    }

    private static function claimBlank(array $payload)
    {
        $id = (int)(isset($payload['id']) ? $payload['id'] : 0);
        if ($id < 1) throw new \InvalidArgumentException('Некорректный ID обращения.');
        $path = \Autobud\Helpers\Warranty::getInstance()->createFile($id);
        if (!$path) throw new \RuntimeException('Не удалось сформировать PDF-бланк.');
        $relative = str_replace(app()->getDocumentRoot(), '', $path);
        return array('id' => $id, 'url' => 'https://pinel.ru' . $relative);
    }
}
