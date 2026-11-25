<template>
  <div class="review-view" v-loading="reviewStore.loading">
    <!-- Empty State: All Reviewed -->
    <div v-if="!reviewStore.loading && reviewStore.items.length === 0" class="review-empty-box">
      <el-icon class="done-icon"><CircleCheckFilled /></el-icon>
      <div class="done-title">全部待确认账单已处理完毕</div>
      <div class="done-desc">所有跨渠道重复流水与账户间转账均已完成核对与归并。</div>
      <router-link to="/dashboard">
        <el-button type="primary">返回看板</el-button>
      </router-link>
    </div>

    <!-- Active Review Layout -->
    <div v-else class="review-container">
      <!-- Left: Candidate Queue List -->
      <aside class="queue-sidebar">
        <div class="queue-topbar">
          <span class="queue-title">待确认候选 ({{ reviewStore.items.length }})</span>
          <el-button
            v-if="reviewStore.highConfidenceItems.length > 0"
            type="primary"
            link
            size="small"
            @click="handleBulkAccept"
          >
            全部采纳 ({{ reviewStore.highConfidenceItems.length }})
          </el-button>
        </div>

        <div class="queue-list">
          <div
            v-for="(item, idx) in reviewStore.items"
            :key="item.id"
            class="queue-item"
            :class="{ 'is-active': idx === reviewStore.currentIndex }"
            @click="reviewStore.currentIndex = idx"
          >
            <div class="item-header">
              <span class="kind-tag" :class="`kind-${item.kind}`">
                {{ item.kind === 'duplicate' ? '重复' : '转账' }}
              </span>
              <span class="score-pill">{{ (item.score * 100).toFixed(0) }}%</span>
            </div>
            <div class="item-counterparty">
              {{ item.left.counterparty || item.right.counterparty || '未知商户' }}
            </div>
            <div class="item-footer">
              <AmountText
                :value="item.left.amount_cents"
                :currency="item.left.currency || 'CNY'"
                :amount-cny="item.left.amount_cny"
                size="13px"
                bold
              />
              <span class="item-date">{{ item.left.txn_time.substring(5, 16) }}</span>
            </div>
          </div>
        </div>
      </aside>

      <!-- Right: Comparison View & Actions -->
      <main class="compare-main" v-if="reviewStore.currentItem">
        <div class="main-header">
          <div class="header-left">
            <span class="title">核对流水候选</span>
            <span class="shortcut-tip">提示: 使用键盘快捷键 Y (确认) / N (排除) / U (稍后)</span>
          </div>
          <div class="nav-controls">
            <el-button size="small" :disabled="reviewStore.currentIndex === 0" @click="reviewStore.prev">
              上一个 (K)
            </el-button>
            <el-button
              size="small"
              :disabled="reviewStore.currentIndex >= reviewStore.items.length - 1"
              @click="reviewStore.next"
            >
              下一个 (J)
            </el-button>
          </div>
        </div>

        <MatchCompare :item="reviewStore.currentItem" />

        <!-- Decision Buttons -->
        <div class="decision-bar">
          <el-button
            size="large"
            @click="handleDefer"
          >
            稍后处理 (U)
          </el-button>

          <el-button
            type="danger"
            plain
            size="large"
            :loading="actionLoading"
            @click="handleReject"
          >
            不是同一笔 (N)
          </el-button>

          <el-button
            type="primary"
            size="large"
            :loading="actionLoading"
            @click="handleAccept"
          >
            确认合并 (Y)
          </el-button>
        </div>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { CircleCheckFilled } from '@element-plus/icons-vue';
import { useReviewStore } from '@/stores/review';
import MatchCompare from '@/components/MatchCompare.vue';
import AmountText from '@/components/AmountText.vue';

const reviewStore = useReviewStore();
const actionLoading = ref(false);

