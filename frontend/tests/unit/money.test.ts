import { describe, it, expect } from 'vitest';
import { fenToYuan, yuanToFen, formatAmount, formatCompact } from '@/utils/money';

describe('money utilities', () => {
  it('converts fen to yuan correctly', () => {
    expect(fenToYuan(100)).toBe(1);
    expect(fenToYuan(1234)).toBe(12.34);
    expect(fenToYuan(0)).toBe(0);
    expect(fenToYuan(null)).toBe(0);
    expect(fenToYuan(undefined)).toBe(0);
  });

  it('converts yuan to fen correctly', () => {
    expect(yuanToFen(1)).toBe(100);
    expect(yuanToFen('12.34')).toBe(1234);
    expect(yuanToFen('¥1,234.56')).toBe(123456);
    expect(yuanToFen(0)).toBe(0);
  });

  it('formats amounts with correct signs according to direction', () => {
    // Expense: negative sign with body color
    expect(formatAmount(2747, { direction: 'expense' })).toBe('-¥27.47');

    // Income: positive sign
    expect(formatAmount(850000, { direction: 'income' })).toBe('+¥8,500.00');

    // Refund: positive sign
    expect(formatAmount(400, { direction: 'refund' })).toBe('+¥4.00');

    // Transfer / Neutral: no sign
    expect(formatAmount(120000, { direction: 'transfer' })).toBe('¥1,200.00');
  });

  it('formats compact amounts correctly', () => {
    expect(formatCompact(50000)).toBe('¥500');
    expect(formatCompact(1500000)).toBe('¥1.5万');
  });
});
