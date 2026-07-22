#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, requests
import urllib.parse
from datetime import datetime
from seleniumbase import SB

# ============================================================
# 环境变量配置
#
# DISCORD_TOKEN 支持多账号：一行一个账号，格式二选一：
#   token
#   备注,token          # 备注仅用于通知里显示，逗号后面的部分才是真正的 token
# 例如（GitHub Secret 里直接填多行）：
#   主号,MTExxxx.xxxx.xxxx
#   小号,MTIyxxxx.xxxx.xxxx
#
# EMAIL 可选，同样按行对应每个账号（仅用于通知里显示，可留空）
# ============================================================
DISCORD_TOKEN_RAW = os.environ.get("DISCORD_TOKEN") or ""
EMAIL_RAW         = os.environ.get("EMAIL") or ""
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN      = os.environ.get("TG_BOT_TOKEN") or ""

IS_PROXY     = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
HEADLESS     = os.environ.get("HEADLESS", "false").lower() == "true"


# ---------------- 多账号解析 ----------------
def parse_multi_line(value: str):
    """按行分割，兼容 Windows/Unix 换行，忽略空行"""
    if not value:
        return []
    return [line.strip() for line in re.split(r"[\r\n]+", value) if line.strip()]


def parse_discord_accounts(raw: str):
    """
    解析 DISCORD_TOKEN，一行一个账号：
      token
    或
      备注,token
    """
    accounts = []
    for idx, line in enumerate(parse_multi_line(raw), start=1):
        if "," in line:
            label, token = line.split(",", 1)
            label = label.strip() or f"账号{idx}"
            token = token.strip()
        else:
            label = f"账号{idx}"
            token = line.strip()
        if token:
            accounts.append({"label": label, "token": token})
    return accounts


def mask_email(email: str) -> str:
    if not email:
        return ""
    if "@" in email:
        name, domain = email.split("@", 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    return email[:2] + "****"


# ---------------- 通知 ----------------
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


def build_account_block(idx: int, result: dict) -> str:
    lines = [f"👤 账号{idx}：{result['label']}"]
    masked = mask_email(result.get("email", ""))
    if masked:
        lines.append(f"📧 {masked}")
    lines.append(f"{result['status_emoji']} {result['status_text']}")
    if result.get("expiry"):
        lines.append(f"📅 到期时间: {result['expiry']}")
    if result.get("extra"):
        lines.append(result["extra"])
    if result.get("error"):
        lines.append(f"⚠️ 错误信息: {result['error']}")
    return "\n".join(lines)


def build_summary_message(results: list) -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    total = len(results)
    success = sum(1 for r in results if r["status_emoji"] == "✅")
    failed = sum(1 for r in results if r["status_emoji"] in ("❌",))
    other = total - success - failed

    lines = [f"🇫🇮 Bot-hosting 续期通知（共 {total} 个账号）", ""]
    for idx, r in enumerate(results, start=1):
        lines.append(build_account_block(idx, r))
        lines.append("─" * 16)
    lines.append(f"📊 汇总：成功 {success} / 失败 {failed} / 其他 {other}")
    lines.append(f"⏱️ 执行时间: {now}")
    return "\n".join(lines)


# ---------------- 工具函数 ----------------
def wait_for_turnstile_pass(sb, timeout=30):
    start = time.time()
    cf_indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    while time.time() - start < timeout:
        page_lower = sb.get_page_source().lower()
        if not any(x in page_lower for x in cf_indicators):
            print("✅ Turnstile 验证已通过")
            return True
        sb.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False


def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()


def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(":")
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        return f"{m}min"
    except Exception:
        return countdown_str


def extract_expiry_date(page_source: str):
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})",
        r"[Ee]xpires\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew",
        r"(\d{2}/\d{2}/\d{4})\s*[\-–]\s*renew",
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew manually to extend for 4 days",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            date_str = match.group(1)
            if len(date_str.split("/")[-1]) == 4:
                parts = date_str.split("/")
                if len(parts[0]) == 2:
                    return f"{parts[2]}/{parts[0]}/{parts[1]}"
            return date_str
    return None


# ---------------- Discord OAuth 登录 ----------------
DISCORD_CLIENT_ID  = "884382422530158623"
OAUTH_REDIRECT_URI = "https://bot-hosting.net/login"
OAUTH_SCOPE        = "identify email guilds"
DISCORD_API        = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
STATE_RE = re.compile(r"[?&]state=([^&]+)")


def capture_discord_state(sb) -> str:
    """打开 /login/discord，从落地页 URL 里提取本次会话的 state"""
    print("🔎 获取 Discord OAuth state...")
    sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", reconnect_time=4)
    time.sleep(2)

    url = sb.get_current_url()
    if "discord.com" not in url:
        print(f"⚠️ 未跳转到 Discord 相关页面，当前 URL：{url}")
        return ""

    m = STATE_RE.search(url)
    if not m:
        print(f"❌ 未能从 URL 中解析出 state，当前 URL：{url}")
        return ""

    state = urllib.parse.unquote(m.group(1))
    print(f"✅ 已捕获 state（当前落地页：{urllib.parse.urlparse(url).path}）")
    return state