async function handleAccept() {
  if (!reviewStore.currentItem || actionLoading.value) return;
  actionLoading.value = true;
  try {
    const id = reviewStore.currentItem.id;
    await reviewStore.accept(id);
    ElMessage.success('已确认合并流水');
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '操作失败');
    }
  } finally {
    actionLoading.value = false;
  }
}

async function handleReject() {
  if (!reviewStore.currentItem || actionLoading.value) return;
  actionLoading.value = true;
  try {
    const id = reviewStore.currentItem.id;
    await reviewStore.reject(id);
    ElMessage.info('已排除该候选关联并记入黑名单');
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '操作失败');
    }
  } finally {
    actionLoading.value = false;
  }
}

function handleDefer() {
  const moved = reviewStore.deferCurrent();
  if (!moved) {
    ElMessage.info('当前队列仅剩一条候选记录');
    return;
  }
  ElMessage.info('已将该候选延后处理');
}

async function handleBulkAccept() {
  try {
    await ElMessageBox.confirm(
      `确定要一键接受全部 ${reviewStore.highConfidenceItems.length} 笔高置信度 (≥85%) 匹配流水吗？`,
      '批量采纳确认',
      {
        confirmButtonText: '确定采纳',
        cancelButtonText: '取消',
        type: 'info',
      }
    );
    actionLoading.value = true;
    await reviewStore.acceptAllHighConfidence();
    ElMessage.success('批量采纳成功');
  } catch {
    // Cancelled
  } finally {
    actionLoading.value = false;
  }
}

// Single keystroke listener (Y / N / U / J / K)
function handleKeydown(e: KeyboardEvent) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (actionLoading.value) return;
  const hasVisibleModal = Array.from(document.querySelectorAll('.el-message-box, .el-dialog')).some(
    (el) => el.getClientRects().length > 0
  );
  if (hasVisibleModal) return;
  const target = e.target as HTMLElement | null;
  const isInput = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable);
  if (isInput) return;

  const key = e.key.toLowerCase();
  if (key === 'y') {
    e.preventDefault();
    handleAccept();
  } else if (key === 'n') {
    e.preventDefault();
    handleReject();
  } else if (key === 'u') {
    e.preventDefault();
    handleDefer();
  } else if (key === 'j') {
    e.preventDefault();
    reviewStore.next();
  } else if (key === 'k') {
    e.preventDefault();
    reviewStore.prev();
  }
}

onMounted(() => {
  reviewStore.fetchList();
  window.addEventListener('keydown', handleKeydown);
});

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown);
});
</script>

<style scoped>
.review-view {
  height: calc(100vh - var(--topbar-height) - 40px);
}

.review-empty-box {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 60px 24px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  gap: 12px;
}

.done-icon {
  font-size: 56px;
  color: var(--success);
}

.done-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text);
}

.done-desc {
  font-size: 13px;
  color: var(--text-3);
  margin-bottom: 8px;
}

.review-container {
  display: grid;
  grid-template-columns: 292px 1fr;
  gap: 16px;
  height: 100%;
}

/* Left Queue */
.queue-sidebar {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.queue-topbar {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-2);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.queue-title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
}

.queue-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.queue-item {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-2);
  background: var(--surface);
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.queue-item:hover {
  background: var(--surface-hover);
}

.queue-item.is-active {
  border-color: var(--primary);
  background: var(--primary-soft-2);
}

.item-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.kind-tag {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
}

.kind-duplicate {
  background: #FFF3E0;
  color: #B45309;
}

.kind-transfer {
  background: #EFF6FF;
  color: #1D4ED8;
}

.score-pill {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-2);
}

.item-counterparty {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.item-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.item-date {
  font-size: 11px;
  color: var(--text-3);
}

/* Right Main */
.compare-main {
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow-y: auto;
}

.main-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 12px 18px;
}

.main-header .title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  margin-right: 12px;
}

.shortcut-tip {
  font-size: 12px;
  color: var(--text-3);
}

.decision-bar {
  display: flex;
  justify-content: flex-end;
  gap: 16px;
  padding: 16px 0;
}
</style>
