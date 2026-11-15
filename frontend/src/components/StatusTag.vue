<template>
  <span class="status-tag" :class="statusClass">
    <span class="icon">{{ icon }}</span>
    <span class="label">{{ label }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  status?: string | null;
  excluded?: boolean | number;
}>();

const isExcluded = computed(() => Boolean(props.excluded));

const label = computed(() => {
  if (isExcluded.value) return '已排除';
  switch (props.status) {
    case 'auto':
      return '自动';
    case 'pending':
      return '待确认';
    case 'confirmed':
      return '已确认';
    case 'rejected':
      return '已拒绝';
    default:
      return props.status || '正常';
  }
});

const icon = computed(() => {
  if (isExcluded.value) return '⊘';
  switch (props.status) {
    case 'auto':
      return '●';
    case 'pending':
      return '◐';
    case 'confirmed':
      return '✓';
    case 'rejected':
      return '✕';
    default:
      return '•';
  }
});

const statusClass = computed(() => {
  if (isExcluded.value) return 'status-excluded';
  return `status-${props.status || 'default'}`;
});
</script>

<style scoped>
.status-tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  font-weight: 500;
  line-height: 18px;
  white-space: nowrap;
}

.status-auto,
.status-confirmed {
  background-color: var(--success-soft);
  color: var(--success);
}

.status-pending {
  background-color: var(--warning-soft);
  color: var(--warning);
}

.status-rejected {
  background-color: var(--transfer-soft);
  color: var(--transfer);
}

.status-excluded {
  background-color: #f3f4f6;
  color: #9ca3af;
  text-decoration: line-through;
}

.status-default {
  background-color: var(--surface-alt);
  color: var(--text-2);
}
</style>
