/* ── Operations (Phase 30) ───────────────────────────────────
 *
 * The persistent board is rendered ENTIRELY from /operations, which reads the
 * service's stored state. Every control posts to the route that performs that
 * exact change and then re-reads the board; nothing here flips a badge locally,
 * so a failed request never leaves the UI claiming something happened.
 * ──────────────────────────────────────────────────────────── */

let operationsMuted = false;

function operationsStatusText(data) {
  const report = (data && data.report) || {};
  const bits = [
    `${report.objectives || 0} objectives`,
    `${report.active || 0} active`,
    `${report.tasks || 0} tasks`,
    `${report.ready || 0} ready`,
    `${report.waiting_approval || 0} awaiting approval`,
    `${report.failed || 0} failed`,
    `${report.blocked || 0} blocked`,
  ];
  const tick = data && data.ticker_running ? "background tick running" : "background tick idle";
  return `${bits.join(" · ")} · ${tick}`;
}

function renderOperationsStatus(data) {
  const target = byId("operationsStatus");
  if (!target) return;
  const notifications = (data && data.notifications) || {};
  target.textContent = `${operationsStatusText(data)} · ${notifications.unread || 0} unread notification(s)`;
}

function operationsButton(label, action, objectiveId, taskId, tone) {
  const button = el("button", `cyber-btn small${tone ? ` ${tone}` : ""}`, label);
  button.type = "button";
  button.dataset.opAction = action;
  button.dataset.objectiveId = objectiveId;
  if (taskId) button.dataset.taskId = taskId;
  button.id = `op-${action}-${(taskId || objectiveId || "").slice(0, 8)}`;
  return button;
}

function operationsBadge(text, state) {
  const badge = el("span", "objective-badge", text);
  badge.dataset.state = state || "";
  return badge;
}

function operationsTaskRow(objective, task) {
  const row = el("div", "objective-task");
  row.dataset.state = task.state || "";
  const head = el("div", "objective-task-head");
  head.appendChild(el("strong", "", task.title));
  head.appendChild(operationsBadge(task.state, task.state));
  if (task.sensitive) head.appendChild(operationsBadge("sensitive", "sensitive"));
  if (task.verified) head.appendChild(operationsBadge("verified", "verified"));
  row.appendChild(head);

  const meta = [];
  if (task.agent) meta.push(`agent: ${task.agent}`);
  if (task.due_at) meta.push(`due ${task.due_at}`);
  if (task.pending_steps && task.pending_steps.length) {
    meta.push(`pending: ${task.pending_steps.join(", ")}`);
  }
  if (task.completed_steps && task.completed_steps.length) {
    meta.push(`verified steps: ${task.completed_steps.length}/${(task.steps || []).length}`);
  }
  if (task.error) meta.push(task.error);
  if (meta.length) row.appendChild(el("p", "muted", meta.join(" · ")));

  const actions = el("div", "button-row");
  if (task.state === "ready" || task.state === "pending") {
    actions.appendChild(
      operationsButton("Run", "task-run", objective.id, task.id, "primary")
    );
  }
  if (task.state === "waiting_approval") {
    actions.appendChild(
      operationsButton("Approve", "task-approve", objective.id, task.id, "primary")
    );
    actions.appendChild(operationsButton("Reject", "task-reject", objective.id, task.id));
  }
  if (task.state === "failed" || task.state === "blocked") {
    actions.appendChild(
      operationsButton("Retry", "task-retry", objective.id, task.id, "primary")
    );
  }
  if (task.state !== "completed" && task.state !== "cancelled") {
    actions.appendChild(operationsButton("Cancel", "task-cancel", objective.id, task.id));
  }
  if (actions.childNodes.length) row.appendChild(actions);
  return row;
}

