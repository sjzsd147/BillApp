import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAppStore } from '@/stores/app';
import { useReviewStore } from '@/stores/review';

describe('Pinia stores', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('initializes app store with defaults', () => {
    const app = useAppStore();
    expect(app.version).toBe('1.2.2');
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
  });
});
