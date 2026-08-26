import json
import os
import smtplib
import ssl
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText

import requests

BASE_URL = "https://api005.dnshe.com/index.php?m=domain_hub"

# 续期阈值：剩余天数小于该值才续期（改数字即可，不走 Actions 变量）
RENEW_THRESHOLD_DAYS = 180


def load_smtp_config():
    """
    读取 Secret SMTP_CONFIG（单个 JSON 对象），例如：

    {
      "host": "smtp.qq.com",
      "port": 465,
      "user": "you@qq.com",
      "pass": "授权码",
      "from": "you@qq.com",
      "to": "recv@example.com",
      "ssl": true
    }

    字段：host/user/pass/to 必填；port 默认 465；from 默认 = user；
    ssl 默认 true（465 SSL）；587 请设 "ssl": false（走 STARTTLS）。
    pass 也可用 password。
    to 多人用逗号分隔：a@x.com,b@y.com
    """
    raw = (os.environ.get("SMTP_CONFIG") or "").strip()
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"SMTP_CONFIG 不是合法 JSON: {e}")
        return None
    if not isinstance(cfg, dict):
        print("SMTP_CONFIG 必须是 JSON 对象")
        return None

    host = (cfg.get("host") or "").strip()
    user = (cfg.get("user") or "").strip()
    password = (cfg.get("pass") or cfg.get("password") or "").strip()
    mail_to = (cfg.get("to") or "").strip()
    if not (host and user and password and mail_to):
        print("SMTP_CONFIG 缺 host/user/pass/to，跳过邮件")
        return None

    port_raw = cfg.get("port", 465)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        print(f"SMTP_CONFIG.port 非法: {port_raw!r}，用 465")
        port = 465

    ssl_val = cfg.get("ssl", True)
    if isinstance(ssl_val, str):
        use_ssl = ssl_val.strip().lower() in ("1", "true", "yes", "on")
    else:
        use_ssl = bool(ssl_val)

    return {
        "host": host,
        "port": port,
        "user": user,
        "pass": password,
        "from": (cfg.get("from") or user).strip(),
        "to": mail_to,
        "ssl": use_ssl,
    }


