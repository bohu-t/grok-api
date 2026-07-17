(function () {
  const form = document.getElementById("loginForm");
  const msg = document.getElementById("loginMessage");
  if (!form) return;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    msg.textContent = "处理中...";
    msg.className = "form-message";
    try {
      const body = new URLSearchParams(new FormData(form));
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
      let data = {};
      try { data = await response.json(); } catch (_) {}
      if (!response.ok) throw new Error(data.detail || "登录失败");
      window.location.href = "/";
    } catch (error) {
      msg.textContent = error.message;
      msg.className = "form-message error";
    }
  });
})();
