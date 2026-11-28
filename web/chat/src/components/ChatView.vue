<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { apiFetch } from "../api";
import { renderAnswerHtml } from "../markdown";
import { clearThread, invalidateToken, session, setThreadId, signOut } from "../session";
import { readChatStream } from "../stream";
import FollowPanel from "./FollowPanel.vue";
import SideNav from "./SideNav.vue";

const props = defineProps({
  active: Boolean,
  isDark: Boolean,
  themeLabel: { type: String, required: true }
});
defineEmits(["toggle-theme"]);

const EXHAUSTED_PLACEHOLDER = "额度已耗尽，申请提额邮件已发送，通过后将通过邮件告知";
const CAPPED_PLACEHOLDER = "已达最高额度上限，如需更多请直接联系管理员";
const DEFAULT_LOADING_STATUS = "正在理解问题";
const STOPPED_TEXT = "已停止回答";
const TYPING_LEAVE_STYLE = { opacity: "0", transform: "translateY(-8px)", transition: "all 0.2s ease" };
const COPIED_MS = 1500;
const DELETE_FAILED = "删除失败，请重试";
const NAV_COLLAPSED_KEY = "technews_nav_collapsed";
const mobileQuery = window.matchMedia("(max-width: 768px)");

const items = ref([]);
const started = ref(false);
const historyLoading = ref(false);
const input = ref("");
const loading = ref(false);
const quotaText = ref("剩余 --/-- 次");
const quotaLow = ref(false);
const inputLocked = ref(false);
const placeholder = ref("");
const followOpen = ref(false);
const threads = ref([]);
const navCollapsed = ref(localStorage.getItem(NAV_COLLAPSED_KEY) === "1");
const drawerOpen = ref(false);
const isMobile = ref(mobileQuery.matches);
const pendingDelete = ref(null);
const deleting = ref(false);
const deleteError = ref("");
const messagesEl = ref(null);
const inputEl = ref(null);
const dialogEl = ref(null);
let editorEl = null;
let nextItemId = 0;
// Bumped whenever the shown conversation changes, so a history response for the conversation left behind is dropped.
let viewSerial = 0;
let threadsRequest = 0;
// The turn being answered: its abort controller, the request_id from the stream's start event, and whether it was stopped.
let activeRequest = null;

const sendDisabled = computed(() => inputLocked.value || !input.value.trim() || historyLoading.value);
// The phone layout always shows the full sidebar inside the drawer.
const rail = computed(() => navCollapsed.value && !isMobile.value);
const navToggleLabel = computed(() => (rail.value ? "展开菜单" : "收起菜单"));
const newChatDisabled = computed(() => loading.value || (!started.value && !session.threadId));
// Only the last question can be edited, because editing replaces its turn.
const lastQuestionId = computed(() => {
  for (let index = items.value.length - 1; index >= 0; index -= 1) {
    if (items.value[index].role === "user") return items.value[index].id;
  }
  return null;
});

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
    sendFromInput();
  }
}

function resetChat() {
  items.value = [];
  started.value = false;
  historyLoading.value = false;
  input.value = "";
  resetInputHeight();
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    messagesEl.value.scrollTo({ top: messagesEl.value.scrollHeight, behavior: "smooth" });
  });
}

function sourceView(source) {
  return {
    n: source.n,
    title: source.title_zh || source.title,
    // Vue binds href verbatim and source URLs come from external feeds.
    href: /^https?:\/\//i.test(source.url) ? source.url : null,
    meta: [source.source, source.published_at].filter(Boolean).join(" ")
  };
}

function processView(process) {
  const entries = Array.isArray(process?.items) ? process.items : [];
  if (!entries.length) return null;
  return {
    summary: String(process.summary || ""),
    items: entries.map((entry) => ({
      action: String(entry?.action || ""),
      subject: String(entry?.subject || ""),
      meta: String(entry?.meta || "")
    }))
  };
}

function pushItem(item) {
  items.value.push(item);
  scrollToBottom();
  return items.value[items.value.length - 1];
}

