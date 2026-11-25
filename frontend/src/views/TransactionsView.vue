<template>
  <div class="transactions-view">
    <!-- Filter Bar Card -->
    <div class="filter-card">
      <div class="filter-row">
        <!-- Date Range -->
        <el-date-picker
          v-model="txnStore.filters.dateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          size="default"
          style="width: 240px"
          @change="handleFilterChange"
        />

        <!-- Account Filter -->
        <el-select
          v-model="txnStore.filters.accountId"
          placeholder="全部账户"
          clearable
          size="default"
          style="width: 160px"
          @change="handleFilterChange"
        >
          <el-option :value="null" label="全部账户" />
          <el-option
            v-for="acc in appStore.accounts"
            :key="acc.id"
            :value="acc.id"
            :label="acc.alias || `${acc.bank}${acc.tail ? ` (${acc.tail})` : ''}`"
          />
        </el-select>

        <!-- Direction Filter -->
        <el-select
          v-model="txnStore.filters.direction"
          placeholder="全部收支"
          clearable
          size="default"
          style="width: 120px"
          @change="handleFilterChange"
        >
          <el-option :value="null" label="全部收支" />
          <el-option value="expense" label="支出" />
          <el-option value="income" label="收入" />
          <el-option value="refund" label="退款" />
          <el-option value="transfer" label="转账" />
        </el-select>

        <!-- Category Picker Filter -->
        <div style="width: 170px">
          <CategoryPicker
            v-model="txnStore.filters.categoryId"
            placeholder="全部分类"
            size="default"
            @change="handleFilterChange"
          />
        </div>

        <!-- Currency Filter (Milestone 6) -->
        <el-select
          v-model="txnStore.filters.currency"
          placeholder="全部币种"
          clearable
          size="default"
          style="width: 120px"
          @change="handleFilterChange"
        >
          <el-option :value="null" label="全部币种" />
          <el-option value="CNY" label="人民币 (CNY)" />
          <el-option value="foreign" label="外币 (全部)" />
          <el-option value="USD" label="美元 (USD)" />
          <el-option value="EUR" label="欧元 (EUR)" />
          <el-option value="HKD" label="港币 (HKD)" />
          <el-option value="GBP" label="英镑 (GBP)" />
          <el-option value="JPY" label="日元 (JPY)" />
        </el-select>

        <!-- Search Input -->
        <el-input
          v-model="txnStore.filters.q"
          placeholder="搜索对方/说明/订单号…"
          clearable
          size="default"
          style="width: 200px"
          :prefix-icon="Search"
          @keyup.enter="handleFilterChange"
          @clear="handleFilterChange"
        />

        <el-checkbox
          v-model="mergeDuplicates"
          size="default"
          @change="handleMergeDuplicatesToggle"
        >
          合并同笔消费
        </el-checkbox>

        <el-checkbox
          v-model="filterUnclassified"
          size="default"
          @change="handleUnclassifiedToggle"
        >
          仅未分类
        </el-checkbox>

        <el-checkbox
          v-model="filterMissingFx"
          size="default"
          @change="handleMissingFxToggle"
        >
          待补汇率
        </el-checkbox>

        <el-checkbox
          v-model="showExcluded"
          size="default"
          @change="handleExcludedToggle"
        >
          显示已排除
        </el-checkbox>

        <div class="filter-actions">
          <el-button size="default" @click="handleReset">重置</el-button>
          <el-button
            type="success"
            plain
            size="default"
            :loading="aiClassifying"
            @click="handleAiClassify"
          >
            AI 智能分类
          </el-button>
          <el-button type="primary" plain size="default" :icon="Download" @click="handleExport">
            导出 Excel
          </el-button>
        </div>
      </div>
    </div>

    <!-- Data Grid Card -->
    <div ref="gridCardRef" class="grid-card" v-loading="txnStore.loading">
      <!-- Grid Summary Line -->
      <div class="grid-summary-line">
        <span class="summary-text">
          当前筛选: <strong>{{ txnStore.total }}</strong> 笔交易
        </span>
      </div>

      <!-- Main Transactions Table -->
      <el-table
        ref="tableRef"
        :data="txnStore.items"
        border
        :fit="false"
        :size="appStore.tableDensity === 'compact' ? 'small' : 'default'"
        :class="{ 'table-density-compact': appStore.tableDensity === 'compact' }"
        style="width: 100%"
        @selection-change="handleSelectionChange"
        @header-dragend="handleColumnDragEnd"
      >
        <el-table-column type="selection" column-key="selection" :width="colWidths.selection" :resizable="false" align="center" />
        <el-table-column label="交易时间" column-key="txn_time" prop="txn_time" :width="colWidths.txn_time" sortable />
        <el-table-column label="账户" column-key="account" :width="colWidths.account" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="acc-cell">
              <span class="acc-text">
                {{ row.account_bank || '银行账户' }}
                <span v-if="row.account_tail" class="text-sub">({{ row.account_tail }})</span>
              </span>
              <span v-if="getSecondaryAccountLabel(row)" class="acc-merged-chip">
                + {{ getSecondaryAccountLabel(row) }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="来源" column-key="source" :width="colWidths.source">
          <template #default="{ row }">
            <div class="source-cell">
              <span>{{ formatSourceName(row.source) }}</span>
              <el-tooltip
                v-if="row.merged_count && row.merged_count > 0"
                :content="formatMergedTooltip(row)"
                placement="top"
              >
                <span class="merged-badge" @click.stop="viewDetail(row)">
                  合并 {{ row.merged_count + 1 }} 源
                </span>
              </el-tooltip>
              <span v-else-if="row.is_duplicate" class="dup-copy-badge">
                已归并副本
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="对方 / 商户" column-key="counterparty" prop="counterparty" :width="colWidths.counterparty" show-overflow-tooltip />
        <el-table-column label="说明" column-key="description" prop="description" :width="colWidths.description" show-overflow-tooltip />
        <el-table-column label="收支" column-key="direction" :width="colWidths.direction" align="center">
          <template #default="{ row }">
            <DirectionTag :direction="row.direction" />
          </template>
        </el-table-column>
        <el-table-column label="金额" column-key="amount" :width="colWidths.amount" align="right">
          <template #default="{ row }">
            <AmountText
              :value="row.amount_cents"
              :currency="row.currency"
              :amount-cny="row.amount_cny"
              :fx-rate="row.fx_rate"
              :fx-rate-at="row.fx_rate_at"
              :fx-rate-source="row.fx_rate_source"
              :direction="row.direction"
              bold
              @click-pending-fx="openManualRateDialog(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="分类" column-key="category" :width="colWidths.category">
          <template #default="{ row }">
            <div class="cell-category" @click="openInlineCategoryEdit(row)">
              <span v-if="row.category_name" class="cat-label">
                {{ row.category_name }}
              </span>
              <span v-else class="cat-empty">未分类</span>
              <el-icon class="cat-edit-icon"><Edit /></el-icon>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" column-key="status" :width="colWidths.status" align="center">
          <template #default="{ row }">
            <StatusTag :status="row.status" :excluded="row.excluded" />
          </template>
        </el-table-column>
        <el-table-column label="详情" column-key="actions" :width="colWidths.actions" :resizable="false" align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="viewDetail(row)">
              查看
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <EmptyState v-if="!txnStore.loading && txnStore.items.length === 0" title="当前筛选条件下没有记录">
        <el-button @click="handleReset">清空所有筛选</el-button>
      </EmptyState>

      <!-- Pagination -->
      <div class="pagination-bar">
        <el-pagination
          v-model:current-page="txnStore.pagination.page"
          v-model:page-size="txnStore.pagination.pageSize"
          :page-sizes="[20, 50, 100, 200]"
          :total="txnStore.total"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="handlePaginationChange"
          @current-change="handlePaginationChange"
        />
      </div>
    </div>

    <!-- Floating Batch Operations Bar -->
    <div v-if="txnStore.selectedIds.length > 0" class="floating-batch-bar">
      <div class="batch-info">
        已选择 <strong>{{ txnStore.selectedIds.length }}</strong> 项
      </div>
      <div class="batch-actions">
        <el-button size="small" @click="openBulkCategoryModal">批量修改分类</el-button>
        <el-button size="small" @click="handleBulkExclude(1)">批量排除</el-button>
        <el-button size="small" @click="handleBulkExclude(0)">恢复正常</el-button>
        <el-button size="small" @click="clearTableSelection">取消选择</el-button>
      </div>
    </div>

    <!-- Inline Category Edit Dialog -->
    <el-dialog v-model="showCategoryDialog" title="修改分类" width="380px">
      <div style="padding: 10px 0;">
        <CategoryPicker v-model="editingCategoryId" placeholder="请选择新分类" />
      </div>
      <template #footer>
        <el-button @click="showCategoryDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingCategory" @click="saveCategoryEdit">
          确定保存
        </el-button>
      </template>
    </el-dialog>

    <!-- Bulk Category Edit Dialog -->
    <el-dialog v-model="showBulkCategoryDialog" title="批量修改分类" width="380px">
      <div style="padding: 10px 0;">
        <CategoryPicker v-model="bulkCategoryId" placeholder="请选择目标分类" />
      </div>
      <template #footer>
        <el-button @click="showBulkCategoryDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingCategory" @click="saveBulkCategory">
          批量更新
        </el-button>
      </template>
    </el-dialog>

    <!-- Detail Drawer -->
    <el-drawer v-model="showDetailDrawer" title="交易详情" size="420px">
      <div v-if="activeDetailTxn" class="detail-drawer-content">
        <div class="detail-amount-box">
          <div class="label">交易金额</div>
          <AmountText
            :value="activeDetailTxn.amount_cents"
            :currency="activeDetailTxn.currency || 'CNY'"
            :amount-cny="activeDetailTxn.amount_cny"
            :fx-rate="activeDetailTxn.fx_rate"
            :fx-rate-at="activeDetailTxn.fx_rate_at"
            :fx-rate-source="activeDetailTxn.fx_rate_source"
            :direction="activeDetailTxn.direction"
            bold
            size="24px"
            @click-pending-fx="openManualRateDialog(activeDetailTxn)"
          />
        </div>

        <div class="detail-list">
          <div class="row">
            <span class="label">交易 ID:</span>
            <span class="val">{{ activeDetailTxn.id }}</span>
          </div>
          <div class="row">
            <span class="label">交易时间:</span>
            <span class="val">{{ activeDetailTxn.txn_time }}</span>
          </div>
          <div class="row">
            <span class="label">对方/商户:</span>
            <span class="val">{{ activeDetailTxn.counterparty || '-' }}</span>
          </div>
          <div class="row">
            <span class="label">说明:</span>
            <span class="val">{{ activeDetailTxn.description || '-' }}</span>
          </div>
          <div class="row">
            <span class="label">渠道/方式:</span>
            <span class="val">{{ activeDetailTxn.channel || activeDetailTxn.pay_method || '-' }}</span>
          </div>
          <div class="row">
            <span class="label">订单号:</span>
            <span class="val monospace">{{ activeDetailTxn.order_id || '-' }}</span>
          </div>
          <div class="row">
            <span class="label">当前分类:</span>
            <span class="val">{{ activeDetailTxn.category_name || '未分类' }}</span>
          </div>

          <!-- FX Section (Milestone 6) -->
          <div v-if="activeDetailTxn.currency && activeDetailTxn.currency !== 'CNY'" class="fx-detail-block">
            <div class="row">
              <span class="label">原始币种:</span>
              <span class="val">{{ activeDetailTxn.currency }}</span>
            </div>
            <div class="row">
              <span class="label">交易时汇率:</span>
              <span class="val">
                <template v-if="activeDetailTxn.fx_rate">
                  {{ activeDetailTxn.fx_rate }}
                  <span class="text-sub">({{ activeDetailTxn.fx_rate_source === 'online' ? '联网' : activeDetailTxn.fx_rate_source === 'manual' ? '手动' : '无' }})</span>
                </template>
                <template v-else>
                  <span style="color: var(--warning, #d97706);">待获取</span>
                </template>
              </span>
            </div>
            <div class="row">
              <span class="label">折算人民币:</span>
              <span class="val">
                <template v-if="activeDetailTxn.amount_cny !== null && activeDetailTxn.amount_cny !== undefined">
                  <AmountText :value="activeDetailTxn.amount_cny" bold />
                </template>
                <template v-else>
                  <span style="color: var(--warning, #d97706);">汇率待获取，未计入统计</span>
                </template>
              </span>
            </div>
            <div class="fx-action-buttons">
              <el-button size="small" type="primary" plain @click="openManualRateDialog(activeDetailTxn)">
                补录汇率
              </el-button>
              <el-button size="small" :loading="retryingFx" @click="handleRetryFx(activeDetailTxn)">
                重试获取汇率
              </el-button>
            </div>
          </div>
        </div>

          <div v-if="detailMergedRecords.length > 0 || detailOtherRelations.length > 0" class="detail-merged-section">
            <div v-if="detailMergedRecords.length > 0" class="detail-sub-block">
              <div class="detail-sub-title">
                同笔消费已合并记录 ({{ detailMergedRecords.length + 1 }} 个来源)
              </div>
              <div
                v-for="item in detailMergedRecords"
                :key="item.txn.id"
                class="merged-record-card"
              >
                <div class="merged-card-top">
                  <span class="merged-card-source">
                    {{ formatSourceName(item.txn.source) }} · {{ item.txn.account_bank || '关联账户' }}<template v-if="item.txn.account_tail">({{ item.txn.account_tail }})</template>
                  </span>
                  <span class="merged-card-amount">
                    <AmountText
                      :value="item.txn.amount_cents || 0"
                      :currency="item.txn.currency || 'CNY'"
                      :amount-cny="item.txn.amount_cny"
                      :direction="item.txn.direction"
                      bold
                    />
                  </span>
                </div>
                <div class="merged-card-meta">
                  <span>{{ item.txn.txn_time }}</span>
                  <span v-if="item.txn.counterparty">{{ item.txn.counterparty }}</span>
                </div>
                <div v-if="item.txn.description" class="merged-card-desc">
                  {{ item.txn.description }}
                </div>
              </div>
            </div>

            <div v-if="detailOtherRelations.length > 0" class="detail-sub-block">
              <div class="detail-sub-title">关联流水</div>
              <div
                v-for="(rel, idx) in detailOtherRelations"
                :key="`${rel.relation}-${idx}`"
                class="merged-record-card"
              >
                <div class="merged-card-top">
                  <span class="merged-card-source">
                    {{ formatRelationLabel(rel.relation) }} · {{ rel.txn.counterparty || formatSourceName(rel.txn.source) || `#${rel.txn.id}` }}
                  </span>
                  <el-button link type="primary" size="small" @click="jumpToRelatedTxn(rel.txn.id)">
                    查看 #{{ rel.txn.id }}
                  </el-button>
                </div>
                <div class="merged-card-meta">
                  <span>{{ rel.txn.txn_time }}</span>
                  <AmountText
                    :value="rel.txn.amount_cents || 0"
                    :currency="rel.txn.currency || 'CNY'"
                    :amount-cny="rel.txn.amount_cny"
                    :direction="rel.txn.direction"
                  />
                </div>
              </div>
            </div>
          </div>

        <div class="drawer-actions">
          <el-button
            type="danger"
            plain
            size="small"
            @click="deleteSingleTxn(activeDetailTxn.id)"
          >
            删除此交易
          </el-button>
        </div>
      </div>
    </el-drawer>

    <!-- Manual FX Rate Dialog -->
    <el-dialog v-model="showManualRateDialog" title="手动补录汇率" width="400px">
      <el-form label-position="top">
        <el-form-item label="币种">
          <el-input v-model="manualFxForm.currency" disabled />
        </el-form-item>
        <el-form-item label="汇率 (1 原币 = ? CNY)">
          <el-input-number
            v-model="manualFxForm.rate"
            :min="0.0001"
            :step="0.01"
            :precision="4"
            style="width: 100%"
            placeholder="例如: 7.2013"
          />
        </el-form-item>
        <el-form-item label="生效日期">
          <el-date-picker
            v-model="manualFxForm.effective_date"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="生效日期"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="备注说明">
          <el-input v-model="manualFxForm.note" placeholder="例如: 银行当期信用卡账单汇率" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="manualFxForm.apply_to_all_missing">
            应用至所有待补汇率的该币种交易
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showManualRateDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingFx" @click="handleSaveManualFx">确定补录</el-button>
      </template>
    </el-dialog>

    <!-- Rule Proposal Modal -->
    <RuleProposalModal ref="ruleProposalModalRef" @applied="handleProposalApplied" />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import dayjs from 'dayjs';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Search, Download, Edit } from '@element-plus/icons-vue';
