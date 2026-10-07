export type Role = 'admin' | 'recruiter' | 'hiring_manager';
export interface Tokens {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  company_id: string;
  user_id: string;
  role: Role;
  token_type: 'bearer';
}
export interface Session extends Tokens {
  expiresAt: number;
}
export interface Job {
  id: string;
  title: string;
  jd_text: string;
  created_at: string;
}
export interface Skill {
  name: string;
  alternatives: string[];
  evidence: string | null;
}
export interface Requirements {
  role: string;
  seniority: string | null;
  experience_min_years: number | null;
  experience_domain: string | null;
  required_skills: Skill[];
  preferred_skills: Skill[];
  locations: string[];
  max_notice_period_days: number | null;
  required_skills_min_match: number | 'all' | null;
  score_weights: Record<string, number> | null;
}
export interface RequirementsVersion {
  id: string;
  version: number;
  requirements: Requirements;
  source: string;
  analysis_meta: Record<string, unknown> | null;
  created_at: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
}
export interface Consent {
  source: string;
  purpose: string;
  /** "talent_pool": the candidate also agreed to be considered for future roles. */
  scope?: 'job' | 'talent_pool';
  granted_at?: string;
}
export interface Resume {
  id: string;
  candidate_id: string | null;
  version: number | null;
  filename: string | null;
  mime: string;
  size: number;
  status: string;
  status_reason: string | null;
  processing_generation: number;
  processed_generation: number;
  created_at: string;
}
export interface Sas {
  blob_name: string;
  upload_url: string;
  method: 'PUT';
  headers: Record<string, string>;
  expires_at: string;
}
export interface Registration {
  blob_name: string;
  filename: string;
  job_id: string | null;
  consent: Consent | null;
}
export interface BulkItem {
  blob_name: string;
  resume: Resume | null;
  created: boolean;
  error: string | null;
  status_code: number;
}
export type ResumeSource = 'database' | 'uploads' | 'both';
export interface SourceCount {
  total: number;
  screenable: number;
  blocked: number;
}
export interface ScreeningSources {
  database: SourceCount;
  uploads: SourceCount;
  both: SourceCount;
  pending_uploads: number;
  database_excluded: number;
}
export interface Run {
  id: string;
  job_id: string;
  requirements_version: number;
  reference_date: string | null;
  retry_of_run_id: string | null;
  status: string;
  total: number;
  done: number;
  blocked: { candidate_id?: string; application_id?: string; resume_id?: string; reason?: string }[];
  created_at: string;
  finished_at: string | null;
  cost_usd: number;
  snapshot?: { resume_source?: ResumeSource };
}
export interface Explanation {
  strengths?: { feature: string; text: string; quote: string }[];
  gaps?: { feature: string; text: string; source: string }[];
  warnings?: string[];
}
export interface RankedCandidate {
  application_id: string;
  candidate_id: string;
  rank: number | null;
  score: number | null;
  eligible: boolean | null;
  status: string;
  reprocess_reason: string | null;
  stage: string;
  knockout_reasons: string[];
  explanation: Explanation | null;
  explanation_source: string | null;
}
export interface CandidatesPage {
  run_id: string;
  run_status: string;
  total: number;
  limit: number;
  offset: number;
  items: RankedCandidate[];
}
export interface Feature {
  name: string;
  applicable: boolean;
  value: number;
  detail: string;
  evidence: string[];
}
export interface ScreeningHistory {
  run_id: string;
  job_id: string;
  requirements_version: number;
  run_created_at: string;
  status: string;
  score: number | null;
  eligible: boolean | null;
  knockout: { reasons?: { rule: string; detail: string }[] } | null;
  features: Feature[] | null;
  explanation: Explanation | null;
  explanation_source: string | null;
}
export interface RunInput {
  application_id: string;
  candidate_id: string;
  resume_id: string;
  resume_version: number;
  processing_generation: number;
  content_hash: string;
}
export interface CandidateProfile {
  candidate_id: string;
  created_at: string;
  contact?: { name: string | null; email: string | null; phone: string | null } | null;
  resumes: {
    resume_id: string;
    filename: string | null;
    version: number;
    status: string;
    created_at: string;
  }[];
  applications: { application_id: string; job_id: string; stage: string; decisions?: DecisionRecord[] }[];
  consents: { source: string; purpose: string; granted_at: string; revoked_at: string | null }[];
  screening: ScreeningHistory[];
}
export type DecisionChoice = 'shortlist' | 'reject' | 'review';
export interface DecisionRecord {
  decision_id: string;
  decision: DecisionChoice | 'move';
  previous_stage: string | null;
  new_stage: string;
  reason: string | null;
  decided_at: string;
  reviewer: { user_id: string; email?: string; role: Role } | null;
}
export interface DecisionHistory {
  application_id: string;
  candidate_id: string;
  job_id: string;
  stage: string;
  decisions: DecisionRecord[];
}
export interface ConsentRecord {
  consent_id: string;
  source: string;
  purpose: string;
  scope: 'job' | 'talent_pool';
  granted_at: string;
  revoked_at: string | null;
  recorded_by: string | null;
  active: boolean;
}
export interface ConsentHistory {
  candidate_id: string;
  has_active_consent: boolean;
  consents: ConsentRecord[];
}
export interface Comparison {
  job_id: string;
  run_id: string;
  run_status: string;
  run_complete: boolean;
  done: number;
  total: number;
  candidates: {
    candidate_id: string;
    status: 'scored' | 'pending' | 'needs_reprocess' | 'blocked' | 'not_in_run';
    screened: boolean;
    reason: string | null;
    reprocess_reason: string | null;
    score: number | null;
    eligible: boolean | null;
    features: Record<string, number | null>;
    strengths: string[];
    gaps: string[];
  }[];
}
export interface Company {
  id: string;
  name: string;
  retention_days: number;
  retention_sweep_hour_utc: number;
  candidates_total: number;
  candidates_past_retention: number;
}
export interface User {
  id: string;
  email: string;
  role: Role;
  is_active: boolean;
  created_at: string;
}
export interface UsageMetrics {
  prompt_tokens: number;
  completion_tokens: number;
  ocr_pages: number;
  cost_usd: number;
}
export interface Usage {
  from: string | null;
  to: string | null;
  totals: UsageMetrics;
  by_kind: Record<string, UsageMetrics>;
  runs: (UsageMetrics & { run_id: string; job_id: string })[];
  by_day: (UsageMetrics & { date: string })[];
}
export interface TalentResult {
  candidate_id: string;
  similarity: number;
  name?: string | null;
  matched_resume: { resume_id: string; version: number | null; created_at: string | null };
  applications: number;
  in_job: { application_id: string; stage: string } | null;
}
export interface TalentSearch {
  model: string;
  query_source: 'query' | 'job_description';
  job_id: string | null;
  indexed_candidates: number;
  resumes_pending_index: number;
  results: TalentResult[];
}
export interface PoolMatch {
  match_id: string;
  candidate_id: string;
  name?: string | null;
  resume_id: string;
  similarity: number | null;
  status: 'pending' | 'checked' | 'failed';
  eligible: boolean | null;
  reasons: string[];
  warnings: string[];
  failure_reason: string | null;
  checked_at: string | null;
  in_job: string | null;
}
export interface PoolMatches {
  job_id: string;
  requirements_version: number | null;
  confirmed: boolean;
  pool_size: number;
  progress: { total: number; checked: number; pending: number; failed: number };
  eligible: PoolMatch[];
  ineligible: PoolMatch[];
  failed: PoolMatch[];
}
