import { expect, it } from 'vitest';
import { uploadAddress } from './uploads';
it('proxies only the configured local storage origin and keeps the SAS query intact', () => {
  expect(uploadAddress('http://127.0.0.1:10000/account/file?sp=c&sig=secret', true)).toBe(
    '/blob-upload/account/file?sp=c&sig=secret',
  );
  expect(uploadAddress('https://private.blob.core.windows.net/container/file?sig=secret', true)).toBe(
    'https://private.blob.core.windows.net/container/file?sig=secret',
  );
  expect(uploadAddress('http://127.0.0.1:10000/account/file?sig=secret', false)).toBe(
    'http://127.0.0.1:10000/account/file?sig=secret',
  );
  expect(
    uploadAddress('http://127.0.0.1:10010/account/file?sig=secret', true, 'http://127.0.0.1:10010'),
  ).toBe('/blob-upload/account/file?sig=secret');
});
