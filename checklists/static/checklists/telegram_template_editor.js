(function () {
    'use strict';

    const form = document.getElementById('telegram-template-form');
    if (!form) return;

    const titleField = form.querySelector('[name="title"]');
    const bodyField = form.querySelector('[name="body"]');
    const eventField = form.querySelector('[name="event_code"]');
    const preview = document.getElementById('telegram-preview-content');
    const status = document.getElementById('telegram-preview-status');
    const refreshButton = document.getElementById('telegram-preview-refresh');
    let activeField = bodyField || titleField;
    let timer = null;
    let requestNumber = 0;

    [titleField, bodyField].filter(Boolean).forEach(function (field) {
        field.addEventListener('focus', function () {
            activeField = field;
        });
    });

    document.querySelectorAll('[data-template-token]').forEach(function (button) {
        button.addEventListener('click', function () {
            if (!activeField) return;
            const token = button.dataset.templateToken;
            const start = activeField.selectionStart ?? activeField.value.length;
            const end = activeField.selectionEnd ?? activeField.value.length;
            activeField.setRangeText(token, start, start, 'end');
            activeField.focus();
            activeField.dispatchEvent(new Event('input', {bubbles: true}));
        });
    });

    function updatePreview() {
        const currentRequest = ++requestNumber;
        status.textContent = 'Обновление…';
        fetch(form.dataset.previewUrl, {
            method: 'POST',
            body: new FormData(form),
            credentials: 'same-origin',
            headers: {'X-Requested-With': 'XMLHttpRequest'}
        })
            .then(function (response) {
                return response.json().then(function (data) {
                    return {ok: response.ok, data: data};
                });
            })
            .then(function (result) {
                if (currentRequest !== requestNumber) return;
                if (!result.ok) {
                    preview.textContent = 'Исправьте ошибки формы, чтобы увидеть предпросмотр.';
                    status.textContent = 'Предпросмотр недоступен';
                    return;
                }
                preview.textContent = result.data.preview;
                status.textContent = 'Предпросмотр актуален';
            })
            .catch(function () {
                if (currentRequest !== requestNumber) return;
                preview.textContent = 'Не удалось обновить предпросмотр.';
                status.textContent = 'Ошибка соединения';
            });
    }

    form.addEventListener('input', function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(updatePreview, 250);
    });
    form.addEventListener('change', function (event) {
        if (event.target === eventField && form.dataset.createUrl) return;
        window.clearTimeout(timer);
        timer = window.setTimeout(updatePreview, 100);
    });

    if (eventField && form.dataset.createUrl) {
        eventField.addEventListener('change', function () {
            const hasText = (titleField && titleField.value.trim()) ||
                (bodyField && bodyField.value.trim());
            if (!hasText || window.confirm('Загрузить стандартный текст выбранного события? Текущие значения формы будут заменены.')) {
                window.location.assign(
                    form.dataset.createUrl + '?event=' + encodeURIComponent(eventField.value)
                );
            }
        });
    }

    if (refreshButton) refreshButton.addEventListener('click', updatePreview);
    updatePreview();
}());
