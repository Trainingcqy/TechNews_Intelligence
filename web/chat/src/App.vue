<script setup>
import { computed, ref, watch } from "vue";
import AuthView from "./components/AuthView.vue";
import ChatView from "./components/ChatView.vue";
import { session } from "./session";
import { useTheme } from "./theme";

const { isDark, toggleTheme } = useTheme();
const themeLabel = computed(() => (isDark.value ? "切换浅色模式" : "切换深色模式"));

const authActive = ref(true);
const chatActive = ref(false);

function showAuth() {
  document.body.classList.remove("chat-active");
  chatActive.value = false;
  requestAnimationFrame(() => {
    authActive.value = true;
  });
}

function showChat() {
  document.body.classList.add("chat-active");
  authActive.value = false;
  requestAnimationFrame(() => {
    chatActive.value = true;
  });
}

watch(
  () => Boolean(session.token),
  (signedIn) => (signedIn ? showChat() : showAuth())
);

if (session.token) showChat();
</script>

<template>
  <button
    v-if="authActive"
    id="theme-toggle"
    class="theme-toggle"
    type="button"
    :aria-label="themeLabel"
    v-tip="themeLabel"
    :aria-pressed="String(isDark)"
    @click="toggleTheme"
  >
    <span class="material-symbols-outlined">{{ isDark ? "light_mode" : "dark_mode" }}</span>
  </button>

  <AuthView :active="authActive" />
  <ChatView :active="chatActive" :is-dark="isDark" :theme-label="themeLabel" @toggle-theme="toggleTheme" />
</template>