import { useTransactionsStore } from '@/stores/transactions';
import { useAppStore } from '@/stores/app';
import { getTransaction, patchTransaction, getExportUrl } from '@/api/transactions';
import { saveFxRate, retryTxnFx } from '@/api/fx';
import { classifyWithLLM } from '@/api/llm';
import AmountText from '@/components/AmountText.vue';
import DirectionTag from '@/components/DirectionTag.vue';
import StatusTag from '@/components/StatusTag.vue';
import CategoryPicker from '@/components/CategoryPicker.vue';
import EmptyState from '@/components/EmptyState.vue';
import RuleProposalModal from '@/components/RuleProposalModal.vue';
import type { TransactionItem, RelatedTxnInfo } from '@/types/api';

const route = useRoute();
const router = useRouter();
const txnStore = useTransactionsStore();
const appStore = useAppStore();

const showExcluded = ref(false);
const mergeDuplicates = ref(true);
const filterUnclassified = ref(false);
const filterMissingFx = ref(false);

const tableRef = ref<any>(null);
const gridCardRef = ref<HTMLElement | null>(null);
const COL_WIDTHS_STORAGE_KEY = 'billapp.transactions.colWidths.v3';
const COL_ORDER = [
  'selection',
  'txn_time',
  'account',
  'source',
  'counterparty',
  'description',
  'direction',
  'amount',
  'category',
  'status',
  'actions',
] as const;
type ColKey = (typeof COL_ORDER)[number];

