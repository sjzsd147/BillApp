<template>
  <div v-if="isForeign" class="amount-container" :class="{ 'is-inline': fxDisplay === 'inline' }">
    <span
      class="amount-text tabular-nums"
      :class="[directionClass, { 'is-bold': bold }]"
      :style="{ fontSize: size }"
    >
      {{ formattedOriginalValue }}
    </span>

    <template v-if="amountCny !== null && amountCny !== undefined">
      <el-tooltip :content="fxTooltipText" placement="top" :disabled="!fxTooltipText">
        <span class="fx-cny-text tabular-nums">≈ {{ formattedCnyValue }}</span>
      </el-tooltip>
    </template>
    <template v-else>
      <span
        class="fx-pending-tag"
        title="点击补录汇率"
        @click.stop="emit('clickPendingFx')"
      >
        汇率待获取
      </span>
    </template>
  </div>

  <span
    v-else
    class="amount-text tabular-nums"
    :class="[directionClass, { 'is-bold': bold }]"
    :style="{ fontSize: size }"
  >
    {{ formattedValue }}
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { formatAmount, getCurrencySymbol } from '@/utils/money';
import type { DirectionType } from '@/types/api';

const props = withDefaults(
  defineProps<{
    value?: number | null; // Cents (fen) in original currency
    amountCents?: number | null; // Alias for value (backend model name)
    cents?: number | null; // Alias for value (design spec name)
    currency?: string;     // Currency code e.g. 'CNY', 'USD'
    amountCny?: number | null; // Converted CNY in cents (fen) strictly computed by backend
    fxRate?: number | null;
    fxRateAt?: string | null;
    fxRateSource?: 'online' | 'manual' | 'none' | string | null;
    fxDisplay?: 'block' | 'inline'; // default 'block'
    direction?: DirectionType;
    showSign?: boolean;
    symbol?: string;
    bold?: boolean;
    size?: string;
    privacy?: boolean;
  }>(),
  {
    value: undefined,
    amountCents: undefined,
    cents: undefined,
    currency: 'CNY',
    amountCny: undefined,
    fxRate: undefined,
    fxRateAt: undefined,
    fxRateSource: undefined,
    fxDisplay: 'block',
    direction: undefined,
    showSign: false,
    symbol: undefined,
    bold: false,
    size: 'inherit',
    privacy: false,
  }
);

const emit = defineEmits<{
  (e: 'clickPendingFx'): void;
}>();

const effectiveCents = computed(() => {
  return props.value ?? props.amountCents ?? props.cents ?? 0;
});

const isForeign = computed(() => {
  return !!props.currency && props.currency.toUpperCase() !== 'CNY';
});

const effectiveSymbol = computed(() => {
  if (props.symbol) return props.symbol;
  return getCurrencySymbol(props.currency);
});

const formattedValue = computed(() => {
  if (props.privacy) {
    return `${effectiveSymbol.value}••••`;
  }
  return formatAmount(effectiveCents.value, {
    symbol: effectiveSymbol.value,
    showSign: props.showSign,
    direction: props.direction,
  });
});

const formattedOriginalValue = computed(() => {
  if (props.privacy) {
    return `${effectiveSymbol.value}••••`;
  }
  return formatAmount(effectiveCents.value, {
    symbol: effectiveSymbol.value,
    showSign: props.showSign,
    direction: props.direction,
  });
});

const formattedCnyValue = computed(() => {
  if (props.privacy) {
    return '¥••••';
  }
  if (props.amountCny === null || props.amountCny === undefined) {
    return '';
  }
  return formatAmount(props.amountCny, {
    symbol: '¥',
    showSign: false,
  });
});

const fxTooltipText = computed(() => {
  if (!props.fxRate) return '';
  const sourceLabel =
    props.fxRateSource === 'online'
      ? '联网'
      : props.fxRateSource === 'manual'
      ? '手动'
      : '';
  const parts = [`交易时汇率 ${props.fxRate}`];
  if (props.fxRateAt) parts.push(props.fxRateAt);
  if (sourceLabel) parts.push(sourceLabel);
  return parts.join(' · ');
});

const directionClass = computed(() => {
  if (!props.direction) return 'dir-default';
  return `dir-${props.direction}`;
});
</script>

<style scoped>
.amount-text {
  display: inline-block;
  white-space: nowrap;
}

.amount-container {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-end;
  vertical-align: middle;
}

.amount-container.is-inline {
  flex-direction: row;
  align-items: baseline;
  gap: 6px;
}

.fx-cny-text {
  font-size: 12px;
  color: var(--text-3, #9ca3af);
  line-height: 1.2;
  white-space: nowrap;
}

.fx-pending-tag {
  font-size: 11px;
  color: var(--warning, #d97706);
  background: rgba(217, 119, 6, 0.1);
  border: 1px solid rgba(217, 119, 6, 0.25);
  padding: 0 4px;
  border-radius: 3px;
  cursor: pointer;
  line-height: 1.3;
  white-space: nowrap;
  transition: all 0.2s ease;
}

.fx-pending-tag:hover {
  background: rgba(217, 119, 6, 0.2);
}

.is-bold {
  font-weight: 600;
}

/* Direction color styling adhering to D2 */
.dir-expense {
  color: var(--expense); /* Body text color #1B1F26, NOT RED! */
}

.dir-income {
  color: var(--income); /* #15803D */
}

.dir-refund {
  color: var(--refund); /* #1D4ED8 */
}

.dir-transfer {
  color: var(--transfer); /* #6B7280 */
}

.dir-neutral,
.dir-default {
  color: var(--text);
}
</style>
