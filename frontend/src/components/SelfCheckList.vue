<template>
  <div class="self-check-list">
    <div
      v-for="(check, idx) in items"
      :key="idx"
      class="check-item"
      :class="{ 'is-passed': check.passed, 'is-failed': !check.passed }"
    >
      <div class="check-status">
        <el-icon v-if="check.passed" class="icon-pass"><CircleCheck /></el-icon>
        <el-icon v-else class="icon-fail"><WarningFilled /></el-icon>
      </div>
      <div class="check-content">
        <div class="check-name">{{ check.name }}</div>
        <div v-if="check.description" class="check-desc">{{ check.description }}</div>
        <div v-if="!check.passed" class="check-diff">
          <span>预期: {{ check.expected }}</span>
          <span>实际: {{ check.actual }}</span>
          <span v-if="check.deviation !== undefined && check.deviation !== null">
            偏差: {{ check.deviation }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { CircleCheck, WarningFilled } from '@element-plus/icons-vue';
import type { SelfCheckItem } from '@/types/api';

defineProps<{
  items: SelfCheckItem[];
}>();
</script>

<style scoped>
.self-check-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.check-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--surface);
  font-size: 13px;
}

.check-item.is-passed {
  background: var(--success-soft);
  border-color: rgba(21, 128, 61, 0.2);
}

.check-item.is-failed {
  background: var(--danger-soft);
  border-color: rgba(198, 40, 40, 0.2);
}

.check-status {
  padding-top: 1px;
}

.icon-pass {
  color: var(--success);
  font-size: 16px;
}

.icon-fail {
  color: var(--danger);
  font-size: 16px;
}

.check-content {
  flex: 1;
}

.check-name {
  font-weight: 500;
  color: var(--text);
}

.check-desc {
  font-size: 12px;
  color: var(--text-2);
  margin-top: 2px;
}

.check-diff {
  display: flex;
  gap: 12px;
  font-size: 12px;
  color: var(--danger);
  margin-top: 4px;
  font-weight: 500;
}
</style>
