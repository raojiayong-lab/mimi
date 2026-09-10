#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金智教务系统课表自动推送服务
- 本地 HTTP 服务，前端展示今日/本周课表
- 每天定时推送次日课表到 webhook（钉钉/企业微信/飞书）
- 生成 iCal 日历文件

依赖：requests、pycryptodome、aiohttp（已预置在 venv 中）
"""
import os
import sys
import json
import base64
import re
import html
import datetime
import uuid
import calendar
import urllib.parse
import urllib3
import requests
import aiohttp.web
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TOKEN_PATH = os.path.join(BASE_DIR, ".token_cache.json")
CACHE_PATH = os.path.join(BASE_DIR, ".schedule_cache.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")

JW_BASE = "https://jwapp.gypec.edu.cn/jwmobile"

# 默认请求头
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN",
    "Referer": "https://jwapp.gypec.edu.cn/jwmobile/index",
}


def load_config():
    """加载本地配置"""
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"找不到配置文件：{CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    """保存配置"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def strip_html_tags(text):
    """清理教务返回的 HTML 标签，提取纯文本"""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_course_cell(cell_detail):
    """从 cellDetail 提取课程名、教师、地点、节次"""
    if not cell_detail:
        return "", "", "", "", ""
    name = strip_html_tags(cell_detail[0].get("text", ""))
    course_type = ""
    if "[" in name:
        m = re.findall(r"\[(.*?)\]", name)
        if m:
            course_type = m[-1]
            name = name.split("[")[0].strip()

    teacher = ""
    place = ""
    sections = ""
    time_range = ""

    for item in cell_detail[1:]:
        text = item.get("text", "")
        # 通过 data-kblx 属性识别教师和地点
        t_match = re.search(r'data-kblx="02"[^>]*>([^<]+)', text)
        p_match = re.search(r'data-kblx="01"[^>]*>([^<]+)', text)
        if t_match:
            teacher = t_match.group(1).strip()
        if p_match:
            place = p_match.group(1).strip()
        # 纯文本兜底
        plain = strip_html_tags(text)
        # 提取节次 "第五节-第八节"
        sec_match = re.search(r"第([一二三四五六七八九十\d]+)节(?:[-—]第?([一二三四五六七八九十\d]+)节)?", plain)
        if sec_match:
            sections = sec_match.group(0)

    return name, course_type, teacher, place, sections


def normalize_lesson(item):
    """统一课程数据结构"""
    # 优先用 cellDetail 解析
    cell_detail = item.get("cellDetail", [])
    title_detail = item.get("titleDetail", [])
    if cell_detail:
        name, ctype, teacher, place, sections = parse_course_cell(cell_detail)
        if not name:
            name = item.get("courseName", "")
        # 兜底：从 titleDetail 的 HTML 中提取教师和地点
        if not teacher or not place:
            title_html = " ".join(title_detail) if isinstance(title_detail, list) else str(title_detail)
            if not teacher:
                t = re.search(r'data-kblx="02"[^>]*>([^<]+)', title_html)
                if t:
                    teacher = t.group(1).strip()
            if not place:
                p = re.search(r'data-kblx="01"[^>]*>([^<]+)', title_html)
                if p:
                    place = p.group(1).strip()
        return {
            "courseName": name,
            "courseType": ctype,
            "teacher": teacher,
            "place": place or item.get("placeName") or "",
            "sections": sections,
            "beginSection": item.get("startSession", item.get("beginSection")),
            "endSection": item.get("endSession", item.get("endSection")),
            "beginTime": item.get("startTime", item.get("beginTime")),
            "endTime": item.get("endTime", ""),
            "dayOfWeek": item.get("todayWeekDay") or item.get("dayOfWeek"),
            "weekDayText": item.get("weekDay"),
            "raw": item,
        }
    else:
        # arrangedList 的格式
        name = item.get("courseName", "")
        ctype = ""
        teacher = ""
        place = item.get("placeName", "")
        return {
            "courseName": name,
            "courseType": ctype,
            "teacher": teacher,
            "place": place,
            "beginSection": item.get("beginSection"),
            "endSection": item.get("endSection"),
            "beginTime": item.get("beginTime", ""),
            "endTime": item.get("endTime", ""),
            "dayOfWeek": item.get("dayOfWeek"),
            "weeksAndTeachers": item.get("weeksAndTeachers", ""),
            "raw": item,
        }


