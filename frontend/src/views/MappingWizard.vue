<template>
  <div class="mapping-wizard-layout">
    <!-- Topbar -->
    <header class="wizard-topbar">
      <div class="topbar-left">
        <el-button link :icon="ArrowLeft" @click="handleExit">返回</el-button>
        <span class="divider-v">|</span>
        <span class="wizard-title">字段映射向导</span>
        <span v-if="fileName" class="file-chip">{{ fileName }}</span>
      </div>
      <div class="topbar-right">
        <el-button size="small" @click="handleExit">退出向导</el-button>
      </div>
    </header>

    <!-- Stepper Header -->
    <div class="stepper-bar">
      <el-steps :active="currentStep" finish-status="success" align-center>
        <el-step title="表头与数据范围" />
        <el-step title="字段映射" />
        <el-step title="金额与账户规则" />
        <el-step title="预览与保存配置" />
      </el-steps>
    </div>

    <!-- Step Content Area -->
    <div class="step-container">
      <!-- Step 0: Header & Data Area -->
      <div v-if="currentStep === 0" class="step-card">
        <div class="card-header">
          <div class="title">步骤 1: 确定表头行与数据范围</div>
          <div class="desc">系统已自动识别表头行，您可以点击下表任意行将其设为表头。</div>
        </div>

        <div class="form-row">
          <el-form :inline="true">
            <el-form-item label="表头所在行">
              <el-input-number v-model="headerRowIndex" :min="1" :max="20" size="default" />
            </el-form-item>
            <el-form-item label="数据起始行">
              <el-input-number v-model="dataStartRowIndex" :min="headerRowIndex + 1" :max="30" size="default" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" plain :loading="aiSuggesting" @click="handleAiSuggest">
                ✨ 让 AI 智能生成字段映射
              </el-button>
              <el-button
                v-if="activeQueueItem"
                type="success"
                plain
                :loading="aiFullParsing"
                @click="handleDirectAiTakeover"
              >
                ✨ 让 AI 整单直接识别
              </el-button>
            </el-form-item>
          </el-form>
        </div>

        <!-- Raw preview sample table -->
        <div class="raw-table-box">
          <el-table
            :data="rawSampleRows"
            :max-height="380"
            border
            size="small"
            highlight-current-row
            style="width: 100%"
            @row-click="handleRowClick"
          >
            <el-table-column label="行号" width="70" align="center">
              <template #default="{ $index }">
                <span :class="{ 'header-indicator': $index + 1 === headerRowIndex }">
                  #{{ $index + 1 }}
                </span>
              </template>
            </el-table-column>
            <el-table-column
              v-for="(col, cIdx) in detectedHeaders"
              :key="cIdx"
              :prop="`col_${cIdx}`"
              :label="col || `列 ${cIdx + 1}`"
              min-width="120"
            />
          </el-table>
        </div>
      </div>

      <!-- Step 1: Field Mapping -->
      <div v-if="currentStep === 1" class="step-card">
        <div class="card-header">
          <div class="title">步骤 2: 标准字段映射</div>
          <div class="desc">请将账单中的列名对应至系统标准字段（交易时间与金额为必填项）。</div>
        </div>

        <div class="ai-assist-box">
          <el-button type="primary" plain :loading="aiSuggesting" @click="handleAiSuggest">
            ✨ 让 AI 智能识别列映射
          </el-button>
          <span class="ai-tip">自动根据表头语义和数据样例推导最佳映射</span>
        </div>

        <div class="mapping-grid">
          <div class="mapping-item required">
            <span class="field-label">交易时间 *</span>
            <el-select v-model="fieldMap.txn_time" placeholder="选择时间列" clearable>
              <el-option v-for="h in detectedHeaders" :key="h" :label="h" :value="h" />
            </el-select>
          </div>

          <div class="mapping-item" :class="{ required: amountMode !== 'split' }">
            <span class="field-label">交易金额 {{ amountMode === 'split' ? '(分列模式在下一步配置)' : '*' }}</span>
            <el-select
              v-model="fieldMap.amount"
              :placeholder="amountMode === 'split' ? '分列模式请在下一步选择支出/收入列' : '选择金额列'"
              :disabled="amountMode === 'split'"
              clearable
            >
              <el-option v-for="h in detectedHeaders" :key="h" :label="h" :value="h" />
            </el-select>
          </div>

          <div class="mapping-item">
            <span class="field-label">对方 / 商户</span>
            <el-select v-model="fieldMap.counterparty" placeholder="选择商户列" clearable>
              <el-option v-for="h in detectedHeaders" :key="h" :label="h" :value="h" />
            </el-select>
          </div>

          <div class="mapping-item">
            <span class="field-label">商品说明 / 附言</span>
            <el-select v-model="fieldMap.description" placeholder="选择说明列" clearable>
              <el-option v-for="h in detectedHeaders" :key="h" :label="h" :value="h" />
            </el-select>
          </div>

          <div class="mapping-item">
            <span class="field-label">支付渠道</span>
            <el-select v-model="fieldMap.channel" placeholder="选择支付渠道列" clearable>
              <el-option v-for="h in detectedHeaders" :key="h" :label="h" :value="h" />
            </el-select>
          </div>

          <div class="mapping-item">
            <span class="field-label">卡尾号</span>
            <el-select v-model="fieldMap.card_tail" placeholder="选择卡号列" clearable>
              <el-option v-for="h in detectedHeaders" :key="h" :label="h" :value="h" />
            </el-select>
          </div>
        </div>
      </div>

      <!-- Step 2: Amount Mode & Direction Rules -->
      <div v-if="currentStep === 2" class="step-card">
        <div class="card-header">
          <div class="title">步骤 3: 金额模式与收支规则</div>
          <div class="desc">根据银行或平台账单格式，指定金额的正负号定义或借贷标志。</div>
        </div>

        <div class="mode-selector">
          <div class="mode-title">金额模式</div>
          <el-radio-group v-model="amountMode" size="large">
            <el-radio-button label="signed">
              <div class="radio-label">单列有符号 (正数收入/负数支出)</div>
            </el-radio-button>
            <el-radio-button label="split">
              <div class="radio-label">收入/支出独立两列</div>
            </el-radio-button>
            <el-radio-button label="flag">
              <div class="radio-label">带收支/借贷标志列</div>
            </el-radio-button>
          </el-radio-group>
        </div>

        <div v-if="amountMode === 'split'" class="mode-config-box">
          <el-form :inline="true">
            <el-form-item label="支出列">
              <el-select v-model="splitExpenseCol" placeholder="选择支出金额列">
                <el-option v-for="h in detectedHeaders" :key="h" :label="h" :value="h" />
              </el-select>
            </el-form-item>
            <el-form-item label="收入列">
              <el-select v-model="splitIncomeCol" placeholder="选择收入金额列">
                <el-option v-for="h in detectedHeaders" :key="h" :label="h" :value="h" />
              </el-select>
            </el-form-item>
          </el-form>
        </div>

        <div v-if="amountMode === 'flag'" class="mode-config-box">
          <el-form :inline="true">
            <el-form-item label="收支标志列">
              <el-select v-model="flagCol" placeholder="选择收支/借贷标志列">
                <el-option v-for="h in detectedHeaders" :key="h" :label="h" :value="h" />
              </el-select>
            </el-form-item>
            <el-form-item label="支出标志值">
              <el-input v-model="flagExpenseVal" placeholder="例如: 支出 / 借" />
            </el-form-item>
            <el-form-item label="收入标志值">
              <el-input v-model="flagIncomeVal" placeholder="例如: 收入 / 贷" />
            </el-form-item>
          </el-form>
        </div>

        <div class="account-config-box">
          <div class="mode-title">账户关联信息</div>
          <el-form :inline="true">
            <el-form-item label="银行/机构名称">
              <el-input v-model="profileBank" placeholder="例如: 招商银行" />
            </el-form-item>
            <el-form-item label="账户类型">
              <el-select v-model="accountKind" style="width: 140px">
                <el-option label="借记卡" value="bank_debit" />
                <el-option label="信用卡" value="bank_credit" />
                <el-option label="第三方钱包" value="wallet" />
              </el-select>
            </el-form-item>
            <el-form-item label="卡号/账号尾号">
              <el-input v-model="defaultCardTail" maxlength="4" style="width: 110px" placeholder="4位如1234" />
            </el-form-item>
          </el-form>
        </div>
      </div>

      <!-- Step 3: Validate & Save -->
      <div v-if="currentStep === 3" class="step-card">
        <div class="card-header">
          <div class="title">步骤 4: 校验与保存配置</div>
          <div class="desc">为该映射模板命名，以便下次遇到相同表头的账单时自动识别。</div>
        </div>

        <div class="save-profile-form">
          <el-form label-position="top">
            <el-form-item label="配置名称 *">
              <el-input v-model="profileName" placeholder="例如: 招商银行储蓄卡流水导出版" />
            </el-form-item>
            <el-form-item label="配置 ID (唯一英文标识) *">
              <el-input v-model="profileId" placeholder="例如: cmb_debit_custom" />
            </el-form-item>
            <el-form-item>
              <el-checkbox v-model="autoApplyNextTime">
                下次遇到相同表头结构时自动使用此配置
              </el-checkbox>
            </el-form-item>
          </el-form>
        </div>
      </div>
    </div>

    <!-- Fixed Bottom Action Bar -->
    <footer class="wizard-footer">
      <div class="footer-left">
        <el-button v-if="currentStep > 0" @click="currentStep--">上一步</el-button>
      </div>
      <div class="footer-right">
        <el-button v-if="currentStep < 3" type="primary" @click="handleNextStep">
          下一步
        </el-button>
        <el-button
          v-else
          type="primary"
          :loading="saving"
          @click="handleSaveAndFinish"
        >
          保存配置并完成
        </el-button>
      </div>
    </footer>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';
