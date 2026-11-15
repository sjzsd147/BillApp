import { describe, it, expect, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import AmountText from '@/components/AmountText.vue';
import { getFxRates, refreshFx, saveFxRate, retryTxnFx } from '@/api/fx';
import apiClient from '@/api/client';

describe('AmountText FX & Dual-Currency Display (Milestone 6)', () => {
  it('renders standard CNY amount normally without dual-currency', () => {
    const wrapper = mount(AmountText, {
      props: {
        value: 10000,
        currency: 'CNY',
        direction: 'expense',
      },
    });
    expect(wrapper.text()).toBe('-¥100.00');
    expect(wrapper.find('.fx-cny-text').exists()).toBe(false);
    expect(wrapper.find('.fx-pending-tag').exists()).toBe(false);
  });

  it('renders foreign currency with original amount and converted CNY', () => {
    // US$42.00 (4200 cents), converted amount_cny: 30245 cents
    const wrapper = mount(AmountText, {
      props: {
        value: 4200,
        currency: 'USD',
        amountCny: 30245,
        fxRate: 7.2013,
        fxRateAt: '2026-10-15',
        fxRateSource: 'online',
        direction: 'expense',
      },
    });

    // Original amount formatted with US$ symbol
    expect(wrapper.text()).toContain('-US$42.00');
    // Converted CNY text strictly from backend amountCny
    expect(wrapper.text()).toContain('≈ ¥302.45');
    expect(wrapper.find('.fx-cny-text').exists()).toBe(true);
    expect(wrapper.find('.fx-pending-tag').exists()).toBe(false);
  });

  it('renders foreign currency with missing FX as 汇率待获取 and never ≈ ¥0.00', () => {
    const wrapper = mount(AmountText, {
      props: {
        value: 2000,
        currency: 'USD',
        amountCny: null,
        fxRateSource: 'none',
        direction: 'expense',
      },
    });

    expect(wrapper.text()).toContain('-US$42.00'.slice(0, 4)); // -US$
    expect(wrapper.text()).toContain('汇率待获取');
    expect(wrapper.text()).not.toContain('≈ ¥0.00');
    expect(wrapper.find('.fx-pending-tag').exists()).toBe(true);
  });

  it('emits clickPendingFx when clicking on 汇率待获取', async () => {
    const wrapper = mount(AmountText, {
      props: {
        value: 1500,
        currency: 'EUR',
        amountCny: null,
        fxRateSource: 'none',
      },
    });

    const tag = wrapper.find('.fx-pending-tag');
    expect(tag.exists()).toBe(true);
    await tag.trigger('click');
    expect(wrapper.emitted('clickPendingFx')).toBeTruthy();
  });
});

describe('FX API Client methods', () => {
  it('getFxRates calls /fx/rates', async () => {
    const mockData = {
      rates: [],
      currencies: ['CNY', 'USD'],
      pending_count: 2,
      pending_by_currency: [{ currency: 'USD', count: 2, amount_cents: 6200 }],
    };
    vi.spyOn(apiClient, 'get').mockResolvedValueOnce(mockData as any);

    const res = await getFxRates();
    expect(apiClient.get).toHaveBeenCalledWith('/fx/rates');
    expect(res.pending_count).toBe(2);
    expect(res.currencies).toContain('USD');
  });

  it('refreshFx calls POST /fx/refresh', async () => {
    const mockData = { total_missing: 3, refreshed_count: 2 };
    vi.spyOn(apiClient, 'post').mockResolvedValueOnce(mockData as any);

    const res = await refreshFx();
    expect(apiClient.post).toHaveBeenCalledWith('/fx/refresh');
    expect(res.refreshed_count).toBe(2);
  });

  it('saveFxRate calls POST /fx/rates with payload', async () => {
    const mockData = {
      success: true,
      rate: {
        id: 1,
        currency: 'USD',
        rate: 7.2013,
        effective_date: '2026-10-15',
        source: 'manual',
        created_at: '2026-10-15 10:00:00',
        updated_at: '2026-10-15 10:00:00',
      },
      updated_transactions: 3,
    };
    vi.spyOn(apiClient, 'post').mockResolvedValueOnce(mockData as any);

    const payload = {
      currency: 'USD',
      rate: 7.2013,
      effective_date: '2026-10-15',
      note: 'October rate',
    };
    const res = await saveFxRate(payload);
    expect(apiClient.post).toHaveBeenCalledWith('/fx/rates', payload);
    expect(res.success).toBe(true);
    expect(res.updated_transactions).toBe(3);
  });

  it('retryTxnFx calls POST /transactions/{id}/fx', async () => {
    const mockData = {
      id: 42,
      amount_cents: 1000,
      currency: 'USD',
      amount_cny: 7200,
      fx_rate: 7.2,
      fx_rate_source: 'online',
    };
    vi.spyOn(apiClient, 'post').mockResolvedValueOnce(mockData as any);

    const res = await retryTxnFx(42);
    expect(apiClient.post).toHaveBeenCalledWith('/transactions/42/fx');
    expect(res.amount_cny).toBe(7200);
  });
});
