(function () {
  const $ = (sel) => document.querySelector(sel);

  // Load existing config
  async function loadConfig() {
    try {
      const resp = await fetch("/api/config", { method: "GET" });
      const cfg = await resp.json();
      $("#baseUrl").value = cfg.llm_base_url || "";
      $("#model").value = cfg.llm_model || "";
      $("#sysPrompt").value = cfg.system_prompt || "";
      $("#temperature").value = parseFloat(cfg.llm_temperature || 0.7);
      $("#maxTokens").value = parseInt(cfg.llm_max_tokens || 2048, 10);
    } catch (e) {
      // silently ignore — first run
    }
  }

  async function saveConfig() {
    const payload = {
      llm_base_url: $("#baseUrl").value.trim(),
      llm_api_key: $("#apiKey").value.trim(),
      llm_model: $("#model").value.trim(),
      system_prompt: $("#sysPrompt").value.trim(),
      llm_temperature: parseFloat($("#temperature").value) || 0.7,
      llm_max_tokens: parseInt($("#maxTokens").value, 10) || 2048,
    };
    if (!payload.llm_base_url || !payload.llm_api_key || !payload.llm_model) {
      showHint("请填写 Base URL、API Key、模型名称。", true);
      return;
    }
    const resp = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      showHint("保存失败：" + (data.error || "未知错误"), true);
      return;
    }
    showHint("配置已保存。", false, true);
  }

  async function testConnection() {
    showHint("测试中…", false);
    try {
      const resp = await fetch("/api/config/test", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) {
        showHint("连接失败：" + (data.message || data.error || "未知"), true);
        return;
      }
      showHint("连接成功！模型回复：" + (data.reply || ""), false, true);
    } catch (e) {
      showHint("连接失败：" + e.message, true);
    }
  }

  function showHint(text, isError, isOk) {
    const el = $("#testResult");
    el.textContent = text;
    el.classList.remove("hidden", "err", "ok");
    if (isError) el.classList.add("err");
    if (isOk) el.classList.add("ok");
  }

  $("#btnSave").addEventListener("click", saveConfig);
  $("#btnTest").addEventListener("click", testConnection);

  $("#btnToggleKey").addEventListener("click", (e) => {
    const el = $("#apiKey");
    el.type = el.type === "password" ? "text" : "password";
    e.target.textContent = el.type === "password" ? "👁 显示" : "🙈 隐藏";
  });

  loadConfig();
})();
