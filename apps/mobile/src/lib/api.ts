import { supabase } from '@/lib/supabase';

const apiUrl = (process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(message);
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

async function authHeaders(): Promise<Record<string, string>> {
  const { data } = await supabase.auth.getSession();
  if (!data.session?.access_token) {
    throw new ApiError('请先登录', 401);
  }
  return {
    Authorization: `Bearer ${data.session.access_token}`,
    'Content-Type': 'application/json',
    'X-Client-Version': '0.1.0',
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = await authHeaders();
  let response: Response;
  try {
    response = await fetch(`${apiUrl}${path}`, {
      ...init,
      headers: { ...headers, ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError('网络连接失败，请稍后重试。', 0, 'NETWORK_ERROR');
  }
  if (!response.ok) {
    let message = '请求失败，请稍后再试';
    let code: string | undefined;
    try {
      const payload = await response.json();
      message = payload.detail ?? message;
      code = payload.code;
    } catch {
      // Keep the safe user-facing fallback.
    }
    throw new ApiError(message, response.status, code);
  }
  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  event: (eventName: string, properties: Record<string, string | number | boolean | null> = {}) =>
    request<{ ok: boolean }>('/events', {
      method: 'POST',
      body: JSON.stringify({ event_name: eventName, properties }),
    }),
};
