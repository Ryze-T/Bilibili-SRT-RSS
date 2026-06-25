# Bilibili-SRT-RSS

把 Bilibili UP 主的最新视频字幕（人工 / AI 自动）打包成 RSS 订阅源，通过 GitHub Pages 分发。

## 工作原理

```
Bilibili UP 主投稿 API (wbi 签名)
        ↓
拿到最新视频列表 (BV)
        ↓
调 player/wbi/v2 拿字幕轨道
        ↓
下载 AI / 人工字幕 JSON 并解析
        ↓
生成分类 RSS XML → docs/feeds/*.xml
        ↓
GitHub Pages 发布
```

## 本地测试

```bash
pip install -r requirements.txt
# 把浏览器导出的 Bilibili Netscape cookie 放到 cookie.txt（需登录态，含 SESSDATA / bili_jct）
python scripts/fetch_and_build.py
```

## GitHub Actions 部署

1. 仓库设置里把 cookie.txt 的全部内容粘贴到 Secret `BILI_RAW_COOKIES`
2. Pages 来源选 `main` 分支 `/docs` 目录
3. Actions 每 3 小时自动跑一次，也可手动触发

## 配置说明

`config.yml`：

- `site_base_url`：你的 GitHub Pages 地址
- `max_new_per_up_per_run`：单 UP 主单次运行最多处理多少条新视频
- `max_items_per_feed`：每个分类 RSS 最多保留多少条
- `transcript_lang_priority`：字幕语言优先级，B 站常见 `zh-CN` / `ai-zh` / `en` / `ai-en`
- `categories`：分类下挂 UP 主，`mid` 取自 `space.bilibili.com/<mid>` 的数字

## 注意事项

- AI 字幕需要登录 Cookie 才会出现在接口返回里
- AI 字幕 `ai_status=2` 表示已生成完毕，否则下次运行会重试
- 不需要海外代理，B 站接口要求大陆 IP（GitHub Actions 默认是海外 IP，可能触发 412，必要时需要切换执行环境）