function operationsObjectiveCard(objective) {
  const card = el("article", "surface objective-card");
  const head = el("div", "objective-card-head");
  const title = el("div");
  title.appendChild(el("h3", "", objective.title));
  title.appendChild(el("p", "muted", objective.outcome));
  head.appendChild(title);
  head.appendChild(operationsBadge(objective.status, objective.status));
  card.appendChild(head);

  const percent = Math.round(Number(objective.progress || 0) * 100);
  const counts = objective.counts || {};
  card.appendChild(
    el(
      "p",
      "objective-progress-text",
      `${percent}% verified · ${counts.completed || 0} complete · ${counts.ready || 0} ready · ` +
        `${counts.waiting_approval || 0} awaiting approval · ${counts.failed || 0} failed`
    )
  );

  const facts = [`autonomy: ${objective.autonomy}`, `priority: ${objective.priority}`];
  if (objective.deadline) facts.push(`deadline: ${objective.deadline}`);
  if (objective.next_due) facts.push(`next due: ${objective.next_due}`);
  card.appendChild(el("p", "muted", facts.join(" · ")));

  if (objective.milestones && objective.milestones.length) {
    const milestones = el("div", "objective-milestones");
    objective.milestones.forEach((milestone) => {
      milestones.appendChild(
        el(
          "p",
          "muted",
          `${milestone.verified_at ? "✓" : "○"} ${milestone.title}` +
            (milestone.due_at ? ` — due ${milestone.due_at}` : "")
        )
      );
    });
    card.appendChild(milestones);
  }

  const tasks = el("div", "objective-tasks");
  (objective.tasks || []).forEach((task) => tasks.appendChild(operationsTaskRow(objective, task)));
  if (!(objective.tasks || []).length) {
    tasks.appendChild(el("p", "muted", "No tasks yet. Add the first step below."));
  }
  card.appendChild(tasks);

  const controls = el("div", "button-row objective-controls");
  controls.appendChild(operationsButton("Run objective", "run", objective.id, "", "primary"));
  if (objective.status === "active") {
    controls.appendChild(operationsButton("Pause", "pause", objective.id));
  }
  if (objective.status === "paused") {
    controls.appendChild(operationsButton("Resume", "resume", objective.id, "", "primary"));
  }
  if (objective.status !== "completed" && objective.status !== "cancelled") {
    controls.appendChild(operationsButton("Cancel objective", "cancel", objective.id));
  }
  const priority = document.createElement("input");
  priority.type = "number";
  priority.min = "0";
  priority.max = "100";
  priority.value = String(objective.priority);
  priority.className = "cyber-input objective-priority";
  priority.dataset.objectivePriority = objective.id;
  priority.setAttribute("aria-label", `Priority for ${objective.title}`);
  controls.appendChild(priority);
  controls.appendChild(operationsButton("Prioritize", "prioritize", objective.id));
  controls.appendChild(operationsButton("Change deadline", "deadline", objective.id));
  card.appendChild(controls);

  const addTask = el("details", "objective-add-task");
  addTask.appendChild(el("summary", "", "Add a task"));
  addTask.appendChild(operationsTaskForm(objective));
  card.appendChild(addTask);
  return card;
}

function operationsTaskForm(objective) {
  const form = el("div", "objective-task-form");
  const title = document.createElement("input");
  title.type = "text";
  title.placeholder = "Task title";
  title.className = "cyber-input";
  title.dataset.objectiveTaskTitle = objective.id;
  const instruction = document.createElement("input");
  instruction.type = "text";
  instruction.placeholder = "What should NovaControl actually do?";
  instruction.className = "cyber-input";
  instruction.dataset.objectiveTaskInstruction = objective.id;
  const due = document.createElement("input");
  due.type = "text";
  due.placeholder = "Due (ISO, optional)";
  due.className = "cyber-input";
  due.dataset.objectiveTaskDue = objective.id;
  const sensitive = document.createElement("input");
  sensitive.type = "checkbox";
  sensitive.id = `objective-task-sensitive-${objective.id.slice(0, 8)}`;
  const sensitiveLabel = el("label", "muted", " Requires approval (sensitive)");
  sensitiveLabel.prepend(sensitive);
  form.append(
    title,
    instruction,
    due,
    sensitiveLabel,
    operationsButton("Add task", "task-add", objective.id)
  );
  return form;
}

