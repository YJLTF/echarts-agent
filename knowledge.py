#!/usr/bin/env python3
"""本地 ECharts 配置项指导知识库（离线内置，不依赖联网搜索）。
内容参考自 Apache ECharts 官方配置项文档（https://echarts.apache.org/zh/option.html），
经过人工整理与缩写，作为生成图表时给 LLM 的提示信息。"""
from typing import Dict, List


# ------------------------- 核心通用配置项说明 -------------------------
GENERAL = """【通用概念】
- ECharts 的根配置对象叫 option，包含 title、legend、grid、xAxis、yAxis、tooltip、series、color 等字段。
- series 是一个数组，每项表示一个系列，必须包含 type 字段（如 bar、line、pie、scatter）。
- 数值轴需要明确 type='value'；类别轴 type='category'，并配 data 数组；时间轴 type='time'。
- xAxis、yAxis 可以是对象或对象数组（多轴）。双 Y 轴时 yAxis=[{type:'value'},{type:'value'}]，每个 series 用 yAxisIndex 绑定。
- tooltip.trigger 常用值：item（点/块触发）、axis（轴触发）。
- color 数组可提供全局配色；series.itemStyle.color 可覆盖单个系列颜色。
- 响应式：调用 chart.resize() 在窗口变化时；也可用 media 配置断点。
"""

TOOLTIP = """【tooltip 常用属性】
- trigger: 'item' | 'axis' | 'none'
- axisPointer.type: 'line' | 'shadow' | 'cross'
- formatter: 可为字符串模板（'{a}<br/>{b}: {c}'）或回调函数 (params)=> string
- backgroundColor、borderColor、textStyle.color 自定义样式
"""

LEGEND = """【legend 常用属性】
- data: 字符串数组，默认取自 series.name。
- type: 'plain' | 'scroll'（数据多时滚动）
- orient: 'horizontal' | 'vertical'
- left/right/top/bottom: 数字或 'left'/'center'/'right'
"""

AXIS = """【xAxis / yAxis 常用属性】
- type: 'category' | 'value' | 'time' | 'log'
- name: 轴名称
- data: category 轴使用，字符串数组
- axisLabel: 刻度文本 { formatter, rotate, color, fontSize }
- axisLine, axisTick, splitLine: { show, lineStyle:{color,type} }
- min/max: 数值；'dataMin' / 'dataMax' 自动
"""


# ------------------------- 分类型详细说明 -------------------------
BAR = """【bar 柱状图】
series[i]-bar:
- name, type:'bar', data: number[] | [{name, value}]
- stack: 字符串，相同 stack 的系列会堆叠
- barGap / barCategoryGap: 间距
- itemStyle:{color, borderRadius, borderColor, borderWidth}
- label:{show, position:'top'|'inside'|'bottom', formatter}
- emphasis:{itemStyle, label, focus:'self'|'series'}
- barWidth / barMaxWidth / barMinHeight
示例：
{
  "xAxis": {"type":"category","data":["A","B","C"]},
  "yAxis": {"type":"value"},
  "series":[{"type":"bar","data":[10,20,30],"itemStyle":{"borderRadius":[4,4,0,0]}}]
}
"""

LINE = """【line 折线/面积图】
series[i]-line:
- type:'line', data: number[]
- smooth: true 平滑曲线
- symbol: 'circle' | 'emptyCircle' | 'rect' | 'roundRect' | 'triangle' | 'diamond' | 'pin' | 'arrow' | 'none'
- symbolSize: 数字或数组
- step: false | 'start' | 'middle' | 'end' 阶梯线
- areaStyle: {color, opacity} 开启面积图
- lineStyle: {color, width, type:'solid'|'dashed'|'dotted'}
- stack: 字符串，堆叠面积图
- emphasis / label 同 bar
"""

