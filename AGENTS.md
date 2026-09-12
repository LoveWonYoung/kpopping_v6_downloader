# AGENTS.md

本仓库是 kpopping.com kpics 下载器。实现与改接口时以 `kpopping_v6_downloader/README.md` 为准。

## 运行

在 `kpopping_v6_downloader/` 下：

```bash
uv run python main.py
```

入口是 `kpopping_v6_downloader/main.py`。用 `curl_cffi` 的 Session，`impersonate="chrome"`。

## 数据流

1. `POST /api/auth/login`，Cookie 存 `session_cookies.json`。已有 `auth_token` 则跳过登录。
2. `GET /api/photos?idolId=...&limit=50&offset=0&sort=hot` 拿第一页图集。
3. 用 `slug` 拼 `https://kpopping.com/kpics/{slug}`。
4. `GET /api/kpics/{slug}` 读 `albumImages`。`*.r2.dev` 把 host 换成 `cdn.kpopping.com`；`kpopping.com/documents/...` 原样下载（会跳到 `legacy.kpopping.com`），不要改成 CDN。

不要去正则筛选页 HTML：`?idol=` 是前端渲染的，首屏不是目标偶像。不要解析 `/_next/image` srcset。不要在未要求时把列表 offset 翻完。

## 约束

- 凭证、Cookie、抓下来的 HTML/JSON 不要提交。已 gitignore：`session_cookies.json`、`*.html`、`karina_kpics.json`。
- 遇到 `429` 按 `retry-after` 等待，不要连打。
- 改接口行为时同步更新 README。
- 对用户用简体中文。
