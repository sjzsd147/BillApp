import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import AmountText from '@/components/AmountText.vue';
import CategoryPicker from '@/components/CategoryPicker.vue';
import DirectionTag from '@/components/DirectionTag.vue';
import ScoreBreakdown from '@/components/ScoreBreakdown.vue';
import StatusTag from '@/components/StatusTag.vue';
import { useAppStore } from '@/stores/app';

describe('AmountText component', () => {
  it('renders standard amount with tabular-nums', () => {
    const wrapper = mount(AmountText, {
      props: {
        value: 123456,
      },
    });
    expect(wrapper.text()).toContain('¥1,234.56');
    expect(wrapper.classes()).toContain('tabular-nums');
  });

  it('renders expense with negative sign and dir-expense class', () => {
    const wrapper = mount(AmountText, {
      props: {
        value: 2747,
        direction: 'expense',
      },
    });
    expect(wrapper.text()).toBe('-¥27.47');
    expect(wrapper.classes()).toContain('dir-expense');
  });

  it('renders privacy mask when privacy is true', () => {
    const wrapper = mount(AmountText, {
      props: {
        value: 88888,
        privacy: true,
      },
    });
    expect(wrapper.text()).toBe('¥••••');
  });

  it('reacts to appStore.amountPrivacy and temporarily reveals on hover (S-11)', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useAppStore();
    store.applyGeneralSettings({ amount_privacy: true });

    const wrapper = mount(AmountText, {
      props: { value: 12345 },
      global: { plugins: [pinia] },
    });
    expect(wrapper.text()).toBe('¥••••');

    await wrapper.trigger('mouseenter');
    expect(wrapper.text()).toBe('¥123.45');

    await wrapper.trigger('mouseleave');
    expect(wrapper.text()).toBe('¥••••');

    // Reset global privacy state so subsequent isolated tests stay unaffected
    store.applyGeneralSettings({ amount_privacy: false });
  });
});

describe('ScoreBreakdown component', () => {
  it('displays negative channel conflict penalty accurately (B-06)', () => {
    const wrapper = mount(ScoreBreakdown, {
      props: {
        score: 0.6,
        breakdown: {
          amount: 0.3,
          tail: 0.25,
          time: 0.2,
          channel: 0.0,
          name: 0.1,
          penalty: -0.25,
        },
      },
    });
    expect(wrapper.find('.item-penalty').exists()).toBe(true);
    expect(wrapper.find('.item-penalty').text()).toContain('-0.25');
  });
});

describe('DirectionTag component', () => {
  it('renders expense tag', () => {
    const wrapper = mount(DirectionTag, {
      props: { direction: 'expense' },
    });
    expect(wrapper.text()).toContain('支出');
    expect(wrapper.classes()).toContain('tag-expense');
  });

  it('renders refund tag', () => {
    const wrapper = mount(DirectionTag, {
      props: { direction: 'refund' },
    });
    expect(wrapper.text()).toContain('退款');
    expect(wrapper.classes()).toContain('tag-refund');
  });
});

describe('StatusTag component', () => {
  it('renders auto status tag', () => {
    const wrapper = mount(StatusTag, {
      props: { status: 'auto' },
    });
    expect(wrapper.text()).toContain('自动');
    expect(wrapper.classes()).toContain('status-auto');
  });

  it('renders excluded tag with strikethrough', () => {
    const wrapper = mount(StatusTag, {
      props: { excluded: 1 },
    });
    expect(wrapper.text()).toContain('已排除');
    expect(wrapper.classes()).toContain('status-excluded');
  });
});

describe('CategoryPicker component (R-FH2)', () => {
  it('normalizes undefined clear emission to null so callers can clear category', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(CategoryPicker, {
      props: { modelValue: 42 },
      global: {
        plugins: [pinia],
        stubs: {
          'el-cascader': {
            name: 'el-cascader',
            props: ['modelValue', 'valueOnClear'],
            emits: ['update:modelValue'],
            template: '<button class="mock-clear" @click="$emit(\'update:modelValue\', undefined)">clear</button>',
          },
        },
      },
    });

    await wrapper.find('.mock-clear').trigger('click');
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([null]);
    expect(wrapper.emitted('change')?.[0]).toEqual([null]);
  });
});