import { ArrowLeft } from '@element-plus/icons-vue';
import { createProfile, suggestMapping } from '@/api/profiles';
import { extractRawImport } from '@/api/import';
import { useImportStore } from '@/stores/import';
import { useAppStore } from '@/stores/app';
import { buildProfileYaml } from '@/utils/profileYaml';

const route = useRoute();
const router = useRouter();
const importStore = useImportStore();
const appStore = useAppStore();

const fileName = ref((route.query.fileName as string) || '');
const detectedFileType = ref<string>('auto');
const currentStep = ref(0);
const saving = ref(false);
const aiSuggesting = ref(false);
const aiFullParsing = ref(false);

const activeQueueItem = computed(() => {
  const token = (route.params.token as string) || '';
  const qFileName = (route.query.fileName as string) || fileName.value || '';
  let found = importStore.queue.find(
    (i) => (token && i.preview?.token === token) || (qFileName && i.name === qFileName)
  );
  if (!found && importStore.queue.length > 0) {
    found = importStore.queue[0];
  }
  return found || null;
});

const headerRowIndex = ref(1);
const dataStartRowIndex = ref(2);

const detectedHeaders = ref<string[]>([
  '交易时间',
  '交易类型',
  '交易对方',
  '商品说明',
  '收/支',
  '金额(元)',
  '支付方式',
  '当前状态',
  '交易单号',
  '商户单号',
]);

