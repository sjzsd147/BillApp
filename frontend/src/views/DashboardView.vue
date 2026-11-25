<template>
  <div class="dashboard-view" v-loading="loading">
    <!-- Header Controls -->
    <div class="dashboard-header">
      <div class="header-left">
        <el-radio-group v-model="timePreset" size="default" @change="handleTimePresetChange">
          <el-radio-button label="month">本月</el-radio-button>
          <el-radio-button label="3months">近 3 月</el-radio-button>
          <el-radio-button label="year">今年</el-radio-button>
          <el-radio-button label="custom">自定义</el-radio-button>
        </el-radio-group>

        <el-date-picker
          v-if="timePreset === 'custom'"
          v-model="customDateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          size="default"
          style="width: 240px"
          @change="fetchDashboardData"
        />

        <el-select
          v-model="selectedAccountId"
          placeholder="全部账户"
          clearable
          size="default"
          style="width: 180px"
          @change="fetchDashboardData"
        >
          <el-option :value="null" label="全部账户" />
          <el-option
            v-for="acc in appStore.accounts"
            :key="acc.id"
            :value="acc.id"
            :label="acc.alias || `${acc.bank}${acc.tail ? ` (${acc.tail})` : ''}`"
          />
        </el-select>

        <el-checkbox v-model="includeTransfer" @change="fetchDashboardData">
          含人情/转账
        </el-checkbox>
      </div>

      <div class="header-right">
        <el-button :icon="Refresh" circle @click="fetchDashboardData" />
      </div>
    </div>

    <!-- FX Pending Warning Banner (§8.2) -->
    <div v-if="summary && summary.fx_pending_count > 0" class="fx-pending-strip">
      <el-icon><WarningFilled /></el-icon>
      <span>有 {{ summary.fx_pending_count }} 笔外币交易汇率待获取，补齐后计入统计</span>
      <router-link :to="{ path: '/transactions', query: { fx: 'missing' } }" class="fx-link">[去查看]</router-link>
    </div>

    <!-- FX Alert Strip -->
    <div v-if="summary && summary.fx_included_count > 0" class="fx-alert-strip">
      <el-icon><InfoFilled /></el-icon>
      <span>
        包含 {{ summary.fx_included_count }} 笔外币交易，已按交易时汇率折算
        <AmountText :value="summary.fx_included_cny" bold />
        计入总支出。
      </span>
      <router-link :to="{ path: '/transactions', query: { currency: 'foreign' } }" class="fx-link">查看外币明细 &gt;</router-link>
    </div>

    <!-- 4 KPI Cards -->
    <div class="kpi-grid">
      <!-- Card 1: Total Net Expense -->
      <div class="kpi-card" @click="goToTransactions('expense')">
        <div class="kpi-title">总支出（净额）</div>
        <div class="kpi-value">
          <AmountText :value="summary?.expense_net ?? summary?.total_expense_cents ?? 0" size="26px" bold />
        </div>
        <div class="kpi-footnote">
          <span v-if="expenseMoM !== null" :class="expenseMoM <= 0 ? 'mom-down' : 'mom-up'">
            {{ expenseMoM <= 0 ? '↓' : '↑' }} {{ Math.abs(expenseMoM) }}% 较上期
          </span>
          <span v-else class="text-sub">无上期对比</span>
          <span class="text-sub">· 退款已冲抵</span>
        </div>
      </div>

      <!-- Card 2: Total Income -->
      <div class="kpi-card" @click="goToTransactions('income')">
        <div class="kpi-title">总收入</div>
        <div class="kpi-value">
          <AmountText
            :value="summary?.income ?? summary?.total_income_cents ?? 0"
            direction="income"
            size="26px"
            bold
          />
        </div>
        <div class="kpi-footnote">
          <span v-if="incomeMoM !== null" :class="incomeMoM >= 0 ? 'mom-good' : 'mom-bad'">
            {{ incomeMoM >= 0 ? '↑' : '↓' }} {{ Math.abs(incomeMoM) }}% 较上期
          </span>
          <span v-else class="text-sub">无上期对比</span>
          <span class="text-sub">· 收入 {{ summary?.income_count ?? 0 }} 笔</span>
        </div>
      </div>

      <!-- Card 3: Net Balance / Savings -->
      <div class="kpi-card">
        <div class="kpi-title">结余</div>
        <div class="kpi-value">
          <AmountText
            :value="summary?.balance ?? summary?.net_savings_cents ?? 0"
            :show-sign="true"
            size="26px"
            bold
          />
        </div>
        <div class="kpi-footnote">
          <span class="text-sub">储蓄率 {{ (summary?.savings_rate ?? 0).toFixed(1) }}%</span>
        </div>
      </div>

      <!-- Card 4: Daily Average -->
      <div class="kpi-card">
        <div class="kpi-title">日均支出</div>
        <div class="kpi-value">
          <AmountText :value="summary?.daily_avg ?? summary?.daily_average_cents ?? 0" size="26px" bold />
        </div>
        <div class="kpi-footnote">
          <span class="text-sub">按 {{ summary?.days ?? 30 }} 天计</span>
        </div>
      </div>
    </div>

    <!-- Charts Row 1: Trend & Category Breakdown -->
    <div class="charts-row-split">
      <div class="chart-card flex-1-5">
        <div class="chart-header">
          <div class="chart-title">月度收支趋势</div>
        </div>
        <div ref="trendChartRef" class="chart-container" />
      </div>

      <div class="chart-card flex-1">
        <div class="chart-header">
          <div class="chart-title">支出分类构成</div>
        </div>
        <div ref="categoryChartRef" class="chart-container" />
      </div>
    </div>

    <!-- Charts Row 2: Top Merchants, Funding Distribution & Heatmap -->
    <div class="charts-row-triple">
      <!-- Top Merchants -->
      <div class="chart-card flex-1">
        <div class="chart-header">
          <div class="chart-title">商户 Top 10</div>
        </div>
        <div class="merchant-rank-list">
          <div
            v-for="(item, idx) in merchantItems"
            :key="idx"
            class="merchant-rank-item"
            @click="searchMerchant(item.merchant)"
          >
            <span class="rank-idx" :class="{ 'top-3': idx < 3 }">{{ idx + 1 }}</span>
            <span class="merchant-name" :title="item.merchant">{{ item.merchant }}</span>
            <div class="rank-bar-bg">
              <div
                class="rank-bar-fill"
                :style="{ width: `${item.percentage}%` }"
              />
            </div>
            <AmountText :value="item.expense_cents" class="rank-amount" />
          </div>
          <EmptyState v-if="merchantItems.length === 0" title="暂无商户排行" />
        </div>
      </div>

      <!-- Funding Account Distribution -->
      <div class="chart-card flex-1">
        <div class="chart-header">
          <div class="chart-title">账户出资分布</div>
        </div>
        <div ref="accountChartRef" class="chart-container" />
      </div>

      <!-- Spending Period Heatmap -->
      <div class="chart-card flex-1">
        <div class="chart-header">
          <div class="chart-title">消费时段热力</div>
        </div>
        <div ref="heatmapChartRef" class="chart-container" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { Refresh, InfoFilled, WarningFilled } from '@element-plus/icons-vue';
