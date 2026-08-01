(() => {
  const table = document.getElementById("shift-calendar");
  if (!table) return;

  const csrfToken = document.getElementById("shift-calendar-csrf")?.value || "";
  const status = document.getElementById("calendar-status");
  const completion = document.getElementById("calendar-completion");
  const completionBar = document.getElementById("calendar-completion-bar");
  const incomplete = document.getElementById("calendar-incomplete");
  const selectionSummary = document.getElementById("selection-summary");
  const fillRangeButton = document.getElementById("fill-range-button");
  const clearSelectionButton = document.getElementById("clear-selection");
  const editorElement = document.getElementById("shift-editor-modal");
  const copyWeekElement = document.getElementById("copy-week-modal");
  const modalControllers = new WeakMap();

  function getModalController(element) {
    if (!element) return null;
    if (modalControllers.has(element)) return modalControllers.get(element);
    const BootstrapModal = window.bootstrap?.Modal;
    if (BootstrapModal) {
      const controller = BootstrapModal.getOrCreateInstance(element);
      modalControllers.set(element, controller);
      return controller;
    }
    let backdrop = null;
    const controller = {
      show() {
        element.style.display = "block";
        element.removeAttribute("aria-hidden");
        element.setAttribute("aria-modal", "true");
        element.setAttribute("role", "dialog");
        element.classList.add("show");
        document.body.classList.add("modal-open");
        backdrop = document.createElement("div");
        backdrop.className = "modal-backdrop fade show";
        backdrop.addEventListener("click", () => controller.hide());
        document.body.appendChild(backdrop);
        element.querySelector("button, input, select, textarea")?.focus();
      },
      hide() {
        element.classList.remove("show");
        element.style.display = "none";
        element.setAttribute("aria-hidden", "true");
        element.removeAttribute("aria-modal");
        element.removeAttribute("role");
        backdrop?.remove();
        backdrop = null;
        document.body.classList.remove("modal-open");
        element.dispatchEvent(new Event("hidden.bs.modal"));
      }
    };
    modalControllers.set(element, controller);
    return controller;
  }

  const editorModal = getModalController(editorElement);
  const copyWeekModal = getModalController(copyWeekElement);
  const selected = new Set();

  let isDragging = false;
  let didDrag = false;
  let dragEmployeeId = null;
  let dragStartCell = null;
  let dragAdditive = false;
  let suppressClick = false;
  let editorCells = [];
  let editorMode = "cell";
  let selectedShiftType = "work";
  let selectedTemplateId = null;

  const classByType = {
    work: "shift-work",
    night: "shift-night",
    day_off: "shift-day-off",
    vacation: "shift-vacation",
    sick_leave: "shift-sick",
    service: "shift-service",
    personal: "shift-personal"
  };

  function setStatus(message, isError = false) {
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("text-danger", isError);
    status.classList.toggle("text-secondary", !isError);
  }

  function formatDate(value) {
    const [year, month, day] = value.split("-");
    return `${day}.${month}.${year}`;
  }

  function updateSelectionUi() {
    const count = selected.size;
    selectionSummary.textContent = count
      ? `Выбрано: ${count}`
      : "Ячейки не выбраны";
    fillRangeButton.disabled = count === 0;
    clearSelectionButton.classList.toggle("d-none", count === 0);
  }

  function selectCell(cell, additive = true) {
    if (!additive) clearSelection();
    if (cell.disabled) return;
    selected.add(cell);
    cell.classList.add("selected");
    updateSelectionUi();
  }

  function selectRange(startCell, endCell, additive = false) {
    if (!startCell || !endCell) return;
    if (startCell.dataset.employeeId !== endCell.dataset.employeeId) return;
    if (!additive) clearSelection();
    const firstDate = startCell.dataset.date < endCell.dataset.date
      ? startCell.dataset.date
      : endCell.dataset.date;
    const lastDate = startCell.dataset.date > endCell.dataset.date
      ? startCell.dataset.date
      : endCell.dataset.date;
    startCell.closest("tr")
      .querySelectorAll("[data-shift-cell]:not(:disabled)")
      .forEach(cell => {
        if (cell.dataset.date >= firstDate && cell.dataset.date <= lastDate) {
          selectCell(cell);
        }
      });
  }

  function clearSelection() {
    selected.forEach(cell => cell.classList.remove("selected"));
    selected.clear();
    updateSelectionUi();
  }

  function updateMetrics(result) {
    if (completion && result.completion_percent !== undefined) {
      completion.textContent = `${result.completion_percent}%`;
      completionBar.style.width = `${result.completion_percent}%`;
    }
    if (incomplete && result.employees_without_schedule !== undefined) {
      incomplete.textContent = result.employees_without_schedule;
    }
  }

  function renderCell(cell, assignment) {
    Object.values(classByType).forEach(css => cell.classList.remove(css));
    if (!assignment) {
      cell.dataset.shiftType = "";
      cell.dataset.comment = "";
      cell.dataset.assignmentId = "";
      cell.textContent = "·";
      cell.title = "Добавить смену";
      return;
    }
    cell.dataset.shiftType = assignment.shift_type;
    cell.dataset.comment = assignment.comment || "";
    cell.dataset.assignmentId = String(assignment.id);
    cell.classList.add(classByType[assignment.shift_type]);
    cell.textContent = assignment.short;
    const time = assignment.shift_start
      ? `, ${assignment.shift_start}–${assignment.shift_end}`
      : "";
    cell.title = `${assignment.label}${time}`;
  }

  function renderResultCells(cells) {
    cells.forEach(item => {
      const selector = [
        "[data-shift-cell]",
        `[data-employee-id="${item.employee_id}"]`,
        `[data-date="${item.date}"]`
      ].join("");
      const cell = table.querySelector(selector);
      if (cell) renderCell(cell, item.assignment);
    });
  }

  async function postJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
        "X-Requested-With": "XMLHttpRequest"
      },
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok || !result.ok) {
      throw new Error(result.error || "Не удалось сохранить график.");
    }
    return result;
  }

  function setEditorError(message = "") {
    const error = document.getElementById("shift-editor-error");
    error.textContent = message;
    error.classList.toggle("d-none", !message);
  }

  function chooseType(shiftType, templateId = null) {
    selectedShiftType = shiftType;
    selectedTemplateId = templateId;
    document.querySelectorAll("[data-editor-type]").forEach(button => {
      button.classList.toggle(
        "active",
        button.dataset.editorType === shiftType && templateId === null
      );
    });
    document.querySelectorAll("[data-editor-template]").forEach(button => {
      button.classList.toggle(
        "active",
        Number(button.dataset.editorTemplate) === templateId
      );
    });
  }

  function showEditor({
    mode,
    cells = [],
    shiftType = "work",
    comment = "",
    title = "Добавить смену"
  }) {
    editorMode = mode;
    editorCells = cells;
    selectedTemplateId = null;
    setEditorError();
    document.getElementById("shift-editor-title").textContent = title;
    document.getElementById("shift-comment").value = comment;
    const manualFields = document.getElementById("manual-editor-fields");
    const summary = document.getElementById("cell-editor-summary");
    const dateField = document.getElementById("cell-editor-date-field");
    const dateInput = document.getElementById("editor-date");
    const deleteButton = document.getElementById("delete-shift-button");

    manualFields.classList.toggle("d-none", mode !== "manual");
    summary.classList.toggle("d-none", mode === "manual");
    dateField.classList.toggle("d-none", mode !== "cell");
    deleteButton.classList.toggle(
      "d-none",
      mode !== "cell" || !cells[0]?.dataset.assignmentId
    );

    if (mode === "cell") {
      document.getElementById("editor-employee-name").textContent =
        cells[0].dataset.employeeName;
      document.getElementById("editor-date-label").textContent = "";
      dateInput.value = cells[0].dataset.date;
    } else if (mode === "range") {
      const dates = cells.map(cell => cell.dataset.date).sort();
      const employeeNames = new Set(
        cells.map(cell => cell.dataset.employeeName)
      );
      document.getElementById("editor-employee-name").textContent =
        employeeNames.size === 1
          ? cells[0].dataset.employeeName
          : `${employeeNames.size} сотрудников`;
      document.getElementById("editor-date-label").textContent =
        `${formatDate(dates[0])}–${formatDate(dates.at(-1))} · ${cells.length} ячеек`;
    }

    chooseType(shiftType || "work");
    editorModal?.show();
  }

  function openCellEditor(cell) {
    clearSelection();
    selectCell(cell);
    const isExisting = Boolean(cell.dataset.assignmentId);
    showEditor({
      mode: "cell",
      cells: [cell],
      shiftType: cell.dataset.shiftType || "work",
      comment: cell.dataset.comment || "",
      title: isExisting ? "Редактировать смену" : "Добавить смену"
    });
  }

  async function saveUpdates(updates, successMessage) {
    setEditorError();
    setStatus(`Сохраняем: ${updates.length}…`);
    document.getElementById("save-shift-button").disabled = true;
    try {
      const result = await postJson(table.dataset.updateUrl, {updates});
      renderResultCells(result.cells);
      updateMetrics(result);
      clearSelection();
      editorModal?.hide();
      setStatus(successMessage || `Сохранено ячеек: ${result.cells.length}.`);
      return true;
    } catch (error) {
      setEditorError(error.message);
      setStatus(error.message, true);
      return false;
    } finally {
      document.getElementById("save-shift-button").disabled = false;
    }
  }

  table.querySelectorAll("[data-shift-cell]:not(:disabled)").forEach(cell => {
    cell.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      isDragging = true;
      didDrag = false;
      dragEmployeeId = cell.dataset.employeeId;
      dragStartCell = cell;
      dragAdditive = event.ctrlKey || event.metaKey;
      selectRange(cell, cell, dragAdditive);
    });

    cell.addEventListener("pointerenter", () => {
      if (!isDragging || cell.dataset.employeeId !== dragEmployeeId) return;
      didDrag = cell !== dragStartCell;
      selectRange(dragStartCell, cell, dragAdditive);
    });

    cell.addEventListener("click", event => {
      event.preventDefault();
      if (suppressClick) {
        suppressClick = false;
        return;
      }
      openCellEditor(cell);
    });
  });

  document.addEventListener("pointermove", event => {
    if (!isDragging) return;
    const cell = document
      .elementFromPoint(event.clientX, event.clientY)
      ?.closest("[data-shift-cell]");
    if (
      !cell
      || cell.disabled
      || cell.dataset.employeeId !== dragEmployeeId
    ) {
      return;
    }
    didDrag = cell !== dragStartCell;
    selectRange(dragStartCell, cell, dragAdditive);
  });

  document.addEventListener("pointerup", () => {
    if (!isDragging) return;
    const clickedCell = dragStartCell;
    isDragging = false;
    dragEmployeeId = null;
    dragStartCell = null;
    if (didDrag && selected.size > 1) {
      suppressClick = true;
      setStatus(
        `Выбрано ячеек: ${selected.size}. Нажмите «Заполнить диапазон».`
      );
    } else if (clickedCell) {
      suppressClick = true;
      openCellEditor(clickedCell);
    }
  });

  document.querySelectorAll("[data-editor-type]").forEach(button => {
    button.addEventListener("click", () => {
      chooseType(button.dataset.editorType);
    });
  });

  document.querySelectorAll("[data-editor-template]").forEach(button => {
    button.addEventListener("click", () => {
      chooseType(
        button.dataset.templateType,
        Number(button.dataset.editorTemplate)
      );
    });
  });

  document.getElementById("add-shift-button")?.addEventListener("click", () => {
    clearSelection();
    showEditor({mode: "manual", title: "Добавить смену"});
  });

  fillRangeButton?.addEventListener("click", () => {
    const cells = Array.from(selected).filter(cell => !cell.disabled);
    if (!cells.length) {
      setStatus("Сначала выделите диапазон.", true);
      return;
    }
    const firstType = cells[0].dataset.shiftType;
    const oneType = cells.every(cell => cell.dataset.shiftType === firstType);
    showEditor({
      mode: "range",
      cells,
      shiftType: oneType && firstType ? firstType : "work",
      title: "Заполнить диапазон"
    });
  });

  clearSelectionButton?.addEventListener("click", clearSelection);

  document.getElementById("save-shift-button")?.addEventListener(
    "click",
    async () => {
      const comment = document.getElementById("shift-comment").value.trim();
      let updates;
      if (editorMode === "manual") {
        const employeeId = Number(
          document.getElementById("manual-employee").value
        );
        const workDate = document.getElementById("manual-date").value;
        if (!employeeId || !workDate) {
          setEditorError("Выберите сотрудника и дату.");
          return;
        }
        updates = [{
          employee_id: employeeId,
          date: workDate,
          shift_type: selectedShiftType,
          template_id: selectedTemplateId,
          comment
        }];
      } else if (editorMode === "cell") {
        const cell = editorCells[0];
        const workDate = document.getElementById("editor-date").value;
        if (!workDate) {
          setEditorError("Укажите дату.");
          return;
        }
        updates = [{
          employee_id: Number(cell.dataset.employeeId),
          original_date: cell.dataset.date,
          date: workDate,
          shift_type: selectedShiftType,
          template_id: selectedTemplateId,
          comment
        }];
      } else {
        updates = editorCells.map(cell => ({
          employee_id: Number(cell.dataset.employeeId),
          date: cell.dataset.date,
          shift_type: selectedShiftType,
          template_id: selectedTemplateId,
          comment
        }));
      }
      await saveUpdates(
        updates,
        editorMode === "range"
          ? `Диапазон сохранён: ${updates.length} ячеек.`
          : "Смена сохранена."
      );
    }
  );

  document.getElementById("delete-shift-button")?.addEventListener(
    "click",
    async () => {
      if (!editorCells.length) return;
      await saveUpdates(
        editorCells.map(cell => ({
          employee_id: Number(cell.dataset.employeeId),
          date: cell.dataset.date,
          shift_type: "clear"
        })),
        "Смена удалена."
      );
    }
  );

  document.getElementById("copy-week-button")?.addEventListener(
    "click",
    async () => {
      const weekStart = document.getElementById("copy-week-start").value;
      const employeeValue = document.getElementById(
        "copy-week-employee"
      ).value;
      const employeeIds = employeeValue === "all"
        ? Array.from(
            document.querySelectorAll("#copy-week-employee option")
          )
            .map(option => Number(option.value))
            .filter(value => Number.isInteger(value) && value > 0)
        : [Number(employeeValue)];
      if (!employeeIds.length) {
        setStatus("Нет сотрудников для копирования.", true);
        return;
      }
      setStatus("Копируем неделю…");
      try {
        const result = await postJson(table.dataset.copyUrl, {
          month: table.dataset.month,
          week_start: weekStart,
          employee_ids: employeeIds
        });
        renderResultCells(result.cells);
        updateMetrics(result);
        copyWeekModal?.hide();
        setStatus(`Обновлено ячеек: ${result.changed}.`);
      } catch (error) {
        setStatus(error.message, true);
      }
    }
  );

  editorElement?.addEventListener("hidden.bs.modal", () => {
    setEditorError();
  });

  if (!window.bootstrap?.Modal) {
    document.querySelectorAll("[data-bs-toggle='modal']").forEach(button => {
      button.addEventListener("click", () => {
        const target = document.querySelector(button.dataset.bsTarget);
        getModalController(target)?.show();
      });
    });
    document.querySelectorAll("[data-bs-dismiss='modal']").forEach(button => {
      button.addEventListener("click", () => {
        getModalController(button.closest(".modal"))?.hide();
      });
    });
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      const openModal = document.querySelector(".modal.show");
      getModalController(openModal)?.hide();
    });
  }
  updateSelectionUi();
})();