const BASE_COL_WIDTHS: Record<ColKey, number> = {
  selection: 40,
  txn_time: 146,
  account: 118,
  source: 76,
  counterparty: 126,
  description: 126,
  direction: 80,
  amount: 116,
  category: 112,
  status: 84,
  actions: 64,
};

const MIN_COL_WIDTHS: Record<ColKey, number> = {
  selection: 40,
  txn_time: 82,
  account: 60,
  source: 52,
  counterparty: 60,
  description: 60,
  direction: 74,
  amount: 62,
  category: 60,
  status: 74,
  actions: 54,
};

const RESIZABLE_COL_KEYS: ColKey[] = COL_ORDER.filter((k) => k !== 'selection');
const colWidths = reactive<Record<ColKey, number>>({ ...BASE_COL_WIDTHS });
let hasCustomColWidths = false;
let gridResizeObserver: ResizeObserver | null = null;
let dragCleanup: (() => void) | null = null;

function getTableTargetWidth(): number {
  const tableEl = tableRef.value?.$el as HTMLElement | undefined;
  if (tableEl && tableEl.clientWidth > 0) {
    return Math.floor(tableEl.clientWidth);
  }
  if (gridCardRef.value && gridCardRef.value.clientWidth > 32) {
    return Math.floor(gridCardRef.value.clientWidth - 32);
  }
  return 0;
}

