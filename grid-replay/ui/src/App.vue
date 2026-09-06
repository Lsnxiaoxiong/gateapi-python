<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import * as echarts from 'echarts'

const chartEl = ref(null)
let chart = null
let timer = null
let playbackAccumulator = 0

const meta = reactive({ symbol: 'BTCUSDT', interval: '1m', total: 0, firstTime: 0, lastTime: 0 })
const loading = ref(true)
const backtesting = ref(false)
const backtestProgress = ref(0)
const backtestProcessedMinutes = ref(0)
const backtestTargetMinutes = ref(0)
const backtestGeneratedFrames = ref(0)
const backtestCurrentTime = ref(0)
const playing = ref(false)
const errorText = ref('')
const startTime = ref('')
const displayInterval = ref('1h')
const speed = ref(1)
const currentTime = ref(0)
const currentPrice = ref(0)
const currentOffset = ref(0)
const realizedPnl = ref(0)
const totalFees = ref(0)
const completedCount = ref(0)
const candles = ref([])
const pendingBuys = ref([])
const positions = ref([])
const events = ref([])
const frameCount = ref(0)
const frameIndex = ref(0)
let frames = []

const DISPLAY_INTERVALS = ['1h', '4h', '12h', '1d', '2d', '3d', '1w', '2w', '3w', '1mo']

const settings = reactive({
  referencePrice: 0,
  marginPerGrid: 100,
  leverage: 2,
  gridStepPct: 0.3,
  takeProfitPct: 0.3,
  buyGridCount: 10,
  takeProfitCount: 10,
  takeProfitGridCount: 20,
  maxUnclosedPerGrid: 2,
  priceTick: 0.1,
  qtyStep: 0.001,
  feePct: 0.05,
})

