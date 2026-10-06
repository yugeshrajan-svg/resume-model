import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ApiError } from './api/client';
import { session } from './api/session';
import App from './App';
import './fonts.css';
import './theme.css';
import { clearUploadQueues } from './lib/uploadQueue';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: (count, error) => count < 2 && !(error instanceof ApiError && error.status < 500),
    },
    mutations: { retry: false },
  },
});
const sessionIdentity = () => {
  const current = session.get();
  return current ? `${current.company_id}:${current.user_id}:${current.role}` : undefined;
};
let identity = sessionIdentity();
session.subscribe(() => {
  const next = sessionIdentity();
  if (next !== identity) {
    identity = next;
    queryClient.clear();
    clearUploadQueues();
  }
});
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
