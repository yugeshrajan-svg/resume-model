import { expect, it } from 'vitest';
import type { Usage } from '../api/types';
import { defaultUsageRange, usageDays, validUsageRange } from './usage';

it('uses UTC calendar boundaries for thirty days including the current day', () => {
  expect(defaultUsageRange(new Date('2026-10-06T23:30:00-07:00'))).toEqual({
    from: '2026-09-08',
    to: '2026-10-08',
  });
});
it('rejects invalid calendar dates and empty or reversed intervals while supporting open bounds', () => {
  expect(validUsageRange('2026-02-30', '2026-03-02')).toBe(false);
  expect(validUsageRange('2026-10-06', '2026-10-06')).toBe(false);
  expect(validUsageRange('2026-10-07', '2026-10-06')).toBe(false);
  expect(validUsageRange('', '2026-10-06')).toBe(true);
  expect(validUsageRange('2024-02-29', '')).toBe(true);
});
it('fills missing UTC days with zeros and excludes the end day without losing reported precision', () => {
  const day = {
    date: '2026-10-04',
    cost_usd: 0.000004,
    prompt_tokens: 10,
    completion_tokens: 3,
    ocr_pages: 1,
  };
  const data = { from: '2026-10-03T00:00:00Z', to: '2026-10-06T00:00:00Z', by_day: [day] } as Usage;
  const days = usageDays(data);
  expect(days.map((day) => day.date)).toEqual(['2026-10-03', '2026-10-04', '2026-10-05']);
  expect(days[0].cost_usd).toBe(0);
  expect(days[1]).toEqual(day);
  expect(days[2].cost_usd).toBe(0);
  expect(usageDays({ ...data, from: null })).toEqual([day]);
});
