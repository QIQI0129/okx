# OKX Quant Pro (SWAP) — WS 行情驱动 + REST 下单 + 交易所托管 TP/SL

这是一套可直接运行的 OKX 合约量化系统（专业级 MVP）：
- **公共 WS**：订阅 `candle{bar}`（如 candle1m），自动重连 + ping/pong
- **私有 WS**：登录后订阅 `account/positions/orders`，实时更新净值/可用/持仓/订单状态
- **REST 私有接口**：下单（市价开仓）并携带 **TP/SL（交易所托管）**；支持撤单
- **风险控制**：
  - 冷却（cooldown）
  - 幂等（同一根K线同一信号不会重复下单）
  - **成交驱动**：只有 `orders` 推送 state=filled 才认为信号完成
  - **订单超时**：超时仍未 filled → 撤单；若已部分成交 → 撤单剩余并标记完成（避免重复开仓）
  - **日亏损熔断**：按新加坡时间自动跨天重置日基准（00:00），并自动解除前一天熔断

> 免责声明：合约交易高风险。请先模拟盘跑通并小额验证。

---

## 1) 项目结构

```
okx_quant/
  requirements.txt
  config.yaml
  README.md
  main.py
  utils/
  data/
  exchange/
  strategy/
  risk/
  execution/
```

---

## 2) Python 版本与虚拟环境

### 2.1 推荐 Python 版本
- Python **3.10+**（建议 3.11/3.12）
- 因为本项目使用 `zoneinfo`（内置时区库）做新加坡时间的日基准重置。

### 2.2 创建虚拟环境（Linux/macOS）
```bash
cd okx_quant

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### 2.3 创建虚拟环境（Windows PowerShell）
```powershell
cd okx_quant

py -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

---

## 3) 模拟盘（Demo Trading）配置

### 3.1 你需要准备
1) 在 OKX 创建 **模拟盘 API Key**（Demo）
2) 权限建议：`Read + Trade`
3) 配置 `config.yaml`：
- `env.demo: true`
- 填入 `auth.api_key/api_secret/passphrase`

### 3.2 模拟盘关键配置示例
```yaml
env:
  demo: true
  base_url_demo: "https://eea.okx.com"
  ws_public_demo: "wss://wseeapap.okx.com:8443/ws/v5/public"
  ws_private_demo: "wss://wseeapap.okx.com:8443/ws/v5/private"

auth:
  api_key: "你的模拟盘KEY"
  api_secret: "你的模拟盘SECRET"
  passphrase: "你的模拟盘PASSPHRASE"
```

### 3.3 启动
```bash
python main.py
```

你应该能看到：
- `Private WS login ok`
- `WS subscribed`（public）
- `BOOT OKX Quant Pro`

---

## 4) 实盘（Production）配置

### 4.1 你需要准备
1) 创建 **实盘 API Key**
2) 强烈建议：
- 限制 IP 白名单（如平台支持）
- 单独账户/子账户单独资金池运行量化
- 最小仓位、低杠杆先跑

### 4.2 实盘关键配置示例
```yaml
env:
  demo: false
  base_url_prod: "https://www.okx.com"
  ws_public_prod: "wss://ws.okx.com:8443/ws/v5/public"
  ws_private_prod: "wss://wseea.okx.com:8443/ws/v5/private"

auth:
  api_key: "你的实盘KEY"
  api_secret: "你的实盘SECRET"
  passphrase: "你的实盘PASSPHRASE"
```

---

## 5) 关键参数解释（务必理解）

### 5.1 逐仓与杠杆
- `account.td_mode: isolated`（逐仓）
- `account.leverage: 5`
启动时会调用 `set-leverage`。如果失败，程序会退出（避免在错误风控模式下交易）。

### 5.2 张数计算（按风险 sizing）
- 目标风险(USDT) = 可用USDT * `risk.risk_pct_per_trade`
- 每张止损亏损(USDT) ≈ `entry_price * ctVal * sl_pct`
- 张数 = 目标风险 / 每张亏损
- 再按 `lotSz` 向下取整并确保 >= `minSz`

> 注意：这是专业常用算法的 MVP 版本。更严谨可用账户净值与保证金占用做约束。

### 5.3 TP/SL（交易所托管）
下单时携带：
- `tpTriggerPx` + `tpOrdPx=-1`（触发后市价）
- `slTriggerPx` + `slOrdPx=-1`（触发后市价）
优点：程序宕机/断网也能执行止盈止损。

### 5.4 成交驱动（filled 才 done）
- 下单会生成 `clOrdId`，写入 SQLite kv：
  - `pending:<idem> = <clOrdId>`
  - `pending_cl:<clOrdId> = <idem>`
- 私有 WS `orders` 推送：
  - `state=filled` -> 写 `done:<idem>=1` 并清理 pending
  - `state=partially_filled/live` -> 继续等待
  - `state=canceled/rejected` -> 保留状态，OrderManager 可清理并允许重试

### 5.5 订单超时 + 部分成交处理
参数：
- `trade.order_timeout_sec`：默认 60 秒
- `trade.cancel_on_timeout: true`

