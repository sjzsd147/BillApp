<template>
  <div class="settings-view" v-loading="loading">
    <el-tabs v-model="activeTab" class="settings-tabs">
      <!-- Tab 1: General Settings -->
      <el-tab-pane label="通用设置" name="general">
        <div class="settings-card">
          <div class="card-title">界面与偏好</div>
          <el-form label-position="left" label-width="180px">
            <el-form-item label="表格行高密度">
              <el-radio-group v-model="settings.general.table_density">
                <el-radio label="standard">标准 (44px)</el-radio>
                <el-radio label="compact">紧凑 (36px)</el-radio>
              </el-radio-group>
            </el-form-item>

            <el-form-item label="隐私模式 (金额遮罩)">
              <el-switch v-model="settings.general.amount_privacy" />
              <span class="form-tip">开启后金额默认显示为 ¥••••，鼠标悬停时临时显现</span>
            </el-form-item>

            <el-form-item label="自动规则学习建议">
              <el-switch v-model="settings.general.learning_suggestions_enabled" />
              <span class="form-tip">检测到重复手动修改同一商户分类时，主动提示创建规则</span>
            </el-form-item>
          </el-form>
          <div class="card-footer">
            <el-button type="primary" :loading="saving" @click="saveSettings">保存通用设置</el-button>
          </div>
        </div>
      </el-tab-pane>

      <!-- Tab 2: Deduplication Settings -->
      <el-tab-pane label="去重与对账规则" name="dedup">
        <div class="settings-card">
          <div class="card-title">重复候选匹配阈值</div>
          <el-form label-position="left" label-width="200px">
            <el-form-item label="自动合并置信度阈值">
              <el-input-number
                v-model="settings.dedup.auto_confirm_threshold"
                :min="0.6"
                :max="1.0"
                :step="0.05"
                :precision="2"
              />
              <span class="form-tip">匹配分 ≥ 此值时导入阶段自动合并 (默认 0.85)</span>
            </el-form-item>

            <el-form-item label="人工核对下限阈值">
              <el-input-number
                v-model="settings.dedup.pending_threshold"
                :min="0.4"
                :max="0.8"
                :step="0.05"
                :precision="2"
              />
              <span class="form-tip">匹配分在下限与自动阈值之间时进入「待确认」队列 (默认 0.55)</span>
            </el-form-item>

            <el-form-item label="秒级交易时间容差 (分钟)">
              <el-input-number
                v-model="settings.dedup.second_window_minutes"
                :min="5"
                :max="120"
                :step="5"
              />
              <span class="form-tip">精确到秒级的跨渠道交易判定窗口 (默认 30 分钟)</span>
            </el-form-item>

            <el-form-item label="储蓄卡天级时间容差 (天)">
              <el-input-number
                v-model="settings.dedup.day_window_debit_days"
                :min="1"
                :max="10"
              />
              <span class="form-tip">仅精确到日的借记卡记账延时容差 (默认 3 天)</span>
            </el-form-item>

            <el-form-item label="信用卡入账时间容差 (天)">
              <el-input-number
                v-model="settings.dedup.day_window_credit_days"
                :min="1"
                :max="15"
              />
              <span class="form-tip">信用卡入账延时容差 (默认 5 天)</span>
            </el-form-item>
          </el-form>
          <div class="card-footer">
            <el-button type="primary" :loading="saving" @click="saveSettings">保存去重设置</el-button>
          </div>
        </div>
      </el-tab-pane>

      <!-- Tab: Refund & Foreign Currency (Milestone 6) -->
      <el-tab-pane label="退款与币种" name="currency">
        <!-- Card 1: Refund Policy -->
        <div class="settings-card">
          <div class="card-title">退款计算口径</div>
          <el-form label-position="left" label-width="160px">
            <el-form-item label="退款冲抵规则">
              <el-radio-group v-model="settings.refund.mode">
                <el-radio label="original_month">冲抵原交易所在月 (推荐)</el-radio>
                <el-radio label="refund_month">计入退款当月</el-radio>
              </el-radio-group>
            </el-form-item>
          </el-form>
          <div class="card-example-box">
            <div class="example-title">口径差异说明：</div>
            <div class="example-line">例：10 月消费 32.80 元，11 月退款。</div>
            <div class="example-line">· <strong>冲抵原月</strong>：10 月支出减少 32.80，11 月支出不受影响（默认模式，真实反映原月净消费）。</div>
            <div class="example-line">· <strong>计入退款当月</strong>：10 月支出不变，11 月支出减少 32.80。</div>
          </div>
          <div class="card-footer">
            <el-button type="primary" :loading="saving" @click="saveSettings">保存退款口径</el-button>
          </div>
        </div>

        <!-- Card 2: FX Policy Explanation -->
        <div class="settings-card">
          <div class="card-title">外币折算与汇率说明</div>
          <div class="card-notice-info">
            <p><strong>外币统一口径</strong>：外币按交易时汇率折算为人民币，计入总支出与全部分析图表；明细列表保留原币金额、汇率与折算值，便于核对银行对账单。</p>
            <p><strong>折算规则</strong>：折算人民币 = 原币金额 × 交易时汇率，四舍五入到分；由后端严格计算，前端不自行计算。</p>
            <p><strong>汇率来源</strong>：优先联网自动获取（如 Frankfurter API），失败或离线时标记「汇率待获取」，不计入合计、不按 0 估算，支持手动补录。</p>
          </div>
        </div>

        <!-- Card 3: Used FX Rates & Refresh/Manual Entry -->
        <div class="settings-card">
          <div class="card-header-flex">
            <div class="card-title">已使用汇率表</div>
            <div class="card-actions">
              <el-button :loading="refreshingFx" @click="handleRefreshFx">刷新缺失汇率</el-button>
              <el-button type="primary" @click="openAddRateDialog">手动补录汇率</el-button>
            </div>
          </div>

          <div v-if="fxOverview.pending_count > 0" class="fx-pending-alert">
            <span>当前有 <strong>{{ fxOverview.pending_count }}</strong> 笔外币交易汇率待获取，补齐后计入总支出。</span>
            <el-button link type="primary" size="small" @click="openAddRateDialog">立即补录</el-button>
          </div>

          <el-table :data="fxOverview.rates" border size="default" style="width: 100%; margin-top: 12px;">
            <el-table-column label="币种" prop="currency" width="90" align="center" />
            <el-table-column label="汇率 (1 原币 = ? CNY)" prop="rate" min-width="150" align="right">
              <template #default="{ row }">
                <span class="tabular-nums font-mono">{{ row.rate }}</span>
              </template>
            </el-table-column>
            <el-table-column label="生效/交易日期" prop="effective_date" width="130" align="center" />
            <el-table-column label="来源" width="100" align="center">
              <template #default="{ row }">
                <el-tag :type="row.source === 'online' ? 'success' : 'info'" size="small">
                  {{ row.source === 'online' ? '联网获取' : '手动补录' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="更新时间" prop="updated_at" width="160" align="center" />
            <el-table-column label="说明" prop="note" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">
                <span>{{ row.note || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="80" align="center">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="openEditRateDialog(row)">编辑</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- Tab 3: LLM Settings -->
      <el-tab-pane label="大语言模型 (AI 辅助)" name="llm">
        <div class="settings-card">
          <div class="card-title">LLM 服务接入与凭据</div>
          <div class="card-notice">
            <el-icon><Lock /></el-icon>
            <span>
              API Key 均保存在本机，且由 Windows DPAPI 加密防护。所有功能在未配置 AI 或离线时仍可 100% 正常运行。
            </span>
          </div>

          <el-form label-position="top">
            <el-form-item label="启用 AI 智能辅助">
              <el-switch v-model="settings.llm.enabled" />
            </el-form-item>

            <el-form-item label="API 服务地址 (Base URL)">
              <el-input
                v-model="settings.llm.base_url"
                placeholder="例如: https://api.openai.com/v1"
              />
            </el-form-item>

            <el-form-item label="模型名称 (Model)">
              <div class="model-select-row">
                <el-select
                  v-model="settings.llm.model"
                  filterable
                  allow-create
                  default-first-option
                  clearable
                  placeholder="请选择或输入模型名称（可点击右侧获取）"
                  class="model-select-control"
                >
                  <el-option
                    v-for="m in availableModels"
                    :key="m.id"
                    :label="m.id"
                    :value="m.id"
                  >
                    <div class="model-option-item">
                      <span>{{ m.id }}</span>
                      <span v-if="m.owned_by" class="model-option-meta">{{ m.owned_by }}</span>
                    </div>
                  </el-option>
                </el-select>
                <el-button
                  :loading="fetchingModels"
                  @click="handleFetchModels"
                >
                  获取模型列表
                </el-button>
              </div>
              <div v-if="fetchedModels.length > 0" class="model-fetch-hint">
                已从接口获取 {{ fetchedModels.length }} 个模型，可直接下拉选择或输入自定义模型名
              </div>
            </el-form-item>

            <el-form-item label="API Key">
              <el-input
                v-model="llmApiKeyInput"
                type="password"
                show-password
                :placeholder="settings.llm.has_api_key ? '已配置密钥 (输入新密钥可覆盖)' : '请输入 API Key'"
              />
            </el-form-item>

            <el-form-item label="请求超时 (秒)">
              <el-input-number
                v-model="settings.llm.timeout_sec"
                :min="10"
                :max="180"
                :step="5"
              />
            </el-form-item>

            <div class="tasks-subgroup">
              <div class="subgroup-title">AI 任务开关</div>
              <el-checkbox v-model="settings.llm.task_classify_enabled">
                未知交易兜底智能分类
              </el-checkbox>
              <el-checkbox v-model="settings.llm.task_review_enabled">
                复杂重复候选归因推理
              </el-checkbox>
              <el-checkbox v-model="settings.llm.task_suggest_profile_enabled">
                未知账单表头列名智能推导
              </el-checkbox>
              <el-checkbox v-model="settings.llm.task_full_parse_enabled">
                复杂/未知账单 AI 整单接管识别
              </el-checkbox>
            </div>
          </el-form>

          <div class="card-footer">
            <el-button
              :loading="testingLLM"
              @click="handleTestLLM"
            >
              测试连接
            </el-button>
            <el-button type="primary" :loading="saving" @click="saveLLMSettings">
              保存 AI 配置
            </el-button>
          </div>
        </div>
      </el-tab-pane>

      <!-- Tab 4: About & System Info -->
      <el-tab-pane label="关于与数据" name="about">
        <div class="settings-card">
          <div class="card-title">系统信息</div>
          <div class="info-list">
            <div class="info-item">
              <span class="label">应用名称:</span>
              <span class="val">账单助手 (Bill App)</span>
            </div>
            <div class="info-item">
              <span class="label">版本号:</span>
              <span class="val">v{{ appStore.version }}</span>
            </div>
            <div class="info-item">
              <span class="label">数据库路径:</span>
              <span class="val path-text">{{ settings.db_path || appStore.dbPath || '-' }}</span>
            </div>
            <div class="info-item">
              <span class="label">数据库体积:</span>
              <span class="val">{{ (settings.db_size_bytes / 1024).toFixed(1) }} KB</span>
            </div>
            <div class="info-item">
              <span class="label">隐私承诺:</span>
              <span class="val" style="color: var(--success);">
                ✓ 严格遵循单机运行标准，无云端数据回传，用户隐私 100% 驻留本机
              </span>
            </div>
          </div>

          <el-divider />

          <div class="card-header-flex" style="margin-bottom: 12px">
            <div class="card-title" style="margin-bottom: 0">软件更新 (GitHub Releases)</div>
            <el-tag size="small" :type="updaterModeTagType">{{ updaterModeLabel }}</el-tag>
          </div>

          <div v-if="isDesktopEnv" class="updater-box">
            <div class="updater-status-row">
              <span class="updater-status-text">{{ updaterStatusText }}</span>
              <span v-if="updateState?.checkedAt" class="updater-checked-at">
                上次检查: {{ formatCheckedAt(updateState.checkedAt) }}
              </span>
            </div>

            <div v-if="updateState?.status === 'downloading' && updateState.downloadProgress" class="updater-progress">
              <el-progress
                :percentage="Math.round(updateState.downloadProgress.percent)"
                :stroke-width="10"
              />
              <div class="updater-progress-meta">
                <span>
                  {{ formatBytes(updateState.downloadProgress.transferred) }} /
                  {{ formatBytes(updateState.downloadProgress.total) }}
                </span>
                <span>{{ formatSpeed(updateState.downloadProgress.bytesPerSecond) }}</span>
              </div>
            </div>

            <div v-if="updateState?.releaseNotes" class="updater-notes-box">
              <div class="example-title">版本更新说明 (v{{ updateState.latestVersion }})</div>
              <div class="updater-notes-body">{{ updateState.releaseNotes }}</div>
            </div>

            <el-alert
              v-if="updateState?.error"
              :title="updateState.error"
              :type="updateState.manualDownloadRequired ? 'warning' : 'error'"
              show-icon
              :closable="false"
              style="margin-top: 10px"
            />

            <div class="data-actions-row" style="margin-top: 12px">
              <el-button
                :loading="checkingUpdate"
                :disabled="updateState?.status === 'downloading'"
                @click="handleCheckUpdates"
              >
                检查更新
              </el-button>
              <el-button
                v-if="canDownloadUpdate"
                type="primary"
                @click="handleDownloadUpdate"
              >
                下载更新 v{{ updateState?.latestVersion }}
              </el-button>
              <el-button
                v-if="updateState?.status === 'downloaded'"
                type="success"
                @click="handleQuitAndInstall"
              >
                立即重启并安装
              </el-button>
              <el-button @click="handleOpenReleasePage">
                前往 GitHub Releases
              </el-button>
            </div>
            <div class="form-tip" style="margin-left: 0; margin-top: 8px">
              仅向 GitHub Releases 查询公开版本元数据，不上传任何个人或账单数据。
            </div>
          </div>

          <div v-else class="updater-box">
            <div class="form-tip" style="margin-left: 0; margin-bottom: 10px">
              当前处于网页访问模式，桌面端自动更新仅在桌面客户端中可用。
            </div>
            <el-button @click="handleOpenReleasePage">前往 GitHub Releases</el-button>
          </div>

          <el-divider />

          <div class="card-title">数据备份与管理</div>
          <div class="data-actions-row">
            <el-button :loading="backingUp" @click="handleBackup">导出数据库备份 (.db)</el-button>
            <el-button :loading="restoring" @click="triggerRestoreUpload">从备份文件恢复</el-button>
            <input
              ref="restoreInputRef"
              type="file"
              accept=".db,.sqlite,.sqlite3"
              style="display: none"
              @change="handleRestoreFileChange"
            />
            <el-button type="danger" plain @click="openResetDialog">清空账单数据</el-button>
          </div>
          <div class="form-tip" style="margin-left: 0">
            建议在重装系统或清理数据前先导出 `.db` 备份；恢复备份将覆盖当前数据库中的交易记录与账户信息。
          </div>
          <el-alert
            v-if="backupNotice"
            :title="backupNotice.title"
            :description="backupNotice.description"
            :type="backupNotice.type"
            show-icon
            closable
            style="margin-top: 12px"
            @close="backupNotice = null"
          />
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- Reset Data Confirmation Dialog -->
    <el-dialog v-model="showResetDialog" title="清空账单数据" width="440px">
      <div class="reset-warning-box">
        此操作将永久删除所有已导入的交易流水、导入批次、账户、汇率缓存与对账记录，建议先导出数据库备份。
      </div>
      <el-form label-position="top" style="margin-top: 14px">
        <el-form-item>
          <el-checkbox v-model="resetForm.keep_rules">保留自定义分类、分类规则与银行模板</el-checkbox>
        </el-form-item>
        <el-form-item label="请输入「确认清空」以继续">
          <el-input v-model="resetForm.confirm_text" placeholder="确认清空" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showResetDialog = false">取消</el-button>
        <el-button
          type="danger"
          :loading="resetting"
          :disabled="resetForm.confirm_text.trim() !== '确认清空'"
          @click="handleConfirmReset"
        >
          确认清空数据
        </el-button>
      </template>
    </el-dialog>

    <!-- Manual FX Rate Dialog -->
    <el-dialog v-model="showFxDialog" title="手动补录汇率" width="420px">
      <el-form label-position="top">
        <el-form-item label="币种代码 (如 USD, EUR)">
          <el-select
            v-model="manualFxForm.currency"
            filterable
            allow-create
            placeholder="请选择或输入币种代码"
            style="width: 100%"
          >
            <el-option
              v-for="curr in availableCurrencies"
              :key="curr"
              :value="curr"
              :label="curr"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="汇率 (1 外币 = ? 人民币)">
          <el-input-number
            v-model="manualFxForm.rate"
            :min="0.0001"
            :step="0.01"
            :precision="4"
            style="width: 100%"
            placeholder="例如: 7.2013"
          />
        </el-form-item>
        <el-form-item label="生效日期">
          <el-date-picker
            v-model="manualFxForm.effective_date"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="请选择生效日期"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="说明备注">
          <el-input v-model="manualFxForm.note" placeholder="例如: 银行信用卡对账单官方汇率" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="manualFxForm.apply_to_all_missing">
            应用至所有待补汇率的该币种交易
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showFxDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingFx" @click="handleSaveManualFx">保存汇率</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue';
import { useRoute } from 'vue-router';
import { ElMessage, ElNotification } from 'element-plus';
import { Lock } from '@element-plus/icons-vue';
import {
  getSettings,
  updateSettings,
  backupDatabase,
  restoreDatabase,
  resetData,
} from '@/api/settings';
import { testLLM, listLLMModels } from '@/api/llm';
import { getFxRates, refreshFx, saveFxRate } from '@/api/fx';
import { useAppStore } from '@/stores/app';
import { isElectronDesktop, saveDatabaseBackupViaDesktop } from '@/utils/desktop';
import type { SettingsOut, FxRatesResponse, FxRateItem, LLMModelInfo } from '@/types/api';

const route = useRoute();
const appStore = useAppStore();

const activeTab = ref((route.params.tab as string) || 'general');
const loading = ref(false);
const saving = ref(false);
const testingLLM = ref(false);
const fetchingModels = ref(false);

const refreshingFx = ref(false);
const savingFx = ref(false);
const showFxDialog = ref(false);

const backingUp = ref(false);
const backupNotice = ref<{
  type: 'success' | 'info' | 'warning' | 'error';
  title: string;
  description: string;
} | null>(null);
const restoring = ref(false);
const resetting = ref(false);
const showResetDialog = ref(false);

const isDesktopEnv = computed(() => isElectronDesktop());
const updateState = computed(() => appStore.updateState);
const checkingUpdate = computed(() => updateState.value?.status === 'checking');
const canDownloadUpdate = computed(() => {
  const s = updateState.value;
  return Boolean(s && s.status === 'available' && s.autoInstallSupported && !s.manualDownloadRequired);
});
const updaterModeLabel = computed(() => {
  if (!isDesktopEnv.value) return '网页模式';
  const mode = updateState.value?.installMode;
  if (mode === 'nsis') return 'Windows 安装版 · 自动更新';
  if (mode === 'portable') return 'Windows 便携版 · 手动更新';
  if (mode === 'appimage') return 'Linux AppImage · 自动更新';
  if (mode === 'deb' || mode === 'rpm' || mode === 'pacman') return `Linux ${mode.toUpperCase()} · 自动更新`;
  if (mode === 'mac') {
    return updateState.value?.autoInstallSupported ? 'macOS · 自动更新' : 'macOS 未签名 · 手动更新';
  }
  if (mode === 'dev') return '开发环境';
  return '桌面客户端';
});
const updaterModeTagType = computed(() => {
  if (!isDesktopEnv.value) return 'info';
  return updateState.value?.autoInstallSupported ? 'success' : 'warning';
});
const updaterStatusText = computed(() => {
  const s = updateState.value;
  if (!s || s.status === 'idle') {
    return `当前版本 v${appStore.version}，尚未检查更新`;
  }
  if (s.status === 'checking') {
    return '正在从 GitHub Releases 检查更新…';
  }
  if (s.status === 'not-available') {
    return `当前已是最新版本 (v${s.currentVersion || appStore.version})`;
  }
  if (s.status === 'available') {
    return s.manualDownloadRequired
      ? `发现新版本 v${s.latestVersion}（需前往 GitHub 手动下载安装包）`
      : `发现新版本 v${s.latestVersion}，可立即下载更新`;
  }
  if (s.status === 'downloading') {
    return `正在下载新版本 v${s.latestVersion}…`;
  }
  if (s.status === 'downloaded') {
    return `新版本 v${s.latestVersion} 已下载就绪，重启即可完成安装`;
  }
  return `检查更新异常（当前版本 v${s.currentVersion || appStore.version}）`;
});
const restoreInputRef = ref<HTMLInputElement | null>(null);
const resetForm = reactive({
  confirm_text: '',
  keep_rules: true,
});

const fxOverview = reactive<FxRatesResponse>({
  rates: [],
  currencies: ['USD', 'EUR', 'HKD', 'GBP', 'JPY'],
  pending_count: 0,
  pending_by_currency: [],
});

const availableCurrencies = computed(() => {
  const defaults = ['USD', 'EUR', 'HKD', 'GBP', 'JPY', 'CAD', 'AUD', 'SGD'];
  const fromApi = fxOverview.currencies.filter((c) => c !== 'CNY');
  return Array.from(new Set([...fromApi, ...defaults]));
});

const manualFxForm = reactive({
  currency: 'USD',
  rate: undefined as number | undefined,
  effective_date: new Date().toISOString().slice(0, 10),
  note: '',
  apply_to_all_missing: false,
});

const llmApiKeyInput = ref('');
const fetchedModels = ref<LLMModelInfo[]>([]);

const PRESET_LLM_MODELS: LLMModelInfo[] = [
  { id: 'gpt-4o-mini', name: 'gpt-4o-mini', owned_by: 'preset' },
  { id: 'gpt-4o', name: 'gpt-4o', owned_by: 'preset' },
  { id: 'qwen-plus', name: 'qwen-plus', owned_by: 'preset' },
  { id: 'qwen-turbo', name: 'qwen-turbo', owned_by: 'preset' },
  { id: 'deepseek-chat', name: 'deepseek-chat', owned_by: 'preset' },
];

const availableModels = computed<LLMModelInfo[]>(() => {
  const base = fetchedModels.value.length > 0 ? fetchedModels.value : PRESET_LLM_MODELS;
  const current = (settings.llm.model || '').trim();
  if (!current || base.some((m) => m.id === current)) {
    return base;
  }
  return [{ id: current, name: current, owned_by: 'current' }, ...base];
});

const settings = reactive<SettingsOut>({
  general: {
    table_density: 'standard',
    amount_privacy: false,
    remember_window: true,
    learning_suggestions_enabled: true,
  },
  dedup: {
    auto_confirm_threshold: 0.85,
    pending_threshold: 0.55,
    second_window_minutes: 30,
    day_window_debit_days: 3,
    day_window_credit_days: 5,
    llm_arbitrate_enabled: false,
    llm_auto_confirm: false,
    llm_auto_confirm_threshold: 0.9,
  },
  refund: {
    mode: 'original_month',
  },
  llm: {
    base_url: '',
    model: '',
    temperature: 0.2,
    timeout_sec: 60,
    enabled: false,
    task_classify_enabled: true,
    task_review_enabled: true,
    task_arbitrate_enabled: false,
    task_merchant_norm_enabled: true,
    task_suggest_profile_enabled: true,
    task_full_parse_enabled: true,
    auto_confirm_dedup: false,
    auto_confirm_threshold: 0.9,
    api_key_masked: '',
    has_api_key: false,
  },
  db_path: '',
  db_size_bytes: 0,
});

async function loadSettings() {
  loading.value = true;
  try {
    const res = await getSettings();
    Object.assign(settings.general, res.general);
    Object.assign(settings.dedup, res.dedup);
    Object.assign(settings.refund, res.refund);
    Object.assign(settings.llm, res.llm);
    settings.db_path = res.db_path;
    settings.db_size_bytes = res.db_size_bytes;
  } catch (e) {
    console.error('Failed to load settings', e);
  } finally {
    loading.value = false;
  }
}

async function saveSettings() {
  saving.value = true;
  try {
    await updateSettings({
      general: settings.general,
      dedup: settings.dedup,
      refund: settings.refund,
    });
    ElMessage.success('设置已成功保存');
  } catch (err: any) {
    ElMessage.error(err.message || '保存设置失败');
  } finally {
    saving.value = false;
  }
}

async function saveLLMSettings() {
  saving.value = true;
  try {
    const payload: any = {
      base_url: settings.llm.base_url,
      model: settings.llm.model,
      timeout_sec: settings.llm.timeout_sec,
      enabled: settings.llm.enabled,
      task_classify_enabled: settings.llm.task_classify_enabled,
      task_review_enabled: settings.llm.task_review_enabled,
      task_suggest_profile_enabled: settings.llm.task_suggest_profile_enabled,
      task_full_parse_enabled: settings.llm.task_full_parse_enabled,
    };
    if (llmApiKeyInput.value.trim()) {
      payload.api_key = llmApiKeyInput.value.trim();
    }
    await updateSettings({
      llm: payload,
    });
    llmApiKeyInput.value = '';
    ElMessage.success('AI 设置已保存');
    await loadSettings();
    await appStore.fetchLLMStatus();
  } catch (err: any) {
    ElMessage.error(err.message || '保存 AI 配置失败');
  } finally {
    saving.value = false;
  }
}

async function handleFetchModels() {
  if (!settings.llm.base_url.trim()) {
    ElMessage.warning('请先填写 API 服务地址 (Base URL)');
    return;
  }
  fetchingModels.value = true;
  try {
    const res = await listLLMModels({
      base_url: settings.llm.base_url || undefined,
      api_key: llmApiKeyInput.value.trim() || undefined,
    });
    if (res.ok && res.models.length > 0) {
      fetchedModels.value = res.models;
      if (!settings.llm.model?.trim()) {
        settings.llm.model = res.models[0].id;
      }
      ElMessage.success(`已获取 ${res.models.length} 个可用模型`);
    } else if (res.ok) {
      fetchedModels.value = [];
      ElMessage.warning('接口未返回模型列表，可直接输入自定义模型名');
    } else {
      ElMessage.error(`获取模型列表失败: ${res.error || '未知错误'}`);
    }
  } catch (err: any) {
    ElMessage.error(err.message || '获取模型列表异常');
  } finally {
    fetchingModels.value = false;
  }
}

async function handleTestLLM() {
  testingLLM.value = true;
  try {
    const res = await testLLM({
      base_url: settings.llm.base_url || undefined,
      model: settings.llm.model || undefined,
      api_key: llmApiKeyInput.value.trim() || undefined,
    });
    if (res.ok) {
      ElMessage.success(`连接成功！模型: ${res.model}，延迟: ${res.latency_ms}ms`);
    } else {
      ElMessage.error(`连接失败: ${res.error || '未知错误'}`);
    }
  } catch (err: any) {
    ElMessage.error(err.message || '测试请求异常');
  } finally {
    testingLLM.value = false;
  }
}

async function loadFxOverview() {
  try {
    const res = await getFxRates();
    fxOverview.rates = res.rates;
    fxOverview.currencies = res.currencies;
    fxOverview.pending_count = res.pending_count;
    fxOverview.pending_by_currency = res.pending_by_currency;
  } catch (e) {
    console.error('Failed to load fx overview', e);
  }
}

async function handleRefreshFx() {
  refreshingFx.value = true;
  try {
    const res = await refreshFx();
    ElMessage.success(`缺失汇率刷新完毕，成功补充 ${res.refreshed_count} 笔汇率`);
    await loadFxOverview();
  } catch (err: any) {
    ElMessage.error(err?.message || '刷新汇率失败');
  } finally {
    refreshingFx.value = false;
  }
}

function openAddRateDialog() {
  manualFxForm.currency = 'USD';
  manualFxForm.rate = undefined;
  manualFxForm.effective_date = new Date().toISOString().slice(0, 10);
  manualFxForm.note = '';
  manualFxForm.apply_to_all_missing = false;
  showFxDialog.value = true;
}

function openEditRateDialog(row: FxRateItem) {
  manualFxForm.currency = row.currency;
  manualFxForm.rate = Number(row.rate);
  manualFxForm.effective_date = row.effective_date;
  manualFxForm.note = row.note || '';
  manualFxForm.apply_to_all_missing = false;
  showFxDialog.value = true;
}

async function handleSaveManualFx() {
  if (!manualFxForm.rate || manualFxForm.rate <= 0) {
    ElMessage.warning('请输入大于 0 的有效汇率');
    return;
  }
  if (!manualFxForm.effective_date) {
    ElMessage.warning('请选择汇率生效日期');
    return;
  }
  savingFx.value = true;
  try {
    const res = await saveFxRate({
      currency: manualFxForm.currency,
      rate: manualFxForm.rate,
      effective_date: manualFxForm.effective_date,
      note: manualFxForm.note || undefined,
      apply_to_all_missing: manualFxForm.apply_to_all_missing,
    });
    ElMessage.success(`汇率已保存，已更新 ${res.updated_transactions} 笔关联交易`);
    showFxDialog.value = false;
    await loadFxOverview();
  } catch (err: any) {
    ElMessage.error(err?.message || '保存汇率失败');
  } finally {
    savingFx.value = false;
  }
}

async function handleBackup() {
  backingUp.value = true;
  backupNotice.value = null;
  const stamp = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19);
  const defaultFilename = `billapp-backup-${stamp}.db`;
  const win = window as any;

  try {
    // 1. Desktop Electron native Save-As dialog via preload IPC bridge
    const desktopRes = await saveDatabaseBackupViaDesktop(defaultFilename);
    if (desktopRes !== null) {
      if (desktopRes.cancelled) {
        ElMessage.info('已取消导出数据库备份');
        return;
      }
      if (!desktopRes.ok) {
        throw new Error(desktopRes.error || '导出数据库备份失败');
      }
      const savedPath = desktopRes.path || desktopRes.filename || defaultFilename;
      backupNotice.value = {
        type: 'success',
        title: '数据库备份已导出',
        description: `保存位置：${savedPath}`,
      };
      ElNotification({
        title: '数据库备份成功',
        message: `文件已保存至：${savedPath}`,
        type: 'success',
        duration: 6000,
      });
      return;
    }

    // 2. Modern browser File System Access API (lets user choose exact folder & filename)
    if (typeof win.showSaveFilePicker === 'function') {
      let handle: any;
      try {
        handle = await win.showSaveFilePicker({
          suggestedName: defaultFilename,
          types: [
            {
              description: 'SQLite 数据库备份 (.db)',
              accept: { 'application/octet-stream': ['.db'] },
            },
          ],
        });
      } catch (pickerErr: any) {
        if (pickerErr?.name === 'AbortError') {
          ElMessage.info('已取消导出数据库备份');
          return;
        }
        throw pickerErr;
      }

      const blob = await backupDatabase();
      const writable = await handle.createWritable();
      try {
        await writable.write(blob);
      } finally {
        await writable.close();
      }
      const chosenName = handle.name || defaultFilename;
      backupNotice.value = {
        type: 'success',
        title: '数据库备份已导出',
        description: `已保存至您选择的位置：${chosenName}`,
      };
      ElNotification({
        title: '数据库备份成功',
        message: `备份文件「${chosenName}」已写入您选择的目录`,
        type: 'success',
        duration: 6000,
      });
      return;
    }

    // 3. Fallback standard browser download
    const blob = await backupDatabase();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = defaultFilename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    backupNotice.value = {
      type: 'info',
      title: '已发起备份下载',
      description: `文件名：${defaultFilename}（当前运行环境不支持弹出路径选择框，已保存至浏览器默认下载目录）`,
    };
    ElNotification({
      title: '已发起备份下载',
      message: `文件「${defaultFilename}」已开始下载，请查看浏览器下载目录`,
      type: 'success',
      duration: 6000,
    });
  } catch (err: any) {
    const msg = err?.message || '导出数据库备份失败';
    backupNotice.value = {
      type: 'error',
      title: '导出数据库备份失败',
      description: msg,
    };
    ElMessage.error(msg);
  } finally {
    backingUp.value = false;
  }
}

function triggerRestoreUpload() {
  restoreInputRef.value?.click();
}

async function handleRestoreFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;

  restoring.value = true;
  try {
    const res = await restoreDatabase(file);
    ElMessage.success(`数据库已恢复：共 ${res.transactions_count} 条交易、${res.accounts_count} 个账户`);
    await Promise.all([loadSettings(), loadFxOverview(), appStore.fetchMeta()]);
  } catch (err: any) {
    ElMessage.error(err?.message || '恢复数据库失败');
  } finally {
    restoring.value = false;
  }
}

function openResetDialog() {
  resetForm.confirm_text = '';
  resetForm.keep_rules = true;
  showResetDialog.value = true;
}

async function handleConfirmReset() {
  if (resetForm.confirm_text.trim() !== '确认清空') {
    ElMessage.warning('请输入「确认清空」后再操作');
    return;
  }
  resetting.value = true;
  try {
    await resetData({
      confirm_text: resetForm.confirm_text,
      keep_rules: resetForm.keep_rules,
      keep_settings: true,
    });
    showResetDialog.value = false;
    ElMessage.success('账单数据已清空');
    await Promise.all([loadSettings(), loadFxOverview(), appStore.fetchMeta()]);
  } catch (err: any) {
    ElMessage.error(err?.message || '清空数据失败');
  } finally {
    resetting.value = false;
  }
}

watch(
  () => route.params.tab,
  (tab) => {
    if (typeof tab === 'string' && tab) {
      activeTab.value = tab;
    }
  }
);

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 B';
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatSpeed(bytesPerSec: number): string {
  if (!bytesPerSec || bytesPerSec <= 0) return '';
  if (bytesPerSec < 1024 * 1024) return `${(bytesPerSec / 1024).toFixed(0)} KB/s`;
  return `${(bytesPerSec / (1024 * 1024)).toFixed(2)} MB/s`;
}

function formatCheckedAt(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

async function handleCheckUpdates() {
  try {
    const res = await appStore.checkForUpdates();
    if (!res) return;
    if (res.status === 'not-available') {
      ElMessage.success(`当前已是最新版本 v${res.currentVersion}`);
    } else if (res.status === 'available') {
      ElMessage.info(`发现新版本 v${res.latestVersion}`);
    }
  } catch (err: any) {
    ElMessage.error(err?.message || '检查更新失败');
  }
}

async function handleDownloadUpdate() {
  try {
    await appStore.downloadUpdate();
  } catch (err: any) {
    ElMessage.error(err?.message || '下载更新失败');
  }
}

async function handleQuitAndInstall() {
  try {
    const res = await appStore.quitAndInstallUpdate();
    if (res && !res.ok && res.error) {
      ElMessage.error(res.error);
    }
  } catch (err: any) {
    ElMessage.error(err?.message || '安装更新失败');
  }
}

async function handleOpenReleasePage() {
  await appStore.openReleasePage();
}

onMounted(() => {
  loadSettings();
  loadFxOverview();
  appStore.initDesktopUpdater();
});
</script>

<style scoped>
.card-example-box {
  background: var(--surface-2, rgba(0, 0, 0, 0.02));
  border: 1px solid var(--border);
  border-radius: var(--radius-md, 6px);
  padding: 10px 14px;
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.6;
}

.example-title {
  font-weight: 600;
  margin-bottom: 4px;
  color: var(--text);
}

.example-line {
  margin-bottom: 2px;
}

.card-notice-info {
  background: rgba(47, 107, 255, 0.05);
  border: 1px solid rgba(47, 107, 255, 0.2);
  border-radius: var(--radius-md, 6px);
  padding: 12px 16px;
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.6;
}

.card-notice-info p {
  margin: 0 0 6px 0;
}

.card-notice-info p:last-child {
  margin-bottom: 0;
}

.card-header-flex {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-actions {
  display: flex;
  gap: 8px;
}

.fx-pending-alert {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(217, 119, 6, 0.08);
  border: 1px solid rgba(217, 119, 6, 0.25);
  border-radius: var(--radius-md, 6px);
  padding: 8px 12px;
  font-size: 13px;
  color: #b45309;
}

.settings-view {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 20px;
}

.settings-card {
  max-width: 680px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 10px 0;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 4px;
}

.card-notice {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--primary-soft-2);
  border: 1px solid rgba(47, 107, 255, 0.2);
  border-radius: var(--radius-md);
  padding: 10px 14px;
  font-size: 12.5px;
  color: var(--text-2);
}

.card-notice .el-icon {
  color: var(--primary);
  font-size: 16px;
}

.form-tip {
  font-size: 12px;
  color: var(--text-3);
  margin-left: 12px;
}

.tasks-subgroup {
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--surface-alt);
  padding: 14px;
  border-radius: var(--radius-md);
  margin-top: 8px;
}

.subgroup-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 4px;
}

.card-footer {
  display: flex;
  gap: 12px;
  margin-top: 8px;
}

.info-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  font-size: 13.5px;
}

.info-item {
  display: flex;
  align-items: center;
  gap: 12px;
}

.info-item .label {
  color: var(--text-3);
  width: 100px;
}

.info-item .val {
  color: var(--text);
  font-weight: 500;
}

.path-text {
  font-family: monospace;
  font-size: 12.5px;
  background: var(--surface-alt);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
}

.data-actions-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.reset-warning-box {
  background: rgba(198, 40, 40, 0.08);
  border: 1px solid rgba(198, 40, 40, 0.25);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  font-size: 13px;
  color: var(--danger);
  line-height: 1.5;
}

.model-select-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.model-select-control {
  flex: 1;
}

.model-option-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.model-option-meta {
  font-size: 12px;
  color: var(--text-3);
}

.model-fetch-hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-3);
}

.updater-box {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 14px;
}

.updater-status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.updater-status-text {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--text);
}

.updater-checked-at {
  font-size: 12px;
  color: var(--text-3);
}

.updater-progress {
  margin-top: 12px;
}

.updater-progress-meta {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-3);
  margin-top: 4px;
}

.updater-notes-box {
  margin-top: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
}

.updater-notes-body {
  font-size: 12.5px;
  color: var(--text-2);
  white-space: pre-line;
  line-height: 1.55;
}
</style>
