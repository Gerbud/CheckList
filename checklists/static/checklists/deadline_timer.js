(function () {
    "use strict";

    function formatDuration(milliseconds) {
        var totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
        var hours = Math.floor(totalSeconds / 3600);
        var minutes = Math.floor((totalSeconds % 3600) / 60);
        var seconds = totalSeconds % 60;
        return [hours, minutes, seconds]
            .map(function (value) { return String(value).padStart(2, "0"); })
            .join(":");
    }

    document.querySelectorAll(".deadline-timer").forEach(function (element) {
        var serverStartedAt = Date.parse(element.dataset.serverNow);
        var browserStartedAt = Date.now();
        var completionAvailableAt = Date.parse(
            element.dataset.completionAvailableAt || element.dataset.opensAt
        );
        var opensAt = Date.parse(element.dataset.opensAt);
        var deadlineAt = Date.parse(element.dataset.deadlineAt);
        var warningAt = Date.parse(element.dataset.warningAt);
        var state = element.dataset.state;
        var completionButton = element.dataset.completionButtonId
            ? document.getElementById(element.dataset.completionButtonId)
            : null;

        function update() {
            if (state === "completed" || state === "completed_late") {
                return;
            }
            var serverNow = serverStartedAt + (Date.now() - browserStartedAt);
            var target;
            var prefix;
            var stageOpened = serverNow >= opensAt;
            var completionAllowed = (
                stageOpened && serverNow >= completionAvailableAt
            );
            if (completionButton) {
                completionButton.disabled = !completionAllowed;
                completionButton.setAttribute(
                    "aria-disabled",
                    completionAllowed ? "false" : "true"
                );
            }
            if (!stageOpened) {
                target = opensAt;
                prefix = "До начала этапа: ";
            } else if (!completionAllowed) {
                target = completionAvailableAt;
                prefix = "До возможности завершения: ";
            } else if (serverNow < deadlineAt) {
                target = deadlineAt;
                prefix = "До окончания этапа: ";
            } else {
                element.textContent = "Этап просрочен на: " + formatDuration(serverNow - deadlineAt);
                element.classList.add("text-danger");
                element.classList.remove("text-warning");
                return;
            }
            var remaining = target - serverNow;
            element.textContent = prefix + formatDuration(remaining);
            element.classList.toggle(
                "text-warning",
                completionAllowed && serverNow >= warningAt
            );
        }

        update();
        window.setInterval(update, 1000);
    });
}());