import { useAppStore } from '@/stores/app';
import {
  getStatsSummary,
  getStatsTrend,
  getCategoryStats,
  getMerchantStats,
  getAccountStats,
  getSpendingHeatmap,
} from '@/api/stats';
import { useChart } from '@/composables/useChart';
import AmountText from '@/components/AmountText.vue';
import EmptyState from '@/components/EmptyState.vue';
import { currentMonthRange, last3MonthsRange, currentYearRange } from '@/utils/datetime';
import type {
  StatsSummary,
  TrendItem,
  MerchantRankItem,
} from '@/types/api';

const router = useRouter();
const appStore = useAppStore();

const loading = ref(false);
const timePreset = ref<'month' | '3months' | 'year' | 'custom'>('month');
const customDateRange = ref<[string, string] | null>(null);
const selectedAccountId = ref<number | null>(null);
const includeTransfer = ref(false);

const summary = ref<StatsSummary | null>(null);
const merchantItems = ref<MerchantRankItem[]>([]);
const lastChartPayloads = {
  trend: null as TrendItem[] | null,
  category: null as any,
  account: null as any,
  heatmap: null as any,
};

// Chart Container Refs
const trendChartRef = ref<HTMLElement | null>(null);
const categoryChartRef = ref<HTMLElement | null>(null);
const accountChartRef = ref<HTMLElement | null>(null);
const heatmapChartRef = ref<HTMLElement | null>(null);

