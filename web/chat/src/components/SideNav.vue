<script setup>
defineProps({
  threads: { type: Array, required: true },
  currentId: { type: String, default: "" },
  collapsed: Boolean,
  busy: Boolean,
  newChatDisabled: Boolean,
  followOpen: Boolean,
  toggleLabel: { type: String, required: true },
  quotaText: { type: String, required: true },
  quotaLow: Boolean,
  isDark: Boolean,
  themeLabel: { type: String, required: true }
});
defineEmits(["toggle", "new-chat", "follow", "open", "delete", "theme", "logout"]);
</script>

<template>
  <nav class="side-nav" :class="{ collapsed }" aria-label="侧边栏">
    <button v-tip="toggleLabel" type="button" class="icon-btn side-nav-toggle" @click="$emit('toggle')">
      <span class="material-symbols-outlined toggle-idle">dock_to_right</span>
      <span class="material-symbols-outlined toggle-action">{{ collapsed ? "left_panel_open" : "left_panel_close" }}</span>
    </button>

    <button
      v-tip="collapsed ? '新对话' : ''"
      type="button"
      class="nav-row"
      :disabled="newChatDisabled"
      @click="$emit('new-chat')"
    >
      <span class="material-symbols-outlined">edit_square</span>
      <span class="nav-label">新对话</span>
    </button>
    <button
      v-tip="collapsed ? '关注列表' : ''"
      type="button"
      class="nav-row"
      aria-controls="follow-panel"
      aria-haspopup="dialog"
      :aria-expanded="String(followOpen)"
      @click="$emit('follow', $event.currentTarget)"
    >
      <span class="material-symbols-outlined">bookmark</span>
      <span class="nav-label">关注列表</span>
    </button>

    <div class="nav-recent">
      <template v-if="threads.length">
        <h2 class="nav-heading">最近</h2>
        <ul class="thread-list scroll-fade">
          <li
            v-for="thread in threads"
            :key="thread.thread_id"
            class="thread-item"
            :class="{ selected: thread.thread_id === currentId }"
          >
            <button
              type="button"
              class="thread-open"
              :disabled="busy"
              :aria-current="thread.thread_id === currentId ? 'true' : undefined"
              @click="$emit('open', thread.thread_id)"
            >{{ thread.subject }}</button>
            <button v-tip="'删除'" type="button" class="icon-btn thread-delete" :disabled="busy" @click="$emit('delete', thread)">
              <span class="material-symbols-outlined">delete</span>
            </button>
          </li>
        </ul>
      </template>
    </div>

    <p class="nav-quota" :class="{ low: quotaLow }">{{ quotaText }}</p>
    <button
      v-tip="collapsed ? themeLabel : ''"
      type="button"
      class="nav-row"
      :aria-pressed="String(isDark)"
      @click="$emit('theme')"
    >
      <span class="material-symbols-outlined">{{ isDark ? "light_mode" : "dark_mode" }}</span>
      <span class="nav-label">{{ themeLabel }}</span>
    </button>
    <button v-tip="collapsed ? '退出登录' : ''" type="button" class="nav-row" @click="$emit('logout')">
      <span class="material-symbols-outlined">logout</span>
      <span class="nav-label">退出登录</span>
    </button>
  </nav>
</template>
