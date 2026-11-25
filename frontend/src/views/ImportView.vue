<template>
  <div class="import-view">
    <el-tabs v-model="activeTab" class="import-tabs">
      <!-- Tab 1: Current Ingestion -->
      <el-tab-pane label="本次导入" name="upload">
        <div class="upload-section">
          <!-- Drag-and-drop area -->
          <div
            class="drop-zone"
            :class="{ 'is-compact': importStore.queue.length > 0, 'is-dragging': isDragging }"
            @dragover.prevent="isDragging = true"
            @dragleave.prevent="isDragging = false"
            @drop.prevent="handleDrop"
            @click="triggerFileInput"
          >
            <input
              ref="fileInputRef"
              type="file"
              multiple
              accept=".csv,.xlsx,.xls,.pdf,.zip"
              style="display: none"
              @change="handleFileSelect"
            />
            <div class="drop-content">
              <el-icon class="upload-icon" :size="importStore.queue.length > 0 ? 24 : 44">
                <UploadFilled />
              </el-icon>
              <div class="drop-text">
                <span class="primary-text">把账单文件拖到这里，或点击选择文件</span>
                <span class="sub-text" v-if="importStore.queue.length === 0">
                  支持 微信 / 支付宝 / 工商银行 / 建设银行 / 招商银行 / 中国银行 等 CSV / XLSX / PDF / ZIP 账单
                </span>
              </div>
            </div>
          </div>

          <!-- File Queue Table -->
          <div v-if="importStore.queue.length > 0" class="queue-card">
            <div class="queue-header">
              <div class="queue-title">
                待处理文件队列 ({{ importStore.queue.length }})
              </div>
              <div class="queue-actions">
                <el-button size="small" @click="importStore.clearQueue">清空队列</el-button>
                <el-button
                  type="primary"
                  size="small"
                  :disabled="!hasReadyItems"
                  :loading="committing"
                  @click="commitAllReady"
                >
                  全部导入 ({{ readyCount }})
                </el-button>
              </div>
            </div>

            <el-table :data="importStore.queue" size="small" border style="width: 100%">
              <el-table-column prop="name" label="文件名" min-width="200" show-overflow-tooltip />
              <el-table-column label="大小" width="100">
                <template #default="{ row }">
                  {{ (row.size / 1024).toFixed(1) }} KB
                </template>
              </el-table-column>
              <el-table-column label="识别来源" width="130">
                <template #default="{ row }">
                  <span v-if="row.preview?.source" class="source-tag">
                    {{ row.preview.source }}
                  </span>
                  <span v-else class="text-sub">待解析</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="160">
                <template #default="{ row }">
                  <div class="status-cell">
                    <el-tag v-if="row.status === 'ready'" type="success" size="small">就绪</el-tag>
                    <el-tag v-else-if="row.status === 'need_password'" type="warning" size="small">
                      需输入密码
                    </el-tag>
                    <el-tag v-else-if="row.status === 'need_mapping'" type="info" size="small">
                      需配置映射
                    </el-tag>
                    <el-tag v-else-if="row.status === 'parsing'" type="primary" size="small">
                      解析中...
                    </el-tag>
                    <el-tooltip
                      v-else-if="row.status === 'error'"
                      :content="row.errorMsg || '解析失败'"
                      placement="top"
                    >
                      <el-tag type="danger" size="small">解析失败</el-tag>
                    </el-tooltip>
                    <el-tag v-else size="small">等待中</el-tag>
                    <el-tag
                      v-if="row.preview?.existing_batch_id"
                      type="warning"
                      size="small"
                      effect="plain"
                    >
                      重导 #{{ row.preview.existing_batch_id }}
                    </el-tag>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="200" fixed="right">
                <template #default="{ row }">
                  <el-button
                    v-if="row.status === 'ready' && row.preview"
                    type="primary"
                    link
                    size="small"
                    @click="selectPreviewItem(row)"
                  >
                    预览
                  </el-button>
                  <el-button
                    v-if="row.status === 'queued' || row.status === 'error'"
                    type="primary"
                    link
                    size="small"
                    @click="processQueueItem(row)"
                  >
                    解析
                  </el-button>
                  <el-button
                    v-if="row.status === 'need_password'"
                    type="warning"
                    link
                    size="small"
                    @click="openPasswordModal(row)"
                  >
                    输入密码
                  </el-button>
                  <el-button
                    v-if="row.status === 'need_mapping' || row.status === 'error'"
                    type="success"
                    link
                    size="small"
                    :loading="aiParsingId === row.id"
                    @click="handleAiFullParse(row)"
                  >
                    AI 整单识别
                  </el-button>
                  <el-button
                    v-if="row.status === 'need_mapping' || row.status === 'error'"
                    type="primary"
                    link
                    size="small"
                    @click="goToMapping(row)"
                  >
                    AI 生成 / 映射向导
                  </el-button>
                  <el-button
                    type="danger"
                    link
                    size="small"
                    @click="importStore.removeQueueItem(row.id)"
                  >
                    移除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- Parse Preview & Self-Check Panel -->
          <div v-if="importStore.activePreview" class="preview-panel">
            <div class="panel-header">
              <div class="panel-title">
                账单预检与解析预览 ({{ importStore.activePreview.file_name }})
              </div>
              <div class="panel-actions">
                <el-button @click="importStore.activePreview = null">关闭预览</el-button>
                <el-button
                  type="primary"
                  :loading="committing"
                  @click="commitActivePreview"
                >
                  {{ importStore.activePreview.existing_batch_id ? '重新解析并覆盖导入' : '确认导入' }}
                  ({{ importStore.activePreview.total_rows }} 笔)
                </el-button>
              </div>
            </div>

            <!-- Warnings (e.g. duplicate batch re-import, multi-file ZIP notice, AI chunk truncation) -->
            <div v-if="importStore.activePreview.warnings?.length" class="preview-warnings">
              <el-alert
                v-for="(warn, wIdx) in importStore.activePreview.warnings"
                :key="wIdx"
                :title="warn"
                type="warning"
                show-icon
                :closable="false"
              />
            </div>

            <!-- Preview Metric Tiles -->
            <div class="metric-tiles">
              <div class="tile">
                <div class="tile-label">识别交易数</div>
                <div class="tile-val">{{ importStore.activePreview.total_rows }} 笔</div>
              </div>
              <div class="tile">
                <div class="tile-label">涉及账户</div>
                <div class="tile-val">
                  {{
                    accountDraft.bank
                      ? `${accountDraft.bank}${accountDraft.tail ? `(${accountDraft.tail})` : ''}`
                      : importStore.activePreview.accounts.map(a => `${a.bank}${a.tail ? `(${a.tail})` : ''}`).join(', ') || '主账户'
                  }}
                </div>
              </div>
              <div class="tile">
                <div class="tile-label">数据来源</div>
                <div class="tile-val">{{ importStore.activePreview.source }}</div>
              </div>
            </div>

            <!-- Top-of-Statement Account Confirmation -->
            <div class="account-confirm-bar">
              <span class="confirm-label">识别账户确认:</span>
              <el-input
                v-model="accountDraft.bank"
                size="small"
                placeholder="银行/机构 (如 招商银行)"
                style="width: 165px"
              />
              <el-input
                v-model="accountDraft.tail"
                size="small"
                maxlength="4"
                placeholder="尾号4位 (如 1234)"
                style="width: 120px"
              />
              <el-select v-model="accountDraft.kind" size="small" style="width: 115px">
                <el-option label="借记卡" value="bank_debit" />
                <el-option label="信用卡" value="bank_credit" />
                <el-option label="第三方钱包" value="wallet" />
                <el-option label="第三方平台" value="platform" />
              </el-select>
              <el-input
                v-model="accountDraft.holder"
                size="small"
                placeholder="户名 (可选)"
                style="width: 125px"
              />
            </div>

            <!-- Self Check Checklist -->
            <div class="self-check-container" v-if="importStore.activePreview.self_checks?.length">
              <div class="section-title">账目勾稽校验</div>
              <SelfCheckList :items="importStore.activePreview.self_checks" />
            </div>

            <!-- Preview Sample Table -->
            <div class="sample-table-container">
              <div class="section-title">数据抽样预览 (前 5 笔)</div>
              <el-table
                :data="importStore.activePreview.rows.slice(0, 5)"
                size="small"
                border
                style="width: 100%"
              >
                <el-table-column prop="txn_time" label="时间" width="160" />
                <el-table-column label="收支" width="80">
                  <template #default="{ row }">
                    <DirectionTag :direction="row.direction" />
                  </template>
                </el-table-column>
                <el-table-column label="金额" width="110">
                  <template #default="{ row }">
                    <AmountText :value="row.amount_cents" :direction="row.direction" bold />
                  </template>
                </el-table-column>
                <el-table-column prop="counterparty" label="对方/商户" min-width="140" show-overflow-tooltip />
                <el-table-column prop="description" label="商品说明" min-width="140" show-overflow-tooltip />
                <el-table-column prop="channel" label="支付渠道" width="100" />
              </el-table>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- Tab 2: Batch History -->
      <el-tab-pane label="导入历史" name="history">
        <div class="history-section" v-loading="importStore.loadingBatches">
          <div class="history-header">
            <span class="count-note">共 {{ importStore.batches.length }} 个批次</span>
            <el-button size="small" :icon="Refresh" @click="importStore.fetchBatches">刷新</el-button>
          </div>

          <el-table :data="importStore.batches" border style="width: 100%">
            <el-table-column prop="id" label="批次 ID" width="90" />
            <el-table-column prop="file_name" label="导入文件名" min-width="180" show-overflow-tooltip />
            <el-table-column prop="source" label="来源渠道" width="120" />
            <el-table-column prop="row_count" label="交易笔数" width="100" />
            <el-table-column prop="imported_at" label="导入时间" width="170" />
            <el-table-column label="期间" min-width="180">
              <template #default="{ row }">
                {{ row.period_start || '-' }} 至 {{ row.period_end || '-' }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="row.can_rollback"
                  type="danger"
                  link
                  size="small"
                  @click="confirmRollback(row)"
                >
                  撤销批次
                </el-button>
                <span v-else class="text-sub">已归档</span>
              </template>
            </el-table-column>
          </el-table>
          <EmptyState v-if="importStore.batches.length === 0" title="暂无导入历史" />
        </div>
      </el-tab-pane>

      <!-- Tab 3: Bank Profiles -->
      <el-tab-pane label="银行配置" name="profiles">
        <div class="profiles-section" v-loading="loadingProfiles">
          <div class="profiles-header">
            <span>支持的银行与自定义格式</span>
            <el-button type="primary" size="small" @click="goToMappingWizard">
              创建新配置
            </el-button>
          </div>
          <el-table
            :data="profiles"
            border
            style="width: 100%"
            empty-text="暂无自定义银行配置"
          >
            <el-table-column prop="id" label="标识" min-width="110" show-overflow-tooltip />
            <el-table-column prop="name" label="配置名称" min-width="140" show-overflow-tooltip />
            <el-table-column prop="bank" label="银行/机构" min-width="100" show-overflow-tooltip />
            <el-table-column prop="file_type" label="文件类型" width="85" />
            <el-table-column label="类型" width="80">
              <template #default="{ row }">
                <el-tag :type="row.is_builtin ? 'info' : 'success'" size="small">
                  {{ row.is_builtin ? '内置' : '自定义' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="80">
              <template #default="{ row }">
                <el-button
                  v-if="!row.is_builtin"
                  link
                  type="danger"
                  size="small"
                  @click="confirmDeleteProfile(row)"
                >
                  删除
                </el-button>
                <span v-else class="count-note">—</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- Ephemeral In-Memory Password Modal (Zero Disk Persistence) -->
    <el-dialog
      v-model="showPasswordModal"
      title="输入账单解密密码"
      width="440px"
      :close-on-click-modal="false"
      @closed="ephemeralPassword = ''"
    >
      <div class="password-dialog-content">
        <p class="pwd-tip">
          文件 <strong>{{ currentPasswordItem?.name }}</strong> 已受密码保护（通常为身份证后 6 位或银行指定密码）。
        </p>
        <el-input
          v-model="ephemeralPassword"
          type="password"
          placeholder="请输入账单解密密码"
          show-password
          size="large"
          @keydown.enter="submitPassword"
        />
        <div class="pwd-security-note">
          <el-icon><InfoFilled /></el-icon>
          <span>此密码仅在内存中临时使用，不会保存至本地或云端。</span>
        </div>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="showPasswordModal = false">取消</el-button>
          <el-button
            type="primary"
            :loading="verifyingPassword"
            :disabled="!ephemeralPassword"
            @click="submitPassword"
          >
            解锁并解析
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { UploadFilled, Refresh, InfoFilled } from '@element-plus/icons-vue';
import { useImportStore, type FileQueueItem } from '@/stores/import';
import { useAppStore } from '@/stores/app';
import { getProfiles, deleteProfile } from '@/api/profiles';
import DirectionTag from '@/components/DirectionTag.vue';
import AmountText from '@/components/AmountText.vue';
import SelfCheckList from '@/components/SelfCheckList.vue';
import EmptyState from '@/components/EmptyState.vue';
import type { BatchItem, ProfileSummary } from '@/types/api';

const router = useRouter();
const importStore = useImportStore();
const appStore = useAppStore();

const activeTab = ref('upload');
const isDragging = ref(false);
const fileInputRef = ref<HTMLInputElement | null>(null);
const committing = ref(false);
const aiParsingId = ref<string | null>(null);

const accountDraft = reactive({
  bank: '',
  tail: '',
  kind: 'bank_debit',
  holder: '',
});

const accountDraftBaseline = reactive({
  bank: '',
  tail: '',
  kind: 'bank_debit',
  holder: '',
});

function buildModifiedAccountOverrides(): Record<string, any> | undefined {
  const overrides: Record<string, any> = {};
  if (accountDraft.bank.trim() !== accountDraftBaseline.bank) {
    if (accountDraft.bank.trim()) overrides.bank = accountDraft.bank.trim();
  }
  if (accountDraft.tail.trim() !== accountDraftBaseline.tail) {
    overrides.tail = accountDraft.tail.trim() || null;
  }
  if (accountDraft.kind !== accountDraftBaseline.kind) {
    overrides.kind = accountDraft.kind;
  }
  if (accountDraft.holder.trim() !== accountDraftBaseline.holder) {
    if (accountDraft.holder.trim()) overrides.holder = accountDraft.holder.trim();
  }
  return Object.keys(overrides).length > 0 ? overrides : undefined;
}

watch(
  () => importStore.activePreview,
  (preview) => {
    const first = preview?.accounts?.[0];
    const baseBank = first?.bank || '';
    const baseTail = first?.tail || '';
    const baseKind = first?.kind || 'bank_debit';
    const baseHolder = first?.holder || '';
    accountDraftBaseline.bank = baseBank;
    accountDraftBaseline.tail = baseTail;
    accountDraftBaseline.kind = baseKind;
    accountDraftBaseline.holder = baseHolder;

    const queueItem = preview
      ? importStore.queue.find((i) => i.preview?.token === preview.token)
      : undefined;
    const savedOv = queueItem?.accountOverrides;
    accountDraft.bank = savedOv?.bank ?? baseBank;
    accountDraft.tail = savedOv && 'tail' in savedOv ? (savedOv.tail || '') : baseTail;
    accountDraft.kind = savedOv?.kind ?? baseKind;
    accountDraft.holder = savedOv?.holder ?? baseHolder;
  },
  { immediate: true }
);

watch(
  accountDraft,
  () => {
    const activeToken = importStore.activePreview?.token;
    if (!activeToken) return;
    const queueItem = importStore.queue.find((i) => i.preview?.token === activeToken);
    if (queueItem) {
      queueItem.accountOverrides = buildModifiedAccountOverrides();
    }
  },
  { deep: true }
);

// Ephemeral PDF/ZIP Password
const showPasswordModal = ref(false);
const ephemeralPassword = ref('');
const verifyingPassword = ref(false);
const currentPasswordItem = ref<FileQueueItem | null>(null);
const pendingPasswordContext = ref<{ profileId?: string; useLlm?: boolean }>({});

// Profiles
const profiles = ref<ProfileSummary[]>([]);
const loadingProfiles = ref(false);

const hasReadyItems = computed(() => {
  return importStore.queue.some((i) => i.status === 'ready');
});

const readyCount = computed(() => {
  return importStore.queue.filter((i) => i.status === 'ready').length;
});

function selectPreviewItem(item: FileQueueItem) {
  if (item.preview) {
    importStore.activePreview = item.preview;
  }
}

function triggerFileInput() {
  fileInputRef.value?.click();
}

function handleFileSelect(e: Event) {
  const target = e.target as HTMLInputElement;
  if (target.files && target.files.length > 0) {
    importStore.addFiles(target.files);
    target.value = '';
    processAllQueued();
  }
}

function handleDrop(e: DragEvent) {
  isDragging.value = false;
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
    importStore.addFiles(e.dataTransfer.files);
    processAllQueued();
  }
}

async function processAllQueued() {
  for (const item of importStore.queue) {
    if (item.status === 'queued') {
      try {
        await importStore.parseFile(item);
      } catch {
        // Error recorded in item.status and errorMsg
      }
    }
  }
}

async function processQueueItem(item: FileQueueItem) {
  try {
    await importStore.parseFile(item);
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '解析失败');
    }
  }
}

