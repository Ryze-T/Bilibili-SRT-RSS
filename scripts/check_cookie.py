# -*- coding: utf-8 -*-
"""
check_cookie.py
---------------
Bilibili Cookie 健康检查工具。

两种用法：
1. 命令行直接跑：
       python scripts/check_cookie.py
   会打印一份人类可读的报告，并把 JSON 报告写到 docs/health.json。
   - 退出码 0：所有检查通过
   - 退出码 1：登录已失效或关键检查失败

2. 作为模块被 fetch_and_build.py 调用：
       from check_cookie import health_report, write_health_file
"""

import json
import os
import sys
import time
import hashlib
import urllib.parse
from datetime import datetime, timezone

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEALTH_FILE = os.path.join("docs", "health.json")

# 必须存在的 cookie 字段（缺一个就直接判失败）
REQUIRED_FIELDS = ["SESSDATA", "bili_jct", "DedeUserID"]
# 强建议存在的字段（缺了打 warning 但不 fail）
RECOMMENDED_FIELDS = ["buvid3", "buvid4", "buvid_fp", "b_nut", "bili_ticket"]


# --------------------------------------------------------------------
# Cookie 载入（与主脚本逻辑一致：环境变量 > 本地 cookie.txt）
# --------------------------------------------------------------------
def _load_cookie_text():
    env = os.environ.get("BILI_RAW_COOKIES")
    if env:
        return env, "env:BILI_RAW_COOKIES"
    if os.path.exists("cookie.txt"):
        with open("cookie.txt", "r", encoding="utf-8") as f:
            return f.read(), "file:cookie.txt"
    return None, None


def _parse_netscape_to_dict(text):
    cookies = {}
    for raw in text.strip().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 7:
            # 兼容只有 name=value 的简单 cookie 串
            if "=" in line:
                k, v = line.split("=", 1)
                if "bilibili" in k.lower() or v:
                    cookies[k.strip()] = v.strip().rstrip(";")
            continue
        domain, _, _, _, _, name = parts[:6]
        value = "\t".join(parts[6:]) if len(parts) > 7 else parts[6]
        if "bilibili.com" in domain:
            cookies[name] = value
    return cookies


# --------------------------------------------------------------------
# 真实接口探测
# --------------------------------------------------------------------
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _wbi_sign(params, mixin):
    params["wts"] = int(time.time())
    items = sorted(params.items())
    items = [(k, "".join(c for c in str(v) if c not in "!'()*")) for k, v in items]
    query = urllib.parse.urlencode(items)
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params


def _mask_uname(name):
    """昵称脱敏：保留首字符 + ***。空值返回 ?***"""
    if not name:
        return "?***"
    return name[0] + "***"


def _hash_mid(mid):
    """把 mid 哈希成不可逆的 8 位标识，便于外部对比是否同账号但不暴露 UID"""
    if mid is None:
        return None
    return hashlib.sha256(str(mid).encode()).hexdigest()[:8]


def _check_nav(cookies):
    """探测 /x/web-interface/nav，能拿到 isLogin / mid 就说明 SESSDATA 有效"""
    try:
        r = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"},
            cookies=cookies,
            timeout=10,
        )
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}", None, None
        data = r.json()
        if data.get("code") != 0:
            return False, f"code={data.get('code')} msg={data.get('message')}", None, None
        d = data.get("data", {}) or {}
        if not d.get("isLogin"):
            return False, "isLogin=false（cookie 已失效或未登录）", None, None
        # wbi keys
        img = d["wbi_img"]["img_url"].rsplit("/", 1)[1].split(".")[0]
        sub = d["wbi_img"]["sub_url"].rsplit("/", 1)[1].split(".")[0]
        mixin = "".join((img + sub)[i] for i in _MIXIN_KEY_ENC_TAB)[:32]
        # 脱敏处理：只暴露脱敏后的信息，原始 uname/mid 不写入接口
        info = {
            "uname_masked": _mask_uname(d.get("uname")),
            "mid_hash": _hash_mid(d.get("mid")),
        }
        return True, "ok", mixin, info
    except Exception as e:
        return False, f"exception: {type(e).__name__}: {e}", None, None


def _check_wbi_endpoint(cookies, mixin, sample_mid=1962124785):
    """探测一次签名接口，确认 wbi 签名链路能走通"""
    try:
        params = _wbi_sign({"mid": sample_mid, "ps": 1, "pn": 1, "order": "pubdate"}, mixin)
        r = requests.get(
            "https://api.bilibili.com/x/space/wbi/arc/search",
            params=params,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": f"https://space.bilibili.com/{sample_mid}",
            },
            cookies=cookies,
            timeout=10,
        )
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("code") != 0:
            return False, f"code={data.get('code')} msg={data.get('message')}"
        return True, "ok"
    except Exception as e:
        return False, f"exception: {type(e).__name__}: {e}"