const rawSampleRows = ref<any[]>([
  { col_0: '交易时间', col_1: '交易类型', col_2: '交易对方', col_3: '商品说明', col_4: '收/支', col_5: '金额(元)', col_6: '支付方式', col_7: '当前状态' },
  { col_0: '2026-05-01 12:30:15', col_1: '商户消费', col_2: '美团外卖', col_3: '午餐外卖订单', col_4: '支出', col_5: '28.50', col_6: '招行储蓄卡(1234)', col_7: '支付成功' },
  { col_0: '2026-05-01 18:20:00', col_1: '商户消费', col_2: '山姆会员商店', col_3: '超市购物', col_4: '支出', col_5: '356.80', col_6: '微信零钱', col_7: '支付成功' },
  { col_0: '2026-05-02 09:10:00', col_1: '转账', col_2: '张三', col_3: 'AA收款', col_4: '收入', col_5: '50.00', col_6: '零钱', col_7: '已到账' },
]);

const fieldMap = reactive<{
  txn_time: string;
  amount: string;
  counterparty: string;
  description: string;
  channel: string;
  card_tail: string;
}>({
  txn_time: '',
  amount: '',
  counterparty: '',
  description: '',
  channel: '',
  card_tail: '',
});

const amountMode = ref<'signed' | 'split' | 'flag'>('signed');
const splitExpenseCol = ref('');
const splitIncomeCol = ref('');
const flagCol = ref('');
const flagExpenseVal = ref('支出');
const flagIncomeVal = ref('收入');

const profileBank = ref('自定义机构');
const accountKind = ref('bank_debit');
const defaultCardTail = ref('');
const externalPreambleRows = ref<string[][]>([]);
const profileName = ref('自定义账单导入模板');
const profileId = ref(`custom_profile_${Date.now().toString(36)}`);
const autoApplyNextTime = ref(true);

function resetFieldMappings() {
  fieldMap.txn_time = '';
  fieldMap.amount = '';
  fieldMap.counterparty = '';
  fieldMap.description = '';
  fieldMap.channel = '';
  fieldMap.card_tail = '';
  splitExpenseCol.value = '';
  splitIncomeCol.value = '';
  flagCol.value = '';
}

