# 自動部署（GitHub Webhook → 乾淨重佈）

push/merge 到 `main` 時，GitHub 透過 webhook 通知主機，自動把 `/opt/saas`
同步到最新 `main` 並乾淨重佈。

## 組成（主機 srv1501416，非容器）
- **GitHub webhook** → `https://saas.aibubu.cloud/__deploy/github`（push 事件、HMAC SHA-256 簽章）。
- **nginx**：`location = /__deploy/github` 反代到 `127.0.0.1:9001`。
- **listener**：`/usr/local/bin/saas-deploy-webhook.py`（systemd `saas-deploy-webhook.service`，僅綁 loopback）。
  驗 `X-Hub-Signature-256`，只接 `refs/heads/main` 的 push，背景觸發部署腳本。
- **部署腳本**：`/usr/local/bin/saas-deploy.sh`（flock 串行化）：
  `git fetch` → `git reset --hard origin/main` → `docker-compose up -d --build` → `docker image prune -f`。
  記錄於 `/var/log/saas-deploy.log`。

## 手動觸發
```bash
/usr/local/bin/saas-deploy.sh >> /var/log/saas-deploy.log 2>&1
```

## 密鑰
webhook 密鑰存於 `/etc/saas-deploy/webhook.secret`（0600）；與 GitHub hook 設定一致。
