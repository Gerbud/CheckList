<?php

use Bitrix\Main\Loader;

Loader::registerAutoLoadClasses('pinel.warrantysync', array(
    'Pinel\\WarrantySync\\Api' => 'lib/api.php',
    'Pinel\\WarrantySync\\Notifier' => 'lib/notifier.php',
));
