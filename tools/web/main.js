import { createApp, ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } from '/static/vue.esm-browser.prod.js';
import { CandleChart } from '/static/chart.js';

// ---------------------------------------------------------------------------
// Global debugging helpers
// ---------------------------------------------------------------------------
console.log('[App] main.js loaded');
window.addEventListener('error', e => console.error('[Global Error]', e.message, e.filename, e.lineno, e.colno));
window.addEventListener('unhandledrejection', e => console.error('[Unhandled Promise Rejection]', e.reason));

// ---------------------------------------------------------------------------
// Configuration: API base URL. Same-origin by default; override with ?api=
// ---------------------------------------------------------------------------
const API_BASE = new URLSearchParams(location.search).get('api') || '';

// AbortController polyfill for older browsers (kept minimal)
function makeAbortSignal(timeoutMs) {
  const controller = new AbortController();
  if (timeoutMs && timeoutMs > 0) {
    setTimeout(() => controller.abort(), timeoutMs);
  }
  return controller.signal;
}

// ---------------------------------------------------------------------------
// API helpers (raw fetch — small and dependency-free)
// ---------------------------------------------------------------------------
async function apiGet(path, timeoutMs = 0) {
  console.log('[apiGet] fetching', path, timeoutMs ? 'timeout=' + timeoutMs : 'no timeout');
  const opts = { headers: { 'Accept': 'application/json' } };
  if (timeoutMs && timeoutMs > 0) {
    opts.signal = makeAbortSignal(timeoutMs);
  }
  let res;
  try {
    res = await fetch(API_BASE + path, opts);
  } catch (e) {
    console.error('[apiGet] network error', path, e.name, e.message);
    if (e.name === 'AbortError') {
      throw new Error('请求超时，请缩小查询范围或稍后重试');
    }
    throw new Error('网络错误: ' + (e.message || String(e)));
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
    console.error('[apiGet] HTTP error', path, res.status, detail);
    throw new Error(detail);
  }
  const data = await res.json();
  console.log('[apiGet] success', path, 'keys=', Object.keys(data));
  return data;
}