function findBestHeaderRowIndex(parsedRows: string[][]): number {
  const maxCols = Math.max(...parsedRows.map((r) => r.length), 1);
  let bestIdx = 0;
  let bestScore = -9999;

  for (let r = 0; r < Math.min(parsedRows.length, 25); r++) {
    const nonEmptyCells = parsedRows[r].map((c) => String(c || '').trim()).filter((c) => c.length > 0);
    if (nonEmptyCells.length < 2) continue;

    let score = nonEmptyCells.length;
    if (maxCols >= 4 && nonEmptyCells.length < 3) {
      // Metadata key-value pairs before wide tables usually occupy only 1-2 columns
      score -= 8;
    }

    let headerMatches = 0;
    let colonPairs = 0;
    let dateValues = 0;
    for (const cell of nonEmptyCells) {
      if (/[：:]\s*\S/.test(cell)) {
        colonPairs += 1;
      }
      if (/^\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2})?/.test(cell) || /^-?\d+(?:,\d{3})*\.\d{2}$/.test(cell)) {
        dateValues += 1;
      }
      if (
        cell.length <= 18 &&
        !/[：:]\s*\S/.test(cell) &&
        /(记账日期|交易日期|交易时间|日期|时间|货币|币种|交易金额|支出金额|收入金额|借方金额|贷方金额|发生额|金额|联机余额|账户余额|余额|交易摘要|摘要|说明|备注|对手信息|对方户名|交易对方|对方账号|商户|收支|借贷|方向|渠道|支付方式|卡号|账号|当前状态|状态|交易单号|单号)/.test(
          cell
        )
      ) {
        headerMatches += 1;
      }
    }

    score += headerMatches * 4;
    score -= colonPairs * 6;
    score -= dateValues * 5;

    if (headerMatches > 0 && score > bestScore) {
      bestScore = score;
      bestIdx = r;
    }
  }
  return bestIdx;
}

function findDataStartRowIndex(rowObjectsOrCells: string[][], headerIdx: number): number {
  let startIdx = headerIdx + 1;
  const maxProbe = Math.min(rowObjectsOrCells.length, headerIdx + 5);
  while (startIdx < maxProbe) {
    const cells = rowObjectsOrCells[startIdx].map((c) => String(c || '').trim()).filter(Boolean);
    if (cells.length === 0) {
      startIdx += 1;
      continue;
    }
    const joined = cells.join(' ');
    // Stop as soon as a row contains digits (dates/amounts) indicating actual transaction data
    if (/\d/.test(joined)) {
      break;
    }
    // Otherwise it's likely a bilingual continuation subheader row (e.g. "Date / Currency / Amount")
    startIdx += 1;
  }
  return Math.min(startIdx, Math.max(headerIdx + 1, rowObjectsOrCells.length - 1));
}

