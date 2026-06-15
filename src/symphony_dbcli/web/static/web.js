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
    const labelText = isDark ? "Switch to light mode" : "Switch to dark mode";
    button.setAttribute("aria-pressed", String(isDark));
    button.setAttribute("aria-label", labelText);
    button.setAttribute("title", labelText);
    const label = button.querySelector("[data-theme-toggle-label]");
    if (label) {
      label.textContent = isDark ? "\u2600" : "\u263E";
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

function setupAccordions(root = document) {
  const toggles = [...root.querySelectorAll("[data-accordion-toggle]")];
  function toggleAccordion(toggle) {
    const bodyId = toggle.getAttribute("aria-controls");
    const body = bodyId ? document.getElementById(bodyId) : null;
    if (!body) {
      return;
    }
    const isExpanded = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!isExpanded));
    body.hidden = isExpanded;
  }
  for (const toggle of toggles) {
    if (toggle.dataset.accordionReady === "true") {
      continue;
    }
    toggle.dataset.accordionReady = "true";
    toggle.addEventListener("click", (event) => {
      const target = event.target;
      if (target instanceof Element && target !== toggle && target.closest("a, button, input, select, textarea")) {
        return;
      }
      toggleAccordion(toggle);
    });
    if (!(toggle instanceof HTMLButtonElement)) {
      toggle.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") {
          return;
        }
        event.preventDefault();
        toggleAccordion(toggle);
      });
    }
  }
}

setupAccordions();

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

function setupChatSubmitProgress(root = document) {
  const forms = [...root.querySelectorAll("[data-chat-submit-form]")];
  if (root instanceof Element && root.matches("[data-chat-submit-form]")) {
    forms.unshift(root);
  }
  for (const form of forms) {
    if (form.dataset.chatSubmitReady === "true") {
      continue;
    }
    form.dataset.chatSubmitReady = "true";
    form.querySelectorAll("textarea").forEach((textarea) => {
      textarea.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" || (!event.metaKey && !event.ctrlKey)) {
          return;
        }
        event.preventDefault();
        if (form.dataset.chatSubmitting === "true") {
          return;
        }
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
          return;
        }
        const submitEvent = new Event("submit", { bubbles: true, cancelable: true });
        if (form.dispatchEvent(submitEvent)) {
          form.submit();
        }
      });
    });
    form.addEventListener("submit", () => {
      if (form.dataset.chatSubmitting === "true") {
        return;
      }
      form.dataset.chatSubmitting = "true";
      const startedAt = Date.now();
      const submitButton = form.querySelector('button[type="submit"]');
      const compact = form.classList.contains("board-start-form");
      let progress = form.querySelector("[data-chat-submit-progress]");
      if (!compact && !progress) {
        progress = document.createElement("p");
        progress.className = "form-hint chat-submit-progress";
        progress.dataset.chatSubmitProgress = "true";
        progress.setAttribute("aria-live", "polite");
        form.querySelector(".form-actions")?.append(progress);
      }

      if (submitButton instanceof HTMLButtonElement) {
        submitButton.dataset.originalText = submitButton.textContent?.trim() || "Send";
        submitButton.disabled = true;
      }

      function render() {
        const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
        if (submitButton instanceof HTMLButtonElement) {
          submitButton.textContent = compact ? "Working..." : "Working";
          submitButton.title = `Waiting for assistant reply (${elapsedSeconds}s)`;
        }
        if (progress) {
          progress.textContent = `Waiting for assistant reply... ${elapsedSeconds}s`;
        }
      }

      render();
      const interval = window.setInterval(render, 1000);
      window.addEventListener(
        "pagehide",
        () => {
          window.clearInterval(interval);
        },
        { once: true },
      );
    });
  }
}

setupChatSubmitProgress();

function setupPromptFillers(root = document) {
  const buttons = [...root.querySelectorAll("[data-fill-target]")];
  if (root instanceof Element && root.matches("[data-fill-target]")) {
    buttons.unshift(root);
  }
  for (const button of buttons) {
    if (button.dataset.fillReady === "true") {
      continue;
    }
    button.dataset.fillReady = "true";
    button.addEventListener("click", () => {
      const targetSelector = button.getAttribute("data-fill-target");
      if (!targetSelector) {
        return;
      }
      const target = document.querySelector(targetSelector);
      if (!(target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement)) {
        return;
      }
      target.value = button.getAttribute("data-fill-value") || "";
      target.dispatchEvent(new Event("input", { bubbles: true }));

      const hiddenSelector = button.getAttribute("data-fill-hidden-target");
      if (hiddenSelector) {
        const hidden = document.querySelector(hiddenSelector);
        if (hidden instanceof HTMLInputElement) {
          hidden.value = button.getAttribute("data-fill-hidden-value") || "";
        }
      }
      target.focus();
    });
  }
}

