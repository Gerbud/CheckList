<?php if (!defined("B_PROLOG_INCLUDED") || B_PROLOG_INCLUDED !== true) die();

/** @var array $arParams */
/** @var array $arResult */
/** @global CMain $APPLICATION */
/** @global CUser $USER */
/** @global CDatabase $DB */
/** @var CBitrixComponentTemplate $this */
/** @var string $templateName */
/** @var string $templateFile */
/** @var string $templateFolder */
/** @var string $componentPath */
/** @var CBitrixComponent $component */

$request = Bitrix\Main\Context::getCurrent()->getRequest();
?>
<div class="warranty-list_container">
    <?php if (!empty($arResult["ITEMS"]) || !empty($arParams["FILTER"])) { ?>
        <div class="warranty-list_filter">
            <form action="<?= \Autobud\Paths::WARRANTY_CLAIMS_LIST?>" method="get" class="warranty-list_filter_form warranty-list_item_form-js">
                <div class="warranty-list_filter_form_input_block">
                    <input
                        type="text"
                        class="warranty-list_filter_form_input warranty-list_filter_form_input-js"
                        name="search_str"
                        placeholder="Номер обращения / телефон / товар"
                        aria-label="Номер обращения / телефон / товар"
                        value="<?= $request->get('search_str') ?>"
                    />
                    <button type="submit" class="base_btn warranty-list_filter_form_submit" aria-label="->">-></button>
                </div>
                <?php if (!empty($arResult["TYPE_OPTIONS"])) { ?>
                <select
                    class="warranty-list_filter_form_select warranty-list_item_filter_select-js"
                    name="type"
                >
                    <?php foreach ($arResult["TYPE_OPTIONS"] as $selectItem) { ?>
                        <option value="<?= $selectItem["VALUE"] ?>" <?= $selectItem["SELECTED"] ?>><?= $selectItem["NAME"] ?></option>
                    <?php } ?>
                </select>
                <?php } ?>
                <?php if (!empty($arResult["STATUSES_OPTIONS"])) { ?>
                    <select
                        class="warranty-list_filter_form_select warranty-list_item_filter_select-js"
                        name="status"
                    >
                        <?php foreach ($arResult["STATUSES_OPTIONS"] as $selectItem) { ?>
                            <option value="<?= $selectItem["VALUE"] ?>" <?= $selectItem["SELECTED"] ?>><?= $selectItem["NAME"] ?></option>
                        <?php } ?>
                    </select>
                <?php } ?>
            </form>
            <div class="warranty-list_sort_block">
                <?php if (!empty($arResult["ELEMENTS_COUNT"])) { ?>
                    <select
                        class="warranty-list_filter_form_select warranty-list_sort_select-js"
                        name="type"
                        data-key="warranty-list-count"
                    >
                        <?php foreach ($arResult["ELEMENTS_COUNT"] as $selectItem) { ?>
                            <option value="<?= $selectItem["VALUE"] ?>" <?= $selectItem["SELECTED"] ?>><?= $selectItem["NAME"] ?></option>
                        <?php } ?>
                    </select>
                <?php } ?>
                <?php if (!empty($arResult["SORT_OPTIONS"])) { ?>
                    <select
                        class="warranty-list_filter_form_select warranty-list_sort_select-js"
                        name="sort"
                        data-key="warranty-list-sort"
                    >
                        <?php foreach ($arResult["SORT_OPTIONS"] as $selectItem) { ?>
                            <option value="<?= $selectItem["VALUE"] ?>" <?= $selectItem["SELECTED"] ?>><?= $selectItem["NAME"] ?></option>
                        <?php } ?>
                    </select>
                <?php } ?>
            </div>
        </div>
    <?php } ?>
    <?php if (!empty($arResult["ITEMS"])) { ?>
        <?php foreach ($arResult["ITEMS"] as $arItem) {
            $preProductName = "";
            $postProductName = "";
            if (!empty($arItem["PRODUCT_DETAIL_PAGE"])) {
                $preProductName = "<a href='{$arItem["PRODUCT_DETAIL_PAGE"]}' target='_blank'>";
                $postProductName = "</a>";
            }
            ?>
            <div class="warranty-list_item warranty-list_item_js" data-id="<?= $arItem["ID"] ?>">
                <div class="warranty-list_block_title warranty-list_item_title_js">
                    <div class="warranty-list_block_title_date"><?= $arItem["UF_CREATE_DATE"]->format("Y-m-d H:i"); ?></div>
                    <div class="warranty-list_block_title_id">Номер заявки: <?= $arItem["ID"] ?></div>
                    <div class="warranty-list_block_title_phone"><?= $arItem["UF_PHONE"] ?></div>
                    <div class="warranty-list_block_title_status status_js"><?= $arItem["STATUS_NAME"] ?></div>
                </div>
                <div class="warranty-list_block_data warranty-list_item_block_data_js" style="display: none">
                    <div class="warranty-list_block_data_union">
                        <div class="warranty-list_block_data_note">ФИО: <span><?= $arItem["UF_FIO"] ?></span></div>
                        <div class="warranty-list_block_data_note">Email: <span><?= $arItem["UF_EMAIL"] ?></span></div>
                        <div class="warranty-list_block_data_note">Телефон: <span><?= $arItem["UF_PHONE"] ?></span></div>
                    </div>
                    <div class="warranty-list_block_data_union">
                        <div class="warranty-list_block_data_note">Тип заявки: <span><?= $arItem["TYPE_NAME"] ?></span></div>
                        <div class="warranty-list_block_data_note">Статус заявки:&nbsp;
                            <?php if ($arResult["USER_CAN_CHANGE"] == "Y") { ?>
                                <select class="warranty-list_block_form_select status_select_js">
                                    <?php foreach ($arResult["STATUSES"] as $value => $name) { ?>
                                        <option
                                            value="<?= $value ?>"
                                            <?= $value == $arItem["UF_STATUS"] ? "selected" : "" ?>
                                        ><?= $name ?></option>
                                    <?php } ?>
                                </select>
                            <?php } else { ?>
                                <span><?= $arItem["STATUS_NAME"] ?></span>
                            <?php } ?>
                        </div>
                    </div>
                    <div class="warranty-list_block_data_union">
                        <div class="warranty-list_block_data_note">Наименование изделия: <span><?= $preProductName . $arItem["UF_PRODUCT_NAME"] . " [" . $arItem["UF_PRODUCT_ID"] . "]" . $postProductName?></span></div>
                        <div class="warranty-list_block_data_note">Серийный номер изделия: <span><?= $arItem["UF_SERIAL_NUMBER"] ?></span></div>
                        <div class="warranty-list_block_data_note">Описание неисправности: <span><?= $arItem["UF_DEFECT"] ?></span></div>
                        <div class="warranty-list_block_data_note">Комплектация: <span><?= $arItem["UF_EQUIPMENT"] ?></span></div>
                        <div class="warranty-list_block_data_note">Товар остался у клиента: <span><?= $arItem["UF_PRODUCT_REMAINS_WITH_CLIENT"] ? "Да" : "Нет"  ?></span></div>
                        <div class="warranty-list_block_data_note">Куплено у нас: <span><?= $arItem["UF_PURCHASED_FROM_US"] ? "Да" : "Нет" ?></span></div>
                        <?php if (!empty($arItem["UF_PRICE"]) && is_numeric($arItem["UF_PRICE"])) { ?>
                            <div class="warranty-list_block_data_note">Стоимость ремонта: <span><?= CCurrencyLang::CurrencyFormat($arItem["UF_PRICE"], "RUB") ?></span></div>
                        <?php } ?>
                        <?php if (!empty($arItem["UF_DATE_OF_PURCHASE"]) && is_object($arItem["UF_DATE_OF_PURCHASE"])) { ?>
                            <div class="warranty-list_block_data_note">Дата покупки: <span><?= $arItem["UF_DATE_OF_PURCHASE"]->format('Y-m-d') ?></span></div>
                        <?php } ?>
                        <div
                            class="warranty-list_block_data_note warranty-list_comment-js"
                            <?= empty($arItem["UF_COMMENT"]) ? 'style="display: none"' : '' ?>
                        >Комментарий: <span class="warranty-list_comment_text-js"><?= htmlspecialcharsbx($arItem["UF_COMMENT"] ?? "") ?></span></div>
                        <form class="warranty-list_comment_form warranty-list_comment_form-js" style="display: none">
                            <textarea
                                class="warranty-list_comment_textarea warranty-list_comment_textarea-js"
                                name="UF_COMMENT"
                                aria-label="Комментарий"
                            ><?= htmlspecialcharsbx($arItem["UF_COMMENT"] ?? "") ?></textarea>
                            <div class="warranty-list_comment_actions">
                                <button type="submit" class="base_btn warranty-list_comment_save-js">Сохранить</button>
                                <button type="button" class="base_btn warranty-list_comment_cancel-js">Отмена</button>
                            </div>
                            <div class="warranty-list_comment_error warranty-list_comment_error-js" style="display: none"></div>
                        </form>
                    </div>
                    <?php if (!empty($arItem["FILES"])) { ?>
                        <div class="warranty-list_block_data_union_files">
                            <?php foreach ($arItem["FILES"] as $fileItem) { ?>
                                <?php
                                $previewSrc = (string)($fileItem["PREVIEW_SRC"] ?? "");
                                if ($previewSrc === "") {
                                    $extension = strtolower((string)pathinfo(
                                        (string)($fileItem["ORIGINAL_NAME"] ?: $fileItem["SRC"]),
                                        PATHINFO_EXTENSION
                                    ));
                                    if (strpos((string)($fileItem["CONTENT_TYPE"] ?? ""), "image/") === 0
                                        || in_array($extension, ["jpg", "jpeg", "png", "gif", "webp", "bmp"], true)
                                    ) {
                                        $preview = CFile::ResizeImageGet(
                                            (int)$fileItem["ID"],
                                            ["width" => 180, "height" => 135],
                                            BX_RESIZE_IMAGE_PROPORTIONAL,
                                            true
                                        );
                                        $previewSrc = (string)($preview["src"] ?? $fileItem["SRC"]);
                                    }
                                }
                                ?>
                                <div class="warranty-list_file">
                                    <a
                                        class="warranty-list_file_open"
                                        target="_blank"
                                        href="<?= htmlspecialcharsbx($fileItem["SRC"]) ?>"
                                        title="Открыть <?= htmlspecialcharsbx($fileItem["ORIGINAL_NAME"]) ?>"
                                    >
                                        <?php if ($previewSrc !== "") { ?>
                                            <img
                                                class="warranty-list_file_preview"
                                                src="<?= htmlspecialcharsbx($previewSrc) ?>"
                                                alt="<?= htmlspecialcharsbx($fileItem["ORIGINAL_NAME"]) ?>"
                                                loading="lazy"
                                            >
                                        <?php } else { ?>
                                            <?php app()->showIconByCode("icon_file_96", $fileItem["ORIGINAL_NAME"]) ?>
                                        <?php } ?>
                                    </a>
                                    <a
                                        class="base_btn warranty-list_file_download"
                                        href="<?= htmlspecialcharsbx($fileItem["SRC"]) ?>"
                                        download="<?= htmlspecialcharsbx($fileItem["ORIGINAL_NAME"]) ?>"
                                    >Скачать фото</a>
                                </div>
                            <?php } ?>
                        </div>
                    <?php } ?>
                    <form class="warranty-list_photos_form warranty-list_photos_form-js" style="display: none">
                        <div class="warranty-list_photos_title">Фотографии</div>
                        <?php $APPLICATION->IncludeComponent(
                            "bitrix:main.file.input",
                            "",
                            [
                                "INPUT_NAME" => "OTHER_FILES",
                                "INPUT_VALUE" => $arItem["FILE_IDS"] ?? [],
                                "CONTROL_ID" => "warranty_photos_" . $arItem["ID"],
                                "CONTROL_UNIQUE_ID" => "warranty_photos_" . $arItem["ID"],
                                "MULTIPLE" => "Y",
                                "MAX_FILE_SIZE" => 10 * 1024 * 1024,
                                "MODULE_ID" => "autobud",
                                "ALLOW_UPLOAD" => "I",
                                "INPUT_CAPTION" => "Добавить фото",
                            ],
                            false,
                            ["HIDE_ICONS" => "Y"]
                        ); ?>
                        <div class="warranty-list_photos_actions">
                            <button type="submit" class="base_btn warranty-list_photos_save-js">Сохранить фотографии</button>
                            <button type="button" class="base_btn warranty-list_photos_cancel-js">Отмена</button>
                        </div>
                        <div class="warranty-list_photos_success warranty-list_photos_success-js" style="display: none"></div>
                        <div class="warranty-list_photos_error warranty-list_photos_error-js" style="display: none"></div>
                    </form>
                    <div class="warranty-list_block_data_union warranty-list_actions">
                        <button type="submit" class="base_btn warranty-list_item_btn_pdf_js" aria-label="Скачать">Скачать PDF</button>
                        <button type="submit" class="base_btn warranty-list_item_btn_send_email_js" aria-label="Скачать">Отправить на почту</button>
                        <?php if ($arResult["USER_CAN_CHANGE"] == "Y") { ?>
                            <button type="submit" class="base_btn warranty-list_item_btn_update-js" aria-label="Изменить">Изменить</button>
                        <?php } ?>
                        <button type="button" class="base_btn warranty-list_comment_edit-js">Изменить комментарий</button>
                        <button type="button" class="base_btn warranty-list_photos_edit-js">Изменить фотографии</button>
                        <?php if ($arResult["USER_CAN_WATCH_LOGS"] == "Y") { ?>
                            <button type="submit" class="base_btn warranty-list_item_btn_log_js" aria-label="Посмотреть историю">Посмотреть историю</button>
                        <?php } ?>
                    </div>
                    <?php if (!empty($arItem["CREATED_BY"])) { ?>
                        <div class="warranty-list_block_created_by">
                            <span>заявка создана пользователем: <?= $arItem["CREATED_BY"]?></span>
                        </div>
                    <?php } ?>
                </div>
            </div>
        <?php } ?>
        <?= $arResult["PAGINATION"]?>
    <?php } else { ?>
            <div class="warranty-list__block_title">Обращений не найдено</div>
    <?php } ?>
</div>
<?php
$objModalHelper = \Autobud\Helpers\Modal::getInstance();
if ($objModalHelper->init("popup__warranty_update")->isSet()) {
    $objModalHelper->show();
}
?>
