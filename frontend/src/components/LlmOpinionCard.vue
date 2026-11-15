<template>
  <div v-if="opinion" class="llm-opinion-card">
    <div class="llm-header">
      <div class="llm-title">
        <span class="sparkle">✨</span>
        <span class="label">AI 建议分析</span>
        <span class="badge" :class="confidenceClass">
          可信度 {{ (opinion.confidence * 100).toFixed(0) }}%
        </span>
      </div>
      <div v-if="opinion.verdict" class="llm-verdict">
        判定: <strong>{{ opinion.verdict }}</strong>
      </div>
    </div>
    <div class="llm-reason">
      {{ opinion.reason }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  opinion?: {
    verdict?: string;
    reason: string;
    confidence: number;
  } | null;
}>();

const confidenceClass = computed(() => {
  if (!props.opinion) return '';
  if (props.opinion.confidence >= 0.85) return 'conf-high';
  if (props.opinion.confidence >= 0.7) return 'conf-mid';
  return 'conf-low';
});
</script>

<style scoped>
.llm-opinion-card {
  padding: 12px 14px;
  background: var(--primary-soft-2);
  border: 1px solid rgba(47, 107, 255, 0.2);
  border-radius: var(--radius-md);
  margin-top: 12px;
}

.llm-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.llm-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--primary);
}

.sparkle {
  font-size: 14px;
}

.badge {
  font-size: 11px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
}

.conf-high {
  background: var(--success-soft);
  color: var(--success);
}
.conf-mid {
  background: var(--primary-soft);
  color: var(--primary);
}
.conf-low {
  background: var(--warning-soft);
  color: var(--warning);
}

.llm-verdict {
  font-size: 12px;
  color: var(--text-2);
}

.llm-reason {
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--text-2);
}
</style>
