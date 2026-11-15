import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import AmountText from '@/components/AmountText.vue';
import DirectionTag from '@/components/DirectionTag.vue';
import StatusTag from '@/components/StatusTag.vue';

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
