#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命令行推送脚本（供定时任务调用）
用法：
    python push.py                # 按 config.push_mode 推送（默认今日）
    python push.py today           # 推送今日课表
    python push.py tomorrow        # 推送明日课表
    python push.py week            # 推送本周课表
    python push.py 2026-09-10      # 推送指定日期课表
    python push.py auto            # 由定时任务调用：仅在用户设定的推送时间点附近才发送（含补充内容）

推送时间与补充内容来自 GitHub 仓库 push-config.json（网页"推送设置"写入），
未设置时回退到本文件 push_time / 无补充内容。
"""
import sys
import re
import os
import json
import datetime
import urllib.request
import urllib.error
import base64
from class_bot import (JwAppClient, make_push_content, make_week_content,
                       send_webhook, load_config)

SCHEDULE_URL = "https://jwapp.gypec.edu.cn/jwmobile/index#/index/home/service"
CARD_BUTTONS = [
    {"title": "📖 看完整课表", "actionURL": SCHEDULE_URL},
]
OWNER, REPO = "raojiayong-lab", "mimi"
GH_API = "https://api.github.com"
STATE_PATH = ".push_state.json"  # 仓库内去重状态，跨 GitHub Actions 运行持久化


def github_read(path, token):
    url = f"{GH_API}/repos/{OWNER}/{REPO}/contents/{path}"
    h = {"Accept": "application/vnd.github+json", "User-Agent": "push-bot"}
    if token:
        h["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return json.loads(base64.b64decode(d["content"]).decode("utf-8"))
    except Exception as e:
        return None


def github_write(path, obj, token):
    """写入仓库文件（用于发送后立即清除 sendNow 标记，避免重复发送）。"""
    url = f"{GH_API}/repos/{OWNER}/{REPO}/contents/{path}"
    h = {"Accept": "application/vnd.github+json", "User-Agent": "push-bot",
         "Authorization": "Bearer " + token, "Content-Type": "application/json"}
    sha = None
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=30) as r:
            sha = json.loads(r.read()).get("sha")
    except Exception:
        sha = None
    content = base64.b64encode(json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
    body = {"message": "update " + path, "content": content}
    if sha:
        body["sha"] = sha
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status in (200, 201)
    except Exception as e:
        print(f"⚠️ github_write 失败：{e}")
        return False


def build_extras(extras):
    if not extras:
        return ""
    lines = ["", "---", "📌 **补充内容**"]
    for it in extras:
        t = it.get("type")
        if t == "text":
            lines.append("> " + (it.get("text") or "").replace("\n", "\n> "))
        elif t == "link":
            lines.append(f"- [{it.get('title') or '链接'}]({it.get('url') or ''})")
        elif t == "image":
            cap = it.get("caption") or "图片"
            lines.append(f"![{cap}]({it.get('url') or ''})")
            if it.get("caption"):
                lines.append(f"_{cap}_")
        elif t == "article":
            lines.append(f"**{it.get('title') or '文章'}**")
            if it.get("text"):
                lines.append(it["text"])
            if it.get("url"):
                lines.append(f"[阅读原文]({it.get('url')})")
    return "\n".join(lines)


def should_send(now, target_time, state):
    try:
        th, tm = (target_time + ":00").split(":")[:2]
        target = now.replace(hour=int(th), minute=int(tm), second=0, microsecond=0)
    except Exception:
        return False
    # 在目标时间起 60 分钟内（即下一个整点附近）、且今天尚未推送过，才发送
    if now < target or now >= target + datetime.timedelta(minutes=60):
        return False
    if state.get("date") == now.strftime("%Y-%m-%d"):
        return False
    return True


def resolve_mode(arg, cfg):
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    if arg:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", arg):
            return ("day", arg, arg)
        if arg == "today":
            return ("day", today.strftime("%Y-%m-%d"), "今日")
        if arg == "tomorrow":
            return ("day", tomorrow.strftime("%Y-%m-%d"), "明日")
        if arg == "week":
            return ("week", None, "本周")
    mode = cfg.get("push_mode", "tomorrow")
    if mode == "today":
        return ("day", today.strftime("%Y-%m-%d"), "今日")
    if mode == "week":
        return ("week", None, "本周")
    return ("day", tomorrow.strftime("%Y-%m-%d"), "明日")


def main():
    cfg = load_config()
    webhook = cfg.get("webhook_url")
    if not webhook:
        print("❌ 未配置 webhook_url，请在 config.json 或网页设置")
        return 1

    arg = sys.argv[1] if len(sys.argv) > 1 else None

    # 读取仓库里的推送配置（时间 + 补充内容 + sendNow 立即发送标记）
    repo_cfg = github_read("push-config.json", cfg.get("github_token"))
    push_time = (repo_cfg or {}).get("time") or cfg.get("push_time", "21:00")
    extras = (repo_cfg or {}).get("extras") or []
    send_now = (repo_cfg or {}).get("sendNow")
    send_now_mode = (repo_cfg or {}).get("sendNowMode") or "today"

    if arg == "auto":
        now = datetime.datetime.now()
        tok = cfg.get("github_token")
        state = github_read(STATE_PATH, tok) or {}
        # 立即发送标记优先：网页点了"立即发送"后，GitHub Actions 会在 15 分钟内捕获并发送
        if send_now:
            arg = send_now_mode
            print(f"⚡ 检测到 sendNow 标记，立即推送（{send_now_mode}）")
            # 发送后立即清除 sendNow 标记，并记录已发送日期避免同日重复
            if repo_cfg is not None:
                try:
                    repo_cfg["sendNow"] = False
                    github_write("push-config.json", repo_cfg, tok)
                    print("✅ 已清除 sendNow 标记")
                except Exception as e:
                    print(f"⚠️ 清除 sendNow 标记失败：{e}")
            state["date"] = now.strftime("%Y-%m-%d")
            github_write(STATE_PATH, state, tok)
        elif not should_send(now, push_time, state):
            print(f"ℹ️ 当前 {now:%H:%M} 未到推送时间 {push_time}，跳过")
            return 0
        else:
            state["date"] = now.strftime("%Y-%m-%d")
            github_write(STATE_PATH, state, tok)
            arg = None  # auto => 推送今日

    if not cfg.get("push_enabled", True):
        print("ℹ️ 推送已禁用，跳过")
        return 0

    if arg:
        mode_type, date_str, label = resolve_mode(arg, cfg)
    else:
        today = datetime.date.today()
        mode_type, date_str, label = ("day", today.strftime("%Y-%m-%d"), "今日")

    print(f"🚀 准备推送（{label}）...")
    client = JwAppClient()

    exams = None
    try:
        exams = client.get_exams()
    except Exception as e:
        print(f"⚠️ 获取考试失败（忽略）：{e}")

    if mode_type == "week":
        _, content = make_week_content(client.get_week_schedule())
        title = "📚 本周课表"
    else:
        di = client.get_date_schedule(date_str)
        title, content = make_push_content(di, exams=exams, mode_label=label)

    extra_md = build_extras(extras)
    if extra_md:
        content = content + extra_md
        print(f"📌 已附加补充内容：{len(extras)} 项")

    print(f"\n{title}\n{'='*40}\n{content}\n")

    result = send_webhook(
        webhook,
        cfg.get("webhook_type", "dingtalk"),
        title,
        content,
        card_buttons=CARD_BUTTONS,
    )
    print(f"📤 Webhook 结果：{result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