function syncTableColumnWidths(targetWidths: Record<ColKey, number> = colWidths) {
  const cols = tableRef.value?.store?.states?.columns?.value;
  if (!Array.isArray(cols)) return;
  for (const col of cols) {
    const k = col?.columnKey as ColKey | undefined;
    if (k && k in targetWidths) {
      col.width = targetWidths[k];
      col.realWidth = targetWidths[k];
    }
  }
}

function saveColWidths() {
  try {
    localStorage.setItem(COL_WIDTHS_STORAGE_KEY, JSON.stringify({ ...colWidths }));
  } catch {
    // Ignore storage write errors
  }
}

function loadSavedColWidths() {
  try {
    const raw = localStorage.getItem(COL_WIDTHS_STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') {
      let applied = false;
      for (const k of COL_ORDER) {
        const val = Number(parsed[k]);
        if (Number.isFinite(val) && val > 0) {
          colWidths[k] = Math.max(MIN_COL_WIDTHS[k], Math.min(900, Math.round(val)));
          applied = true;
        }
      }
      hasCustomColWidths = applied;
    }
  } catch {
    // Ignore storage read errors
  }
}

/**
 * Scale flexible columns proportionally to match `targetWidth` exactly while keeping
 * `selection` fixed and honoring every column's minimum width.
 */
function scaleColumnsToWidth(targetWidth: number, weights: Record<ColKey, number>) {
  if (targetWidth <= 0) return;
  colWidths.selection = MIN_COL_WIDTHS.selection;
  const budget = Math.max(0, targetWidth - colWidths.selection);
  const minSum = RESIZABLE_COL_KEYS.reduce((s, k) => s + MIN_COL_WIDTHS[k], 0);
  if (budget <= minSum) {
    for (const k of RESIZABLE_COL_KEYS) {
      colWidths[k] = MIN_COL_WIDTHS[k];
    }
    syncTableColumnWidths();
    nextTick(() => tableRef.value?.doLayout?.());
    return;
  }

  // Water-filling allocation: pin any column whose proportional share would fall below its min.
  const free = new Set<ColKey>(RESIZABLE_COL_KEYS);
  let remainingBudget = budget;
  let changed = true;
  while (changed && free.size > 0) {
    changed = false;
    const totalWeight = Array.from(free).reduce((s, k) => s + Math.max(1, weights[k]), 0);
    for (const k of Array.from(free)) {
      const share = (remainingBudget * Math.max(1, weights[k])) / totalWeight;
      if (share < MIN_COL_WIDTHS[k]) {
        colWidths[k] = MIN_COL_WIDTHS[k];
        remainingBudget -= MIN_COL_WIDTHS[k];
        free.delete(k);
        changed = true;
      }
    }
  }

  const freeList = Array.from(free);
  if (freeList.length > 0) {
    const totalWeight = freeList.reduce((s, k) => s + Math.max(1, weights[k]), 0);
    let allocated = 0;
    const remainders: Array<{ key: ColKey; frac: number }> = [];
    for (const k of freeList) {
      const exact = (remainingBudget * Math.max(1, weights[k])) / totalWeight;
      const floored = Math.max(MIN_COL_WIDTHS[k], Math.floor(exact));
      colWidths[k] = floored;
      allocated += floored;
      remainders.push({ key: k, frac: exact - floored });
    }
    let leftover = remainingBudget - allocated;
    remainders.sort((a, b) => b.frac - a.frac);
    let idx = 0;
    while (leftover > 0 && remainders.length > 0) {
      colWidths[remainders[idx % remainders.length].key] += 1;
      leftover -= 1;
      idx += 1;
    }
  }

  syncTableColumnWidths();
  nextTick(() => {
    tableRef.value?.doLayout?.();
  });
}