class JwAppClient:
    def __init__(self, config=None):
        self.config = config or load_config()
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.token = self._load_cached_token()

    def _load_cached_token(self):
        if os.path.exists(TOKEN_PATH):
            try:
                with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("token")
            except Exception:
                return None
        return None

    def _save_token(self, token):
        self.token = token
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump({"token": token}, f, ensure_ascii=False)

    def _rsa_encrypt(self, password, public_key_pem):
        key = RSA.import_key(public_key_pem)
        cipher = PKCS1_v1_5.new(key)
        encrypted = cipher.encrypt(password.encode("utf-8"))
        return base64.b64encode(encrypted).decode("utf-8")

    def login(self):
        """执行 RSA 登录并缓存 token"""
        cfg = self.config
        # 1. 获取公钥
        r = self.session.get(f"{JW_BASE}/auth/getPublicKey", verify=False, timeout=30)
        r.raise_for_status()
        key_data = r.json()["data"]
        key_id = key_data["keyId"]
        public_key_pem = key_data["publicKey"]

        # 2. 加密密码
        encrypted_pwd = self._rsa_encrypt(cfg["password"], public_key_pem)

        # 3. 登录
        body = {
            "loginName": cfg["username"],
            "loginPassword": encrypted_pwd,
            "openId": "",
            "keyId": key_id,
        }
        r = self.session.post(f"{JW_BASE}/auth/login/mobile", json=body, verify=False, timeout=30)
        resp = r.json()
        if resp.get("code") != 200:
            raise RuntimeError(f"登录失败：{resp.get('msg')}")
        token = resp["data"]["token"]
        self._save_token(token)
        return token

    def _request(self, method, url, **kwargs):
        """带 token 的请求，失败自动重登一次"""
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = self.token or ""
        kwargs["headers"] = headers
        kwargs.setdefault("verify", False)
        kwargs.setdefault("timeout", 30)

        r = self.session.request(method, url, **kwargs)
        # token 失效则重登
        if r.status_code == 401 or (r.status_code == 200 and r.json().get("code") in (401, 403, 500) and "登录" in r.text):
            self.login()
            headers["Authorization"] = self.token
            r = self.session.request(method, url, **kwargs)
        r.raise_for_status()
        return r

    def ensure_login(self):
        """确保已登录"""
        if not self.token:
            self.login()
        # 简单测试 token 是否有效
        r = self._request("GET", f"{JW_BASE}/biz/user/info")
        if r.json().get("code") != 200:
            self.login()

    def get_user_info(self):
        self.ensure_login()
        r = self._request("GET", f"{JW_BASE}/biz/user/info")
        return r.json()

    def get_term_list(self):
        self.ensure_login()
        r = self._request("GET", f"{JW_BASE}/biz/v410/schedule/termList")
        return r.json()["data"]

    def get_current_term_and_week(self):
        """返回当前学期和当前周号"""
        cfg = self.config
        terms = self.get_term_list()
        current_term = None
        for t in terms:
            if t.get("currentFlag"):
                current_term = t
                break
        if not current_term:
            current_term = terms[-1]
        term_code = current_term["termCode"]

        r = self._request("GET", f"{JW_BASE}/biz/v410/schedule/getTermWeeks", params={"termCode": term_code})
        weeks = r.json()["data"]
        current_week = None
        for w in weeks:
            if w.get("curWeek"):
                current_week = w
                break
        return {
            "term": current_term,
            "week": current_week,
            "weeks": weeks,
        }

    def get_today_lessons(self):
        self.ensure_login()
        r = self._request("GET", f"{JW_BASE}/biz/v410/schedule/listStudentTodayLesson")
        data = r.json().get("data", {}).get("data", [])
        return [normalize_lesson(x) for x in data]

    def get_week_schedule(self, term_code=None, serial_number=None):
        """获取指定学期/周的完整课表"""
        self.ensure_login()
        if not term_code or not serial_number:
            info = self.get_current_term_and_week()
            term_code = term_code or info["term"]["termCode"]
            serial_number = serial_number or info["week"]["serialNumber"]

        r = self._request("POST", f"{JW_BASE}/biz/v410/schedule/getMyScheduleDetail",
                          json={"termCode": term_code, "serialNumber": serial_number})
        data = r.json()["data"]
        arranged = [normalize_lesson(x) for x in data.get("arrangedList", [])]
        not_arranged = [normalize_lesson(x) for x in data.get("notArrangeList", [])]
        practice = [normalize_lesson(x) for x in data.get("practiceList", [])]
        return {
            "termCode": term_code,
            "serialNumber": serial_number,
            "studentName": data.get("name", ""),
            "studentCode": data.get("code", ""),
            "arranged": arranged,
            "notArranged": not_arranged,
            "practice": practice,
        }

    def get_date_schedule(self, date_str=None):
        """获取某一天的课程（date_str: YYYY-MM-DD），会自动判断所在学期和周"""
        if not date_str:
            date_obj = datetime.date.today()
        else:
            date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()

        # 找当前学期和包含该日期的周
        terms = self.get_term_list()
        target_term = None
        for t in terms:
            # currentFlag 为 true 的学期，如果日期不在其中，则尝试根据学期名称推断
            if t.get("currentFlag"):
                target_term = t
                break
        if not target_term:
            target_term = terms[-1]

        term_code = target_term["termCode"]
        r = self._request("GET", f"{JW_BASE}/biz/v410/schedule/getTermWeeks", params={"termCode": term_code})
        weeks = r.json()["data"]
        target_week = None
        for w in weeks:
            start = w.get("startDate", "")[:10]
            end = w.get("endDate", "")[:10]
            if start and end:
                s = datetime.datetime.strptime(start, "%Y-%m-%d").date()
                e = datetime.datetime.strptime(end, "%Y-%m-%d").date()
                if s <= date_obj <= e:
                    target_week = w
                    break
        if not target_week:
            target_week = weeks[0] if weeks else {"serialNumber": 1}

        schedule = self.get_week_schedule(term_code, target_week["serialNumber"])
        weekday = date_obj.isoweekday()  # 1=周一
        today_lessons = [x for x in schedule["arranged"] if int(x.get("dayOfWeek") or 0) == weekday]
        today_lessons.sort(key=lambda x: x.get("beginSection") or 99)
        return {
            "date": date_obj.strftime("%Y-%m-%d"),
            "weekday": weekday,
            "weekdayText": ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday],
            "termCode": term_code,
            "serialNumber": target_week["serialNumber"],
            "lessons": today_lessons,
            "studentName": schedule["studentName"],
        }

    def get_exams(self):
        self.ensure_login()
        r = self._request("GET", f"{JW_BASE}/biz/v410/examTask/recentExams")
        resp = r.json()
        # 返回内部 data 部分（可能是 list / dict / None）
        return resp.get("data", None)