const fmtPrice = (value) => Number(value || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const fmtNumber = (value, digits = 2) => Number(value || 0).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
const fmtTime = (ts) => ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false }) : '--'
const toLocalInput = (ts) => {
  const d = new Date(ts)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const openTpCount = computed(() => positions.value.filter((p) => p.tpOpen).length)
const unrealizedPnl = computed(() => positions.value.reduce((sum, p) => sum + (currentPrice.value - p.buyPrice) * p.size - p.entryFee, 0))
const netPnl = computed(() => realizedPnl.value + unrealizedPnl.value)
const marginUsed = computed(() => positions.value.length * Number(settings.marginPerGrid || 0))
const exposure = computed(() => positions.value.reduce((sum, p) => sum + p.buyPrice * p.size, 0))
const minimumMarginEstimate = computed(() => {
  const price = Number(currentPrice.value || 0)
  const qtyStep = Number(settings.qtyStep || 0)
  const leverage = Number(settings.leverage || 1)
  if (price <= 0 || qtyStep <= 0 || leverage <= 0) return 0
  return price * qtyStep / leverage
})
const orderSizeBlocked = computed(() =>
  currentPrice.value > 0
  && pendingBuys.value.length === 0
  && positions.value.length === 0
  && Number(settings.marginPerGrid || 0) < minimumMarginEstimate.value
)
const progressPct = computed(() => backtesting.value
  ? backtestProgress.value
  : (frameCount.value ? Math.min(100, frameIndex.value / frameCount.value * 100) : 0))

function clampSettings() {
  settings.referencePrice = Math.max(0, Number(settings.referencePrice) || 0)
  settings.marginPerGrid = Math.max(0.01, Number(settings.marginPerGrid) || 100)
  settings.leverage = Math.max(1, Number(settings.leverage) || 1)
  settings.gridStepPct = Math.min(99, Math.max(0.001, Number(settings.gridStepPct) || 0.3))
  settings.takeProfitPct = Math.min(99, Math.max(0.001, Number(settings.takeProfitPct) || 0.3))
  settings.buyGridCount = Math.max(1, Math.min(100, Math.trunc(Number(settings.buyGridCount) || 10)))
  settings.takeProfitCount = Math.max(1, Math.min(100, Math.trunc(Number(settings.takeProfitCount) || 10)))
  settings.takeProfitGridCount = Math.max(1, Math.min(500, Math.trunc(Number(settings.takeProfitGridCount) || 20)))
  settings.maxUnclosedPerGrid = Math.max(1, Math.min(20, Math.trunc(Number(settings.maxUnclosedPerGrid) || 2)))
  settings.priceTick = Math.max(0.000001, Number(settings.priceTick) || 0.1)
  settings.qtyStep = Math.max(0.000001, Number(settings.qtyStep) || 0.001)
  settings.feePct = Math.max(0, Math.min(10, Number(settings.feePct) || 0))
}

async function apiJson(path, options = {}) {
  const response = await fetch(path, options)
  const contentType = response.headers.get('content-type') || ''
  if (!response.ok) {
    if (contentType.includes('application/json')) {
      const body = await response.json().catch(() => ({}))
      throw new Error(body.error || `HTTP ${response.status}`)
    }
    throw new Error(`请求失败：HTTP ${response.status}`)
  }
  if (!contentType.includes('application/json')) throw new Error('服务返回的不是 JSON')
  return response.json()
}

function resetPlaybackState() {
  playing.value = false
  playbackAccumulator = 0
  frameIndex.value = 0
  currentTime.value = 0
  currentPrice.value = 0
  currentOffset.value = 0
  realizedPnl.value = 0
  totalFees.value = 0
  completedCount.value = 0
  candles.value = []
  pendingBuys.value = []
  positions.value = []
  events.value = []
  renderChart()
}

function decodeBuys(rows = []) {
  return rows.map((row, index) => ({ id: `b-${index}-${row[0]}`, gridIndex: row[0], price: row[1], size: row[2] }))
}

function decodePositions(rows = []) {
  return rows.map((row, index) => ({
    id: `p-${index}-${row[0]}-${row[1]}`,
    gridIndex: row[0],
    buyPrice: row[1],
    tpPrice: row[2],
    size: row[3],
    tpOpen: Boolean(row[4]),
    entryFee: row[5],
  }))
}

function appendEvents(rows = []) {
  if (!rows.length) return
  const incoming = rows.slice(-100).reverse().map((row, index) => ({
    id: `${row[0]}-${row[1]}-${index}`,
    type: row[0],
    ts: row[1],
    text: row[2],
  }))
  events.value = incoming.concat(events.value).slice(0, 100)
}

function applyInitial(initial) {
  currentOffset.value = initial.offset || 0
  currentTime.value = initial.time || 0
  currentPrice.value = initial.price || 0
  pendingBuys.value = decodeBuys(initial.buys)
  positions.value = decodePositions(initial.positions)
}

function applyFrame(frame) {
  candles.value.push(frame.k)
  if (candles.value.length > 240) candles.value.shift()
  currentOffset.value = frame.o
  currentTime.value = frame.t
  currentPrice.value = frame.p
  realizedPnl.value = frame.r
  totalFees.value = frame.f
  completedCount.value = frame.cc
  pendingBuys.value = decodeBuys(frame.b)
  positions.value = decodePositions(frame.ps)
  appendEvents(frame.e)
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function runBacktest() {
  if (backtesting.value) return
  try {
    clampSettings()
    backtesting.value = true
    backtestProgress.value = 0
    backtestProcessedMinutes.value = 0
    backtestTargetMinutes.value = 0
    backtestGeneratedFrames.value = 0
    backtestCurrentTime.value = 0
    playing.value = false
    errorText.value = ''
    resetPlaybackState()

    const payload = {
      startTime: startTime.value ? new Date(startTime.value).getTime() : meta.firstTime,
      displayInterval: displayInterval.value,
      frameLimit: 2000,
      ...settings,
    }
    const started = await apiJson('/api/backtest/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const jobId = started.jobId
    if (!jobId) throw new Error('后端未返回回测任务 ID')

    let result = null
    while (backtesting.value) {
      const status = await apiJson(`/api/backtest/status?jobId=${encodeURIComponent(jobId)}`)
      backtestProgress.value = Math.max(0, Math.min(100, Number(status.progress || 0)))
      backtestProcessedMinutes.value = Number(status.processedMinutes || 0)
      backtestTargetMinutes.value = Number(status.targetMinutes || 0)
      backtestGeneratedFrames.value = Number(status.generatedFrames || 0)
      backtestCurrentTime.value = Number(status.currentTime || 0)

      if (status.status === 'error') throw new Error(status.error || '后端回测失败')
      if (status.status === 'done') {
        result = status.result || {}
        break
      }
      await sleep(200)
    }

    if (!result) return
    frames = Array.isArray(result.frames) ? result.frames : []
    frameCount.value = frames.length
    applyInitial(result.initial || {})
    if (result.summary?.referencePrice) settings.referencePrice = result.summary.referencePrice
    events.value = [{
      id: `system-${Date.now()}`,
      type: 'system',
      ts: currentTime.value,
      text: `后端回测完成：处理 ${Number(result.summary?.processedMinutes || 0).toLocaleString()} 根 1m，生成 ${frames.length.toLocaleString()} 根 ${displayInterval.value} 播放帧`,
    }]
    renderChart()
  } catch (err) {
    errorText.value = err instanceof Error ? err.message : String(err)
  } finally {
    backtesting.value = false
  }
}

function step(count = 1) {
  if (backtesting.value || !frames.length) return
  let applied = 0
  while (applied < count && frameIndex.value < frames.length) {
    applyFrame(frames[frameIndex.value])
    frameIndex.value += 1
    applied += 1
  }
  if (frameIndex.value >= frames.length) playing.value = false
  renderChart()
}

function togglePlay() {
  if (!frames.length || backtesting.value) return
  playing.value = !playing.value
}

function tick() {
  if (!playing.value || backtesting.value) return
  playbackAccumulator += Number(speed.value) / 10
  const count = Math.floor(playbackAccumulator)
  if (count <= 0) return
  playbackAccumulator -= count
  step(Math.min(count, 100))
}

function renderChart() {
  if (!chart) return
  const rows = candles.value
  const categories = rows.map((r) => new Date(r[0]).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }))
  const kData = rows.map((r) => [r[1], r[4], r[3], r[2]])
  const volume = rows.map((r) => r[5])
  const orderLines = [
    ...pendingBuys.value.slice(0, 30).map((o) => ({ yAxis: o.price, lineStyle: { type: 'dashed', opacity: 0.85, color: '#3b82f6', width: 1.5 } })),
    ...positions.value.filter((p) => p.tpOpen).slice(0, 30).map((p) => ({ yAxis: p.tpPrice, lineStyle: { type: 'dotted', opacity: 0.9, color: '#f59e0b', width: 1.5 } })),
  ]

  chart.setOption({
    animation: false,
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [
      { left: 62, right: 18, top: 28, height: '67%' },
      { left: 62, right: 18, top: '78%', height: '14%' },
    ],
    xAxis: [
      { type: 'category', data: categories, boundaryGap: true, axisLine: { lineStyle: { color: '#475569' } }, axisLabel: { color: '#94a3b8', hideOverlap: true } },
      { type: 'category', gridIndex: 1, data: categories, boundaryGap: true, axisLabel: { show: false }, axisLine: { lineStyle: { color: '#475569' } } },
    ],
    yAxis: [
      { scale: true, splitLine: { lineStyle: { color: '#1e293b' } }, axisLabel: { color: '#94a3b8' } },
      { scale: true, gridIndex: 1, splitNumber: 2, splitLine: { show: false }, axisLabel: { color: '#64748b' } },
    ],
    series: [
      {
        name: meta.symbol,
        type: 'candlestick',
        data: kData,
        barMaxWidth: 22,
        itemStyle: { color: '#22c55e', color0: '#ef4444', borderColor: '#22c55e', borderColor0: '#ef4444' },
        markLine: { symbol: ['none', 'none'], silent: true, label: { show: false }, data: orderLines },
      },
      { name: 'Volume', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volume, barMaxWidth: 28, itemStyle: { opacity: 0.45 } },
    ],
  }, true)
}

function resizeChart() {
  chart?.resize()
}

async function init() {
  try {
    loading.value = true
    Object.assign(meta, await apiJson('/api/meta'))
    startTime.value = toLocalInput(meta.firstTime)
    await nextTick()
    chart = echarts.init(chartEl.value, 'dark')
    window.addEventListener('resize', resizeChart)
    renderChart()
  } catch (err) {
    errorText.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  timer = window.setInterval(tick, 100)
  init()
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  window.removeEventListener('resize', resizeChart)
  chart?.dispose()
})
</script>

<template>
  <main class="app-shell">
    <header class="topbar">
      <div>
        <div class="eyebrow">历史行情 · 模拟交易</div>
        <div class="title-row">
          <h1>{{ meta.symbol }} <span>{{ displayInterval }} 显示 · {{ meta.interval }} 交易</span></h1>
          <div class="live-price" :class="{ active: playing }">当前价 {{ fmtPrice(currentPrice) }} USDT</div>
        </div>
      </div>
      <div class="playback-controls">
        <label>开始时间<input v-model="startTime" type="datetime-local" :disabled="backtesting" /></label>
        <label>显示级别
          <select v-model="displayInterval" :disabled="backtesting || playing">
            <option v-for="item in DISPLAY_INTERVALS" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label>速度（当前级别）
          <select v-model.number="speed">
            <option :value="0.25">0.25 根/秒</option><option :value="0.5">0.5 根/秒</option>
            <option :value="1">1 根/秒</option><option :value="2">2 根/秒</option>
            <option :value="5">5 根/秒</option><option :value="10">10 根/秒</option>
          </select>
        </label>
        <button class="secondary" :disabled="loading || backtesting" @click="runBacktest">重置</button>
        <button class="secondary" :disabled="loading || backtesting || playing || !frameCount" @click="step(1)">单步</button>
        <button class="primary" :disabled="loading || backtesting || !frameCount" @click="togglePlay">{{ backtesting ? `后端计算 ${backtestProgress.toFixed(0)}%` : (playing ? '暂停' : '播放') }}</button>
      </div>
    </header>

    <div v-if="errorText" class="error-banner">{{ errorText }}</div>

    <section class="stats-grid">
      <article><span>当前时间</span><strong>{{ fmtTime(currentTime) }}</strong><small>{{ currentOffset.toLocaleString() }} / {{ meta.total.toLocaleString() }}</small></article>
      <article><span>总盈亏</span><strong :class="netPnl >= 0 ? 'profit' : 'loss'">{{ netPnl >= 0 ? '+' : '' }}{{ fmtNumber(netPnl) }} USDT</strong><small>已实现 {{ fmtNumber(realizedPnl) }} / 浮动 {{ fmtNumber(unrealizedPnl) }}</small></article>
      <article><span>持仓 / 保证金</span><strong>{{ positions.length }} / {{ fmtNumber(marginUsed) }}</strong><small>名义敞口 {{ fmtNumber(exposure) }} USDT</small></article>
      <article><span>活动挂单</span><strong>{{ pendingBuys.length }} 买 / {{ openTpCount }} 止盈</strong><small>累计手续费 {{ fmtNumber(totalFees) }} USDT</small></article>
      <article><span>已完成周期</span><strong>{{ completedCount.toLocaleString() }}</strong><small>网格参考价 {{ fmtPrice(settings.referencePrice) }}</small></article>
    </section>

    <div class="progress"><i :style="{ width: `${progressPct}%` }"></i></div>

    <section class="workspace">
      <div class="chart-panel">
        <div class="panel-head">
          <div><b>K线回放 · {{ displayInterval }}</b><span>后端严格按 1m 完成回测，前端仅播放结果 · <span class="buy-legend">蓝色虚线=挂买单</span> · <span class="sell-legend">橙色点线=止盈卖单</span></span></div>
          <span>{{ backtesting ? `后端回测 ${backtestProgress.toFixed(1)}% · ${backtestGeneratedFrames.toLocaleString()} 帧` : (playing ? `▶ ${speed} 根 ${displayInterval}/秒` : `❚❚ 已暂停 · ${frameIndex}/${frameCount} 帧`) }}</span>
        </div>
        <div v-if="orderSizeBlocked" class="order-warning">
          当前参数无法生成有效挂买单：每格保证金 {{ fmtNumber(settings.marginPerGrid) }} USDT，在当前价 {{ fmtPrice(currentPrice) }}、杠杆 {{ settings.leverage }}x、数量精度 {{ settings.qtyStep }} 下，下单数量向下取整后为 0。每格保证金至少约需 <b>{{ fmtNumber(minimumMarginEstimate) }} USDT</b>。
        </div>
        <div ref="chartEl" class="chart"></div>
      </div>

      <aside class="settings-panel">
        <div class="panel-head"><div><b>网格参数</b><span>参考 auto_grid_futures_generic.py</span></div></div>
        <div class="form-grid">
          <label>参考价格<input v-model.number="settings.referencePrice" type="number" step="0.1" /></label>
          <label>每格保证金 (USDT)<input v-model.number="settings.marginPerGrid" type="number" step="1" /></label>
          <label>杠杆<input v-model.number="settings.leverage" type="number" step="1" min="1" /></label>
          <label>网格间距 (%)<input v-model.number="settings.gridStepPct" type="number" step="0.01" /></label>
          <label>止盈比例 (%)<input v-model.number="settings.takeProfitPct" type="number" step="0.01" /></label>
          <label>买格数量<input v-model.number="settings.buyGridCount" type="number" min="1" /></label>
          <label>止盈单数量<input v-model.number="settings.takeProfitCount" type="number" min="1" /></label>
          <label>止盈检查网格<input v-model.number="settings.takeProfitGridCount" type="number" min="1" /></label>
          <label>单格最大未平仓<input v-model.number="settings.maxUnclosedPerGrid" type="number" min="1" /></label>
          <label>价格精度<input v-model.number="settings.priceTick" type="number" step="0.1" /></label>
          <label>数量精度<input v-model.number="settings.qtyStep" type="number" step="0.001" /></label>
          <label>单边手续费 (%)<input v-model.number="settings.feePct" type="number" step="0.01" /></label>
        </div>
        <button class="apply" :disabled="loading || backtesting" @click="runBacktest">{{ backtesting ? `后端回测 ${backtestProgress.toFixed(1)}%` : '应用参数并重新回放' }}</button>
        <div v-if="backtesting" class="logic-note">已处理 <b>{{ backtestProcessedMinutes.toLocaleString() }}</b> / {{ backtestTargetMinutes.toLocaleString() }} 根 1m · 已生成 <b>{{ backtestGeneratedFrames.toLocaleString() }}</b> 根 {{ displayInterval }} 帧 · 当前 {{ fmtTime(backtestCurrentTime) }}</div>
        <div class="logic-note">点击应用后，参数发送到 Python 后端。后端顺序扫描原始 <b>1m</b> K线完成买入、止盈、挂单和盈亏计算，并按 {{ displayInterval }} 聚合成最多 2000 个播放帧。Vue 不再执行策略计算。</div>
      </aside>
    </section>

    <section class="tables-grid">
      <div class="table-card">
        <div class="panel-head"><div><b>当前买单</b><span>现价下方最近 {{ settings.buyGridCount }} 个网格</span></div><em>{{ pendingBuys.length }}</em></div>
        <div class="table-scroll"><table><thead><tr><th>网格</th><th>买价</th><th>数量</th><th>名义金额</th></tr></thead><tbody>
          <tr v-for="o in pendingBuys" :key="o.id" class="buy-order-row"><td>#{{ o.gridIndex }}</td><td class="buy-order-price">{{ fmtPrice(o.price) }}</td><td>{{ fmtNumber(o.size, 4) }}</td><td>{{ fmtNumber(o.price * o.size) }}</td></tr>
          <tr v-if="!pendingBuys.length"><td colspan="4" class="empty">暂无挂单</td></tr>
        </tbody></table></div>
      </div>

      <div class="table-card">
        <div class="panel-head"><div><b>当前持仓 / 止盈</b><span>最多维护 {{ settings.takeProfitCount }} 个活动止盈单</span></div><em>{{ positions.length }}</em></div>
        <div class="table-scroll"><table><thead><tr><th>买入价</th><th>止盈价</th><th>数量</th><th>状态</th><th>浮盈亏</th></tr></thead><tbody>
          <tr v-for="p in positions" :key="p.id" :class="p.tpOpen ? 'sell-order-row' : ''"><td>{{ fmtPrice(p.buyPrice) }}</td><td :class="p.tpOpen ? 'sell-order-price' : ''">{{ fmtPrice(p.tpPrice) }}</td><td>{{ fmtNumber(p.size, 4) }}</td><td><span class="status" :class="p.tpOpen ? 'open' : 'planned'">{{ p.tpOpen ? '止盈挂单' : '等待挂止盈' }}</span></td><td :class="currentPrice >= p.buyPrice ? 'profit' : 'loss'">{{ fmtNumber((currentPrice-p.buyPrice)*p.size-p.entryFee) }}</td></tr>
          <tr v-if="!positions.length"><td colspan="5" class="empty">暂无持仓</td></tr>
        </tbody></table></div>
      </div>

      <div class="table-card events-card">
        <div class="panel-head"><div><b>成交事件</b><span>最近 100 条模拟事件</span></div></div>
        <div class="event-list"><div v-for="e in events" :key="e.id" class="event-row"><span :class="e.type">{{ e.type === 'buy' ? 'BUY' : e.type === 'sell' ? 'TP' : 'SYS' }}</span><time>{{ fmtTime(e.ts) }}</time><p>{{ e.text }}</p></div></div>
      </div>
    </section>
  </main>
</template>