function fitColumnsToContainer() {
  const available = getTableTargetWidth();
  if (available <= 0) return;
  const weights = hasCustomColWidths ? { ...colWidths } : { ...BASE_COL_WIDTHS };
  scaleColumnsToWidth(available, weights);
}

/**
 * Clamp Element Plus's live drag guide line so users visually stay within the adjacent two columns.
 */
function installLiveDragBoundsClamp() {
  const tableEl = tableRef.value?.$el as HTMLElement | undefined;
  if (!tableEl) return;

  let activeBounds: { minLeft: number; maxLeft: number; proxy: HTMLElement } | null = null;

  const onDocMouseMove = () => {
    if (!activeBounds) return;
    const currentLeft = Number.parseFloat(activeBounds.proxy.style.left);
    if (!Number.isFinite(currentLeft)) return;
    const clamped = Math.max(activeBounds.minLeft, Math.min(activeBounds.maxLeft, currentLeft));
    if (clamped !== currentLeft) {
      activeBounds.proxy.style.left = `${clamped}px`;
    }
  };

  const onDocMouseUp = () => {
    activeBounds = null;
    document.removeEventListener('mousemove', onDocMouseMove);
    document.removeEventListener('mouseup', onDocMouseUp);
  };

  const onTableMouseDown = (ev: MouseEvent) => {
    if (document.body.style.cursor !== 'col-resize') return;
    const target = ev.target as HTMLElement | null;
    const th = target?.closest?.('th') as HTMLTableCellElement | null;
    const tr = th?.parentElement;
    if (!th || !tr) return;

    const colIdx = Array.from(tr.children).indexOf(th);
    if (colIdx < 0 || colIdx >= COL_ORDER.length - 1) return;
    const key = COL_ORDER[colIdx];
    const nextKey = COL_ORDER[colIdx + 1];
    if (key === 'selection') return;

    const proxy = tableEl.querySelector('.el-table__column-resize-proxy') as HTMLElement | null;
    if (!proxy) return;

    const tableRect = tableEl.getBoundingClientRect();
    const thRect = th.getBoundingClientRect();
    const minLeft = Math.round(thRect.left - tableRect.left + MIN_COL_WIDTHS[key]);
    const maxLeft = Math.round(thRect.right - tableRect.left + Math.max(0, colWidths[nextKey] - MIN_COL_WIDTHS[nextKey]));
    activeBounds = { minLeft, maxLeft, proxy };

    document.addEventListener('mousemove', onDocMouseMove);
    document.addEventListener('mouseup', onDocMouseUp);
  };

  tableEl.addEventListener('mousedown', onTableMouseDown);
  dragCleanup = () => {
    onDocMouseUp();
    tableEl.removeEventListener('mousedown', onTableMouseDown);
  };
}

/**
 * Adjust ONLY the dragged column and its immediate right neighbor so their combined width
 * stays strictly constant and all other columns keep their exact widths and positions.
 */
function handleColumnDragEnd(newWidth: number, oldWidth: number, column: any) {
  const key = (column?.columnKey || '') as ColKey;
  const idx = COL_ORDER.indexOf(key);
  if (idx < 0 || idx >= COL_ORDER.length - 1) {
    syncTableColumnWidths();
    nextTick(() => tableRef.value?.doLayout?.());
    return;
  }

  const nextKey = COL_ORDER[idx + 1];
  const leftOld = colWidths[key];
  const rightOld = colWidths[nextKey];
  const refOld = Number.isFinite(oldWidth) && oldWidth > 0 ? oldWidth : leftOld;
  const rawDelta = Math.round(newWidth - refOld);

  const minDelta = -(leftOld - MIN_COL_WIDTHS[key]);
  const maxDelta = rightOld - MIN_COL_WIDTHS[nextKey];
  const clampedDelta = Math.max(minDelta, Math.min(maxDelta, rawDelta));

  colWidths[key] = leftOld + clampedDelta;
  colWidths[nextKey] = rightOld - clampedDelta;

  hasCustomColWidths = true;
  saveColWidths();
  syncTableColumnWidths();
  nextTick(() => {
    tableRef.value?.doLayout?.();
  });
}
const showCategoryDialog = ref(false);
const editingTxn = ref<TransactionItem | null>(null);
const editingCategoryId = ref<number | null>(null);
const savingCategory = ref(false);

const showBulkCategoryDialog = ref(false);
const bulkCategoryId = ref<number | null>(null);

const showDetailDrawer = ref(false);
const activeDetailTxn = ref<TransactionItem | null>(null);
const activeDetailRelated = ref<RelatedTxnInfo[]>([]);

const detailMergedRecords = computed(() =>
  activeDetailRelated.value.filter((r) => r.relation === 'duplicate_child')
);

const detailOtherRelations = computed(() =>
  activeDetailRelated.value.filter((r) => r.relation !== 'duplicate_child')
);

const SOURCE_LABELS: Record<string, string> = {
  alipay: '支付宝',
  wechat: '微信支付',
  boc_debit: '中国银行',
  icbc_credit: '工商银行信用卡',
  icbc_debit: '工商银行',
  cmb: '招商银行',
  manual: '手动补录',
};

function formatSourceName(src?: string | null): string {
  if (!src) return '-';
  if (SOURCE_LABELS[src]) return SOURCE_LABELS[src];
  if (src.startsWith('profile:')) return src.slice(8);
  return src;
}

