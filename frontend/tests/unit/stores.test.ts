import { describe, it, expect, beforeEach, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAppStore } from '@/stores/app';
import { useReviewStore } from '@/stores/review';
import { useImportStore } from '@/stores/import';
import { useTransactionsStore } from '@/stores/transactions';
import * as importApi from '@/api/import';
import * as txApi from '@/api/transactions';
import apiClient from '@/api/client';
import { testLLM, listLLMModels } from '@/api/llm';

describe('Pinia stores', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('initializes app store with defaults', () => {
    const app = useAppStore();
    expect(app.version).toBe(__APP_VERSION__);
    expect(app.pendingReviewCount).toBe(0);
    app.setPendingReviewCount(5);
    expect(app.pendingReviewCount).toBe(5);
    app.decrementReviewCount();
    expect(app.pendingReviewCount).toBe(4);
  });

  it('manages review store navigation and items', () => {
    const review = useReviewStore();
    expect(review.items).toEqual([]);
    expect(review.currentItem).toBeNull();

    // Mock candidates
    review.items = [
      {
        id: 1,
        kind: 'duplicate',
        score: 0.9,
        status: 'pending',
        breakdown: { amount: 0.3, tail: 0.25, time: 0.3, channel: 0.05, name: 0.0, penalty: 0 },
        left: { id: 10, source: 'alipay', source_label: '支付宝', txn_time: '2026-05-01 12:00:00', amount_cents: 2500, amount_display: '25.00' },
        right: { id: 11, source: 'icbc', source_label: '工行', txn_time: '2026-05-01 12:00:05', amount_cents: 2500, amount_display: '25.00' },
        created_at: '2026-05-01',
      },
      {
        id: 2,
        kind: 'duplicate',
        score: 0.7,
        status: 'pending',
        breakdown: { amount: 0.3, tail: 0, time: 0.3, channel: 0.1, name: 0.0, penalty: 0 },
        left: { id: 20, source: 'wechat', source_label: '微信', txn_time: '2026-05-02 12:00:00', amount_cents: 1000, amount_display: '10.00' },
        right: { id: 21, source: 'ccb', source_label: '建行', txn_time: '2026-05-02 12:00:00', amount_cents: 1000, amount_display: '10.00' },
        created_at: '2026-05-02',
      },
    ];

    expect(review.highConfidenceItems.length).toBe(1);
    expect(review.currentItem?.id).toBe(1);

    review.next();
    expect(review.currentIndex).toBe(1);
    expect(review.currentItem?.id).toBe(2);

    review.prev();
    expect(review.currentIndex).toBe(0);
    expect(review.currentItem?.id).toBe(1);

    // Defer moves first item to end
    expect(review.deferCurrent()).toBe(true);
    expect(review.items.map((i) => i.id)).toEqual([2, 1]);

    // Defer on last item wraps back to index 0
    review.currentIndex = 1;
    expect(review.deferCurrent()).toBe(true);
    expect(review.currentIndex).toBe(0);

    // Defer on single-item queue returns false
    review.items = [review.items[0]];
    review.currentIndex = 0;
    expect(review.deferCurrent()).toBe(false);
  });

  it('auto-advances activePreview on commit + removeQueueItem (R-FM1) and persists profileId on retry (R-FL1)', async () => {
    const store = useImportStore();
    const f1 = new File(['a'], 'a.csv', { type: 'text/csv' });
    const f2 = new File(['b'], 'b.csv', { type: 'text/csv' });
    store.addFiles([f1, f2]);

    const previewSpy = vi
      .spyOn(importApi, 'previewImport')
      .mockResolvedValueOnce({
        token: 'tok-1',
        source: 'cmb',
        need_password: true,
        need_mapping: false,
      } as any)
      .mockResolvedValueOnce({
        token: 'tok-1-unlocked',
        source: 'cmb',
        need_password: false,
        need_mapping: false,
      } as any)
      .mockResolvedValueOnce({
        token: 'tok-2',
        source: 'alipay',
        need_password: false,
        need_mapping: false,
      } as any);

    const commitSpy = vi.spyOn(importApi, 'commitImport').mockResolvedValueOnce({
      batch_id: 1,
      imported_count: 3,
      duplicate_count: 0,
      auto_dedup_count: 0,
      pending_review_count: 0,
      auto_transfer_count: 0,
      refund_linked_count: 0,
      classified_count: 3,
    } as any);

    // First parse saves profileId even when password is required
    await store.parseFile(store.queue[0], undefined, 'prof_cmb');
    expect(store.queue[0].status).toBe('need_password');
    expect(store.queue[0].profileId).toBe('prof_cmb');

    // Retry with password omits profileId -> reuses persisted item.profileId
    await store.parseFile(store.queue[0], 'secret123');
    expect(previewSpy).toHaveBeenNthCalledWith(2, f1, 'secret123', 'prof_cmb', undefined);
    expect(store.queue[0].status).toBe('ready');

    // Parse second file so both are ready; select first as activePreview
    await store.parseFile(store.queue[1]);
    store.activePreview = store.queue[0].preview!;

    // Commit first file -> stays set until removeQueueItem advances to next ready file
    await store.commitActivePreview('tok-1-unlocked');
    expect(commitSpy).toHaveBeenCalledTimes(1);
    expect(store.activePreview?.token).toBe('tok-1-unlocked');

    store.removeQueueItem(store.queue[0].id);
    expect(store.activePreview?.token).toBe('tok-2');

    previewSpy.mockRestore();
    commitSpy.mockRestore();
  });

  it('ignores stale out-of-order fetchList responses in transactions store (R-FL3)', async () => {
    const store = useTransactionsStore();

    let resolveFirst!: (v: any) => void;
    let resolveSecond!: (v: any) => void;
    const firstPromise = new Promise<any>((r) => {
      resolveFirst = r;
    });
    const secondPromise = new Promise<any>((r) => {
      resolveSecond = r;
    });

    const spy = vi
      .spyOn(txApi, 'getTransactions')
      .mockReturnValueOnce(firstPromise)
      .mockReturnValueOnce(secondPromise);

    const p1 = store.fetchList();
    store.filters.q = 'coffee';
    const p2 = store.fetchList();

    resolveSecond({ items: [{ id: 2, counterparty: 'Coffee' }], total: 1 });
    await p2;
    expect(store.items.map((x: any) => x.id)).toEqual([2]);
    expect(store.loading).toBe(false);

    // Stale first response resolves later and must NOT overwrite newer data
    resolveFirst({ items: [{ id: 1, counterparty: 'Stale' }], total: 1 });
    await p1;
    expect(store.items.map((x: any) => x.id)).toEqual([2]);
    expect(store.total).toBe(1);

    spy.mockRestore();
  });

  it('uses dynamic HTTP timeout for LLM test and model list calls (R-FM3)', async () => {
    const postSpy = vi.spyOn(apiClient, 'post').mockResolvedValue({} as any);

    await testLLM({ base_url: 'https://api.example.com/v1', model: 'm', timeout_sec: 120 });
    expect(postSpy).toHaveBeenLastCalledWith(
      '/llm/test',
      expect.objectContaining({ timeout_sec: 120 }),
      { timeout: 135000 }
    );

    await listLLMModels({ base_url: 'https://api.example.com/v1', timeout_sec: 10 });
    expect(postSpy).toHaveBeenLastCalledWith(
      '/llm/models',
      expect.objectContaining({ timeout_sec: 10 }),
      { timeout: 30000 }
    );

    postSpy.mockRestore();
  });
});
