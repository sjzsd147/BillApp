<template>
  <div class="accounts-view" v-loading="loading">
    <div class="accounts-topbar">
      <div class="topbar-info">
        <span class="title">我的账户 ({{ accounts.length }})</span>
        <span class="tip">账户流水与动态余额来自各账单导入计算，支持账户别名与卡号合并。</span>
      </div>
      <el-button type="primary" size="default" @click="openMergeModal">
        合并账户
      </el-button>
    </div>

    <!-- Account Cards Grid -->
    <div class="account-cards-grid">
      <div
        v-for="acc in accounts"
        :key="acc.id"
        class="account-card"
      >
        <div class="card-top">
          <div class="bank-badge">
            <span class="bank-name">{{ acc.bank }}</span>
            <span v-if="acc.tail" class="card-tail">({{ acc.tail }})</span>
          </div>
          <el-tag size="small" :type="getKindTagType(acc.kind)">
            {{ getKindLabel(acc.kind) }}
          </el-tag>
        </div>

        <div class="card-alias-row">
          <span class="alias-text">{{ acc.alias || acc.display_name }}</span>
          <el-button link size="small" :icon="Edit" @click="openRenameDialog(acc)" />
        </div>

        <div class="card-balance-box">
          <div class="balance-label">计算结余</div>
          <AmountText :value="acc.computed_balance_cents" bold size="20px" />
        </div>

        <div class="card-footer-metrics">
          <div class="metric">
            <span class="label">主账户记录:</span>
            <span class="val">{{ acc.transaction_count }} 笔</span>
          </div>
          <div class="metric">
            <span class="label">作为出资方:</span>
            <span class="val">{{ acc.funded_count }} 笔</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Rename Dialog -->
    <el-dialog v-model="showRenameDialog" title="设置账户别名" width="380px">
      <el-form label-position="top">
        <el-form-item label="账户别名">
          <el-input v-model="renameForm.alias" placeholder="例如: 常用信用卡 / 常用薪资卡" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showRenameDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingRename" @click="saveRename">保存</el-button>
      </template>
    </el-dialog>

    <!-- Account Merge Modal -->
    <el-dialog v-model="showMergeModal" title="合并账户" width="460px">
      <div class="merge-dialog-body">
        <p class="merge-tip">
          当因账单导出的卡号末位格式差异产生重复账户时，可通过合并将源账户的所有流水迁移至目标账户，并删除冗余账户。
        </p>

        <el-form label-position="top">
          <el-form-item label="源账户 (将被合并并删除) *">
            <el-select v-model="mergeForm.sourceId" style="width: 100%" placeholder="选择要删除的源账户">
              <el-option
                v-for="a in accounts"
                :key="a.id"
                :value="a.id"
                :label="`${a.bank}${a.tail ? `(${a.tail})` : ''} - ${a.alias || a.display_name}`"
                :disabled="a.id === mergeForm.targetId"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="目标账户 (保留主账户) *">
            <el-select v-model="mergeForm.targetId" style="width: 100%" placeholder="选择保留的目标账户">
              <el-option
                v-for="a in accounts"
                :key="a.id"
                :value="a.id"
                :label="`${a.bank}${a.tail ? `(${a.tail})` : ''} - ${a.alias || a.display_name}`"
                :disabled="a.id === mergeForm.sourceId"
              />
            </el-select>
          </el-form-item>
        </el-form>

        <div class="danger-warning">
          ⚠️ 注意: 合并操作不可撤销，迁移完成后源账户将被彻底删除。
        </div>
      </div>
      <template #footer>
        <el-button @click="showMergeModal = false">取消</el-button>
        <el-button
          type="danger"
          :loading="merging"
          :disabled="!mergeForm.sourceId || !mergeForm.targetId"
          @click="executeMerge"
        >
          确认合并
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue';
import { ElMessage } from 'element-plus';
import { Edit } from '@element-plus/icons-vue';
import { getAccounts, patchAccount, mergeAccounts } from '@/api/accounts';
import { useAppStore } from '@/stores/app';
import AmountText from '@/components/AmountText.vue';
import type { AccountOut, AccountKind } from '@/types/api';