function openPasswordModal(item: FileQueueItem, options?: { profileId?: string; useLlm?: boolean }) {
  currentPasswordItem.value = item;
  pendingPasswordContext.value = {
    profileId: options?.profileId ?? item.profileId,
    useLlm: options?.useLlm,
  };
  ephemeralPassword.value = '';
  showPasswordModal.value = true;
}

async function submitPassword() {
  if (!currentPasswordItem.value || !ephemeralPassword.value) return;
  verifyingPassword.value = true;
  try {
    const preview = await importStore.parseFile(
      currentPasswordItem.value,
      ephemeralPassword.value,
      pendingPasswordContext.value.profileId,
      pendingPasswordContext.value.useLlm
    );
    showPasswordModal.value = false;
    pendingPasswordContext.value = {};
    if (preview?.need_mapping) {
      ElMessage.info('密码验证通过，该账单格式需配置字段映射或使用 AI 整单识别');
    } else if (preview?.need_password) {
      ElMessage.warning('仍需输入密码解锁内层加密文件');
    } else {
      ElMessage.success('密码验证通过，账单已成功解析');
    }
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '密码错误，请重试');
    }
  } finally {
    verifyingPassword.value = false;
    ephemeralPassword.value = ''; // Strictly discard from memory
  }
}

async function handleAiFullParse(item: FileQueueItem) {
  aiParsingId.value = item.id;
  try {
    const preview = await importStore.parseFile(item, undefined, undefined, true);
    if (preview && preview.need_password) {
      openPasswordModal(item, { useLlm: true });
    } else if (preview && !preview.need_mapping) {
      ElMessage.success('AI 整单识别成功');
    } else {
      ElMessage.warning('AI 未能识别出完整交易明细，请进入映射向导设置');
    }
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err?.message || 'AI 整单识别失败，请检查设置中的 AI 配置');
    }
  } finally {
    aiParsingId.value = null;
  }
}

