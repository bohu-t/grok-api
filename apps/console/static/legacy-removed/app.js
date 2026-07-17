(function () {
  const state = { tasks: [], selectedTaskId: null };

  const taskListEl = document.getElementById("taskList");
  const detailTitleEl = document.getElementById("detailTitle");
  const detailMetaEl = document.getElementById("detailMeta");
  const detailSummaryEl = document.getElementById("detailSummary");
  const consoleOutputEl = document.getElementById("consoleOutput");
  const formEl = document.getElementById("taskForm");
  const formMessageEl = document.getElementById("formMessage");
  const stopBtnEl = document.getElementById("stopBtn");
  const refreshBtnEl = document.getElementById("refreshBtn");

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    if (response.status === 401) {
      window.location.href = "/login";
      throw new Error("Login required");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Request failed");
    return data;
  }

  function statusClass(status) {
    return `status-pill status-${status || "unknown"}`;
  }

  function cpaLabel(task) {
    const status = task.cpa_import_status || "未导入";
    if (status === "success") return `HM导入 ${task.cpa_imported_count || 0}`;
    if (status === "failed") return `已删除 ${task.cpa_import_failed_count || 0}`;
    if (status === "running") return "HM筛号中";
    if (status === "waiting_config") return "HM等待配置";
    return `HM ${status}`;
  }

  function setDefaults() {
    const defaults = window.__DEFAULTS__ || {};
    formEl.elements.name.value = `grok-task-${Date.now()}`;
    formEl.elements.count.value = defaults.run?.count || 50;
  }

  function renderTaskList() {
    if (!state.tasks.length) {
      taskListEl.innerHTML = '<div class="empty">暂无任务</div>';
      return;
    }
    taskListEl.innerHTML = state.tasks.map((task) => `
      <button class="task-card ${task.id === state.selectedTaskId ? "selected" : ""}" data-task-id="${task.id}">
        <div class="task-row">
          <strong title="${escapeHtml(task.name)}">#${task.id} ${escapeHtml(task.name)}</strong>
          <span class="${statusClass(task.status)}">${escapeHtml(task.status)}</span>
        </div>
        <div class="task-subrow">目标 ${task.target_count} · 成功 ${task.completed_count} · 失败 ${task.failed_count}</div>
        <div class="task-subrow">${escapeHtml(cpaLabel(task))}</div>
        <div class="task-actions">
          <span class="task-action-hint">点击查看日志</span>
          <button class="button button-danger button-small" type="button" data-delete-task-id="${task.id}">删除</button>
        </div>
      </button>
    `).join("");

    taskListEl.querySelectorAll("[data-task-id]").forEach((button) => {
      button.addEventListener("click", () => {
        state.selectedTaskId = Number(button.dataset.taskId);
        renderTaskList();
        refreshDetail();
      });
    });

    taskListEl.querySelectorAll("[data-delete-task-id]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const taskId = Number(button.dataset.deleteTaskId);
        if (!window.confirm(`确认删除任务 #${taskId} 吗？`)) return;
        try {
          await fetchJson(`/api/tasks/${taskId}`, { method: "DELETE" });
          if (state.selectedTaskId === taskId) state.selectedTaskId = null;
          await refreshAll();
        } catch (error) {
          formMessageEl.textContent = error.message;
          formMessageEl.className = "form-message error";
        }
      });
    });
  }

  function summaryItem(label, value) {
    return `<div class="summary-item"><div class="meta-item-label">${escapeHtml(label)}</div><div class="meta-item-value">${escapeHtml(value)}</div></div>`;
  }


  function formatMailDomains(cfg) {
    const domains = Array.isArray(cfg.temp_mail_domains) ? cfg.temp_mail_domains.filter(Boolean) : [];
    return domains.length ? domains.join(", ") : (cfg.temp_mail_domain || "-");
  }

  function metaItem(label, value) {
    return `<div class="meta-item"><div class="meta-item-label">${escapeHtml(label)}</div><div class="meta-item-value">${escapeHtml(value || "-")}</div></div>`;
  }

  function renderTaskDetail(task) {
    detailTitleEl.textContent = `任务 #${task.id} · ${task.name}`;
    stopBtnEl.disabled = !["queued", "running", "stopping"].includes(task.status);
    detailSummaryEl.innerHTML = [
      ["状态", task.status],
      ["目标", task.target_count],
      ["成功", task.completed_count],
      ["失败", task.failed_count],
      ["当前轮次", task.current_round],
      ["阶段", task.current_phase || "-"],
      ["HM 筛号", task.cpa_import_status || "-"],
      ["HM导入", task.cpa_imported_count || 0],
      ["已删除", task.cpa_import_failed_count || 0],
    ].map(([label, value]) => summaryItem(label, value)).join("");

    const cfg = task.config || {};
    detailMetaEl.innerHTML = [
      ["最近邮箱", task.last_email],
      ["最近错误", task.last_error],
      ["HM 筛号时间", task.cpa_import_at],
      ["HM 筛号结果", task.cpa_import_last_error],
      ["浏览器代理", cfg.browser_proxy],
      ["请求代理", cfg.proxy],
      ["邮箱 API", cfg.temp_mail_api_base],
      ["邮箱域名", formatMailDomains(cfg)],
      ["创建时间", task.created_at],
      ["开始时间", task.started_at],
      ["结束时间", task.finished_at],
      ["PID", task.pid],
    ].map(([label, value]) => metaItem(label, value)).join("") + `
      <div class="meta-item meta-item-action">
        <div class="meta-item-label">HM 操作</div>
        <button class="button button-small" type="button" data-import-cpa-task="${task.id}">重新导入 HM 筛号</button>
      </div>
    `;
  }

  async function refreshTasks() {
    const data = await fetchJson("/api/tasks");
    state.tasks = data.tasks || [];
    if (!state.selectedTaskId && state.tasks.length) state.selectedTaskId = state.tasks[0].id;
    if (state.selectedTaskId && !state.tasks.some((task) => task.id === state.selectedTaskId)) {
      state.selectedTaskId = state.tasks[0]?.id || null;
    }
    renderTaskList();
  }

  async function refreshDetail() {
    if (!state.selectedTaskId) {
      detailTitleEl.textContent = "实时日志";
      detailSummaryEl.innerHTML = "";
      detailMetaEl.innerHTML = "";
      consoleOutputEl.textContent = "选择任务后显示输出";
      return;
    }
    const taskData = await fetchJson(`/api/tasks/${state.selectedTaskId}`);
    renderTaskDetail(taskData.task);
    const logData = await fetchJson(`/api/tasks/${state.selectedTaskId}/logs?limit=350`);
    consoleOutputEl.innerHTML = escapeHtml((logData.lines || []).join("\n"));
    consoleOutputEl.scrollTop = consoleOutputEl.scrollHeight;
  }

  async function refreshAll() {
    try {
      await refreshTasks();
      await refreshDetail();
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    }
  }

  formEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      name: formEl.elements.name.value.trim(),
      count: Number(formEl.elements.count.value),
    };
    try {
      const data = await fetchJson("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.selectedTaskId = data.task.id;
      formMessageEl.textContent = `任务 #${data.task.id} 已创建`;
      formMessageEl.className = "form-message success";
      formEl.elements.name.value = `grok-task-${Date.now()}`;
      await refreshAll();
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    }
  });

  stopBtnEl.addEventListener("click", async () => {
    if (!state.selectedTaskId) return;
    try {
      await fetchJson(`/api/tasks/${state.selectedTaskId}/stop`, { method: "POST" });
      await refreshAll();
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    }
  });

  refreshBtnEl.addEventListener("click", refreshAll);

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-import-cpa-task]");
    if (!button) return;
    const taskId = Number(button.dataset.importCpaTask);
    button.disabled = true;
    button.textContent = "筛号中...";
    try {
      const data = await fetchJson(`/api/tasks/${taskId}/import-cpa`, { method: "POST" });
      renderTaskDetail(data.task);
      await refreshTasks();
    } catch (error) {
      formMessageEl.textContent = error.message;
      formMessageEl.className = "form-message error";
    } finally {
      button.disabled = false;
      button.textContent = "重新导入 HM 筛号";
    }
  });

  setDefaults();
  refreshAll();
  window.setInterval(refreshAll, 2000);
})();