// The single renderer entry the backend route-consumer registry names. The
// board and the notification rail both render from this one payload reader.
function renderOperations(data) {
  renderOperationsStatus(data);
  renderOperationsBoard(data);
}

function renderOperationsBoard(data) {
  const list = byId("objectiveList");
  if (!list) return;
  clearNode(list);
  const objectives = (data && data.objectives) || [];
  if (!objectives.length) {
    const empty = el("p", "muted", "No persistent objectives yet. Create one above; it survives a restart.");
    empty.classList.add("empty-state");
    list.appendChild(empty);
    return;
  }
  objectives.forEach((objective) => list.appendChild(operationsObjectiveCard(objective)));
}

function renderOperationsNotifications(data) {
  const target = byId("operationsNotifications");
  if (!target) return;
  clearNode(target);
  const rows = (data && data.notifications) || [];
  const preferences = (data && data.preferences) || {};
  operationsMuted = Boolean(preferences.muted);
  const mute = byId("operationsMuteButton");
  if (mute) mute.textContent = operationsMuted ? "Unmute notifications" : "Mute notifications";
  if (!rows.length) {
    target.appendChild(el("p", "muted", "No notifications. A quiet board is a real answer, not a missing one."));
    return;
  }
  rows.forEach((notification) => {
    const row = el("div", "objective-notification");
    row.dataset.severity = notification.severity || "info";
    const head = el("div", "objective-notification-head");
    head.appendChild(el("strong", "", notification.title));
    head.appendChild(operationsBadge(notification.kind, notification.kind));
    if (!notification.read) head.appendChild(operationsBadge("unread", "unread"));
    row.appendChild(head);
    row.appendChild(el("p", "muted", notification.body));
    row.appendChild(el("p", "muted", notification.created_at));
    if (!notification.read) {
      const button = el("button", "cyber-btn small", "Mark read");
      button.type = "button";
      button.dataset.notificationRead = notification.id;
      button.id = `op-notification-${notification.id.slice(0, 8)}`;
      row.appendChild(button);
    }
    target.appendChild(row);
  });
}

async function refreshOperations() {
  try {
    const [status, notifications] = await Promise.all([
      requestJson("/operations"),
      requestJson("/operations/notifications?limit=25"),
    ]);
    renderOperations(status);
    renderOperationsNotifications(notifications);
  } catch (error) {
    const target = byId("operationsStatus");
    if (target) target.textContent = `Could not read the board: ${String(error.message || error)}`;
  }
}

function operationsAction(button, path, payload) {
  return run("objectiveOutput", "generic", () => requestJson(path, payload), button.id, {
    loadingText: "Updating the operations board...",
    successToast: "Operations updated",
    onSuccess: () => refreshOperations(),
  });
}