// saved: the question's turn is stored in the conversation, so editing it replaces that turn on the server.
function appendQuestion(text, saved) {
  return pushItem({ id: nextItemId++, role: "user", text, saved, copied: false, editing: false, draft: "", note: "" });
}

function appendAnswer(text, sources = [], process = null) {
  return pushItem({
    id: nextItemId++,
    role: "agent",
    text,
    html: renderAnswerHtml(text, sources),
    sources: sources.map(sourceView),
    sourcesOpen: false,
    process: processView(process),
    processOpen: false,
    copied: false
  });
}

// The source list follows the answer so its [n] markers still resolve once pasted.
function copyText(item) {
  const lines = item.sources.map((source) => [`[${source.n}]`, source.title, source.href].filter(Boolean).join(" "));
  return lines.length ? `${item.text}\n\n${lines.join("\n")}` : item.text;
}

async function copyMessage(item) {
  try {
    await navigator.clipboard.writeText(item.role === "agent" ? copyText(item) : item.text);
  } catch {
    return;
  }
  item.copied = true;
  setTimeout(() => {
    item.copied = false;
  }, COPIED_MS);
}

function showTyping() {
  return pushItem({ id: nextItemId++, role: "typing", status: DEFAULT_LOADING_STATUS, detail: "", leaving: false });
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
  placeholder.value = locked ? lockedPlaceholder : "";
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

function updateQuotaFrom(data) {
  updateQuota(data.remaining, data.quota, data.status, Boolean(data.unlimited));
}

async function fetchQuota() {
  try {
    const res = await apiFetch(`/quota/${encodeURIComponent(session.token)}`);
    if (res.status === 404 || res.status === 401) {
      invalidateToken();
      return;
    }
    const data = await res.json();
    updateQuotaFrom(data);
  } catch {}
}

async function loadThreads() {
  const request = ++threadsRequest;
  try {
    const res = await apiFetch("/threads");
    if (request !== threadsRequest) return;
    if (res.status === 401) {
      invalidateToken();
      return;
    }
    if (!res.ok) return;
    const data = await res.json();
    if (request !== threadsRequest) return;
    threads.value = Array.isArray(data.threads) ? data.threads : [];
  } catch {}
}

async function httpErrorOutcome(res) {
  if (res.status === 401) {
    invalidateToken();
    return { kind: "handled" };
  }
  if (res.status === 403) {
    showQuotaExhausted();
    return { kind: "handled" };
  }
  if (res.status === 429) return { kind: "error", text: "请求过于频繁，请稍后再试。" };
  const err = await res.json().catch(() => ({}));
  return { kind: "error", text: `出错了：${err.detail || "未知错误"}` };
}

function applyChatPayload(data) {
  const threadId = String(data.thread_id || "").trim();
  if (threadId) setThreadId(threadId);
  appendAnswer(String(data.reply || "").trim(), Array.isArray(data.sources) ? data.sources : [], data.process);
  updateQuotaFrom(data);
  loadThreads();
}

function chatRequestBody(text, replaceLast) {
  const payload = { message: text, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone };
  if (session.threadId) payload.thread_id = session.threadId;
  if (replaceLast) payload.replace_last = true;
  return JSON.stringify(payload);
}

// Resolves to final, stopped, error with the text to show, or handled when a 401 or 403 has been dealt with.
async function requestAnswer(text, replaceLast, request, typing) {
  const options = { method: "POST", body: chatRequestBody(text, replaceLast), signal: request.controller.signal };
  let streamRes = null;
  try {
    streamRes = await apiFetch("/chat-stream", options);
    if (!streamRes.ok) return await httpErrorOutcome(streamRes);
    const streamed = await readChatStream(streamRes, (event, payload) => {
      if (event === "start") request.requestId = String(payload.request_id || "");
      else if (event === "progress") updateTypingProgress(typing, payload);
    });
    if (streamed.error) return { kind: "error", text: `出错了：${streamed.error.detail || "未知错误"}` };
    if (!streamed.final) return { kind: "error", text: "出错了：未收到完整回复" };
    if (streamed.final.kind === "stopped") return { kind: "stopped" };
    return { kind: "final", data: streamed.final };
  } catch {
    if (request.stopped) return { kind: "stopped" };
    // The request never got a response: retry once without streaming.
    if (!streamRes) {
      try {
        const res = await apiFetch("/chat", options);
        if (!res.ok) return await httpErrorOutcome(res);
        return { kind: "final", data: await res.json() };
      } catch {
        if (request.stopped) return { kind: "stopped" };
      }
    }
    return { kind: "error", text: "网络错误，请检查连接后重试。" };
  }
}

// An edit left open would otherwise replace the turn this new question adds.
function sendFromInput() {
  const text = input.value.trim();
  if (!text || loading.value || historyLoading.value) return;
  for (const item of items.value) {
    if (item.role === "user") item.editing = false;
  }
  input.value = "";
  resetInputHeight();
  submitQuestion(text);
}

// replace carries the stored turn an edit takes the place of, shown again if the server keeps it.
async function submitQuestion(text, replace = null) {
  const request = { controller: new AbortController(), requestId: "", stopped: false };
  activeRequest = request;
  loading.value = true;
  started.value = true;
  const question = appendQuestion(text, false);
  const typing = showTyping();
  const outcome = await requestAnswer(text, replace !== null, request, typing);
  // Signing out detaches the request and resets the page.
  if (activeRequest !== request) return;
  activeRequest = null;
  removeTyping(typing);
  loading.value = false;
  showOutcome(outcome, question, replace);
}

// The stop request reaches the server even where the closed connection does not; the abort ends the wait at once.
function stopAnswer() {
  const request = activeRequest;
  if (!request || request.stopped) return;
  request.stopped = true;
  if (request.requestId) {
    apiFetch(`/chat-stream/${encodeURIComponent(request.requestId)}/stop`, { method: "POST" }).catch(() => {});
  }
  request.controller.abort();
}

function showOutcome(outcome, question, replace) {
  const kept = outcome.kind === "final" && outcome.data.kind !== "error";
  if (replace && !kept) {
    restoreTurn(question, replace, outcome);
    return;
  }
  if (outcome.kind === "final") {
    question.saved = kept;
    applyChatPayload(outcome.data);
  } else if (outcome.kind === "stopped") {
    pushItem({ id: nextItemId++, role: "stopped" });
    fetchQuota();
  } else if (outcome.kind === "error") {
    appendAnswer(outcome.text);
  }
  inputEl.value.focus();
}

function outcomeNote(outcome) {
  if (outcome.kind === "stopped") return STOPPED_TEXT;
  if (outcome.kind === "final") return String(outcome.data.reply || "").trim();
  return outcome.text || "";
}

// An edit that did not finish leaves the stored turn as it was: show that turn again, with the edit still open.
function restoreTurn(question, replace, outcome) {
  const index = items.value.indexOf(question);
  items.value.splice(index, items.value.length - index, ...replace.turn);
  const original = replace.turn[0];
  original.draft = question.text;
  original.note = outcomeNote(outcome);
  original.editing = true;
  if (outcome.kind === "final") updateQuotaFrom(outcome.data);
  else if (outcome.kind === "stopped") fetchQuota();
  focusEditor();
}

function setEditor(el) {
  editorEl = el;
}

function resizeEditor(el) {
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
}

async function focusEditor() {
  await nextTick();
  if (!editorEl) return;
  resizeEditor(editorEl);
  editorEl.focus();
  editorEl.setSelectionRange(editorEl.value.length, editorEl.value.length);
}

function startEdit(item) {
  item.draft = item.text;
  item.note = "";
  item.editing = true;
  focusEditor();
}

function cancelEdit(item) {
  item.editing = false;
  item.note = "";
}

// The raw value is read, as in the input box, so the update button follows IME composition.
function onEditInput(item, event) {
  item.draft = event.target.value;
  resizeEditor(event.target);
}

function onEditKeydown(item, event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submitEdit(item);
  } else if (event.key === "Escape") {
    cancelEdit(item);
  }
}

