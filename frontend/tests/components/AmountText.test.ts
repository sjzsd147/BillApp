import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import AmountText from '@/components/AmountText.vue';
import CommonAmountText from '@/components/common/AmountText.vue';

describe('AmountText Prop Aliasing & Contract Robustness', () => {
  it('correctly renders foreign currency when using amountCents alias', () => {
    const wrapper = mount(AmountText, {
      props: {
        amountCents: 4200,
        currency: 'USD',
        amountCny: 30245,
      },
    });

    expect(wrapper.text()).toContain('US$42.00');
    expect(wrapper.text()).toContain('≈ ¥302.45');
    expect(wrapper.find('.amount-text').text()).toBe('US$42.00');
    expect(wrapper.find('.fx-cny-text').text()).toBe('≈ ¥302.45');
  });

  it('correctly renders foreign currency when using cents alias', () => {
    const wrapper = mount(AmountText, {
      props: {
        cents: 4200,
        currency: 'USD',
        amountCny: 30245,
      },
    });

    expect(wrapper.text()).toContain('US$42.00');
    expect(wrapper.text()).toContain('≈ ¥302.45');
    expect(wrapper.find('.amount-text').text()).toBe('US$42.00');
    expect(wrapper.find('.fx-cny-text').text()).toBe('≈ ¥302.45');
  });

  it('correctly renders foreign currency when using value prop', () => {
    const wrapper = mount(AmountText, {
      props: {
        value: 4200,
        currency: 'USD',
        amountCny: 30245,
      },
    });

    expect(wrapper.text()).toContain('US$42.00');
    expect(wrapper.text()).toContain('≈ ¥302.45');
    expect(wrapper.find('.amount-text').text()).toBe('US$42.00');
    expect(wrapper.find('.fx-cny-text').text()).toBe('≈ ¥302.45');
  });

  it('respects prop precedence: value > amountCents > cents', () => {
    // value wins over amountCents
    const wrapper1 = mount(AmountText, {
      props: {
        value: 1000,
        amountCents: 2000,
        currency: 'CNY',
      },
    });
    expect(wrapper1.text()).toBe('¥10.00');

    // amountCents wins over cents
    const wrapper2 = mount(AmountText, {
      props: {
        amountCents: 3000,
        cents: 4000,
        currency: 'CNY',
      },
    });
    expect(wrapper2.text()).toBe('¥30.00');
  });

  it('defaults to 0 when no amount prop is provided', () => {
    const wrapper = mount(AmountText);
    expect(wrapper.text()).toBe('¥0.00');
  });

  it('verifies domestic currency (CNY) does not render FX elements', () => {
    const wrapper = mount(AmountText, {
      props: {
        amountCents: 4200,
        currency: 'CNY',
        amountCny: 4200,
      },
    });

    expect(wrapper.text()).toBe('¥42.00');
    expect(wrapper.find('.amount-container').exists()).toBe(false);
    expect(wrapper.find('.fx-cny-text').exists()).toBe(false);
  });

  it('verifies expense direction uses negative sign and dir-expense class', () => {
    const wrapper = mount(AmountText, {
      props: {
        amountCents: 4200,
        currency: 'CNY',
        direction: 'expense',
      },
    });

    expect(wrapper.text()).toBe('-¥42.00');
    expect(wrapper.classes()).toContain('dir-expense');
    expect(wrapper.classes()).not.toContain('dir-income');
  });

  it('verifies missing FX (amountCny: null) renders warning tag and never ≈ ¥0.00', () => {
    const wrapper = mount(AmountText, {
      props: {
        amountCents: 4200,
        currency: 'USD',
        amountCny: null,
      },
    });

    expect(wrapper.text()).toContain('US$42.00');
    expect(wrapper.text()).toContain('汇率待获取');
    expect(wrapper.text()).not.toContain('≈ ¥0.00');
    expect(wrapper.find('.fx-pending-tag').exists()).toBe(true);
  });

  it('verifies common/AmountText re-export works identically', () => {
    const wrapper = mount(CommonAmountText, {
      props: {
        amountCents: 5000,
        currency: 'USD',
        amountCny: 36000,
      },
    });

    expect(wrapper.text()).toContain('US$50.00');
    expect(wrapper.text()).toContain('≈ ¥360.00');
  });
});
