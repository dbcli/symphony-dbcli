const THEME_KEY = "symphony-dbcli-theme";

function preferredTheme() {
  const stored = window.localStorage.getItem(THEME_KEY);
  if (stored === "dark" || stored === "light") {
    return stored;
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  for (const button of document.querySelectorAll("[data-theme-toggle]")) {
    const isDark = theme === "dark";
    button.setAttribute("aria-pressed", String(isDark));
    button.setAttribute("aria-label", isDark ? "Switch to light mode" : "Switch to dark mode");
    const label = button.querySelector("[data-theme-toggle-label]");
    if (label) {
      label.textContent = isDark ? "Light" : "Dark";
    }
  }
}

document.documentElement.classList.add("has-js");
applyTheme(preferredTheme());

for (const button of document.querySelectorAll("[data-theme-toggle]")) {
  button.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    window.localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  });
}

function formBody(values) {
  const body = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    body.set(key, value);
  }
  return body;
}

async function moveWorkItem(workItemId, targetState) {
  const response = await window.fetch(`/work-items/${workItemId}/move`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: formBody({ target_state: targetState }),
  });
  if (!response.ok) {
    throw new Error(`Move failed with ${response.status}`);
  }
}

function restoreDroppedItem(event) {
  event.from.insertBefore(event.item, event.from.children[event.oldIndex] || null);
}

function openMoveWorkModal(workItemId, targetState) {
  if (!window.htmx) {
    window.location.href = `/work-items/${workItemId}?target_state=${encodeURIComponent(targetState)}#move-work`;
    return;
  }
  const returnTo = `${window.location.pathname}${window.location.search}`;
  const params = new URLSearchParams({
    target_state: targetState,
    return_to: returnTo,
  });
  window.htmx.ajax("GET", `/work-items/${workItemId}/move-form?${params.toString()}`, {
    target: "#modal-root",
    swap: "innerHTML",
  });
}

function openActivateWorkModal(sourceItemId) {
  if (!window.htmx) {
    window.location.href = `/source-items/${sourceItemId}/activate`;
    return;
  }
  const returnTo = `${window.location.pathname}${window.location.search}`;
  const params = new URLSearchParams({ return_to: returnTo });
  window.htmx.ajax("GET", `/source-items/${sourceItemId}/activate-form?${params.toString()}`, {
    target: "#modal-root",
    swap: "innerHTML",
  });
}

function setupKanbanDrag() {
  if (!window.Sortable) {
    return;
  }
  const lists = [...document.querySelectorAll(".kanban-list[data-state]")];
  for (const list of lists) {
    if (list.dataset.sortableReady === "true") {
      continue;
    }
    list.dataset.sortableReady = "true";
    window.Sortable.create(list, {
      group: "work-items",
      animation: 120,
      draggable: "[data-work-item-id], [data-source-item-id]",
      ghostClass: "is-dragging",
      emptyInsertThreshold: 48,
      onEnd: async (event) => {
        const workItemId = event.item.dataset.workItemId;
        const sourceItemId = event.item.dataset.sourceItemId;
        const targetState = event.to.dataset.state;
        const previousState = event.from.dataset.state;
        if (!targetState || targetState === previousState) {
          return;
        }
        if (sourceItemId) {
          restoreDroppedItem(event);
          if (previousState === "backlog" && targetState === "todo") {
            openActivateWorkModal(sourceItemId);
          }
          return;
        }
        if (!workItemId) {
          return;
        }
        if (targetState === "backlog") {
          restoreDroppedItem(event);
          return;
        }
        if (previousState === "in_review" && targetState === "in_progress") {
          restoreDroppedItem(event);
          openMoveWorkModal(workItemId, targetState);
          return;
        }
        event.item.classList.add("is-moving");
        try {
          await moveWorkItem(workItemId, targetState);
          window.location.reload();
        } catch {
          window.location.reload();
        }
      },
    });
  }
}

setupKanbanDrag();

function setupMoveWorkForm() {
  const movePanel = document.querySelector("#move-work");
  const targetSelect = movePanel?.querySelector('select[name="target_state"]');
  if (!movePanel || !targetSelect) {
    return;
  }
  const requestedTarget = new URLSearchParams(window.location.search).get("target_state");
  if (requestedTarget) {
    targetSelect.value = requestedTarget;
  }
  if (requestedTarget === "in_progress" && !movePanel.querySelector("[data-rerun-prompt]")) {
    const hint = document.createElement("p");
    hint.className = "form-hint";
    hint.dataset.rerunPrompt = "true";
    hint.textContent =
      "Add rerun reasons and optional model context before moving this item back to In Progress.";
    movePanel.querySelector("h2")?.after(hint);
  }
}

