/* Durable order workflow, available only when the local server enables it. */
(async function orderWorkflow() {
  const panel = document.querySelector("#order-workflow");
  const find = (selector) => panel.querySelector(selector);
  const url = "/api/order-tasks";
  const form = find("#order-submit");
  const reconcile = find("#order-reconcile");
  const recent = find("#order-recent");
  const ledger = find("#order-ledger");
  const pendingKey = "threadline.pending-order";
  let selection = 0;
  let taskId = null;
  let poll;
  let previousRender;
  let submission;

  function remember(value) {
    submission = value;
    try {
      if (value) sessionStorage.setItem(pendingKey, JSON.stringify(value));
      else sessionStorage.removeItem(pendingKey);
    } catch {
      // Recovery within the current page still works with storage disabled.
    }
  }

  function showError(error) {
    const el = find("#order-error");
    el.textContent = error?.message || "";
    el.hidden = !error;
  }

  function render(task) {
    find("#order-task").hidden = false;
    find("#order-title").textContent = "Order " + task.order_id;
    const status = find("#order-state");
    status.textContent = task.state.replaceAll("_", " ");
    status.className = "pill " + ({complete: "ok", needs_review: "warn"}[task.state] || "info");
    for (const [id, value] of Object.entries({
      dialog: task.dialog_id, id: task.task_id, key: task.operation_key,
      attempts: task.attempts, receipt: task.receipt_id || "Awaiting receipt",
    })) {
      find("#order-" + id).textContent = value;
    }
    reconcile.hidden = task.state !== "needs_review";
    const report = task.events.map((event) => {
      const item = document.createElement("li");
      item.textContent = event.detail;
      return item;
    });
    find("#order-events").replaceChildren(...report);
  }

  function displayList(result) {
    find("#order-history").hidden = result.tasks.length === 0;
    recent.replaceChildren(...result.tasks.map((task) => {
      const option = document.createElement("option");
      option.value = task.task_id;
      option.textContent = task.order_id + " · " + task.state.replaceAll("_", " ");
      return option;
    }));
    if (taskId) recent.value = taskId;
  }

  async function loadTask(generation) {
    try {
      const task = await api("GET", url + "/" + taskId);
      if (generation !== selection) return;
      const version = task.task_id + ":" + task.events.length;
      if (version !== previousRender) {
        previousRender = version;
        render(task);
        const option = Array.from(recent.options).find((item) => item.value === task.task_id);
        if (option) option.textContent = task.order_id + " · " + task.state.replaceAll("_", " ");
      }
      showError(null);
      if (!["complete", "needs_review"].includes(task.state)) {
        poll = setTimeout(() => loadTask(generation), 450);
      }
    } catch (error) {
      if (generation === selection) {
        showError(error);
        poll = setTimeout(() => loadTask(generation), 1500);
      }
    }
  }

  function select(id) {
    clearTimeout(poll);
    taskId = id;
    recent.value = id;
    ledger.hidden = true;
    find("#order-task").hidden = true;
    reconcile.reset();
    reconcile.hidden = true;
    previousRender = null;
    loadTask(++selection);
  }

  try {
    const result = await api("GET", url);
    panel.hidden = false;
    find("#order-fault").hidden = !result.demo_faults;
    try {
      const saved = JSON.parse(sessionStorage.getItem(pendingKey));
      if (saved?.request_id && saved.request) {
        submission = saved;
        form.elements.order_id.value = saved.request.order_id;
        form.elements.policy.value = saved.request.policy;
        form.elements.drop_reply.checked = Boolean(saved.request.drop_reply && result.demo_faults);
      }
    } catch {
      // Ignore unreadable browser state.
    }
    displayList(result);
    if (result.tasks.length) select(result.tasks[0].task_id);
  } catch {
    // The hosted replay demo has no order task API.
    return;
  }

  form.onsubmit = async (event) => {
    event.preventDefault();
    const fields = form.elements;
    const request = {
      order_id: fields.order_id.value.trim(),
      policy: fields.policy.value,
      drop_reply: fields.drop_reply.checked,
    };
    const fingerprint = JSON.stringify(request);
    if (JSON.stringify(submission?.request) !== fingerprint) {
      remember({ request, request_id: crypto.randomUUID() });
    }
    const button = form.querySelector("button");
    button.disabled = true;
    showError(null);
    try {
      const task = await api("POST", url, { ...request, request_id: submission.request_id });
      remember(null);
      select(task.task_id);
      const generation = selection;
      const latest = await api("GET", url);
      if (generation === selection) displayList(latest);
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
    }
  };

  recent.onchange = () => select(recent.value);

  find("#order-evidence").onclick = async () => {
    const generation = selection;
    try {
      const result = await api("GET", url + "/" + taskId + "/evidence");
      if (generation !== selection) return;
      ledger.textContent = "Order ledger: " + result.effect_count + " replacement record(s)" +
        (result.receipt_ids.length ? " · " + result.receipt_ids.join(", ") : "");
      ledger.hidden = false;
    } catch (error) {
      if (generation === selection) showError(error);
    }
  };

  reconcile.onsubmit = async (event) => {
    event.preventDefault();
    const generation = selection;
    const button = reconcile.querySelector("button");
    button.disabled = true;
    try {
      await api("POST", url + "/" + taskId + "/reconcile",
        { receipt_id: reconcile.elements.receipt_id.value.trim() });
      if (generation === selection) loadTask(generation);
    } catch (error) {
      if (generation === selection) showError(error);
    } finally {
      button.disabled = false;
    }
  };
})();