function splitCsvLine(line: string, delimiter: string): string[] {
  const result: string[] = [];
  let cur = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (char === '"') {
      if (inQuotes && line[i + 1] === '"') {
        cur += '"';
        i++;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (char === delimiter && !inQuotes) {
      result.push(cur.trim());
      cur = '';
    } else {
      cur += char;
    }
  }
  result.push(cur.trim());
  return result;
}

function autoMapHeaders(headers: string[]) {
  resetFieldMappings();
  amountMode.value = 'signed';
  for (const h of headers) {
    if (/(时间|日期|交易日|记账日)/.test(h) && !fieldMap.txn_time) {
      fieldMap.txn_time = h;
    } else if (/(发生额|交易金额|金额)/.test(h) && !fieldMap.amount) {
      fieldMap.amount = h;
    } else if (/(对方|对手|商户|户名|交易对象)/.test(h) && !fieldMap.counterparty) {
      fieldMap.counterparty = h;
    } else if (/(说明|摘要|商品|备注|附言)/.test(h) && !fieldMap.description) {
      fieldMap.description = h;
    } else if (/(支付方式|渠道|方式)/.test(h) && !fieldMap.channel) {
      fieldMap.channel = h;
    } else if (/(卡号|账号|尾号)/.test(h) && !fieldMap.card_tail) {
      fieldMap.card_tail = h;
    } else if (/(收\/支|收支|借贷|方向)/.test(h)) {
      flagCol.value = h;
      amountMode.value = 'flag';
    } else if (/(借方金额|支出金额|借方)/.test(h)) {
      splitExpenseCol.value = h;
    } else if (/(贷方金额|收入金额|贷方)/.test(h)) {
      splitIncomeCol.value = h;
    }
  }
  if (splitExpenseCol.value && splitIncomeCol.value) {
    amountMode.value = 'split';
  }
}

function populateFromRawGrid(parsedRows: string[][]) {
  if (!parsedRows || parsedRows.length === 0) return;

  const bestHeaderIdx = findBestHeaderRowIndex(parsedRows);
  const dataStartIdx = findDataStartRowIndex(parsedRows, bestHeaderIdx);

  headerRowIndex.value = bestHeaderIdx + 1;
  dataStartRowIndex.value = dataStartIdx + 1;

  const rawHeaders = parsedRows[bestHeaderIdx].map((c) => String(c || '').trim());
  const maxCols = Math.max(...parsedRows.map((r) => r.length), rawHeaders.length);
  const headers: string[] = [];
  for (let c = 0; c < maxCols; c++) {
    headers.push(rawHeaders[c] || `列 ${c + 1}`);
  }
  detectedHeaders.value = headers;

  const sampleTableData: any[] = [];
  parsedRows.forEach((rowCells) => {
    const rowObj: any = {};
    for (let c = 0; c < maxCols; c++) {
      rowObj[`col_${c}`] = rowCells[c] || '';
    }
    sampleTableData.push(rowObj);
  });
  rawSampleRows.value = sampleTableData;

  autoMapHeaders(headers);

  // Local preamble extraction for top-of-statement account metadata (bank + 4-digit card tail)
  const preambleText = [
    ...externalPreambleRows.value.map((r) => r.join(' ')),
    ...parsedRows.slice(0, bestHeaderIdx).map((r) => r.join(' ')),
    fileName.value || '',
  ].join('\n');
  if (preambleText.trim()) {
    const cleaned = preambleText.replace(/\u00a0/g, ' ');
    if (profileBank.value === '自定义机构') {
      const mBank = cleaned.match(/([\u4e00-\u9fa5]{2,8}银行)/);
      if (mBank && !/对方/.test(mBank[1])) {
        profileBank.value = mBank[1];
      } else if (/China\s+Merchants\s+Bank/i.test(cleaned)) {
        profileBank.value = '招商银行';
      }
    }
    if (!defaultCardTail.value) {
      const mAcct = cleaned.match(
        /(?:账\s*号|卡\s*号|银行账号|银行卡号|Account\s*No\.?|Card\s*No\.?)\s*[：:]\s*([A-Za-z0-9*\- ]{4,40})/i
      );
      if (mAcct) {
        const tails = mAcct[1].match(/(\d{4})(?!\d)/g);
        if (tails && tails.length > 0) {
          defaultCardTail.value = tails[tails.length - 1];
        }
      }
      if (!defaultCardTail.value) {
        const mTail = cleaned.match(/尾\s*号\s*[：:为]?\s*(\d{4})\b/);
        if (mTail) defaultCardTail.value = mTail[1];
      }
    }
    if (/信用卡|贷记卡|Credit\s*Card/i.test(cleaned)) {
      accountKind.value = 'bank_credit';
    } else if (/借记卡|储蓄卡|Debit\s*Card/i.test(cleaned)) {
      accountKind.value = 'bank_debit';
    }
  }
}

function parseAndPopulateFromCsvText(text: string) {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.length > 0);
  if (lines.length === 0) return;

  const firstLine = lines[0];
  let delimiter = ',';
  if (firstLine.includes('\t')) delimiter = '\t';
  else if (firstLine.includes(';') && !firstLine.includes(',')) delimiter = ';';

  const parsedRows: string[][] = [];
  for (let i = 0; i < Math.min(lines.length, 25); i++) {
    const rawCells = splitCsvLine(lines[i], delimiter);
    if (rawCells.length > 0 && rawCells.some((c) => c.trim())) {
      parsedRows.push(rawCells);
    }
  }

  populateFromRawGrid(parsedRows);
}

function populateFromPreviewRows(previewRows: any[]) {
  if (previewRows.length === 0) return;
  const sample = previewRows.slice(0, 10);
  const sampleData: any[] = [];
  sample.forEach((r) => {
    sampleData.push({
      col_0: r.date || '',
      col_1: r.time || '',
      col_2: r.counterparty || '',
      col_3: r.description || '',
      col_4: r.direction || '',
      col_5: r.amount_display || '',
      col_6: r.channel || '',
    });
  });
  rawSampleRows.value = sampleData;
  detectedHeaders.value = ['日期', '时间', '交易对方', '商品说明', '收/支', '金额', '支付方式'];
}

