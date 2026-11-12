import json
import os
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from github_updater import update_token_file

DATA_PATH = "user_repos.json"

_schedulers  = {}
_stop_flags  = {}

INITIAL_DELAY_SECONDS = 30 * 60

GEN_WORKERS = 15
GEN_TIMEOUT = 15


def load_data() -> dict:
    if not os.path.exists(DATA_PATH):
        return {}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_data(data: dict):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def find_repo(repo_id: str):
    data = load_data()
    for uid, udata in data.items():
        for repo in udata.get("repos", []):
            if repo.get("id") == repo_id:
                return uid, udata, repo
    return None, None, None


# ============================================================
# JWT Generation
# ============================================================
def _try_generate_one(api: dict, uid: str, pw: str, timeout: int = GEN_TIMEOUT):
    try:
        base = api.get("base", "").rstrip("/")
        style = api.get("style", "guest")
        if not base:
            return None

        params = (
            {"guest_uid": uid, "guest_password": pw}
            if style == "guest"
            else {"uid": uid, "password": pw}
        )

        r = requests.get(
            f"{base}/api/public/token", params=params, timeout=timeout
        )
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        tok = data.get("token")
        if tok:
            return tok
        inner = data.get("data")
        if isinstance(inner, dict) and inner.get("token"):
            return inner["token"]
        return None
    except Exception:
        return None


def gen_jwt_batch(accounts: list, api_bases: list) -> list:
    if not api_bases or not accounts:
        return []

    def _one(acc):
        uid = str(acc.get("uid", "")).strip()
        pw  = str(acc.get("password", "")).strip()
        if not uid or not pw:
            return None
        for api in api_bases:
            tok = _try_generate_one(api, uid, pw)
            if tok:
                return {"token": tok}
        return None

    out = []
    with ThreadPoolExecutor(max_workers=GEN_WORKERS) as pool:
        futures = [pool.submit(_one, acc) for acc in accounts]
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception:
                res = None
            if res:
                out.append(res)
    return out


