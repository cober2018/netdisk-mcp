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
| `inspect_share_tree` | 只读。递归列出夸克分享目录树，不转存 |
| `selective_ingest` | 只转存夸克分享里选中的路径，再创建自己的永久分享 |
| `download_share_files` | 把夸克文件下载到本地。分享链接会先把选中项转存到下载缓存，不创建分享 |
| `upload_file_or_folder` | 把本地文件或文件夹经官方 CLI 上传到资源目录，可选立即创建永久公开分享 |

`ingest_share_link` 发现目标资源目录已经有文件时会停下，避免把旧文件和新文件打成同一条分享。`selective_ingest` 使用同一条规则。

## 从大合集精选上架

`include_paths` 是分享**内容根**下的相对路径，不是源 URL，也不包含分享标题那一层。如果分享根只有一个文件夹，内容根就是这个文件夹里面；否则内容根就是分享根列出的那些条目。

先看树，再转存选中的子目录。下面只转存「抖音电商」这一个子合集：

```text
inspect_share_tree(share_url="https://pan.quark.cn/s/xxxx", max_depth=2, max_entries=2000)

selective_ingest(
  share_url="https://pan.quark.cn/s/xxxx",
  include_paths=["11-Codex Skills｜抖音电商抖店平台skills技能库集合"],
  category="技能",
  title="抖音电商与抖店平台 Skills 技能库"
)
```

路径可以多选，顺序无关。匹配不到的放进 `skipped_missing`，其余继续。一个都匹配不到，或者 `include_paths` 为空，会直接失败，不会做出空分享。选中一个目录时，转存的是这个目录本身，不是把它拆成散文件。

官方转存接口支持子集：`/open/v1/share/saveas` 的 `fid_list` 和 `save_all` 互斥。这里固定 `save_all=false`，并只在每个选中项都有 `share_fid_token` 时附带 `fid_token_list`。不采用「先整包转存再移动」，避免失败时留下一整份临时副本。一次最多提交 100 个 fid；后一批失败时不会创建分享，目标目录里可能已经有前面成功的那一批。

任务轮询 `query_task` 偶发被网关以 10001 拒绝时会自动重试；重试仍失败时改用列目录确认转存是否已落地，目录里有文件就继续走分享。转存阶段失败时返回 `dest_path` 和补救指引，先用 `list_directory` 确认，非空就调 `create_share_for_saved_folder` 补永久分享。

目标目录必须是 `{根目录}/{分类}/{资源名}`。品类目录或根目录会被拒绝。目录里已经有文件也会拒绝，同一批路径再调一次会因为目录非空而失败，不会混进去。

分享创建失败或回读发现是空的时，返回里有 `saved=true` 和 `dest_path`，没有可上架的 `share_url`。接着用 `create_share_for_saved_folder` 只对这个资源目录补永久分享。

到达 `max_depth` 的文件夹不会继续打开，`truncated` 为 true。分页若重复返回同一页，会停下来并标成截断，不会死循环。

## 转存后下载到本地

官方开放平台没有「直接下载别人分享里的文件」的接口。官方 CLI 的 read-file 对每个自己网盘里的 fid 单独调用 `POST /open/v1/file/get_download_url`，请求体是 `{"fid":"..."}`，查询串只有 `req_id` 和 `access_token`。签名仍是 `SHA256(POST&/open/v1/file/get_download_url&毫秒时间戳&signKey)`，头是 `x-pan-client-id`、`x-pan-tm`、`x-pan-token`。批量接口 `multi_get_download_url` 在同一套签名下会返回 `10001 签名验证失败`，这里不再调用。拿到地址后按文件串行下载。

自己网盘里的目录直接传绝对路径。`include_paths` 为空表示下载该目录下的全部文件；非空时相对这个目录。传入分享链接时，只会把选中项转存到 `{根目录}/下载缓存/{分享标题}`，然后下载，**不创建分享**。这个缓存目录若已有文件，会拒绝混入；这时把该路径再传一次，即可直接下载已经转存的文件。

```text
download_share_files(
  share_url_or_dest_path="/闲鱼资源/技能/抖音电商与抖店平台 Skills 技能库",
  include_paths=[],
  local_dir="/Users/you/Downloads/skills-douyin",
  max_files=10,
  max_total_mb=200
)
```

建议先把 `max_files` 设为 10 看限流，再放开。`max_files` 和 `max_total_mb` 是硬上限，一触顶就停止，剩下的进 `skipped` 并写明原因。同名文件默认跳过，只有 `overwrite=true` 才覆盖。`local_dir` 必须是绝对路径，不能包含 `..`，也不能是符号链接。

下载和转存共用 `NETDISK_MIN_INTERVAL_SECONDS`（默认 3 秒）。官方 CLI 也是串行取文件，没有公布「第 N 个文件必定被封」的数字，这里没有对生产账号做批量压测。单个文件取地址失败不会作废整批，原因逐条写进 `errors`。取地址和下载遇到 HTTP 429 时同一文件只再试一次；连续 3 个文件失败就中断，避免把账号打进风控。不要并行下载。

## 本地上传并分享

把本地制作、提纯后的成品（单个 zip 或整个文件夹）传回夸克资源目录，然后按上架惯例创建永久公开分享并回读验证：

```text
upload_file_or_folder(
  local_path="/Users/you/Products/xxx_通用版.zip",
  dest_dir="/闲鱼资源/技能/闲鱼实操Skills合集"
)
```

`dest_dir` 必须是「根目录/分类/资源名」三级目录；留空时按 `title` 和 `category`（或规则关键词）自动分配。`create_share=false` 只上传不分享。分享失败但上传已成功时，返回 `uploaded=true` 和 `dest_path`，交给 `create_share_for_saved_folder` 补分享。

上传走官方 CLI 的 `upload <路径> --parent-fid <fid>`，支持秒传和断点续传；返回里的 `instant_upload` 表示这次是否命中秒传。安装脚本不再提供全局命令，需要设置 `QUARK_CLI_SCRIPT` 指向官方 Skills 目录里的 `scripts/quark-drive.cjs`（`.cjs` 会自动用 node 执行）。子进程会清掉 `PYTHONPATH` 和 `PYTHONHOME`，在工作目录里运行，超时默认 1800 秒，可用 `QUARK_CLI_TIMEOUT_SECONDS` 调整。

上传文件夹时，CLI 会在资源目录下创建同名文件夹再递归上传，分享的就是这个文件夹；上传单个文件时分享这个文件本身。目标资源目录必须为空，已有内容会以 `DEST_NOT_EMPTY` 拒绝，避免新旧文件混进同一条分享。

本地路径必须是绝对路径且不能是符号链接，文件夹内部也不允许包含符号链接（CLI 会跟随符号链接，可能把指向任意位置的内容传上网盘）；空文件夹会被拒绝，且不发起任何网盘请求。文件夹里有文件失败时同样拒绝创建分享，此时目标目录可能已有部分文件。失败返回带 `code`：`FILE_NOT_FOUND`、`LOCAL_PATH_INVALID`、`CLI_NOT_FOUND`、`DEST_NOT_EMPTY`、`UPLOAD_FAILED`、`UPLOAD_FOLDER_MISSING`。

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