setupPromptFillers();

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

const LIVE_EVENT_LIMIT = 200;

function formatLiveTimestamp(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value || "";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  })
    .format(date)
    .replace(",", "");
}

function updateLiveStatus(panel, text, state) {
  const status = panel.querySelector("[data-live-status]");
  if (!status) {
    return;
  }
  status.textContent = text;
  status.dataset.liveState = state;
}

function setLiveOutputMode(panel, mode) {
  const rendered = panel.querySelector("[data-live-codex-transcript]");
  const raw = panel.querySelector("[data-live-codex-raw]");
  if (!rendered || !raw) {
    return;
  }
  const showRaw = mode === "raw";
  rendered.hidden = showRaw;
  raw.hidden = !showRaw;
  for (const button of panel.querySelectorAll("[data-live-output-mode]")) {
    button.setAttribute("aria-pressed", String(button.dataset.liveOutputMode === mode));
  }
}

function setupLiveOutputMode(panel) {
  if (panel.dataset.liveOutputModeReady === "true") {
    return;
  }
  panel.dataset.liveOutputModeReady = "true";
  for (const button of panel.querySelectorAll("[data-live-output-mode]")) {
    button.addEventListener("click", () => {
      setLiveOutputMode(panel, button.dataset.liveOutputMode || "rendered");
    });
  }
  setLiveOutputMode(panel, "rendered");
}

function isSafeMarkdownUrl(value) {
  try {
    const url = new URL(value, window.location.origin);
    return url.protocol === "http:" || url.protocol === "https:" || url.protocol === "mailto:";
  } catch {
    return false;
  }
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(`[^`]+`|\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|https?:\/\/[^\s<)]+)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) {
      parent.append(document.createTextNode(text.slice(cursor, match.index)));
    }
    const token = match[0];
    if (token.startsWith("`") && token.endsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else if (match[2] && match[3] && isSafeMarkdownUrl(match[3])) {
      const link = document.createElement("a");
      link.href = match[3];
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = match[2];
      parent.append(link);
    } else if (match[4]) {
      const strong = document.createElement("strong");
      strong.textContent = match[4];
      parent.append(strong);
    } else if (isSafeMarkdownUrl(token)) {
      const link = document.createElement("a");
      link.href = token;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = token;
      parent.append(link);
    } else {
      parent.append(document.createTextNode(token));
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) {
    parent.append(document.createTextNode(text.slice(cursor)));
  }
}

function markdownBlock(tagName, lines) {
  const element = document.createElement(tagName);
  appendInlineMarkdown(element, lines.join(" "));
  return element;
}

function flushMarkdownParagraph(fragment, lines) {
  if (lines.length === 0) {
    return;
  }
  fragment.append(markdownBlock("p", lines));
  lines.splice(0, lines.length);
}

function flushMarkdownList(fragment, list) {
  if (!list || list.items.length === 0) {
    return;
  }
  const element = document.createElement(list.ordered ? "ol" : "ul");
  for (const itemText of list.items) {
    const item = document.createElement("li");
    appendInlineMarkdown(item, itemText);
    element.append(item);
  }
  fragment.append(element);
}

function renderMarkdown(container, source) {
  const fragment = document.createDocumentFragment();
  const paragraph = [];
  let list = null;
  let codeFence = null;
  const lines = source.replace(/\r\n/g, "\n").split("\n");

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      flushMarkdownParagraph(fragment, paragraph);
      flushMarkdownList(fragment, list);
      list = null;
      if (codeFence) {
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = codeFence.lines.join("\n");
        pre.append(code);
        fragment.append(pre);
        codeFence = null;
      } else {
        codeFence = { lines: [] };
      }
      continue;
    }
    if (codeFence) {
      codeFence.lines.push(line);
      continue;
    }
    if (!trimmed) {
      flushMarkdownParagraph(fragment, paragraph);
      flushMarkdownList(fragment, list);
      list = null;
      continue;
    }

    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushMarkdownParagraph(fragment, paragraph);
      flushMarkdownList(fragment, list);
      list = null;
      const level = String(Math.min(heading[1].length + 2, 6));
      fragment.append(markdownBlock(`h${level}`, [heading[2]]));
      continue;
    }

    const unordered = trimmed.match(/^[-*+]\s+(.+)$/);
    const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushMarkdownParagraph(fragment, paragraph);
      const orderedList = Boolean(ordered);
      if (!list || list.ordered !== orderedList) {
        flushMarkdownList(fragment, list);
        list = { ordered: orderedList, items: [] };
      }
      list.items.push((ordered || unordered)[1]);
      continue;
    }

    flushMarkdownList(fragment, list);
    list = null;
    paragraph.push(trimmed);
  }

  flushMarkdownParagraph(fragment, paragraph);
  flushMarkdownList(fragment, list);
  if (codeFence) {
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = codeFence.lines.join("\n");
    pre.append(code);
    fragment.append(pre);
  }

  container.replaceChildren(fragment);
}

