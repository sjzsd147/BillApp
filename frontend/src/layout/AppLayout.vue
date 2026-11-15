<template>
  <div class="app-layout" :class="{ 'is-collapsed': isCollapsed }">
    <!-- Sidebar -->
    <aside class="sidebar">
      <div class="brand">
        <div class="logo">账</div>
        <div class="brand-text" v-if="!isCollapsed">
          <div class="app-title">账单助手</div>
          <div class="app-subtitle">Bill App</div>
        </div>
      </div>

      <nav class="nav-groups">
        <!-- Group 1: Analysis -->
        <div class="nav-group-label" v-if="!isCollapsed">分析</div>
        <router-link to="/dashboard" class="nav-item" active-class="is-active">
          <el-icon><DataLine /></el-icon>
          <span class="nav-label" v-if="!isCollapsed">看板</span>
        </router-link>
        <router-link to="/transactions" class="nav-item" active-class="is-active">
          <el-icon><Tickets /></el-icon>
          <span class="nav-label" v-if="!isCollapsed">明细</span>
        </router-link>

        <div class="divider" />

        <!-- Group 2: Data -->
        <div class="nav-group-label" v-if="!isCollapsed">数据</div>
        <router-link to="/import" class="nav-item" active-class="is-active">
          <el-icon><Upload /></el-icon>
          <span class="nav-label" v-if="!isCollapsed">导入</span>
        </router-link>
        <router-link to="/review" class="nav-item" active-class="is-active">
          <el-icon><Select /></el-icon>
          <span class="nav-label" v-if="!isCollapsed">待确认</span>
          <span
            v-if="appStore.pendingReviewCount > 0"
            class="badge"
            :class="{ 'badge-dot': isCollapsed }"
          >
            {{ isCollapsed ? '' : (appStore.pendingReviewCount > 99 ? '99+' : appStore.pendingReviewCount) }}
          </span>
        </router-link>

        <div class="divider" />

        <!-- Group 3: Configuration -->
        <div class="nav-group-label" v-if="!isCollapsed">配置</div>
        <router-link to="/rules" class="nav-item" active-class="is-active">
          <el-icon><CollectionTag /></el-icon>
          <span class="nav-label" v-if="!isCollapsed">分类与规则</span>
        </router-link>
        <router-link to="/accounts" class="nav-item" active-class="is-active">
          <el-icon><CreditCard /></el-icon>
          <span class="nav-label" v-if="!isCollapsed">账户</span>
        </router-link>
        <router-link to="/settings" class="nav-item" active-class="is-active">
          <el-icon><Setting /></el-icon>
          <span class="nav-label" v-if="!isCollapsed">设置</span>
        </router-link>
      </nav>

      <div class="sidebar-footer">
        <div class="privacy-note" v-if="!isCollapsed">
          <span class="dot-green" />
          <span>数据仅保存在本机</span>
        </div>
        <div class="version" v-if="!isCollapsed">v{{ appStore.version }}</div>
      </div>
    </aside>

    <!-- Main Container -->
    <div class="main-container">
      <!-- Topbar -->
      <header class="topbar">
        <div class="topbar-left">
          <el-button
            class="collapse-btn"
            text
            size="small"
            @click="isCollapsed = !isCollapsed"
          >
            <el-icon><Expand v-if="isCollapsed" /><Fold v-else /></el-icon>
          </el-button>
          <div class="breadcrumbs">
            <span class="crumb-root">账单助手</span>
            <span class="crumb-separator">/</span>
            <span class="crumb-current">{{ currentRouteTitle }}</span>
          </div>
        </div>

        <div class="topbar-center">
          <div class="search-box" @click="focusSearch">
            <el-icon class="search-icon"><Search /></el-icon>
            <input
              ref="searchInputRef"
              v-model="appStore.globalSearchQuery"
              class="search-input"
              placeholder="搜索对方、说明、订单号…"
              @keydown.enter="handleSearchSubmit"
            />
            <kbd class="shortcut-kbd">Ctrl K</kbd>
          </div>
        </div>

        <div class="topbar-right">
          <!-- LLM Status Pill -->
          <div class="status-pill" :class="llmStatusClass" @click="jumpToLLMSettings">
            <span class="status-dot" />
            <span class="status-label">{{ llmStatusText }}</span>
          </div>

          <!-- Shortcuts Help Button -->
          <el-button circle size="small" @click="showHelpModal = true">
            <el-icon><QuestionFilled /></el-icon>
          </el-button>
        </div>
      </header>

      <!-- Viewport Content -->
      <main class="page-content">
        <router-view />
      </main>
    </div>

    <!-- Shortcuts Help Modal -->
    <el-dialog v-model="showHelpModal" title="快捷键帮助" width="460px">
      <div class="shortcuts-grid">
        <div class="shortcut-item">
          <span class="action">全局搜索</span>
          <kbd>Ctrl + K</kbd>
        </div>
        <div class="shortcut-item">
          <span class="action">看板 / 明细</span>
          <kbd>Ctrl + 1 / 4</kbd>
        </div>
        <div class="shortcut-item">
          <span class="action">导入 / 待确认</span>
          <kbd>Ctrl + 2 / 3</kbd>
        </div>
        <div class="shortcut-item">
          <span class="action">分类 / 账户 / 设置</span>
          <kbd>Ctrl + 5 / 6 / 7</kbd>
        </div>
        <div class="shortcut-item">
          <span class="action">待确认: 确认 / 排除 / 稍后</span>
          <kbd>Y / N / U</kbd>
        </div>
        <div class="shortcut-item">
          <span class="action">待确认: 上一个 / 下一个</span>
          <kbd>K / J</kbd>
        </div>
        <div class="shortcut-item">
          <span class="action">关闭弹窗 / 取消</span>
          <kbd>Esc</kbd>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAppStore } from '@/stores/app';
