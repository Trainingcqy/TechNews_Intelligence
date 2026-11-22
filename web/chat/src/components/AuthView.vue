<script setup>
import { ref } from "vue";
import { apiFetch } from "../api";
import { session, setAuthStatus, signIn } from "../session";

defineProps({ active: Boolean });

const email = ref("");
const tokenValue = ref("");
const tokenInputEl = ref(null);
const emailLoading = ref(false);
const pressedButton = ref("");

async function pressButton(name) {
  pressedButton.value = name;
  await new Promise((resolve) => setTimeout(resolve, 90));
  pressedButton.value = "";
}

async function requestAccess() {
  const value = email.value.trim();
  if (!value) return;

  await pressButton("email");
  emailLoading.value = true;
  setAuthStatus("", "");

  try {
    const res = await apiFetch("/request-access", {
      method: "POST",
      body: JSON.stringify({ email: value })
    });
    const data = await res.json();
    if (res.ok) {
      setAuthStatus(data.message, "success");
      tokenInputEl.value.focus();
    } else {
      setAuthStatus(data.detail || "请求失败", "error");
    }
  } catch {
    setAuthStatus("网络错误，请检查连接", "error");
  } finally {
    emailLoading.value = false;
  }
}

async function submitToken() {
  const value = tokenValue.value.trim();
  if (!value) return;
  await pressButton("token");
  signIn(value);
}
</script>

<template>
  <main id="auth-view" class="view" :class="{ active }">
    <div class="auth-card">
      <h1 class="auth-title">TechNews Intelligence</h1>
      <p class="auth-subtitle">科技新闻智能分析助手</p>

      <form id="email-form" class="auth-form" @submit.prevent="requestAccess">
        <div class="input-group">
          <input
            id="email-input"
            v-model="email"
            type="email"
            placeholder="输入邮箱获取访问权限"
            required
            autocomplete="email"
          />
        </div>
        <button
          id="email-btn"
          type="submit"
          class="secondary-btn"
          :class="{ 'btn-pressing': pressedButton === 'email', 'btn-loading': emailLoading }"
          :disabled="emailLoading"
        >
          <span class="btn-label">获取访问权限</span>
        </button>
      </form>

      <p id="email-status" class="status-msg" :class="session.authStatus.type">{{ session.authStatus.text }}</p>

      <div class="auth-divider">
        <span>已有 Token？</span>
      </div>

      <form id="token-form" class="auth-form" @submit.prevent="submitToken">
        <div class="input-group">
          <input id="token-input" ref="tokenInputEl" v-model="tokenValue" type="text" placeholder="粘贴 Token" required />
        </div>
        <button type="submit" class="secondary-btn" :class="{ 'btn-pressing': pressedButton === 'token' }">
          <span class="btn-label">开始对话</span>
        </button>
      </form>
    </div>
  </main>
</template>
