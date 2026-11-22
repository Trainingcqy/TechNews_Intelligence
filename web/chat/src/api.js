import { session } from "./session";

const API_BASE = import.meta.env.DEV ? "/api" : "https://agentapi.trainingcqy.com";

export function apiFetch(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  return fetch(API_BASE + path, { ...options, headers });
}
