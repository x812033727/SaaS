# 自動部署（GitHub Webhook → 乾淨重佈）

push/merge 到 `main` 時，GitHub 透過 webhook 通知主機，自動把 `/opt/saas`
同步到最新 `main` 並乾淨重佈。

## 組成（主機 srv1501416，非容器）
- **GitHub webhook** → `https://saas.aibubu.cloud/__deploy/github`（push 事件、HMAC SHA-256 簽章）。
- **nginx**：`location = /__deploy/github` 反代到 `127.0.0.1:9001`。
- **listener**：`/usr/local/bin/saas-deploy-webhook.py`（systemd `saas-deploy-webhook.service`，僅綁 loopback）。
  驗 `X-Hub-Signature-256`，只接 `refs/heads/main` 的 push，背景觸發部署腳本。
- **部署腳本**：`/usr/local/bin/saas-deploy.sh`（flock 串行化，版控來源 `docker/saas-deploy.sh`）：
  `git fetch` → **等 CI 綠燈** → `git reset --hard origin/main` → `docker-compose up -d --build` → `docker image prune -f`。
  記錄於 `/var/log/saas-deploy.log`。

## CI 門檻（.github/workflows/ci.yml）
- push main / PR 都會在 GitHub Actions 跑全量 `run_tests.sh`。
- 部署腳本用 `gh api .../commits/{sha}/check-runs` 輪詢 origin/main 的 CI 結果：
  綠燈才 reset+rebuild；**紅燈或 15 分鐘逾時則中止本次部署**（舊版本繼續跑）。
- 腳本更新後需重新安裝到主機：
  ```bash
  install -m 755 /opt/saas/docker/saas-deploy.sh /usr/local/bin/saas-deploy.sh
  ```

## 手動觸發
```bash
/usr/local/bin/saas-deploy.sh >> /var/log/saas-deploy.log 2>&1
# 緊急繞過 CI 門檻（僅限救火）：
SAAS_DEPLOY_SKIP_CI_GATE=1 /usr/local/bin/saas-deploy.sh >> /var/log/saas-deploy.log 2>&1
```

## 密鑰
webhook 密鑰存於 `/etc/saas-deploy/webhook.secret`（0600）；與 GitHub hook 設定一致。