def _check_subtitle_endpoint(cookies, sample_bvid="BV1Ju7868Erp"):
    """探测一次字幕接口，确认能看到 ai-zh 字幕轨。
    出于隐私考虑，对外暴露的 detail 不包含采样 bvid。
    """
    try:
        # BV -> cid
        r = requests.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": sample_bvid},
            headers={"User-Agent": USER_AGENT, "Referer": f"https://www.bilibili.com/video/{sample_bvid}/"},
            cookies=cookies,
            timeout=10,
        ).json()
        if r.get("code") != 0:
            return False, f"view code={r.get('code')}"
        cid, aid = r["data"]["cid"], r["data"]["aid"]
        r2 = requests.get(
            "https://api.bilibili.com/x/player/wbi/v2",
            params={"bvid": sample_bvid, "cid": cid, "aid": aid},
            headers={"User-Agent": USER_AGENT, "Referer": f"https://www.bilibili.com/video/{sample_bvid}/"},
            cookies=cookies,
            timeout=10,
        ).json()
        if r2.get("code") != 0:
            return False, f"player/wbi/v2 code={r2.get('code')}"
        subs = r2.get("data", {}).get("subtitle", {}).get("subtitles", []) or []
        if not subs:
            return False, "subtitle track empty"
        return True, f"subtitle_track_count={len(subs)}"
    except Exception as e:
        return False, f"exception: {type(e).__name__}"


# --------------------------------------------------------------------
# 主报告
# --------------------------------------------------------------------
def health_report():
    """返回结构化健康报告 dict。"""
    report = {
        "ok": False,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cookie_source": None,
        "checks": {},
        "summary": "",
        "advice": "",
    }

    text, source = _load_cookie_text()
    report["cookie_source"] = source

    if not text:
        report["checks"]["cookie_loaded"] = {"ok": False, "detail": "未找到 cookie"}
        report["summary"] = "❌ 未找到 Bilibili Cookie"
        report["advice"] = (
            "在 GitHub Secrets 中设置 BILI_RAW_COOKIES，"
            "或本地放置 cookie.txt（Netscape 格式）。"
        )
        return report
    report["checks"]["cookie_loaded"] = {"ok": True, "detail": source}

    cookies = _parse_netscape_to_dict(text)
    # 字段完整性
    missing_req = [f for f in REQUIRED_FIELDS if f not in cookies]
    missing_rec = [f for f in RECOMMENDED_FIELDS if f not in cookies]
    report["checks"]["fields_required"] = {
        "ok": not missing_req,
        "missing": missing_req,
    }
    report["checks"]["fields_recommended"] = {
        "ok": not missing_rec,
        "missing": missing_rec,
    }
    if missing_req:
        report["summary"] = f"❌ Cookie 缺少必需字段：{missing_req}"
        report["advice"] = "重新从浏览器导出 cookie.txt，至少包含 SESSDATA / bili_jct / DedeUserID。"
        return report

    # 登录态 + wbi keys
    ok, detail, mixin, info = _check_nav(cookies)
    report["checks"]["nav_login"] = {"ok": ok, "detail": detail, "info": info}
    if not ok:
        report["summary"] = f"❌ 登录态校验失败：{detail}"
        report["advice"] = (
            "SESSDATA 很可能已过期或被风控。请在浏览器登录 bilibili.com 后重新导出 cookie，"
            "用 scripts/clean_cookie.py 清洗后更新 BILI_RAW_COOKIES。"
        )
        return report

    # wbi 签名链路
    ok, detail = _check_wbi_endpoint(cookies, mixin)
    report["checks"]["wbi_signature"] = {"ok": ok, "detail": detail}
    if not ok:
        report["summary"] = f"❌ wbi 签名接口失败：{detail}"
        report["advice"] = "wbi 算法可能被 B 站更新，检查 _MIXIN_KEY_ENC_TAB 是否最新。"
        return report

    # 字幕轨道
    ok, detail = _check_subtitle_endpoint(cookies)
    report["checks"]["subtitle_endpoint"] = {"ok": ok, "detail": detail}
    if not ok:
        report["summary"] = f"⚠️ 字幕接口探测失败：{detail}"
        report["advice"] = (
            "登录态正常但拿不到字幕轨道，可能因为目标视频已删除或当前 IP 被限流。"
            "如果接下来抓取持续失败，考虑切换执行环境或刷新 cookie。"
        )
        # 不强制 fail，因为采样视频可能就是没字幕
        report["ok"] = True
        return report

    report["ok"] = True
    report["summary"] = "✅ Cookie 健康，登录态有效"
    return report


def write_health_file(report, path=HEALTH_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def _print_human(report):
    print("==================== Bilibili Cookie 健康检查 ====================")
    print(f"检查时间: {report['checked_at']}")
    print(f"凭证来源: {report['cookie_source']}")
    print("------------------------------------------------------------------")
    for name, c in report["checks"].items():
        mark = "✅" if c.get("ok") else "❌"
        extra = c.get("detail") or c.get("info") or c.get("missing") or ""
        print(f"  {mark} {name}: {extra}")
    print("------------------------------------------------------------------")
    print(report["summary"])
    if report.get("advice"):
        print(f"💡 建议: {report['advice']}")
    print("==================================================================")


def main():
    report = health_report()
    _print_human(report)
    try:
        write_health_file(report)
        print(f"📝 已写入 {HEALTH_FILE}")
    except Exception as e:
        print(f"⚠️ 写入 {HEALTH_FILE} 失败：{e}")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