setupMoveWorkForm();

function setupLineNumberedEditors() {
  const editors = [...document.querySelectorAll("[data-line-numbered-editor]")];
  for (const editor of editors) {
    if (editor.dataset.lineNumbersReady === "true") {
      continue;
    }
    const source = editor.querySelector("[data-line-numbered-source]");
    const gutter = editor.querySelector("[data-line-number-gutter]");
    if (!(source instanceof HTMLTextAreaElement) || !gutter) {
      continue;
    }
    editor.dataset.lineNumbersReady = "true";

    function renderLineNumbers() {
      const lineCount = Math.max(1, source.value.split("\n").length);
      gutter.replaceChildren(
        ...Array.from({ length: lineCount }, (_, index) => {
          const line = document.createElement("span");
          line.textContent = String(index + 1);
          return line;
        }),
      );
      gutter.scrollTop = source.scrollTop;
    }

    source.addEventListener("input", renderLineNumbers);
    source.addEventListener("scroll", () => {
      gutter.scrollTop = source.scrollTop;
    });
    renderLineNumbers();
  }
}

setupLineNumberedEditors();

function closeModal() {
  const modalRoot = document.querySelector("#modal-root");
  if (modalRoot) {
    modalRoot.innerHTML = "";
  }
}

document.body.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) {
    return;
  }
  if (target.closest("[data-modal-close]") || target.matches("[data-modal-backdrop]")) {
    closeModal();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeModal();
  }
});

function setupWorkflowFlowchart() {
  const frame = document.querySelector("[data-workflow-flowchart]");
  const svg = document.querySelector("[data-workflow-svg]");
  const label = document.querySelector("[data-workflow-zoom-label]");
  const controls = document.querySelector("[data-workflow-controls]");
  if (!frame || !svg || !label || !controls || frame.dataset.workflowReady === "true") {
    return;
  }

  frame.dataset.workflowReady = "true";
  const baseWidth = Number(svg.getAttribute("width")) || 1;
  const baseHeight = Number(svg.getAttribute("height")) || 1;
  let scale = 1;
  let isPanning = false;
  let panStartX = 0;
  let panStartY = 0;
  let scrollStartLeft = 0;
  let scrollStartTop = 0;

  function applyScale(nextScale) {
    scale = Math.max(0.35, Math.min(2, nextScale));
    svg.style.width = `${Math.round(baseWidth * scale)}px`;
    svg.style.height = `${Math.round(baseHeight * scale)}px`;
    label.textContent = `${Math.round(scale * 100)}%`;
  }

  function fitToFrame() {
    const widthScale = (frame.clientWidth - 24) / baseWidth;
    const heightScale = (frame.clientHeight - 24) / baseHeight;
    applyScale(Math.min(1, widthScale, heightScale));
    frame.scrollTo({ left: 0, top: 0 });
  }

  controls.addEventListener("click", (event) => {
    const button = event.target.closest("[data-workflow-zoom]");
    if (!button) {
      return;
    }
    const action = button.dataset.workflowZoom;
    if (action === "fit") {
      fitToFrame();
    } else if (action === "out") {
      applyScale(scale - 0.15);
    } else if (action === "in") {
      applyScale(scale + 0.15);
    } else {
      applyScale(1);
      frame.scrollTo({ left: 0, top: 0 });
    }
  });

  frame.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest("a, button, input, textarea, select")) {
      return;
    }
    isPanning = true;
    panStartX = event.clientX;
    panStartY = event.clientY;
    scrollStartLeft = frame.scrollLeft;
    scrollStartTop = frame.scrollTop;
    frame.classList.add("is-panning");
    frame.setPointerCapture(event.pointerId);
  });

  frame.addEventListener("pointermove", (event) => {
    if (!isPanning) {
      return;
    }
    frame.scrollLeft = scrollStartLeft - (event.clientX - panStartX);
    frame.scrollTop = scrollStartTop - (event.clientY - panStartY);
  });

  frame.addEventListener("pointerup", (event) => {
    isPanning = false;
    frame.classList.remove("is-panning");
    frame.releasePointerCapture(event.pointerId);
  });

  frame.addEventListener("pointercancel", () => {
    isPanning = false;
    frame.classList.remove("is-panning");
  });

  applyScale(1);
}

setupWorkflowFlowchart();

document.body.addEventListener("htmx:afterSwap", (event) => {
  if (event.target.id === "board-columns") {
    setupKanbanDrag();
  }
  if (event.target.id === "dashboard-main") {
    setupKanbanDrag();
    setupMoveWorkForm();
    setupLineNumberedEditors();
    setupWorkflowFlowchart();
  }
});