function goToMapping(item: FileQueueItem) {
  router.push({
    path: `/import/mapping/${item.preview?.token || ''}`,
    query: { fileName: item.name },
  });
}

function goToMappingWizard() {
  router.push('/import/mapping');
}

async function commitActivePreview() {
  if (!importStore.activePreview) return;
  const activeToken = importStore.activePreview.token;
  const committedItem = importStore.queue.find((i) => i.preview?.token === activeToken);
  committing.value = true;
  try {
    const overrides = buildModifiedAccountOverrides();
    const res = await importStore.commitActivePreview(activeToken, overrides);
    if (res.replaced_batch_id) {
      ElMessage.success(
        `重复批次 #${res.replaced_batch_id} 已重新解析并入库（新批次 #${res.batch_id}，写入 ${res.imported_count} 条交易）`
      );
    } else {
      ElMessage.success(`导入成功: 写入 ${res.imported_count} 条交易`);
    }
    if (committedItem) {
      importStore.removeQueueItem(committedItem.id);
    }
    await Promise.all([appStore.fetchMeta(), importStore.fetchBatches()]);
    if (importStore.queue.length === 0) {
      router.push('/transactions');
    }
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '导入提交失败');
    }
  } finally {
    committing.value = false;
  }
}

async function commitAllReady() {
  committing.value = true;
  let successCount = 0;
  let replacedCount = 0;
  const succeededIds: string[] = [];
  for (const item of [...importStore.queue]) {
    if (item.status === 'ready' && item.preview) {
      try {
        const res = await importStore.commitActivePreview(item.preview.token, item.accountOverrides);
        item.status = 'success';
        succeededIds.push(item.id);
        successCount++;
        if (res.replaced_batch_id) replacedCount++;
      } catch (e: any) {
        item.status = 'error';
        item.errorMsg = e?.message || '导入提交失败';
      }
    }
  }
  for (const id of succeededIds) {
    importStore.removeQueueItem(id);
  }
  committing.value = false;
  if (successCount > 0) {
    const replaceNote = replacedCount > 0 ? `（含 ${replacedCount} 个重复批次重新解析入库）` : '';
    ElMessage.success(`完成导入，共提交 ${successCount} 个文件${replaceNote}`);
  }
  await Promise.all([appStore.fetchMeta(), importStore.fetchBatches()]);
}