async function initFromSource() {
  const token = (route.params.token as string) || '';
  const qFileName = (route.query.fileName as string) || '';

  let queueItem = importStore.queue.find(
    (i) => (token && i.preview?.token === token) || (qFileName && i.name === qFileName)
  );
  if (!queueItem && importStore.queue.length > 0) {
    queueItem = importStore.queue[0];
  }

  if (queueItem) {
    if (!fileName.value && queueItem.name) {
      fileName.value = queueItem.name;
    }
    if (profileName.value === '自定义账单导入模板' && queueItem.name) {
      const cleanName = queueItem.name.replace(/\.[^/.]+$/, '');
      profileName.value = `${cleanName} 导入模板`;
    }

    if (queueItem.preview?.detected_file_type) {
      detectedFileType.value = queueItem.preview.detected_file_type;
    }

    if (queueItem.preview?.preamble_rows && queueItem.preview.preamble_rows.length > 0) {
      externalPreambleRows.value = queueItem.preview.preamble_rows;
    }

    let populated = false;
    if (queueItem.preview?.raw_rows && queueItem.preview.raw_rows.length > 0) {
      populateFromRawGrid(queueItem.preview.raw_rows);
      populated = true;
    } else if (queueItem.file) {
      try {
        const extracted = await extractRawImport(queueItem.file);
        if (extracted.file_type) {
          detectedFileType.value = extracted.file_type;
        }
        if (extracted.preamble_rows && extracted.preamble_rows.length > 0) {
          externalPreambleRows.value = extracted.preamble_rows;
        }
        if (extracted.raw_rows && extracted.raw_rows.length > 0) {
          populateFromRawGrid(extracted.raw_rows);
          populated = true;
        }
      } catch {
        // Fallback to local text parser below for plain UTF-8 CSV/TXT
      }

      if (!populated) {
        try {
          const file = queueItem.file;
          const lower = file.name.toLowerCase();
          const isTextOrCsv =
            lower.endsWith('.csv') ||
            lower.endsWith('.txt') ||
            file.type.includes('csv') ||
            file.type.includes('text');
          if (isTextOrCsv) {
            const sliceText = await file.slice(0, 65536).text();
            parseAndPopulateFromCsvText(sliceText);
            populated = true;
          }
        } catch (e) {
          console.warn('Failed to parse file sample in wizard, falling back to defaults', e);
        }
      }
    }

    if (!populated && queueItem.preview?.rows && queueItem.preview.rows.length > 0) {
      populateFromPreviewRows(queueItem.preview.rows);
      populated = true;
    }

    // Automatically invoke AI mapping once when opening an unrecognized statement with AI enabled
    if (populated && appStore.llmStatus?.configured && appStore.llmStatus?.enabled) {
      await handleAiSuggest({ autoTriggered: true });
    }
  }
}

function handleRowClick(row: any, _col: any, _event: MouseEvent) {
  const idx = rawSampleRows.value.indexOf(row);
  if (idx !== -1) {
    headerRowIndex.value = idx + 1;
    const rowMatrix: string[][] = rawSampleRows.value.map((r) =>
      detectedHeaders.value.map((_, cIdx) => String(r[`col_${cIdx}`] ?? ''))
    );
    dataStartRowIndex.value = findDataStartRowIndex(rowMatrix, idx) + 1;

    const newHeaders: string[] = [];
    detectedHeaders.value.forEach((_, cIdx) => {
      newHeaders.push(String(row[`col_${cIdx}`] ?? ''));
    });
    if (newHeaders.some((h) => h.trim())) {
      detectedHeaders.value = newHeaders;
      autoMapHeaders(newHeaders);
    }
  }
}

