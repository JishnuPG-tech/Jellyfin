import AsyncStorage from "@react-native-async-storage/async-storage";
import { ThemeName } from "../constants/types";

export const storageKeys = {
  serverUrl: "opencode_server_url",
  authType: "opencode_auth_type",
  authValue: "opencode_auth_value",
  theme: "opencode_theme",
  lastSessionId: "opencode_last_session_id",
} as const;

export type AuthType = "none" | "basic" | "bearer" | "apikey";

const cache: Record<string, string | null> = {};

async function get(key: string): Promise<string | null> {
  if (cache[key] !== undefined) return cache[key];
  const val = await AsyncStorage.getItem(key);
  cache[key] = val;
  return val;
}

async function set(key: string, value: string): Promise<void> {
  cache[key] = value;
  await AsyncStorage.setItem(key, value);
}

async function del(key: string): Promise<void> {
  cache[key] = null;
  await AsyncStorage.removeItem(key);
}

function getSync(key: string): string | null {
  if (cache[key] !== undefined) return cache[key];
  return null;
}

// Preload all keys on module load
const _loaded = (async () => {
  const entries = await AsyncStorage.multiGet(Object.values(storageKeys));
  for (const [k, v] of entries) {
    cache[k] = v;
  }
})();

// ── Server URL ──

export function getServerUrl(): string {
  return getSync(storageKeys.serverUrl) || "http://127.0.0.1:4096";
}

export async function setServerUrl(url: string): Promise<void> {
  await set(storageKeys.serverUrl, url);
}

// ── Auth ──

export function getAuthType(): AuthType {
  return (getSync(storageKeys.authType) as AuthType) || "none";
}

export async function setAuthType(type: AuthType): Promise<void> {
  await set(storageKeys.authType, type);
}

export function getAuthValue(): string {
  return getSync(storageKeys.authValue) || "";
}

export async function setAuthValue(value: string): Promise<void> {
  await set(storageKeys.authValue, value);
}

export function getCredentials(): { username: string; password: string } {
  const val = getAuthValue();
  if (getAuthType() === "basic" && val.includes(":")) {
    const [username, ...rest] = val.split(":");
    return { username, password: rest.join(":") };
  }
  return { username: "", password: "" };
}

export async function setCredentials(username: string, password: string): Promise<void> {
  await setAuthType("basic");
  await setAuthValue(`${username}:${password}`);
}

export async function clearAuth(): Promise<void> {
  await del(storageKeys.authType);
  await del(storageKeys.authValue);
}

export function getAuthHeader(): Record<string, string> {
  const type = getAuthType();
  const value = getAuthValue();

  if (!value) return {};

  switch (type) {
    case "basic": {
      const creds = getCredentials();
      if (creds.username) {
        return { Authorization: `Basic ${btoa(`${creds.username}:${creds.password}`)}` };
      }
      return {};
    }
    case "bearer":
      return { Authorization: `Bearer ${value}` };
    case "apikey":
      return { "X-API-Key": value };
    default:
      return {};
  }
}

// ── Theme ──

export function getTheme(): ThemeName {
  return (getSync(storageKeys.theme) as ThemeName) || "tokyo";
}

export async function setTheme(theme: ThemeName): Promise<void> {
  await set(storageKeys.theme, theme);
}

// ── Last session ──

export function getLastSessionId(): string | undefined {
  return getSync(storageKeys.lastSessionId) || undefined;
}

export async function setLastSessionId(id: string): Promise<void> {
  await set(storageKeys.lastSessionId, id);
}