// A stored turn is replaced on the server. A question that was stopped or failed exists only on this page, so its
// edit goes out as a new question.
function submitEdit(item) {
  const text = item.draft.trim();
  if (!text || loading.value || historyLoading.value) return;
  const turn = items.value.splice(items.value.indexOf(item));
  item.editing = false;
  item.note = "";
  submitQuestion(text, item.saved ? { turn } : null);
}

function historyText(item) {
  const parts = Array.isArray(item?.parts) ? item.parts : [];
  return parts.map((part) => String(part?.text || "")).join("").trim();
}

async function loadHistory(threadId) {
  const serial = viewSerial;
  started.value = true;
  historyLoading.value = true;
  try {
    const res = await apiFetch(`/history/${encodeURIComponent(threadId)}`);
    if (serial !== viewSerial) return;
    if (res.status === 401) {
      invalidateToken();
      return;
    }
    if (res.status === 404) {
      // Deleted on another page.
      showNewChat();
      loadThreads();
      return;
    }
    if (!res.ok) return;

    const data = await res.json();
    if (serial !== viewSerial) return;
    const messages = Array.isArray(data.messages) ? data.messages : [];
    for (const item of messages) {
      const role = String(item?.role || "").toLowerCase();
      if (role === "user") {
        appendQuestion(historyText(item), true);
      } else if (role === "model") {
        appendAnswer(historyText(item), Array.isArray(item.sources) ? item.sources : [], item.process);
      }
    }
  } catch {
  } finally {
    if (serial === viewSerial) historyLoading.value = false;
  }
}

