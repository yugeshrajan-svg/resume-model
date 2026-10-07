import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import DriveImport from './DriveImport';

afterEach(() => vi.restoreAllMocks());

it('imports each selected resume with consent and retries only failures', async () => {
  const request = vi
    .spyOn(api, 'request')
    .mockResolvedValueOnce({
      files: [
        { id: 'first-id', filename: 'one.pdf' },
        { id: 'second-id', filename: 'two.txt' },
      ],
    })
    .mockResolvedValueOnce({ created: true })
    .mockRejectedValueOnce(new Error('Drive quota exceeded'))
    .mockResolvedValueOnce({ created: false });
  const consent = { source: 'Application form', purpose: 'Recruitment', scope: 'talent_pool' as const };
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <DriveImport jobId="job-one" disabled={false} consentValid consent={consent} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText('Google Drive link'), {
    target: { value: 'https://drive.google.com/drive/folders/example' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Preview files' }));
  await screen.findByText(/2 resumes found/);
  const button = screen.getByRole('button', { name: 'Import selected resumes / retry failed' });
  fireEvent.click(button);
  await screen.findByText(/Drive quota exceeded/);
  await waitFor(() => expect(button).toBeEnabled());
  expect(request.mock.calls[1]).toEqual([
    '/drive/import',
    { method: 'POST', body: { file_id: 'first-id', filename: 'one.pdf', job_id: 'job-one', consent } },
  ]);
  fireEvent.click(button);
  await screen.findByText(/2 imported/);
  expect(request).toHaveBeenCalledTimes(4);
  expect(request.mock.calls[3][1]?.body).toMatchObject({ file_id: 'second-id' });
  expect(button).toBeDisabled();
});

it('previews a large folder in pages and imports all selected files beyond 500', async () => {
  const files = Array.from({ length: 552 }, (_, index) => ({
    id: `drive-${index}`,
    filename: `resume-${index}.pdf`,
  }));
  const imported: string[] = [];
  vi.spyOn(api, 'request').mockImplementation(async (path, options) => {
    if (path === '/drive/preview') return { files } as never;
    imported.push((options!.body as { file_id: string }).file_id);
    return { created: true } as never;
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <DriveImport jobId="large-job" disabled={false} consentValid consent={null} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText('Google Drive link'), {
    target: { value: 'https://drive.google.com/drive/folders/example' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Preview files' }));
  await screen.findByText(/552 resumes found/);
  expect(screen.getAllByRole('checkbox')).toHaveLength(26);
  expect(screen.queryByLabelText('resume-25.pdf')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Next Drive files' }));
  fireEvent.click(screen.getByLabelText('resume-25.pdf'));
  expect(screen.getByText(/551 selected/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Import selected resumes / retry failed' }));
  await screen.findByText(/551 imported/);
  expect(imported).toHaveLength(551);
  expect(imported).not.toContain('drive-25');
  expect(imported).toContain('drive-551');
  expect(new Set(imported).size).toBe(551);
});

it('pauses after the current file and resumes the remaining files', async () => {
  let finish!: (value: unknown) => void;
  const pending = new Promise((resolve) => {
    finish = resolve;
  });
  const request = vi
    .spyOn(api, 'request')
    .mockResolvedValueOnce({
      files: [
        { id: 'first', filename: 'one.txt' },
        { id: 'second', filename: 'two.txt' },
      ],
    })
    .mockImplementationOnce(() => pending as never)
    .mockResolvedValueOnce({ created: true });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <DriveImport jobId="pause-job" disabled={false} consentValid consent={null} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText('Google Drive link'), {
    target: { value: 'https://drive.google.com/drive/folders/example' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Preview files' }));
  await screen.findByText(/2 resumes found/);
  fireEvent.click(screen.getByRole('button', { name: 'Import selected resumes / retry failed' }));
  fireEvent.click(screen.getByRole('button', { name: 'Pause import' }));
  await act(async () => {
    finish({ created: true });
    await pending;
  });
  expect(request).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole('button', { name: 'Import selected resumes / retry failed' }));
  await screen.findByText(/2 imported/);
  expect(request).toHaveBeenCalledTimes(3);
  expect(request.mock.calls[2][1]?.body).toMatchObject({ file_id: 'second' });
});
