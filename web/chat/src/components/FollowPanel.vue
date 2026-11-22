<script setup>
import { ref, watch } from "vue";
import { apiFetch } from "../api";
import { invalidateToken, session } from "../session";

const props = defineProps({ open: Boolean });
defineEmits(["close"]);

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

watch(
  () => props.open,
  (open) => {
    if (open) loadFollows();
  }
);

watch(
  () => session.token,
  (token) => {
    if (!token) follows.value = [];
  }
);
</script>

<template>
  <Transition name="follow-slide">
    <aside v-show="open" id="follow-panel" class="follow-panel">
      <div class="follow-panel-head">
        <h2 class="follow-panel-title">关注列表</h2>
        <button id="follow-close-btn" class="icon-btn" type="button" title="关闭" @click="$emit('close')">
          <span class="material-symbols-outlined">close</span>
        </button>
      </div>
      <form id="follow-form" class="follow-form" @submit.prevent="addFollow">
        <div class="input-group">
          <input id="follow-input" v-model="query" type="text" placeholder="添加关注的公司、产品或话题" required />
        </div>
        <button type="submit" class="icon-btn" title="添加">
          <span class="material-symbols-outlined">add</span>
        </button>
      </form>
      <p id="follow-status" class="status-msg" :class="status.type">{{ status.text }}</p>
      <ul id="follow-list" class="follow-list">
        <li v-for="follow in follows" :key="follow.follow_id" class="follow-item">
          <span class="follow-query">{{ follow.query }}</span>
          <button type="button" class="icon-btn" title="删除" @click="removeFollow(follow.follow_id)">
            <span class="material-symbols-outlined">close</span>
          </button>
        </li>
      </ul>
    </aside>
  </Transition>
</template>
