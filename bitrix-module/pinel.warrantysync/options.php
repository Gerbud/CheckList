<?php

use Bitrix\Main\Loader;

defined('B_PROLOG_INCLUDED') && B_PROLOG_INCLUDED === true || die();

$moduleId = 'pinel.warrantysync';
if (!$USER->IsAdmin() || !Loader::includeModule($moduleId)) {
    return;
}

$saveMessage = '';
$saveError = '';
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
        $saveMessage = 'Настройки интеграции сохранены.';
    } else {
        $saveError = 'Webhook должен использовать защищённый адрес HTTPS.';
    }
}

$active = COption::GetOptionString($moduleId, 'active', 'N');
$secretConfigured = COption::GetOptionString($moduleId, 'secret', '') !== '';
$webhookActive = COption::GetOptionString($moduleId, 'webhook_active', 'N');
$webhookUrl = COption::GetOptionString($moduleId, 'webhook_url', '');
$apiUrl = (CMain::IsHTTPS() ? 'https://' : 'http://') . $_SERVER['HTTP_HOST'] . '/warranty-sync/';
$moduleVersion = '1.3.3';
?>
<style>
    #pinel-warranty-sync { max-width: 980px; margin-top: 18px; color: #263238; }
    #pinel-warranty-sync * { box-sizing: border-box; }
    .pinel-sync-hero { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 24px 28px; border-radius: 12px; background: linear-gradient(135deg, #17324d 0%, #245b72 100%); color: #fff; box-shadow: 0 8px 24px rgba(28, 55, 77, .18); }
    .pinel-sync-hero h2 { margin: 0 0 7px; color: #fff; font-size: 22px; }
    .pinel-sync-hero p { margin: 0; max-width: 620px; color: rgba(255,255,255,.78); font-size: 13px; line-height: 1.5; }
    .pinel-sync-version { flex: 0 0 auto; padding: 7px 11px; border: 1px solid rgba(255,255,255,.22); border-radius: 20px; background: rgba(255,255,255,.1); font: 12px/1.2 monospace; }
    .pinel-sync-summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 14px 0; }
    .pinel-sync-summary-item { padding: 14px 16px; border: 1px solid #dce5ea; border-radius: 9px; background: #fff; box-shadow: 0 2px 7px rgba(40, 60, 72, .06); }
    .pinel-sync-summary-label { display: block; margin-bottom: 8px; color: #73838d; font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
    .pinel-sync-badge { display: inline-flex; align-items: center; gap: 7px; font-weight: 600; }
    .pinel-sync-dot { width: 8px; height: 8px; border-radius: 50%; background: #abb8bf; }
    .pinel-sync-badge.is-on .pinel-sync-dot { background: #32b36b; box-shadow: 0 0 0 4px rgba(50,179,107,.12); }
    .pinel-sync-card { margin-top: 14px; overflow: hidden; border: 1px solid #dce5ea; border-radius: 10px; background: #fff; box-shadow: 0 3px 12px rgba(40, 60, 72, .07); }
    .pinel-sync-card-head { padding: 17px 20px; border-bottom: 1px solid #e7edf0; background: #f8fafb; }
    .pinel-sync-card-head h3 { margin: 0 0 4px; font-size: 16px; color: #263238; }
    .pinel-sync-card-head p { margin: 0; color: #73838d; font-size: 12px; }
    .pinel-sync-fields { padding: 4px 20px; }
    .pinel-sync-field { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 22px; align-items: center; padding: 17px 0; border-bottom: 1px solid #edf1f3; }
    .pinel-sync-field:last-child { border-bottom: 0; }
    .pinel-sync-field-label strong { display: block; margin-bottom: 4px; font-size: 13px; }
    .pinel-sync-field-label span, .pinel-sync-hint { color: #7a8992; font-size: 12px; line-height: 1.45; }
    .pinel-sync-input { width: 100%; height: 38px; padding: 0 11px; border: 1px solid #b9c7ce; border-radius: 6px; background: #fff; box-shadow: inset 0 1px 2px rgba(30,50,60,.05); }
    .pinel-sync-input:focus { border-color: #2f91ca; outline: 0; box-shadow: 0 0 0 3px rgba(47,145,202,.12); }
    .pinel-sync-secret-state { margin-top: 7px; color: <?=$secretConfigured ? '#278a54' : '#b06a00'?>; font-size: 12px; }
    .pinel-sync-switch { display: inline-flex; align-items: center; gap: 10px; cursor: pointer; font-weight: 600; }
    .pinel-sync-switch input { position: absolute; opacity: 0; }
    .pinel-sync-switch-ui { position: relative; width: 42px; height: 23px; border-radius: 15px; background: #b8c4ca; transition: .2s; }
    .pinel-sync-switch-ui:after { content: ''; position: absolute; top: 3px; left: 3px; width: 17px; height: 17px; border-radius: 50%; background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.22); transition: .2s; }
    .pinel-sync-switch input:checked + .pinel-sync-switch-ui { background: #36a66a; }
    .pinel-sync-switch input:checked + .pinel-sync-switch-ui:after { transform: translateX(19px); }
    .pinel-sync-endpoint { display: flex; align-items: center; gap: 8px; }
    .pinel-sync-endpoint code { flex: 1; overflow: auto; padding: 10px 12px; border: 1px solid #d7e0e5; border-radius: 6px; background: #f5f8f9; color: #37505e; white-space: nowrap; }
    .pinel-sync-copy { padding: 8px 11px; border: 1px solid #c5d1d7; border-radius: 6px; background: #fff; color: #47616f; cursor: pointer; }
    .pinel-sync-copy:hover { background: #eef5f8; }
    .pinel-sync-actions { display: flex; align-items: center; gap: 14px; padding: 18px 20px; border-top: 1px solid #e7edf0; background: #f8fafb; }
    .pinel-sync-actions .adm-btn-save { min-width: 150px; }
    @media (max-width: 800px) { .pinel-sync-summary { grid-template-columns: 1fr; } .pinel-sync-field { grid-template-columns: 1fr; gap: 9px; } .pinel-sync-hero { align-items: flex-start; flex-direction: column; } }
</style>

<div id="pinel-warranty-sync">
    <?php if ($saveMessage !== ''): ?>
        <?php CAdminMessage::ShowMessage(array('MESSAGE' => $saveMessage, 'TYPE' => 'OK')); ?>
    <?php elseif ($saveError !== ''): ?>
        <?php CAdminMessage::ShowMessage(array('MESSAGE' => $saveError, 'TYPE' => 'ERROR')); ?>
    <?php endif; ?>

    <div class="pinel-sync-hero">
        <div>
            <h2>Обмен гарантийными обращениями</h2>
            <p>Защищённая синхронизация рекламаций между pinel.ru, Store Checklist и рабочими темами Telegram.</p>
        </div>
        <span class="pinel-sync-version">v<?=htmlspecialcharsbx($moduleVersion)?></span>
    </div>

    <div class="pinel-sync-summary">
        <div class="pinel-sync-summary-item">
            <span class="pinel-sync-summary-label">Bitrix API</span>
            <span class="pinel-sync-badge <?=$active === 'Y' ? 'is-on' : ''?>"><i class="pinel-sync-dot"></i><?=$active === 'Y' ? 'Включён' : 'Выключен'?></span>
        </div>
        <div class="pinel-sync-summary-item">
            <span class="pinel-sync-summary-label">Webhook</span>
            <span class="pinel-sync-badge <?=$webhookActive === 'Y' ? 'is-on' : ''?>"><i class="pinel-sync-dot"></i><?=$webhookActive === 'Y' ? 'Отправляется' : 'Выключен'?></span>
        </div>
        <div class="pinel-sync-summary-item">
            <span class="pinel-sync-summary-label">HMAC-защита</span>
            <span class="pinel-sync-badge <?=$secretConfigured ? 'is-on' : ''?>"><i class="pinel-sync-dot"></i><?=$secretConfigured ? 'Настроена' : 'Не настроена'?></span>
        </div>
    </div>

    <form method="post">
        <?=bitrix_sessid_post()?>
        <div class="pinel-sync-card">
            <div class="pinel-sync-card-head">
                <h3>Подключение</h3>
                <p>Основные параметры доступа к API и подписи запросов.</p>
            </div>
            <div class="pinel-sync-fields">
                <div class="pinel-sync-field">
                    <div class="pinel-sync-field-label"><strong>Bitrix API</strong><span>Разрешает Store Checklist читать и обновлять рекламации.</span></div>
                    <label class="pinel-sync-switch"><input type="checkbox" name="active" value="Y" <?=$active === 'Y' ? 'checked' : ''?>><span class="pinel-sync-switch-ui"></span><span>Включить API</span></label>
                </div>
                <div class="pinel-sync-field">
                    <div class="pinel-sync-field-label"><strong>Общий HMAC-секрет</strong><span>Используется для подписи запросов в обе стороны.</span></div>
                    <div>
                        <input class="pinel-sync-input" type="password" name="secret" autocomplete="new-password" placeholder="Оставьте пустым, чтобы не менять">
                        <div class="pinel-sync-secret-state"><?=$secretConfigured ? '✓ Секрет настроен и скрыт.' : '⚠ Нужна случайная строка длиной не менее 32 символов.'?></div>
                    </div>
                </div>
            </div>
        </div>

        <div class="pinel-sync-card">
            <div class="pinel-sync-card-head">
                <h3>Мгновенные уведомления</h3>
                <p>Bitrix сообщает проекту о создании и изменении рекламации сразу после сохранения.</p>
            </div>
            <div class="pinel-sync-fields">
                <div class="pinel-sync-field">
                    <div class="pinel-sync-field-label"><strong>Webhook</strong><span>Оставьте включённым для обновления статусов без ожидания cron.</span></div>
                    <label class="pinel-sync-switch"><input type="checkbox" name="webhook_active" value="Y" <?=$webhookActive === 'Y' ? 'checked' : ''?>><span class="pinel-sync-switch-ui"></span><span>Отправлять изменения</span></label>
                </div>
                <div class="pinel-sync-field">
                    <div class="pinel-sync-field-label"><strong>Адрес Store Checklist</strong><span>Только HTTPS. Секрет в URL не передаётся.</span></div>
                    <input class="pinel-sync-input" type="url" name="webhook_url" value="<?=htmlspecialcharsbx($webhookUrl)?>" placeholder="https://checklist.es-helper.ru/warranty/bitrix/webhook/">
                </div>
            </div>
        </div>

        <div class="pinel-sync-card">
            <div class="pinel-sync-card-head"><h3>Служебные адреса</h3><p>Готовые endpoint’ы текущей конфигурации.</p></div>
            <div class="pinel-sync-fields">
                <div class="pinel-sync-field">
                    <div class="pinel-sync-field-label"><strong>API модуля</strong><span>Принимает подписанные запросы Store Checklist.</span></div>
                    <div class="pinel-sync-endpoint"><code id="pinel-api-url"><?=htmlspecialcharsbx($apiUrl)?></code><button class="pinel-sync-copy" type="button" data-copy="pinel-api-url">Копировать</button></div>
                </div>
                <div class="pinel-sync-field">
                    <div class="pinel-sync-field-label"><strong>Webhook проекта</strong><span>Получает события от этого модуля.</span></div>
                    <div class="pinel-sync-endpoint"><code id="pinel-webhook-url"><?=htmlspecialcharsbx($webhookUrl !== '' ? $webhookUrl : 'Не настроен')?></code><button class="pinel-sync-copy" type="button" data-copy="pinel-webhook-url">Копировать</button></div>
                </div>
            </div>
            <div class="pinel-sync-actions"><input type="submit" class="adm-btn-save" value="Сохранить настройки"><span class="pinel-sync-hint">Изменение секрета сразу влияет на обмен в обе стороны.</span></div>
        </div>
    </form>
</div>
<script>
    (function () {
        var buttons = document.querySelectorAll('#pinel-warranty-sync [data-copy]');
        for (var i = 0; i < buttons.length; i++) {
            buttons[i].onclick = function () {
                var source = document.getElementById(this.getAttribute('data-copy'));
                if (!source || !navigator.clipboard) return;
                navigator.clipboard.writeText(source.textContent || source.innerText);
                var button = this;
                var oldText = button.innerText;
                button.innerText = 'Скопировано';
                setTimeout(function () { button.innerText = oldText; }, 1200);
            };
        }
    })();
</script>
