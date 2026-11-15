import { describe, it, expect } from 'vitest';
import { buildProfileYaml } from '@/utils/profileYaml';

describe('buildProfileYaml', () => {
  it('generates valid YAML for signed amount mode', () => {
    const yaml = buildProfileYaml({
      profileId: 'cmb_debit_custom',
      profileName: '招商银行储蓄卡流水',
      profileBank: '招商银行',
      accountKind: 'bank_debit',
      headerRowIndex: 1,
      dataStartRowIndex: 2,
      amountMode: 'signed',
      fieldMap: {
        txn_time: '交易时间',
        amount: '金额(元)',
        counterparty: '交易对方',
        description: '商品说明',
        channel: '支付方式',
        card_tail: '卡号',
      },
    });

    expect(yaml).toContain('id: "cmb_debit_custom"');
    expect(yaml).toContain('account_kind: "debit"');
    expect(yaml).toContain('header_row_index: 0');
    expect(yaml).toContain('data_start_offset: 1');
    expect(yaml).toContain('column_mapping:');
    expect(yaml).toContain('date: "交易时间"');
    expect(yaml).toContain('amount: "金额(元)"');
    expect(yaml).toContain('counterparty: "交易对方"');
    expect(yaml).toContain('amount_mode:');
    expect(yaml).toContain('type: "signed"');
    expect(yaml).toContain('amount_col: "金额(元)"');
  });

  it('generates valid YAML for split amount mode with credit kind', () => {
    const yaml = buildProfileYaml({
      profileId: 'ccb_credit_custom',
      profileName: '建设银行信用卡对账单',
      profileBank: '建设银行',
      accountKind: 'bank_credit',
      headerRowIndex: 3,
      dataStartRowIndex: 5,
      amountMode: 'split',
      fieldMap: {
        txn_time: '交易日期',
        counterparty: '对方户名',
        description: '摘要',
      },
      splitExpenseCol: '借方金额',
      splitIncomeCol: '贷方金额',
    });

    expect(yaml).toContain('id: "ccb_credit_custom"');
    expect(yaml).toContain('account_kind: "credit"');
    expect(yaml).toContain('header_row_index: 2');
    expect(yaml).toContain('data_start_offset: 2');
    expect(yaml).toContain('expense_amount: "借方金额"');
    expect(yaml).toContain('income_amount: "贷方金额"');
    expect(yaml).toContain('type: "split"');
    expect(yaml).toContain('expense_col: "借方金额"');
    expect(yaml).toContain('income_col: "贷方金额"');
  });

  it('generates valid YAML for flag amount mode', () => {
    const yaml = buildProfileYaml({
      profileId: 'pab_flag_custom',
      profileName: '平安银行流水',
      profileBank: '平安银行',
      accountKind: 'bank_debit',
      headerRowIndex: 1,
      dataStartRowIndex: 2,
      amountMode: 'flag',
      fieldMap: {
        txn_time: '记账时间',
        amount: '发生额',
        counterparty: '商户名称',
        description: '附言',
      },
      flagCol: '借贷标志',
      flagExpenseVal: '借',
      flagIncomeVal: '贷',
    });

    expect(yaml).toContain('id: "pab_flag_custom"');
    expect(yaml).toContain('account_kind: "debit"');
    expect(yaml).toContain('date: "记账时间"');
    expect(yaml).toContain('amount: "发生额"');
    expect(yaml).toContain('direction: "借贷标志"');
    expect(yaml).toContain('type: "flag"');
    expect(yaml).toContain('amount_col: "发生额"');
    expect(yaml).toContain('direction_col: "借贷标志"');
    expect(yaml).toContain('flag_col: "借贷标志"');
    expect(yaml).toContain('"借": "expense"');
    expect(yaml).toContain('"贷": "income"');
  });

  it('safely escapes special characters in field names', () => {
    const yaml = buildProfileYaml({
      profileId: 'special_test',
      profileName: '带"引号"与:冒号的配置',
      accountKind: 'wallet',
      headerRowIndex: 1,
      dataStartRowIndex: 2,
      amountMode: 'signed',
      fieldMap: {
        txn_time: '日期:时间',
        amount: '金额 "RMB"',
      },
    });

    expect(yaml).toContain('name: "带\\"引号\\"与:冒号的配置"');
    expect(yaml).toContain('account_kind: "debit"');
    expect(yaml).toContain('date: "日期:时间"');
    expect(yaml).toContain('amount: "金额 \\"RMB\\""');
  });

  it('emits metadata_extraction card_tail rule and default when defaultCardTail is provided', () => {
    const yaml = buildProfileYaml({
      profileId: 'cmb_top_acct',
      profileName: '招行顶部账户模板',
      profileBank: '招商银行',
      accountKind: 'bank_debit',
      fileType: 'pdf',
      headerRowIndex: 10,
      dataStartRowIndex: 14,
      amountMode: 'signed',
      fieldMap: {
        txn_time: '记账日期',
        amount: '交易金额',
        counterparty: '对手信息',
        description: '交易摘要',
      },
      defaultCardTail: '1234',
    });

    expect(yaml).toContain('metadata_extraction:');
    expect(yaml).toContain('card_tail:');
    expect(yaml).toContain('group: 1');
    expect(yaml).toContain('default: "1234"');
  });
});
