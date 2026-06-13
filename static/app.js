(function () {
  const $ = (sel) => document.querySelector(sel);

  const state = {
    data: null,        // 来自 /api/parse 的解析结果
    chart: null,       // echarts 实例
    chartTheme: "light",
    lastResp: null,    // 最近一次 /api/chart 响应
  };

  // -------- Chart ----------
  function initChart() {
    if (state.chart) return;
    state.chart = echarts.init(document.getElementById("chart"), null, { renderer: "canvas" });
    window.addEventListener("resize", () => state.chart && state.chart.resize());
    // 一个占位图
    state.chart.setOption({
      title: { text: "等待生成图表…", left: "center", top: "center", textStyle: { color: "#94a3b8", fontWeight: 400, fontSize: 14 } },
    });
  }

  // -------- Parse file/text ----------
  async function parseCurrent() {
    const file = $("#fileInput").files[0];
    const text = $("#textInput").value.trim();
    const hint = $("#hintMsg");

    hide(hint);
    if (!file && !text) {
      showHint("请先选择文件或粘贴数据。", true);
      return;
    }

    const fd = new FormData();
    if (file) {
      fd.append("file", file);
      $("#fileName").textContent = file.name;
    } else {
      fd.append("text", text);
      $("#fileName").textContent = "粘贴的文本数据";
    }

    try {
      const resp = await fetch("/api/parse", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "解析失败");
      state.data = data;
      $("#dataPreview").classList.remove("hidden");
      $("#dataPreview").textContent =
        (data.description || "") + "\n\n前 5 行：\n" +
        JSON.stringify((data.rows || []).slice(0, 5), null, 2);
      showHint("已解析数据，可以发送需求。", false, true);
    } catch (e) {
      showHint("解析出错：" + e.message, true);
      state.data = null;
    }
  }

  function clearData() {
    state.data = null;
    $("#fileInput").value = "";
    $("#textInput").value = "";
    $("#fileName").textContent = "未选择文件";
    $("#dataPreview").classList.add("hidden");
    hide($("#hintMsg"));
  }

  // -------- Send prompt to generate chart ----------
  async function generate() {
    const prompt = $("#promptInput").value.trim();
    const title = $("#titleInput").value.trim();
    const typeHint = $("#typeSel").value;
    const theme = $("#themeSel").value;

    if (!prompt && !state.data) {
      showHint("请至少输入需求或上传数据。", true);
      return;
    }

    state.chartTheme = theme;
    if (state.chart) state.chart.dispose();
    state.chart = echarts.init(
      document.getElementById("chart"),
      theme === "dark" ? "dark" : null
    );
    state.chart.showLoading({ text: "生成中…", color: "#5470c6" });

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
      });
      const data = await resp.json();
      state.chart.hideLoading();

      if (!resp.ok || data.error) {
        showFallbackError(data.error || "生成失败");
        showHint(data.error || "生成失败", true);
        return;
      }

      state.lastResp = data;
      renderResponse(data);
      showHint(`已生成图表：${data.chart_type} · ${data.type_reason || ""}`, false, true);
    } catch (e) {
      state.chart.hideLoading();
      showHint("请求失败：" + e.message, true);
    }
  }

  function showFallbackError(msg) {
    state.chart.setOption({
      title: { text: msg || "生成失败", left: "center", top: "center",
        textStyle: { color: "#c0392b", fontWeight: 400, fontSize: 14 } },
    });
  }

  function renderResponse(data) {
    try {
      state.chart.setOption(data.option, true);
    } catch (e) {
      showFallbackError("图表 option 渲染失败：" + e.message);
    }
    const reason = document.getElementById("typeReason");
    reason.textContent = "推荐类型：" + data.chart_type + "；理由：" + (data.type_reason || "");
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

  $("#btnExport").addEventListener("click", () => {
    if (!state.lastResp) return showHint("先生成一个图表再导出。", true);
    const opt = JSON.stringify(state.lastResp.option, null, 2);
    const html = buildExportHtml(opt, state.lastResp.chart_type, state.chartTheme);
    const blob = new Blob([html], { type: "text/html;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `echarts-${state.lastResp.chart_type}-${Date.now()}.html`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  $("#btnFullscreen").addEventListener("click", () => {
    const el = document.querySelector(".chart");
    if (!document.fullscreenElement) el.requestFullscreen().then(()=>state.chart.resize());
    else document.exitFullscreen().then(()=>state.chart.resize());
  });

  function buildExportHtml(optionJSON, type, theme) {
    const themeScript = theme === "dark"
      ? '<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/theme/dark.js"><\/script>'
      : '';
    return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>ECharts · ${type}</title>
<style>html,body{margin:0;padding:0;height:100%;background:${theme==='dark'?'#0f172a':'#f5f7fb'}}#chart{width:100%;height:100vh}</style>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"><\/script>
${themeScript}
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

  // On load
  initChart();
})();