async function handleAiSuggest(options?: { autoTriggered?: boolean } | Event) {
  const autoTriggered = options && !(options instanceof Event) && Boolean(options.autoTriggered);
  aiSuggesting.value = true;
  try {
    const startIdx = Math.max(0, dataStartRowIndex.value - 1);
    const dataSlice =
      rawSampleRows.value.length > startIdx
        ? rawSampleRows.value.slice(startIdx, startIdx + 5)
        : rawSampleRows.value.slice(Math.max(0, headerRowIndex.value), Math.max(0, headerRowIndex.value) + 5);
    const sampleRowsForAi: string[][] = [];
    dataSlice.forEach((row) => {
      const rowArr: string[] = [];
      detectedHeaders.value.forEach((_, cIdx) => {
        rowArr.push(String(row[`col_${cIdx}`] ?? ''));
      });
      sampleRowsForAi.push(rowArr);
    });

    const preambleRowsForAi: string[][] = [];
    const seenPreamble = new Set<string>();
    const pushPreambleRow = (cells: string[]) => {
      const cleaned = cells.map((c) => String(c || '').trim()).filter(Boolean);
      if (cleaned.length === 0) return;
      const key = cleaned.join('|');
      if (seenPreamble.has(key)) return;
      seenPreamble.add(key);
      preambleRowsForAi.push(cleaned);
    };
    externalPreambleRows.value.slice(0, 10).forEach(pushPreambleRow);
    const headerIdx = Math.max(0, headerRowIndex.value - 1);
    rawSampleRows.value.slice(0, headerIdx).slice(0, 10).forEach((row) => {
      const cells: string[] = [];
      detectedHeaders.value.forEach((_, cIdx) => {
        const val = String(row[`col_${cIdx}`] ?? '').trim();
        if (val) cells.push(val);
      });
      pushPreambleRow(cells);
    });

    const res = await suggestMapping(
      {
        headers: detectedHeaders.value,
        sample_rows: sampleRowsForAi.length > 0 ? sampleRowsForAi : undefined,
        preamble_rows: preambleRowsForAi.length > 0 ? preambleRowsForAi.slice(0, 15) : undefined,
        file_name: fileName.value || undefined,
        file_type: detectedFileType.value || 'csv',
      },
      { silent: autoTriggered }
    );
    if (res.amount_mode) {
      amountMode.value = res.amount_mode;
    }
    if (res.column_mapping && Object.keys(res.column_mapping).length > 0) {
      resetFieldMappings();
      for (const [k, v] of Object.entries(res.column_mapping)) {
        if (!v) continue;
        if (k === 'date' || k === 'txn_time') {
          fieldMap.txn_time = v as string;
        } else if (k === 'expense_amount' || k === 'expense') {
          splitExpenseCol.value = v as string;
        } else if (k === 'income_amount' || k === 'income') {
          splitIncomeCol.value = v as string;
        } else if (k === 'direction' || k === 'flag') {
          flagCol.value = v as string;
        } else if ((fieldMap as any)[k] !== undefined) {
          (fieldMap as any)[k] = v;
        }
      }
    }
    if (res.bank) {
      profileBank.value = res.bank;
    }
    if (res.account_kind === 'credit' || res.account_kind === 'debit') {
      accountKind.value = res.account_kind === 'credit' ? 'bank_credit' : 'bank_debit';
    } else if (res.account_kind === 'wallet') {
      accountKind.value = 'wallet';
    }
    if (res.card_tail && /^\d{4}$/.test(res.card_tail)) {
      defaultCardTail.value = res.card_tail;
    }
    if (res.flag_expense_val) {
      flagExpenseVal.value = res.flag_expense_val;
    }
    if (res.flag_income_val) {
      flagIncomeVal.value = res.flag_income_val;
    }

    if (res.source === 'llm') {
      ElMessage.success('AI 智能生成成功');
    } else if (!autoTriggered) {
      if (res.llm_status === 'offline' || res.llm_status === 'disabled') {
        ElMessage.warning(`${res.llm_error || '未启用 AI 服务'}，已先按表头规则自动匹配`);
      } else {
        ElMessage.error(`AI 调用失败（已回退规则匹配）：${res.llm_error || '请检查模型配置'}`);
      }
    }
  } catch (err: any) {
    if (!autoTriggered && !err?.toasted) {
      ElMessage.error(err?.message || 'AI 服务请求失败，请检查系统设置中的 AI 配置');
    }
  } finally {
    aiSuggesting.value = false;
  }
}

function handleNextStep() {
  if (currentStep.value === 1) {
    if (!fieldMap.txn_time) {
      ElMessage.warning('请配置交易时间对应列');
      return;
    }
    if (amountMode.value !== 'split' && !fieldMap.amount) {
      ElMessage.warning('请配置金额对应列');
      return;
    }
  }
  if (currentStep.value === 2) {
    if (amountMode.value === 'split' && (!splitExpenseCol.value || !splitIncomeCol.value)) {
      ElMessage.warning('请配置支出列与收入列');
      return;
    }
    if (amountMode.value === 'flag' && !flagCol.value) {
      ElMessage.warning('请配置收支/借贷标志列');
      return;
    }
  }
  currentStep.value++;
}

