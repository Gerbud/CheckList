<?php

namespace Pinel\WarrantySync;

use Bitrix\Main\Application;

final class Api
{
    private const MODULE_ID = 'pinel.warrantysync';
    private const ALLOWED_FIELDS = array('UF_STATUS', 'UF_COMMENT');

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
                return array('module' => self::MODULE_ID, 'version' => '1.0.0');
            case 'claims.list':
                return self::listClaims($payload);
            case 'claims.update':
                return self::updateClaim($payload);
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
}
