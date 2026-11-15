import dayjs from 'dayjs';

export function fmtDate(val: string | Date | null | undefined): string {
  if (!val) return '-';
  const d = dayjs(val);
  return d.isValid() ? d.format('YYYY-MM-DD') : String(val);
}

export function fmtDateTime(val: string | Date | null | undefined): string {
  if (!val) return '-';
  const d = dayjs(val);
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm:ss') : String(val);
}

export function fmtTime(val: string | Date | null | undefined): string {
  if (!val) return '-';
  const d = dayjs(val);
  return d.isValid() ? d.format('HH:mm:ss') : String(val);
}

export function currentMonthRange(): [string, string] {
  const now = dayjs();
  return [now.startOf('month').format('YYYY-MM-DD'), now.endOf('month').format('YYYY-MM-DD')];
}

export function last3MonthsRange(): [string, string] {
  const now = dayjs();
  return [now.subtract(2, 'month').startOf('month').format('YYYY-MM-DD'), now.endOf('month').format('YYYY-MM-DD')];
}

export function currentYearRange(): [string, string] {
  const now = dayjs();
  return [now.startOf('year').format('YYYY-MM-DD'), now.endOf('year').format('YYYY-MM-DD')];
}
