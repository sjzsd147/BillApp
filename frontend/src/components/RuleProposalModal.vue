<template>
  <el-dialog
    v-model="visible"
    title="自动分类规则建议"
    width="520px"
    :close-on-click-modal="false"
  >
    <div v-if="proposal" class="proposal-body">
      <div class="prompt-msg">
        {{ proposal.prompt_message }}
      </div>

      <div class="rule-details">
        <div class="detail-row">
          <span class="label">规则名称:</span>
          <span class="val">{{ proposal.candidate_rule.name }}</span>
        </div>
        <div class="detail-row">
          <span class="label">匹配条件:</span>
          <span class="val">
            字段「{{ proposal.candidate_rule.field }}」包含「{{ proposal.candidate_rule.pattern_value }}」
          </span>
        </div>
        <div class="detail-row">
          <span class="label">历史影响:</span>
          <span class="val highlight">
            发现 {{ proposal.applicable_count }} 笔相同特征的历史记录可被批量更新
          </span>
        </div>
      </div>

      <div class="options-box">
        <el-checkbox v-model="applyToExisting">
          同时应用到现有的 {{ proposal.applicable_count }} 笔历史交易
        </el-checkbox>
      </div>
    </div>

    <template #footer>
      <div class="dialog-footer">
        <el-button @click="visible = false">忽略</el-button>
        <el-button type="primary" :loading="applying" @click="handleApply">
          创建规则并应用
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { ElMessage } from 'element-plus';
import { applyProposal } from '@/api/rules';
import type { RuleProposal } from '@/types/api';

const visible = ref(false);
const applying = ref(false);
const proposal = ref<RuleProposal | null>(null);
const applyToExisting = ref(true);

const emit = defineEmits<{
  (e: 'applied'): void;
}>();

function open(p: RuleProposal) {
  proposal.value = p;
  applyToExisting.value = true;
  visible.value = true;
}

async function handleApply() {
  if (!proposal.value) return;
  applying.value = true;
  try {
    const res = await applyProposal({
      name: proposal.value.candidate_rule.name,
      category_id: proposal.value.candidate_rule.category_id,
      field: proposal.value.candidate_rule.field,
      pattern_type: proposal.value.candidate_rule.pattern_type,
      pattern_value: proposal.value.candidate_rule.pattern_value,
      priority: proposal.value.candidate_rule.priority,
      apply_to_existing: applyToExisting.value,
    });
    ElMessage.success(`规则创建成功，已更新 ${res.updated_count} 笔历史交易`);
    visible.value = false;
    emit('applied');
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '应用规则失败');
    }
  } finally {
    applying.value = false;
  }
}

defineExpose({ open });
</script>

<style scoped>
.proposal-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.prompt-msg {
  font-size: 14px;
  color: var(--text);
  line-height: 1.5;
}

.rule-details {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
}

.detail-row {
  display: flex;
  gap: 8px;
}

.detail-row .label {
  color: var(--text-3);
  width: 72px;
}

.detail-row .val {
  color: var(--text);
  font-weight: 500;
}

.detail-row .val.highlight {
  color: var(--primary);
}

.options-box {
  margin-top: 4px;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