async function apiPost(path, timeoutMs = 0) {
  console.log('[apiPost] posting', path, timeoutMs ? 'timeout=' + timeoutMs : 'no timeout');
  const opts = {
    method: 'POST',
    headers: { 'Accept': 'application/json' },
  };
  if (timeoutMs && timeoutMs > 0) {
    opts.signal = makeAbortSignal(timeoutMs);
  }
  let res;
  try {
    res = await fetch(API_BASE + path, opts);
  } catch (e) {
    console.error('[apiPost] network error', path, e.name, e.message);
    if (e.name === 'AbortError') throw new Error('请求超时，请稍后重试');
    throw new Error('网络错误: ' + (e.message || String(e)));
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  return await res.json();
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// CandleChartCard: one trading-pair panel with a canvas chart + stats
// ---------------------------------------------------------------------------
const CandleChartCard = {
  props: ['config'],
  emits: ['remove'],
  setup(props, { emit }) {
    const loading = ref(false);
    const error = ref('');
    const candles = ref([]);
    const canvasRef = ref(null);
    let chart = null;

    const stats = computed(() => {
      if (!candles.value.length) return null;
      const first = candles.value[0];
      const last = candles.value[candles.value.length - 1];
      const open = parseFloat(first.open);
      const close = parseFloat(last.close);
      const gain = open > 0 ? ((close - open) / open) * 100 : 0;
      let hi = -Infinity, lo = Infinity;
      for (const c of candles.value) {
        hi = Math.max(hi, parseFloat(c.high));
        lo = Math.min(lo, parseFloat(c.low));
      }
      return {
        open: first.open, close: last.close,
        high: String(hi), low: String(lo),
        gain, count: candles.value.length,
        fromTs: first.timestamp, toTs: last.timestamp,
      };
    });

    async function load() {
      loading.value = true;
      error.value = '';
      candles.value = [];
      try {
        const q = new URLSearchParams({
          market_type: props.config.marketType,
          trading_pair: props.config.pair,
          interval: props.config.interval,
          span_days: String(props.config.spanDays),
          settle: props.config.settle,
        });
        if (props.config.endTime) q.set('end_time', String(props.config.endTime));
        const d = await apiGet('/api/candles?' + q.toString());
        candles.value = d.candles || [];
        await nextTick();
        draw();
      } catch (e) {
        error.value = e.message || String(e);
      } finally {
        loading.value = false;
      }
    }

    function draw() {
      if (!canvasRef.value) return;
      if (!chart) chart = new CandleChart(canvasRef.value);
      chart.setData(candles.value, {
        interval: props.config.interval,
      });
    }

    function onResize() { draw(); }

    onMounted(() => {
      load();
      window.addEventListener('resize', onResize);
    });
    onBeforeUnmount(() => {
      window.removeEventListener('resize', onResize);
      if (chart) {
        chart.destroy();
        chart = null;
      }
    });
    watch(() => props.config, load, { deep: true });

    function remove() { emit('remove'); }
    function fmtTs(ts) {
      return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + 'Z';
    }

    return { loading, error, candles, canvasRef, stats, load, remove, fmtTs };
  },
  template: `
    <div class="card">
      <div class="card-head">
        <span class="pair">{{ config.pair }}</span>
        <span class="meta">{{ config.marketType }} · {{ config.interval }} · {{ config.spanDays }}d
          <template v-if="config.marketType === 'futures'"> · {{ config.settle }}</template>
        </span>
        <button class="ghost" style="padding:3px 8px" @click="load" :disabled="loading" title="刷新">⟳</button>
        <button class="close-btn" @click="remove" title="移除">×</button>
      </div>
      <div class="card-stats" v-if="stats">
        <span>数量 <b>{{ stats.count }}</b></span>
        <span>开 <b>{{ stats.open }}</b></span>
        <span>收 <b>{{ stats.close }}</b></span>
        <span>高 <b>{{ stats.high }}</b></span>
        <span>低 <b>{{ stats.low }}</b></span>
        <span :style="{color: stats.gain >= 0 ? 'var(--up)' : 'var(--down)'}">
          涨跌 <b>{{ stats.gain >= 0 ? '+' : '' }}{{ stats.gain.toFixed(2) }}%</b>
        </span>
        <span class="hint">{{ fmtTs(stats.fromTs) }} ~ {{ fmtTs(stats.toTs) }}</span>
      </div>
      <div class="card-body">
        <canvas ref="canvasRef"></canvas>
        <div class="card-loading" v-if="loading"><span class="spinner"></span> 加载中…</div>
        <div class="card-error" v-if="error && !loading">{{ error }}</div>
      </div>
    </div>
  `,
};

// ---------------------------------------------------------------------------
// RankView: futures yearly-low-gain ranking table
// ---------------------------------------------------------------------------
const RankView = {
  props: ['active'],
  emits: ['add-pair'],
  setup(props, { emit }) {
    console.log('[RankView] setup, active=', props.active);
    const loading = ref(false);
    const error = ref('');
    const report = ref(null);
    const partialResults = ref([]);
    const filter = ref('');
    const scanLimit = ref(50); // fast preview default
    const minMarketCap = ref('');
    const maxMarketCap = ref('');
    const minOrderValue = ref('');
    const maxOrderValue = ref('');
    const progress = ref({
      completed: 0, total: 0, successful: 0, failed: 0,
      percent: 0, current_contract: null, phase: 'idle', message: '',
      stage_completed: 0, stage_total: 0, stage_percent: 0,
      elapsed_seconds: 0, details: {},
    });
    let loadToken = 0;

    async function load() {
      if (loading.value) return;
      const token = ++loadToken;
      console.log('[RankView] load start, limit=', scanLimit.value);
      loading.value = true;
      error.value = '';
      report.value = null;
      partialResults.value = [];
      progress.value = {
        completed: 0, total: Number(scanLimit.value) || 0,
        successful: 0, failed: 0, percent: 0, current_contract: null,
        phase: 'queued', message: '正在创建扫描任务',
        stage_completed: 0, stage_total: Number(scanLimit.value) || 0,
        stage_percent: 0, elapsed_seconds: 0, details: {},
      };
      try {
        const limit = Math.max(1, Math.floor(Number(scanLimit.value) || 50));
        const optionalNumber = (value, label) => {
          if (value === '' || value === null || value === undefined) return null;
          const number = Number(value);
          if (!Number.isFinite(number) || number < 0) throw new Error(label + '必须是非负数');
          return number;
        };
        const ranges = {
          min_market_cap_musd: optionalNumber(minMarketCap.value, '最小市值'),
          max_market_cap_musd: optionalNumber(maxMarketCap.value, '最大市值'),
          min_order_value: optionalNumber(minOrderValue.value, '最小下单价值下限'),
          max_order_value: optionalNumber(maxOrderValue.value, '最小下单价值上限'),
        };
        if (ranges.min_market_cap_musd !== null && ranges.max_market_cap_musd !== null &&
            ranges.min_market_cap_musd > ranges.max_market_cap_musd) {
          throw new Error('币种市值下限不能大于上限');
        }
        if (ranges.min_order_value !== null && ranges.max_order_value !== null &&
            ranges.min_order_value > ranges.max_order_value) {
          throw new Error('最小下单价值下限不能大于上限');
        }
        const params = new URLSearchParams({ max_workers: '6', limit: String(limit) });
        Object.entries(ranges).forEach(([key, value]) => {
          if (value !== null) params.set(key, String(value));
        });
        const started = await apiPost('/api/rank/jobs?' + params.toString(), 10000);
        let job = started;
        while (token === loadToken) {
          progress.value = job.progress || progress.value;
          partialResults.value = job.partial_results || partialResults.value;
          if (job.status === 'completed') {
            report.value = job.report;
            console.log('[RankView] load success, results=', (job.report?.results || []).length,
                        'stats=', job.report?.stats);
            return;
          }
          if (job.status === 'failed') {
            throw new Error(job.error || '排行扫描失败');
          }
          await sleep(500);
          job = await apiGet('/api/rank/jobs/' + encodeURIComponent(job.job_id), 10000);
        }
      } catch (e) {
        console.error('[RankView] load error', e);
        if (token === loadToken) error.value = e.message || String(e);
      } finally {
        if (token === loadToken) loading.value = false;
        console.log('[RankView] load finished, loading=', loading.value);
      }
    }

    const filtered = computed(() => {
      const sourceRows = (report.value && report.value.results) || partialResults.value;
      if (!sourceRows) return [];
      const f = filter.value.trim().toUpperCase();
      let rows = sourceRows.slice();
      if (f) rows = rows.filter(r => (r.contract || '').toUpperCase().includes(f));
      rows.sort((a, b) => (Number(b.gain_pct) || 0) - (Number(a.gain_pct) || 0));
      console.log('[RankView] filtered computed, rows=', rows.length);
      return rows;
    });

    const phaseOrder = ['queued', 'contracts', 'market_caps', 'filtering', 'scanning', 'completed'];
    const phaseLabels = {
      queued: '准备任务', contracts: '获取合约', market_caps: '查询市值',
      filtering: '筛选合约', scanning: '下载 K 线', completed: '完成', failed: '失败',
    };
    const phaseIndex = computed(() => Math.max(0, phaseOrder.indexOf(progress.value.phase)));
    const phaseLabel = computed(() => phaseLabels[progress.value.phase] || '处理中');
    const stageCompleted = computed(() => Number(progress.value.stage_completed || 0));
    const stageTotal = computed(() => Number(progress.value.stage_total || 0));
    const stagePercent = computed(() => Number(progress.value.stage_percent || 0));
    const progressDetails = computed(() => progress.value.details || {});

    function addPair(contract) {
      console.log('[RankView] addPair', contract);
      emit('add-pair', contract);
    }
    function fmt(n, d = 2) {
      if (n === null || n === undefined || n === '' || isNaN(n)) return '-';
      const num = Number(n);
      if (Math.abs(num) >= 1000) return num.toFixed(0);
      return num.toFixed(d);
    }
    function fmtCap(n) {
      if (n === null || n === undefined || n === '' || isNaN(n)) return '-';
      return Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
    }

    onMounted(() => console.log('[RankView] mounted; waiting for manual refresh'));
    watch(() => props.active, (isActive, wasActive) => {
      console.log('[RankView] active changed', wasActive, '->', isActive,
                  '; ranking refresh remains manual');
    });

    return { loading, error, report, filter, scanLimit,
             minMarketCap, maxMarketCap, minOrderValue, maxOrderValue,
             progress, partialResults, filtered, load, addPair, fmt, fmtCap,
             phaseOrder, phaseLabels, phaseIndex, phaseLabel,
             stageCompleted, stageTotal, stagePercent, progressDetails };
  },
  template: `
    <div>
      <div class="rank-controls">
        <button @click="load" :disabled="loading">
          <span v-if="loading"><span class="spinner"></span> 扫描中…</span>
          <span v-else>刷新排行</span>
        </button>
        <input v-model="filter" placeholder="筛选合约 (如 BTC)" style="width:160px" />
        <label class="hint">快速预览前
          <input v-model.number="scanLimit" type="number" min="10" max="1000" style="width:60px" />
          个活跃合约
        </label>
        <label class="rank-range">币种市值（M$）
          <input v-model="minMarketCap" type="number" min="0" step="any" placeholder="最小" />
          <span>—</span>
          <input v-model="maxMarketCap" type="number" min="0" step="any" placeholder="最大" />
        </label>
        <label class="rank-range">最小下单价值（USDT）
          <input v-model="minOrderValue" type="number" min="0" step="any" placeholder="最小" />
          <span>—</span>
          <input v-model="maxOrderValue" type="number" min="0" step="any" placeholder="最大" />
        </label>
        <span class="stats" v-if="report">
          合约 {{ report.stats.total_contracts }}
          · 筛选后 {{ report.stats.eligible_contracts ?? report.stats.total_contracts }}
          · 扫描 {{ report.stats.requested_contracts }}
          · 成功 {{ report.stats.successful_contracts }} · 失败 {{ report.stats.failed_contracts }}
          · 耗时 {{ (report.stats.elapsed_seconds||0).toFixed(1) }}s
        </span>
      </div>
      <div class="card-error" v-if="error" style="margin:10px 14px">{{ error }}</div>
      <div v-if="loading" class="rank-loading">
        <div class="rank-stage-steps" aria-label="扫描阶段">
          <span v-for="(phase, index) in phaseOrder.slice(1, 5)" :key="phase"
                :class="{ active: progress.phase === phase, done: phaseIndex > index + 1 }">
            <i>{{ phaseIndex > index + 1 ? '✓' : index + 1 }}</i>{{ phaseLabels[phase] }}
          </span>
        </div>
        <div class="rank-progress-head">
          <span><span class="spinner"></span> {{ phaseLabel }}</span>
          <b v-if="stageTotal">{{ stageCompleted }} / {{ stageTotal }}</b>
          <b v-else>处理中</b>
        </div>
        <div class="rank-progress-track" role="progressbar"
             :aria-valuenow="stagePercent" aria-valuemin="0" aria-valuemax="100">
          <div class="rank-progress-bar" :style="{ width: Math.max(0, Math.min(100, stagePercent)) + '%' }"></div>
        </div>
        <div class="rank-progress-meta">
          <span>阶段 {{ stagePercent.toFixed(1) }}%</span>
          <span>总耗时 {{ Number(progress.elapsed_seconds || 0).toFixed(1) }}s</span>
          <span v-if="progress.phase === 'scanning'">成功 {{ progress.successful || 0 }}</span>
          <span v-if="progress.phase === 'scanning'">失败 {{ progress.failed || 0 }}</span>
        </div>
        <div class="rank-progress-detail">
          <b>{{ progress.message || '正在处理…' }}</b>
          <span v-if="progressDetails.contracts_fetched !== undefined">
            已获取合约 {{ progressDetails.contracts_fetched }}
            <template v-if="progressDetails.page"> · 第 {{ progressDetails.page }} 页</template>
          </span>
          <span v-if="progress.phase === 'market_caps'">
            数据源 {{ progressDetails.provider || '准备中' }}
            · 已匹配 {{ progressDetails.symbols_matched || 0 }}/{{ progressDetails.symbols_requested || scanLimit }} 个币种
          </span>
          <span v-if="progressDetails.candidate_contracts !== undefined">
            候选 {{ progressDetails.candidate_contracts }}
            <template v-if="progressDetails.eligible_contracts !== undefined">
              · 筛选通过 {{ progressDetails.eligible_contracts }}
            </template>
          </span>
          <span v-if="progress.current_contract">当前交易对 {{ progress.current_contract }}</span>
        </div>
      </div>
      <div class="rank-wrap" v-if="filtered.length">
        <table class="rank">
          <thead>
            <tr>
              <th>#</th><th>合约</th>
              <th>市值（M$）</th><th>最小下单价值</th>
              <th>年最低</th><th>现价/收盘</th>
              <th>涨幅%</th><th>加入图表</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in filtered" :key="r.contract">
              <td>{{ i + 1 }}</td>
              <td>{{ r.contract }}</td>
              <td>{{ fmtCap(r.market_cap_musd) }}</td>
              <td>{{ fmt(r.min_order_value, 4) }}</td>
              <td>{{ fmt(r.year_low, 6) }}</td>
              <td>{{ fmt(r.current_price || r.last_price, 4) }}</td>
              <td :class="r.gain_pct >= 0 ? 'gain-up' : 'gain-down'">
                {{ r.gain_pct >= 0 ? '+' : '' }}{{ fmt(r.gain_pct, 2) }}%
              </td>
              <td><span class="add-link" @click="addPair(r.contract)">+ 添加</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="hint" style="padding:14px" v-if="!loading && !filtered.length && !error">
        点击「刷新排行」开始扫描活跃永续合约。
      </div>
    </div>
  `,
};

// ---------------------------------------------------------------------------
// Root App
// ---------------------------------------------------------------------------
const App = {
  components: { CandleChartCard, RankView },
  setup() {
    const tab = ref('candles'); // 'candles' | 'rank'
    const intervals = ref(['1h','2h','3h','4h','6h','8h','12h','1d','2d','3d','7d','30d']);
    const newPair = reactive({
      marketType: 'futures', pair: 'BTC_USDT',
      interval: '1d', spanDays: 365, settle: 'usdt',
    });
    const cards = reactive([]); // each: { id, config }
    let nextId = 1;

    function addCard(pairOverride) {
      const pair = (pairOverride || newPair.pair).trim().toUpperCase().replace(/-/g, '_');
      if (!pair) return;
      const spanDays = Math.max(1, Math.floor(Number(newPair.spanDays) || 1));
      const cfg = {
        marketType: newPair.marketType,
        pair,
        interval: newPair.interval,
        spanDays,
        settle: newPair.settle,
        endTime: null,
      };
      // Prevent exact duplicate configs.
      const exists = cards.some(c =>
        c.config.pair === cfg.pair &&
        c.config.marketType === cfg.marketType &&
        c.config.interval === cfg.interval &&
        c.config.spanDays === cfg.spanDays &&
        c.config.settle === cfg.settle
      );
      if (exists) return;
      cards.push({ id: nextId++, config: cfg });
    }
    function removeCard(id) {
      const i = cards.findIndex(c => c.id === id);
      if (i >= 0) cards.splice(i, 1);
    }
    function addPairFromRank(contract) {
      tab.value = 'candles';
      addCard(contract);
    }

    onMounted(async () => {
      try {
        const d = await apiGet('/api/valid_intervals');
        intervals.value = d.intervals.map(x => x.interval);
      } catch (_) { /* keep defaults */ }
      addCard(); // start with one default panel
    });

    return { tab, intervals, newPair, cards, addCard, removeCard, addPairFromRank };
  },
  template: `
    <div>
      <div class="header">
        <h1>GateAPI K-Line & Ranking</h1>
        <div class="tabs">
          <button :class="{active: tab==='candles'}" @click="tab='candles'">K线图</button>
          <button :class="{active: tab==='rank'}" @click="tab='rank'">合约涨幅排行</button>
        </div>
      </div>

      <div v-show="tab==='candles'">
        <div class="toolbar">
          <label>市场</label>
          <select v-model="newPair.marketType">
            <option value="futures">futures</option>
            <option value="spot">spot</option>
          </select>
          <label>交易对</label>
          <input v-model="newPair.pair" style="width:120px"
                 @keydown.enter="addCard()" placeholder="BTC_USDT" />
          <label>周期</label>
          <select v-model="newPair.interval">
            <option v-for="iv in intervals" :key="iv" :value="iv">{{ iv }}</option>
          </select>
          <label>天数</label>
          <input v-model.number="newPair.spanDays" type="number" min="1" style="width:70px" />
          <template v-if="newPair.marketType==='futures'">
            <label>结算</label>
            <select v-model="newPair.settle">
              <option value="usdt">usdt</option>
              <option value="btc">btc</option>
            </select>
          </template>
          <button @click="addCard()">+ 添加图表</button>
          <span class="hint">提示：1h 周期最多约 414 天；1d/7d/30d 可追溯至合约上线日。</span>
        </div>
        <div class="content">
          <div class="grid" v-if="cards.length">
            <candle-chart-card v-for="c in cards" :key="c.id"
              :config="c.config" @remove="removeCard(c.id)" />
          </div>
          <div class="hint" v-else style="padding:20px">点击「+ 添加图表」开始。</div>
        </div>
      </div>

      <div v-show="tab==='rank'">
        <div class="content">
          <rank-view :active="tab==='rank'" @add-pair="addPairFromRank" />
        </div>
      </div>
    </div>
  `,
};

createApp(App).mount('#app');
