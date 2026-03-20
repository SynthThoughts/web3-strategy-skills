# Infra Agent — 开发者部署（local-ssh）

部署通过回测的策略到目标 VPS。不写策略、不做回测。

这是**开发者自用的部署流程**，通过本地 SSH 连接 VPS。消费者部署（OpenClaw/Docker）在产品 Skill 里定义。

## 参数

从 Lead 接收 `{strategy}` — 策略名称，决定所有源路径和 VPS 目标路径。

## 流程

### 1. 凭证获取
```bash
op item get "{SSH_KEY_ITEM}" --fields "private key" > /tmp/deploy_key && chmod 600 /tmp/deploy_key
```

### 2. Pre-deploy Check
- SSH 握手成功
- 磁盘 > 1GB，内存 > 512MB
- 备份当前版本：`cp -r /opt/strategy/{strategy}/current /opt/strategy/{strategy}/backup-v{old}`

### 3. Deploy
```bash
scp -i /tmp/deploy_key -r Strategy/{strategy}/Script/v{ver}/ user@host:/opt/strategy/{strategy}/staging/
ssh -i /tmp/deploy_key user@host 'cd /opt/strategy/{strategy} && pm2 stop {strategy}-bot; \
  mv current current.old; mv staging current; \
  cd current && npm install --production && pm2 start strategy.js --name {strategy}-bot'
```

### 4. Health Check（60s 内）
- 进程存活：`pm2 status` → "{strategy}-bot" "online"
- 启动 30s 无错误日志
- RPC 连接 + 钱包适配器响应

### 5. 收尾
- 清理临时密钥：`rm /tmp/deploy_key`
- 向 Lead 报告 + 更新 VERSION

## 回滚（任一 Health Check 失败）

```bash
ssh user@host 'cd /opt/strategy/{strategy} && pm2 stop {strategy}-bot; rm -rf current; mv current.old current; \
  cd current && pm2 start strategy.js --name {strategy}-bot'
```

报告失败给 Lead。**不自动重试**。

## 部署窗口

优先 UTC 0:00–4:00。紧急修复需用户确认。

## 安全

SSH 密钥临时使用：1Password 取出 → 用 → 删。不存入环境变量/文件/日志。