# ============================================================
# ✅ Update Cycle — fixed with is_manual flag + progress
# ============================================================
def do_update_cycle(bot_token: str, chat_id: int, api_bases: list,
                    repo_id: str, notify_group_id: int = 0,
                    is_manual: bool = False):
    import telebot
    from html import escape as html_escape
    bot = telebot.TeleBot(bot_token, parse_mode="HTML")

    def _notify(text):
        try:
            bot.send_message(chat_id, text)
        except Exception:
            pass

    uid, udata, repo = find_repo(repo_id)
    if not repo:
        if is_manual:
            _notify("❌ <b>REPO NOT FOUND</b>")
        return

    if not repo.get("active", True):
        if is_manual:
            _notify("⚠️ <b>REPO INACTIVE</b> — আগে ON করুন তারপর আবার চেষ্টা করুন।")
        return

    accounts = repo.get("accounts", [])
    if not accounts:
        if is_manual:
            _notify("⚠️ <b>NO ACCOUNTS</b> — এই repo তে কোনো account নেই।")
        return

    # ✅ Manual: progress — token generation
    if is_manual:
        _notify(
            f"🔑 <b>GENERATING JWT TOKENS...</b>\n\n"
            f"👥 Accounts: <b>{len(accounts)}</b>\n"
            f"⏳ অপেক্ষা করুন..."
        )

    tokens = gen_jwt_batch(accounts, api_bases)

    # ✅ Manual: progress — generation done
    if is_manual:
        _notify(
            f"🔑 Generated: <b>{len(tokens)}/{len(accounts)}</b> tokens\n"
            f"📤 GitHub এ upload করা হচ্ছে..."
        )

    if not tokens:
        msg_fail = (
            f"⚠️ <b>UPDATE FAILED</b>\n\n"
            f"🎯 Button: <b>{html_escape(repo.get('button_name', '-'))}</b>\n"
            f"🗂 Repo: <code>{html_escape(repo.get('repo', '-'))}</code>\n"
            f"👤 Owner: <code>{uid}</code>\n"
            f"❌ NO TOKENS GENERATED (all APIs failed)"
        )
        _notify(msg_fail)
        if notify_group_id:
            try:
                bot.send_message(notify_group_id, msg_fail)
            except Exception:
                pass
        return

    res = update_token_file(
        repo["repo"],
        repo["file_path"],
        tokens,
        udata.get("github_token", ""),
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if res.get("ok"):
        msg = (
            f"✅ <b>AUTO UPDATE SUCCESS</b>\n\n"
            f"🎯 Button: <b>{html_escape(repo.get('button_name', '-'))}</b>\n"
            f"🗂 Repo: <code>{html_escape(repo['repo'])}</code>\n"
            f"📄 File: <code>{html_escape(repo['file_path'])}</code>\n"
            f"👤 Owner: <code>{uid}</code>\n"
            f"🔢 Tokens: <b>{len(tokens)}</b>\n"
            f"🕒 Time: <b>{now}</b>"
        )
    else:
        msg = (
            f"❌ <b>AUTO UPDATE FAILED</b>\n\n"
            f"🎯 Button: <b>{html_escape(repo.get('button_name', '-'))}</b>\n"
            f"🗂 Repo: <code>{html_escape(repo['repo'])}</code>\n"
            f"👤 Owner: <code>{uid}</code>\n"
            f"🕒 Time: <b>{now}</b>\n"
            f"⚠️ <code>{html_escape(str(res.get('error'))[:300])}</code>"
        )

    _notify(msg)

    if notify_group_id:
        try:
            bot.send_message(notify_group_id, msg)
        except Exception:
            pass


# ============================================================
# Scheduler Control
# ============================================================
def _finish_schedule(bot_token: str, chat_id: int, repo_id: str):
    import telebot
    bot = telebot.TeleBot(bot_token, parse_mode="HTML")
    _, _, repo = find_repo(repo_id)
    name = repo.get("button_name", repo_id) if repo else repo_id
    try:
        bot.send_message(
            chat_id,
            f"🛑 <b>AUTO UPDATE FINISHED</b>\n\n"
            f"🎯 Button: <b>{name}</b>\n"
            f"⌛ Schedule সম্পন্ন হয়েছে।",
        )
    except Exception:
        pass

    data = load_data()
    for _, ud in data.items():
        for r in ud.get("repos", []):
            if r.get("id") == repo_id:
                r["active"] = False
                save_data(data)
                return


def start_scheduler(bot_token: str, chat_id: int, api_bases: list,
                    hours, end_time: float, repo_id: str,
                    notify_group_id: int = 0, initial_delay: int = None):
    """
    hours can be float (0.5 = 30min, 0.75 = 45min).
    initial_delay = None → DEFAULT (30 min)
    initial_delay = 0    → immediate first update
    """
    global _schedulers, _stop_flags

    stop_scheduler(repo_id)
    _stop_flags[repo_id] = False

    if initial_delay is None:
        initial_delay = INITIAL_DELAY_SECONDS

    # ✅ FIX: cast to int for range() — prevents float crash
    try:
        sleep_seconds = max(1, int(float(hours) * 3600))
    except (ValueError, TypeError):
        sleep_seconds = 3600

    def loop():
        try:
            delay_until = time.time() + max(0, initial_delay)
            while not _stop_flags.get(repo_id, True) and time.time() < delay_until:
                if time.time() >= end_time:
                    _finish_schedule(bot_token, chat_id, repo_id)
                    return
                time.sleep(1)

            while not _stop_flags.get(repo_id, True) and time.time() < end_time:
                try:
                    do_update_cycle(
                        bot_token, chat_id, api_bases, repo_id, notify_group_id
                    )
                except Exception:
                    pass

                # ✅ FIX: int() cast — handles float hours safely
                for _ in range(sleep_seconds):
                    if _stop_flags.get(repo_id, True) or time.time() >= end_time:
                        break
                    time.sleep(1)

            if not _stop_flags.get(repo_id, True) and time.time() >= end_time:
                _finish_schedule(bot_token, chat_id, repo_id)
        except Exception:
            pass

    thread = threading.Thread(target=loop, daemon=True)
    _schedulers[repo_id] = thread
    thread.start()


def stop_scheduler(repo_id: str):
    global _schedulers, _stop_flags
    if repo_id in _schedulers:
        _stop_flags[repo_id] = True
        if _schedulers[repo_id].is_alive():
            _schedulers[repo_id].join(timeout=1)
        del _schedulers[repo_id]