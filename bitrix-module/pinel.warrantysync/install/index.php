<?php

use Bitrix\Main\ModuleManager;

class pinel_warrantysync extends CModule
{
    public $MODULE_ID = 'pinel.warrantysync';
    public $MODULE_VERSION;
    public $MODULE_VERSION_DATE;
    public $MODULE_NAME = 'Pinel: обмен гарантийными обращениями';
    public $MODULE_DESCRIPTION = 'Защищённая двусторонняя синхронизация таблиц warranty и warranty_log с рабочим сайтом.';
    public $PARTNER_NAME = 'Pinel';
    public $PARTNER_URI = 'https://pinel.ru';

    public function __construct()
    {
        $version = array();
        include __DIR__ . '/version.php';
        $this->MODULE_VERSION = $arModuleVersion['VERSION'];
        $this->MODULE_VERSION_DATE = $arModuleVersion['VERSION_DATE'];
    }

    public function DoInstall()
    {
        ModuleManager::registerModule($this->MODULE_ID);
        $this->InstallFiles();
    }

    public function DoUninstall()
    {
        $this->UnInstallFiles();
        COption::RemoveOption($this->MODULE_ID);
        ModuleManager::unRegisterModule($this->MODULE_ID);
    }

    public function InstallFiles()
    {
        CopyDirFiles(
            __DIR__ . '/public',
            $_SERVER['DOCUMENT_ROOT'] . '/warranty-sync',
            true,
            true
        );
        return true;
    }

    public function UnInstallFiles()
    {
        DeleteDirFilesEx('/warranty-sync');
        return true;
    }
}
