(function () {
  const settingsFormEl = document.getElementById("settingsForm");
  const settingsMessageEl = document.getElementById("settingsMessage");
  const healthRefreshBtnEl = document.getElementById("healthRefreshBtn");
  const healthGridEl = document.getElementById("healthGrid");
  const healthMetaEl = document.getElementById("healthMeta");
  const cpaImportFormEl = document.getElementById("cpaImportForm");
  const cpaImportMessageEl = document.getElementById("cpaImportMessage");
  const cpaImportOutputEl = document.getElementById("cpaImportOutput");

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


  function formatDefaultMailDomains(defaults) {
    const domains = Array.isArray(defaults.temp_mail_domains) ? defaults.temp_mail_domains.filter(Boolean) : [];
    return domains.length ? domains.join(", ") : (defaults.temp_mail_domain || "");
  }

  function healthClass(ok) {
    return ok ? "health-pill health-ok" : "health-pill health-bad";
  }

  function renderHealth(data) {
    const items = data.items || [];
    healthMetaEl.textContent = `最近检测时间 ${data.checked_at || "-"}`;
    if (!items.length) {
      healthGridEl.innerHTML = '<div class="empty">暂无健康检查结果</div>';
      return;
    }
    healthGridEl.innerHTML = items.map((item) => `
      <div class="health-card">
        <div class="task-row">
          <strong>${escapeHtml(item.label)}</strong>
          <span class="${healthClass(item.ok)}">${item.ok ? "正常" : "异常"}</span>
        </div>
        <div class="health-summary">${escapeHtml(item.summary || "-")}</div>
        <div class="health-target">${escapeHtml(item.target || "-")}</div>
        <div class="health-detail">${escapeHtml(item.detail || "-")}</div>
      </div>
    `).join("");
  }

  function setDefaults() {
    const defaults = window.__DEFAULTS__ || {};
    settingsFormEl.elements.proxy.value = defaults.proxy || "";
    settingsFormEl.elements.browser_proxy.value = defaults.browser_proxy || "";
    settingsFormEl.elements.temp_mail_api_base.value = defaults.temp_mail_api_base || "";
    settingsFormEl.elements.temp_mail_admin_password.value = defaults.temp_mail_admin_password ? "" : "";
    settingsFormEl.elements.temp_mail_domain.value = formatDefaultMailDomains(defaults);
    settingsFormEl.elements.temp_mail_site_password.value = defaults.temp_mail_site_password ? "" : "";
    settingsFormEl.elements.hm_url.value = defaults.hm?.url || "";
    settingsFormEl.elements.hm_admin_password.value = "";
    settingsFormEl.elements.hm_probe_model.value = defaults.hm?.probe_model || "grok-4.5";
    settingsFormEl.elements.cpa_url.value = defaults.cpa?.url || "";
    settingsFormEl.elements.cpa_management_key.value = "";
  }

  async function refreshHealth() {
    try {
      healthMetaEl.textContent = "检测中...";
      const data = await fetchJson("/api/health");
      renderHealth(data);
    } catch (error) {
      healthMetaEl.textContent = `检测失败: ${error.message}`;
      healthGridEl.innerHTML = '<div class="empty">健康检查失败</div>';
    }
  }

  settingsFormEl.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      proxy: settingsFormEl.elements.proxy.value.trim(),
      browser_proxy: settingsFormEl.elements.browser_proxy.value.trim(),
      temp_mail_api_base: settingsFormEl.elements.temp_mail_api_base.value.trim(),
      temp_mail_admin_password: settingsFormEl.elements.temp_mail_admin_password.value.trim(),
      temp_mail_domain: settingsFormEl.elements.temp_mail_domain.value.trim(),
      temp_mail_site_password: settingsFormEl.elements.temp_mail_site_password.value.trim(),
      hm_url: settingsFormEl.elements.hm_url.value.trim(),
      hm_admin_password: settingsFormEl.elements.hm_admin_password.value.trim(),
      hm_probe_model: settingsFormEl.elements.hm_probe_model.value.trim() || "grok-4.5",
      cpa_url: settingsFormEl.elements.cpa_url.value.trim(),
      cpa_management_key: settingsFormEl.elements.cpa_management_key.value.trim(),
    };
    try {
      const data = await fetchJson("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      window.__DEFAULTS__ = data.defaults || window.__DEFAULTS__;
      settingsFormEl.elements.cpa_management_key.value = "";
      settingsFormEl.elements.temp_mail_admin_password.value = "";
      settingsFormEl.elements.temp_mail_site_password.value = "";
      settingsFormEl.elements.hm_admin_password.value = "";
      settingsMessageEl.textContent = "设置已保存";
      settingsMessageEl.className = "form-message success";
      await refreshHealth();
    } catch (error) {
      settingsMessageEl.textContent = error.message;
      settingsMessageEl.className = "form-message error";
    }
  });

  if (cpaImportFormEl) {
    cpaImportFormEl.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = {
        sso_text: cpaImportFormEl.elements.sso_text.value,
        workers: Number(cpaImportFormEl.elements.workers.value || 2),
        backup: false,
        dry_run: cpaImportFormEl.elements.dry_run.checked,
        remote_import: true,
      };
      cpaImportMessageEl.textContent = "正在转换并导入，请稍候…";
      cpaImportMessageEl.className = "form-message";
      cpaImportOutputEl.textContent = "运行中（不会显示 SSO/token）";
      try {
        const data = await fetchJson("/api/cpa/import-sso", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        cpaImportMessageEl.textContent = `完成：成功 ${data.success || 0} / 总数 ${data.total || 0}，失败 ${data.failed || 0}`;
        cpaImportMessageEl.className = data.failed ? "form-message error" : "form-message success";
        cpaImportFormEl.elements.sso_text.value = "";
        cpaImportOutputEl.textContent = JSON.stringify({
          ok: data.ok,
          total: data.total,
          success: data.success,
          failed: data.failed,
          cpa_url: data.cpa_url,
          remote_import: data.remote_import,
          dry_run: data.dry_run,
          results: data.results,
        }, null, 2);
      } catch (error) {
        cpaImportMessageEl.textContent = error.message;
        cpaImportMessageEl.className = "form-message error";
        cpaImportOutputEl.textContent = error.message;
      }
    });
  }

  healthRefreshBtnEl.addEventListener("click", refreshHealth);
  setDefaults();
  refreshHealth();
})();