// ECharts Composables
const { setOption: setTrendOption } = useChart(trendChartRef);
const { setOption: setCategoryOption } = useChart(categoryChartRef);
const { setOption: setAccountOption } = useChart(accountChartRef);
const { setOption: setHeatmapOption } = useChart(heatmapChartRef);

const dateFilter = computed<[string, string]>(() => {
  if (timePreset.value === 'month') return currentMonthRange();
  if (timePreset.value === '3months') return last3MonthsRange();
  if (timePreset.value === 'year') return currentYearRange();
  if (customDateRange.value && customDateRange.value.length === 2) return customDateRange.value;
  return currentMonthRange();
});

const expenseMoM = computed(() => {
  if (!summary.value || !summary.value.prev) return null;
  const current = summary.value.expense_net ?? summary.value.total_expense_cents;
  const prev = summary.value.prev.expense_net;
  if (!prev || prev <= 0) return null;
  return Number((((current - prev) / prev) * 100).toFixed(1));
});

const incomeMoM = computed(() => {
  if (!summary.value || !summary.value.prev) return null;
  const current = summary.value.income ?? summary.value.total_income_cents;
  const prev = summary.value.prev.income;
  if (!prev || prev <= 0) return null;
  return Number((((current - prev) / prev) * 100).toFixed(1));
});

function handleTimePresetChange() {
  if (timePreset.value !== 'custom') {
    fetchDashboardData();
  }
}

let dashboardFetchSeq = 0;

async function fetchDashboardData() {
  const seq = ++dashboardFetchSeq;
  loading.value = true;
  const [from, to] = dateFilter.value;
  const params: Record<string, any> = {
    from,
    to,
    include_transfer: includeTransfer.value,
  };
  if (selectedAccountId.value !== null) {
    params.account_id = selectedAccountId.value;
  }

  try {
    const [sumData, trendData, catData, merData, accData, heatData] = await Promise.all([
      getStatsSummary(params),
      getStatsTrend(params),
      getCategoryStats(params),
      getMerchantStats({ ...params, limit: 10 }),
      getAccountStats({ ...params, by: 'fund_account' }),
      getSpendingHeatmap({ ...params, mode: 'weekday_hour' }),
    ]);

    if (seq !== dashboardFetchSeq) return;

    summary.value = sumData;
    merchantItems.value = merData.items;
    lastChartPayloads.trend = trendData;
    lastChartPayloads.category = catData;
    lastChartPayloads.account = accData;
    lastChartPayloads.heatmap = heatData;

    renderTrendChart(trendData);
    renderCategoryChart(catData);
    renderAccountChart(accData);
    renderHeatmapChart(heatData);
  } catch (e) {
    if (seq === dashboardFetchSeq) {
      console.error('Failed to load dashboard data', e);
    }
  } finally {
    if (seq === dashboardFetchSeq) {
      loading.value = false;
    }
  }
}