async function handleSaveAndFinish() {
  if (!profileName.value.trim() || !profileId.value.trim()) {
    ElMessage.warning('请填写配置名称与唯一标识 ID');
    return;
  }
  saving.value = true;
  try {
    const yamlConfig = buildProfileYaml({
      profileId: profileId.value.trim(),
      profileName: profileName.value.trim(),
      profileBank: profileBank.value.trim(),
      accountKind: accountKind.value,
      fileType: detectedFileType.value || 'auto',
      headerRowIndex: headerRowIndex.value,
      dataStartRowIndex: dataStartRowIndex.value,
      amountMode: amountMode.value,
      fieldMap: {
        txn_time: fieldMap.txn_time,
        amount: fieldMap.amount,
        counterparty: fieldMap.counterparty,
        description: fieldMap.description,
        channel: fieldMap.channel,
        card_tail: fieldMap.card_tail,
      },
      splitExpenseCol: splitExpenseCol.value,
      splitIncomeCol: splitIncomeCol.value,
      flagCol: flagCol.value,
      flagExpenseVal: flagExpenseVal.value,
      flagIncomeVal: flagIncomeVal.value,
      defaultCardTail: defaultCardTail.value.trim() || undefined,
      autoApplyNextTime: autoApplyNextTime.value,
    });

    const savedId = profileId.value.trim();
    await createProfile({
      id: savedId,
      name: profileName.value.trim(),
      yaml_content: yamlConfig,
    });
    const pendingItem = importStore.queue.find(
      (i) =>
        (i.status === 'need_mapping' || i.status === 'error') &&
        (!fileName.value || i.name === fileName.value)
    );
    let needsPasswordAgain = false;
    let reparseSucceeded = true;
    if (pendingItem) {
      try {
        const reparse = await importStore.parseFile(pendingItem, undefined, savedId);
        if (reparse?.need_password) {
          needsPasswordAgain = true;
        } else if (reparse?.need_mapping) {
          reparseSucceeded = false;
        }
      } catch {
        reparseSucceeded = false;
      }
    }
    if (needsPasswordAgain) {
      ElMessage.info('配置已保存，请返回导入页输入解密密码完成解析');
    } else if (!reparseSucceeded) {
      ElMessage.warning('配置已保存，但使用该配置重解析当前文件未成功，请返回导入页查看或调整映射');
    } else {
      ElMessage.success('配置已保存并生效');
    }
    router.push('/import');
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '保存配置失败');
    }
  } finally {
    saving.value = false;
  }
}

async function handleDirectAiTakeover() {
  const targetItem = activeQueueItem.value;
  if (!targetItem) {
    ElMessage.warning('未找到对应的待导入文件');
    return;
  }
  aiFullParsing.value = true;
  try {
    const preview = await importStore.parseFile(targetItem, undefined, undefined, true);
    if (preview?.need_password) {
      ElMessage.info('该账单受密码保护，请返回导入页输入密码后使用 AI 整单识别');
      router.push('/import');
    } else if (preview && !preview.need_mapping) {
      ElMessage.success('AI 整单识别成功');
      router.push('/import');
    } else {
      ElMessage.warning('AI 整单识别未提取到完整交易明细，请尝试手动映射');
    }
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err?.message || 'AI 整单识别失败，请检查设置中的 AI 配置');
    }
  } finally {
    aiFullParsing.value = false;
  }
}

function handleExit() {
  router.push('/import');
}

onMounted(() => {
  initFromSource();
});
</script>

<style scoped>
.mapping-wizard-layout {
  display: flex;
  flex-direction: column;
  width: 100vw;
  height: 100vh;
  background: var(--bg);
  overflow: hidden;
}

.wizard-topbar {
  flex-shrink: 0;
  height: var(--topbar-height);
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.divider-v {
  color: var(--border-strong);
}

.wizard-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}

.file-chip {
  background: var(--primary-soft);
  color: var(--primary);
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.stepper-bar {
  flex-shrink: 0;
  background: var(--surface);
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}

.step-container {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  justify-content: center;
  align-items: flex-start;
}

.step-card {
  width: 100%;
  max-width: 900px;
  min-width: 0;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.card-header .title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 4px;
}

.card-header .desc {
  font-size: 13px;
  color: var(--text-3);
}

.raw-table-box {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.header-indicator {
  color: var(--primary);
  font-weight: 700;
}

.ai-assist-box {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: var(--primary-soft-2);
  border: 1px solid rgba(47, 107, 255, 0.2);
  border-radius: var(--radius-md);
}

.ai-tip {
  font-size: 12.5px;
  color: var(--text-2);
}

.mapping-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

.mapping-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mapping-item .field-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-2);
}

.mapping-item.required .field-label {
  color: var(--text);
  font-weight: 600;
}

.mode-selector {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.mode-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.radio-label {
  font-size: 13px;
}

.mode-config-box,
.account-config-box {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.wizard-footer {
  flex-shrink: 0;
  height: 60px;
  background: var(--surface);
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
}
</style>