function getSecondaryAccountLabel(row: TransactionItem): string {
  const firstMerged = row.merged_duplicates?.[0];
  if (firstMerged) {
    const bank = firstMerged.account_bank || formatSourceName(firstMerged.source);
    return firstMerged.account_tail ? `${bank}(${firstMerged.account_tail})` : bank;
  }
  if (row.fund_account_bank) {
    return row.fund_account_tail
      ? `${row.fund_account_bank}(${row.fund_account_tail})`
      : row.fund_account_bank;
  }
  return '';
}

function formatMergedTooltip(row: TransactionItem): string {
  const parts = [formatSourceName(row.source)];
  for (const d of row.merged_duplicates || []) {
    const acc = d.account_bank ? `${d.account_bank}${d.account_tail ? `(${d.account_tail})` : ''}` : formatSourceName(d.source);
    parts.push(acc);
  }
  return `同笔消费已合并 ${parts.length} 个账单来源：${parts.join(' + ')}（点击查看详情）`;
}

function formatRelationLabel(rel: RelatedTxnInfo['relation']): string {
  switch (rel) {
    case 'duplicate_primary':
      return '合并主记录';
    case 'refund_parent':
      return '原消费';
    case 'refund_child':
      return '对应退款';
    case 'transfer_peer':
      return '转账对端';
    default:
      return rel;
  }
}

const ruleProposalModalRef = ref<any>(null);

// Manual FX Dialog State
const showManualRateDialog = ref(false);
const savingFx = ref(false);
const retryingFx = ref(false);
const aiClassifying = ref(false);
const manualFxForm = reactive({
  currency: 'USD',
  rate: undefined as number | undefined,
  effective_date: '',
  note: '',
  apply_to_all_missing: false,
});

function openManualRateDialog(row?: TransactionItem | null) {
  const target = row || activeDetailTxn.value;
  if (!target) return;
  manualFxForm.currency = target.currency || 'USD';
  manualFxForm.rate = target.fx_rate ? Number(target.fx_rate) : undefined;
  manualFxForm.effective_date = (target.txn_time || '').slice(0, 10) || dayjs().format('YYYY-MM-DD');
  manualFxForm.note = '';
  manualFxForm.apply_to_all_missing = false;
  showManualRateDialog.value = true;
}

async function handleSaveManualFx() {
  if (!manualFxForm.rate || manualFxForm.rate <= 0) {
    ElMessage.warning('请输入大于 0 的有效汇率');
    return;
  }
  if (!manualFxForm.effective_date) {
    ElMessage.warning('请选择汇率生效日期');
    return;
  }
  savingFx.value = true;
  try {
    const res = await saveFxRate({
      currency: manualFxForm.currency,
      rate: manualFxForm.rate,
      effective_date: manualFxForm.effective_date,
      note: manualFxForm.note || undefined,
      apply_to_all_missing: manualFxForm.apply_to_all_missing,
    });
    ElMessage.success(`汇率已保存，已更新 ${res.updated_transactions} 笔交易折算金额`);
    showManualRateDialog.value = false;
    await txnStore.fetchList();

    if (activeDetailTxn.value && activeDetailTxn.value.currency === manualFxForm.currency) {
      const refreshed = txnStore.items.find((i) => i.id === activeDetailTxn.value?.id);
      if (refreshed) {
        activeDetailTxn.value = refreshed;
      } else {
        try {
          const detail = await getTransaction(activeDetailTxn.value.id);
          activeDetailTxn.value = detail.transaction;
          activeDetailRelated.value = detail.related || [];
        } catch {
          // ignore refresh failure
        }
      }
    }
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err?.message || '保存汇率失败');
    }
  } finally {
    savingFx.value = false;
  }
}

async function handleRetryFx(row: TransactionItem) {
  retryingFx.value = true;
  try {
    const updated = await retryTxnFx(row.id);
    if (updated.amount_cny !== null && updated.amount_cny !== undefined) {
      ElMessage.success('汇率获取成功，已更新折算金额');
    } else {
      ElMessage.warning('未能获取在线汇率，可点击补录汇率手动录入');
    }
    activeDetailTxn.value = updated;
    await txnStore.fetchList();
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err?.message || '重试获取汇率失败');
    }
  } finally {
    retryingFx.value = false;
  }
}

function handleFilterChange() {
  txnStore.pagination.page = 1;
  txnStore.fetchList();
}

function handleReset() {
  showExcluded.value = false;
  mergeDuplicates.value = true;
  filterUnclassified.value = false;
  filterMissingFx.value = false;
  if (Object.keys(route.query || {}).length > 0) {
    router.replace('/transactions');
  }
  txnStore.resetFilters();
}

function handleMergeDuplicatesToggle(val: any) {
  txnStore.filters.mergeDuplicates = Boolean(val);
  handleFilterChange();
}

function handleUnclassifiedToggle(val: any) {
  txnStore.filters.unclassified = Boolean(val);
  handleFilterChange();
}

function handleExcludedToggle(val: any) {
  txnStore.filters.excluded = val ? null : 0;
  handleFilterChange();
}

function handleMissingFxToggle(val: any) {
  txnStore.filters.fx = val ? 'missing' : null;
  handleFilterChange();
}

function handlePaginationChange() {
  txnStore.fetchList();
}

function handleSelectionChange(selection: TransactionItem[]) {
  txnStore.selectedIds = selection.map((i) => i.id);
}

function clearTableSelection() {
  tableRef.value?.clearSelection?.();
  txnStore.selectedIds = [];
}

function handleExport() {
  const [from, to] = txnStore.filters.dateRange || ['', ''];
  const url = getExportUrl({
    from: from || undefined,
    to: to || undefined,
    account_id: txnStore.filters.accountId || undefined,
    category_id: txnStore.filters.categoryId || undefined,
  });
  window.open(url, '_blank');
}

