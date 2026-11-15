/**
 * Declarative Bank Statement Profile YAML Generator
 * Complies strictly with backend BankProfile schema (backend/billapp/importers/profile_engine.py).
 */

export interface ProfileYamlParams {
  profileId: string;
  profileName: string;
  profileBank?: string;
  accountKind: string; // 'bank_debit' | 'bank_credit' | 'wallet' | 'debit' | 'credit'
  fileType?: string;
  currency?: string;
  headerRowIndex: number; // 1-based index from UI
  dataStartRowIndex: number; // 1-based index from UI
  amountMode: 'signed' | 'split' | 'flag';
  fieldMap: {
    txn_time: string;
    amount?: string;
    counterparty?: string;
    description?: string;
    channel?: string;
    card_tail?: string;
  };
  splitExpenseCol?: string;
  splitIncomeCol?: string;
  flagCol?: string;
  flagExpenseVal?: string;
  flagIncomeVal?: string;
  defaultCardTail?: string;
}

export function buildProfileYaml(params: ProfileYamlParams): string {
  // 1. account_kind mapping: backend accepts only 'debit' or 'credit'
  const mappedAccountKind =
    params.accountKind === 'bank_credit' || params.accountKind === 'credit'
      ? 'credit'
      : 'debit';

  // 2. table detection row indices (0-based for header_row_index, 1-based offset for data_start_offset)
  const headerIdx = Math.max(0, (params.headerRowIndex || 1) - 1);
  const dataStartOffset = Math.max(1, (params.dataStartRowIndex || 2) - (params.headerRowIndex || 1));
  const bank = (params.profileBank || '').trim() || '自定义机构';
  const fileType = params.fileType || 'auto';
  const currency = params.currency || 'CNY';

  const headerKeywords = Array.from(
    new Set(
      [
        params.fieldMap.txn_time,
        params.amountMode === 'split' ? params.splitExpenseCol : params.fieldMap.amount,
        params.amountMode === 'split' ? params.splitIncomeCol : undefined,
        params.amountMode === 'flag' ? params.flagCol : undefined,
        params.fieldMap.counterparty,
        params.fieldMap.description,
      ]
        .map((s) => (s || '').trim())
        .filter(Boolean)
    )
  );

  const lines: string[] = [
    `id: ${JSON.stringify(params.profileId.trim())}`,
    `name: ${JSON.stringify(params.profileName.trim())}`,
    `bank: ${JSON.stringify(bank)}`,
    `account_kind: ${JSON.stringify(mappedAccountKind)}`,
    `file_type: ${JSON.stringify(fileType)}`,
    `currency: ${JSON.stringify(currency)}`,
    'table_detection:',
    `  header_row_index: ${headerIdx}`,
    `  data_start_offset: ${dataStartOffset}`,
    ...(headerKeywords.length > 0
      ? ['  header_row_keywords:', ...headerKeywords.map((k) => `    - ${JSON.stringify(k)}`)]
      : []),
    ...(params.defaultCardTail && /^\d{4}$/.test(params.defaultCardTail.trim())
      ? [
          'metadata_extraction:',
          '  card_tail:',
          `    pattern: ${JSON.stringify('(?:账\\s*号|卡\\s*号|尾\\s*号)[^\\r\\n]{0,30}(\\d{4})')}`,
          '    group: 1',
          `    default: ${JSON.stringify(params.defaultCardTail.trim())}`,
        ]
      : []),
    'column_mapping:',
    `  date: ${JSON.stringify(params.fieldMap.txn_time || '')}`,
  ];

  // 3. column_mapping based on amount_mode
  if (params.amountMode === 'signed') {
    lines.push(`  amount: ${JSON.stringify(params.fieldMap.amount || '')}`);
  } else if (params.amountMode === 'split') {
    lines.push(`  expense_amount: ${JSON.stringify(params.splitExpenseCol || '')}`);
    lines.push(`  income_amount: ${JSON.stringify(params.splitIncomeCol || '')}`);
  } else if (params.amountMode === 'flag') {
    lines.push(`  amount: ${JSON.stringify(params.fieldMap.amount || '')}`);
    lines.push(`  direction: ${JSON.stringify(params.flagCol || '')}`);
  }

  lines.push(`  counterparty: ${JSON.stringify(params.fieldMap.counterparty || '')}`);
  lines.push(`  description: ${JSON.stringify(params.fieldMap.description || '')}`);
  lines.push(`  channel: ${JSON.stringify(params.fieldMap.channel || '')}`);
  if (params.fieldMap.card_tail) {
    lines.push(`  card_tail: ${JSON.stringify(params.fieldMap.card_tail)}`);
  }

  // 4. amount_mode structured configuration
  lines.push('amount_mode:');
  if (params.amountMode === 'signed') {
    lines.push('  type: "signed"');
    lines.push(`  amount_col: ${JSON.stringify(params.fieldMap.amount || '')}`);
  } else if (params.amountMode === 'split') {
    lines.push('  type: "split"');
    lines.push(`  expense_col: ${JSON.stringify(params.splitExpenseCol || '')}`);
    lines.push(`  income_col: ${JSON.stringify(params.splitIncomeCol || '')}`);
  } else if (params.amountMode === 'flag') {
    lines.push('  type: "flag"');
    lines.push(`  amount_col: ${JSON.stringify(params.fieldMap.amount || '')}`);
    lines.push(`  direction_col: ${JSON.stringify(params.flagCol || '')}`);
    lines.push(`  flag_col: ${JSON.stringify(params.flagCol || '')}`);
    lines.push('  flag_mapping:');
    lines.push(`    ${JSON.stringify(params.flagExpenseVal || '支出')}: "expense"`);
    lines.push(`    ${JSON.stringify(params.flagIncomeVal || '收入')}: "income"`);
  }

  return lines.join('\n');
}