async function confirmRollback(batch: BatchItem) {
  try {
    await ElMessageBox.confirm(
      `确定要撤销批次「${batch.file_name}」吗？将同时清除该批次的 ${batch.row_count} 条交易并恢复原对账状态。`,
      '撤销批次确认',
      {
        confirmButtonText: '确定撤销',
        cancelButtonText: '取消',
        type: 'warning',
      }
    );
    await importStore.rollbackBatch(batch.id);
    ElMessage.success('批次已成功撤销');
    await appStore.fetchMeta();
  } catch {
    // Cancelled
  }
}

async function loadProfilesList() {
  loadingProfiles.value = true;
  try {
    profiles.value = await getProfiles();
  } catch (e) {
    console.error('Failed to load profiles', e);
  } finally {
    loadingProfiles.value = false;
  }
}

async function confirmDeleteProfile(profile: ProfileSummary) {
  try {
    await ElMessageBox.confirm(
      `确定要删除银行配置「${profile.name}」(${profile.id}) 吗？删除后下次导入同格式账单将需要重新配置映射。`,
      '删除银行配置确认',
      {
        confirmButtonText: '确定删除',
        cancelButtonText: '取消',
        type: 'warning',
      }
    );
  } catch {
    return;
  }

  try {
    await deleteProfile(profile.id);
    ElMessage.success(`银行配置「${profile.name}」已删除`);
    await loadProfilesList();
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err?.message || '删除银行配置失败');
    }
  }
}

