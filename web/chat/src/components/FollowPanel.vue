<script setup>
import { onBeforeUnmount, ref, watch } from "vue";
import { apiFetch } from "../api";
import { invalidateToken, session } from "../session";

// anchor: the sidebar button that opens the card. The card lines up with its top, and a click on it is left to the
// button, which toggles the card.
const props = defineProps({ open: Boolean, anchor: { type: Object, default: null } });
const emit = defineEmits(["close"]);

const root = ref(null);
const top = ref(0);
const query = ref("");
const follows = ref([]);
const status = ref({ text: "", type: "" });

function setStatus(text, type = "") {
  status.value = { text, type };
}

async function loadFollows() {
  try {
    const res = await apiFetch("/follows");
    if (res.status === 401) {
      invalidateToken();
      return;
    }
    if (!res.ok) {
      setStatus("关注列表加载失败", "error");
      return;
    }
    const data = await res.json();
    follows.value = Array.isArray(data.follows) ? data.follows : [];
    setStatus(follows.value.length ? "" : "还没有关注项");
  } catch {
    setStatus("网络错误，请检查连接", "error");
  }
}

async function addFollow() {
  const value = query.value.trim();
  if (!value) return;
  try {
    const res = await apiFetch("/follows", {
      method: "POST",
      body: JSON.stringify({ query: value })
    });
    if (res.status === 401) {
      invalidateToken();
      return;
    }
    if (res.status === 409) {
      setStatus("关注项已达上限 20 个", "error");
      return;
    }
    if (!res.ok) {
      setStatus("添加失败", "error");
      return;
    }
  } catch {
    setStatus("网络错误，请检查连接", "error");
    return;
  }
  query.value = "";
  loadFollows();
}

async function removeFollow(followId) {
  try {
    const res = await apiFetch(`/follows/${encodeURIComponent(followId)}`, { method: "DELETE" });
    if (res.status === 401) {
      invalidateToken();
      return;
    }
  } catch {
    setStatus("网络错误，请检查连接", "error");
    return;
  }
  loadFollows();
}

function close() {
  emit("close");
  props.anchor?.focus();
}

// A click closes the card rather than a press: on a phone the click then lands on the scrim, which is still there,
// instead of on whatever the scrim uncovers.
function onDocumentClick(event) {
  if (!root.value?.contains(event.target) && !props.anchor?.contains(event.target)) emit("close");
}

function onDocumentKeydown(event) {
  if (event.key === "Escape") close();
}

function listen(on) {
  const method = on ? "addEventListener" : "removeEventListener";
  document[method]("click", onDocumentClick);
  document[method]("keydown", onDocumentKeydown);
}

watch(
  () => props.open,
  (open) => {
    listen(open);
    if (!open) return;
    top.value = props.anchor ? props.anchor.getBoundingClientRect().top : 0;
    loadFollows();
  }
);

watch(
  () => session.token,
  (token) => {
    if (!token) follows.value = [];
  }
);

onBeforeUnmount(() => listen(false));
</script>

<template>
  <Transition name="menu">
    <section
      v-if="open"
      id="follow-panel"
      ref="root"
      class="follow-panel"
      role="dialog"
      aria-labelledby="follow-panel-title"
      :style="{ '--follow-top': `${top}px` }"
    >
      <div class="follow-panel-head">
        <h2 id="follow-panel-title" class="follow-panel-title">关注列表</h2>
        <button id="follow-close-btn" v-tip="'关闭'" class="icon-btn" type="button" @click="close">
          <span class="material-symbols-outlined">close</span>
        </button>
      </div>
      <form id="follow-form" class="follow-form" @submit.prevent="addFollow">
        <div class="input-group">
          <input
            id="follow-input"
            v-model="query"
            type="text"
            placeholder="添加关注的公司、产品或话题"
            autocomplete="off"
            required
          />
        </div>
        <button v-tip="'添加'" type="submit" class="icon-btn">
          <span class="material-symbols-outlined">add</span>
        </button>
      </form>
      <p id="follow-status" class="status-msg" :class="status.type">{{ status.text }}</p>
      <ul id="follow-list" class="follow-list scroll-fade">
        <li v-for="follow in follows" :key="follow.follow_id" class="follow-item">
          <span class="follow-query">{{ follow.query }}</span>
          <button v-tip="'删除'" type="button" class="icon-btn follow-delete" @click="removeFollow(follow.follow_id)">
            <span class="material-symbols-outlined">delete</span>
          </button>
        </li>
      </ul>
    </section>
  </Transition>
</template>
