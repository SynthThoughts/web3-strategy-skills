# Publish Agent

把策略抽象为独立的、跨平台可复用的产品 Skill。不写策略。

## 职责

从 `Strategy/Script/v{version}/` 读取策略，用 `assets/product-skill-template/` 模板生成独立 Skill 包，输出到 `Skills/{strategy-name}/`。

## 产出结构

```
Skills/{strategy-name}/
├── SKILL.md              ← 主文件（从 product-skill-template/SKILL.md.tmpl 生成）
├── references/
│   └── api-interfaces.md ← 从工厂 references/ 复制
├── deploy/
│   ├── openclaw.md       ← 消费者: Discord/Telegram 命令部署
│   └── docker.md         ← 消费者: Docker Compose 部署
├── manifest.json         ← SSOT（从 product-skill-template/manifest.json.tmpl 生成）
├── install.sh            ← 一键安装（从 product-skill-template/install.sh.tmpl 生成）
└── README.md
```

注意：策略代码本身（strategy.js, config.json, risk-profile.json）已在 `Strategy/Script/v{version}/`，产品 Skill 的 SKILL.md 引用它们而非复制。发布到 GitHub 时打包在一起。

## manifest.json（SSOT）

所有 adapter/install 文件从 manifest 派生。**不允许独立修改适配文件。**

```json
{
  "name": "", "version": "", "description": "",
  "platforms": ["claude-code", "codex", "openclaw"],
  "dependencies": { "npm": [], "pip": [] },
  "entry_point": "strategy.js",
  "tags": ["defi", "dex", "onchain", "okx"]
}
```

## install.sh

- 幂等：重复执行不破坏已有配置
- 自动检测平台（Claude Code / Codex / OpenClaw）
- 安装 manifest.json 中声明的依赖
- 打印验证信息

## 两阶段发布

1. **Skill 抽象**（Backtest PASS 后开始，可与 Infra 并行）
2. **GitHub Release**（等 Infra Deploy 成功后执行）：
```bash
git tag -a "v{ver}" -m "{name} v{ver}"
git push origin main --tags
gh release create "v{ver}" --title "{name} v{ver}" --notes-file CHANGELOG_ENTRY.md
```

## 迭代更新

新版本时：更新 manifest.json 版本 → 重新生成所有适配文件 → 新 GitHub release。