PIE = """【pie 饼图/环形图】
series[i]-pie:
- type:'pie', data: [{name, value, itemStyle?}]
- radius: 数字或 ['内半径','外半径']（做成环形）
- center: ['50%','50%'] 圆心
- roseType: 'radius' | 'area' 南丁格尔玫瑰图
- startAngle / endAngle
- minAngle: 最小角度
- label: {show, position:'outside'|'inside'|'center', formatter}
- labelLine: {show, length, length2, lineStyle}
- avoidLabelOverlap: true
- itemStyle: {borderColor, borderWidth, borderRadius}
- emphasis: {scale, scaleSize, label:{fontSize}}
饼图无 xAxis/yAxis。
"""

SCATTER = """【scatter 散点图】
series[i]-scatter:
- type:'scatter', data: [[x,y],...] 或 [{value:[x,y],...}]
- symbol、symbolSize 同 line
- itemStyle.color 可通过回调按维度着色
- 需要 xAxis/ yAxis 均为 value 或 time 类型
"""

RADAR = """【radar 雷达图】
顶层 radar:
- indicator: [{name, max, min}]
- radius, center, startAngle
- axisName / axisLine / splitLine / splitArea
series[i]-radar:
- type:'radar', data: [{name, value:[...], areaStyle, lineStyle}]
"""

GAUGE = """【gauge 仪表盘】
series[i]-gauge:
- type:'gauge', min, max, startAngle, endAngle
- data: [{value, name}]
- progress: {show, width, itemStyle}
- pointer: {show, length, width, itemStyle}
- axisLine: {lineStyle:{width, color:[[0.2,'#a'],[1,'#b']]}}
- axisTick, splitLine, axisLabel
- title / detail: {show, formatter, valueAnimation, fontSize}
"""

FUNNEL = """【funnel 漏斗图】
series[i]-funnel:
- type:'funnel', data: [{name, value}]
- left/right/top/bottom, width, height
- min, max, minSize, maxSize
- sort: 'descending' | 'ascending' | 'none'
- gap
- label, labelLine, itemStyle, emphasis 同 pie
"""

HEATMAP = """【heatmap 热力图】
series[i]-heatmap:
- type:'heatmap', data: [[xIndex, yIndex, value],...]
- label, itemStyle, emphasis
- 需要在 visualMap 中指定 min/max 并关联 seriesIndex: 'all' 或指定下标
"""

SUNBURST = """【sunburst 旭日图】
series[i]-sunburst:
- type:'sunburst', data: [{name, value, children:[...]}]
- radius, center, startAngle, endAngle
- label, itemStyle, levels 每层样式控制
"""

TREEMAP = """【treemap 矩形树图】
series[i]-treemap:
- type:'treemap', data: [{name, value, children:[...]}]
- left/top/width/height
- roam: true 可缩放
- label, itemStyle, breadcrumb, upperLabel
- visualMin / visualMax / colorAlpha / colorSaturation 控制颜色
"""

SANKEY = """【sankey 桑基图】
series[i]-sankey:
- type:'sankey', data: [{name}]
- links/edges: [{source, target, value}]
- nodeWidth, nodeGap, nodeAlign, layoutIterations
- orient: 'horizontal' | 'vertical'
- label, lineStyle, emphasis
"""

CANDLESTICK = """【candlestick 蜡烛图/K线】
series[i]-candlestick:
- type:'candlestick', data: [[open, close, low, high],...]
- itemStyle:{color, color0, borderColor, borderColor0} （带 0 的是下跌）
"""

BOXPLOT = """【boxplot 箱线图】
series[i]-boxplot:
- type:'boxplot', data: [[min,Q1,median,Q3,max],...]
- layout: 'horizontal' | 'vertical'
- itemStyle:{color, borderColor, borderWidth}
"""

EFFECTSCATTER = """【effectScatter 涟漪散点】
与 scatter 类似，多了涟漪动画：
- rippleEffect: {brushType:'stroke'|'scale', scale, period}
- showEffectOn: 'render' | 'emphasis'
"""

