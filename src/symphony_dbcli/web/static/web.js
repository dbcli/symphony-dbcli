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

function setupKanbanDrag() {
  if (!window.Sortable) {
    return;
  }
  const lists = [...document.querySelectorAll(".kanban-list[data-state]")].filter(
    (list) => list.dataset.state !== "backlog",
  );
  for (const list of lists) {
    window.Sortable.create(list, {
      group: "work-items",
      animation: 120,
      draggable: "[data-work-item-id]",
      ghostClass: "is-dragging",
      onEnd: async (event) => {
        const workItemId = event.item.dataset.workItemId;
        const targetState = event.to.dataset.state;
        const previousState = event.from.dataset.state;
        if (!workItemId || !targetState || targetState === previousState) {
          return;
        }
        if (previousState === "in_review" && targetState === "in_progress") {
          window.location.href = `/work-items/${workItemId}#move-work`;
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