def discord_authorize(state: str, dc_token: str) -> str:
    """用指定账号的 dc_token 直接完成 Discord 侧授权，返回跳转回 bot-hosting.net 的 location"""
    query = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "scope":         OAUTH_SCOPE,
        "state":         state,
    })
    authorize_url = f"{DISCORD_API}?{query}"

    referer = (
        "https://discord.com/oauth2/authorize?" +
        urllib.parse.urlencode({
            "client_id":     DISCORD_CLIENT_ID,
            "redirect_uri":  OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope":         OAUTH_SCOPE,
            "state":         state,
        })
    )

    headers = {
        "accept":           "*/*",
        "authorization":    dc_token,
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       DISCORD_UA,
        "x-discord-locale": "zh-CN",
    }

    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    })

    proxies = None
    if IS_PROXY:
        proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER}

    try:
        resp = requests.post(authorize_url, headers=headers, data=body, proxies=proxies, timeout=20)
        if resp.status_code != 200:
            print(f"❌ Discord OAuth2 授权失败: HTTP {resp.status_code} - {resp.text[:300]}")
            return ""
        resp_data = resp.json()
    except Exception as e:
        print(f"❌ Discord OAuth2 授权异常: {e}")
        return ""

    location = resp_data.get("location", "")
    if not location:
        print(f"❌ 授权响应中未找到 location 字段: {resp_data}")
        return ""

    masked = re.sub(r"code=[^&]+", "code=***", location)
    print(f"✅ 拿到回调 URL: {masked}")
    return location


def do_discord_login(sb, dc_token: str) -> bool:
    """通过指定账号的 Discord Token 走完整 OAuth 流程登录 bot-hosting.net"""
    print("\n🔑 通过 Discord Token 登录...")

    state = capture_discord_state(sb)
    if not state:
        sb.save_screenshot("login_no_state.png")
        return False

    location = discord_authorize(state, dc_token)
    if not location:
        return False

    print("↩️ 携带授权码打开回调链接...")
    sb.uc_open_with_reconnect(location, reconnect_time=4)
    time.sleep(3)

    url = sb.get_current_url()

    if "/error/banned" in url:
        print("🚫 账号已被封禁")
        sb.save_screenshot("login_banned.png")
        return False

    if "bot-hosting.net" not in url:
        print(f"❌ 回调后未跳转至 bot-hosting.net，当前 URL：{url}")
        sb.save_screenshot("login_no_redirect.png")
        return False

    try:
        body_text = sb.get_text("body")
    except Exception:
        body_text = ""
    if "fraud" in body_text.lower():
        print("🚫 触发风控（fraud attempt），可能是 IP 被拦截")
        sb.save_screenshot("login_fraud.png")
        return False

    for _ in range(30):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if "bot-hosting.net" in url and path != "/login" and not path.startswith("/login/discord"):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            return True
        time.sleep(0.5)

    print(f"❌ 登录超时或未跳转成功，最终停留在：{url}")
    try:
        body_text = sb.get_text("body")
        print(f"📄 页面正文片段：{body_text[:200].strip()!r}")
    except Exception:
        pass
    sb.save_screenshot("login_timeout.png")
    return False


