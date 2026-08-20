<?php

namespace Pinel\WarrantySync;

use Bitrix\Main\Event;

final class Notifier
{
    private const MODULE_ID = 'pinel.warrantysync';

    public static function handleWarrantyEvent(Event $event)
    {
        $id = $event->getParameter('id');
        if (is_array($id)) {
            $id = current($id);
        }
        $eventName = substr((string)$event->getEventType(), -10) === 'OnAfterAdd'
            ? 'claim.added'
            : 'claim.updated';
        self::notify($eventName, (int)$id);
    }

    public static function notify($eventName, $claimId)
    {
        if (\COption::GetOptionString(self::MODULE_ID, 'webhook_active', 'N') !== 'Y') {
            return false;
        }
        $url = trim(\COption::GetOptionString(self::MODULE_ID, 'webhook_url', ''));
        $secret = \COption::GetOptionString(self::MODULE_ID, 'secret', '');
        if (!preg_match('~^https://~i', $url) || strlen($secret) < 32 || (int)$claimId < 1) {
            self::log('Webhook не настроен полностью.');
            return false;
        }
        $body = json_encode(array(
            'event' => (string)$eventName,
            'claimId' => (int)$claimId,
            'eventId' => (string)$eventName . ':' . (int)$claimId . ':' . str_replace('.', '', uniqid('', true)),
        ), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        $timestamp = (string)time();
        $signature = hash_hmac('sha256', $timestamp . '.' . $body, $secret);
        $http = new \Bitrix\Main\Web\HttpClient(array(
            'socketTimeout' => 5,
            'streamTimeout' => 25,
            'redirect' => false,
        ));
        $http->setHeader('Content-Type', 'application/json');
        $http->setHeader('X-Warranty-Timestamp', $timestamp);
        $http->setHeader('X-Warranty-Signature', $signature);
        try {
            $response = $http->post($url, $body);
            $status = (int)$http->getStatus();
            if ($status < 200 || $status >= 300) {
                self::log('Webhook HTTP ' . $status . ': ' . substr((string)$response, 0, 500));
                return false;
            }
        } catch (\Exception $exception) {
            self::log($exception->getMessage());
            return false;
        }
        return true;
    }

    private static function log($message)
    {
        \AddMessage2Log((string)$message, self::MODULE_ID);
    }
}
