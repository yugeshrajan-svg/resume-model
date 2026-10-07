import { beforeEach, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import { session } from '../api/session';
import { uploadFile } from '../api/uploads';
import {
  UploadQueue,
  clearUploadQueues,
  getUploadQueue,
  removeCandidateUploads,
  UPLOAD_CONCURRENCY,
} from './uploadQueue';
import type { Registration } from '../api/types';

vi.mock('../api/uploads', () => ({ uploadFile: vi.fn() }));
beforeEach(() => {
  clearUploadQueues();
  session.set({
    access_token: 'test',
    refresh_token: 'refresh-token-123456789',
    expires_in: 3600,
    company_id: 'company',
    user_id: 'admin',
    role: 'admin',
    token_type: 'bearer',
  });
  vi.mocked(uploadFile)
    .mockReset()
    .mockImplementation(async (file) => ({
      blob_name: `company/${file.name}`,
      upload_url: 'https://storage/file?sig=secret',
      headers: {},
      method: 'PUT',
      expires_at: '',
    }));
});
it('erases candidate entries across mounted and stored jobs while preserving other candidates and companies', () => {
  const entries = [
    {
      id: 'linked',
      name: 'private.txt',
      status: 'ready',
      resume: { id: 'resume-1', candidate_id: 'candidate-1' },
    },
    {
      id: 'before-processing',
      name: 'private-old.txt',
      status: 'registered',
      resume: { id: 'resume-2', candidate_id: null },
    },
    {
      id: 'keep',
      name: 'keep.txt',
      status: 'ready',
      resume: { id: 'resume-3', candidate_id: 'candidate-2' },
    },
  ];
  const stored = JSON.stringify(entries);
  sessionStorage.setItem('shortlist.uploads.company.corrupt-job', '{bad-json');
  sessionStorage.setItem('shortlist.uploads.company.job-1', stored);
  sessionStorage.setItem('shortlist.uploads.company.job-2', stored);
  sessionStorage.setItem('shortlist.uploads.company.talent-pool', stored);
  sessionStorage.setItem('shortlist.uploads.other-company.job-1', stored);
  const mounted = getUploadQueue('company', 'job-1');
  const talentPool = getUploadQueue('company');
  const listener = vi.fn();
  mounted.subscribe(listener);
  removeCandidateUploads('company', 'candidate-1', ['resume-1', 'resume-2']);
  expect(sessionStorage.getItem('shortlist.uploads.company.corrupt-job')).toBeNull();
  expect(mounted.get().entries.map((entry) => entry.id)).toEqual(['keep']);
  expect(talentPool.get().entries.map((entry) => entry.id)).toEqual(['keep']);
  expect(listener).toHaveBeenCalledTimes(1);
  expect(
    JSON.parse(sessionStorage.getItem('shortlist.uploads.company.job-2')!).map(
      (entry: { id: string }) => entry.id,
    ),
  ).toEqual(['keep']);
  expect(sessionStorage.getItem('shortlist.uploads.other-company.job-1')).toBe(stored);
});
function successful(items: Registration[]) {
  return items.map((item) => ({
    blob_name: item.blob_name,
    created: true,
    status_code: 201,
    error: null,
    resume: { id: item.filename, filename: item.filename, status: 'registered', size: 1 },
  }));
}
it('keeps talent-pool upload queues separate from job queues and other companies', () => {
  const talentPool = getUploadQueue('company');
  const job = getUploadQueue('company', 'job-1');
  const otherCompany = getUploadQueue('other-company');
  talentPool.add([new File(['Skills: Python'], 'pool.txt')]);
  job.add([new File(['Skills: SQL'], 'job.txt')]);
  otherCompany.add([new File(['Skills: Java'], 'other.txt')]);

  expect(getUploadQueue('company', null)).toBe(talentPool);
  expect(getUploadQueue('company', 'job-1')).toBe(job);
  expect(talentPool).not.toBe(job);
  expect(talentPool).not.toBe(otherCompany);
  expect(talentPool.get().entries.map((entry) => entry.name)).toEqual(['pool.txt']);
  expect(job.get().entries.map((entry) => entry.name)).toEqual(['job.txt']);
  expect(otherCompany.get().entries.map((entry) => entry.name)).toEqual(['other.txt']);
  expect(sessionStorage.getItem('shortlist.uploads.company.talent-pool')).toContain('pool.txt');
  expect(sessionStorage.getItem('shortlist.uploads.company.job-1')).toContain('job.txt');
  expect(sessionStorage.getItem('shortlist.uploads.other-company.talent-pool')).toContain('other.txt');
});
it('registers unassigned resumes with talent-pool consent and preserves it when retrying after reload', async () => {
  const consent = { source: 'Candidate form', purpose: 'Future roles', scope: 'talent_pool' as const };
  const request = vi
    .spyOn(api, 'request')
    .mockRejectedValueOnce(new Error('Connection lost'))
    .mockImplementation(async (_, options) => successful((options?.body as { items: Registration[] }).items));
  const queue = getUploadQueue('company', null);
  queue.add([new File(['Skills: Python'], 'future-candidate.txt')]);
  await queue.start(consent);
  expect(queue.get().entries[0].status).toBe('failed');

  const restored = new UploadQueue('shortlist.uploads.company.talent-pool', null, 'company');
  await restored.start(null);
  expect(restored.get().entries[0].status).toBe('registered');
  expect(uploadFile).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenCalledTimes(2);
  for (const [path, options] of request.mock.calls) {
    expect(path).toBe('/resumes/register-bulk');
    expect(options?.body).toEqual({
      items: [{ blob_name: 'company/future-candidate.txt', filename: 'future-candidate.txt', job_id: null, consent }],
    });
  }
});
it('registers successful uploads while reporting each failed file', async () => {
  vi.mocked(uploadFile).mockImplementation(async (file) => {
    if (file.name === 'broken.pdf') throw new Error('Storage unavailable');
    return {
      blob_name: `company/${file.name}`,
      upload_url: 'https://storage/file?sig=secret',
      headers: {},
      method: 'PUT',
      expires_at: '',
    };
  });
  const request = vi
    .spyOn(api, 'request')
    .mockImplementation(async (_, options) => successful((options?.body as { items: Registration[] }).items));
  const queue = new UploadQueue('test-queue', 'job', 'company');
  queue.add([new File(['a'], 'good.txt'), new File(['b'], 'broken.pdf'), new File(['c'], 'other.docx')]);
  await queue.start({ source: 'Form', purpose: 'Screening' });
  expect(queue.get().entries.map((entry) => entry.status)).toEqual(['registered', 'failed', 'registered']);
  expect(request).toHaveBeenCalledTimes(1);
  expect(queue.get().busy).toBe(false);
  expect(sessionStorage.getItem('test-queue')).not.toContain('sig=secret');
});
it('retries registration using the original blob after an ambiguous network failure', async () => {
  const request = vi
    .spyOn(api, 'request')
    .mockRejectedValueOnce(new Error('Connection lost'))
    .mockImplementation(async (_, options) => successful((options?.body as { items: Registration[] }).items));
  const queue = new UploadQueue('retry-queue', 'job', 'company');
  queue.add([new File(['a'], 'good.txt')]);
  await queue.start(null);
  expect(queue.get().entries[0].status).toBe('failed');
  await queue.start(null);
  expect(queue.get().entries[0].status).toBe('registered');
  expect(uploadFile).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenCalledTimes(2);
});
it('restores registered resumes and registration retries after a page reload', async () => {
  vi.spyOn(api, 'request')
    .mockRejectedValueOnce(new Error('Connection lost'))
    .mockImplementation(async (_, options) => successful((options?.body as { items: Registration[] }).items));
  const queue = new UploadQueue('reload-queue', 'job', 'company');
  queue.add([new File(['a'], 'good.txt')]);
  await queue.start(null);
  const restored = new UploadQueue('reload-queue', 'job', 'company');
  await restored.start(null);
  expect(restored.get().entries[0].resume?.id).toBe('good.txt');
  expect(uploadFile).toHaveBeenCalledTimes(1);
  expect(new UploadQueue('reload-queue', 'job', 'company').get().entries[0].resume?.id).toBe('good.txt');
});
it('rejects invalid local files and does not send them to storage', async () => {
  const queue = new UploadQueue('invalid-queue', 'job', 'company');
  queue.add([new File(['a'], 'image.png'), new File([], 'empty.txt')]);
  await queue.start(null);
  expect(queue.get().entries.every((entry) => entry.status === 'failed')).toBe(true);
  expect(uploadFile).not.toHaveBeenCalled();
});
it('finishes cleanly when signing out disposes an active upload queue', async () => {
  let resolve!: (result: Awaited<ReturnType<typeof uploadFile>>) => void;
  vi.mocked(uploadFile).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const request = vi.spyOn(api, 'request');
  const queue = new UploadQueue('disposed-queue', 'job', 'company');
  queue.add([new File(['a'], 'good.txt')]);
  const pending = queue.start(null);
  queue.dispose();
  resolve({ blob_name: 'company/file', upload_url: '', headers: {}, method: 'PUT', expires_at: '' });
  await expect(pending).resolves.toBeUndefined();
  expect(request).not.toHaveBeenCalled();
  expect(queue.get().entries).toEqual([]);
});
it('stops registration when the workspace changes during uploading', async () => {
  vi.mocked(uploadFile).mockImplementation(async () => {
    session.set(null);
    return { blob_name: 'company/file', upload_url: '', headers: {}, method: 'PUT', expires_at: '' };
  });
  const request = vi.spyOn(api, 'request');
  const queue = new UploadQueue('changed-queue', 'job', 'company');
  queue.add([new File(['a'], 'good.txt')]);
  await queue.start(null);
  expect(request).not.toHaveBeenCalled();
  expect(queue.get().entries[0].status).toBe('failed');
});

it('uploads more than 500 resumes with bounded concurrency, preserves duplicates and consent, and retries failures', async () => {
  const fileCount = 601;
  const consent = { source: 'Application form', purpose: 'Screening' };
  let active = 0;
  let peak = 0;
  vi.mocked(uploadFile).mockImplementation(async (file, progress) => {
    active++;
    peak = Math.max(peak, active);
    progress(50);
    await new Promise((resolve) => setTimeout(resolve, 0));
    active--;
    return { blob_name: `company/${file.name}`, upload_url: '', headers: {}, method: 'PUT', expires_at: '' };
  });
  let failOne = true;
  const request = vi.spyOn(api, 'request').mockImplementation(async (_, options) => {
    const items = (options?.body as { items: Registration[] }).items;
    expect(items.length).toBeLessThanOrEqual(UPLOAD_CONCURRENCY);
    expect(items.every((item) => item.job_id === 'job' && item.consent?.source === consent.source)).toBe(
      true,
    );
    const result = successful(items);
    return result.map((item) => {
      if (item.blob_name.endsWith('/resume-550.txt') && failOne) {
        failOne = false;
        return { ...item, resume: null, error: 'Please retry', status_code: 503 };
      }
      if (item.blob_name.endsWith('/resume-600.txt')) return { ...item, created: false };
      return item;
    });
  });
  const queue = new UploadQueue('large-queue', 'job', 'company');
  queue.add(Array.from({ length: fileCount }, (_, i) => new File(['Skills: Python'], `resume-${i}.txt`)));
  await queue.start(consent);
  expect(peak).toBe(UPLOAD_CONCURRENCY);
  expect(queue.get().entries.filter((entry) => entry.resume)).toHaveLength(fileCount - 1);
  expect(queue.get().entries[550].error).toBe('Please retry');
  expect(queue.get().entries[600].created).toBe(false);
  expect(queue.get().busy).toBe(false);
  expect(uploadFile).toHaveBeenCalledTimes(fileCount);
  expect(request).toHaveBeenCalledTimes(Math.ceil(fileCount / UPLOAD_CONCURRENCY));
  await queue.start(null);
  expect(queue.get().entries.every((entry) => entry.resume)).toBe(true);
  expect(uploadFile).toHaveBeenCalledTimes(fileCount);
  const restored = new UploadQueue('large-queue', 'job', 'company');
  expect(restored.get().entries).toHaveLength(fileCount);
  expect(restored.get().entries.every((entry) => entry.resume)).toBe(true);
});

it('keeps every file across large and repeated selections while allowing removal and clearing', () => {
  const queue = new UploadQueue('collection-queue', 'job', 'company');
  const files = Array.from({ length: 801 }, (_, i) => new File(['a'], `${i}.txt`));
  queue.add(files.slice(0, 501));
  expect(queue.get().entries).toHaveLength(501);
  queue.add(files.slice(501));
  expect(queue.get().entries.map((entry) => entry.name)).toEqual(files.map((file) => file.name));
  expect(queue.get().entries.every((entry) => queue.canRetry(entry.id))).toBe(true);
  const removedId = queue.get().entries[600].id;
  queue.remove(removedId);
  expect(queue.canRetry(removedId)).toBe(false);
  expect(queue.get().entries).toHaveLength(800);
  queue.clearWaiting();
  expect(queue.get().entries).toHaveLength(0);
});

it('does not rewrite session storage for every upload progress event', async () => {
  const storage = vi.spyOn(Storage.prototype, 'setItem');
  vi.mocked(uploadFile).mockImplementation(async (file, progress) => {
    for (let percent = 0; percent <= 100; percent++) progress(percent);
    return { blob_name: `company/${file.name}`, upload_url: '', headers: {}, method: 'PUT', expires_at: '' };
  });
  vi.spyOn(api, 'request').mockImplementation(async (_, options) =>
    successful((options?.body as { items: Registration[] }).items),
  );
  const queue = new UploadQueue('progress-queue', 'job', 'company');
  queue.add([new File(['a'], 'one.txt')]);
  await queue.start(null);
  expect(storage.mock.calls.filter(([key]) => key === 'progress-queue').length).toBeLessThan(10);
  expect(queue.get().entries[0].resume).toBeDefined();
});

it('clears waiting files in one action so a full batch can be selected again after reload', async () => {
  vi.spyOn(api, 'request').mockImplementation(async (_, options) =>
    successful((options?.body as { items: Registration[] }).items),
  );
  const queue = new UploadQueue('clear-queue', 'job', 'company');
  queue.add([new File(['a'], 'saved.txt')]);
  await queue.start(null);
  queue.add(Array.from({ length: 500 }, (_, i) => new File(['b'], `pending-${i}.txt`)));
  const restored = new UploadQueue('clear-queue', 'job', 'company');
  restored.clearWaiting();
  expect(restored.get().entries.map((entry) => entry.name)).toEqual(['saved.txt']);
  expect(() =>
    restored.add(Array.from({ length: 500 }, (_, i) => new File(['b'], `retry-${i}.txt`))),
  ).not.toThrow();
  expect(restored.get().entries).toHaveLength(501);
});