function renderTrendChart(trends: TrendItem[]) {
  const masked = appStore.amountPrivacy;
  const periods = trends.map((t) => t.period);
  const expenseSeries = trends.map((t) => t.expense_cents / 100);
  const incomeSeries = trends.map((t) => t.income_cents / 100);

  setTrendOption({
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        let str = `<div style="font-weight:600;margin-bottom:4px;">${params[0].axisValue}</div>`;
        for (const item of params) {
          const valText = masked ? '¥••••' : `¥${Number(item.value).toFixed(2)}`;
          str += `<div style="display:flex;justify-content:space-between;gap:12px;font-size:12px;">
            <span>${item.marker} ${item.seriesName}</span>
            <span style="font-weight:600;">${valText}</span>
          </div>`;
        }
        return str;
      },
    },
    legend: {
      data: ['净支出', '总收入'],
      top: 0,
      right: 0,
    },
    grid: {
      left: '3%',
      right: '3%',
      bottom: '5%',
      top: '18%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: periods,
      axisLine: { lineStyle: { color: '#E6E8EB' } },
      axisLabel: { color: '#5B6472', fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisLabel: {
        color: '#8B94A3',
        fontSize: 11,
        formatter: masked ? () => '••••' : '{value}',
      },
      splitLine: { lineStyle: { color: '#F0F1F3', type: 'dashed' } },
    },
    series: [
      {
        name: '净支出',
        type: 'bar',
        data: expenseSeries,
        itemStyle: { color: '#2F6BFF', borderRadius: [4, 4, 0, 0] },
        barMaxWidth: 28,
      },
      {
        name: '总收入',
        type: 'line',
        data: incomeSeries,
        itemStyle: { color: '#15803D' },
        lineStyle: { width: 2 },
        symbolSize: 6,
      },
    ],
  });
}

function renderCategoryChart(data: any) {
  const masked = appStore.amountPrivacy;
  const items = data.items || [];
  const pieData = items.map((c: any) => ({
    name: c.name,
    value: c.amount_cents / 100,
  }));

  setCategoryOption({
    tooltip: {
      trigger: 'item',
      formatter: masked ? '{b}: ¥•••• ({d}%)' : '{b}: ¥{c} ({d}%)',
    },
    series: [
      {
        type: 'pie',
        radius: ['45%', '70%'],
        center: ['50%', '50%'],
        avoidLabelOverlap: true,
        itemStyle: {
          borderRadius: 4,
          borderColor: '#fff',
          borderWidth: 2,
        },
        label: {
          show: true,
          position: 'outside',
          formatter: '{b} {d}%',
          fontSize: 11,
          color: '#5B6472',
        },
        data: pieData,
      },
    ],
  });
}

function renderAccountChart(data: any) {
  const masked = appStore.amountPrivacy;
  const items = data.items || [];
  const pieData = items.map((a: any) => ({
    name: a.name || '未指定账户',
    value: a.total_out_cents / 100,
  }));

  setAccountOption({
    tooltip: {
      trigger: 'item',
      formatter: masked ? '{b}: 出资 ¥•••• ({d}%)' : '{b}: 出资 ¥{c} ({d}%)',
    },
    series: [
      {
        type: 'pie',
        radius: ['40%', '65%'],
        center: ['50%', '50%'],
        data: pieData,
        label: {
          show: true,
          formatter: masked ? '{b}\n¥••••' : '{b}\n¥{c}',
          fontSize: 10,
          color: '#5B6472',
        },
      },
    ],
  });
}

function renderHeatmapChart(data: any) {
  const masked = appStore.amountPrivacy;
  const items = data.weekday_hour_items || [];
  // 7 days x 24 hours format: [hour, day, count]
  const heatData = items.map((h: any) => [h.hour, h.weekday - 1, h.expense_cents / 100]);
  const hours = Array.from({ length: 24 }, (_, i) => `${i}h`);
  const days = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

  setHeatmapOption({
    tooltip: {
      position: 'top',
      formatter: (p: any) =>
        `${days[p.value[1]]} ${p.value[0]}:00 支出: ${masked ? '¥••••' : `¥${Number(p.value[2]).toFixed(2)}`}`,
    },
    grid: {
      top: 10,
      bottom: 25,
      left: 35,
      right: 10,
    },
    xAxis: {
      type: 'category',
      data: hours,
      splitArea: { show: true },
      axisLabel: { fontSize: 9, interval: 3 },
    },
    yAxis: {
      type: 'category',
      data: days,
      splitArea: { show: true },
      axisLabel: { fontSize: 10 },
    },
    visualMap: {
      min: 0,
      max: 500,
      calculable: false,
      show: false,
      inRange: {
        color: ['#F1F3F6', '#E7EEFF', '#BED0FF', '#7FA3FB', '#2F6BFF'],
      },
    },
    series: [
      {
        type: 'heatmap',
        data: heatData,
        itemStyle: {
          borderColor: '#fff',
          borderWidth: 1,
        },
      },
    ],
  });
}

