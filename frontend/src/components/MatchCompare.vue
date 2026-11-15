<template>
  <div class="match-compare">
    <div class="cards-wrapper">
      <!-- Left Record -->
      <div class="card-side left-side">
        <div class="side-header">
          <span class="source-tag">{{ item.left.source_label || item.left.source }}</span>
          <span class="account-name">{{ item.left.account_name || '主账户' }}</span>
        </div>
        <div class="field-row amount-row">
          <div class="field-label">金额</div>
          <div class="field-value">
            <AmountText :value="item.left.amount_cents" bold size="22px" />
          </div>
        </div>
        <div class="field-row">
          <div class="field-label">交易时间</div>
          <div class="field-value">{{ item.left.txn_time }}</div>
        </div>
        <div class="field-row" :class="{ 'is-diff': isDiff('counterparty') }">
          <div class="field-label">对方/商户</div>
          <div class="field-value">{{ item.left.counterparty || '-' }}</div>
        </div>
        <div class="field-row" :class="{ 'is-diff': isDiff('description') }">
          <div class="field-label">交易说明</div>
          <div class="field-value">{{ item.left.description || '-' }}</div>
        </div>
        <div v-if="item.left.channel" class="field-row">
          <div class="field-label">支付渠道</div>
          <div class="field-value">{{ item.left.channel }}</div>
        </div>
      </div>

      <!-- Center Divider with Match Icon -->
      <div class="compare-divider">
        <div class="divider-line" />
        <div class="compare-badge">VS</div>
        <div class="divider-line" />
      </div>

      <!-- Right Record -->
      <div class="card-side right-side">
        <div class="side-header">
          <span class="source-tag">{{ item.right.source_label || item.right.source }}</span>
          <span class="account-name">{{ item.right.account_name || '关联账户' }}</span>
        </div>
        <div class="field-row amount-row">
          <div class="field-label">金额</div>
          <div class="field-value">
            <AmountText :value="item.right.amount_cents" bold size="22px" />
          </div>
        </div>
        <div class="field-row">
          <div class="field-label">交易时间</div>
          <div class="field-value">{{ item.right.txn_time }}</div>
        </div>
        <div class="field-row" :class="{ 'is-diff': isDiff('counterparty') }">
          <div class="field-label">对方/商户</div>
          <div class="field-value">{{ item.right.counterparty || '-' }}</div>
        </div>
        <div class="field-row" :class="{ 'is-diff': isDiff('description') }">
          <div class="field-label">交易说明</div>
          <div class="field-value">{{ item.right.description || '-' }}</div>
        </div>
        <div v-if="item.right.channel" class="field-row">
          <div class="field-label">支付渠道</div>
          <div class="field-value">{{ item.right.channel }}</div>
        </div>
      </div>
    </div>

    <!-- Match Analysis Section -->
    <div class="analysis-section">
      <ScoreBreakdown :score="item.score" :breakdown="item.breakdown" />
      <LlmOpinionCard v-if="item.llm" :opinion="item.llm" />
    </div>
  </div>
</template>

<script setup lang="ts">
import AmountText from './AmountText.vue';
import ScoreBreakdown from './ScoreBreakdown.vue';
import LlmOpinionCard from './LlmOpinionCard.vue';
import type { ReviewItemOut } from '@/types/api';

const props = defineProps<{
  item: ReviewItemOut;
}>();

function isDiff(field: 'counterparty' | 'description'): boolean {
  const left = (props.item.left as any)[field] || '';
  const right = (props.item.right as any)[field] || '';
  return left !== right && Boolean(left && right);
}
</script>

<style scoped>
.match-compare {
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 20px;
}

.cards-wrapper {
  display: grid;
  grid-template-columns: 1fr 40px 1fr;
  align-items: stretch;
  gap: 12px;
}

.card-side {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.side-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border-2);
}

.source-tag {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  background: var(--primary-soft);
  color: var(--primary);
  border-radius: var(--radius-sm);
}

.account-name {
  font-size: 12px;
  color: var(--text-2);
}

.field-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
}

.field-row.is-diff {
  background: #FFFBEB; /* Pale yellow diff highlight per design doc */
}

.field-label {
  color: var(--text-3);
  font-size: 12px;
}

.field-value {
  color: var(--text);
  font-weight: 500;
  text-align: right;
  word-break: break-all;
}

.amount-row .field-value {
  color: var(--text);
}

.compare-divider {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.divider-line {
  flex: 1;
  width: 1px;
  background: var(--border-2);
}

.compare-badge {
  font-size: 11px;
  font-weight: 700;
  padding: 4px;
  color: var(--text-3);
  border: 1px solid var(--border);
  border-radius: 50%;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--surface);
  margin: 8px 0;
}

.analysis-section {
  border-top: 1px solid var(--border-2);
  padding-top: 16px;
}
</style>