const appStore = useAppStore();

const accounts = ref<AccountOut[]>([]);
const loading = ref(false);

// Rename Dialog
const showRenameDialog = ref(false);
const activeRenameAccount = ref<AccountOut | null>(null);
const renameForm = reactive({ alias: '' });
const savingRename = ref(false);

// Merge Modal
const showMergeModal = ref(false);
const mergeForm = reactive({
  sourceId: null as number | null,
  targetId: null as number | null,
});
const merging = ref(false);

function getKindLabel(kind: AccountKind): string {
  switch (kind) {
    case 'bank_debit':
      return '储蓄卡';
    case 'bank_credit':
      return '信用卡';
    case 'wallet':
      return '第三方钱包';
    default:
      return '其他';
  }
}

function getKindTagType(kind: AccountKind): '' | 'success' | 'warning' | 'info' | 'danger' {
  switch (kind) {
    case 'bank_credit':
      return 'warning';
    case 'wallet':
      return 'success';
    default:
      return 'info';
  }
}

async function loadAccounts() {
  loading.value = true;
  try {
    accounts.value = await getAccounts();
  } catch (e) {
    console.error('Failed to load accounts', e);
  } finally {
    loading.value = false;
  }
}

function openRenameDialog(acc: AccountOut) {
  activeRenameAccount.value = acc;
  renameForm.alias = acc.alias || '';
  showRenameDialog.value = true;
}

async function saveRename() {
  if (!activeRenameAccount.value) return;
  savingRename.value = true;
  try {
    await patchAccount(activeRenameAccount.value.id, {
      alias: renameForm.alias.trim() || undefined,
    });
    ElMessage.success('账户别名已更新');
    showRenameDialog.value = false;
    await loadAccounts();
    await appStore.fetchMeta();
  } catch (err: any) {
    ElMessage.error(err.message || '更新失败');
  } finally {
    savingRename.value = false;
  }
}

function openMergeModal() {
  mergeForm.sourceId = null;
  mergeForm.targetId = null;
  showMergeModal.value = true;
}

async function executeMerge() {
  if (!mergeForm.sourceId || !mergeForm.targetId) return;
  merging.value = true;
  try {
    const res = await mergeAccounts({
      source_id: mergeForm.sourceId,
      target_id: mergeForm.targetId,
    });
    ElMessage.success(`账户合并成功，已迁移 ${res.migrated_count} 条流水`);
    showMergeModal.value = false;
    await loadAccounts();
    await appStore.fetchMeta();
  } catch (err: any) {
    ElMessage.error(err.message || '合并失败');
  } finally {
    merging.value = false;
  }
}

onMounted(() => {
  loadAccounts();
});
</script>

<style scoped>
.accounts-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.accounts-topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px 20px;
}

.topbar-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.topbar-info .title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}

.topbar-info .tip {
  font-size: 12.5px;
  color: var(--text-3);
}

.account-cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}

.account-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  transition: transform 0.15s, box-shadow 0.15s;
}

.account-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-sm);
}

.card-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.bank-badge {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.card-tail {
  color: var(--text-3);
  font-weight: 400;
}

.card-alias-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.alias-text {
  font-size: 13.5px;
  color: var(--text-2);
}

.card-balance-box {
  background: var(--surface-alt);
  border: 1px solid var(--border-2);
  border-radius: var(--radius-md);
  padding: 10px 14px;
}

.balance-label {
  font-size: 11.5px;
  color: var(--text-3);
  margin-bottom: 2px;
}

.card-footer-metrics {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-3);
  border-top: 1px solid var(--border-2);
  padding-top: 10px;
}

.metric .val {
  font-weight: 500;
  color: var(--text-2);
  margin-left: 4px;
}

.merge-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.merge-tip {
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.5;
}

.danger-warning {
  font-size: 12px;
  color: var(--danger);
  background: var(--danger-soft);
  padding: 8px 12px;
  border-radius: var(--radius-sm);
}
</style>
