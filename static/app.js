(function () {
  const $ = (sel) => document.querySelector(sel);

  const state = {
    data: null,        // 来自 /api/parse 的解析结果
    chart: null,       // echarts 实例
    chartTheme: "light",
    lastResp: null,    // 最近一次 /api/chart 响应
    config: null,      // 最近一次 /api/config 响应（不含明文 key）
    pendingFile: null, // 当前正被「选 sheet」阻塞的原始 File 对象（用于二次提交）
    genController: null, // AbortController：用于「取消」按钮中断请求
  };

  // -------- Chart ----------
  function setChartTitle(text, color) {
    if (!state.chart) return;
    state.chart.setOption({
      title: {
        text,
        left: "center",
        top: "center",
        textStyle: { color: color || "#94a3b8", fontWeight: 400, fontSize: 14 },
      },
    });
  }

  function ensureChart(theme) {
    const wantDark = theme === "dark";
    if (state.chart) {
      if (state.chartTheme === wantDark) {
        // 主题没变：清空再交给 setOption 复用
        state.chart.clear();
        state.chart.hideLoading();
        return state.chart;
      }
      state.chart.dispose();
    }
    state.chartTheme = wantDark;
    state.chart = echarts.init(
      document.getElementById("chart"),
      wantDark ? "dark" : null
    );
    return state.chart;
  }

  function initChart() {
    if (state.chart) return;
    state.chart = echarts.init(
      document.getElementById("chart"),
      null,
      { renderer: "canvas" }
    );
    // 单一稳定的 resize 处理：始终操作当前 chart 实例
    window.addEventListener("resize", () => state.chart && state.chart.resize());
    setChartTitle("等待生成图表…");
  }

  // -------- Parse file/text ----------
  function buildParseFormData(opts) {
    // opts: { file?, text?, useLlm?, noHeader?, hint?, selectedSheets? }
    const fd = new FormData();
    if (opts.file) fd.append("file", opts.file);
    else if (opts.text) fd.append("text", opts.text);
    if (opts.useLlm) {
      fd.append("use_llm", "1");
      if (opts.hint) fd.append("hint", opts.hint);
    }
    if (opts.noHeader) fd.append("no_header", "1");
    if (opts.selectedSheets && opts.selectedSheets.length) {
      opts.selectedSheets.forEach((s) => fd.append("selected_sheets", s));
    }
    return fd;
  }

  function resetDataUI() {
    hideSheetPicker();
    setPreviewExpanded(false);
    const ds = $("#dataSummary");
    ds.classList.add("hidden");
    ds.innerHTML = "";
    $("#dataPreview").innerHTML = "";
    const dpa = $("#dataPreviewActions");
    if (dpa) dpa.classList.add("hidden");
    $("#btnTogglePreview").classList.add("hidden");
    state.data = null;
  }

  async function submitParse(fd, btn) {
    const oldText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "🔍 解析中…";
    try {
      const resp = await fetch("/api/parse", { method: "POST", body: fd });
      const data = await resp.json();
      handleParseResponse(data);
    } catch (e) {
      showHint("解析出错：" + e.message, true);
      state.data = null;
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }

  async function parseCurrent() {
    const file = $("#fileInput").files[0];
    const text = $("#textInput").value.trim();
    const useLlm = $("#useLlmChk").checked;
    const noHeader = $("#noHeaderChk").checked;
    const promptVal = $("#promptInput").value.trim();

    hide($("#hintMsg"));
    if (!file && !text) {
      showHint("请先选择文件或粘贴数据。", true);
      return;
    }

    resetDataUI();

    if (file) {
      state.pendingFile = file;
      $("#fileName").textContent = file.name;
    } else {
      state.pendingFile = null;
      $("#fileName").textContent = "粘贴的文本数据";
    }

    const fd = buildParseFormData({ file, text, useLlm, noHeader, hint: promptVal });
    const btn = $("#btnParse");
    if (useLlm) btn.textContent = "🧠 大模型整理中…";
    await submitParse(fd, btn);
  }

  function handleParseResponse(data) {
    if (!data) {
      showHint("解析失败：空响应", true);
      state.data = null;
      return;
    }

    // xlsx 多 sheet：弹选择器，停止后续流程
    if (data.needs_sheet_selection) {
      showSheetPicker(data.sheets || []);
      showHint("检测到多 Sheet 文件，请勾选要解析的 Sheet。", false, true);
      return;
    }

    if (data.error) {
      if (data.fallback) {
        state.data = data.fallback;
        renderDataPreview(state.data, { method: "fallback", error: data.error });
        showHint("大模型整理失败，已退回代码解析：" + data.error, true);
      } else {
        throw new Error(data.error || "解析失败");
      }
    } else {
      state.data = data;
      renderDataPreview(data, { method: data.understand_method });
      showHint(
        data.understand_method === "llm"
          ? "已用大模型整理数据，可以发送需求。"
          : "已解析数据，可以发送需求。",
        false,
        true
      );
    }
  }

  // -------- Sheet picker (多 sheet xlsx) ----------
  function showSheetPicker(sheets) {
    const list = $("#sheetList");
    list.innerHTML = "";
    sheets.forEach((s) => {
      const label = document.createElement("label");
      label.className = "sheet-item";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = s.name;
      cb.checked = true; // 默认全选
      const body = document.createElement("div");
      body.className = "sheet-item-body";
      const name = document.createElement("div");
      name.className = "sheet-item-name";
      name.textContent = s.name + (s.error ? " ⚠️" : "");
      const meta = document.createElement("div");
      meta.className = "sheet-item-meta";
      if (s.error) {
        meta.textContent = "读取失败：" + s.error;
      } else {
        meta.textContent = `${s.rows} 行 × ${s.columns.length} 列`;
      }
      const cols = document.createElement("div");
      cols.className = "sheet-item-cols";
      cols.textContent = s.columns && s.columns.length
        ? "列：" + s.columns.join("、")
        : "";
      body.appendChild(name);
      body.appendChild(meta);
      if (cols.textContent) body.appendChild(cols);
      label.appendChild(cb);
      label.appendChild(body);
      list.appendChild(label);
    });
    $("#sheetPicker").classList.remove("hidden");
  }

  function hideSheetPicker() {
    $("#sheetPicker").classList.add("hidden");
    $("#sheetList").innerHTML = "";
  }

  async function confirmSheetSelection() {
    const checks = document.querySelectorAll("#sheetList input[type=checkbox]");
    const selected = [...checks].filter((c) => c.checked).map((c) => c.value);
    if (selected.length === 0) {
      showHint("请至少勾选一个 Sheet。", true);
      return;
    }
    if (!state.pendingFile) {
      showHint("文件已失效，请重新选择文件。", true);
      hideSheetPicker();
      return;
    }
    hideSheetPicker();

    const fd = buildParseFormData({
      file: state.pendingFile,
      useLlm: $("#useLlmChk").checked,
      noHeader: $("#noHeaderChk").checked,
      hint: $("#promptInput").value.trim(),
      selectedSheets: selected,
    });
    await submitParse(fd, $("#btnParse"));
  }

  function renderDataPreview(data, understanding) {
    const preview = $("#dataPreview");
    const summaryEl = $("#dataSummary");
    const previewActions = $("#dataPreviewActions");
    const toggleBtn = $("#btnTogglePreview");
    const tag = understanding && understanding.method === "llm"
      ? '<span class="understand-tag">🧠 LLM 整理</span>'
      : (understanding && understanding.method === "fallback"
        ? '<span class="understand-tag fallback">↩ 已回退</span>'
        : "");
    const summary = data.summary ? `摘要：${data.summary}` : "";
    const notes = data.notes ? `整理说明：${data.notes}` : "";
    const errNote = understanding && understanding.error
      ? `（${understanding.error}）`
      : "";

    // 摘要行：始终显示，一行式
    summaryEl.classList.remove("hidden");
    summaryEl.innerHTML = tag + (data.description || "已解析数据") +
      (summary ? `<br>${summary}` : "") +
      (notes ? `<br>${notes}` : "") +
      (errNote ? ` ${errNote}` : "");

    // 完整预览（默认折叠，点了"展开数据"才显示）
    const schema = renderSchema(data.columns);
    preview.innerHTML =
      (data.description || "") +
      (data.summary ? `\n摘要：${data.summary}` : "") +
      (data.notes ? `\n整理说明：${data.notes}` : "") +
      (errNote ? `\n${errNote}` : "") +
      schema +
      "\n\n前 5 行：\n" +
      JSON.stringify((data.rows || []).slice(0, 5), null, 2);
    setPreviewExpanded(false);

    // 显示「展开数据」按钮
    if (previewActions) previewActions.classList.remove("hidden");
    if (toggleBtn) toggleBtn.classList.remove("hidden");
  }

  function setPreviewExpanded(expanded) {
    const preview = $("#dataPreview");
    const summaryEl = $("#dataSummary");
    const toggleBtn = $("#btnTogglePreview");
    if (expanded) {
      preview.classList.remove("hidden");
      summaryEl.classList.add("hidden");
      if (toggleBtn) toggleBtn.textContent = "📊 收起数据 ▴";
    } else {
      preview.classList.add("hidden");
      summaryEl.classList.remove("hidden");
      if (toggleBtn) toggleBtn.textContent = "📊 展开数据 ▾";
    }
  }

  function renderSchema(columns) {
    if (!Array.isArray(columns) || !columns.length) return "";
    if (typeof columns[0] === "string") {
      return "\n\n列：" + columns.join(", ");
    }
    const lines = columns.map((c) => {
      const t = c.type || "string";
      const r = c.role || "value";
      const d = c.description ? ` — ${c.description}` : "";
      return `  • ${c.name}  [${t}/${r}]${d}`;
    });
    return "\n\n数据 schema：\n" + lines.join("\n");
  }

  function clearData() {
    state.pendingFile = null;
    $("#fileInput").value = "";
    $("#textInput").value = "";
    $("#useLlmChk").checked = false;
    $("#noHeaderChk").checked = false;
    $("#fileName").textContent = "未选择文件";
    resetDataUI();
    hide($("#hintMsg"));
  }

  // -------- Send prompt to generate chart ----------
  // AbortController：用于「取消」按钮中断请求
  state.genController = null;

  function setChartStatus(text, show) {
    const el = $("#chartStatus");
    const txt = $("#chartStatusText");
    if (txt) txt.textContent = text;
    if (el) el.classList.toggle("hidden", !show);
  }

  async function generate() {
    const prompt = $("#promptInput").value.trim();
    const title = $("#titleInput").value.trim();
    const typeHint = $("#typeSel").value;
    const theme = $("#themeSel").value;

    if (!prompt && !state.data) {
      showHint("请至少输入需求或上传数据。", true);
      return;
    }

    // 取消上一次未完成的请求
    if (state.genController) {
      try { state.genController.abort(); } catch (e) {}
    }
    state.genController = new AbortController();

    ensureChart(theme);
    state.chart.showLoading({ text: "正在调用大模型…", color: "#5470c6" });
    setChartStatus("正在调用大模型生成图表，可能需要 1–3 分钟…", true);

    try {
      const body = {
        prompt,
        data: state.data || null,
        chart_type_hint: typeHint || "",
        style_hint: {
          theme,
          title,
        },
      };
      const resp = await fetch("/api/chart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: state.genController.signal,
      });
      const data = await resp.json();
      state.chart.hideLoading();
      setChartStatus("", false);

      if (!resp.ok || data.error) {
        showFallbackError(data.error || "生成失败");
        showHint(data.error || "生成失败", true);
        return;
      }

      state.lastResp = data;
      renderResponse(data);
      let msg = `已生成图表：${data.chart_type} · ${data.type_reason || ""}`;
      if (data.understanding) {
        const u = data.understanding;
        msg += u.method === "llm"
          ? " · 🧠 已用 LLM 整理数据"
          : " · ↩ 数据整理已回退到代码解析";
      }
      showHint(msg, false, true);
    } catch (e) {
      state.chart.hideLoading();
      if (e.name === "AbortError") {
        setChartStatus("已取消", false);
        showHint("已取消本次生成。", false, true);
        return;
      }
      setChartStatus("", false);
      showHint("请求失败：" + e.message, true);
    } finally {
      state.genController = null;
    }
  }

  function cancelGeneration() {
    if (state.genController) {
      try { state.genController.abort(); } catch (e) {}
    }
    if (state.chart && state.chart.hideLoading) {
      state.chart.hideLoading();
    }
    setChartStatus("", false);
    showHint("已取消本次生成。", false, true);
    setChartTitle("已取消");
  }

  function showFallbackError(msg) {
    setChartTitle(msg || "生成失败", "#c0392b");
  }

  function renderResponse(data) {
    try {
      state.chart.setOption(data.option, true);
    } catch (e) {
      showFallbackError("图表 option 渲染失败：" + e.message);
    }
    const reason = document.getElementById("typeReason");
    const reasonLine = reason.querySelector(".reason-line");
    const reasonUnd = reason.querySelector(".reason-understanding");
    let baseReason = "推荐类型：" + data.chart_type + "；理由：" + (data.type_reason || "");
    if (data.retried) {
      baseReason += ' <span class="understand-tag">🔁 已自动修正</span>';
    }
    reasonLine.innerHTML = baseReason;
    if (data.understanding) {
      const u = data.understanding;
      const tag = u.method === "llm"
        ? '<span class="understand-tag">🧠 LLM 已整理</span>'
        : '<span class="understand-tag fallback">↩ 整理回退</span>';
      const parts = [tag + " " + (u.summary || "(无摘要)")];
      if (u.notes) parts.push("整理说明：" + u.notes);
      if (u.error) parts.push("（" + u.error + "）");
      reasonUnd.innerHTML = parts.join("；");
      reasonUnd.classList.remove("hidden");
    } else {
      reasonUnd.innerHTML = "";
      reasonUnd.classList.add("hidden");
    }
    reason.classList.remove("hidden");

    document.getElementById("panel-explain").textContent = data.explanation || "(无文字解释)";
    document.getElementById("panel-option").textContent = JSON.stringify(data.option, null, 2);
    document.getElementById("panel-code").textContent = data.code || "(无代码)";
    document.getElementById("panel-raw").textContent = data.raw_reply || "";
  }

  // -------- UI helpers ----------
  function showHint(text, isError, isOk) {
    const el = document.getElementById("hintMsg");
    el.textContent = text;
    el.classList.remove("hidden", "err", "ok");
    if (isError) el.classList.add("err");
    if (isOk) el.classList.add("ok");
  }
  function hide(el) { el.classList.add("hidden"); }

  // -------- Tabs ----------
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.dataset.tab;
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      document.querySelectorAll(".tab-panel").forEach((p) => {
        p.classList.toggle("active", p.dataset.panel === name);
      });
    });
  });

  // -------- Buttons ----------
  $("#btnParse").addEventListener("click", parseCurrent);
  $("#btnClearData").addEventListener("click", clearData);
  $("#btnGen").addEventListener("click", generate);
  $("#btnCancelGen").addEventListener("click", cancelGeneration);
  $("#btnSheetsAll").addEventListener("click", () => {
    document.querySelectorAll("#sheetList input[type=checkbox]").forEach((c) => (c.checked = true));
  });
  $("#btnSheetsNone").addEventListener("click", () => {
    document.querySelectorAll("#sheetList input[type=checkbox]").forEach((c) => (c.checked = false));
  });
  $("#btnSheetsConfirm").addEventListener("click", confirmSheetSelection);
  $("#btnTogglePreview").addEventListener("click", () => {
    const preview = $("#dataPreview");
    const expanded = !preview.classList.contains("hidden");
    setPreviewExpanded(!expanded);
  });
  $("#fileInput").addEventListener("change", () => {
    const f = $("#fileInput").files[0];
    $("#fileName").textContent = f ? f.name : "未选择文件";
  });

  $("#btnSample").addEventListener("click", () => {
    $("#textInput").value =
      "月份,销售额,利润\n1月,120,22\n2月,132,28\n3月,101,19\n4月,134,30\n5月,90,15\n6月,230,55\n7月,210,50\n8月,182,40\n";
    $("#promptInput").value = "X 轴为月份，Y 轴为销售额，画出柱状图并带圆角，销售额用蓝色，同时在顶部显示标题。";
    parseCurrent();
  });

  $("#btnExport").addEventListener("click", async () => {
    if (!state.lastResp) return showHint("先生成一个图表再导出。", true);
    const opt = JSON.stringify(state.lastResp.option, null, 2);
    try {
      const html = await buildExportHtml(opt, state.lastResp.chart_type, state.chartTheme);
      const blob = new Blob([html], { type: "text/html;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `echarts-${state.lastResp.chart_type}-${Date.now()}.html`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      showHint("已导出独立 HTML（完全脱机，双击即可在任意机器打开）。", false, true);
    } catch (e) {
      showHint("导出失败：" + e.message, true);
    }
  });

  $("#btnFullscreen").addEventListener("click", () => {
    const el = document.querySelector(".chart");
    if (!document.fullscreenElement) el.requestFullscreen().then(()=>state.chart.resize());
    else document.exitFullscreen().then(()=>state.chart.resize());
  });

  // 缓存 vendor 文件内容（首次拉取），让「导出 HTML」能完全脱机
  const _vendorCache = { echarts: null, dark: null };
  async function _loadVendor(path) {
    if (_vendorCache[path]) return _vendorCache[path];
    const r = await fetch(path);
    if (!r.ok) throw new Error(`无法加载 vendor 资源：${path} (HTTP ${r.status})`);
    const text = await r.text();
    _vendorCache[path] = text;
    return text;
  }

  async function buildExportHtml(optionJSON, type, theme) {
    // 内联 ECharts 主库 + 主题，导出的 HTML 完全自包含，不依赖任何 CDN
    const echartsSrc = await _loadVendor("/static/vendor/echarts/echarts.min.js");
    const themeBlock = theme === "dark"
      ? `<script>\n${await _loadVendor("/static/vendor/echarts/dark.js")}\n<\/script>`
      : "";
    return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>ECharts · ${type}</title>
<style>html,body{margin:0;padding:0;height:100%;background:${theme==='dark'?'#0f172a':'#f5f7fb'}}#chart{width:100%;height:100vh}</style>
<script>
${echartsSrc}
<\/script>
${themeBlock}
</head>
<body>
<div id="chart"></div>
<script>
const chart = echarts.init(document.getElementById('chart')${theme==='dark'?",'dark'":''});
const option = ${optionJSON};
chart.setOption(option);
window.addEventListener('resize',()=>chart.resize());
<\/script>
</body>
</html>`;
  }

  // -------- Config modal ----------
  // 模态框逻辑：openConfigModal() 加载并显示；closeConfigModal() 隐藏。
  // 通过 history.pushState 维护 URL 状态（/config 可分享/收藏），但不触发整页刷新。
  function initConfigModal() {
    const modal = $("#configModal");
    if (!modal) return;

    // 关闭按钮 / 点击背景
    modal.querySelectorAll("[data-config-close]").forEach((el) =>
      el.addEventListener("click", closeConfigModal)
    );
    // Esc 关闭
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.classList.contains("hidden")) {
        closeConfigModal();
      }
    });

    // 显示 / 隐藏 API Key
    $("#btnToggleKey").addEventListener("click", (e) => {
      const el = $("#apiKey");
      el.type = el.type === "password" ? "text" : "password";
      e.target.textContent = el.type === "password" ? "👁 显示" : "🙈 隐藏";
    });

    // 修改 key：展开输入框
    $("#btnEditKey").addEventListener("click", () => {
      $("#apiKeyInputRow").classList.remove("hidden");
      $("#apiKeyStatus").classList.add("hidden");
      $("#apiKey").value = "";
      $("#apiKey").focus();
    });

    // 保存 / 测试
    $("#btnSave").addEventListener("click", saveConfig);
    $("#btnTest").addEventListener("click", testConnection);

    // 主区域右上角的「⚙️ 配置」按钮
    const btnConfigOpen = $("#btnConfigOpen");
    if (btnConfigOpen) {
      btnConfigOpen.addEventListener("click", openConfigModal);
    }

    // 处理浏览器前进/后退
    window.addEventListener("popstate", () => {
      if (window.location.pathname === "/config") {
        openConfigModal();
      } else {
        closeConfigModal();
      }
    });

    // 服务端标记需要自动打开（/config 直链）
    if (window.__autoOpenConfig) {
      openConfigModal();
    }
  }

  function openConfigModal() {
    const modal = $("#configModal");
    if (!modal) return;
    modal.classList.remove("hidden");
    reloadConfig();
    if (window.location.pathname !== "/config") {
      history.pushState({}, "", "/config");
    }
    setTimeout(() => $("#baseUrl") && $("#baseUrl").focus(), 50);
  }

  function closeConfigModal() {
    const modal = $("#configModal");
    if (!modal) return;
    modal.classList.add("hidden");
    const tr = $("#testResult");
    if (tr) {
      tr.classList.add("hidden");
      tr.textContent = "";
    }
    if (window.location.pathname === "/config") {
      history.pushState({}, "", "/");
    }
  }

  async function reloadConfig() {
    try {
      const r = await fetch("/api/config");
      if (!r.ok) return;
      const cfg = await r.json();
      populateConfigForm(cfg);
    } catch (e) {}
  }

  function populateConfigForm(cfg) {
    state.config = cfg;
    $("#baseUrl").value = cfg.llm_base_url || "";
    $("#model").value = cfg.llm_model || "";
    $("#sysPrompt").value = cfg.system_prompt || "";
    $("#temperature").value = parseFloat(cfg.llm_temperature || 0.7);
    $("#maxTokens").value = parseInt(cfg.llm_max_tokens || 2048, 10);
    $("#apiKey").value = "";
    updateKeyStatusUI();
  }

  function updateKeyStatusUI() {
    const cfg = state.config || {};
    const status = $("#apiKeyStatus");
    const inputRow = $("#apiKeyInputRow");
    if (cfg.llm_api_key_present) {
      status.classList.remove("hidden");
      inputRow.classList.add("hidden");
      $("#apiKeyMasked").textContent = cfg.llm_api_key_masked || "***";
    } else {
      status.classList.add("hidden");
      inputRow.classList.remove("hidden");
    }
  }

  async function saveConfig() {
    const apiKey = $("#apiKey").value.trim();
    const payload = {
      llm_base_url: $("#baseUrl").value.trim(),
      llm_model: $("#model").value.trim(),
      system_prompt: $("#sysPrompt").value.trim(),
      llm_temperature: parseFloat($("#temperature").value) || 0.7,
      llm_max_tokens: parseInt($("#maxTokens").value, 10) || 2048,
    };
    if (apiKey) payload.llm_api_key = apiKey;

    if (!payload.llm_base_url) { showConfigHint("请填写 Base URL。", true); return; }
    if (!payload.llm_model)    { showConfigHint("请填写模型名称。", true); return; }
    if (!apiKey && !(state.config && state.config.llm_api_key_present)) {
      showConfigHint("请填写 API Key。", true);
      return;
    }

    try {
      const r = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        showConfigHint("保存失败：" + (data.error || "未知错误"), true);
        return;
      }
      showConfigHint("✓ 配置已保存", false, true);
      await reloadConfig();
      setTimeout(closeConfigModal, 1200);
    } catch (e) {
      showConfigHint("保存失败：" + e.message, true);
    }
  }

  async function testConnection() {
    showConfigHint("测试中…", false);
    const payload = {
      llm_base_url: $("#baseUrl").value.trim(),
      llm_model: $("#model").value.trim(),
      llm_temperature: parseFloat($("#temperature").value),
      llm_max_tokens: parseInt($("#maxTokens").value, 10),
    };
    const apiKey = $("#apiKey").value.trim();
    if (apiKey) payload.llm_api_key = apiKey;

    try {
      const r = await fetch("/api/config/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok) {
        showConfigHint("连接失败：" + (data.message || data.error || "未知错误"), true);
        return;
      }
      showConfigHint("✓ 连接成功！模型回复：" + (data.reply || ""), false, true);
    } catch (e) {
      showConfigHint("连接失败：" + e.message, true);
    }
  }

  function showConfigHint(text, isError, isOk) {
    const el = $("#testResult");
    if (!el) return;
    el.textContent = text;
    el.classList.remove("hidden", "err", "ok");
    if (isError) el.classList.add("err");
    if (isOk) el.classList.add("ok");
  }

  // On load
  initChart();
  initConfigModal();
})();
