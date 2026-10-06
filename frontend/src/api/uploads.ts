import { ApiError, api } from './client';
import type { Sas } from './types';

export function uploadAddress(
  url: string,
  development = import.meta.env.DEV,
  proxyOrigin = import.meta.env.VITE_BLOB_PROXY_ORIGIN || 'http://127.0.0.1:10000',
) {
  const parsed = new URL(url);
  return development && parsed.origin === new URL(proxyOrigin).origin
    ? `/blob-upload${parsed.pathname}${parsed.search}`
    : url;
}
export async function uploadFile(
  file: File,
  progress: (percent: number) => void,
  signal?: AbortSignal,
): Promise<Sas> {
  const sas = await api.request<Sas>('/uploads/sas', {
    method: 'POST',
    body: { filename: file.name, size: file.size },
    signal,
  });
  signal?.throwIfAborted();
  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(sas.method, uploadAddress(sas.upload_url));
    Object.entries(sas.headers).forEach(([key, value]) => xhr.setRequestHeader(key, value));
    xhr.timeout = 120_000;
    const abort = () => xhr.abort();
    signal?.addEventListener('abort', abort, { once: true });
    xhr.onloadend = () => signal?.removeEventListener('abort', abort);
    xhr.onabort = () => reject(new DOMException('Upload cancelled.', 'AbortError'));
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) progress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new ApiError(`File upload failed (${xhr.status}). Try uploading again.`, xhr.status));
    xhr.onerror = () =>
      reject(new Error('Could not upload the file. Check the storage connection and try again.'));
    xhr.ontimeout = () => reject(new Error('The upload timed out. Try uploading again.'));
    xhr.send(file);
  });
  return sas;
}