function appendLiveOutput(panel, delta) {
  if (!delta) {
    return;
  }
  const rendered = panel.querySelector("[data-live-codex-transcript]");
  const raw = panel.querySelector("[data-live-codex-raw]");
  if (!rendered || !raw) {
    return;
  }
  panel.liveCodexMarkdown = `${panel.liveCodexMarkdown || ""}${delta}`;
  raw.textContent = panel.liveCodexMarkdown;
  renderMarkdown(rendered, panel.liveCodexMarkdown);
  const empty = panel.querySelector("[data-live-output-empty]");
  if (empty) {
    empty.hidden = panel.liveCodexMarkdown.length > 0;
  }
  rendered.scrollTop = rendered.scrollHeight;
  raw.scrollTop = raw.scrollHeight;
}

function liveEventDetail(payload) {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const body = document.createElement("pre");
  summary.textContent = "Details";
  body.textContent = JSON.stringify(payload || {}, null, 2);
  details.append(summary, body);
  return details;
}

function renderLiveEvent(panel, payload, seen) {
  const source = payload.source || "event";
  const id = payload.id ?? "";
  const key = `${source}:${id}`;
  if (seen.has(key)) {
    return;
  }
  seen.add(key);
  appendLiveOutput(panel, payload.outputDelta || "");

  const list = panel.querySelector("[data-live-event-list]");
  if (!list) {
    return;
  }
  panel.querySelector("[data-live-event-empty]")?.remove();

  const item = document.createElement("li");
  item.className = `live-event is-${source}`;
  item.dataset.liveEventKey = key;

  const header = document.createElement("div");
  header.className = "live-event-header";

  const time = document.createElement("span");
  time.className = "live-event-time";
  time.textContent = formatLiveTimestamp(payload.createdAt);
  time.title = payload.createdAt || "";

  const sourceLabel = document.createElement("span");
  sourceLabel.className = "live-event-source";
  sourceLabel.textContent = source;

  const title = document.createElement("span");
  title.className = "live-event-title";
  title.textContent = payload.title || payload.eventType || "Event";

  header.append(time, sourceLabel, title);
  item.append(header);

  if (payload.message) {
    const message = document.createElement("div");
    message.className = "live-event-message";
    message.textContent = payload.message;
    item.append(message);
  }
  item.append(liveEventDetail(payload.payload));
  list.append(item);

  while (list.children.length > LIVE_EVENT_LIMIT) {
    list.firstElementChild?.remove();
  }
  const count = panel.querySelector("[data-live-event-count]");
  if (count) {
    count.textContent = String(seen.size);
  }
  list.scrollTop = list.scrollHeight;
}

function setupLiveCodexPanels(root = document) {
  const panels = [...root.querySelectorAll("[data-live-events-url]")];
  if (root instanceof Element && root.matches("[data-live-events-url]")) {
    panels.unshift(root);
  }
  for (const panel of panels) {
    if (panel.dataset.liveReady === "true") {
      continue;
    }
    panel.dataset.liveReady = "true";
    const url = panel.dataset.liveEventsUrl;
    if (!url || !window.EventSource) {
      updateLiveStatus(panel, "unavailable", "error");
      return;
    }

    setupLiveOutputMode(panel);
    const seen = new Set();
    updateLiveStatus(panel, "connecting", "connecting");
    const source = new EventSource(url);

    source.addEventListener("open", () => {
      if (!panel.querySelector("[data-live-status]")?.textContent) {
        updateLiveStatus(panel, "connected", "connected");
      }
    });
    source.addEventListener("attempt", (event) => {
      const payload = JSON.parse(event.data);
      updateLiveStatus(panel, payload.status || payload.message || "unknown", "connected");
    });
    for (const eventName of ["codex", "timeline", "error"]) {
      source.addEventListener(eventName, (event) => {
        renderLiveEvent(panel, JSON.parse(event.data), seen);
      });
    }
    source.addEventListener("error", () => {
      updateLiveStatus(panel, "reconnecting", "warning");
    });
    window.addEventListener("beforeunload", () => {
      source.close();
    });
  }
}

setupLiveCodexPanels();

document.body.addEventListener("htmx:afterSwap", (event) => {
  if (event.target.id === "board-columns") {
    setupKanbanDrag();
  }
  if (event.target.id === "dashboard-main") {
    setupKanbanDrag();
    setupMoveWorkForm();
    setupAccordions(event.target);
    setupLineNumberedEditors();
    setupWorkflowFlowchart();
    setupLiveCodexPanels(event.target);
    setupChatSubmitProgress(event.target);
    setupPromptFillers(event.target);
  }
});