# ---------------- 单账号处理 ----------------
def process_account(idx: int, total: int, account: dict, email: str) -> dict:
    label = account["label"]
    dc_token = account["token"]
    result = {
        "label": label,
        "email": email,
        "status_emoji": "❌",
        "status_text": "登录失败",
        "expiry": None,
        "extra": "",
        "error": "",
    }

    print(f"\n{'=' * 40}\n🔑 [{idx}/{total}] 处理账号: {label}\n{'=' * 40}")

    sb_kwargs = {"uc": True, "headless": HEADLESS}
    if IS_PROXY:
        sb_kwargs["proxy"] = PROXY_SERVER

    try:
        with SB(**sb_kwargs) as sb:
            login_ok = False
            if do_discord_login(sb, dc_token):
                print("🌐 访问 https://bot-hosting.net/a/billings ...")
                sb.open("https://bot-hosting.net/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")
                if "a/billings" in current_url:
                    login_ok = True
                    print(f"✅ [{label}] Discord OAuth 登录成功，当前已到达账单页")
                else:
                    print(f"❌ [{label}] 登录后未到达账单页，当前URL: {current_url}")
            else:
                print(f"❌ [{label}] Discord OAuth 登录失败")

            if not login_ok:
                result["error"] = "Discord OAuth 登录失败"
                return result

            sb.sleep(2)
            page_source = sb.get_page_source()
            current_expiry = extract_expiry_date(page_source)
            result["expiry"] = current_expiry
            if current_expiry:
                print(f"📅 [{label}] 当前到期日期: {current_expiry}")
            else:
                print(f"⚠️ [{label}] 未能提取当前到期日期")

            outer_renew_selector = None
            countdown_text = None
            possible_selectors = [
                'button:contains("Renew")',
                'button:contains("Renew free plan")',
                'a:contains("Renew")',
                '[class*="renew"]',
                '[class*="Renew"]',
            ]
            for selector in possible_selectors:
                try:
                    if sb.is_element_visible(selector):
                        button_text = sb.get_text(selector)
                        if "Renew in" in button_text:
                            m = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", button_text)
                            if m:
                                countdown_text = m.group(1)
                            break
                        elif "Renew" in button_text and "in" not in button_text.lower():
                            outer_renew_selector = selector
                            print(f"✅ [{label}] 续期按钮可用: '{button_text}'")
                            break
                except Exception:
                    pass

            if outer_renew_selector:
                print(f"🔄 [{label}] 点击外部续期按钮，等待验证窗口...")
                try:
                    sb.sleep(2)
                    sb.click(outer_renew_selector)
                    sb.sleep(15)
                except Exception as e:
                    result["status_text"] = "续期失败"
                    result["error"] = f"点击外部续期按钮出错: {e}"
                    return result

                print(f"🔒 [{label}] 检测弹窗中的 Turnstile 验证...")
                turnstile_passed = False
                for attempt in range(1, 4):
                    try:
                        sb.uc_gui_click_captcha()
                        time.sleep(12)
                    except Exception as e:
                        print(f"⚠️ [{label}] 点击 Turnstile 出错: {e}")
                    if wait_for_turnstile_pass(sb, timeout=20):
                        turnstile_passed = True
                        break
                    print(f"⏳ [{label}] 第 {attempt} 次未通过，重试点击...")

                if not turnstile_passed:
                    result["status_text"] = "续期失败"
                    result["error"] = "Turnstile 验证未通过"
                    return result

                print(f"⏳ [{label}] 等待续期按钮可用并点击...")
                time.sleep(5)
                try:
                    sb.click('button:contains("Renew for 4 days")', timeout=8)
                    print(f"✅ [{label}] 已点击续期按钮")
                except Exception as e:
                    print(f"[{label}] 续期按钮点击失败: {e}")

                print(f"⏳ [{label}] 等待新的过期时间...")
                sb.sleep(6)

                new_page_text = sb.get_page_source()
                new_expiry = extract_expiry_date(new_page_text)
                new_match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", new_page_text)
                if new_match:
                    new_countdown = new_match.group(1)
                    print(f"✅ [{label}] 续期成功！新的倒计时: {new_countdown}")
                    result["status_emoji"] = "✅"
                    result["status_text"] = "续期成功"
                    result["extra"] = f"⏱️ 可续期时间: {format_countdown(new_countdown)}后"
                    result["expiry"] = new_expiry or current_expiry
                elif new_expiry and new_expiry != current_expiry:
                    print(f"✅ [{label}] 续期成功，到期日期已更新为: {new_expiry}")
                    result["status_emoji"] = "✅"
                    result["status_text"] = "续期成功"
                    result["extra"] = "到期日期已更新"
                    result["expiry"] = new_expiry
                else:
                    print(f"⚠️ [{label}] 续期结果未知，到期日期未变化，请手动检查")
                    result["status_emoji"] = "⚠️"
                    result["status_text"] = "续期可能未成功"
                    result["extra"] = "请登录后台检查"
            else:
                if countdown_text:
                    friendly = format_countdown(countdown_text)
                    print(f"⏳ [{label}] 未到续期时间，倒计时: {countdown_text} ({friendly})")
                    result["status_emoji"] = "⏳"
                    result["status_text"] = "未到续期时间"
                    result["extra"] = f"⏱️ 可续期时间: {friendly}后"
                else:
                    print(f"ℹ️ [{label}] 未找到续期按钮或倒计时，状态未知")
                    result["status_emoji"] = "ℹ️"
                    result["status_text"] = "无需续期"
                    result["extra"] = "当前状态未知，请手动检查"

            return result
    except Exception as e:
        result["error"] = f"账号处理异常: {e}"
        return result


# ---------------- 主流程 ----------------
def main():
    print("#" * 25)
    print("   Bot-hosting 多账号自动续期")
    print("#" * 25)

    accounts = parse_discord_accounts(DISCORD_TOKEN_RAW)
    emails = parse_multi_line(EMAIL_RAW)

    if not accounts:
        print("ℹ️ 未配置 DISCORD_TOKEN，脚本终止。")
        sys.exit(1)

    total = len(accounts)
    print(f"📋 共检测到 {total} 个账号")

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
    else:
        print("🍭 未使用代理，直连访问")

    try:
        ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
        print(f"📍 当前出口IP: {ip}")
    except Exception as e:
        print(f"⚠️ 获取出口 IP 失败: {e}")

    results = []
    for idx, account in enumerate(accounts, start=1):
        email = emails[idx - 1] if idx - 1 < len(emails) else (emails[-1] if emails else "")
        result = process_account(idx, total, account, email)
        results.append(result)

    message = build_summary_message(results)
    print("\n" + message)
    send_telegram_message(message)

    print("\n🏁 全部账号处理完毕")


if __name__ == "__main__":
    main()