策略：
- 超时仍未 `filled`：
  - 若 `accFillSz>0`（部分成交）：撤销剩余并标记 done（避免重复开仓）
  - 若 `accFillSz==0`：撤销并清理 pending，允许下一次信号重试

### 5.6 日基准自动重置（Asia/Singapore）
- 每次循环都会检测新加坡日期
- 跨天会：
  - `daily_base_equity = 当前 equity(totalEq)`
  - 清除 `halted`
- 日亏损熔断：
  - (base - equity) / base >= `risk.daily_loss_limit_pct` -> 写 `halted=1`

---

## 6) 运行与守护（推荐 systemd）

### 6.1 前台运行
```bash
python main.py
```

### 6.2 systemd 示例
创建：`/etc/systemd/system/okx-quant.service`
```ini
[Unit]
Description=OKX Quant Pro
After=network.target

[Service]
WorkingDirectory=/path/to/okx_quant
ExecStart=/path/to/okx_quant/.venv/bin/python main.py
Restart=always
RestartSec=3
User=youruser
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

启动：
```bash
sudo systemctl daemon-reload
sudo systemctl enable okx-quant
sudo systemctl start okx-quant
sudo systemctl status okx-quant
```

---

## 7) 故障排查

1) **签名/时间戳错误**
- 机器时间不准：务必启用 NTP（chrony/systemd-timesyncd）

2) **set_leverage 失败**
- API Key 权限不足、账户模式限制、instId 不正确
- 先在 OKX 确认能手动设置逐仓/杠杆

3) **WS 无数据**
- 网络限制或域名不可达
- 程序会 REST 兜底（public WS 断线不影响交易链路），但建议解决网络问题

4) **熔断后不交易**
- 当天触发熔断会写 `halted=1`
- 次日新加坡时间 00:00 会自动清除；或你手动 sqlite 删除该键

---

## 8) 下一步可增强
- 成交回报更细：部分成交时动态更新仓位、自动追单
- 订单超时策略更细：live 超时只撤单，partially_filled 超时按剩余比例再下补单/或直接止损
- 引入 Prometheus/告警（飞书/Telegram）


---

## 9) 本地 + VPN/代理 测试（Windows 常见）

如果你在 Windows 本机网络无法直连 OKX（DNS_FAIL/ConnectTimeout），建议使用 Clash/v2rayN 等开启 **HTTP 代理端口**（例如 `127.0.0.1:7890`），然后在本项目启用代理。

### 9.1 直接本机运行（不使用 Docker）
在 `config.yaml` 设置：
```yaml
proxy:
  enabled: true
  url: "http://127.0.0.1:7890"
```

### 9.2 Docker 运行（重要）
容器里的 `127.0.0.1` 是容器自身，不是宿主机，所以要改成：
```yaml
proxy:
  enabled: true
  url: "http://host.docker.internal:7890"
```

并建议同时传入环境变量（requests 对这套最通用）：

Windows CMD 示例：
```bat
docker run -it --rm ^
  --name okx-quant ^
  -e PROXY_URL=http://host.docker.internal:7890 ^
  -e HTTPS_PROXY=http://host.docker.internal:7890 ^
  -e HTTP_PROXY=http://host.docker.internal:7890 ^
  -e NO_PROXY=localhost,127.0.0.1 ^
  -v %cd%\config.yaml:/app/config.yaml ^
  -v %cd%\state.db:/app/state.db ^
  okx-quant:latest
```

> WS 代理：本项目使用 websocket-client 的 HTTP proxy 参数（最稳定）；SOCKS5 代理不保证兼容。


---

## 常见问题（你这次遇到的都在这里）

### 1) Windows 报 ZoneInfoNotFoundError / No module named tzdata
Windows 下 Python 的 zoneinfo 需要 tzdata 包。本项目 requirements.txt 已加入 tzdata：
```bash
pip install -r requirements.txt
```

### 2) 下单“看似成功”，但 60 秒后查不到订单
OKX 下单接口有批量返回语义：顶层 code=0 也可能 data[0].sCode!=0。
本项目已在 exchange/okx_rest.py 的 _request() 对 /api/v5/trade/* 自动校验 sCode，
失败会当场抛错，避免进入超时逻辑里“查一个不存在的订单”。

### 3) 51603 Order does not exist（查单）
市价单可能很快进入历史订单，/trade/order 查不到。
本项目提供 get_order_anywhere()：当前订单查不到会自动去 orders-history / archive 分页兜底。

### 4) Docker + 代理
- Windows / Mac Docker: 代理地址一般用 `http://host.docker.internal:7890`
- Linux Docker: 用宿主机网关（通常是 `http://172.17.0.1:7890`），或用 `--network host`


## 常见坑位（已修复/需正确配置）

- **K线订阅 60018**：`candle1m` 需要连接 `ws/v5/business`，不是 `ws/v5/public`。
- **模拟盘 50119 API key doesn't exist**：必须使用“模拟交易”创建的 API Key，并保持 `env.demo: true`（程序会自动加 `x-simulated-trading: 1`）。
- **WS 代理报 Only http/socks...**：请确保代理 URL 用 `http://`，并已强制 `proxy_type=http`。
