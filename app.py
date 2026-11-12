#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot-hosting 自动续期脚本 —— 多账号版

账号配置（二选一）：
  1.【推荐】ACCOUNTS 环境变量，纯 JSON 数组（GitHub Secrets 里不要在外面再套引号）：
     [
       {"email":"a@example.com","password":"密码1"},
       {"email":"b@example.com","password":"密码2"}
     ]
  2. 只有一个账号时，沿用旧的 EMAIL / PASSWORD 两个变量

所有账号跑完后，只发一条汇总 Telegram 通知。
"""

import os, re, sys, json, time, random, requests, subprocess, ctypes
from seleniumbase import SB

# ================= 环境变量配置 =================
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID") or ""     # 不填写则不通知，需和 bot token 一起填
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""

BASE_URL = "https://bot-hosting.net"


# ================= 账号读取 =================
def load_accounts():
    """返回 [{"email":..., "password":...}, ...]"""
    accounts = []

    raw = (os.environ.get("ACCOUNTS") or "").lstrip("\ufeff").strip()
    print(f"🔍 调试信息：读到的 ACCOUNTS 长度 = {len(raw)} 字符")  # 只打印长度，不泄露内容

    if raw:
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("ACCOUNTS 必须是 JSON 数组，形如 [{...}, {...}]")
            for i, item in enumerate(data, 1):
                email = (item.get("email") or item.get("username") or "").strip()
                password = item.get("password") or ""
                if not (email and password):
                    print(f"⚠️ 跳过第 {i} 个账号配置：缺少 email/password")
                    continue
                accounts.append({"email": email, "password": password})
        except Exception as e:
            print(f"❌ 解析 ACCOUNTS 失败: {e}")
            print('👉 请确认 Secret 值是纯 JSON，例如: [{"email":"a@x.com","password":"pw"}]')
            sys.exit(1)

    # 兼容旧的单账号写法
    if not accounts:
        email = os.environ.get("EMAIL") or ""
        password = os.environ.get("PASSWORD") or ""
        if email and password:
            accounts.append({"email": email, "password": password})

    return accounts


# ================= 通知 =================
def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")


def mask_email(email: str) -> str:
    if '@' in email:
        name, domain = email.split('@', 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    return email[:2] + '****'


def format_summary(results: list) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    lines = [f"🇫🇮 Bot-hosting 续期通知（共 {len(results)} 个账号）", f"⏱️ 时间: {now}", ""]
    for r in results:
        lines.append(f"👤 账户: {r['email']}")
        lines.append(r["status"])
        if r.get("expiry"):
            lines.append(f"📅 到期时间: {r['expiry']}")
        if r.get("extra"):
            lines.append(r["extra"])
        if r.get("error"):
            lines.append(f"⚠️ 错误信息: {r['error']}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ================= 基础工具 =================
def js(sb, script, default=None, errors=None):
    try:
        result = sb.execute_script(script)
        return default if result is None else result
    except Exception as e:
        if errors is not None:
            errors.append(f"execute_script: {type(e).__name__}: {e}")
        try:
            result = sb.cdp.evaluate("(function(){ " + script + " })()")
            return default if result is None else result
        except Exception as e2:
            if errors is not None:
                errors.append(f"cdp.evaluate: {type(e2).__name__}: {e2}")
            return default


# Turnstile 定位失败时的现场诊断快照
def diag_turnstile_dom(sb, errors=None):
    snap = js(sb, """
        var out = [];
        var ifr = document.querySelectorAll('iframe');
        out.push('top-doc iframes: ' + ifr.length);
        for (var i = 0; i < ifr.length; i++) {
            var r = ifr[i].getBoundingClientRect();
            out.push('iframe w=' + Math.round(r.width) + ' h=' + Math.round(r.height) + ' src=' + (ifr[i].src || '').slice(0, 90));
        }
        var ts = document.querySelectorAll('[class*="turnstile"], [id*="turnstile"], input[name="cf-turnstile-response"]');
        out.push('turnstile-ish elements: ' + ts.length);
        for (var j = 0; j < ts.length; j++) {
            var rr = ts[j].getBoundingClientRect();
            var cls = (typeof ts[j].className === 'string') ? ts[j].className : (ts[j].getAttribute('class') || '');
            out.push(' - ' + ts[j].tagName + ' class=' + cls + ' w=' + Math.round(rr.width) + ' h=' + Math.round(rr.height));
        }
        out.push('window.turnstile=' + (typeof window.turnstile) + ' readyState=' + document.readyState);
        return out.join('\\n');
    """, "")
    for line in str(snap).splitlines():
        print("   [diag] " + line)
    if errors:
        for err in errors:
            print("   [diag][exec-error] " + str(err)[:200])


# 页面注入脚本
TURNSTILE_TOKEN_JS = """
try { if (window.turnstile && window.turnstile.getResponse) {
        var t = window.turnstile.getResponse(); if (t) return t; } } catch (e) {}