def send_smtp(content, title):
    """SMTP 发信。失败只打日志，不抛。"""
    cfg = load_smtp_config()
    if not cfg:
        return

    body = f"{title}\n\n{content}"
    recipients = [x.strip() for x in cfg["to"].split(",") if x.strip()]
    if not recipients:
        print("SMTP_CONFIG.to 解析后为空，跳过邮件")
        return

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = str(Header(title, "utf-8"))
        msg["From"] = cfg["from"]
        msg["To"] = ", ".join(recipients)

        host, port = cfg["host"], cfg["port"]
        if cfg["ssl"] or port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as s:
                s.login(cfg["user"], cfg["pass"])
                s.sendmail(cfg["from"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.login(cfg["user"], cfg["pass"])
                s.sendmail(cfg["from"], recipients, msg.as_string())
        print("SMTP 推送成功 ->", ", ".join(recipients))
    except Exception as e:
        print("SMTP 推送失败:", str(e))


def send_telegram(content, title):
    """Telegram Bot 推送。失败只打日志，不抛。"""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return

    text = f"{title}\n\n{content}"
    # Telegram 单条上限 4096，超长拆多条
    max_len = 4000
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            chunk = f"({i}/{len(chunks)})\n{chunk}"
        data = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=data, timeout=15)
            print(f"Telegram 推送 [{i}/{len(chunks)}]:", resp.text)
            if resp.status_code >= 400:
                print("Telegram 推送失败 HTTP", resp.status_code)
        except Exception as e:
            print("推送失败:", str(e))


def send_notification(content, title="DNSHE 域名自动续期报告——GitHub"):
    """
    通知通道（可并存，互不影响）：
    - SMTP：Secret SMTP_CONFIG（JSON）
    - Telegram：TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    都未配置则只打日志跳过。
    """
    has_smtp = bool((os.environ.get("SMTP_CONFIG") or "").strip())
    has_tg = bool(
        (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        and (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    )
    if not has_smtp and not has_tg:
        print("未配置 SMTP_CONFIG 或 TELEGRAM_*，跳过推送")
        return

    if has_smtp:
        send_smtp(content, title)
    if has_tg:
        send_telegram(content, title)


def load_accounts():
    """
    加载账号列表，支持两种方式（优先 JSON 多账号）：

    1) Secret DNSHE_ACCOUNTS（推荐，多账号顺序执行）:
       [
         {"name": "号1", "api_key": "xxx", "api_secret": "yyy"},
         {"name": "号2", "api_key": "aaa", "api_secret": "bbb"}
       ]

    2) 单账号兼容:
       DNSHE_API_KEY + DNSHE_API_SECRET
       可选 DNSHE_ACCOUNT_NAME（默认 default）
    """
    raw = os.environ.get("DNSHE_ACCOUNTS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"DNSHE_ACCOUNTS 不是合法 JSON: {e}")

        if not isinstance(data, list) or not data:
            raise SystemExit("DNSHE_ACCOUNTS 必须是非空 JSON 数组")

        accounts = []
        for i, item in enumerate(data, 1):
            if not isinstance(item, dict):
                raise SystemExit(f"DNSHE_ACCOUNTS[{i}] 必须是对象")
            key = (item.get("api_key") or item.get("key") or "").strip()
            secret = (item.get("api_secret") or item.get("secret") or "").strip()
            name = (item.get("name") or item.get("label") or f"account-{i}").strip()
            if not key or not secret:
                raise SystemExit(f"账号 [{name}] 缺少 api_key / api_secret")
            accounts.append({"name": name, "api_key": key, "api_secret": secret})
        return accounts

    key = (os.environ.get("DNSHE_API_KEY") or "").strip()
    secret = (os.environ.get("DNSHE_API_SECRET") or "").strip()
    if key and secret:
        name = (os.environ.get("DNSHE_ACCOUNT_NAME") or "default").strip()
        return [{"name": name, "api_key": key, "api_secret": secret}]

    raise SystemExit(
        "未配置账号：请设置 DNSHE_ACCOUNTS（多账号 JSON），"
        "或 DNSHE_API_KEY + DNSHE_API_SECRET（单账号）"
    )


def process_account(account):
    """
    处理单个账号：列域名 → 按阈值续期。
    返回 (detail_log, summary_line, has_error)
    - detail_log: 完整日志（打到控制台 / Actions）
    - summary_line: 精简汇总（发通知）
    """
    name = account["name"]
    headers = {
        "X-API-Key": account["api_key"],
        "X-API-Secret": account["api_secret"],
        "Content-Type": "application/json",
    }

    lines = [f"## 账号: {name}", ""]
    has_error = False
    total = 0
    skipped = 0
    success = 0
    failed = 0
    failed_domains = []  # [(domain, reason), ...]

    list_url = (
        f"{BASE_URL}&endpoint=subdomains&action=list"
        f"&fields=id,subdomain,rootdomain,full_domain,status,expires_at,never_expires"
    )
    try:
        resp = requests.get(list_url, headers=headers, timeout=30)
        body = resp.json()
        subdomains = body.get("subdomains", [])
        if resp.status_code >= 400:
            has_error = True
            lines.append(f"❌ 获取域名列表 HTTP {resp.status_code}: {body}")
            summary = f"【{name}】获取域名列表失败 (HTTP {resp.status_code})"
            return "\n".join(lines), summary, has_error
    except Exception as e:
        has_error = True
        lines.append(f"❌ 获取域名列表失败: {e}")
        summary = f"【{name}】获取域名列表失败: {e}"
        return "\n".join(lines), summary, has_error

    total = len(subdomains)
    if not subdomains:
        lines.append("（该账号下无子域名）")
        summary = f"【{name}】共 0 个域名"
        return "\n".join(lines), summary, has_error

    today = datetime.now()
    renewal_results = []
    expiry_info = []

    for domain in subdomains:
        domain_id = domain["id"]
        full_domain = domain["full_domain"]
        expires_at_str = domain.get("expires_at")
        never_expires = domain.get("never_expires", 0)

        if never_expires:
            skipped += 1
            expiry_info.append(f"{full_domain}: 到期时间 永久有效")
            renewal_results.append(f"⏭️ {full_domain}: 已设置为永不过期，跳过续期")
            continue

        expires_at = None
        days_remaining = None
        if expires_at_str:
            try:
                expires_at = datetime.strptime(expires_at_str, "%Y-%m-%d %H:%M:%S")
                days_remaining = (expires_at - today).days
            except ValueError:
                days_remaining = None

        if days_remaining is not None:
            expiry_info.append(
                f"{full_domain}: 到期时间 {expires_at_str} (剩余 {days_remaining}天)"
            )
        else:
            expiry_info.append(f"{full_domain}: 到期时间 未知")

        if days_remaining is not None and days_remaining >= RENEW_THRESHOLD_DAYS:
            skipped += 1
            renewal_results.append(
                f"⏭️ {full_domain}: 剩余 {days_remaining}天 >= {RENEW_THRESHOLD_DAYS}天，跳过续期"
            )
            continue

        renew_url = f"{BASE_URL}&endpoint=subdomains&action=renew"
        payload = {"subdomain_id": domain_id}

        try:
            r_resp = requests.post(
                renew_url, headers=headers, json=payload, timeout=30
            ).json()
            if r_resp.get("success"):
                success += 1
                new_expiry = r_resp.get("new_expires_at", "未知")
                charged = r_resp.get("charged_amount", 0)
                renewal_results.append(
                    f"✅ {full_domain}: 续期成功 (新到期: {new_expiry}, 消耗: {charged}积分)"
                )
            else:
                failed += 1
                has_error = True
                msg = r_resp.get("message", "未知错误")
                failed_domains.append((full_domain, msg))
                renewal_results.append(f"❌ {full_domain}: 续期失败 ({msg})")
        except Exception as e:
            failed += 1
            has_error = True
            failed_domains.append((full_domain, str(e)))
            renewal_results.append(f"❌ {full_domain}: 请求异常 ({e})")

    lines.append("=== 本次续期结果 ===")
    if renewal_results:
        lines.extend(renewal_results)
    else:
        lines.append(
            f"（所有域名剩余天数 >= {RENEW_THRESHOLD_DAYS}天，本次无需续期）"
        )

    lines.append("")
    lines.append("=== 所有域名到期时间 ===")
    lines.extend(expiry_info)

    # 精简汇总：只给通知；失败时附域名列表
    summary = (
        f"【{name}】共 {total} 个域名"
        f"，无需续期 {skipped}"
        f"，续期成功 {success}"
        f"，续期失败 {failed}"
    )
    if failed_domains:
        fail_lines = [f"  · {d} ({reason})" for d, reason in failed_domains]
        summary += "\n失败域名:\n" + "\n".join(fail_lines)
    return "\n".join(lines), summary, has_error


def main():
    accounts = load_accounts()
    print(f"共 {len(accounts)} 个账号，将顺序执行")
    print(f"续期阈值: 剩余 < {RENEW_THRESHOLD_DAYS} 天")

    summaries = []
    any_error = False

    for i, account in enumerate(accounts, 1):
        print(f"\n-------- [{i}/{len(accounts)}] {account['name']} --------")
        detail, summary, err = process_account(account)
        print(detail)
        print(f"[汇总] {summary}")
        summaries.append(summary)
        if err:
            any_error = True

    status = "有失败" if any_error else "完成"
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"GitHub DNSHE 续期报告 ({run_time}) · {status}"
    send_notification("\n".join(summaries), title=title)

    if any_error:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
