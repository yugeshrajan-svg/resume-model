export function date(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}
export function label(value: string) {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}
export function shortId(value: string) {
  return value.slice(0, 8).toUpperCase();
}
export function timestamp(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}
export function usd(value: number) {
  return `$${value.toFixed(6)}`;
}
export const featureLabels: Record<string, string> = {
  required_skill_coverage: 'Required skills',
  relevant_experience: 'Relevant experience',
  project_relevance: 'Project relevance',
  seniority_fit: 'Seniority fit',
  preferred_skill_coverage: 'Preferred skills',
  semantic_relevance: 'Overall profile match',
};
/** Color band for a 0–100 screening score. An ineligible candidate never gets a "good" color. */
export function scoreTier(score: number | null, eligible: boolean | null = null) {
  if (score === null) return '';
  if (eligible === false) return 'tier-ineligible';
  return score >= 75 ? 'tier-high' : score >= 50 ? 'tier-mid' : 'tier-low';
}