import { setupGlobalShortcuts } from '@/utils/shortcuts';
import {
  DataLine,
  Tickets,
  Upload,
  Select,
  CollectionTag,
  CreditCard,
  Setting,
  Search,
  QuestionFilled,
  Fold,
  Expand,
} from '@element-plus/icons-vue';

const route = useRoute();
const router = useRouter();
const appStore = useAppStore();

const isCollapsed = ref(false);
const showHelpModal = ref(false);
const searchInputRef = ref<HTMLInputElement | null>(null);

const currentRouteTitle = computed(() => {
  return (route.meta?.title as string) || '';
});

const llmStatusText = computed(() => {
  if (!appStore.llmStatus || !appStore.llmStatus.configured) {
    return '未配置 AI';
  }
  if (!appStore.llmStatus.enabled) {
    return 'AI 已停用';
  }
  return `AI: ${appStore.llmStatus.model || '在线'}`;
});

const llmStatusClass = computed(() => {
  if (!appStore.llmStatus || !appStore.llmStatus.configured) return 'status-offline';
  if (!appStore.llmStatus.enabled) return 'status-disabled';
  return 'status-online';
});

function focusSearch() {
  searchInputRef.value?.focus();
}

function handleSearchSubmit() {
  if (appStore.globalSearchQuery.trim()) {
    router.push({
      path: '/transactions',
      query: { q: appStore.globalSearchQuery.trim() },
    });
  }
}

function jumpToLLMSettings() {
  router.push('/settings/llm');
}

setupGlobalShortcuts(router, {
  onSearch: focusSearch,
  onHelp: () => {
    showHelpModal.value = !showHelpModal.value;
  },
  onEscape: () => {
    showHelpModal.value = false;
  },
});

onMounted(() => {
  appStore.fetchMeta();
  appStore.fetchLLMStatus();

  // Responsive sidebar collapse on narrow desktop windows
  if (window.innerWidth < 1180) {
    isCollapsed.value = true;
  }
});
</script>

<style scoped>
.app-layout {
  display: flex;
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  background-color: var(--bg);
}

/* Sidebar */
.sidebar {
  width: var(--sidebar-width);
  height: 100%;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  transition: width 0.2s ease;
  user-select: none;
  z-index: 20;
}

.is-collapsed .sidebar {
  width: var(--sidebar-collapsed-width);
}