async function handleAiClassify() {
  aiClassifying.value = true;
  try {
    const payload =
      txnStore.selectedIds.length > 0
        ? { transaction_ids: txnStore.selectedIds, limit: 100, update_db: true }
        : { limit: 100, update_db: true };
    const res = await classifyWithLLM(payload);
    if (res.status === 'offline') {
      ElMessage.warning('请先在“系统设置 -> 大语言模型 (AI 辅助)”中启用并保存 AI 配置');
      return;
    }
    if (res.status === 'disabled') {
      ElMessage.warning(res.reason || '已在设置中关闭「未知交易兜底智能分类」开关');
      return;
    }
    if (!res.success) {
      ElMessage.error(res.reason || 'AI 分类请求失败');
      return;
    }
    if (res.classified_count > 0) {
      ElMessage.success(`AI 已完成 ${res.classified_count} 笔未归类交易的智能分类`);
      await Promise.all([txnStore.fetchList(), appStore.fetchMeta()]);
    } else if (res.results.length === 0) {
      ElMessage.info('当前暂无未分类交易需要 AI 处理');
    } else {
      ElMessage.info('AI 已评估待分类交易，暂无高置信度可自动落库的结果');
    }
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err?.message || 'AI 分类失败');
    }
  } finally {
    aiClassifying.value = false;
  }
}

function openInlineCategoryEdit(row: TransactionItem) {
  editingTxn.value = row;
  editingCategoryId.value = row.category_id || null;
  showCategoryDialog.value = true;
}

async function saveCategoryEdit() {
  if (!editingTxn.value) return;
  savingCategory.value = true;
  const targetCategoryId = editingCategoryId.value ?? null;
  try {
    const res = await patchTransaction(editingTxn.value.id, {
      category_id: targetCategoryId,
    });
    showCategoryDialog.value = false;
    ElMessage.success(targetCategoryId === null ? '已清除分类' : '分类已更新');
    await txnStore.fetchList();

    // Check if backend returned rule proposal suggestion!
    if (res.rule_proposal) {
      ruleProposalModalRef.value?.open(res.rule_proposal);
    }
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '更新分类失败');
    }
  } finally {
    savingCategory.value = false;
  }
}

function handleProposalApplied() {
  txnStore.fetchList();
}

function openBulkCategoryModal() {
  bulkCategoryId.value = null;
  showBulkCategoryDialog.value = true;
}

async function saveBulkCategory() {
  if (bulkCategoryId.value == null) {
    ElMessage.warning('请先选择目标分类');
    return;
  }
  savingCategory.value = true;
  try {
    await txnStore.bulkSetCategory(bulkCategoryId.value);
    tableRef.value?.clearSelection?.();
    showBulkCategoryDialog.value = false;
    ElMessage.success('批量修改分类成功');
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '批量修改失败');
    }
  } finally {
    savingCategory.value = false;
  }
}

async function handleBulkExclude(excluded: number) {
  try {
    await txnStore.bulkSetExcluded(excluded);
    tableRef.value?.clearSelection?.();
    ElMessage.success(excluded ? '已批量排除选中记录' : '已恢复选中记录');
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '批量操作失败');
    }
  }
}

async function viewDetail(row: TransactionItem) {
  activeDetailTxn.value = row;
  activeDetailRelated.value = (row.merged_duplicates || []).map((d) => ({
    relation: 'duplicate_child' as const,
    txn: d as Record<string, any>,
  }));
  showDetailDrawer.value = true;
  try {
    const detail = await getTransaction(row.id);
    if (activeDetailTxn.value?.id === row.id) {
      activeDetailTxn.value = detail.transaction;
      activeDetailRelated.value = detail.related || [];
    }
  } catch {
    // Keep row fallback data
  }
}

async function jumpToRelatedTxn(id: number) {
  try {
    const detail = await getTransaction(id);
    activeDetailTxn.value = detail.transaction;
    activeDetailRelated.value = detail.related || [];
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err?.message || '加载关联交易详情失败');
    }
  }
}

async function deleteSingleTxn(id: number) {
  try {
    await ElMessageBox.confirm('确定要删除此笔交易吗？', '删除确认', {
      type: 'warning',
    });
    await txnStore.removeTransaction(id);
    showDetailDrawer.value = false;
    ElMessage.success('删除成功');
  } catch {
    // Cancelled
  }
}

let lastFittedWidth = 0;
let resizeRafId: number | null = null;

function syncCheckboxesFromStore() {
  showExcluded.value = txnStore.filters.excluded === null;
  mergeDuplicates.value = txnStore.filters.mergeDuplicates;
  filterUnclassified.value = txnStore.filters.unclassified;
  filterMissingFx.value = txnStore.filters.fx === 'missing';
}

function applyRouteQuery(query: Record<string, any>) {
  const hasQuery = Object.keys(query || {}).length > 0;
  if (hasQuery) {
    // Clear stale filters when drilling down from Dashboard / Global Search
    txnStore.filters.dateRange = null;
    txnStore.filters.accountId = null;
    txnStore.filters.fundAccountId = null;
    txnStore.filters.source = null;
    txnStore.filters.categoryId = null;
    txnStore.filters.direction = null;
    txnStore.filters.status = null;
    txnStore.filters.q = '';
    txnStore.filters.excluded = 0;
    txnStore.filters.mergeDuplicates = true;
    txnStore.filters.unclassified = false;
    txnStore.filters.currency = null;
    txnStore.filters.fx = null;
    txnStore.pagination.page = 1;

    if (query.direction) {
      txnStore.filters.direction = query.direction as any;
    }
    if (query.q) {
      txnStore.filters.q = String(query.q);
    }
    if (query.from && query.to) {
      txnStore.filters.dateRange = [String(query.from), String(query.to)];
    }
    if (query.currency) {
      txnStore.filters.currency = String(query.currency);
    }
    if (query.account_id) {
      const aid = Number(query.account_id);
      if (Number.isFinite(aid)) txnStore.filters.accountId = aid;
    }
    if (query.category_id) {
      const cid = Number(query.category_id);
      if (Number.isFinite(cid)) txnStore.filters.categoryId = cid;
    }
    if (query.unclassified === '1' || query.unclassified === 'true') {
      txnStore.filters.unclassified = true;
    }
    if (query.fx === 'missing') {
      txnStore.filters.fx = 'missing';
    }
  }
  syncCheckboxesFromStore();
}

