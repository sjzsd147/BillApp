<template>
  <div class="rules-view">
    <div class="rules-container">
      <!-- Left Category Tree Pane -->
      <aside class="category-pane" v-loading="loadingCategories">
        <div class="pane-header">
          <span class="pane-title">分类体系 (2 级)</span>
          <el-button type="primary" link size="small" @click="openAddCategoryDialog(null)">
            + 根分类
          </el-button>
        </div>

        <div class="tree-wrapper">
          <el-tree
            :data="categoryTree"
            node-key="id"
            default-expand-all
            :expand-on-click-node="false"
            class="cat-tree"
          >
            <template #default="{ node, data }">
              <div class="cat-tree-node">
                <span class="cat-name">{{ data.name }}</span>
                <span class="cat-count" v-if="data.transaction_count > 0">
                  ({{ data.transaction_count }})
                </span>
                <div class="node-actions">
                  <el-button
                    v-if="!data.parent_id"
                    link
                    size="small"
                    @click.stop="openAddCategoryDialog(data)"
                  >
                    +子类
                  </el-button>
                  <el-button
                    v-if="!data.is_builtin"
                    link
                    type="danger"
                    size="small"
                    @click.stop="handleDeleteCategory(data)"
                  >
                    删除
                  </el-button>
                </div>
              </div>
            </template>
          </el-tree>
        </div>
      </aside>

      <!-- Right Rules Manager -->
      <main class="rules-main" v-loading="loadingRules">
        <el-tabs v-model="activeTab" class="rules-tabs">
          <!-- Tab 1: User Rules -->
          <el-tab-pane label="我的自定义规则" name="user_rules">
            <div class="tab-toolbar">
              <span class="tip-text">按优先级（由小到大）依次匹配，命中后停止后续规则匹配。</span>
              <div class="actions">
                <el-button size="small" @click="openTesterDrawer">测试规则匹配</el-button>
                <el-button type="primary" size="small" @click="openCreateRuleDialog">
                  + 新建规则
                </el-button>
              </div>
            </div>

            <el-table :data="userRules" border style="width: 100%">
              <el-table-column prop="priority" label="优先级" width="80" align="center" sortable />
              <el-table-column prop="name" label="规则名称" min-width="140" />
              <el-table-column label="匹配条件" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">
                  字段「{{ row.field }}」{{ row.pattern_type === 'contains' ? '包含' : row.pattern_type }}「{{ row.pattern_value }}」
                </template>
              </el-table-column>
              <el-table-column prop="category_name" label="目标分类" width="130" />
              <el-table-column prop="hit_count" label="命中次数" width="90" align="center" />
              <el-table-column label="操作" width="130" fixed="right">
                <template #default="{ row }">
                  <el-button link type="primary" size="small" @click="openEditRuleDialog(row)">
                    编辑
                  </el-button>
                  <el-button link type="danger" size="small" @click="handleDeleteRule(row)">
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
            <EmptyState v-if="userRules.length === 0" title="暂无自定义规则" />
          </el-tab-pane>

          <!-- Tab 2: Built-in Rules -->
          <el-tab-pane label="系统内置规则" name="builtin_rules">
            <div class="tab-toolbar">
              <span class="tip-text">系统预置的商户与渠道自动分类规则 (只读)。</span>
            </div>
            <el-table :data="builtinRules" border style="width: 100%">
              <el-table-column prop="priority" label="优先级" width="80" align="center" />
              <el-table-column prop="name" label="规则名称" min-width="140" />
              <el-table-column label="匹配模式" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ row.field }} ~ {{ row.pattern_value }}
                </template>
              </el-table-column>
              <el-table-column prop="category_name" label="对应分类" width="130" />
              <el-table-column prop="hit_count" label="命中" width="80" align="center" />
            </el-table>
          </el-tab-pane>

          <!-- Tab 3: Learning Suggestions -->
          <el-tab-pane label="学习建议" name="suggestions">
            <div class="tab-toolbar">
              <span class="tip-text">根据近期手动修改分类记录自动聚类推荐的规则。</span>
              <el-button size="small" :icon="Refresh" @click="loadSuggestions">刷新建议</el-button>
            </div>

            <div class="suggestions-grid">
              <div
                v-for="(item, idx) in suggestions"
                :key="idx"
                class="suggestion-card"
              >
                <div class="card-brand">{{ item.brand }}</div>
                <div class="card-desc">
                  检测到 {{ item.count }} 笔包含「{{ item.brand }}」的历史交易被归类为
                  <strong>「{{ item.category_name }}」</strong>
                </div>
                <div class="card-actions">
                  <el-button
                    type="primary"
                    size="small"
                    :loading="adoptingKey === suggestionKey(item)"
                    :disabled="adoptingKey !== null"
                    @click="adoptSuggestion(item)"
                  >
                    采纳并生成规则
                  </el-button>
                </div>
              </div>
              <EmptyState v-if="suggestions.length === 0" title="暂无学习建议" />
            </div>
          </el-tab-pane>
        </el-tabs>
      </main>
    </div>

    <!-- Category Add/Edit Dialog -->
    <el-dialog v-model="showCategoryDialog" :title="dialogCategoryTitle" width="400px">
      <el-form label-position="top">
        <el-form-item label="分类名称 *">
          <el-input v-model="categoryForm.name" placeholder="请输入分类名称" />
        </el-form-item>
        <el-form-item label="收支类型">
          <el-radio-group v-model="categoryForm.direction" :disabled="Boolean(parentCategory)">
            <el-radio label="expense">支出</el-radio>
            <el-radio label="income">收入</el-radio>
          </el-radio-group>
          <div v-if="parentCategory" class="tip-text" style="margin-top: 4px; width: 100%">
            子分类默认继承父分类收支类型
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCategoryDialog = false">取消</el-button>
        <el-button type="primary" @click="submitCategoryForm">保存</el-button>
      </template>
    </el-dialog>

    <!-- Category Delete & Reassign Dialog -->
    <el-dialog v-model="showDeleteCategoryDialog" title="删除分类" width="440px">
      <div v-if="deletingCategory" style="display: flex; flex-direction: column; gap: 12px">
        <div style="font-size: 13.5px; color: var(--text)">
          确定要删除自定义分类 <strong>「{{ deletingCategory.name }}」</strong> 吗？
        </div>
        <div
          v-if="
            deletingCategory.transaction_count > 0 ||
            (deletingCategory.rule_count || 0) > 0 ||
            (deletingCategory.children && deletingCategory.children.length > 0)
          "
          class="tip-text"
          style="color: var(--warning)"
        >
          该分类当前关联了 {{ deletingCategory.transaction_count }} 笔交易
          <template v-if="(deletingCategory.rule_count || 0) > 0">
            、{{ deletingCategory.rule_count }} 条规则
          </template>
          <template v-if="deletingCategory.children && deletingCategory.children.length > 0">
            及 {{ deletingCategory.children.length }} 个子分类（仅支持迁移至一级根分类）
          </template>
          ，请选择一个同方向分类以自动迁移关联数据。
        </div>
        <el-form label-position="top">
          <el-form-item label="迁移目标分类（存在关联交易/规则/子分类时必填）">
            <el-select
              v-model="deleteReassignToId"
              clearable
              filterable
              style="width: 100%"
              placeholder="若无关联数据可留空，否则请选择迁移分类"
            >
              <el-option
                v-for="opt in deleteReassignOptions"
                :key="opt.id"
                :value="opt.id"
                :label="opt.label"
              />
            </el-select>
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="showDeleteCategoryDialog = false">取消</el-button>
        <el-button type="danger" :loading="deletingCategoryLoading" @click="confirmDeleteCategory">
          确认删除
        </el-button>
      </template>
    </el-dialog>

    <!-- Rule Add/Edit Dialog -->
    <el-dialog v-model="showRuleDialog" :title="editingRuleId ? '编辑规则' : '新建规则'" width="480px">
      <el-form label-position="top">
        <el-form-item label="规则名称 *">
          <el-input v-model="ruleForm.name" placeholder="例如: 星巴克自动分类" />
        </el-form-item>
        <el-form-item label="匹配字段">
          <el-select v-model="ruleForm.field" style="width: 100%">
            <el-option label="对方/商户名称 (counterparty)" value="counterparty" />
            <el-option label="商品说明 (description)" value="description" />
            <el-option label="规范商户名 (merchant_norm)" value="merchant_norm" />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配模式">
          <el-select v-model="ruleForm.pattern_type" style="width: 100%">
            <el-option label="包含 (contains)" value="contains" />
            <el-option label="前缀匹配 (prefix)" value="prefix" />
            <el-option label="精准完全匹配 (exact)" value="exact" />
            <el-option label="正则表达式 (regex)" value="regex" />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配内容 (关键字 / 正则) *">
          <el-input v-model="ruleForm.pattern_value" placeholder="例如: 星巴克|Starbucks" />
        </el-form-item>
        <el-form-item label="目标分类 *">
          <CategoryPicker v-model="ruleForm.category_id" />
        </el-form-item>
        <el-form-item label="优先级 (数值越小越先执行)">
          <el-input-number v-model="ruleForm.priority" :min="1" :max="999" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showRuleDialog = false">取消</el-button>
        <el-button type="primary" @click="submitRuleForm">确定保存</el-button>
      </template>
    </el-dialog>

    <!-- Rule Tester Drawer -->
    <el-drawer v-model="showTesterDrawer" title="规则匹配测试器" size="460px">
      <div class="tester-content">
        <div class="tip">模拟规则对现有交易的命中效果（不产生修改）。</div>
        <el-form label-position="top">
          <el-form-item label="测试字段">
            <el-select v-model="testerForm.field" style="width: 100%">
              <el-option label="对方名称 (counterparty)" value="counterparty" />
              <el-option label="商品说明 (description)" value="description" />
            </el-select>
          </el-form-item>
          <el-form-item label="测试模式">
            <el-select v-model="testerForm.pattern_type" style="width: 100%">
              <el-option label="包含 (contains)" value="contains" />
              <el-option label="正则 (regex)" value="regex" />
            </el-select>
          </el-form-item>
          <el-form-item label="测试关键词">
            <el-input v-model="testerForm.pattern_value" placeholder="输入测试词" />
          </el-form-item>
          <el-button type="primary" :loading="testingRule" @click="runRuleTest">
            运行测试
          </el-button>
        </el-form>
        <div v-if="testResult" class="test-result-box">
          <div class="result-summary">
            已成功匹配到 <strong>{{ testResult.matched_count }}</strong> 笔历史交易
          </div>
          <div class="matched-list">
            <div v-for="t in testResult.preview" :key="t.id" class="matched-item">
              <span class="t-name">{{ t.counterparty || t.description }}</span>
              <AmountText :value="t.amount_cents" />
            </div>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Refresh } from '@element-plus/icons-vue';
