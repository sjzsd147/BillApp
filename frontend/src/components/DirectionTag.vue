<template>
  <span class="direction-tag" :class="directionClass">
    <span class="icon">{{ icon }}</span>
    <span class="label">{{ label }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { DirectionType } from '@/types/api';

const props = defineProps<{
  direction: DirectionType | string;
}>();

const label = computed(() => {
  switch (props.direction) {
    case 'expense':
      return '支出';
    case 'income':
      return '收入';
    case 'refund':
      return '退款';
    case 'transfer':
      return '转账';
    case 'neutral':
      return '中性';
    default:
      return props.direction;
  }
});

const icon = computed(() => {
  switch (props.direction) {
    case 'expense':
      return '-';
    case 'income':
      return '+';
    case 'refund':
      return '↩';
    case 'transfer':
      return '⇄';
    default:
      return '•';
  }
});

const directionClass = computed(() => `tag-${props.direction}`);
</script>

<style scoped>
.direction-tag {
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

.tag-expense {
  background-color: var(--expense-soft);
  color: var(--text-2);
}

.tag-income {
  background-color: var(--income-soft);
  color: var(--income);
}

.tag-refund {
  background-color: var(--refund-soft);
  color: var(--refund);
}

.tag-transfer {
  background-color: var(--transfer-soft);
  color: var(--transfer);
}

.tag-neutral {
  background-color: var(--neutral-soft);
  color: var(--text-3);
}
</style>
