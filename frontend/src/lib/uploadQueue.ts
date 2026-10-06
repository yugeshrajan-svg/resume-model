import { api, message } from '../api/client';
import { uploadFile } from '../api/uploads';
import { session } from '../api/session';
import type { BulkItem, Consent, Registration, Resume } from '../api/types';

export const MAX_UPLOAD_BATCH = 500;
export const UPLOAD_CONCURRENCY = 3;

export interface UploadEntry {
  id: string;
  name: string;
  size: number;
  status: string;
  percent: number;
  resume?: Resume;
  created?: boolean;
  error?: string;
  registration?: Registration;
}
interface UploadState {
  entries: UploadEntry[];
  busy: boolean;
}
const stores = new Map<string, UploadQueue>();
export function removeCandidateUploads(company: string, candidateId: string, resumeIds: string[] = []) {
  const prefix = `shortlist.uploads.${company}.`;
  stores.forEach((queue, key) => {
    if (key.startsWith(prefix)) queue.removeCandidate(candidateId, resumeIds);
  });
  try {
    for (let index = 0; index < sessionStorage.length; index++) {
      const key = sessionStorage.key(index);
      if (!key?.startsWith(prefix) || stores.has(key)) continue;
      try {
        const entries: unknown = JSON.parse(sessionStorage.getItem(key) || '[]');
        if (!Array.isArray(entries)) {
          sessionStorage.removeItem(key);
          continue;
        }
        sessionStorage.setItem(
          key,
          JSON.stringify(
            entries.filter(
              (entry) =>
                entry?.resume?.candidate_id !== candidateId && !resumeIds.includes(entry?.resume?.id),
            ),
          ),
        );
      } catch {
        sessionStorage.removeItem(key);
      }
    }
  } catch {
    /* Storage is optional. */
  }
}
export function clearUploadQueues() {
  stores.forEach((queue) => queue.dispose());
  stores.clear();
  try {
    for (let index = sessionStorage.length - 1; index >= 0; index--) {
      const key = sessionStorage.key(index);
      if (key?.startsWith('shortlist.uploads.')) sessionStorage.removeItem(key);
    }
  } catch {
    /* Storage is optional. */
  }
}
export function getUploadQueue(company: string, job: string) {
  const key = `shortlist.uploads.${company}.${job}`;
  let store = stores.get(key);
  if (!store) {
    store = new UploadQueue(key, job, company);
    stores.set(key, store);
  }
  return store;
}
export class UploadQueue {
  private state: UploadState;
  private listeners = new Set<() => void>();
  private files = new Map<string, File>();
  private disposed = false;
  private controller: AbortController | null = null;
  constructor(
    private key: string,
    private job: string,
    private company: string,
  ) {
    let entries: UploadEntry[] = [];
    try {
      const parsed: unknown = JSON.parse(sessionStorage.getItem(key) || '[]');
      if (Array.isArray(parsed))
        entries = parsed
          .filter(
            (item): item is UploadEntry =>
              typeof item?.id === 'string' &&
              typeof item?.name === 'string' &&
              typeof item?.status === 'string',
          )
          .map((entry) =>
            !entry.resume && !entry.registration
              ? { ...entry, status: 'failed', error: 'Choose this file again to upload it.' }
              : entry.registration && !entry.resume
                ? { ...entry, status: 'uploaded' }
                : entry,
          );
    } catch {
      /* Storage is optional. */
    }
    this.state = { entries, busy: false };
  }
  get = () => this.state;
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };
  private commit(entries: UploadEntry[], busy = this.state.busy, persist = true) {
    if (this.disposed) return;
    this.state = { entries, busy };
    try {
      if (persist) sessionStorage.setItem(this.key, JSON.stringify(entries));
    } catch {
      /* Keep working in memory. */
    }
    this.listeners.forEach((fn) => fn());
  }
  update(id: string, patch: Partial<UploadEntry>, persist = true) {
    this.commit(
      this.state.entries.map((entry) => (entry.id === id ? { ...entry, ...patch } : entry)),
      this.state.busy,
      persist,
    );
  }
  add(files: File[]) {
    if (this.state.busy) return;
    if (!files.length) return;
    const waiting = this.state.entries.filter((entry) => !entry.resume).length;
    if (files.length > MAX_UPLOAD_BATCH || waiting + files.length > MAX_UPLOAD_BATCH)
      throw new Error(
        `Add up to ${MAX_UPLOAD_BATCH} files per batch. Upload or remove the waiting files before adding more.`,
      );
    const entries = files.map((file) => {
      const id = crypto.randomUUID();
      const valid = /\.(pdf|docx|txt)$/i.test(file.name) && file.size > 0;
      if (valid) this.files.set(id, file);
      return {
        id,
        name: file.name,
        size: file.size,
        status: valid ? 'pending' : 'failed',
        percent: 0,
        ...(!valid ? { error: 'Choose a nonempty PDF, DOCX or TXT file.' } : {}),
      };
    });
    this.commit([...this.state.entries, ...entries]);
  }
  remove(id: string) {
    if (this.state.busy) return;
    this.files.delete(id);
    this.commit(this.state.entries.filter((entry) => entry.id !== id));
  }
  clearWaiting() {
    if (this.state.busy) return;
    this.files.clear();
    this.commit(this.state.entries.filter((entry) => entry.resume));
  }
  removeCandidate(candidateId: string, resumeIds: string[] = []) {
    this.state.entries
      .filter(
        (entry) => entry.resume?.candidate_id === candidateId || resumeIds.includes(entry.resume?.id || ''),
      )
      .forEach((entry) => this.files.delete(entry.id));
    this.commit(
      this.state.entries.filter(
        (entry) => entry.resume?.candidate_id !== candidateId && !resumeIds.includes(entry.resume?.id || ''),
      ),
    );
  }
  canRetry(id: string) {
    return this.files.has(id) || !!this.state.entries.find((entry) => entry.id === id)?.registration;
  }
  dispose() {
    this.disposed = true;
    this.controller?.abort();
    this.files.clear();
    this.state = { entries: [], busy: false };
  }
  private assertIdentity() {
    if (this.disposed || session.get()?.company_id !== this.company)
      throw new Error('Your session changed. Sign in to the original workspace to continue.');
  }
  async start(consent: Consent | null, onlyId?: string) {
    if (this.state.busy) return;
    const entries = this.state.entries.filter(
      (entry) =>
        !entry.resume &&
        this.canRetry(entry.id) &&
        (onlyId ? entry.id === onlyId : ['pending', 'uploaded', 'failed'].includes(entry.status)),
    );
    if (!entries.length) return;
    this.controller = new AbortController();
    this.commit(this.state.entries, true);
    try {
      // Small batches bound browser memory/network use; the API registers each file independently.
      for (let offset = 0; offset < entries.length; offset += UPLOAD_CONCURRENCY) {
        if (this.disposed) break;
        const batch = entries.slice(offset, offset + UPLOAD_CONCURRENCY);
        await Promise.all(
          batch.map(async (entry) => {
            if (entry.registration) return;
            try {
              this.assertIdentity();
              this.update(entry.id, { status: 'uploading', percent: 0, error: undefined });
              const sas = await uploadFile(
                this.files.get(entry.id)!,
                // Progress changes need no synchronous rewrite of all 500 persisted entries.
                (percent) => this.update(entry.id, { percent }, false),
                this.controller!.signal,
              );
              const registration = {
                blob_name: sas.blob_name,
                filename: entry.name,
                job_id: this.job,
                consent,
              };
              this.update(entry.id, { status: 'uploaded', percent: 100, registration });
            } catch (error) {
              this.update(entry.id, { status: 'failed', error: message(error) });
            }
          }),
        );
        if (this.disposed) break;
        const ready = batch
          .map((entry) => this.state.entries.find((item) => item.id === entry.id)!)
          .filter((entry) => entry.registration && !entry.resume);
        if (!ready.length) continue;
        ready.forEach((entry) => this.update(entry.id, { status: 'registering', error: undefined }));
        try {
          this.assertIdentity();
          const result = await api.request<BulkItem[]>('/resumes/register-bulk', {
            method: 'POST',
            body: { items: ready.map((entry) => entry.registration) },
            signal: this.controller.signal,
          });
          ready.forEach((entry) => {
            const item = result.find((item) => item.blob_name === entry.registration!.blob_name);
            if (item?.resume) {
              this.files.delete(entry.id);
              this.update(entry.id, {
                resume: item.resume,
                created: item.created,
                status: item.resume.status,
                error: item.resume.status_reason || undefined,
              });
            } else
              this.update(entry.id, {
                status: 'failed',
                error: item?.error || 'Registration failed. You can retry this file.',
              });
          });
        } catch (error) {
          ready.forEach((entry) => this.update(entry.id, { status: 'failed', error: message(error) }));
        }
      }
    } finally {
      this.commit(this.state.entries, false);
    }
  }
}