import { useAppStore } from '@/stores/app';
import {
  getCategories,
  createCategory,
  deleteCategory,
} from '@/api/categories';
import {
  getRules,
  createRule,
  updateRule,
  deleteRule,
  testRule,
  applyProposal,
  getSuggestions,
} from '@/api/rules';
import CategoryPicker from '@/components/CategoryPicker.vue';
import AmountText from '@/components/AmountText.vue';
import EmptyState from '@/components/EmptyState.vue';
import type {
  CategoryItemOut,
  RuleOut,
  RuleSuggestionOut,
  RuleTestResponse,
} from '@/types/api';

const appStore = useAppStore();

const activeTab = ref('user_rules');
const loadingCategories = ref(false);
const loadingRules = ref(false);

const categoryTree = ref<CategoryItemOut[]>([]);
const allRules = ref<RuleOut[]>([]);
const suggestions = ref<RuleSuggestionOut[]>([]);

// Category Dialog
const showCategoryDialog = ref(false);
const dialogCategoryTitle = ref('新建分类');
const parentCategory = ref<CategoryItemOut | null>(null);
const categoryForm = reactive({
  name: '',
  direction: 'expense' as 'expense' | 'income',
});

// Category Delete & Reassign Dialog
const showDeleteCategoryDialog = ref(false);
const deletingCategory = ref<CategoryItemOut | null>(null);
const deleteReassignToId = ref<number | null>(null);
const deletingCategoryLoading = ref(false);
const adoptingKey = ref<string | null>(null);

