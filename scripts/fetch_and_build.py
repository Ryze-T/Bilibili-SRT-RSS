# -*- coding: utf-8 -*-
"""
fetch_and_build.py (Bilibili 版)
-------------------
读取 Bilibili UP 主的最新视频，抓取 AI / 人工字幕，生成 RSS。
逻辑结构与 Youtube-SRT-RSS 项目保持一致。
"""

import os
import re
import sys
import json
import time
import hashlib
import tempfile
import calendar
import urllib.parse
from datetime import datetime, timezone

import yaml
import requests
from feedgen.feed import FeedGenerator

# 把自身所在目录加入 sys.path，确保 `python scripts/fetch_and_build.py`
# 这种调用方式下也能 import 同目录的 check_cookie 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = "config.yml"
STATE_PATH = "state/items.json"
OUTPUT_DIR = "docs/feeds"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------
# 💡 安全核心：双轨制动态加载 Bilibili Cookie
# --------------------------------------------------------------------
_TEMP_COOKIE_FILE = None
_COOKIES_DICT = None


def get_cookie_path():
    """双轨凭证路由：优先读环境变量；若无，则读取本地 cookie.txt"""
    global _TEMP_COOKIE_FILE
    if _TEMP_COOKIE_FILE and os.path.exists(_TEMP_COOKIE_FILE):
        return _TEMP_COOKIE_FILE

    cookie_content = None
    env_content = os.environ.get("BILI_RAW_COOKIES")
    if env_content:
        cookie_content = env_content
        print("[凭证路由] 🌐 成功激活云端轨道：从加密环境变量载入 Bilibili 凭证")
    else:
        local_cookie = "cookie.txt"
        if os.path.exists(local_cookie):
            with open(local_cookie, "r", encoding="utf-8") as f:
                cookie_content = f.read()
            print(f"[凭证路由] 💻 成功激活本地轨道：从 {local_cookie} 载入 Bilibili 凭证")

    if not cookie_content:
        return None

    # 格式鲁棒性防御：把可能被复制成空格的分隔符还原为 \t
    cleaned_lines = []
    for line in cookie_content.strip().splitlines():
        if not line.strip() or line.startswith("#"):
            cleaned_lines.append(line)
            continue
        parts = line.split()
        if len(parts) >= 6:
            domain, include_sub, path, secure, expiry, name = parts[:6]
            value = " ".join(parts[6:]) if len(parts) > 6 else ""
            cleaned_lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
        else:
            cleaned_lines.append(line)

    tmp_file = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt", encoding="utf-8")
    tmp_file.write("\n".join(cleaned_lines))
    tmp_file.close()
    _TEMP_COOKIE_FILE = tmp_file.name
    return _TEMP_COOKIE_FILE


def get_cookies():
    """把 Netscape cookie 文件解析成 dict 供 requests 使用，只取 bilibili 域"""
    global _COOKIES_DICT
    if _COOKIES_DICT is not None:
        return _COOKIES_DICT
    import http.cookiejar
    path = get_cookie_path()
    if not path:
        _COOKIES_DICT = {}
        return _COOKIES_DICT
    cj = http.cookiejar.MozillaCookieJar(path)
    cj.load(ignore_discard=True, ignore_expires=True)
    _COOKIES_DICT = {c.name: c.value for c in cj if "bilibili.com" in c.domain}
    missing = [k for k in ("SESSDATA", "bili_jct") if k not in _COOKIES_DICT]
    if missing:
        print(f"[凭证警告] ⚠️ Bilibili cookie 缺少关键字段：{missing}，AI 字幕可能拿不到")
    return _COOKIES_DICT


def clean_temporary_cookie():
    global _TEMP_COOKIE_FILE
    if _TEMP_COOKIE_FILE and os.path.exists(_TEMP_COOKIE_FILE):
        try:
            os.remove(_TEMP_COOKIE_FILE)
            print("[安全防御] 🛡️ 沙盒临时 Cookie 文件已完成物理粉碎。")
        except Exception:
            pass


