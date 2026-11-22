<script setup>
import { computed, ref, watch } from "vue";
import { apiFetch } from "../api";
import { renderAnswerHtml } from "../markdown";
import { clearThread, invalidateToken, session, setThreadId, signOut } from "../session";
import { readChatStream } from "../stream";
import FollowPanel from "./FollowPanel.vue";

const props = defineProps({ active: Boolean });

const DEFAULT_PLACEHOLDER = "你想知道什么？";
const EXHAUSTED_PLACEHOLDER = "额度已耗尽，申请提额邮件已发送，通过后将通过邮件告知";
const CAPPED_PLACEHOLDER = "已达最高额度上限，如需更多请直接联系管理员";
const DEFAULT_LOADING_STATUS = "正在理解问题";
const TYPING_LEAVE_STYLE = { opacity: "0", transform: "translateY(-8px)", transition: "all 0.2s ease" };

const items = ref([]);
const started = ref(false);
const input = ref("");
const loading = ref(false);
const quotaText = ref("剩余 --/-- 次");
const quotaLow = ref(false);
const inputLocked = ref(false);
const placeholder = ref(DEFAULT_PLACEHOLDER);
const followOpen = ref(false);
const messagesEl = ref(null);
const inputEl = ref(null);
let nextItemId = 0;

const sendDisabled = computed(() => inputLocked.value || !input.value.trim() || loading.value);

function resetInputHeight() {
  inputEl.value.style.height = "auto";
}

// v-model would skip updates during IME composition; the send button follows the raw value.
function onInput(event) {
  const el = event.target;
  input.value = el.value;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
}

function onKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
}

function resetChat() {
  items.value = [];
  started.value = false;
  input.value = "";
  resetInputHeight();
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    messagesEl.value.scrollTo({ top: messagesEl.value.scrollHeight, behavior: "smooth" });
  });
}

function appendMessage(role, text, sources = []) {
  if (role === "agent") {
    items.value.push({ id: nextItemId++, role, html: renderAnswerHtml(text, sources) });
  } else {
    items.value.push({ id: nextItemId++, role, text });
  }
  scrollToBottom();
}

function showTyping() {
  items.value.push({ id: nextItemId++, role: "typing", status: DEFAULT_LOADING_STATUS, detail: "", leaving: false });
  scrollToBottom();
  return items.value[items.value.length - 1];
}

function updateTypingProgress(typing, payload) {
  const title = String(payload.title || "").trim();
  if (title) typing.status = title;
  typing.detail = String(payload.detail || "").trim();
}

function removeTyping(typing) {
  if (typing.leaving || !items.value.includes(typing)) return;
  typing.leaving = true;
  setTimeout(() => {
    items.value = items.value.filter((item) => item !== typing);
  }, 200);
}

function setInputLocked(locked, lockedPlaceholder = EXHAUSTED_PLACEHOLDER) {
  inputLocked.value = locked;
  placeholder.value = locked ? lockedPlaceholder : DEFAULT_PLACEHOLDER;
  if (locked) {
    input.value = "";
    resetInputHeight();
  }
}

function showQuotaExhausted(status = "pending") {
  setInputLocked(true, status === "capped" ? CAPPED_PLACEHOLDER : EXHAUSTED_PLACEHOLDER);
}

function updateQuota(remaining, total, status, unlimited = false) {
  if (unlimited) {
    quotaText.value = "额度 无限";
    quotaLow.value = false;
    setInputLocked(false);
    return;
  }

  const safeRemaining = Number.isFinite(remaining) ? remaining : 0;
  const safeTotal = Number.isFinite(total) ? total : 0;
  quotaText.value = `剩余 ${safeRemaining}/${safeTotal} 次`;
  quotaLow.value = safeRemaining <= 3;

  if ((status === "pending" || status === "exhausted") && safeRemaining <= 0) {
    showQuotaExhausted("pending");
  } else if (status === "capped" && safeRemaining <= 0) {
    showQuotaExhausted("capped");
  } else {
    setInputLocked(false);
  }
}

async function fetchQuota() {
  try {
    const res = await apiFetch(`/quota/${encodeURIComponent(session.token)}`);
    if (res.status === 404 || res.status === 401) {
      invalidateToken();
      return;
    }
    const data = await res.json();
    updateQuota(data.remaining, data.quota, data.status, Boolean(data.unlimited));
  } catch {}
}

async function handleChatHttpError(res) {
  if (res.status === 401) {
    invalidateToken();
  } else if (res.status === 403) {
    showQuotaExhausted();
  } else if (res.status === 429) {
    appendMessage("agent", "请求过于频繁，请稍后再试。");
  } else {
    const err = await res.json().catch(() => ({}));
    appendMessage("agent", `出错了：${err.detail || "未知错误"}`);
  }
}

function applyChatPayload(data) {
  const threadId = String(data.thread_id || "").trim();
  if (threadId) setThreadId(threadId);
  appendMessage("agent", String(data.reply || "").trim(), Array.isArray(data.sources) ? data.sources : []);
  updateQuota(data.remaining, data.quota, data.status, Boolean(data.unlimited));
}

function chatRequestBody(text) {
  const payload = { message: text };
  if (session.threadId) payload.thread_id = session.threadId;
  return JSON.stringify(payload);
}