function goToTransactions(direction: string) {
  router.push({
    path: '/transactions',
    query: {
      direction,
      from: dateFilter.value[0],
      to: dateFilter.value[1],
    },
  });
}

function searchMerchant(name: string) {
  router.push({
    path: '/transactions',
    query: {
      q: name,
      from: dateFilter.value[0],
      to: dateFilter.value[1],
    },
  });
}

watch(
  () => appStore.amountPrivacy,
  () => {
    if (lastChartPayloads.trend) renderTrendChart(lastChartPayloads.trend);
    if (lastChartPayloads.category) renderCategoryChart(lastChartPayloads.category);
    if (lastChartPayloads.account) renderAccountChart(lastChartPayloads.account);
    if (lastChartPayloads.heatmap) renderHeatmapChart(lastChartPayloads.heatmap);
  }
);

onMounted(() => {
  fetchDashboardData();
});
</script>

<style scoped>
.dashboard-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.dashboard-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}

.header-left {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.fx-pending-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: rgba(217, 119, 6, 0.08);
  border: 1px solid rgba(217, 119, 6, 0.3);
  border-radius: var(--radius-md);
  font-size: 13px;
  color: #b45309;
}

.fx-pending-strip .el-icon {
  color: #d97706;
  font-size: 16px;
}

.fx-alert-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: var(--primary-soft-2);
  border: 1px solid rgba(47, 107, 255, 0.2);
  border-radius: var(--radius-md);
  font-size: 13px;
  color: var(--text-2);
}

.fx-alert-strip .el-icon {
  color: var(--primary);
  font-size: 16px;
}

.fx-link {
  margin-left: auto;
  font-weight: 500;
  font-size: 12.5px;
}

/* 4 KPI Cards */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

@media (max-width: 1080px) {
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 18px 20px;
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s;
}

.kpi-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-sm);
}

.kpi-title {
  font-size: 13px;
  color: var(--text-3);
  font-weight: 500;
  margin-bottom: 8px;
}

.kpi-value {
  margin-bottom: 8px;
}

.kpi-footnote {
  font-size: 12px;
  display: flex;
  gap: 6px;
  align-items: center;
}

.mom-down,
.mom-good {
  color: var(--success); /* Drop in expense / rise in income is green */
  font-weight: 600;
}

.mom-up,
.mom-bad {
  color: var(--warning);
  font-weight: 600;
}

.text-sub {
  color: var(--text-3);
}

/* Chart Rows */
.charts-row-split {
  display: grid;
  grid-template-columns: 1.55fr 1fr;
  gap: 16px;
}

.charts-row-triple {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}

@media (max-width: 1180px) {
  .charts-row-split,
  .charts-row-triple {
    grid-template-columns: 1fr;
  }
}

.chart-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px;
  display: flex;
  flex-direction: column;
}

.chart-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.chart-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.chart-container {
  width: 100%;
  height: 260px;
}

/* Merchant Ranking */
.merchant-rank-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
  max-height: 260px;
}

.merchant-rank-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 12.5px;
}

.merchant-rank-item:hover {
  background: var(--surface-hover);
}

.rank-idx {
  width: 18px;
  font-weight: 600;
  color: var(--text-3);
  text-align: center;
}

.rank-idx.top-3 {
  color: var(--primary);
}

.merchant-name {
  width: 80px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text);
  font-weight: 500;
}

.rank-bar-bg {
  flex: 1;
  height: 6px;
  background: var(--border-2);
  border-radius: 3px;
  overflow: hidden;
}

.rank-bar-fill {
  height: 100%;
  background: var(--primary);
  border-radius: 3px;
}

.rank-amount {
  font-size: 12px;
  width: 65px;
  text-align: right;
}
</style>
