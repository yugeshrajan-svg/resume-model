import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/v1': { target: env.API_PROXY_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
        '/blob-upload': {
          target: env.VITE_BLOB_PROXY_ORIGIN || 'http://127.0.0.1:10000',
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/blob-upload/, ''),
        },
      },
    },
  };
});
