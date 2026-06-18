(function () {
  const $ = (sel) => document.querySelector(sel);

  // 缓存「每次生成都会读写」的 DOM 节点，避免 renderResponse 里反复 querySelector
  const $els = {};

  const state = {
    data: null,          // 来自 /api/parse 的解析结果
    chart: null,         // echarts 实例
    chartTheme: false,   // ECharts dark 主题标志：true=dark, false=light
    lastResp: null,      // 最近一次 /api/chart 响应
    config: null,        // 最近一次 /api/config 响应（不含明文 key）
    pendingFile: null,   // 当前正被「选 sheet」阻塞的原始 File 对象（用于二次提交）
    genController: null, // AbortController：用于「取消」按钮中断请求
    genStartedAt: 0,     // 当前生成开始的时间戳（用于显示耗时）
    elapsedTimer: null,  // setInterval ID：周期性刷新 chartStatus 的耗时显示
  };

  // -------- Small utilities ----------
  const hide = (el) => el && el.classList.add("hidden");
  const show = (el) => el && el.classList.remove("hidden");
  const shortText = (s, max) => {
    if (s == null) return "";
    s = String(s);
    return s.length <= max ? s : s.slice(0, max) + "…";
  };

  // localStorage 读/写工具：包装 try/catch 避免每处重复
  const lsGet = (key) => {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  };
  const lsSet = (key, value) => {
    try { localStorage.setItem(key, value); } catch (e) { /* ignore */ }
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
    setChartTitle("请上传数据 → 描述需求 → 生成图表", "#94a3b8");
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

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
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
    const summary = data.summary ? `摘要：${escapeHtml(data.summary)}` : "";
    const notes = data.notes ? `整理说明：${escapeHtml(data.notes)}` : "";
    const errNote = understanding && understanding.error
      ? `（${escapeHtml(understanding.error)}）`
      : "";

    // 摘要行：始终显示，一行式
    // 注意：data.description 由服务端代码生成，原则上可信，但仍走 escapeHtml 兜底
    //        避免上游被注入后影响前端。
    summaryEl.classList.remove("hidden");
    summaryEl.innerHTML = tag + escapeHtml(data.description || "已解析数据") +
      (summary ? `<br>${summary}` : "") +
      (notes ? `<br>${notes}` : "") +
      (errNote ? ` ${errNote}` : "");

    // 完整预览（默认折叠，点了"展开数据"才显示）
    const schema = renderSchema(data.columns);
    preview.textContent =
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
    const icon = $("#btnTogglePreviewIcon");
    const text = $("#btnTogglePreviewText");
    if (expanded) {
      preview.classList.remove("hidden");
      summaryEl.classList.add("hidden");
      if (icon) icon.textContent = "▴";
      if (text) text.textContent = "收起数据";
    } else {
      preview.classList.add("hidden");
      summaryEl.classList.remove("hidden");
      if (icon) icon.textContent = "▾";
      if (text) text.textContent = "展开数据";
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
  // 统一的请求体构造：generate() 与 generateFallback() 都要用
  function buildChartBody() {
    const prompt = $("#promptInput").value.trim();
    const title = $("#titleInput").value.trim();
    const typeHint = $("#typeSel").value;
    const theme = $("#themeSel").value;
    const wantUnderstand = $("#understandChk") && $("#understandChk").checked;
    let dataForChart = state.data;
    if (dataForChart) {
      dataForChart = { ...dataForChart };
      if (wantUnderstand) dataForChart.need_understanding = true;
      else delete dataForChart.need_understanding;
    }
    return {
      prompt,
      data: dataForChart,
      chart_type_hint: typeHint || "",
      style_hint: { theme, title },
    };
  }

  // 把生成按钮设为 disabled + 记录开始时间 + 启动顶部状态条耗时定时器
  function setGenBusy(busy) {
    $("#btnSample").disabled = busy;
    const btn = $("#btnGen");
    if (!btn) return;
    btn.disabled = busy;
    btn.textContent = busy ? "⏳ 生成中…" : "✨ 生成图表";
    if (busy) {
      state.genStartedAt = Date.now();
      updateElapsedLabel();
      if (state.elapsedTimer) clearInterval(state.elapsedTimer);
      state.elapsedTimer = setInterval(updateElapsedLabel, 1000);
    } else {
      if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
      updateElapsedLabel();  // 闪一次最终耗时
    }
  }

  function updateElapsedLabel() {
    const el = $("#chartStatusElapsed");
    if (!el) return;
    const elapsed = elapsedSinceStart();
    el.textContent = elapsed ? `(${elapsed})` : "";
  }

  function elapsedSinceStart() {
    if (!state.genStartedAt) return "";
    const sec = Math.round((Date.now() - state.genStartedAt) / 1000);
    return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${sec % 60}s`;
  }

  function setChartStatus(text, show) {
    const el = $("#chartStatus");
    const txt = $("#chartStatusText");
    if (txt) txt.textContent = text;
    if (el) el.classList.toggle("hidden", !show);
  }

  function hideChartStatus() {
    setChartStatus("", false);
  }

  // -------- Generation progress panel (streaming) ----------
  // 6 个阶段：顺序与后端 run_chart_pipeline 一致；STAGE_LABELS 是 stage → 显示文案的单一来源
  const STAGE_KEYS = ["prepare", "understand", "preprocess", "pick_type", "generate", "parse"];
  const STAGE_LABELS = {
    prepare: "数据准备",
    understand: "智能数据整理",
    preprocess: "数据预处理",
    pick_type: "选择图表类型",
    generate: "生成图表配置",
    parse: "解析与校验",
  };

  // 由 JS 动态生成 6 个 stage 的 DOM；HTML 模板只留容器，避免 6 次重复 <li>。
  function initGenStages() {
    const list = document.getElementById("genStages");
    if (!list || list.children.length) return;
    const html = STAGE_KEYS.map((key) =>
      `<li class="gen-stage" data-stage="${key}">
         <span class="gen-stage-icon">○</span>
         <div class="gen-stage-body">
           <div class="gen-stage-label">${STAGE_LABELS[key]}</div>
           <div class="gen-stage-detail"></div>
         </div>
       </li>`
    ).join("");
    list.innerHTML = html;
  }

  function resetGenPanel() {
    const panel = $("#genPanel");
    if (panel) panel.classList.add("hidden");
    document.querySelectorAll("#genStages .gen-stage").forEach((li) => {
      li.classList.remove("running", "done", "error", "skipped");
      const icon = li.querySelector(".gen-stage-icon");
      if (icon) icon.textContent = "○";
      const detail = li.querySelector(".gen-stage-detail");
      if (detail) detail.textContent = "";
    });
    const sub = $("#genPanelSub");
    if (sub) sub.textContent = "等待开始…";
    const stream = $("#genStreamText");
    if (stream) {
      stream.textContent = "";
      stream.classList.remove("live");
    }
    const streamWrap = $("#genStreamWrap");
    if (streamWrap) streamWrap.classList.add("hidden");
    const stat = $("#genStreamStat");
    if (stat) stat.textContent = "";
  }

  function showGenPanel() {
    const panel = $("#genPanel");
    if (panel) panel.classList.remove("hidden");
    // 每次显示（用户刚点"生成图表"）都强制展开，让他能看到进度
    setGenPanelCollapsed(false);
  }

  // -------- Generation progress panel: collapse / expand ----------
  // 折叠状态持久化到 localStorage；用户每次"生成图表"时都强制展开。
  const GEN_PANEL_KEY = "ea.genPanelCollapsed";

  function setGenPanelCollapsed(collapsed) {
    const panel = $("#genPanel");
    const head = $("#genPanelHead");
    const toggle = $("#genPanelToggle");
    if (!panel || !head) return;
    const next = !!collapsed;
    panel.classList.toggle("collapsed", next);
    head.setAttribute("aria-expanded", String(!next));
    if (toggle) {
      toggle.setAttribute("aria-label", next ? "展开" : "折叠");
      toggle.setAttribute("title", next ? "展开" : "折叠");
    }
    lsSet(GEN_PANEL_KEY, next ? "1" : "0");
  }

  function toggleGenPanel() {
    const head = $("#genPanelHead");
    if (!head) return;
    setGenPanelCollapsed(head.getAttribute("aria-expanded") === "true");
  }

  function initGenPanelCollapse() {
    setGenPanelCollapsed(lsGet(GEN_PANEL_KEY) === "1");
    const head = $("#genPanelHead");
    if (!head) return;
    head.addEventListener("click", toggleGenPanel);
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleGenPanel();
      }
    });
  }

  // 把后端的 "start" 状态映射到 CSS 的 .running 类
  function setStageStatus(stageName, status, detail) {
    const li = document.querySelector(`#genStages .gen-stage[data-stage="${stageName}"]`);
    if (!li) return;
    li.classList.remove("running", "done", "error", "skipped");
    const visual = status === "start" ? "running" : status;
    li.classList.add(visual);
    const icon = li.querySelector(".gen-stage-icon");
    if (icon) icon.textContent = ICON_FOR[visual] || "○";
    const det = li.querySelector(".gen-stage-detail");
    if (det) det.textContent = detail || "";
  }

  function setGenSub(text) {
    const el = $("#genPanelSub");
    if (el) el.textContent = text;
  }

  function showGenStream() {
    const wrap = $("#genStreamWrap");
    if (wrap) wrap.classList.remove("hidden");
  }

  function setGenStreamLive(live) {
    const stream = $("#genStreamText");
    if (stream) stream.classList.toggle("live", !!live);
  }

  function appendStreamText(chunk) {
    if (!chunk) return;
    const stream = $("#genStreamText");
    const wrap = $("#genStreamWrap");
    if (wrap) wrap.classList.remove("hidden");
    if (stream) {
      stream.textContent += chunk;
      stream.scrollTop = stream.scrollHeight;
    }
    const stat = $("#genStreamStat");
    if (stat && stream) stat.textContent = `${stream.textContent.length} 字符`;
  }

  async function generate() {
    const body = buildChartBody();
    if (!body.prompt && !body.data) {
      showHint("请至少输入需求或上传数据。", true);
      return;
    }

    // 取消上一次未完成的请求
    if (state.genController) {
      try { state.genController.abort(); } catch (e) {}
    }
    state.genController = new AbortController();

    ensureChart(body.style_hint.theme);
    state.chart.showLoading({ text: "正在生成图表…", color: "#5470c6" });
    setChartStatus("正在调用大模型生成图表…", true);
    setGenBusy(true);
    resetGenPanel();
    showGenPanel();
    setStageStatus("prepare", "running", "进行中");

    // 优先用 XMLHttpRequest 处理流式，它在各种代理/浏览器下比 fetch 更稳；
    // 若浏览器异常无法 XHR 再退回 fetch。
    let result;
    try {
      result = await requestStreamXhr("/api/chart/stream", JSON.stringify(body));
    } catch (e) {
      state.chart.hideLoading();
      if (e.name === "AbortError") {
        setChartStatus("已取消", false);
        showHint("已取消本次生成。", false, true);
        return;
      }
      hideChartStatus();
      showHint("请求失败：" + e.message, true);
      state.genController = null;
      setGenBusy(false);
      return;
    }

    if (!result.ok && !result.aborted) {
      // 流式失败 → 降级到一次性 JSON 接口（更适合代理环境）
      // 一次性响应是完整的，代理不会在中途断开
      const diagMsg = result.diag ? `（流式：${result.diag}）` : "";
      showHint("流式连接中断，正在重试… " + diagMsg, false);
      try {
        await generateFallback(state.genController.signal);
      } catch (fb_e) {
        state.chart.hideLoading();
        hideChartStatus();
        showHint("降级也失败：" + fb_e.message, true);
      } finally {
        state.genController = null;
        setGenBusy(false);
      }
      return;
    }
    state.genController = null;
    setGenBusy(false);
  }

  // 把成功的 done 事件渲染到主图表 + 顶部提示
  function applyDoneEvent(done) {
    if (!done) return;
    state.lastResp = done;
    renderResponse(done);
    let msg = `已生成图表：${done.chart_type} · ${done.type_reason || ""}`;
    if (done.understanding) {
      const u = done.understanding;
      if (u.method === "llm") msg += " · 🧠 已用 LLM 整理数据";
      else if (u.method === "fallback") msg += " · ↩ 数据整理已回退";
    }
    const elapsed = elapsedSinceStart();
    if (elapsed) msg += ` · 耗时 ${elapsed}`;
    showHint(msg, false, true);
    setGenSub(`完成 · 图表类型 ${done.chart_type}`);
  }

  // 用 XMLHttpRequest 处理 SSE 流：按 "\n\n" 切片 + JSON.parse
  // 返回 Promise<{ ok, errorMessage?, diag? }>
  function requestStreamXhr(url, bodyStr) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      let buffer = "";       // 累积的 responseText（XHR 一直追加）
      let tail = "";         // SSE 块的「上次残尾」，下次拼接处理（局部变量，避免全局污染）
      let done = null;       // type==done 的事件
      let errored = false;
      let lastEvt = null;
      let finished = false;

      const finalize = (result) => {
        if (finished) return;
        finished = true;
        resolve(result);
      };

      const handleEvt = (evt) => {
        if (!evt) return;
        lastEvt = evt;
        if (evt.type === "done") done = evt;
        else if (evt.type === "error") errored = true;
        handleStreamEvent(evt);
      };

      const processNewBytes = (newBytes) => {
        const pieces = newBytes.split("\n\n");
        let pending = tail + pieces.shift();   // 第一段拼上次残尾
        for (const next of pieces) {           // 中间各段都是完整的 SSE 块
          if (done || errored) break;
          handleEvt(parseSseBlock(pending));
          pending = next;
        }
        tail = pending;                        // 最后一段作为新的残尾
      };

      const finalizeWithResult = () => {
        // 把可能残留的 tail 当最后一段处理
        if (tail.trim()) {
          handleEvt(parseSseBlock(tail));
          tail = "";
        }
        state.chart.hideLoading();
        hideChartStatus();
        if (done && !errored) {
          applyDoneEvent(done);
          finalize({ ok: true });
        } else if (errored) {
          finalize({ ok: false, errorMessage: (lastEvt && lastEvt.message) || "生成失败" });
        } else if (xhr.status >= 400) {
          const msg = `生成失败 (HTTP ${xhr.status})`;
          showFallbackError(msg);
          showHint(msg, true);
          finalize({ ok: false, errorMessage: msg });
        } else {
          const msg = (lastEvt && lastEvt.message) || "生成失败：连接中断";
          showHint(msg, true);
          showFallbackError(msg);
          finalize({ ok: false, errorMessage: msg });
        }
      };

      xhr.open("POST", url, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.onprogress = () => {
        const text = xhr.responseText;
        if (!text || text.length <= buffer.length) return;
        const newBytes = text.slice(buffer.length);
        buffer = text;
        processNewBytes(newBytes);
      };
      xhr.onload = finalizeWithResult;
      xhr.onerror = () => {
        // SSE 长连接关闭时，浏览器有时会触发 onerror 而非 onload；
        // 若已收到完整 done/error，按正常路径处理
        if (done || errored) { finalizeWithResult(); return; }
        state.chart.hideLoading();
        hideChartStatus();
        const msg = "读取流失败：网络错误";
        const diag = `已接收 ${buffer.length} 字符 · status=${xhr.status}`;
        showHint(`${msg} ${diag}`, true);
        finalize({ ok: false, errorMessage: msg, diag });
      };
      xhr.ontimeout = () => {
        if (done || errored) { finalizeWithResult(); return; }
        state.chart.hideLoading();
        hideChartStatus();
        const msg = "生成超时";
        showHint(`${msg}（已接收 ${buffer.length} 字符）`, true);
        finalize({ ok: false, errorMessage: msg });
      };
      xhr.onabort = () => {
        state.chart.hideLoading();
        hideChartStatus();
        finalize({ ok: false, errorMessage: "已取消", aborted: true });
      };

      try {
        xhr.send(bodyStr);
      } catch (e) {
        finalize({ ok: false, errorMessage: e.message });
      }
    });
  }

  // 浏览器不支持流式读取时，退回到原 /api/chart 一次性 JSON 接口
  async function generateFallback(signal) {
    const body = buildChartBody();
    try {
      const resp = await fetch("/api/chart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      });
      const data = await resp.json();
      state.chart.hideLoading();
      hideChartStatus();
      if (!resp.ok || data.error) {
        showFallbackError(data.error || "生成失败");
        showHint(data.error || "生成失败", true);
        return;
      }
      // 一次性把整段 raw_reply 填进流式面板，让用户能事后看到
      markAllStagesDone();
      appendStreamText(data.raw_reply || "");
      applyDoneEvent(data);
    } catch (e) {
      state.chart.hideLoading();
      hideChartStatus();
      showHint("请求失败：" + e.message, true);
    }
  }

  // 兜底模式下没有真正的 stage 事件；显式把全部阶段标 done（用 detail 凑一句说明）
  function markAllStagesDone() {
    // 走一次性 /api/chart 时，前端拿不到 stage 事件流；
    // 只把"必有"的阶段标 done（prepare / generate / parse），其它标 skipped。
    const mark = { prepare: "done", generate: "done", parse: "done" };
    for (const k of STAGE_KEYS) {
      setStageStatus(k, mark[k] || "skipped", mark[k] ? "✓ 一次通过" : "未启用");
    }
  }

  function parseSseBlock(block) {
    if (!block) return null;
    // 按 SSE 规范，每个 event 块以 "\n\n" 结尾；我们只关心 data: 行的第一个 payload
    for (const line of block.split("\n")) {
      if (line.startsWith("data:")) {
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try { return JSON.parse(payload); } catch (e) { /* skip */ }
      }
    }
    return null;
  }

  // -------- Stage detail templates ----------
  // 每个阶段在不同 status 下显示什么细节。查表替代原来 60 行的 if/else 链。
  const ICON_FOR = { running: "●", done: "✓", skipped: "—", error: "✕" };

  const DETAIL_BY_STATUS = {
    start: {
      prepare:    () => "进行中…",
      understand: () => "调用 LLM 整理数据中…",
      preprocess: () => "解析需求中的数据处理指令…",
      pick_type:  () => "调用 LLM 推荐图表类型…",
      generate:   () => "等待模型输出…",
      parse:      () => "解析 JSON…",
    },
    skipped: {
      // 没识别到规则 / 数据用代码解析 / 用户没勾选 🧠
      preprocess: () => "未识别到数据预处理指令",
      understand: () => "未启用（数据用代码解析）",
    },
    error: (evt) => evt.message || "失败",
    done: {
      prepare:    () => "✓ 就绪",
      understand: (evt) => {
        const u = evt.understanding || {};
        if (u.method === "llm") return u.reused ? "✓ LLM 已整理（解析阶段）" : "✓ LLM 已整理数据";
        if (u.method === "fallback") return u.reused ? "↩ 解析阶段 LLM 整理失败" : "↩ 已回退到代码解析";
        return "未启用";
      },
      preprocess: (evt) => {
        const pp = evt.preprocess || {};
        const applied = (pp.applied || []).filter((a) => a && !a.skipped);
        return applied.length ? shortText(pp.summary || "已应用", 64) : "无匹配规则（已跳过）";
      },
      pick_type: (evt) => {
        const reason = evt.reason ? " · " + shortText(evt.reason, 28) : "";
        return `${evt.chart_type || ""}${reason}`;
      },
      generate: (evt) => `已生成 ${evt.length || 0} 字符`,
      parse:    (evt) => "✓ 一次通过",
    },
  };

  function pickDetail(stage, status, evt) {
    const table = DETAIL_BY_STATUS[status];
    if (!table) return "已跳过";
    // error 是顶层函数（不区分阶段），其它状态按 stage 查表
    const fn = (typeof table === "function") ? table : table[stage];
    return fn ? fn(evt || {}) : "已跳过";
  }

  function handleStreamEvent(evt) {
    const t = evt.type;
    if (t === "stage") {
      const stage = evt.stage;
      const status = evt.status;
      const detail = pickDetail(stage, status, evt);

      setStageStatus(stage, status, detail);
      setGenSub(`${STAGE_LABELS[stage] || stage} · ${statusLabel(status)}`);

      // 主生成阶段开始 → 立即展开流式输出区 + 打开输入光标
      if (stage === "generate" && status === "start") {
        showGenStream();
        setGenStreamLive(true);
      } else if (stage === "generate" && (status === "done" || status === "error")) {
        setGenStreamLive(false);
      }
    } else if (t === "delta") {
      appendStreamText(evt.content || "");
    } else if (t === "error") {
      showHint(evt.message || "生成失败", true);
      showFallbackError(evt.message || "生成失败");
      setGenStreamLive(false);
    }
    // "done" 类型由 requestStreamXhr 在 finalizeWithResult 里收集并渲染，
    // handleStreamEvent 不用处理
  }

  function statusLabel(s) {
    return ({
      start: "进行中…",
      running: "进行中…",
      done: "完成",
      error: "出错",
      skipped: "已跳过",
    })[s] || s;
  }

  function cancelGeneration() {
    if (state.genController) {
      try { state.genController.abort(); } catch (e) {}
    }
    if (state.chart && state.chart.hideLoading) {
      state.chart.hideLoading();
    }
    hideChartStatus();
    setGenBusy(false);
    resetGenPanel();
    showHint("已取消本次生成。", false, true);
    setChartTitle("已取消");
  }

  function showFallbackError(msg) {
    setChartTitle(msg || "生成失败", "#c0392b");
  }

  // 缓存一次 + 懒填充：第一次访问某个 id 时去 DOM 找
  function cachedEl(id) {
    if (!$els[id]) $els[id] = document.getElementById(id);
    return $els[id];
  }

  // -------- Sandbox (sandboxed iframe) for executing LLM-generated JS code --------
  // 模式：父页面把 code 字符串 postMessage 给 /static/sandbox.html（运行在 null origin），
  //       sandbox 在里面 new Function 执行，捕获 chart.setOption(option) 的 option，postMessage 回主页面。
  // 父页面创建 iframe 时 sandbox="allow-scripts"（无 allow-same-origin）→ 隔离 cookie / localStorage。
  let _sandboxIframe = null;
  let _sandboxReady = false;
  let _sandboxReadyWaiters = [];
  const _sandboxCallbacks = new Map();

  function _ensureSandbox() {
    if (_sandboxIframe) return _sandboxIframe;
    const iframe = document.createElement("iframe");
    iframe.id = "echarts-agent-sandbox";
    iframe.setAttribute("aria-hidden", "true");
    iframe.style.cssText = "position:fixed;top:-9999px;left:0;width:1px;height:1px;border:0;visibility:hidden;";
    iframe.sandbox = "allow-scripts";  // 注意：故意不给 allow-same-origin
    iframe.src = "/static/sandbox.html";
    document.body.appendChild(iframe);
    _sandboxIframe = iframe;
    return iframe;
  }

  // 父页面监听 sandbox 发回的消息
  window.addEventListener("message", (e) => {
    const data = e.data || {};
    // sandbox 报告自己已就绪
    if (data.type === "ready" && e.source === _sandboxIframe?.contentWindow) {
      _sandboxReady = true;
      const waiters = _sandboxReadyWaiters;
      _sandboxReadyWaiters = [];
      for (const fn of waiters) fn();
      return;
    }
    // sandbox 报告执行结果
    if (data.id && _sandboxCallbacks.has(data.id)) {
      const { resolve, reject } = _sandboxCallbacks.get(data.id);
      _sandboxCallbacks.delete(data.id);
      if (data.error) reject(new Error(data.error));
      else resolve(data.option);
    }
  });

  function _waitSandboxReady() {
    return new Promise((resolve) => {
      if (_sandboxReady) return resolve();
      _sandboxReadyWaiters.push(resolve);
      // 再触发一次 iframe 加载（首次会自己跑）
      _ensureSandbox();
    });
  }

  function executeChartCode(code) {
    return new Promise((resolve, reject) => {
      const id = "exec-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
      _sandboxCallbacks.set(id, { resolve, reject });
      _waitSandboxReady().then(() => {
        const iframe = _ensureSandbox();
        try {
          iframe.contentWindow.postMessage({ id, code }, "*");
        } catch (e) {
          _sandboxCallbacks.delete(id);
          reject(new Error("无法向 sandbox 发送代码：" + e.message));
        }
      });
    });
  }

  // sandbox 返回的 option 里，函数被序列化成 ``{__fn__: true, src: "..."}`` 标记对象
  // （postMessage 用 structured clone 不能传函数）。这里把标记对象还原成真函数。
  // 用 ``new Function('return ' + src)()``：src 是表达式（function 或箭头函数）。
  //
  // 安全网：还原后的函数会被 ``safeWrap`` 包一层 try/catch。
  // 原因：LLM 写出的 formatter 偶尔会用错 ``params`` 字段（比如 sankey link 上的 tooltip
  // 调用时 ``params.value`` 不存在），错误一旦抛出，会让 ECharts sankey 的
  // 布局初始化（``setOption`` 同步阶段）整体失败，导致整个图表无法渲染。
  // 包一层后 formatter 报错时降级返回 ``params.name``，让图表能继续渲染。
  function safeWrap(fn, path) {
    return function safeFn() {
      try {
        return fn.apply(this, arguments);
      } catch (e) {
        console.warn(`[hydrate] 函数 ${path || "<anonymous>"} 调用失败：`, e && e.message);
        // 降级：尽量给个能用的字符串
        const arg = arguments[0];
        if (arg && typeof arg === "object") {
          if (typeof arg.name === "string") return arg.name;
          if (typeof arg.value !== "undefined") return String(arg.value);
        }
        return "";
      }
    };
  }

  function hydrateFunctions(value, depth, path) {
    if (value == null || depth > 30) return value;
    if (typeof value === "function") return value;  // 已经是函数
    if (typeof value !== "object") return value;
    if (Array.isArray(value)) {
      const out = new Array(value.length);
      for (let i = 0; i < value.length; i++) {
        out[i] = hydrateFunctions(value[i], depth + 1, `${path}[${i}]`);
      }
      return out;
    }
    if (value.__fn__ === true && typeof value.src === "string") {
      try {
        const fn = new Function("return (" + value.src + ")")();
        return safeWrap(fn, path);
      } catch (e) {
        console.warn("[hydrate] 还原函数失败，保留为字符串：", e.message);
        return value.src;
      }
    }
    const out = {};
    for (const k in value) {
      if (Object.prototype.hasOwnProperty.call(value, k)) {
        out[k] = hydrateFunctions(value[k], depth + 1, path ? `${path}.${k}` : k);
      }
    }
    return out;
  }

  // ECharts 5.x sankey / graph / tree 等系列的 ``data`` 字段如果传 ``["name1", "name2"]``
  // （字符串数组）会被静默忽略 —— 实际渲染时不绘制节点。
  // LLM 经常写 ``data: ['a', 'b', 'c']``（简洁），所以在主页面 setOption 之前做规范化：
  // 把字符串数组转成 ``[{name: "a"}, {name: "b"}, ...]``，并保留 LLM 提供的对象数组原样。
  //
  // 另一个已知 ECharts 5.5.0 行为：sankey 节点的 ``itemStyle.color`` **回调函数被忽略**——
  // visual.color 设了函数后，Rect 渲染时按字符串处理，所以节点全部用默认 color 调色板。
  // 解法：检测到 sankey + itemStyle.color 是函数时，预求值到 ``data[i].itemStyle.color``。
  function normalizeEChartsOption(option) {
    if (option == null || typeof option !== "object") return option;
    if (Array.isArray(option)) return option.map(normalizeEChartsOption);
    const out = {};
    for (const k in option) {
      if (Object.prototype.hasOwnProperty.call(option, k)) {
        out[k] = normalizeEChartsOption(option[k], k);
      }
    }
    if (Array.isArray(out.series)) {
      for (const s of out.series) {
        if (s && Array.isArray(s.data) && needsNodeObjects(s.type)) {
          s.data = s.data.map(_toNodeObject);
          // sankey 节点 itemStyle.color 回调函数预求值
          if (s.type === "sankey" && s.itemStyle && typeof s.itemStyle.color === "function") {
            const colorFn = s.itemStyle.color;
            s.data = s.data.map((node, idx) => {
              if (node && node.itemStyle && node.itemStyle.color != null) return node;  // 节点已自带 color，跳过
              const fakeParams = {
                componentType: "series",
                seriesType: "sankey",
                seriesIndex: 0,
                dataIndex: idx,
                name: node && node.name,
                data: node,
                value: node && node.value,
              };
              let color;
              try { color = colorFn(fakeParams); } catch (e) { color = null; }
              if (typeof color === "string" && color) {
                return { ...node, itemStyle: { ...(node.itemStyle || {}), color } };
              }
              return node;
            });
            // 清掉顶层 itemStyle.color（已被分解到每个节点）
            delete s.itemStyle.color;
          }
        }
      }
    }
    return out;
  }

  // 哪些 series.type 需要 data 是 ``{name, ...}[]`` 节点对象数组
  const NEEDS_NODE_OBJECTS = new Set([
    "sankey", "graph", "tree", "sunburst", "treemap", "boxplot",
  ]);
  function needsNodeObjects(t) {
    return NEEDS_NODE_OBJECTS.has(String(t || "").toLowerCase());
  }
  function _toNodeObject(v) {
    if (v == null) return v;
    if (typeof v === "string") return { name: v };
    // 已经是对象（含 {name: 'x', value: 100}）→ 原样返回
    return v;
  }

  function renderResponse(data) {
    // 双模式：code 模式（LLM 直接输出 JS 代码）→ 沙箱执行提取 option；
    //         option 模式（LLM 输出严格 JSON）→ 直接使用
    (async () => {
      let option = data.option;
      // is_code_mode 表示 data.code 是 LLM 实际提供的 JS 代码（不是 wrap_code 合成的）
      if (data.is_code_mode && data.code) {
        try {
          option = await executeChartCode(data.code);
          // sandbox 把函数 toString 成 ``{__fn__: true, src: "..."}``，
          // 这里还原成真函数供 ECharts 调用
          option = hydrateFunctions(option, 0, "option");
        } catch (e) {
          showFallbackError("沙箱执行 LLM 生成的代码失败：" + e.message);
          return;
        }
      }
      if (!option) {
        showFallbackError("生成结果缺少 option 或 code 字段");
        return;
      }
      // ECharts 数据规范化：sankey / graph / tree / sunburst / treemap / boxplot
      // 的 ``data: ["name1", "name2"]`` 字符串数组不会被 ECharts 当成节点，
      // 需要先转成 ``[{name: "name1"}, ...]``
      option = normalizeEChartsOption(option);
      try {
        state.chart.setOption(option, true);
        // 异步触发一次 resize，确保容器布局变化时 chart 跟得上
        requestAnimationFrame(() => state.chart && state.chart.resize());
      } catch (e) {
        showFallbackError("图表 option 渲染失败：" + e.message);
        return;
      }

      const reason = cachedEl("typeReason");
      const reasonLine = reason.querySelector(".reason-line");
      const reasonUnd = reason.querySelector(".reason-understanding");
      const parseBadge = data.parse_method && !["primary", "primary_bare"].includes(data.parse_method)
        ? ' <span class="understand-tag warn" title="LLM 没有按 schema 输出，已自动兼容">⚠ LLM 偏离 schema</span>'
        : "";
      const modeBadge = data.is_code_mode
        ? ' <span class="understand-tag" title="LLM 直接输出 JS 代码（含 ECharts 回调函数）—— 已由 sandbox 提取 option">⚡ Code 模式</span>'
        : "";
      reasonLine.innerHTML =
        "推荐类型：" + escapeHtml(data.chart_type || "") +
        "；理由：" + escapeHtml(data.type_reason || "") +
        parseBadge + modeBadge;

      // 拼接「理解 + 预处理」两块信息
      const blocks = [];
      if (data.understanding) {
        const u = data.understanding;
        const tag = u.method === "llm"
          ? '<span class="understand-tag">🧠 LLM 已整理</span>'
          : '<span class="understand-tag fallback">↩ 整理回退</span>';
        const parts = [tag + " " + escapeHtml(u.summary || "(无摘要)")];
        if (u.notes) parts.push("整理说明：" + escapeHtml(u.notes));
        if (u.error) parts.push("（" + escapeHtml(u.error) + "）");
        blocks.push(parts.join("；"));
      }
      if (data.preprocess) {
        const pp = data.preprocess;
        const applied = (pp.applied || []).filter((a) => a && !a.skipped);
        if (pp.rules && pp.rules.length && applied.length) {
          const tag = '<span class="understand-tag">🔧 数据预处理</span>';
          const items = applied.map((a) => a.action).filter(Boolean);
          const summary = items.length ? escapeHtml(items.join("；")) : escapeHtml(pp.summary || "");
          blocks.push(tag + " " + summary);
        }
      }
      if (blocks.length) {
        reasonUnd.innerHTML = blocks.join("&nbsp;&nbsp;·&nbsp;&nbsp;");
        reasonUnd.classList.remove("hidden");
      } else {
        reasonUnd.innerHTML = "";
        reasonUnd.classList.add("hidden");
      }
      reason.classList.remove("hidden");

      cachedEl("panel-explain").textContent = data.content || data.explanation || "(无文字解释)";
      cachedEl("panel-option").textContent = JSON.stringify(option, null, 2);
      cachedEl("panel-code").textContent = data.code || "(无代码)";
      cachedEl("panel-raw").textContent = data.raw_reply || "";
    })();
  }

  // -------- UI helpers ----------
  function showHint(text, isError, isOk) {
    const el = document.getElementById("hintMsg");
    el.textContent = text;
    el.classList.remove("hidden", "err", "ok");
    if (isError) el.classList.add("err");
    if (isOk) el.classList.add("ok");
  }

  // -------- Tabs ----------
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.dataset.tab;
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      document.querySelectorAll(".tab-panel-wrap").forEach((p) => {
        p.classList.toggle("active", p.dataset.panel === name);
      });
    });
  });

  // -------- 复制按钮（hover 浮出的 tab 工具） ----------
  document.querySelectorAll(".tab-copy").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const target = cachedEl(btn.dataset.copyTarget);
      if (!target) return;
      const text = target.textContent || "";
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        // 旧浏览器或非安全上下文：降级到 execCommand
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (e2) {}
        ta.remove();
      }
      btn.classList.add("copied");
      btn.textContent = "✓ 已复制";
      setTimeout(() => {
        btn.classList.remove("copied");
        btn.textContent = "📋 复制";
      }, 1400);
    });
  });

  // -------- Buttons & shortcuts ----------
  $("#btnParse").addEventListener("click", parseCurrent);
  $("#btnClearData").addEventListener("click", clearData);
  $("#btnGen").addEventListener("click", generate);
  $("#btnCancelGen").addEventListener("click", cancelGeneration);

  // Ctrl/Cmd+Enter 在 prompt / 数据输入框里直接生成图表
  document.addEventListener("keydown", (e) => {
    const isSubmit = (e.ctrlKey || e.metaKey) && e.key === "Enter";
    if (!isSubmit) return;
    const t = e.target;
    if (t && (t.tagName === "TEXTAREA" || (t.tagName === "INPUT" && t.type === "text"))) {
      e.preventDefault();
      if (!state.genController) generate();
    }
  });
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
      const themeName = state.chartTheme ? "dark" : "light";
      const html = await buildExportHtml(opt, state.lastResp.chart_type, themeName);
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

    // Base URL 改了：实时刷新 provider 嗅探提示（用户已手动指定时给出对比）
    $("#baseUrl").addEventListener("input", updateProviderHint);
    // Provider 下拉改变：刷新提示
    $("#provider").addEventListener("change", updateProviderHint);

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

  // 把 server 返回的 "raw provider"（openai/ollama/glm/""）翻译成 UI 文案
  const PROVIDER_LABEL = {
    "":       "自动",
    openai:   "OpenAI 兼容",
    ollama:   "Ollama",
    glm:      "GLM 智谱",
  };

  function sniffProviderFromUrl(url) {
    // 镜像后端 _sniff_provider 规则；用于在用户改 Base URL 时即时刷新提示
    const u = (url || "").toLowerCase();
    if (u.includes(":11434") || u.includes("/ollama")) return "ollama";
    if (u.includes("bigmodel.cn") || u.includes("zhipuai") || u.includes("zhipu")) return "glm";
    return "openai";
  }

  function updateProviderHint() {
    const sel = $("#provider");
    const hint = $("#providerHint");
    if (!sel || !hint) return;
    const v = sel.value;
    const detected = sniffProviderFromUrl($("#baseUrl").value.trim());
    const detectedLabel = PROVIDER_LABEL[detected] || detected;
    if (!v) {
      hint.textContent = "嗅探为 " + detectedLabel;
      hint.classList.remove("provider-hint-override");
    } else {
      const userLabel = PROVIDER_LABEL[v] || v;
      hint.textContent = "已手动指定为 " + userLabel + (v !== detected ? "（嗅探为 " + detectedLabel + "）" : "（与嗅探一致）");
      hint.classList.add("provider-hint-override");
    }
  }

  function populateConfigForm(cfg) {
    state.config = cfg;
    $("#baseUrl").value = cfg.llm_base_url || "";
    $("#model").value = cfg.llm_model || "";
    $("#sysPrompt").value = cfg.system_prompt || "";
    $("#temperature").value = parseFloat(cfg.llm_temperature || 0.7);
    $("#maxTokens").value = parseInt(cfg.llm_max_tokens || 2048, 10);
    // Provider：DB 里空串 = 自动；落在白名单里才赋值；非法值回退到自动
    const provAllowed = ["", "openai", "ollama", "glm"];
    $("#provider").value = provAllowed.includes((cfg.llm_provider || "").toLowerCase())
      ? (cfg.llm_provider || "")
      : "";
    // 思考深度：DB 里可能存空串（=不发送）；UI 上仍选中"关闭"作为对应项
    const thinking = (cfg.llm_thinking || "").trim().toLowerCase();
    $("#thinking").value = ["off", "low", "medium", "high"].includes(thinking) ? thinking : "off";
    $("#apiKey").value = "";
    updateKeyStatusUI();
    updateProviderHint();
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
      llm_provider: $("#provider").value || "",
      system_prompt: $("#sysPrompt").value.trim(),
      llm_temperature: parseFloat($("#temperature").value) || 0.7,
      llm_max_tokens: parseInt($("#maxTokens").value, 10) || 2048,
      llm_thinking: $("#thinking").value || "off",
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
      llm_provider: $("#provider").value || "",
      llm_temperature: parseFloat($("#temperature").value),
      llm_max_tokens: parseInt($("#maxTokens").value, 10),
      llm_thinking: $("#thinking").value || "off",
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
  initGenStages();
  initConfigModal();
  initGenPanelCollapse();
})();
