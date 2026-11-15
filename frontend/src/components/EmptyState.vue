<template>
  <div class="empty-state">
    <div class="empty-icon">
      <slot name="icon">
        <el-icon :size="48"><DocumentRemove /></el-icon>
      </slot>
    </div>
    <div class="empty-title">{{ title }}</div>
    <div v-if="description" class="empty-desc">{{ description }}</div>
    <div v-if="$slots.default" class="empty-actions">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import { DocumentRemove } from '@element-plus/icons-vue';

withDefaults(
  defineProps<{
    title?: string;
    description?: string;
  }>(),
  {
    title: '暂无数据',
    description: '',
  }
);
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  text-align: center;
  color: var(--text-3);
}

.empty-icon {
  margin-bottom: 16px;
  color: var(--border-strong);
}

.empty-title {
  font-size: 15px;
  font-weight: 500;
  color: var(--text-2);
  margin-bottom: 8px;
}

.empty-desc {
  font-size: 13px;
  color: var(--text-3);
  max-width: 360px;
  line-height: 1.5;
  margin-bottom: 16px;
}

.empty-actions {
  display: flex;
  gap: 12px;
}
</style>