watch(
  () => route.query,
  (newQuery) => {
    if (Object.keys(newQuery || {}).length > 0) {
      applyRouteQuery(newQuery as Record<string, any>);
      txnStore.fetchList();
    }
  },
  { deep: true }
);

onMounted(() => {
  loadSavedColWidths();
  nextTick(() => {
    const w = getTableTargetWidth();
    if (w > 0) {
      lastFittedWidth = w;
      fitColumnsToContainer();
    }
    installLiveDragBoundsClamp();
  });
  if (gridCardRef.value && typeof ResizeObserver !== 'undefined') {
    gridResizeObserver = new ResizeObserver(() => {
      if (resizeRafId !== null) cancelAnimationFrame(resizeRafId);
      resizeRafId = requestAnimationFrame(() => {
        resizeRafId = null;
        const w = getTableTargetWidth();
        if (w > 0 && w !== lastFittedWidth) {
          lastFittedWidth = w;
          fitColumnsToContainer();
        }
      });
    });
    gridResizeObserver.observe(gridCardRef.value);
  }

  // Sync query params and checkbox states from store
  applyRouteQuery(route.query as Record<string, any>);
  txnStore.fetchList();
});

onBeforeUnmount(() => {
  if (resizeRafId !== null) {
    cancelAnimationFrame(resizeRafId);
    resizeRafId = null;
  }
  if (gridResizeObserver) {
    gridResizeObserver.disconnect();
    gridResizeObserver = null;
  }
  if (dragCleanup) {
    dragCleanup();
    dragCleanup = null;
  }
});
</script>

<style scoped>
.fx-detail-block {
  margin-top: 12px;
  padding: 12px;
  background: var(--surface-2, rgba(0, 0, 0, 0.02));
  border: 1px dashed var(--border);
  border-radius: var(--radius-md, 6px);
}

.fx-action-buttons {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}

.transactions-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
  position: relative;
  min-width: 0;
}

.filter-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px 20px;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}

.filter-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}

.grid-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}

.grid-card :deep(.el-table th.el-table__cell) {
  user-select: none;
}

.grid-card :deep(.el-table .cell) {
  padding: 0 8px;
}

.grid-card :deep(.el-table th.el-table__cell:not(:first-child):not(:last-child):hover) {
  border-right-color: var(--primary);
}

.grid-summary-line {
  font-size: 13px;
  color: var(--text-2);
}

.cell-category {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  transition: background 0.15s;
}

.cell-category:hover {
  background: var(--surface-hover);
}

.cat-label {
  color: var(--text);
  font-weight: 500;
  font-size: 13px;
}

.cat-empty {
  color: var(--text-3);
  font-style: italic;
  font-size: 12.5px;
}

.cat-edit-icon {
  font-size: 12px;
  color: var(--text-3);
  opacity: 0.6;
}

.cell-category:hover .cat-edit-icon {
  opacity: 1;
  color: var(--primary);
}

.pagination-bar {
  display: flex;
  justify-content: flex-end;
  padding-top: 8px;
}

/* Floating Batch Action Bar */
.floating-batch-bar {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  background: #1B1F26;
  color: #fff;
  padding: 10px 20px;
  border-radius: 24px;
  box-shadow: var(--shadow-lg);
  display: flex;
  align-items: center;
  gap: 18px;
  z-index: 100;
  font-size: 13px;
}

.batch-info strong {
  color: #5B8DEF;
}

.batch-actions {
  display: flex;
  gap: 8px;
}

/* Detail Drawer */
.detail-drawer-content {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.detail-amount-box {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 16px;
  text-align: center;
}

.detail-amount-box .label {
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 4px;
}

.detail-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 13px;
}

.detail-list .row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.detail-list .label {
  color: var(--text-3);
  width: 80px;
}

.detail-list .val {
  color: var(--text);
  font-weight: 500;
  text-align: right;
  word-break: break-all;
}

.monospace {
  font-family: monospace;
}

.drawer-actions {
  margin-top: auto;
  display: flex;
  justify-content: flex-end;
}

.acc-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.3;
}

.acc-merged-chip {
  font-size: 11px;
  color: var(--primary);
  opacity: 0.9;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.source-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  line-height: 1.25;
}

.merged-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  color: var(--success, #16a34a);
  background: var(--success-soft, rgba(22, 163, 74, 0.12));
  padding: 1px 6px;
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  white-space: nowrap;
}

.merged-badge:hover {
  filter: brightness(0.95);
}

.dup-copy-badge {
  display: inline-block;
  font-size: 11px;
  color: var(--warning, #d97706);
  background: var(--warning-soft, rgba(217, 119, 6, 0.12));
  padding: 1px 6px;
  border-radius: var(--radius-sm, 4px);
  white-space: nowrap;
}

.detail-merged-section {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.detail-sub-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.detail-sub-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-2);
}

.merged-record-card {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 6px);
  background: var(--surface-alt);
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12.5px;
}

.merged-card-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.merged-card-source {
  font-weight: 600;
  color: var(--text);
}

.merged-card-amount {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}

.merged-card-meta {
  display: flex;
  justify-content: space-between;
  color: var(--text-3);
  font-size: 11.5px;
}

.merged-card-desc {
  color: var(--text-2);
  font-size: 12px;
  word-break: break-all;
}

:deep(.table-density-compact .el-table__cell) {
  padding: 4px 0;
}

:deep(.table-density-compact .cell) {
  line-height: 20px;
}
</style>