onMounted(() => {
  importStore.fetchBatches();
  loadProfilesList();
});
</script>

<style scoped>
.import-view {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 20px;
}

.upload-section {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* Drop Zone */
.drop-zone {
  height: 200px;
  border: 2px dashed var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--surface-alt);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.2s ease;
  user-select: none;
}

.drop-zone:hover,
.drop-zone.is-dragging {
  border-color: var(--primary);
  background: var(--primary-soft-2);
}

.drop-zone.is-compact {
  height: 72px;
}

.drop-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  text-align: center;
}

.is-compact .drop-content {
  flex-direction: row;
  gap: 12px;
}

.upload-icon {
  color: var(--primary);
}

.primary-text {
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
  display: block;
}

.sub-text {
  font-size: 12px;
  color: var(--text-3);
  display: block;
  margin-top: 4px;
}

/* Queue Card */
.queue-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 16px;
  background: var(--surface);
}

.queue-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.queue-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.source-tag {
  background: var(--primary-soft);
  color: var(--primary);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  font-size: 11.5px;
  font-weight: 500;
}

.status-cell {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}

.preview-warnings {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* Preview Panel */
.preview-panel {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 20px;
  background: var(--surface);
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.panel-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}

.metric-tiles {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}

.tile {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 12px;
}

.tile-label {
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 4px;
}

.tile-val {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.account-confirm-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-soft, #fafbfc);
}

.confirm-label {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-2);
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
  margin-bottom: 8px;
}

/* History */
.history-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.history-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.count-note {
  font-size: 13px;
  color: var(--text-3);
}

/* Profiles */
.profiles-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.profiles-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  color: var(--text-2);
}

/* Ephemeral Password Dialog */
.password-dialog-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.pwd-tip {
  font-size: 13px;
  color: var(--text);
  line-height: 1.5;
}

.pwd-security-note {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--success);
}
</style>