PICTORIALBAR = """【pictorialBar 象形柱图】
series[i]-pictorialBar:
- type:'pictorialBar', data: [...]
- symbol, symbolSize, symbolRotate, symbolPosition, symbolOffset, symbolBoundingData
- 适合做图标化柱图
"""


TYPE_MAP: Dict[str, List[str]] = {
    "bar": ["柱状图"],
    "line": ["折线图"],
    "pie": ["饼图"],
    "scatter": ["散点图"],
    "radar": ["雷达图"],
    "gauge": ["仪表盘"],
    "funnel": ["漏斗图"],
    "heatmap": ["热力图"],
    "sunburst": ["旭日图"],
    "treemap": ["矩形树图"],
    "sankey": ["桑基图"],
    "candlestick": ["蜡烛图"],
    "boxplot": ["箱线图"],
    "effectscatter": ["涟漪散点"],
    "pictorialbar": ["象形柱图"],
}

TYPE_BODY: Dict[str, str] = {
    "bar": BAR,
    "line": LINE,
    "pie": PIE,
    "scatter": SCATTER,
    "radar": RADAR,
    "gauge": GAUGE,
    "funnel": FUNNEL,
    "heatmap": HEATMAP,
    "sunburst": SUNBURST,
    "treemap": TREEMAP,
    "sankey": SANKEY,
    "candlestick": CANDLESTICK,
    "boxplot": BOXPLOT,
    "effectscatter": EFFECTSCATTER,
    "pictorialbar": PICTORIALBAR,
}


# 哪些图表类型用得到 axis / legend。
# 不相关章节不发给 LLM，节省 token、减少噪音。
CHART_USES_AXIS: Dict[str, bool] = {
    "bar": True, "line": True, "scatter": True, "heatmap": True,
    "candlestick": True, "boxplot": True, "pictorialbar": True,
    "effectscatter": True, "radar": True,
    "pie": False, "funnel": False, "sunburst": False,
    "treemap": False, "sankey": False, "gauge": False,
}
# tooltip 对所有图表都适用（hover 提示）；不分类


def get_knowledge_for_type(chart_type: str) -> Dict[str, str]:
    """按图表类型裁剪 KB：只发相关章节。

    通用基础 + 图表类型详情 永远发；tooltip 永远发（每个图表都有 hover 提示）；
    axis / legend 按 CHART_USES_AXIS 表按需发。
    这样 prompt 更紧凑、LLM 注意力更集中、token 也省。
    """
    ct = (chart_type or "").lower().strip()
    body = TYPE_BODY.get(ct, BAR)
    sections: Dict[str, str] = {
        "通用基础": GENERAL,
        "tooltip": TOOLTIP,
        f"图表类型：{ct or 'bar'}": body,
    }
    if CHART_USES_AXIS.get(ct, True):
        sections["轴配置 (xAxis / yAxis)"] = AXIS
    # legend 在 pie / funnel / sunburst / treemap / sankey / radar / 多 series 图表里都很有用
    # 简化起见：除单系列的 gauge / heatmap / candlestick / boxplot 之外都发
    if ct not in ("gauge", "heatmap", "candlestick", "boxplot"):
        sections["legend"] = LEGEND
    return sections


def search_knowledge(query: str) -> Dict[str, str]:
    if not query:
        return {"通用基础": GENERAL}
    q = query.lower()
    hits = {"通用基础": GENERAL}
    for name, aliases in TYPE_MAP.items():
        if name in q or any(a in query for a in aliases):
            hits[f"图表类型：{name}"] = TYPE_BODY[name]
    if "tooltip" in q or "提示" in query:
        hits["tooltip"] = TOOLTIP
    if "legend" in q or "图例" in query:
        hits["legend"] = LEGEND
    if "axis" in q or "轴" in query:
        hits["轴配置 (xAxis / yAxis)"] = AXIS
    if len(hits) <= 1:
        # 默认返回 bar 供参考
        hits["图表类型：bar"] = BAR
    return hits
