import type { Usage, UsageMetrics } from '../api/types';

export function defaultUsageRange(now = new Date()) {
  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
  const start = new Date(end.getTime() - 30 * 86400000);
  return { from: start.toISOString().slice(0, 10), to: end.toISOString().slice(0, 10) };
}
export function validUsageRange(from: string, to: string) {
  const validDate = (value: string) => {
    if (!value) return true;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
  };
  return validDate(from) && validDate(to) && (!from || !to || from < to);
}
export function usageDays(data: Usage) {
  if (!data.from || !data.to) return data.by_day;
  const start = Date.parse(data.from);
  const end = Date.parse(data.to);
  const count = Math.ceil((end - start) / 86400000);
  if (count < 1 || count > 366) return data.by_day;
  const byDate = new Map(data.by_day.map((day) => [day.date, day]));
  const zero: UsageMetrics = { prompt_tokens: 0, completion_tokens: 0, ocr_pages: 0, cost_usd: 0 };
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(start + index * 86400000).toISOString().slice(0, 10);
    return byDate.get(date) || { date, ...zero };
  });
}
