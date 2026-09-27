/**
 * Single source of truth for session identity validation (Issue #75 S1).
 *
 * Every client (AuthApiService facade, SessionApiService, guards, login,
 * header) resolves the identity through {@link resolveSessionIdentity}. An
 * identity is valid only when the server says `authenticated: true`, names a
 * user, and reports a known role. Anything else is rejected — no role is ever
 * inferred, and a missing/unknown role never falls back to `admin`.
 */

export const SESSION_ROLES = ['admin', 'viewer'] as const;

export type SessionRole = (typeof SESSION_ROLES)[number];

export interface SessionIdentity {
  user: string;
  login: string;
  role: SessionRole;
  display_name: string | null;
  email: string | null;
}

export interface PasswordPolicy {
  min_length: number;
  require_different_from_current: boolean;
}

/** Raw `GET/POST /api/session` body as it may arrive from the server. */
export interface SessionEnvelope {
  authenticated?: boolean | null;
  user?: string | null;
  login?: string | null;
  role?: string | null;
  display_name?: string | null;
  email?: string | null;
  password_policy?: Partial<PasswordPolicy> | null;
}

export const DEFAULT_PASSWORD_POLICY: PasswordPolicy = {
  min_length: 12,
  require_different_from_current: true,
};

export class InvalidSessionIdentityError extends Error {
  constructor(reason: string) {
    super(`Некоректна сесія: ${reason}`);
    this.name = 'InvalidSessionIdentityError';
  }
}

/**
 * Basic credentials encoded as UTF-8.
 *
 * `btoa` only accepts Latin-1, so a Cyrillic login or password threw before the
 * request was sent and the login page hung. The backend decodes the header as
 * UTF-8 (`base64.b64decode(...).decode("utf-8")`), so the client must encode the
 * same way.
 */
export function basicCredentials(login: string, password: string): string {
  const bytes = new TextEncoder().encode(`${login}:${password}`);
  let binary = '';
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary);
}

function asText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

export function isSessionRole(value: unknown): value is SessionRole {
  return typeof value === 'string' && (SESSION_ROLES as readonly string[]).includes(value);
}

/** Normalize the server-reported policy, falling back to the agreed default. */
export function resolvePasswordPolicy(
  policy: Partial<PasswordPolicy> | null | undefined,
): PasswordPolicy {
  const minLength = Number(policy?.min_length);
  return {
    min_length: Number.isFinite(minLength) && minLength > 0 ? minLength : DEFAULT_PASSWORD_POLICY.min_length,
    require_different_from_current: policy?.require_different_from_current !== false,
  };
}

/**
 * Return the validated identity, or `null` when the response is not a complete
 * authenticated session. Callers must treat `null` as "not signed in".
 */
export function resolveSessionIdentity(payload: unknown): SessionIdentity | null {
  if (!payload || typeof payload !== 'object') {
    return null;
  }
  const session = payload as SessionEnvelope;
  if (session.authenticated !== true) {
    return null;
  }
  const user = asText(session.user) || asText(session.login);
  if (!user) {
    return null;
  }
  if (!isSessionRole(session.role)) {
    return null;
  }
  return {
    user,
    login: asText(session.login) || user,
    role: session.role,
    display_name: asText(session.display_name) || null,
    email: asText(session.email) || null,
  };
}

/** Same contract as {@link resolveSessionIdentity}, but failing loudly. */
export function requireSessionIdentity(payload: unknown): SessionIdentity {
  const identity = resolveSessionIdentity(payload);
  if (!identity) {
    throw new InvalidSessionIdentityError('відповідь сервера не містить валідної identity');
  }
  return identity;
}