async function sendMessage() {
  const text = input.value.trim();
  if (!text || loading.value) return;

  loading.value = true;
  started.value = true;
  appendMessage("user", text);
  input.value = "";
  resetInputHeight();

  const typing = showTyping();
  let streamRes = null;

  try {
    streamRes = await apiFetch("/chat-stream", { method: "POST", body: chatRequestBody(text) });
    if (!streamRes.ok) {
      await handleChatHttpError(streamRes);
      return;
    }

    const streamed = await readChatStream(streamRes, (event, payload) => {
      if (event === "progress") updateTypingProgress(typing, payload);
    });
    if (streamed.error) {
      appendMessage("agent", `出错了：${streamed.error.detail || "未知错误"}`);
      return;
    }
    if (!streamed.final) {
      appendMessage("agent", "出错了：未收到完整回复");
      return;
    }
    removeTyping(typing);
    applyChatPayload(streamed.final);
  } catch {
    // The request never got a response: retry once without streaming.
    if (!streamRes) {
      try {
        const res = await apiFetch("/chat", { method: "POST", body: chatRequestBody(text) });
        if (!res.ok) {
          await handleChatHttpError(res);
          return;
        }
        const data = await res.json();
        removeTyping(typing);
        applyChatPayload(data);
        return;
      } catch {}
    }
    appendMessage("agent", "网络错误，请检查连接后重试。");
  } finally {
    removeTyping(typing);
    loading.value = false;
    inputEl.value.focus();
  }
}

function historyText(item) {
  const parts = Array.isArray(item?.parts) ? item.parts : [];
  return parts.map((part) => String(part?.text || "")).join("").trim();
}

async function loadHistory() {
  try {
    const res = await apiFetch(`/history/${encodeURIComponent(session.threadId)}`);
    if (res.status === 401) {
      invalidateToken();
      return;
    }
    if (res.status === 404) {
      clearThread();
      return;
    }
    if (!res.ok) return;

    const data = await res.json();
    const messages = Array.isArray(data.messages) ? data.messages : [];
    if (!messages.length) return;

    started.value = true;
    for (const item of messages) {
      const role = String(item?.role || "").toLowerCase();
      if (role === "user") {
        appendMessage("user", historyText(item));
      } else if (role === "model") {
        appendMessage("agent", historyText(item), Array.isArray(item.sources) ? item.sources : []);
      }
    }
  } catch {}
}

async function clearChat() {
  const previousThreadId = session.threadId;
  clearThread();
  resetChat();
  if (!previousThreadId) return;
  // Best effort: the client state is already cleared and an orphaned thread is never read again.
  try {
    await apiFetch(`/history/${encodeURIComponent(previousThreadId)}`, { method: "DELETE" });
  } catch {}
}

function logout() {
  resetChat();
  signOut();
}

watch(
  () => session.token,
  (token) => {
    if (token) {
      fetchQuota();
      return;
    }
    followOpen.value = false;
    setInputLocked(false);
  }
);

watch(
  () => props.active,
  (active) => {
    if (active) inputEl.value.focus();
  },
  { flush: "post" }
);

if (session.token) {
  fetchQuota();
  if (session.threadId) loadHistory();
}
</script>

<template>
  <main id="chat-view" class="view" :class="{ active }">
    <FollowPanel :open="followOpen" @close="followOpen = false" />
    <div class="chat-container" :class="{ 'chat-empty': !started }">
      <div class="chat-header">
        <div class="chat-header-left">
          <span id="quota-display" class="quota-badge" :class="{ low: quotaLow }">{{ quotaText }}</span>
        </div>
        <div class="chat-header-actions">
          <button id="follow-btn" class="icon-btn" title="关注列表" @click="followOpen = !followOpen">
            <span class="material-symbols-outlined">bookmark</span>
          </button>
          <button id="clear-btn" class="icon-btn" title="清空对话" @click="clearChat">
            <span class="material-symbols-outlined">delete_sweep</span>
          </button>
          <button id="logout-btn" class="icon-btn" title="退出登录" @click="logout">
            <span class="material-symbols-outlined">logout</span>
          </button>
        </div>
      </div>

      <div id="messages" ref="messagesEl" class="messages">
        <div v-if="!started" class="welcome-msg">
          <h1 class="welcome-title">TechNews Intelligence</h1>
        </div>
        <template v-for="item in items" :key="item.id">
          <div v-if="item.role === 'typing'" class="msg agent typing-msg" :style="item.leaving ? TYPING_LEAVE_STYLE : null">
            <div class="typing-indicator">
              <div class="typing-copy">
                <div class="typing-label" role="status" aria-live="polite" :data-text="item.status">{{ item.status }}</div>
                <div
                  class="typing-detail"
                  :data-text="item.detail"
                  :data-empty="String(!item.detail)"
                  :aria-hidden="String(!item.detail)"
                >{{ item.detail }}</div>
              </div>
            </div>
          </div>
          <div v-else-if="item.role === 'agent'" class="msg agent">
            <div class="msg-content" v-html="item.html"></div>
          </div>
          <div v-else class="msg user">
            <div class="msg-content">{{ item.text }}</div>
          </div>
        </template>
      </div>

      <div class="input-bar">
        <div class="input-bar-inner" :class="{ 'quota-locked': inputLocked }">
          <textarea
            id="chat-input"
            ref="inputEl"
            :value="input"
            :placeholder="placeholder"
            rows="1"
            :disabled="inputLocked"
            @input="onInput"
            @keydown="onKeydown"
          ></textarea>
          <button id="send-btn" class="send-btn" :disabled="sendDisabled" @click="sendMessage">
            <span class="material-symbols-outlined">arrow_upward</span>
          </button>
        </div>
      </div>
    </div>
  </main>
</template>
