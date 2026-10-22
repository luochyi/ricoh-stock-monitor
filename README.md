# 理光小程序官翻品库存监测

基于 Python 标准库的库存监测脚本，支持钉钉机器人通知、收藏商品规格库存监测，以及按配置规则创建一笔待付款订单。需要 Python 3.9+；macOS 后台服务使用 launchd 和 caffeinate。

## 当前规则

- 北京时间每天 **08:00（含）—18:00（不含）** 查询库存并发送通知，夜间暂停。
- 正常每轮完成后等待 30 秒；库存接口异常时退避。
- 上架列表监测全部官翻商品；收藏功能每 5 分钟同步官翻收藏，每轮检查各规格库存，包括未上架商品。
- 普通 **GR4／GR IV 静默**，**GR4 HDF／GR IV HDF 及其他型号继续通知**；列表、收藏及旧待发消息采用同一通知策略。
- 完整服务提供白天每小时健康报告，以及故障、恢复通知。
- `watch_order.py` 可为上架列表中名称包含 GR IIIx／GR3x 或 HDF 的商品创建一笔待付款订单，所有匹配商品合计 1 台、不设价格上限，使用第一条收货地址和默认发票。不会自动付款。收藏详情只触发通知，不进入下单候选。

## 仅监测上架列表

在脚本目录执行：

```sh
# 只读查询，不发送通知、不修改状态
python3 monitor.py --config config.ricoh.json --dry-run

# 在本地设置自己的机器人凭证
export DINGTALK_WEBHOOK='https://oapi.dingtalk.com/robot/send?access_token=替换为自己的令牌'
export DINGTALK_SECRET='替换为自己的加签密钥'

# 测试机器人消息
python3 monitor.py --test-notify

# 持续监测上架列表；不启用收藏、健康报告或自动下单
python3 monitor.py --config config.ricoh.json
```

如使用机器人关键词验证，关键词设为“理光库存”。不要将真实凭证写进受 Git 跟踪的文件。上述网络操作也受工作时段限制。

## 完整服务

运行 `watch_order.py` 会启用收藏监测、健康报告和自动下单。先准备本地凭证并核对规则，见 [自动下单说明](自动下单说明.md)。macOS 部署方式见 [后台服务说明](后台服务说明.md)。不要同时运行多个监测副本，尤其不要在不同目录并行启动，以免各自维护独立订单状态。

`config.ricoh.json` 为实际适配器配置；`config.example.json` 和 `sample.json` 仅用于通用 JSON 数据演示。`notification_policy.mute_plain_gr4` 控制普通 GR4 静默；收藏额外关注的商品 ID 在 `favorites.pinned_product_ids` 中配置。

## 库存与状态

列表查询逐页直到空页，失败或结构异常时保留旧库存。收藏详情按 `productValue` 中的规格库存比较，不以商品汇总库存代替；未上架库存不代表可以下单。首次有货、补货、售罄及库存变化按通知策略发送；未变化不重复发。商品从列表消失不直接视为库存归零。

库存和通知队列原子保存，重启后继续去重和重试。网络超时等情况下可能重复通知；轮询间隔内短暂补货也可能遗漏。小程序接口、签名协议或登录状态变化时可能需要更新适配或重新登录。

运行状态、凭证、订单、地址、发票资料、抓包及历史日志均不在仓库中。`.gitignore` 排除常用运行文件。

## 测试

```sh
python3 -m unittest discover -s . -q
```

测试覆盖签名、分页、库存事件、GR4/HDF 通知策略、收藏、工作时段、健康报告、下单校验和防重复提交，使用模拟接口及本地 HTTP 服务，不进行真实下单。

## 文件

- `monitor.py`：列表查询、签名、库存比较及钉钉通知。
- `favorites.py`：收藏同步和详情规格监测。
- `checkout.py` / `watch_order.py`：购买校验、订单状态和主循环。
- `operating_window.py` / `health.py`：北京工作时段及运行健康报告。
- `service_runner.py`：macOS 服务入口和日志轮转。
- `com.fst.ricoh-stock-monitor.plist.example`：需要替换路径的服务模板。
