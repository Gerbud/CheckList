(function () {
    'use strict';

    function enhanceSelect(select) {
        if (select.dataset.emojiPickerReady === 'true') return;
        select.dataset.emojiPickerReady = 'true';

        const picker = document.createElement('div');
        picker.className = 'telegram-emoji-search-picker';

        const trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'telegram-emoji-search-trigger';
        trigger.setAttribute('aria-expanded', 'false');
        trigger.title = 'Выбрать иконку Telegram';

        const panel = document.createElement('div');
        panel.className = 'telegram-emoji-search-panel';
        panel.hidden = true;

        const search = document.createElement('input');
        search.type = 'search';
        search.className = 'telegram-emoji-search-input';
        search.placeholder = 'Поиск: робот, сумка, машина…';
        search.setAttribute('aria-label', 'Поиск иконки');

        const grid = document.createElement('div');
        grid.className = 'telegram-emoji-search-grid';
        grid.setAttribute('role', 'listbox');

        const empty = document.createElement('div');
        empty.className = 'telegram-emoji-search-empty';
        empty.textContent = 'Ничего не найдено';
        empty.hidden = true;

        function updateTrigger() {
            const option = select.options[select.selectedIndex];
            trigger.textContent = option && option.value ? option.text : '—';
        }

        Array.from(select.options).forEach(function (option) {
            if (!option.value) return;
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'telegram-emoji-search-option';
            button.textContent = option.text;
            button.dataset.value = option.value;
            button.dataset.search = (option.dataset.search || option.text).toLowerCase();
            button.title = option.dataset.search || option.text;
            button.setAttribute('role', 'option');
            button.addEventListener('click', function () {
                select.value = option.value;
                select.dispatchEvent(new Event('change', {bubbles: true}));
                updateTrigger();
                closePanel();
            });
            grid.appendChild(button);
        });

        function closePanel() {
            panel.hidden = true;
            trigger.setAttribute('aria-expanded', 'false');
            search.value = '';
            search.dispatchEvent(new Event('input'));
        }

        function positionPanel() {
            if (panel.hidden) return;

            const margin = 8;
            const gap = 6;
            const triggerRect = trigger.getBoundingClientRect();
            const panelWidth = Math.min(330, window.innerWidth - (margin * 2));
            panel.style.width = panelWidth + 'px';
            panel.style.left = Math.max(
                margin,
                Math.min(triggerRect.left, window.innerWidth - panelWidth - margin),
            ) + 'px';

            const panelHeight = panel.offsetHeight;
            const roomBelow = window.innerHeight - triggerRect.bottom - gap - margin;
            const roomAbove = triggerRect.top - gap - margin;
            const openAbove = roomBelow < Math.min(panelHeight, 260)
                && roomAbove > roomBelow;
            const desiredTop = openAbove
                ? triggerRect.top - gap - panelHeight
                : triggerRect.bottom + gap;
            panel.style.top = Math.max(
                margin,
                Math.min(desiredTop, window.innerHeight - panelHeight - margin),
            ) + 'px';
        }

        trigger.addEventListener('click', function () {
            const opening = panel.hidden;
            document.querySelectorAll('.telegram-emoji-search-panel:not([hidden])').forEach(
                function (otherPanel) {
                    otherPanel.hidden = true;
                    const otherTrigger = otherPanel.previousElementSibling;
                    if (otherTrigger) otherTrigger.setAttribute('aria-expanded', 'false');
                }
            );
            panel.hidden = !opening;
            trigger.setAttribute('aria-expanded', String(opening));
            if (opening) {
                positionPanel();
                search.focus();
            }
        });

        search.addEventListener('input', function () {
            const terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
            let visible = 0;
            grid.querySelectorAll('.telegram-emoji-search-option').forEach(function (button) {
                const matches = terms.every(function (term) {
                    return button.dataset.search.includes(term);
                });
                button.hidden = !matches;
                if (matches) visible += 1;
            });
            empty.hidden = visible !== 0;
            positionPanel();
        });

        window.addEventListener('resize', positionPanel);
        window.addEventListener('scroll', positionPanel, true);

        panel.append(search, grid, empty);
        picker.append(trigger, panel);
        select.after(picker);
        select.classList.add('telegram-emoji-select-enhanced');
        updateTrigger();
    }

    function init(root) {
        root.querySelectorAll('select.telegram-emoji-select').forEach(enhanceSelect);
    }

    document.addEventListener('DOMContentLoaded', function () {
        init(document);
        document.addEventListener('formset:added', function (event) {
            init(event.target);
        });
        document.addEventListener('click', function (event) {
            if (!event.target.closest('.telegram-emoji-search-picker')) {
                document.querySelectorAll('.telegram-emoji-search-panel:not([hidden])').forEach(
                    function (panel) { panel.hidden = true; }
                );
            }
        });
    });
}());