function suggestionKey(item: RuleSuggestionOut): string {
  return `${item.brand}::${item.category_id}`;
}

const deleteReassignOptions = computed(() => {
  const target = deletingCategory.value;
  if (!target) return [];
  const hasChildren = Boolean(target.children && target.children.length > 0);
  const excludedIds = new Set<number>([target.id]);
  for (const ch of target.children || []) {
    excludedIds.add(ch.id);
  }
  const opts: Array<{ id: number; label: string }> = [];
  for (const root of categoryTree.value) {
    if (root.direction !== target.direction) continue;
    if (!excludedIds.has(root.id)) {
      opts.push({ id: root.id, label: root.name });
    }
    // Only allow reassigning to 2nd-level subcategories when target itself has no children (depth <= 2)
    if (!hasChildren) {
      for (const child of root.children || []) {
        if (child.direction !== target.direction || excludedIds.has(child.id)) continue;
        opts.push({ id: child.id, label: `${root.name} / ${child.name}` });
      }
    }
  }
  return opts;
});

// Rule Dialog
const showRuleDialog = ref(false);
const editingRuleId = ref<number | null>(null);
const ruleForm = reactive({
  name: '',
  field: 'counterparty' as any,
  pattern_type: 'contains' as any,
  pattern_value: '',
  category_id: null as number | null,
  priority: 100,
});

