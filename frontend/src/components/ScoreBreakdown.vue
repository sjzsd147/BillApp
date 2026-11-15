<template>
  <div class="score-breakdown">
    <div class="score-header">
      <span class="score-title">匹配度计算</span>
      <span class="score-badge" :class="scoreClass">{{ scoreText }}</span>
    </div>

    <!-- Stacked progress bar -->
    <div class="breakdown-bar">
      <div
        class="bar-segment seg-amount"
        :style="{ width: `${(breakdown.amount / maxTotal) * 100}%` }"
        title="金额匹配"
      />
      <div
        class="bar-segment seg-tail"
        :style="{ width: `${(breakdown.tail / maxTotal) * 100}%` }"
        title="卡号末四位"
      />
      <div
        class="bar-segment seg-time"
        :style="{ width: `${(breakdown.time / maxTotal) * 100}%` }"
        title="交易时间"
      />
      <div
        class="bar-segment seg-channel"
        :style="{ width: `${(breakdown.channel / maxTotal) * 100}%` }"
        title="渠道一致"
      />
      <div
        class="bar-segment seg-name"
        :style="{ width: `${(breakdown.name / maxTotal) * 100}%` }"
        title="商户名称"
      />
    </div>

    <!-- Breakdown Legend -->
    <div class="breakdown-grid">
      <div class="item">
        <span class="dot dot-amount" />
        <span class="label">金额:</span>
        <span class="val">{{ breakdown.amount.toFixed(2) }}</span>
      </div>
      <div class="item">
        <span class="dot dot-tail" />
        <span class="label">卡尾:</span>
        <span class="val">{{ breakdown.tail.toFixed(2) }}</span>
      </div>
      <div class="item">
        <span class="dot dot-time" />
        <span class="label">时间:</span>
        <span class="val">{{ breakdown.time.toFixed(2) }}</span>
      </div>
      <div class="item">
        <span class="dot dot-channel" />
        <span class="label">渠道:</span>
        <span class="val">{{ breakdown.channel.toFixed(2) }}</span>
      </div>
      <div class="item">
        <span class="dot dot-name" />
        <span class="label">名称:</span>
        <span class="val">{{ breakdown.name.toFixed(2) }}</span>
      </div>
      <div v-if="breakdown.penalty > 0" class="item item-penalty">
        <span class="dot dot-penalty" />
        <span class="label">渠道冲突罚分:</span>
        <span class="val">-{{ breakdown.penalty.toFixed(2) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { BreakdownScore } from '@/types/api';

const props = defineProps<{
  score: number;
  breakdown: BreakdownScore;
}>();

const maxTotal = 1.0;

const scoreText = computed(() => {
  return `${(props.score * 100).toFixed(0)}% (${props.score.toFixed(2)})`;
});

const scoreClass = computed(() => {
  if (props.score >= 0.85) return 'score-high';
  if (props.score >= 0.7) return 'score-mid';
  return 'score-low';
});
</script>

<style scoped>
.score-breakdown {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.score-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.score-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-2);
}

.score-badge {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
}

.score-high {
  background: var(--success-soft);
  color: var(--success);
}
.score-mid {
  background: var(--primary-soft);
  color: var(--primary);
}
.score-low {
  background: var(--warning-soft);
  color: var(--warning);
}

.breakdown-bar {
  display: flex;
  height: 8px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--border-2);
}

.bar-segment {
  height: 100%;
}

.seg-amount, .dot-amount { background: #2F6BFF; }
.seg-tail, .dot-tail { background: #10B981; }
.seg-time, .dot-time { background: #F59E0B; }
.seg-channel, .dot-channel { background: #8B5CF6; }
.seg-name, .dot-name { background: #EC4899; }
.dot-penalty { background: #EF4444; }

.breakdown-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--text-2);
}

.item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.val {
  font-weight: 500;
  color: var(--text);
}

.item-penalty {
  color: var(--danger);
}
</style>
