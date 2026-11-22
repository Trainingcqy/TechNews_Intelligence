import { reactive } from "vue";

const TOKEN_KEY = "agent_token";
const THREAD_KEY = "agent_thread_id";

export const session = reactive({
  token: localStorage.getItem(TOKEN_KEY) || "",
  threadId: localStorage.getItem(THREAD_KEY) || "",
  authStatus: { text: "", type: "" }
});

export function setAuthStatus(text, type) {
  session.authStatus = { text: String(text), type };
}

export function signIn(token) {
  if (token !== session.token) clearThread();
  session.token = token;
  localStorage.setItem(TOKEN_KEY, token);
}

export function signOut() {
  session.token = "";
  localStorage.removeItem(TOKEN_KEY);
  clearThread();
}

export function invalidateToken() {
  signOut();
  setAuthStatus("Token 无效或已过期，请重新获取", "error");
}

export function setThreadId(threadId) {
  session.threadId = threadId;
  localStorage.setItem(THREAD_KEY, threadId);
}

export function clearThread() {
  session.threadId = "";
  localStorage.removeItem(THREAD_KEY);
}