// On a phone the keyboard would cover the conversation the user just opened.
function focusInput() {
  if (!isMobile.value) inputEl.value.focus();
}

function showNewChat() {
  viewSerial += 1;
  clearThread();
  resetChat();
}

function newChat() {
  drawerOpen.value = false;
  showNewChat();
  focusInput();
}

function openThread(threadId) {
  drawerOpen.value = false;
  if (threadId === session.threadId) return;
  viewSerial += 1;
  resetChat();
  setThreadId(threadId);
  loadHistory(threadId);
  focusInput();
}

function toggleNav() {
  if (isMobile.value) {
    drawerOpen.value = false;
    return;
  }
  navCollapsed.value = !navCollapsed.value;
  localStorage.setItem(NAV_COLLAPSED_KEY, navCollapsed.value ? "1" : "0");
}

function toggleFollow() {
  drawerOpen.value = false;
  followOpen.value = !followOpen.value;
}

function closeOverlays() {
  drawerOpen.value = false;
  followOpen.value = false;
}

function requestDelete(thread) {
  deleteError.value = "";
  pendingDelete.value = thread;
}

function closeDeleteDialog() {
  pendingDelete.value = null;
}

// The dialog box fills the element, so a click that lands on the element itself hit the backdrop.
function onDialogClick(event) {
  if (event.target === dialogEl.value) closeDeleteDialog();
}

async function confirmDelete() {
  const thread = pendingDelete.value;
  if (!thread || deleting.value) return;
  deleting.value = true;
  deleteError.value = "";
  let res = null;
  try {
    res = await apiFetch(`/history/${encodeURIComponent(thread.thread_id)}`, { method: "DELETE" });
  } catch {
  } finally {
    deleting.value = false;
  }
  if (res?.status === 401) {
    invalidateToken();
    return;
  }
  // 404: the conversation is already gone.
  if (!res || (!res.ok && res.status !== 404)) {
    deleteError.value = DELETE_FAILED;
    return;
  }
  threads.value = threads.value.filter((item) => item.thread_id !== thread.thread_id);
  if (thread.thread_id === session.threadId) showNewChat();
  if (pendingDelete.value === thread) closeDeleteDialog();
}

function onMobileChange(event) {
  isMobile.value = event.matches;
  drawerOpen.value = false;
}

watch(pendingDelete, (thread) => {
  const dialog = dialogEl.value;
  if (thread && !dialog.open) dialog.showModal();
  else if (!thread && dialog.open) dialog.close();
});