def build_ical_event(lesson, date_obj):
    """把一节课转成 VEVENT"""
    # 估算时间：第一节 08:00 开始，每节 45 分钟，课间 10 分钟
    # 简单映射：1=08:00, 2=08:55, 3=10:00, 4=10:55, 5=13:30, 6=14:20, 7=15:30, 8=16:20
    section_time_map = {
        1: "08:00", 2: "08:55", 3: "10:00", 4: "10:55",
        5: "13:30", 6: "14:20", 7: "15:30", 8: "16:20"
    }
    begin = lesson.get("beginTime") or section_time_map.get(lesson.get("beginSection"), "08:00")
    end = lesson.get("endTime") or section_time_map.get(lesson.get("endSection") or (lesson.get("beginSection") or 1) + 1, "08:45")

    start_dt = datetime.datetime.strptime(f"{date_obj} {begin}", "%Y-%m-%d %H:%M")
    end_dt = datetime.datetime.strptime(f"{date_obj} {end}", "%Y-%m-%d %H:%M")

    uid = f"{date_obj}-{lesson.get('beginSection')}-{lesson.get('courseName')}-classbot"
    uid = re.sub(r"[^a-zA-Z0-9-]", "_", uid)[:60]

    summary = lesson.get("courseName", "课程")
    location = lesson.get("place", "")
    description = f"教师：{lesson.get('teacher', '')}｜{lesson.get('courseType', '')}"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTART;TZID=Asia/Shanghai:{start_dt.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID=Asia/Shanghai:{end_dt.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{summary}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    if description:
        lines.append(f"DESCRIPTION:{description}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def generate_ical(lessons, date_str):
    """生成 iCal 文件内容"""
    events = []
    for lesson in lessons:
        events.append(build_ical_event(lesson, date_str))
    body = "\r\n".join(events)
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//ClassBot//CN\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        "BEGIN:VTIMEZONE\r\n"
        "TZID:Asia/Shanghai\r\n"
        "END:VTIMEZONE\r\n"
        f"{body}\r\n"
        "END:VCALENDAR"
    )


def format_lesson_text(lesson):
    """把课程格式化成一行文本"""
    sections = f"第{lesson.get('beginSection')}节"
    if lesson.get("endSection") != lesson.get("beginSection"):
        sections += f"-第{lesson.get('endSection')}节"
    time_range = ""
    if lesson.get("beginTime"):
        time_range = f"({lesson.get('beginTime')}"
        if lesson.get("endTime"):
            time_range += f"-{lesson.get('endTime')}"
        time_range += ")"
    return f"{sections}{time_range} {lesson.get('courseName')} [{lesson.get('courseType')}] | {lesson.get('teacher')} | {lesson.get('place')}".strip()


def send_webhook(webhook_url, webhook_type, title, content, card_buttons=None, secret=None):
    """发送 webhook 消息

    card_buttons: 钉钉 actionCard 的按钮列表，如
        [{"title": "看课表", "actionURL": "https://..."}]
    传入则发送带按钮的互动卡片；不传则发普通 markdown。
    secret: 钉钉机器人“加签”安全设置下的密钥；为空则按“自定义关键词”模式发送。
    """
    if not webhook_url:
        return {"ok": False, "msg": "未配置 webhook_url"}

    # 钉钉：若配置了加签密钥，则追加 timestamp + sign（兼容“加签”安全设置）
    if webhook_type == "dingtalk" and secret:
        import hmac, hashlib, time
        ts = str(int(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        mac = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(mac).decode("utf-8"))
        sep = "&" if "?" in webhook_url else "?"
        webhook_url = f"{webhook_url}{sep}timestamp={ts}&sign={sign}"

    # 钉钉
    if webhook_type == "dingtalk":
        text = f"### {title}\n\n{content}"
        if card_buttons:
            payload = {
                "msgtype": "actionCard",
                "actionCard": {
                    "title": title,
                    "text": text,
                    "btnOrientation": "0",
                    "btns": card_buttons,
                },
            }
        else:
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": text},
            }
        r = requests.post(webhook_url, json=payload, timeout=30)
        return r.json() if r.text else {"ok": True}

    # 企业微信
    elif webhook_type == "wecom":
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"**{title}**\n\n{content}"
            }
        }
        r = requests.post(webhook_url, json=payload, timeout=30)
        return r.json() if r.text else {"ok": True}

    # 飞书
    elif webhook_type == "feishu":
        payload = {
            "msg_type": "text",
            "content": {
                "text": f"{title}\n\n{content}"
            }
        }
        r = requests.post(webhook_url, json=payload, timeout=30)
        return r.json() if r.text else {"ok": True}

    return {"ok": False, "msg": "不支持的 webhook_type"}


