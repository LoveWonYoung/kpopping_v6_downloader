# Kpopping Idol 图片下载器

这是一个基于 Tkinter 的桌面下载器。输入 idol UUID 后，程序会分页读取该 idol
的全部图集，再从每个图集的 `albumImages` 下载 CDN 原图。网络请求和文件下载在
后台线程运行，不会阻塞界面。

## 运行

在本目录执行：

```bash
uv run python main.py
```

Tkinter 随 Python 提供，不需要额外安装 PyPI 依赖。如果系统 Python 没有包含
Tkinter，需要先安装带 Tk 支持的 Python。

界面参数：

- **账号 / 邮箱、密码**：首次登录时填写；保存配置时会保存账号，但不会保存密码。
- **设备指纹**：启动时自动生成，也可以替换为自己的值。
- **Idol ID**：必填，即 kpopping 接口使用的 UUID。界面默认填入 Karina 的示例 ID。
- **Idol 名称**：仅用于请求 referer 和输出目录名称。
- **排序**：支持 `hot` 和 `date`，只影响图集处理顺序。
- **并发**：当前图集内同时下载的图片数，默认为 `4`，可设置为 `1–16`。
- **保存目录**：图片最终保存到 `<保存目录>/<Idol 名称>/<图集>/`。
- **保存配置**：把账号、设备指纹、Idol、排序、并发数和保存目录写入
  `config.json`，下次启动自动加载。为安全起见，密码不会写入配置。

登录成功后，Cookie 保存在同目录的 `session_cookies.json`。下次启动检测到
`auth_token` 会跳过登录，因此账号和密码可以留空。程序会跳过同名且非空的已有
图片，下载中的临时文件使用 `.part` 后缀；点击“取消”会在当前网络操作结束后停止。
如果 JWT 中的有效期已经结束，则会使用界面中填写的凭证重新登录。

一个图集处理结束后，程序会把相册文件夹名称追加到程序目录下的
`download_history.json` 字符串数组中，不记录图片名称。以后即使把 `downloads` 中的
相册目录删除，同名相册仍会按历史记录跳过。个别图片下载失败也会记录这个相册，不再
自动补下；图集详情读取失败或用户取消时不会记录。`config.json`、
`download_history.json` 和 Cookie 文件都已加入 `.gitignore`，升级或搬动程序时请
自行保留下载记录文件。

## 接口说明

站点在 Cloudflare 后，列表页是客户端渲染 + 无限滚动。不要解析筛选后的 HTML，直接打下面三个接口。请求需带登录 Cookie（`auth_token`），未登录列表最多预览 16 条。

HTTP 客户端用 `curl_cffi.requests.Session(impersonate="chrome")`，同一 Session 里先登录再拉数据。

## 1. 登录

`POST https://kpopping.com/api/auth/login`

```json
{
  "email": "<username-or-email>",
  "password": "<password>",
  "deviceFingerprint": "<hex-fingerprint>"
}
```

成功：`200`，`{ "success": true, "user": { ... } }`，响应 Cookie 写入 `auth_token`（JWT）。  
失败：`429` 为限流，看 `retry-after`。

本地把 Cookie 存到 `session_cookies.json`，下次启动若已有 `auth_token` 就跳过登录。

## 2. 图集列表

`GET https://kpopping.com/api/photos`

对应页面：`https://kpopping.com/kpics?idol=<uuid>&idolName=<name>`

| 参数 | 说明 |
| --- | --- |
| `idolId` | 偶像 UUID。Karina：`077c4f02-7ca6-49a6-9daf-df1dabc55d0f` |
| `groupId` | 与 `idolId` 二选一 |
| `limit` | 每页条数，前端用 `50` |
| `offset` | `0 / 50 / 100 / ...`；界面版会持续翻页直到取完该 idol 的图集 |
| `sort` | 默认 `hot`，也可 `date` |

返回数组。每条有 `slug`、`title`、`src`、`albumCount`、`idolName`。  
图集页 URL：`https://kpopping.com/kpics/{slug}`  
独立调用接口时默认只需拉 `offset=0` 第一页；界面中的“下载全部图片”功能会按需
翻完所有分页，并按 `slug` 去重。

## 3. 图集详情（图片列表）

`GET https://kpopping.com/api/kpics/{slug}`

`albumImages` 即该页全部原图，数量等于 `albumCount`。按 `sortOrder` 排序。

`albumImages.src` 有两种托管方式，下载时不要一律改成 CDN：

- 新图集：`*.r2.dev/kpics/...`。路径相同，把 host 换成 `cdn.kpopping.com`：

```
https://cdn.kpopping.com/kpics/2026/08/1787925197112-9vooq6-1.jpg
```

- 旧图集：`https://kpopping.com/documents/...`。原链会 302 到
  `legacy.kpopping.com`，能下到原图；改成 `cdn.kpopping.com/documents/...` 会 404。

不要解析 `/_next/image?url=...` 的 srcset。

## 请求头

```
accept: */*
origin: https://kpopping.com
referer: https://kpopping.com/kpics?...
sec-fetch-site: same-origin
```

Cookie 随 Session 自动带上。

## 下载行为

- 列表接口按每页 50 条依次读取，直到返回不足 50 条。
- 图集之间依次处理；单个图集的图片使用固定线程池并发下载，每个线程使用独立的
  `curl_cffi` Session。登录 Cookie 仅用于 kpopping API，不复制给 CDN 下载线程。
- 每个图集通过 `/api/kpics/{slug}` 获取 `albumImages`，不解析页面 HTML 或
  `/_next/image` 的 srcset。
- `*.r2.dev` 的 `src` 只替换 host 为 `cdn.kpopping.com`，保留原路径；已经是
  `kpopping.com` / `legacy.kpopping.com` 的地址（含 `/documents/`）原样下载。
- 遇到 `429` 时所有下载线程共享限流截止时间，遵守响应中的 `retry-after`，等待后
  再重试，连续失败不会密集请求。
- 单张图片失败会记录在日志中并继续，完成弹窗会显示下载、跳过和失败数量。
- 已存在于 `download_history.json` 的相册不会再请求详情或下载图片；每个相册处理结束
  后立即原子更新文件夹名列表，删除下载目录不会清除历史。