var el = document.querySelector('input[name="cf-turnstile-response"]');
return el ? (el.value || '') : '';
"""

TURNSTILE_EXISTS_JS = """
return document.querySelector(
    'input[name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"], .cf-turnstile'
) !== null;
"""
TS_COORDS_JS = """
function findWidget(root) {
    var els = root.querySelectorAll(
        'iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [id*="cf-chl"]');
    for (var i = 0; i < els.length; i++) {
        var r = els[i].getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return els[i];
    }
    var all = root.querySelectorAll('*');
    for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) {
            var f = findWidget(all[j].shadowRoot);
            if (f) return f;
        }
    }
    return null;
}
function boxFromTokenInput() {
    var el = document.querySelector('input[name="cf-turnstile-response"]');
    var cur = el ? el.parentElement : null;
    for (var k = 0; k < 6 && cur; k++) {
        var r = cur.getBoundingClientRect();
        if (r.width > 100 && r.width < 600 && r.height > 30 && r.height < 140) return cur;
        cur = cur.parentElement;
    }
    return null;
}
var hit = findWidget(document) || boxFromTokenInput();
if (!hit) return null;
var hr = hit.getBoundingClientRect();
return {cx: Math.round(hr.x + 30), cy: Math.round(hr.y + hr.height / 2)};
"""


# Turnstile 脚本异步渲染，轮询等待组件出现
def wait_turnstile_present(sb, timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(sb, TURNSTILE_EXISTS_JS, False):
            return True
        time.sleep(1)
    return False


def _activate_window():
    if os.name == "nt":
        return

    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls],
                               capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]],
                               timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"],
                       timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _windows_mouse_click(x: int, y: int):
    try:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.08)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    except Exception:
        pass


def _xdotool_click(x: int, y: int):
    if os.name == "nt":
        _windows_mouse_click(x, y)
        return

    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)],
                       timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")


def turnstile_token(sb) -> str:
    token = js(sb, TURNSTILE_TOKEN_JS, "")
    return token if isinstance(token, str) else ""


def _click_turnstile(sb, locate_timeout=10) -> bool:
    coords = None
    errors = []
    deadline = time.time() + locate_timeout
    while time.time() < deadline:
        coords = js(sb, TS_COORDS_JS, None, errors)
        if coords:
            break
        time.sleep(1)
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标, 打印现场诊断:")
        diag_turnstile_dom(sb, errors)
        return False

    try:
        wi = js(sb, "return '' + (window.screenX || 0) + '|' + (window.screenY || 0) + '|' +"
                    " window.outerHeight + '|' + window.innerHeight;", "")
        parts = str(wi).split("|")
        sx, sy = int(parts[0] or 0), int(parts[1] or 0)
        bar = int(parts[2] or 800) - int(parts[3] or 768)
        ax = int(coords["cx"]) + sx
        ay = int(coords["cy"]) + sy + bar
        print(f"🖱️ 底层点击 Turnstile ({ax}, {ay})")
        _xdotool_click(ax, ay)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"⚠️ 底层点击失败: {e}")
        return False


def click_turnstile(sb) -> bool:
    if _click_turnstile(sb):
        return True
    coords = js(sb, TS_COORDS_JS)
    if coords:
        try:
            if hasattr(sb, "cdp") and hasattr(sb.cdp, "gui_click_x_y"):
                print(f"🖱️ 坐标点击 Turnstile ({coords['cx']},{coords['cy']})")
                sb.cdp.gui_click_x_y(int(coords["cx"]), int(coords["cy"]))
                return True
        except Exception as e:
            print(f"⚠️ 坐标点击失败: {e}")
    try:
        if hasattr(sb, "cdp") and hasattr(sb.cdp, "click_captcha"):
            print("🖱️ 兜底调用cdp点击 ...")
            sb.cdp.click_captcha()
            return True
        print("⚠️ 当前 SeleniumBase 版本无 sb.cdp.click_captcha")
    except Exception as e:
        print(f"⚠️ click_captcha 出错: {e}")
    return False


def solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8) -> bool:
    if turnstile_token(sb):
        return True

    deadline = time.time() + timeout
    next_click = time.time() + first_wait
    clicks = 0
    while time.time() < deadline:
        if turnstile_token(sb):
            print(f"✅ {tag}已通过(已获取 token)")
            return True
        if time.time() >= next_click:
            clicks += 1
            print(f"🖱️ 第 {clicks} 次点击 Turnstile 验证框...")
            click_turnstile(sb)
            next_click = time.time() + 10 + random.uniform(0, 3)
        time.sleep(2)
    ok = bool(turnstile_token(sb))
    print(f"{'✅' if ok else '❌'} {tag}{'已通过' if ok else '未通过'}")
    return ok


def handle_turnstile(sb) -> bool:
    print("🛡️ 处理 Turnstile 验证...")
    return solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8)


# 获取当前出口ip
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()


# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except Exception:
        return countdown_str


# 归一化日期为 YYYY/MM/DD
def normalize_date(date_str: str) -> str:
    parts = date_str.split('/')
    if len(parts) == 3:
        if len(parts[2]) == 4:   # MM/DD/YYYY
            return f"{parts[2]}/{int(parts[0]):02d}/{int(parts[1]):02d}"
        if len(parts[0]) == 4:   # YYYY/MM/DD
            return f"{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}"
    return date_str


DATE_RE = r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})"


# 获取过期日期
def extract_expiry_date(page_source: str) -> str:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*" + DATE_RE,   # 优先匹配包含 "Expires" 的
        DATE_RE,                                # 兜底匹配
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            return normalize_date(match.group(1))
    return None


# 优先从 DOM 的 st-meta 元素提取到期日期 (不依赖页面语言)
def get_expiry_from_dom(sb) -> str:
    for selector in ['span.st-meta', '.st-meta', '[class*="st-meta"]']:
        try:
            if sb.is_element_present(selector):
                match = re.search(DATE_RE, sb.get_text(selector))
                if match:
                    return normalize_date(match.group(1))
        except Exception:
            pass
    return None


# ================= 单个账号的完整流程 =================
def process_account(sb, email: str, password: str) -> dict:
    """处理一个账号，返回结果字典（不在这里发通知，统一在最后汇总发送）"""
    res = {"email": mask_email(email), "status": "", "extra": "", "expiry": "", "error": ""}

    # ---------- 登录 ----------
    login_ok = False
    print("🚀 开始登录...")
    try:
        print(f"🌐 访问 {BASE_URL}/login")
        sb.open(f"{BASE_URL}/login")
        sb.wait_for_ready_state_complete()
        sb.sleep(3)
        sb.click('button[type="submit"]')  # 点击 Continue with email 按钮
        sb.sleep(12)  # 等待页面加载
        print("📧 填写邮箱...")
        sb.input('input[type="email"]', email)
        sb.sleep(1)
        print("🔑 填写密码...")
        sb.input('input[type="password"]', password)
        sb.sleep(3)

        if wait_turnstile_present(sb):
            if not handle_turnstile(sb):
                print("❌ 登录界面的 Turnstile 验证失败，尝试兜底点击...")
                try:
                    sb.cdp.click_captcha()
                    sb.sleep(4)
                except Exception as e:
                    print(f"⚠️ 兜底点击失败: {e}")
        else:
            print("ℹ️ 未检测到 Turnstile")
        sb.sleep(1)
        sb.click('button[type="submit"]')  # 点击登录按钮
        print("🖱️ 提交登录按钮")
        sb.sleep(6)
        sb.wait_for_ready_state_complete()
        sb.open(f"{BASE_URL}/dash/billings")
        sb.wait_for_ready_state_complete()
        sb.sleep(3)
        current_url = sb.get_current_url()
        current_title = sb.get_title()
        print(f"📝 当前URL: {current_url}, Title: {current_title}")

        if "/dash/billings" in current_url and "/login" not in current_url and "error=" not in current_url:
            login_ok = True
            print("✅ Password 登录成功,当前已到达账单页")
        else:
            print(f"❌ Password 登录后仍未到达账单页，当前URL: {current_url}")
    except Exception as e:
        print(f"❌ Password 登录过程异常: {type(e).__name__}: {str(e)[:150]}")

    if not login_ok:
        res["status"] = "❌ 登录失败"
        res["error"] = "Password 登录失败"
        return res

    # ---------- 提取当前到期日期 ----------
    sb.sleep(3)
    try:
        page_source = sb.get_page_text() + "\n" + sb.get_page_source()
    except Exception:
        page_source = sb.get_page_source()
    current_expiry = get_expiry_from_dom(sb) or extract_expiry_date(page_source)
    if current_expiry:
        print(f"📅 当前到期日期: {current_expiry}")
    else:
        print("⚠️ 未能提取当前到期日期")

    # ---------- 寻找外部续期按钮 ----------
    outer_renew_selector = None
    countdown_text = None
    possible_selectors = [
        'type:button("Renew free plan")',
        'button:contains("Renew free plan")',
        '[class*="btn btn-primary btn-sm"]',
    ]

    for selector in possible_selectors:
        try:
            if sb.is_element_visible(selector):
                button_text = sb.get_text(selector)
                # "Renew in HH:MM:SS" / "Renewal cooldown HH:MM:SS" 都表示冷却中
                match = re.search(r"(?:Renew in|Renewal cooldown)\s+(\d{1,2}:\d{2}:\d{2})", button_text, re.I)
                if match:
                    countdown_text = match.group(1)
                    print(f"⏳ 续期冷却中, 未到续期时间: '{button_text}'")
                    break
                elif "Renew" in button_text:
                    outer_renew_selector = selector
                    print(f"✅ 续期按钮可用: '{button_text}'")
                    break
        except Exception:
            pass

    # ---------- 没有可点的续期按钮 ----------
    if not outer_renew_selector:
        res["expiry"] = current_expiry or "（未获取到）"
        if countdown_text:
            friendly = format_countdown(countdown_text)
            print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
            res["status"] = "⏳ 未到续期时间"
            res["extra"] = f"⏱️ 可续期时间: {friendly}后"
        else:
            print("ℹ️ 未找到续期按钮或倒计时，状态未知")
            res["status"] = "ℹ️ 无需续期"
            res["extra"] = "当前状态未知，请手动检查"
        return res

    # ---------- 点击外部续期按钮，等待弹窗 ----------
    print("🔄 点击外部续期按钮，等待验证窗口...")
    try:
        sb.sleep(2)
        sb.click(outer_renew_selector)
        sb.sleep(15)  # 等待模态框加载，可能因网络因素加载慢
    except Exception as e:
        print(f"❌ 点击外部按钮失败: {e}")
        res["status"] = "❌ 续期失败"
        res["error"] = "点击外部续期按钮出错"
        return res

    # ---------- 弹窗中的 Turnstile ----------
    turnstile_passed = True  # 未检测到 Turnstile 时视为无需验证
    if wait_turnstile_present(sb):
        turnstile_passed = handle_turnstile(sb)
        if not turnstile_passed:
            print("❌ Turnstile 验证失败，尝试兜底点击...")
            try:
                sb.cdp.click_captcha()
                sb.sleep(4)
                turnstile_passed = bool(turnstile_token(sb))
            except Exception as e:
                print(f"⚠️ 兜底点击失败: {e}")
    else:
        print("ℹ️ 未检测到 Turnstile")

    if not turnstile_passed:
        print("❌ Turnstile 验证最终未通过")
        res["status"] = "❌ 续期失败"
        res["error"] = "Turnstile 验证未通过"
        return res

    # ---------- 点击弹窗里的续期按钮 ----------
    print("⏳ 等待续期按钮可用并点击...")
    time.sleep(5)
    try:
        sb.click('div[role="dialog"] button.btn-primary', timeout=8)
        print("✅ 已点击续期按钮")
    except Exception as e:
        print(f"续期按钮点击失败: {e}")

    print("⏳ 等待新的过期时间...")
    sb.sleep(6)

    # ---------- 提取续期后的结果 ----------
    try:
        new_page_text = sb.get_page_text() + "\n" + sb.get_page_source()
    except Exception:
        new_page_text = sb.get_page_source()
    new_expiry = get_expiry_from_dom(sb) or extract_expiry_date(new_page_text)
    new_match = re.search(r"(?:Renewal cooldown|Renew in)\s+(\d{1,2}:\d{2}:\d{2})", new_page_text)

    if new_match:
        new_countdown = new_match.group(1)
        print(f"✅ 续期成功！新的倒计时: {new_countdown}")
        if new_expiry:
            print(f"📅 新的到期日期: {new_expiry}")
        res["status"] = "✅ 续期成功"
        res["extra"] = f"⏱️ 可续期时间: {format_countdown(new_countdown)}后"
        res["expiry"] = new_expiry or "（未获取到）"
    elif new_expiry and new_expiry != current_expiry:
        print(f"✅ 续期成功，到期日期已更新为: {new_expiry}")
        res["status"] = "✅ 续期成功"
        res["extra"] = "到期日期已更新"
        res["expiry"] = new_expiry
    else:
        print("⚠️ 续期结果未知，到期日期未变化，请手动检查")
        res["status"] = "⚠️ 续期可能未成功"
        res["extra"] = "请登录后台检查"
        res["expiry"] = current_expiry or "（未获取到）"
    return res


# ================= 主流程 =================
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期（多账号）")
    print("#" * 25)

    accounts = load_accounts()
    if not accounts:
        print("❌ 没有找到任何账号配置，请设置 ACCOUNTS（JSON 数组）或 EMAIL/PASSWORD")
        sys.exit(1)
    print(f"📋 共读取到 {len(accounts)} 个账号，开始依次处理...")

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"

    sb_kwargs = {"uc": True, "headless": HEADLESS, "locale_code": "en-US"}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    try:
        print(f"📍 当前出口IP: {get_current_ip(PROXY_SERVER if IS_PROXY else '')}")
    except Exception as e:
        print(f"⚠️ 获取出口 IP 失败: {e}")

    results = []
    for idx, acc in enumerate(accounts, 1):
        print(f"\n========== [{idx}/{len(accounts)}] {mask_email(acc['email'])} ==========")
        try:
            # 每个账号用独立浏览器会话，互不干扰
            with SB(**sb_kwargs) as sb:
                res = process_account(sb, acc["email"], acc["password"])
        except Exception as e:
            print(f"❌ 处理异常: {type(e).__name__}: {str(e)[:150]}")
            res = {"email": mask_email(acc["email"]), "status": "❌ 脚本异常",
                   "extra": "", "expiry": "", "error": f"{type(e).__name__}: {str(e)[:100]}"}
        results.append(res)
        if idx < len(accounts):
            time.sleep(5)

    summary = format_summary(results)
    print(f"\n🏁 全部账号处理完毕：\n{summary}")
    send_telegram_message(summary)


if __name__ == "__main__":
    main()
