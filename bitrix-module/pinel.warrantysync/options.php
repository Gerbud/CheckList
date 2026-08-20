<?php

use Bitrix\Main\Loader;

defined('B_PROLOG_INCLUDED') && B_PROLOG_INCLUDED === true || die();

$moduleId = 'pinel.warrantysync';
if (!$USER->IsAdmin() || !Loader::includeModule($moduleId)) {
    return;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && check_bitrix_sessid()) {
    COption::SetOptionString($moduleId, 'active', isset($_POST['active']) ? 'Y' : 'N');
    $secret = trim((string)(isset($_POST['secret']) ? $_POST['secret'] : ''));
    if ($secret !== '') {
        COption::SetOptionString($moduleId, 'secret', $secret);
    }
    COption::SetOptionString($moduleId, 'webhook_active', isset($_POST['webhook_active']) ? 'Y' : 'N');
    $webhookUrl = trim((string)(isset($_POST['webhook_url']) ? $_POST['webhook_url'] : ''));
    if ($webhookUrl === '' || preg_match('~^https://~i', $webhookUrl)) {
        COption::SetOptionString($moduleId, 'webhook_url', $webhookUrl);
    }
}

$active = COption::GetOptionString($moduleId, 'active', 'N');
$secretConfigured = COption::GetOptionString($moduleId, 'secret', '') !== '';
$webhookActive = COption::GetOptionString($moduleId, 'webhook_active', 'N');
$webhookUrl = COption::GetOptionString($moduleId, 'webhook_url', '');
?>
<form method="post">
    <?=bitrix_sessid_post()?>
    <table class="adm-detail-content-table edit-table">
        <tr>
            <td width="40%">Включить API:</td>
            <td><input type="checkbox" name="active" value="Y" <?=$active === 'Y' ? 'checked' : ''?>></td>
        </tr>
        <tr>
            <td>Общий секрет:</td>
            <td>
                <input type="password" name="secret" size="60" autocomplete="new-password">
                <div class="adm-info-message-wrap"><div class="adm-info-message">
                    <?=$secretConfigured ? 'Секрет настроен. Оставьте поле пустым, чтобы не менять его.' : 'Укажите случайную строку длиной не менее 32 символов.'?>
                </div></div>
            </td>
        </tr>
        <tr>
            <td>Отправлять webhook:</td>
            <td><input type="checkbox" name="webhook_active" value="Y" <?=$webhookActive === 'Y' ? 'checked' : ''?>></td>
        </tr>
        <tr>
            <td>Адрес webhook Django:</td>
            <td><input type="url" name="webhook_url" size="80" value="<?=htmlspecialcharsbx($webhookUrl)?>" placeholder="https://checklist.es-helper.ru/warranty/bitrix/webhook/"></td>
        </tr>
        <tr>
            <td>Адрес API:</td>
            <td><code><?=htmlspecialcharsbx((CMain::IsHTTPS() ? 'https://' : 'http://') . $_SERVER['HTTP_HOST'] . '/warranty-sync/')?> </code></td>
        </tr>
    </table>
    <input type="submit" class="adm-btn-save" value="Сохранить">
</form>
