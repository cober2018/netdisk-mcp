# netdisk-ingest MCP

给闲鱼上架流程用的网盘入库服务。它跑在 OpenClaw 那台机器上，不依赖这台开发机，也不调用闲鱼接口。

输入一条夸克或百度分享链接，它会：

1. 识别网盘，读出标题和文件列表。
2. 按 `directory_rules.json` 把资源放进自己的目录：`{根目录}/{分类}/{资源标题}`。
3. 等转存完成，只用这个资源目录的新文件 ID 创建**自己的永久分享**。
4. 回读新链接，确认里面有文件。

返回里的 `share_url` 和 `passcode` 才是后面封面和闲鱼自动发货要用的内容。`source_url` 是别人的链接，不能上架。

封面、闲鱼发布、自动发货、掉线重登都不在这个 MCP 里。

依赖的是 MCP Python SDK 2（`MCPServer`）。参考仓库里的 `FastMCP` 写法是 SDK 1，不能混用。

## 生产配置

凭证只放宿主机环境变量或宿主机文件，不进 git，不写进 Skill。夸克走官方开放平台 OAuth，不使用网页 Cookie。

```bash
cd netdisk-mcp
python3 -m pip install -e .
cp .env.example .env
cp directory_rules.example.json /opt/openclaw/directory_rules.json
```

OpenClaw 注册示例：

```json
{
  "mcpServers": {
    "netdisk-ingest": {
      "command": "netdisk-ingest-mcp",
      "env": {
        "QUARK_ACCESS_TOKEN": "生产机上的夸克 access_token",
        "QUARK_REFRESH_TOKEN": "生产机上的夸克 refresh_token",
        "QUARK_DEVICE_ID": "官方登录返回的 device_id",
        "BAIDU_NETDISK_ACCESS_TOKEN": "生产机上的百度 access_token",
        "NETDISK_RULES_PATH": "/opt/openclaw/directory_rules.json",
        "NETDISK_MIN_INTERVAL_SECONDS": "3"
      }
    }
  }
}
```

也可以不把凭证写进 MCP 配置。百度用 `BAIDU_NETDISK_ACCESS_TOKEN_FILE`。夸克用 `QUARK_ACCESS_TOKEN_FILE`、`QUARK_REFRESH_TOKEN_FILE` 和 `QUARK_DEVICE_ID`。如果生产机已经用官方 CLI 登录过，不设置这些变量时会读取 `~/.quarkclouddrive/config.json`。access_token 过期后用 refresh_token 轮换，新令牌写入 `QUARK_OPEN_TOKEN_FILE`（默认 `.secrets/quark_open_token.json`），之后以这个文件为准。

夸克的列目录、建目录、分享详情、转存和创建分享都调用官方接口。转存目录仍由 `directory_rules.json` 算出，并作为 `to_pdir_fid` 传入，不会落到官方的默认目录。分享固定 `expired_type=1`（永久）。私密提取码仍由夸克生成。

百度应用必须在开放平台开通分享转存和创建分享。没开通时，转存会失败并说明原因，不会假装成功。

`NETDISK_BAIDU_SHARE_PWD` 留空是公开分享。要提取码就设 4 位，并让自动发货模板带上返回的 `passcode`。这里没有 `1234` 和 7 天这种默认值。

## 工具

| 工具 | 作用 |
|---|---|
| `provider_status` | 只报告夸克开放平台授权和百度 token 是否已配置，不返回内容 |
| `inspect_share_link` | 只读，给出建议目录 |
| `ingest_share_link` | 转存并创建自己的永久分享 |
| `create_share_for_saved_folder` | 转存已成功、分享失败时，对那个资源目录补分享 |
| `verify_share_link` | 回读分享，确认里面有文件 |
| `list_directory` | 列自己的目录，不会顺手创建目录 |

`ingest_share_link` 发现目标资源目录已经有文件时会停下，避免把旧文件和新文件打成同一条分享。

## 分享规则

- 先转存，再分享转存后的目录。原链接不算你的拉新。
- 夸克 `expired_type` 固定为 `1`（永久）。私密分享的提取码由夸克生成，调用方不能指定。
- 百度 `period` 固定为 `0`（永久）。返回如果不是永久，这条链接会被丢掉。
- 一次只分享一个资源目录，不分享整个分类目录。
- 创建成功之后还会回读。空目录不会交给上架。
- 接口没有单独的拉新字段。账号是否算拉新，要在夸克或百度的推广后台用真实新用户转存核对。这个 MCP 保证的是「自己的、永久的、可转存的分享」，不保证佣金归因。

## 本地检查

不连网盘：

```bash
cd netdisk-mcp
PYTHONPATH=. python3 -m unittest tests/test_flow.py
```

拆仓库时把整个 `netdisk-mcp/` 目录拿走即可。它不 import 闲鱼项目里的代码。