def make_exam_block(exams):
    """返回考试倒计时文本块（无考试返回空串）"""
    if not exams:
        return ""
    items = exams if isinstance(exams, list) else [exams]
    today = datetime.date.today()
    upcoming = []
    for ex in items:
        if not isinstance(ex, dict):
            continue
        name = ex.get("examName") or ex.get("courseName") or ex.get("name") or "考试"
        date_str = (ex.get("examDate") or ex.get("examTime") or ex.get("date")
                    or ex.get("startTime") or ex.get("kssj") or "")[:10]
        if not date_str:
            continue
        try:
            d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        delta = (d - today).days
        if delta < 0:
            continue
        upcoming.append((delta, name, date_str))
    if not upcoming:
        return ""
    upcoming.sort()
    lines = ["\n📝 **近期考试**"]
    for delta, name, date_str in upcoming[:3]:
        if delta == 0:
            lines.append(f"- {name}：就是今天！({date_str})")
        else:
            lines.append(f"- {name}：还有 **{delta}** 天（{date_str}）")
    return "\n".join(lines)


def make_push_content(date_info, exams=None, mode_label="明日"):
    """构造某一天推送文本（mode_label: 今日/明日/日期）"""
    title = f"📚 {mode_label}课表"
    lines = []
    if not date_info["lessons"]:
        wd = date_info.get("weekday")
        if wd in (6, 7):
            lines.append("🎉 周末啦，今天没有课，好好休息～")
        else:
            lines.append("🎉 今天没有课，可以好好休息～")
    else:
        lines.append(f"**{date_info['date']} {date_info['weekdayText']}**")
        for i, lesson in enumerate(date_info["lessons"], 1):
            lines.append(f"{i}. {format_lesson_text(lesson)}")
    content = "\n".join(lines)
    if exams:
        content += make_exam_block(exams)
    return title, content


