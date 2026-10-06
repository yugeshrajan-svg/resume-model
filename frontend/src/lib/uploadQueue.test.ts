import { beforeEach, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import { session } from '../api/session';
import { uploadFile } from '../api/uploads';
import {
  UploadQueue,
  clearUploadQueues,
  getUploadQueue,
  removeCandidateUploads,
  MAX_UPLOAD_BATCH,
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
  sessionStorage.setItem('shortlist.uploads.other-company.job-1', stored);
  const mounted = getUploadQueue('company', 'job-1');
  const listener = vi.fn();
  mounted.subscribe(listener);
  removeCandidateUploads('company', 'candidate-1', ['resume-1', 'resume-2']);
  expect(sessionStorage.getItem('shortlist.uploads.company.corrupt-job')).toBeNull();
  expect(mounted.get().entries.map((entry) => entry.id)).toEqual(['keep']);
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

it('uploads 500 resumes with bounded concurrency, retains partial successes, and retries one registration', async () => {
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
    const result = successful((options?.body as { items: Registration[] }).items);
    return result.map((item) => {
      if (item.blob_name.endsWith('/resume-250.txt') && failOne) {
        failOne = false;
        return { ...item, resume: null, error: 'Please retry', status_code: 503 };
      }
      return item;
    });
  });
  const queue = new UploadQueue('large-queue', 'job', 'company');
  queue.add(
    Array.from({ length: MAX_UPLOAD_BATCH }, (_, i) => new File(['Skills: Python'], `resume-${i}.txt`)),
  );
  await queue.start({ source: 'Application form', purpose: 'Screening' });
  expect(peak).toBe(UPLOAD_CONCURRENCY);
  expect(queue.get().entries.filter((entry) => entry.resume)).toHaveLength(499);
  expect(queue.get().entries[250].error).toBe('Please retry');
  expect(queue.get().busy).toBe(false);
  expect(uploadFile).toHaveBeenCalledTimes(500);
  expect(request).toHaveBeenCalledTimes(Math.ceil(500 / UPLOAD_CONCURRENCY));
  await queue.start(null);
  expect(queue.get().entries.every((entry) => entry.resume)).toBe(true);
  expect(uploadFile).toHaveBeenCalledTimes(500);
  const restored = new UploadQueue('large-queue', 'job', 'company');
  expect(restored.get().entries).toHaveLength(500);
  expect(restored.get().entries.every((entry) => entry.resume)).toBe(true);
});

it('rejects 501 files atomically and caps waiting files across multiple selections', () => {
  const queue = new UploadQueue('limit-queue', 'job', 'company');
  const files = Array.from({ length: 501 }, (_, i) => new File(['a'], `${i}.txt`));
  expect(() => queue.add(files)).toThrow('500 files per batch');
  expect(queue.get().entries).toHaveLength(0);
  queue.add(files.slice(0, 300));
  expect(() => queue.add(files.slice(300))).toThrow('500 files per batch');
  expect(queue.get().entries).toHaveLength(300);
  queue.add(files.slice(300, 500));
  expect(queue.get().entries).toHaveLength(500);
  queue.remove(queue.get().entries[0].id);
  expect(() => queue.add(files.slice(500))).not.toThrow();
  expect(queue.get().entries).toHaveLength(500);
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