async function operationsHandleClick(event) {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const readButton = target.closest("[data-notification-read]");
  if (readButton) {
    await operationsAction(readButton, "/operations/notifications/read", {
      id: readButton.dataset.notificationRead,
    });
    return;
  }
  const button = target.closest("[data-op-action]");
  if (!button) return;
  const action = button.dataset.opAction;
  const objectiveId = button.dataset.objectiveId;
  const taskId = button.dataset.taskId || "";
  if (action === "run") {
    await operationsAction(button, "/objectives/run", { objective_id: objectiveId });
    return;
  }
  if (action === "pause" || action === "resume" || action === "cancel") {
    await operationsAction(button, "/objectives/control", {
      objective_id: objectiveId,
      action,
      reason: action === "cancel" ? "cancelled from the Operations board" : "",
    });
    return;
  }
  if (action === "prioritize") {
    const input = document.querySelector(`[data-objective-priority="${objectiveId}"]`);
    await operationsAction(button, "/objectives/control", {
      objective_id: objectiveId,
      action: "prioritize",
      priority: Number(input ? input.value : 50),
    });
    return;
  }
  if (action === "deadline") {
    const value = window.prompt("New deadline (ISO 8601, empty clears it)", "");
    if (value === null) return;
    await operationsAction(button, "/objectives/control", {
      objective_id: objectiveId,
      action: "deadline",
      deadline: value.trim(),
    });
    return;
  }
  if (action === "task-run") {
    await operationsAction(button, "/objectives/run", { objective_id: objectiveId });
    return;
  }
  if (action.startsWith("task-")) {
    await operationsAction(button, "/objectives/tasks/decide", {
      objective_id: objectiveId,
      task_id: taskId,
      action: action.slice(5),
      reason: action === "task-reject" ? "rejected from the Operations board" : "",
    });
    return;
  }
  if (action === "task-add") {
    const title = document.querySelector(`[data-objective-task-title="${objectiveId}"]`);
    const instruction = document.querySelector(`[data-objective-task-instruction="${objectiveId}"]`);
    const due = document.querySelector(`[data-objective-task-due="${objectiveId}"]`);
    const sensitive = byId(`objective-task-sensitive-${objectiveId.slice(0, 8)}`);
    const taskTitle = title ? title.value.trim() : "";
    const taskInstruction = instruction ? instruction.value.trim() : "";
    if (!taskTitle || !taskInstruction) {
      showToast("A task needs a title and the instruction to run");
      return;
    }
    await operationsAction(button, "/objectives/tasks", {
      objective_id: objectiveId,
      title: taskTitle,
      instruction: taskInstruction,
      due_at: due && due.value.trim() ? due.value.trim() : null,
      sensitive: Boolean(sensitive && sensitive.checked),
    });
  }
}

function setupOperations() {
  const create = byId("objectiveCreateButton");
  if (!create) return;
  create.addEventListener("click", () => {
    const title = byId("objectiveTitle").value.trim();
    const outcome = byId("objectiveOutcome").value.trim();
    if (!title || !outcome) {
      showToast("An objective needs a title and the outcome it is trying to achieve");
      return;
    }
    const deadline = byId("objectiveDeadline").value.trim();
    operationsAction(create, "/objectives", {
      title,
      outcome,
      kind: byId("objectiveKind").value,
      autonomy: byId("objectiveAutonomy").value,
      priority: Number(byId("objectivePriority").value || 50),
      deadline: deadline || null,
    }).then(() => {
      byId("objectiveTitle").value = "";
      byId("objectiveOutcome").value = "";
      byId("objectiveDeadline").value = "";
    });
  });

  const refresh = byId("operationsRefreshButton");
  if (refresh) refresh.addEventListener("click", refreshOperations);

  const tick = byId("operationsTickButton");
  if (tick) {
    tick.addEventListener("click", () =>
      operationsAction(tick, "/operations/tick", {})
    );
  }

  const mute = byId("operationsMuteButton");
  if (mute) {
    mute.addEventListener("click", () =>
      operationsAction(mute, "/operations/notifications/preferences", {
        muted: !operationsMuted,
      })
    );
  }

  const readAll = byId("operationsNotificationsReadAll");
  if (readAll) {
    readAll.addEventListener("click", () =>
      operationsAction(readAll, "/operations/notifications/read", { all: true })
    );
  }

  const clear = byId("operationsNotificationsClear");
  if (clear) {
    clear.addEventListener("click", () =>
      operationsAction(clear, "/operations/notifications/clear", {})
    );
  }

  const list = byId("objectiveList");
  if (list) list.addEventListener("click", operationsHandleClick);
  const notifications = byId("operationsNotifications");
  if (notifications) notifications.addEventListener("click", operationsHandleClick);

  const nav = document.querySelector('[data-panel="operationsPanel"]');
  if (nav) nav.addEventListener("click", refreshOperations);

  refreshOperations();
}