# --------------------------------------------------------------------
# wbi 签名（Bilibili 部分接口必须签名，否则返回 -403）
# --------------------------------------------------------------------
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
_WBI_KEYS_CACHE = None


def _get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def _refresh_wbi_keys():
    global _WBI_KEYS_CACHE
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
    r = requests.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=headers,
        cookies=get_cookies(),
        timeout=15,
    ).json()
    img = r["data"]["wbi_img"]["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub = r["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[1].split(".")[0]
    _WBI_KEYS_CACHE = _get_mixin_key(img + sub)
    return _WBI_KEYS_CACHE


def wbi_sign(params: dict) -> dict:
    mixin = _WBI_KEYS_CACHE or _refresh_wbi_keys()
    params["wts"] = int(time.time())
    items = sorted(params.items())
    items = [(k, "".join(c for c in str(v) if c not in "!'()*")) for k, v in items]
    query = urllib.parse.urlencode(items)
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params


# --------------------------------------------------------------------
# 字幕获取核心逻辑
# --------------------------------------------------------------------
def _bili_get(url, params=None, referer="https://www.bilibili.com/", timeout=15):
    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    return requests.get(url, params=params, headers=headers, cookies=get_cookies(), timeout=timeout)


def fetch_up_videos(mid):
    """通过 wbi 签名接口拉取 UP 主最新投稿"""
    params = wbi_sign({"mid": mid, "ps": 30, "pn": 1, "order": "pubdate"})
    r = _bili_get(
        "https://api.bilibili.com/x/space/wbi/arc/search",
        params=params,
        referer=f"https://space.bilibili.com/{mid}",
    ).json()
    if r.get("code") != 0:
        raise RuntimeError(f"Bilibili 投稿接口错误 code={r.get('code')} msg={r.get('message')}")
    videos = []
    for v in r["data"]["list"]["vlist"]:
        videos.append({
            "bvid": v["bvid"],
            "aid": v.get("aid"),
            "title": v["title"],
            "link": f"https://www.bilibili.com/video/{v['bvid']}/",
            "description": v.get("description", "") or "",
            "duration": v.get("length", ""),
            "published_dt": datetime.fromtimestamp(v["created"], tz=timezone.utc),
        })
    videos.sort(key=lambda x: x["published_dt"])
    return videos


def _get_video_view(bvid):
    """BV -> cid / aid / 标题等基本信息"""
    r = _bili_get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid},
        referer=f"https://www.bilibili.com/video/{bvid}/",
    ).json()
    if r.get("code") != 0:
        return None
    d = r["data"]
    return {"cid": d["cid"], "aid": d["aid"], "title": d["title"], "desc": d.get("desc", "")}


def _list_subtitles(bvid, cid, aid):
    """返回字幕轨道列表"""
    r = _bili_get(
        "https://api.bilibili.com/x/player/wbi/v2",
        params={"bvid": bvid, "cid": cid, "aid": aid},
        referer=f"https://www.bilibili.com/video/{bvid}/",
    ).json()
    if r.get("code") != 0:
        return []
    return r.get("data", {}).get("subtitle", {}).get("subtitles", []) or []


def _download_subtitle(sub_url, bvid):
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    j = _bili_get(sub_url, referer=f"https://www.bilibili.com/video/{bvid}/").json()
    body = j.get("body", []) or []
    pieces = [seg["content"].strip() for seg in body if seg.get("content", "").strip()]
    return " ".join(pieces) if pieces else None


