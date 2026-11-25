/**
 * Currency and financial amount formatting utilities.
 * Handles integer cents (fen) to Yuan formatting, signs, and decimal precision.
 */

export function fenToYuan(cents: number | null | undefined): number {
  if (cents === null || cents === undefined || isNaN(cents)) {
    return 0;
  }
  return cents / 100;
}

export function yuanToFen(yuan: number | string | null | undefined): number {
  if (yuan === null || yuan === undefined) return 0;
  const num = typeof yuan === 'string' ? parseFloat(yuan.replace(/[^\d.-]/g, '')) : yuan;
  if (isNaN(num)) return 0;
  return Math.round(num * 100);
}

export interface FormatAmountOptions {
  symbol?: string;      // Default '¥'
  showSign?: boolean;   // Explicit '+' or '-'
  direction?: 'expense' | 'income' | 'transfer' | 'refund' | 'neutral';
  decimals?: number;    // Default 2
  useGrouping?: boolean; // Default true (e.g. 1,234.56)
}

export function formatAmount(
  cents: number | null | undefined,
  options: FormatAmountOptions = {}
): string {
  const {
    symbol = '¥',
    showSign = false,
    direction,
    decimals = 2,
    useGrouping = true,
  } = options;

  if (cents === null || cents === undefined || isNaN(cents)) {
    return `${symbol}0.00`;
  }

  const yuan = Math.abs(cents) / 100;
  let formattedNumber = yuan.toLocaleString('zh-CN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
    useGrouping,
  });

  let prefix = '';
  if (direction === 'expense') {
    prefix = '-';
  } else if (direction === 'income' || direction === 'refund') {
    prefix = '+';
  } else if (showSign) {
    if (cents > 0) prefix = '+';
    else if (cents < 0) prefix = '-';
  } else if (cents < 0) {
    prefix = '-';
  }

  return `${prefix}${symbol}${formattedNumber}`;
}

export function formatCompact(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || isNaN(cents)) return '¥0';
  const sign = cents < 0 ? '-' : '';
  const yuan = Math.abs(cents) / 100;
  if (yuan >= 10000) {
    return `${sign}¥${(yuan / 10000).toFixed(1)}万`;
  }
  return `${sign}¥${yuan.toFixed(0)}`;
}

export function getCurrencySymbol(currency?: string): string {
  if (!currency || currency === 'CNY') return '¥';
  switch (currency.toUpperCase()) {
    case 'USD': return 'US$';
    case 'EUR': return '€';
    case 'HKD': return 'HK$';
    case 'GBP': return '£';
    case 'JPY': return 'JP¥';
    case 'TWD': return 'NT$';
    case 'AUD': return 'A$';
    case 'CAD': return 'C$';
    default: return `${currency} `;
  }
}