def make_week_content(week_data):
    """构造整周课表推送文本"""
    arranged = week_data.get("arranged", [])
    by_day = {i: [] for i in range(1, 8)}
    for x in arranged:
        dow = int(x.get("dayOfWeek") or 0)
        if dow in by_day:
            by_day[dow].append(x)
    wd_names = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    lines = [f"**第 {week_data.get('serialNumber', '?')} 周**"]
    for d in range(1, 8):
        lessons = sorted(by_day[d], key=lambda x: x.get("beginSection") or 99)
        if lessons:
            lines.append(f"\n**{wd_names[d]}**")
            for i, les in enumerate(lessons, 1):
                lines.append(f"{i}. {format_lesson_text(les)}")
        else:
            lines.append(f"\n**{wd_names[d]}**　休息")
    title = "📚 本周课表"
    return title, "\n".join(lines)


# ============== aiohttp 路由 ==============

client = None


def get_client():
    global client
    if client is None:
        client = JwAppClient()
    return client


async def handle_index(request):
    """前端主页"""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return aiohttp.web.Response(text=f.read(), content_type="text/html")
    return aiohttp.web.Response(text="index.html not found", status=404)


async def handle_api_config(request):
    """获取/更新配置"""
    if request.method == "GET":
        cfg = load_config()
        # 不返回密码
        safe = {k: v for k, v in cfg.items() if k != "password"}
        return aiohttp.web.json_response(safe)
    try:
        body = await request.json()
        cfg = load_config()
        for key in ["webhook_url", "webhook_type", "push_time", "push_enabled", "term_code"]:
            if key in body:
                cfg[key] = body[key]
        if "password" in body:
            cfg["password"] = body["password"]
        save_config(cfg)
        # 刷新 client
        global client
        client = JwAppClient()
        return aiohttp.web.json_response({"code": 200, "msg": "已保存"})
    except Exception as e:
        return aiohttp.web.json_response({"code": 500, "msg": str(e)}, status=500)


async def handle_api_today(request):
    try:
        data = get_client().get_today_lessons()
        return aiohttp.web.json_response({"code": 200, "data": data})
    except Exception as e:
        return aiohttp.web.json_response({"code": 500, "msg": str(e)}, status=500)


async def handle_api_date(request):
    try:
        date_str = request.query.get("date")
        data = get_client().get_date_schedule(date_str)
        return aiohttp.web.json_response({"code": 200, "data": data})
    except Exception as e:
        return aiohttp.web.json_response({"code": 500, "msg": str(e)}, status=500)


async def handle_api_week(request):
    try:
        term_code = request.query.get("termCode") or None
        serial = request.query.get("serialNumber")
        serial = int(serial) if serial else None
        data = get_client().get_week_schedule(term_code, serial)
        return aiohttp.web.json_response({"code": 200, "data": data})
    except Exception as e:
        return aiohttp.web.json_response({"code": 500, "msg": str(e)}, status=500)


