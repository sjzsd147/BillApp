# 账单助手（BillApp）

本地优先的多来源个人账单综合管理桌面应用，支持微信、支付宝及多家银行（CSV / XLSX / PDF）账单导入、跨渠道去重与转账/退款配对、规则与 AI 辅助分类、多币种汇率换算以及多维消费统计看板。所有业务数据默认仅保存在本机 SQLite 数据库中。

## 核心功能

- **多来源账单导入与字段映射向导**
  - 原生支持微信支付、支付宝账单解析，支持加密 PDF / ZIP 解密预览与导入批次 SHA-1 去重、整批回滚。
  - 遇到自定义银行格式（如各银行 CSV / XLSX / PDF 对账单）时自动进入全屏 4 步「字段映射向导」（支持 AI 辅助生成字段映射或 AI 整单解析），可将自定义映射保存为可复用 YAML 配置。
- **跨渠道去重与对账（待确认队列）**
  - 自动识别同一笔消费在第三方支付与银行卡流水之间的重复记录、账户间转账/提现/信用卡还款配对以及退款冲抵，支持人工确认、拆分或加入拦截规则。
- **两级分类体系 + 规则引擎 + AI 辅助分类**
  - 预置完整 2 级收支分类树，支持按商户/关键词/正则配置高优先级自动规则，以及基于历史修改的自动规则建议。
  - 可选接入任意 OpenAI 兼容接口，在「设置 → 大语言模型 (AI 辅助)」中配置接口地址、密钥并在线拉取或自定义模型。
- **统计看板与交易明细**
  - 提供总支出（净额）、总收入、结余、储蓄率、月度收支趋势、支出分类构成、商户 Top 10、账户出资分布及消费时段热力图。
  - 支持按时间、账户、收支类型、币种、分类检索明细，支持批量改分类、导出 Excel（`.xlsx`）及本地数据库备份/恢复。
- **跨平台 Electron 桌面壳**
  - 基于 Electron 封装原生桌面应用（支持 Windows / macOS / Linux），内置单实例锁、原生“另存为”文件对话框，以及随桌面窗口启停的本地 FastAPI 无头 Sidecar 后端。
  - 同时保留 `--browser` / `--no-browser` 参数以支持直接在浏览器或无头服务模式下运行。

## 技术栈

- **桌面壳**：Electron、electron-builder
- **后端**：Python 3、FastAPI、Uvicorn、SQLite（WAL 模式）、PyMuPDF、OpenPyXL、PyYAML、httpx、PyInstaller
- **前端**：Vue 3、TypeScript、Vite、Element Plus、Pinia、Vue Router、ECharts

## 目录结构

```text
Bill/
├── backend/
│   ├── billapp/          # FastAPI 后端、导入/去重/分类/LLM/统计引擎与 Sidecar 启动器
│   ├── tests/            # pytest 测试套件
│   └── requirements.txt
├── frontend/
│   ├── src/              # Vue 3 + Element Plus + ECharts 前端源码与 Vitest 单测
│   └── package.json
├── electron/
│   ├── src/              # Electron 主进程 (main.cjs)、预加载桥接 (preload.cjs) 与后端管理 (backend.cjs)
│   ├── scripts/          # 开发联动启动脚本与图标生成工具
│   ├── tests/            # Node 测试套件
│   ├── electron-builder.yml
│   └── package.json
└── build/
    ├── billapp.spec      # PyInstaller 无头后端 Sidecar 打包配置
    ├── build.mjs         # 跨平台一键构建编排脚本 (Win / macOS / Linux)
    └── build.ps1         # Windows PowerShell 构建入口
```

## 本地开发运行

### 1. 安装依赖

```powershell
# 后端依赖
pip install -r backend/requirements.txt

# 前端依赖
cd frontend && npm install && cd ..

# Electron 桌面壳依赖
cd electron && npm install && cd ..
```

### 2. 以 Electron 桌面开发模式运行（热重载）

```powershell
cd electron
npm run dev
```

该命令会自动在空闲回环端口启动本地 FastAPI 后端、启动 Vite 开发服务器（`http://127.0.0.1:5173`）并拉起 Electron 窗口；关闭窗口会自动清理全部子进程。

### 3. 仅启动后端 / 浏览器模式

```powershell
# 仅启动无头 HTTP 后端
python backend/billapp/launcher.py --no-browser --port 8000

# 使用系统默认浏览器打开
python backend/billapp/launcher.py --browser --port 8000
```

## 版本管理

项目根目录的 `version.json` 是全仓唯一版本号配置源：

- Python 后端（含 PyInstaller 打包产物）、Vue 前端及全部单元测试均动态读取 `version.json`，无需手动修改代码文件。
- 升级版本只需执行一条同步命令，即可自动更新 `version.json` 以及 Electron / 前端所需的 `package.json` 与 `package-lock.json`：

```powershell
# 一键更新并同步全仓版本号
node build/sync-version.mjs 1.3.0

# 仅根据 version.json 同步清单，或校验一致性
node build/sync-version.mjs
node build/sync-version.mjs --check
```

## 多平台桌面打包

在项目根目录执行跨平台构建脚本（依次完成：前端生产构建 → PyInstaller 打包 `billapp-backend` Sidecar → Sidecar 自检 → `electron-builder` 打包 → Electron 端到端冒烟验证）：

```powershell
# 输出未压缩桌面目录（快速验证）
node build/build.mjs --dir

# 分别构建对应平台安装包（需在对应系统环境运行）
node build/build.mjs --win     # Windows (NSIS 安装包 + Portable)
node build/build.mjs --mac     # macOS (DMG + ZIP Universal)
node build/build.mjs --linux   # Linux (AppImage + deb)

# Windows PowerShell 便捷入口
powershell -ExecutionPolicy Bypass -File .\build\build.ps1 -DirOnly
```

## 数据与日志位置

- **默认数据库**：
  - Windows：`%APPDATA%\BillApp\bill.db`
  - macOS：`~/Library/Application Support/BillApp/bill.db`
  - Linux：`$XDG_DATA_HOME/BillApp/bill.db`（默认 `~/.local/share/BillApp/bill.db`）
  - 支持通过启动参数 `--db <路径>` 或环境变量 `BILLAPP_DB_PATH` 覆盖
- **运行日志**：同数据目录下的 `launcher.log`（后端运行日志，自动轮转）与 `desktop.log`（Electron 桌面壳与更新日志）；可在应用内「设置 → 关于与数据 → 运行日志与诊断」一键脱敏预览或导出 `.zip` 诊断包 / `.log` 文本日志。

## 运行测试

```powershell
# 后端单元测试与 API 集成测试
python -m pytest backend/tests -q

# 前端类型检查与单测
cd frontend
npx vue-tsc --noEmit
npm test

# Electron 后端管理单元与生命周期测试
cd electron
npm test
```
