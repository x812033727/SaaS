# 收款上線 checklist（stub → ECPay 正式）

## 第 0 步:就緒檢查（每次上線前先跑）
```bash
cd /opt/saas && docker-compose exec web python -m saas_mvp.ops.check_readiness
```
逐項 PASS/WARN/FAIL + 修復提示;有 FAIL 修完再往下走。


## 0. 商務前置（lead time 最長，第一天就送件）
- [ ] 申請綠界**正式商店**（需公司/行號統編；個人可評估藍新或個人賣家方案）
- [ ] 向綠界申請開通「**信用卡定期定額**」功能（需另外申請，非預設開通）
- [ ] 確認撥款帳戶與手續費率

## 1. 平台後台設定（不需主機指令）

1. 以平台管理員登入「平台管理 → 金流設定」。
2. 先選測試環境，輸入測試 MerchantID、HashKey、HashIV 並儲存。
3. 按「執行設定自我檢查」，再完成下方 stage 全流程。
4. 正式商店核准後切換正式環境，改填正式憑證。

憑證會加密保存並立即生效，不需修改 `.env`、Docker 重建或重啟服務。環境變數
`SAAS_PAYMENT_PROVIDER` / `SAAS_ECPAY_*` 僅保留作災難復原備援，後台設定優先。

## 2. 綠界後台設定
- [ ] 付款結果通知網址允許清單加入 `https://saas.aibubu.cloud/payments/ecpay/*`
- [ ] 確認 nginx 對 `/payments/ecpay/*` 無額外認證/限流（程式端為公開 router + 驗簽）

## 3. stage 全流程演練（測試商店 2000132，SAAS_ECPAY_ENV=stage）
- [ ] `/ui/plan` 訂閱標準版 → 綠界測試付款頁 → 首期授權
- [ ] 確認 `subscribe-callback` 後 `tenant.plan` 已改、`/ui/billing` 出現第 1 期成功明細
- [ ] `/ui/plan` 退訂 → 確認 `cancel_period` 成功、方案保留至寬限期（trial 欄位）
- [ ] 換方案（standard→pro）：舊訂閱 cancelled、新訂閱 active
- [ ] 模擬回調驗簽失敗（改一個欄位重放）→ 回 `0|CheckMacValue Error`、無任何寫入

## 4. 上線後首筆
- [ ] 用自己的信用卡訂閱一次，全流程走通並對帳（綠界後台 vs SubscriptionCharge）
- [ ] 首月底人工核對每期扣款 vs 綠界後台（定期定額期扣在 stage 無法快轉，正式首月必對）

## 5. 已知界線（刻意延後）
- 電子發票：月營收達起徵點/10+ 付費店家後再接綠界 B2C 發票 API（service 掛
  activate/record_period hook 即可，架構已留位）；在此之前提供 /ui/billing
  扣款明細作為收據。
- 期扣失敗通知店家：email 通知隨 B3（mailer）補上；目前失敗會降 free 並記
  PlanChangeHistory + warning log。
- 退款：個案人工處理（綠界後台），暫不做自助退款 UI。