async def handle_api_term(request):
    try:
        data = get_client().get_term_list()
        return aiohttp.web.json_response({"code": 200, "data": data})
    except Exception as e:
        return aiohttp.web.json_response({"code": 500, "msg": str(e)}, status=500)


async def handle_api_exams(request):
    try:
        data = get_client().get_exams()
        return aiohttp.web.json_response({"code": 200, "data": data})
    except Exception as e:
        return aiohttp.web.json_response({"code": 500, "msg": str(e)}, status=500)


async def handle_api_ics(request):
    try:
        date_str = request.query.get("date", datetime.date.today().strftime("%Y-%m-%d"))
        info = get_client().get_date_schedule(date_str)
        ics = generate_ical(info["lessons"], date_str)
        return aiohttp.web.Response(
            text=ics,
            content_type="text/calendar",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="class_{date_str}.ics"'}
        )
    except Exception as e:
        return aiohttp.web.json_response({"code": 500, "msg": str(e)}, status=500)


async def handle_api_push(request):
    """手动触发推送（默认按 config.push_mode，支持 ?mode=today|tomorrow|week|日期）"""
    try:
        cfg = load_config()
        if not cfg.get("webhook_url"):
            return aiohttp.web.json_response({"code": 400, "msg": "未配置 webhook_url，请先在网页设置"})

        mode = request.query.get("mode") or cfg.get("push_mode", "tomorrow")
        client = get_client()
        SCHEDULE_URL = "https://jwapp.gypec.edu.cn/jwmobile/index#/index/home/service"
        buttons = [
            {"title": "📖 看完整课表", "actionURL": SCHEDULE_URL},
        ]
        exams = None
        try:
            exams = client.get_exams()
        except Exception:
            exams = None

        if mode == "week":
            week = client.get_week_schedule()
            title, content = make_week_content(week)
        else:
            if mode == "today":
                date_str = datetime.date.today().strftime("%Y-%m-%d")
                label = "今日"
            elif mode == "tomorrow":
                date_str = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                label = "明日"
            else:
                date_str = mode
                label = mode
            date_info = client.get_date_schedule(date_str)
            title, content = make_push_content(date_info, exams=exams, mode_label=label)

        result = send_webhook(cfg["webhook_url"], cfg.get("webhook_type", "dingtalk"), title, content, card_buttons=buttons)
        return aiohttp.web.json_response({"code": 200, "msg": "已推送", "webhook_result": result})
    except Exception as e:
        return aiohttp.web.json_response({"code": 500, "msg": str(e)}, status=500)


async def handle_api_status(request):
    try:
        cfg = load_config()
        info = get_client().get_user_info()
        # 金智字段映射：xm=姓名, xh=学号, className=班级, yxmc=院系, zymc=专业
        user = {
            "name": info.get("xm", ""),
            "code": info.get("xh", ""),
            "className": info.get("className", ""),
            "college": info.get("yxmc", ""),
            "major": info.get("zymc", ""),
        }
        return aiohttp.web.json_response({
            "code": 200,
            "data": {
                "user": user,
                "webhook_url_set": bool(cfg.get("webhook_url")),
                "push_enabled": cfg.get("push_enabled", True),
            }
        })
    except Exception as e:
        return aiohttp.web.json_response({"code": 500, "msg": str(e)}, status=500)


def build_app():
    app = aiohttp.web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/config", handle_api_config)
    app.router.add_post("/api/config", handle_api_config)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_get("/api/today", handle_api_today)
    app.router.add_get("/api/date", handle_api_date)
    app.router.add_get("/api/week", handle_api_week)
    app.router.add_get("/api/term", handle_api_term)
    app.router.add_get("/api/exams", handle_api_exams)
    app.router.add_get("/api/ics", handle_api_ics)
    app.router.add_get("/api/push", handle_api_push)
    app.router.add_static("/static/", path=STATIC_DIR, name="static")
    return app


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5678
    # 初始化时尝试登录一次，验证账号
    try:
        get_client().ensure_login()
        print(f"✅ 登录成功：{get_client().config['username']}")
    except Exception as e:
        print(f"⚠️ 登录测试失败：{e}")
        print("请检查 config.json 中的账号密码")

    app = build_app()
    print(f"🚀 服务已启动：http://localhost:{port}")
    aiohttp.web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