watch(
  () => session.token,
  (token) => {
    if (token) {
      fetchQuota();
      loadThreads();
      return;
    }
    // Leaving mid-answer stops the turn, as closing the page does.
    activeRequest?.controller.abort();
    activeRequest = null;
    loading.value = false;
    viewSerial += 1;
    threadsRequest += 1;
    resetChat();
    threads.value = [];
    closeOverlays();
    closeDeleteDialog();
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

onMounted(() => mobileQuery.addEventListener("change", onMobileChange));
onBeforeUnmount(() => mobileQuery.removeEventListener("change", onMobileChange));

if (session.token) {
  fetchQuota();
  loadThreads();
  if (session.threadId) loadHistory(session.threadId);
}
</script>

<template>
  <main
    id="chat-view"
    class="view"
    :class="{ active, 'nav-collapsed': rail, 'follow-open': followOpen }"
  >
    <SideNav
      :class="{ 'drawer-open': drawerOpen }"
      :threads="threads"
      :current-id="session.threadId"
      :collapsed="rail"
      :busy="loading"
      :new-chat-disabled="newChatDisabled"
      :follow-open="followOpen"
      :toggle-label="navToggleLabel"
      :quota-text="quotaText"
      :quota-low="quotaLow"
      :is-dark="isDark"
      :theme-label="themeLabel"
      @toggle="toggleNav"
      @new-chat="newChat"
      @follow="toggleFollow"
      @open="openThread"
      @delete="requestDelete"
      @theme="$emit('toggle-theme')"
      @logout="signOut"
    />
    <FollowPanel :open="followOpen" @close="followOpen = false" />
    <Transition name="scrim">
      <div v-if="isMobile && (drawerOpen || followOpen)" class="scrim" @click="closeOverlays"></div>
    </Transition>

    <div class="chat-main">
      <header class="mobile-bar">
        <button v-tip="'展开菜单'" type="button" class="icon-btn" @click="drawerOpen = true">
          <span class="material-symbols-outlined">menu</span>
        </button>
        <button v-tip="'新对话'" type="button" class="icon-btn" :disabled="newChatDisabled" @click="newChat">
          <span class="material-symbols-outlined">edit_square</span>
        </button>
      </header>

      <div class="chat-container" :class="{ 'chat-empty': !started }">
        <div id="messages" ref="messagesEl" class="messages" :class="{ 'scroll-fade': started }">
          <div v-if="!started" class="welcome-msg">
            <h1 class="welcome-title">TechNews Intelligence</h1>
          </div>
          <template v-for="item in items" :key="item.id">
            <div v-if="item.role === 'typing'" class="msg agent typing-msg" :style="item.leaving ? TYPING_LEAVE_STYLE : null">
              <div class="typing-indicator">
                <div class="typing-copy">
                  <div class="typing-label" role="status" aria-live="polite">{{ item.status }}</div>
                  <div
                    class="typing-detail"
                    :data-empty="String(!item.detail)"
                    :aria-hidden="String(!item.detail)"
                  >{{ item.detail }}</div>
                </div>
              </div>
            </div>
            <div v-else-if="item.role === 'agent'" class="msg agent">
              <button
                v-if="item.process"
                type="button"
                class="sources-toggle process-toggle"
                :aria-expanded="String(item.processOpen)"
                @click="item.processOpen = !item.processOpen"
              >
                {{ item.process.summary }}
                <span class="material-symbols-outlined" aria-hidden="true">expand_more</span>
              </button>
              <ul v-if="item.process && item.processOpen" class="msg-sources msg-process">
                <li v-for="(entry, index) in item.process.items" :key="index">
                  <span>{{ entry.action }}</span>
                  <span>{{ entry.subject }} <span v-if="entry.meta" class="msg-sources-meta">{{ entry.meta }}</span></span>
                </li>
              </ul>
              <div class="msg-content" v-html="item.html"></div>
              <div class="msg-actions">
                <button v-tip="item.copied ? '已复制' : '复制'" type="button" class="icon-btn" @click="copyMessage(item)">
                  <span class="material-symbols-outlined">{{ item.copied ? "check" : "content_copy" }}</span>
                </button>
                <button
                  v-if="item.sources.length"
                  type="button"
                  class="sources-toggle"
                  :aria-expanded="String(item.sourcesOpen)"
                  @click="item.sourcesOpen = !item.sourcesOpen"
                >
                  来源<span class="sources-count">{{ item.sources.length }}</span>
                  <span class="material-symbols-outlined" aria-hidden="true">expand_more</span>
                </button>
              </div>
              <ol v-if="item.sourcesOpen" class="msg-sources">
                <li v-for="source in item.sources" :key="source.n">
                  <span>[{{ source.n }}]</span>
                  <!-- The space between the link and the meta must share one line, or Vue drops it. -->
                  <span>
                    <a
                      :href="source.href"
                      target="_blank"
                      rel="noopener noreferrer"
                    >{{ source.title }}</a> <span v-if="source.meta" class="msg-sources-meta">{{ source.meta }}</span>
                  </span>
                </li>
              </ol>
            </div>
            <div v-else-if="item.role === 'stopped'" class="msg agent stopped-note">{{ STOPPED_TEXT }}</div>
            <div v-else class="msg user" :class="{ editing: item.editing }">
              <template v-if="item.editing">
                <div class="edit-box">
                  <textarea
                    :ref="setEditor"
                    class="edit-input"
                    :value="item.draft"
                    rows="1"
                    aria-label="编辑提问"
                    @input="onEditInput(item, $event)"
                    @keydown="onEditKeydown(item, $event)"
                  ></textarea>
                  <div class="edit-actions">
                    <button type="button" class="dialog-btn" @click="cancelEdit(item)">取消</button>
                    <button
                      type="button"
                      class="dialog-btn"
                      :disabled="!item.draft.trim() || loading"
                      @click="submitEdit(item)"
                    >更新</button>
                  </div>
                </div>
                <p v-if="item.note" class="edit-note">{{ item.note }}</p>
              </template>
              <template v-else>
                <div class="msg-content">{{ item.text }}</div>
                <div class="msg-actions user-actions">
                  <button v-tip="item.copied ? '已复制' : '复制'" type="button" class="icon-btn" @click="copyMessage(item)">
                    <span class="material-symbols-outlined">{{ item.copied ? "check" : "content_copy" }}</span>
                  </button>
                  <button
                    v-if="item.id === lastQuestionId && !loading"
                    v-tip="'编辑'"
                    type="button"
                    class="icon-btn"
                    @click="startEdit(item)"
                  >
                    <span class="material-symbols-outlined">edit</span>
                  </button>
                </div>
              </template>
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
            <button v-if="loading" v-tip="'停止回答'" type="button" class="send-btn stop-btn" @click="stopAnswer">
              <span class="material-symbols-outlined">stop</span>
            </button>
            <button v-else id="send-btn" type="button" class="send-btn" :disabled="sendDisabled" @click="sendFromInput">
              <span class="material-symbols-outlined">arrow_upward</span>
            </button>
          </div>
        </div>
      </div>
    </div>

    <dialog
      ref="dialogEl"
      class="confirm-dialog"
      aria-labelledby="delete-dialog-title"
      @close="closeDeleteDialog"
      @click="onDialogClick"
    >
      <div class="confirm-dialog-box">
        <h2 id="delete-dialog-title" class="confirm-dialog-title">删除对话？</h2>
        <p class="confirm-dialog-text">删除后无法恢复。</p>
        <p v-if="deleteError" class="confirm-dialog-error" role="alert">{{ deleteError }}</p>
        <div class="confirm-dialog-actions">
          <button type="button" class="dialog-btn" @click="closeDeleteDialog">取消</button>
          <button type="button" class="dialog-btn" :disabled="deleting" @click="confirmDelete">删除</button>
        </div>
      </div>
    </dialog>
  </main>
</template>