// Rule Tester Drawer
const showTesterDrawer = ref(false);
const testingRule = ref(false);
const testerForm = reactive({
  field: 'counterparty' as any,
  pattern_type: 'contains' as any,
  pattern_value: '',
});
const testResult = ref<RuleTestResponse | null>(null);

const userRules = computed(() => allRules.value.filter((r) => !r.is_builtin));
const builtinRules = computed(() => allRules.value.filter((r) => r.is_builtin));

async function loadCategories() {
  loadingCategories.value = true;
  try {
    categoryTree.value = await getCategories();
  } catch (e) {
    console.error('Failed to load categories', e);
  } finally {
    loadingCategories.value = false;
  }
}

async function loadRules() {
  loadingRules.value = true;
  try {
    allRules.value = await getRules(undefined, true);
  } catch (e) {
    console.error('Failed to load rules', e);
  } finally {
    loadingRules.value = false;
  }
}

async function loadSuggestions() {
  try {
    suggestions.value = await getSuggestions(3);
  } catch (e) {
    console.error('Failed to load suggestions', e);
  }
}

function openAddCategoryDialog(parent: CategoryItemOut | null) {
  parentCategory.value = parent;
  dialogCategoryTitle.value = parent ? `在「${parent.name}」下创建子分类` : '创建根分类';
  categoryForm.name = '';
  categoryForm.direction = parent ? (parent.direction as any) : 'expense';
  showCategoryDialog.value = true;
}

async function submitCategoryForm() {
  if (!categoryForm.name.trim()) {
    ElMessage.warning('请输入分类名称');
    return;
  }
  try {
    await createCategory({
      name: categoryForm.name.trim(),
      direction: categoryForm.direction,
      parent_id: parentCategory.value ? parentCategory.value.id : undefined,
    });
    ElMessage.success('分类创建成功');
    showCategoryDialog.value = false;
    await loadCategories();
    await appStore.fetchMeta();
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '分类创建失败');
    }
  }
}

function handleDeleteCategory(cat: CategoryItemOut) {
  deletingCategory.value = cat;
  deleteReassignToId.value = null;
  showDeleteCategoryDialog.value = true;
}

async function confirmDeleteCategory() {
  if (!deletingCategory.value) return;
  const needsTarget =
    deletingCategory.value.transaction_count > 0 ||
    (deletingCategory.value.rule_count || 0) > 0 ||
    Boolean(deletingCategory.value.children && deletingCategory.value.children.length > 0);
  if (needsTarget && !deleteReassignToId.value) {
    ElMessage.warning('该分类存在关联数据，请先选择迁移目标分类');
    return;
  }
  deletingCategoryLoading.value = true;
  try {
    await deleteCategory(deletingCategory.value.id, deleteReassignToId.value || undefined);
    ElMessage.success('分类已删除');
    showDeleteCategoryDialog.value = false;
    deletingCategory.value = null;
    await loadCategories();
    await loadRules();
    await appStore.fetchMeta();
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err?.message || '删除分类失败');
    }
  } finally {
    deletingCategoryLoading.value = false;
  }
}

function openCreateRuleDialog() {
  editingRuleId.value = null;
  ruleForm.name = '';
  ruleForm.field = 'counterparty';
  ruleForm.pattern_type = 'contains';
  ruleForm.pattern_value = '';
  ruleForm.category_id = null;
  ruleForm.priority = 100;
  showRuleDialog.value = true;
}

function openEditRuleDialog(r: RuleOut) {
  editingRuleId.value = r.id;
  ruleForm.name = r.name;
  ruleForm.field = r.field;
  ruleForm.pattern_type = r.pattern_type;
  ruleForm.pattern_value = r.pattern_value;
  ruleForm.category_id = r.category_id;
  ruleForm.priority = r.priority;
  showRuleDialog.value = true;
}