def get_transcript(bvid, lang_priority):
    """从 B 站抓字幕：按 lang_priority 优先级选择字幕轨道"""
    view = _get_video_view(bvid)
    if not view:
        print(f"      └─ ❌ 无法获取视频基本信息（可能 cookie 失效或视频被删）")
        return None, None

    subs = _list_subtitles(bvid, view["cid"], view["aid"])
    if not subs:
        print("      └─ ❌ 该视频未提供任何字幕轨道（包括 AI 字幕）")
        return None, None

    # 按语言优先级排序
    def lang_rank(s):
        lan = s.get("lan", "")
        for i, code in enumerate(lang_priority):
            if lan == code:
                return i
        # 优先级里没有的轨道靠后
        return 999

    subs_sorted = sorted(subs, key=lang_rank)
    chosen = subs_sorted[0]
    lan = chosen.get("lan", "?")
    print(f"      >> 选定字幕轨道: lan={lan} doc={chosen.get('lan_doc')} ai_status={chosen.get('ai_status')}")

    # AI 字幕未生成完成时跳过本轮（下次再来）
    if str(lan).startswith("ai-") and chosen.get("ai_status") not in (2, None):
        print("      └─ ⏳ AI 字幕尚未生成完成，本轮跳过")
        return None, None

    text = _download_subtitle(chosen.get("subtitle_url", ""), bvid)
    if not text:
        print("      └─ ❌ 字幕内容为空")
        return None, None
    return text, lan


# --------------------------------------------------------------------
# 基础配置与主循环
# --------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_items():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_items(items):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def slugify_filename(name):
    return re.sub(r"\s+", "-", name.strip())


def build_feed_for_category(category_name, items, site_base_url):
    fg = FeedGenerator()
    fg.id(site_base_url or f"urn:category:{category_name}")
    fg.title(f"{category_name} · Bilibili 文字稿订阅")
    fg.link(href=f"{site_base_url}/feeds/{slugify_filename(category_name)}.xml", rel="self")
    fg.link(href=site_base_url or "https://example.com", rel="alternate")
    fg.description(f"自动抓取「{category_name}」分类下 UP 主的最新视频文字稿")
    fg.language("zh-cn")

    for it in items:
        fe = fg.add_entry()
        fe.id(it["link"])
        fe.title(f"【{it['up_name']}】{it['title']}")
        fe.link(href=it["link"])
        fe.pubDate(datetime.fromisoformat(it["published"]))
        if it.get("transcript"):
            body = it["transcript"]
        elif it.get("description"):
            body = f"（该视频暂无字幕，以下为简介）<br>{it['description']}"
        else:
            body = "（该视频未提供字幕，请点击标题查看原视频）"
        fe.description(
            f'<p><a href="{it["link"]}">▶ 观看原视频</a> ｜ UP主：{it["up_name"]}</p><p>{body}</p>'
        )

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{slugify_filename(category_name)}.xml")
    fg.rss_file(out_path)


