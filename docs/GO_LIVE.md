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

## 5. 電子發票驗收
- [ ] 平台管理員到「平台管理 → 發票設定」先以測試環境完成自我檢查，再切換正式憑證。
- [ ] 店家 owner 到「帳務設定 → 帳單」選擇個人、公司統編、手機條碼／自然人憑證或愛心捐贈資料。
- [ ] 完成一筆低額正式扣款，核對發票號碼、買受資訊、載具／捐贈狀態與綠界後台一致。
- [ ] 演練開立失敗重試及作廢；確認原發票使用開立當下的資料快照，不受後續設定修改影響。

## 6. 已知界線（刻意延後）
- 期扣失敗會降 free、保留資料、寫入 PlanChangeHistory 並寄送 Email 通知；正式驗收時需演練失敗信件與寄送重試。
- 退款：個案人工處理（綠界後台），暫不做自助退款 UI。
