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
  <nav class="topbar">
    <a class="logo-area" href="https://trainingcqy.com/">
      Trainingcqy
    </a>
    <a class="nav-item" href="https://trainingcqy.com/">
      首页
    </a>
    <a class="nav-item" href="https://trainingcqy.com/projects.html">
      项目
    </a>
    <a class="nav-item" href="https://trainingcqy.com/gallery.html">
      摄影
    </a>
  </nav>

  <button
    id="theme-toggle"
    class="theme-toggle"
    type="button"
    :aria-label="themeLabel"
    :title="themeLabel"
    :aria-pressed="String(isDark)"
    @click="toggleTheme"
  >
    <span class="material-symbols-outlined">{{ isDark ? "light_mode" : "dark_mode" }}</span>
  </button>

  <nav class="bottom-nav">
    <a class="bottom-nav-item" href="https://trainingcqy.com/">
      <div class="icon-container">
        <span class="material-symbols-outlined">home</span>
      </div>首页
    </a>
    <a class="bottom-nav-item" href="https://trainingcqy.com/projects.html">
      <div class="icon-container">
        <span class="material-symbols-outlined">folder_open</span>
      </div>项目
    </a>
    <a class="bottom-nav-item" href="https://trainingcqy.com/gallery.html">
      <div class="icon-container">
        <span class="material-symbols-outlined">photo_library</span>
      </div>摄影
    </a>
  </nav>

  <AuthView :active="authActive" />
  <ChatView :active="chatActive" />
</template>