async function submitRuleForm() {
  const trimmedName = ruleForm.name.trim();
  const trimmedPattern = ruleForm.pattern_value.trim();
  if (!trimmedName) {
    ElMessage.warning('请输入规则名称');
    return;
  }
  if (!trimmedPattern || !ruleForm.category_id) {
    ElMessage.warning('请填写匹配内容并选择目标分类');
    return;
  }
  try {
    if (editingRuleId.value) {
      await updateRule(editingRuleId.value, {
        name: trimmedName,
        field: ruleForm.field,
        pattern_type: ruleForm.pattern_type,
        pattern_value: trimmedPattern,
        category_id: ruleForm.category_id,
        priority: ruleForm.priority,
      });
      ElMessage.success('规则更新成功');
    } else {
      await createRule({
        name: trimmedName,
        field: ruleForm.field,
        pattern_type: ruleForm.pattern_type,
        pattern_value: trimmedPattern,
        category_id: ruleForm.category_id,
        priority: ruleForm.priority,
      });
      ElMessage.success('规则创建成功');
    }
    showRuleDialog.value = false;
    await loadRules();
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '操作失败');
    }
  }
}

async function handleDeleteRule(r: RuleOut) {
  try {
    await ElMessageBox.confirm(`确定删除规则「${r.name}」吗？`, '删除规则确认', {
      type: 'warning',
    });
    await deleteRule(r.id);
    ElMessage.success('规则已删除');
    await loadRules();
  } catch {
    // Cancelled
  }
}

async function adoptSuggestion(item: RuleSuggestionOut) {
  const key = suggestionKey(item);
  if (adoptingKey.value !== null) return;
  adoptingKey.value = key;
  try {
    const res = await applyProposal({
      name: `${item.brand} 自动规则`,
      category_id: item.category_id,
      field: 'counterparty',
      pattern_type: 'contains',
      pattern_value: item.brand,
      priority: 100,
      apply_to_existing: true,
    });
    ElMessage.success(`规则已创建，批量更新了 ${res.updated_count} 笔历史交易`);
    await loadRules();
    await loadSuggestions();
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '应用建议失败');
    }
  } finally {
    adoptingKey.value = null;
  }
}

function openTesterDrawer() {
  testResult.value = null;
  testerForm.pattern_value = '';
  showTesterDrawer.value = true;
}

async function runRuleTest() {
  if (!testerForm.pattern_value.trim()) return;
  testingRule.value = true;
  try {
    testResult.value = await testRule({
      field: testerForm.field,
      pattern_type: testerForm.pattern_type,
      pattern_value: testerForm.pattern_value.trim(),
    });
  } catch (err: any) {
    if (!err?.toasted) {
      ElMessage.error(err.message || '测试失败');
    }
  } finally {
    testingRule.value = false;
  }
}

onMounted(() => {
  loadCategories();
  loadRules();
  loadSuggestions();
});
</script>

<style scoped>
.rules-view {
  height: calc(100vh - var(--topbar-height) - 40px);
}

.rules-container {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 16px;
  height: 100%;
}

/* Category Pane */
.category-pane {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.pane-header {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-2);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.pane-title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
}

.tree-wrapper {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
}

.cat-tree-node {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  font-size: 13px;
  padding-right: 4px;
}

.cat-name {
  color: var(--text);
  font-weight: 500;
}

.cat-count {
  font-size: 11px;
  color: var(--text-3);
  margin-left: 4px;
}

.node-actions {
  display: flex;
  gap: 4px;
  opacity: 0.8;
}

/* Rules Main */
.rules-main {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px 20px;
  overflow-y: auto;
}

.tab-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}

.tip-text {
  font-size: 12.5px;
  color: var(--text-3);
}

.suggestions-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}

.suggestion-card {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.card-brand {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}

.card-desc {
  font-size: 12.5px;
  color: var(--text-2);
  line-height: 1.5;
}

.card-actions {
  margin-top: auto;
  display: flex;
  justify-content: flex-end;
}

/* Tester Drawer */
.tester-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.tester-content .tip {
  font-size: 12.5px;
  color: var(--text-3);
}

.test-result-box {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.result-summary {
  font-size: 13.5px;
  color: var(--text);
}

.matched-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 200px;
  overflow-y: auto;
}

.matched-item {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px solid var(--border-2);
}
</style>