def write_index_html(categories_cfg, site_base_url):
    rows = []
    for category_name, ups in categories_cfg.items():
        feed_filename = f"{slugify_filename(category_name)}.xml"
        up_list = "、".join(u.get("name", str(u["mid"])) for u in ups)
        rows.append(
            f'<li><b>{category_name}</b> — '
            f'<a href="feeds/{feed_filename}">RSS 订阅链接</a><br>'
            f'<small>包含 UP 主：{up_list}</small></li>'
        )
    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<title>Bilibili 文字稿 RSS 订阅</title></head>'
        '<body><h1>Bilibili 文字稿 RSS 订阅</h1>'
        f'<p>最后更新时间（UTC）：{datetime.now(timezone.utc).isoformat()}</p>'
        f'<ul>{"".join(rows)}</ul></body></html>'
    )
    with open(os.path.join("docs", "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def main():
    print("=== [开始运行工作流：Bilibili 字幕 RSS] ===")

    # ---- 启动前先做 cookie 健康检查 ----
    try:
        from check_cookie import health_report, write_health_file
        report = health_report()
        write_health_file(report)
        print(f"[健康检查] {report['summary']}")
        if not report["ok"]:
            print(f"[健康检查] 💡 建议：{report.get('advice')}")
            print(
                "[健康检查] ❌ Cookie 不可用，终止本轮抓取。"
                "health.json 已写入 docs/，请通过 GitHub Pages 端口刷新外部探测器。"
            )
            import sys as _sys
            _sys.exit(2)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[健康检查] ⚠️ 健康检查脚本异常：{e}（继续尝试抓取）")

    config = load_config()
    items = load_items()
    seen_ids = {it["bvid"] for it in items}

    lang_priority = config.get("transcript_lang_priority", ["zh-CN", "ai-zh", "en"])
    max_new = config.get("max_new_per_up_per_run", 3)
    max_per_feed = config.get("max_items_per_feed", 50)
    only_keep_days = int(config.get("only_keep_days", 7))
    site_base_url = (config.get("site_base_url") or "").rstrip("/")
    categories_cfg = config.get("categories", {})
    ok_count, fail_count = 0, 0

    # 时间窗口：只关心发布时间在 cutoff 之后的视频
    from datetime import timedelta
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=only_keep_days)
    print(f"[配置] 仅处理 {cutoff_dt.isoformat(timespec='seconds')} 之后发布的视频（最近 {only_keep_days} 天）")

    # 提前刷一次 wbi keys
    try:
        _refresh_wbi_keys()
    except Exception as e:
        print(f"[警告] wbi keys 刷新失败：{e}（后续接口可能 -403）")

    try:
        for category_name, ups in categories_cfg.items():
            for up in ups:
                mid = up["mid"]
                up_name = up.get("name", str(mid))
                print(f"\n=================== ⏳ 分类 [{category_name}] -> UP: {up_name} (mid={mid}) ===================")

                try:
                    videos = fetch_up_videos(mid)
                except Exception as e:
                    print(f"    [错误] ❌ 获取 UP 投稿失败：{e}")
                    continue

                new_videos = [
                    v for v in videos
                    if v["bvid"] not in seen_ids and v["published_dt"] >= cutoff_dt
                ][-max_new:]
                if not new_videos:
                    print("    [状态] ✨ 检查完毕，该 UP 主在窗口期内没有新视频。")
                    continue

                print(f"    [状态] 🔔 发现 {len(new_videos)} 个新视频，开始抓字幕...")
                for v in new_videos:
                    print(f"    🎬 视频目标: {v['title']} ({v['bvid']})")
                    transcript, source = get_transcript(v["bvid"], lang_priority)
                    if transcript:
                        ok_count += 1
                        print(f"      └─ 🎉 字幕提取成功！来源轨道: [{source}] 字数={len(transcript)}")
                    else:
                        fail_count += 1

                    items.append({
                        "bvid": v["bvid"],
                        "category": category_name,
                        "mid": mid,
                        "up_name": up_name,
                        "title": v["title"],
                        "link": v["link"],
                        "description": v["description"],
                        "published": v["published_dt"].isoformat(),
                        "transcript": transcript,
                        "transcript_source": source,
                    })
                    seen_ids.add(v["bvid"])
                    time.sleep(1.5)
    finally:
        clean_temporary_cookie()

    # 历史裁剪与静态编译
    trimmed = []
    for category_name in categories_cfg:
        cat_items = [it for it in items if it["category"] == category_name]
        cat_items.sort(key=lambda it: it["published"], reverse=True)
        trimmed.extend(cat_items[: max_per_feed * 3])
    items = trimmed
    save_items(items)

    # RSS 输出阶段也按时间窗口过滤：只发布最近 N 天内的条目
    cutoff_iso = cutoff_dt.isoformat()
    for category_name in categories_cfg:
        cat_items = [
            it for it in items
            if it["category"] == category_name and it["published"] >= cutoff_iso
        ]
        cat_items.sort(key=lambda it: it["published"], reverse=True)
        print(f"[输出] 分类 [{category_name}]: 窗口内 {len(cat_items)} 条 → RSS")
        build_feed_for_category(category_name, cat_items[:max_per_feed], site_base_url)

    write_index_html(categories_cfg, site_base_url)
    print(f"\n=================== 🎉 工作流运行结束（成功: {ok_count}，失败: {fail_count}） ===================")


if __name__ == "__main__":
    main()