.brand {
  height: var(--topbar-height);
  display: flex;
  align-items: center;
  padding: 0 16px;
  gap: 10px;
  border-bottom: 1px solid var(--border-2);
}

.logo {
  width: 32px;
  height: 32px;
  background: linear-gradient(135deg, var(--primary) 0%, #5B8DEF 100%);
  color: #fff;
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 16px;
  flex-shrink: 0;
}

.brand-text {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.app-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.2;
}

.app-subtitle {
  font-size: 10px;
  color: var(--text-3);
  letter-spacing: 0.5px;
}

.nav-groups {
  flex: 1;
  overflow-y: auto;
  padding: 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.nav-group-label {
  font-size: 11px;
  color: var(--text-3);
  font-weight: 600;
  padding: 8px 10px 4px;
  text-transform: uppercase;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--radius-md);
  color: var(--text-2);
  font-size: 13.5px;
  font-weight: 500;
  transition: all 0.15s ease;
  text-decoration: none;
  position: relative;
}

.nav-item:hover {
  background-color: var(--surface-hover);
  color: var(--text);
}

.nav-item.is-active {
  background-color: var(--primary-soft);
  color: var(--primary);
  font-weight: 600;
}

.nav-item .badge {
  margin-left: auto;
  background: #FFF3E0;
  color: #B45309;
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 10px;
}

.nav-item .badge-dot {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 8px;
  height: 8px;
  padding: 0;
  border-radius: 50%;
  background: #B45309;
}

.divider {
  height: 1px;
  background: var(--border-2);
  margin: 8px 4px;
}

.sidebar-footer {
  padding: 12px 16px;
  border-top: 1px solid var(--border-2);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.privacy-note {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-3);
}

.dot-green {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: var(--success);
}

.version {
  font-size: 11px;
  color: var(--text-3);
}

/* Main Container */
.main-container {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

/* Topbar */
.topbar {
  height: var(--topbar-height);
  background: rgba(255, 255, 255, 0.9);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  z-index: 10;
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.collapse-btn {
  font-size: 16px;
  color: var(--text-2);
}

.breadcrumbs {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.crumb-root {
  color: var(--text-3);
}

.crumb-separator {
  color: var(--border-strong);
}

.crumb-current {
  font-weight: 500;
  color: var(--text);
}

.topbar-center {
  flex: 1;
  max-width: 360px;
  margin: 0 20px;
}

.search-box {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 5px 10px;
  cursor: text;
  transition: border-color 0.2s;
}

.search-box:focus-within {
  border-color: var(--primary);
  background: var(--surface);
}

.search-icon {
  color: var(--text-3);
  font-size: 14px;
}

.search-input {
  flex: 1;
  border: none;
  background: transparent;
  outline: none;
  font-size: 12.5px;
  color: var(--text);
}

.search-input::placeholder {
  color: var(--text-3);
}

.shortcut-kbd {
  font-size: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 2px 4px;
  color: var(--text-3);
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.status-pill {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 12px;
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid transparent;
}

.status-pill .status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.status-online {
  background: var(--success-soft);
  color: var(--success);
  border-color: rgba(21, 128, 61, 0.2);
}
.status-online .status-dot {
  background: var(--success);
}

.status-offline {
  background: var(--surface-alt);
  color: var(--text-3);
  border-color: var(--border);
}
.status-offline .status-dot {
  background: var(--neutral);
}

.status-disabled {
  background: var(--warning-soft);
  color: var(--warning);
  border-color: rgba(180, 83, 9, 0.2);
}
.status-disabled .status-dot {
  background: var(--warning);
}

/* Page Content Viewport */
.page-content {
  flex: 1;
  overflow: auto;
  padding: 20px;
}

/* Shortcuts Dialog */
.shortcuts-grid {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.shortcut-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  padding: 4px 0;
  border-bottom: 1px solid var(--border-2);
}

.shortcut-item .action {
  color: var(--text-2);
}

.shortcut-item kbd {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 2px 6px;
  font-size: 11.5px;
  color: var(--text);
  font-family: inherit;
}
</style>
