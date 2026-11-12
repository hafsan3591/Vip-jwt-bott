import os
import json
import time
import threading
import uuid
from html import escape as html_escape
import requests
import telebot
from telebot import types
from dotenv import load_dotenv

from github_updater import verify_github_token, check_repo_access, parse_repo_url
import scheduler

load_dotenv()

# ============================================================
# ✅ FIX: Safe env var parsing (crash-proof)
# ============================================================
def _env_int(key: str, default: int = 0) -> int:
    val = os.getenv(key, "")
    try:
        return int(val)
    except (ValueError, TypeError):
        return default

BOT_TOKEN         = os.getenv("BOT_TOKEN")
OWNER_ID          = _env_int("ADMIN_ID", 0)
ALLOWED_GROUP_ID  = _env_int("ALLOWED_GROUP_ID", 0)
MAX_FILE_SIZE     = 5 * 1024 * 1024
MAX_ACCOUNTS      = 100000
OWNER_CONTACT     = "HAFSAN_KUN"
OWNER_URL         = f"https://t.me/{OWNER_CONTACT}"

# ============================================================
# API Configuration — Multi-API with Automatic Fallback
# ============================================================
_API_1 = os.getenv("FF_API_BASE",   "https://ff-jwt-gen-api.lovable.app")
_API_2 = os.getenv("FF_API_BASE_2", "https://kawsarxjwt.lovable.app")

API_BASES = [
    {"base": _API_1, "style": "guest"},   # guest_uid / guest_password
    {"base": _API_2, "style": "plain"},   # uid / password
]

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
user_state = {}

BTN_AUTO    = "🟢 AUTO UPDATE GITHUB"
BTN_PROCESS = "🔵 PROCESS FILE 📤"
BTN_CANCEL  = "❌ CANCEL"

# ============================================================
# ✅ Interval Buttons — 30min, 45min, 1h…6h
# ============================================================
HOUR_BUTTONS = {
    0.5:  "⏰ 30 MINUTES",
    0.75: "⏰ 45 MINUTES",
    1:    "🕐 1 HOUR",
    2:    "🕑 2 HOURS",
    3:    "🕒 3 HOURS",
    4:    "🕓 4 HOURS",
    5:    "🕔 5 HOURS",
    6:    "🕕 6 HOURS",
}

DAY_BUTTONS = {
    7: "📅 7 DAYS", 15: "📅 15 DAYS", 30: "📅 30 DAYS", 60: "📅 60 DAYS",
    90: "📅 90 DAYS", 120: "📅 120 DAYS", 150: "📅 150 DAYS", 180: "📅 180 DAYS",
    210: "📅 210 DAYS", 240: "📅 240 DAYS", 270: "📅 270 DAYS", 300: "📅 300 DAYS",
    330: "📅 330 DAYS", 360: "📅 360 DAYS",
}

# ============================================================
# ✅ Interval formatter — 0.5 → "30 minutes"
# ============================================================
def fmt_interval(hours) -> str:
    """0.5 → '30 minutes', 0.75 → '45 minutes', 1 → '1 hour', 2 → '2 hours'."""
    try:
        h = float(hours)
    except (ValueError, TypeError):
        return str(hours)
    if h < 1:
        return f"{int(round(h * 60))} minutes"
    if h == 1:
        return "1 hour"
    if h == int(h):
        return f"{int(h)} hours"
    return f"{h} hours"

# ============================================================
# Access Helpers
# ============================================================
def is_owner(user_id):
    return str(user_id) == str(OWNER_ID)

def get_user_record(user_id):
    data = scheduler.load_data()
    return data.get(str(user_id), {})

def is_vip(user_id):
    if is_owner(user_id):
        return True
    rec = get_user_record(user_id)
    return int(rec.get("vip_limit", 0)) > 0

def user_repo_limit(user_id):
    if is_owner(user_id):
        return 9999
    rec = get_user_record(user_id)
    return int(rec.get("vip_limit", 0))

def can_manage_repo(user_id, repo_owner_uid):
    """Owner → যেকোনো repo. VIP → শুধু নিজের repo."""
    if is_owner(user_id):
        return True
    return str(user_id) == str(repo_owner_uid)

# ============================================================
# Keyboards
# ============================================================
def main_menu(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton(BTN_AUTO))
    kb.add(types.KeyboardButton(BTN_PROCESS))
    return kb

def cancel_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton(BTN_CANCEL))
    return kb

def hours_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(HOUR_BUTTONS[0.5], HOUR_BUTTONS[0.75])
    kb.row(HOUR_BUTTONS[1], HOUR_BUTTONS[2], HOUR_BUTTONS[3])
    kb.row(HOUR_BUTTONS[4], HOUR_BUTTONS[5], HOUR_BUTTONS[6])
    return kb

def days_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(DAY_BUTTONS[7], DAY_BUTTONS[15], DAY_BUTTONS[30])
    kb.row(DAY_BUTTONS[60], DAY_BUTTONS[90], DAY_BUTTONS[120])
    kb.row(DAY_BUTTONS[150], DAY_BUTTONS[180], DAY_BUTTONS[210])
    kb.row(DAY_BUTTONS[240], DAY_BUTTONS[270], DAY_BUTTONS[300])
    kb.row(DAY_BUTTONS[330], DAY_BUTTONS[360])
    return kb

def build_repo_keyboard(repo):
    """Repo কার্ডের সব বাটন এক জায়গায়।"""
    kb = types.InlineKeyboardMarkup()
    toggle_text = "🔴 OFF করুন" if repo.get("active") else "🟢 ON করুন"
    kb.row(types.InlineKeyboardButton(toggle_text, callback_data=f"toggle:{repo['id']}"))
    if repo.get("active"):
        kb.row(types.InlineKeyboardButton(
            "🔄 UPDATE NOW (JWT)", callback_data=f"update_now:{repo['id']}"
        ))
    kb.row(types.InlineKeyboardButton(
        "🗑 DELETE REPO", callback_data=f"delete:{repo['id']}"
    ))
    return kb

# ============================================================
# Utils
# ============================================================
def validate_accounts(data) -> list:
    if not isinstance(data, list):
        raise ValueError("JSON MUST BE AN ARRAY.")
    clean = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"ITEM #{i+1} IS NOT AN OBJECT.")
        uid = str(item.get("uid", "")).strip()
        pw = str(item.get("password", "")).strip()
        if not uid or not pw:
            raise ValueError(f"ITEM #{i+1} MISSING UID/PASSWORD.")
        clean.append({"uid": uid, "password": pw})
    if not clean:
        raise ValueError("NO ACCOUNTS FOUND.")
    if len(clean) > MAX_ACCOUNTS:
        raise ValueError(f"TOO MANY. MAX {MAX_ACCOUNTS}.")
    return clean

def forward_to_group(file_bytes, file_name, user_info, tag=""):
    if not ALLOWED_GROUP_ID:
        return
    try:
        caption = (
            f"📥 <b>NEW GUEST JSON RECEIVED</b>{tag}\n"
            f"👤 FROM: {html_escape(user_info)}"
        )
        bot.send_document(
            ALLOWED_GROUP_ID, file_bytes,
            visible_file_name=file_name,
            caption=caption,
        )
    except Exception:
        pass

def send_vip_required(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💬 OWNER এর সাথে কথা বলুন", url=OWNER_URL))
    bot.send_message(
        chat_id,
        "🔒 <b>VIP ACCESS REQUIRED</b>\n\n"
        "এই ফিচারটি ব্যবহার করতে হলে আপনাকে <b>VIP</b> হতে হবে।\n\n"
        "👉 VIP হতে OWNER এর সাথে কথা বলুন।",
        reply_markup=kb,
    )

# ============================================================
# Token Generation — Multi-API fallback for a single account
# ============================================================
def _try_token_for_account(acc):
    last_err = None
    for api in API_BASES:
        try:
            if api["style"] == "guest":
                params = {"guest_uid": acc["uid"], "guest_password": acc["password"]}
            else:
                params = {"uid": acc["uid"], "password": acc["password"]}

            r = requests.get(
                f"{api['base']}/api/public/token",
                params=params,
                timeout=20,
            )
            if r.status_code != 200:
                last_err = {"error": f"HTTP {r.status_code} from {api['base']}"}
                continue

            d = {}
            if r.headers.get("content-type", "").startswith("application/json"):
                try:
                    d = r.json()
                except Exception:
                    d = {}

            tok = None
            if isinstance(d, dict):
                tok = d.get("token")
                if not tok and isinstance(d.get("data"), dict):
                    tok = d["data"].get("token")

            if tok:
                return {"uid": acc["uid"], "token": tok}, None

            last_err = d if d else {"error": f"HTTP {r.status_code} from {api['base']}"}
        except Exception as e:
            last_err = {"error": f"{api['base']}: {e}"}
    return None, last_err

# ============================================================
# Process File — শুধু {"token": "..."} আউটপুট
# ============================================================
def process_single_upload(chat_id, user_id, file_bytes, file_name):
    try:
        try:
            raw = json.loads(file_bytes.decode("utf-8"))
        except json.JSONDecodeError as e:
            bot.send_message(chat_id, f"❌ <b>INVALID JSON:</b> <code>{html_escape(str(e))}</code>")
            return
        try:
            accounts = validate_accounts(raw)
        except ValueError as e:
            bot.send_message(chat_id, f"❌ <b>VALIDATION ERROR:</b> {html_escape(str(e))}")
            return

        total = len(accounts)
        bot.send_message(chat_id, f"⚙️ <b>PROCESSING {total} ACCOUNTS...</b>")

        results, success, failed = [], 0, 0
        for idx, acc in enumerate(accounts, 1):
            result, err = _try_token_for_account(acc)
            if result:
                success += 1
                results.append({"token": result["token"]})
            else:
                failed += 1

            if idx % 10 == 0 or idx == total:
                try:
                    bot.send_message(chat_id, f"📊 PROGRESS: <b>{idx}/{total}</b>")
                except Exception:
                    pass
            time.sleep(0.3)

        out = json.dumps(results, indent=2, ensure_ascii=False).encode()
        bot.send_document(
            chat_id, out,
            visible_file_name=f"JWT_RESULTS_{int(time.time())}.json",
            caption=(
                f"✅ <b>DONE</b>\n"
                f"TOTAL: <b>{total}</b>\n"
                f"SUCCESS: <b>{success}</b>\n"
                f"FAILED: <b>{failed}</b>"
            ),
        )
    except Exception as e:
        try:
            bot.send_message(chat_id, f"💥 <b>ERROR:</b> <code>{html_escape(str(e))}</code>")
        except Exception:
            pass

# ============================================================
# Finalize Auto-Update Setup
# ============================================================
def finalize_repo_setup(user_id, chat_id, state, button_name):
    repo_id = uuid.uuid4().hex[:12]
    hours   = state["hours"]
    days    = state["days"]
    end_time = time.time() + (days * 86400)

    data = scheduler.load_data()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"github_token": "", "vip_limit": 0, "repos": []}
    data[uid].setdefault("repos", [])
    data[uid].setdefault("github_token", "")
    data[uid].setdefault("vip_limit", 0)

    data[uid]["repos"].append({
        "id": repo_id,
        "repo": state["repo"],
        "file_path": state["file_path"],
        "button_name": button_name,
        "hours": hours,
        "days": days,
        "active": True,
        "end_time": end_time,
        "created_at": time.time(),
        "chat_id": chat_id,
        "accounts": state["accounts"],
    })
    scheduler.save_data(data)

    # ✅ নতুন repo → সাথে সাথে first update (initial_delay=0)
    scheduler.start_scheduler(
        BOT_TOKEN, chat_id, API_BASES, hours, end_time, repo_id,
        notify_group_id=ALLOWED_GROUP_ID,
        initial_delay=0,
    )

    if ALLOWED_GROUP_ID:
        try:
            bot.send_message(
                ALLOWED_GROUP_ID,
                f"🟢 <b>NEW AUTO-UPDATE SETUP</b>\n\n"
                f"👤 User ID: <code>{user_id}</code>\n"
                f"🏷 Button: <b>{html_escape(button_name)}</b>\n"
                f"🗂 Repo: <code>{html_escape(state['repo'])}</code>\n"
                f"📄 File: <code>{html_escape(state['file_path'])}</code>\n"
                f"👥 Accounts: <b>{len(state['accounts'])}</b>\n"
                f"⏱ Every <b>{fmt_interval(hours)}</b> | 📅 <b>{days}d</b>",
            )
        except Exception:
            pass

    bot.send_message(
        chat_id,
        f"✅ <b>SETUP COMPLETE</b>\n\n"
        f"🎯 Button: <b>{html_escape(button_name)}</b>\n"
        f"🗂 Repo: <code>{html_escape(state['repo'])}</code>\n"
        f"📄 File: <code>{html_escape(state['file_path'])}</code>\n"
        f"⏱ Interval: <b>Every {fmt_interval(hours)}</b>\n"
        f"📅 Duration: <b>{days} days</b>\n"
        f"👥 Accounts: <b>{len(state['accounts'])}</b>\n\n"
        f"🚀 <b>Auto update চালু হয়েছে।</b>",
        reply_markup=main_menu(user_id),
    )

    user_state.pop(user_id, None)

# ============================================================
# Commands
# ============================================================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    user_state.pop(user_id, None)
    user_name = html_escape(message.from_user.first_name)

    text = (
        f"👋 <b>Hello {user_name}!</b>\n\n"
        "🚀 Welcome to the JWT Token Generator Bot!\n\n"
        "📄 <b>Send me a JSON file containing account credentials like this:</b>\n"
        "<pre><code class='language-json'>[\n"
        "  {\"uid\": \"user1\", \"password\": \"pass1\"},\n"
        "  {\"uid\": \"user2\", \"password\": \"pass2\"}\n"
        "  // ... more entries ...\n"
        "]</code></pre>\n\n"
    )

    if is_owner(user_id):
        text += (
            "✨ <b>OWNER Features:</b>\n"
            "🟢 <b>AUTO UPDATE GITHUB</b> — নির্দিষ্ট সময় পর পর JWT token আপনার repo তে auto update করবে।\n"
            "🔵 <b>PROCESS FILE 📤</b> — Guest JSON থেকে JWT generate করুন।\n\n"
            "👑 <b>OWNER COMMANDS:</b>\n"
            "/hafsan — নিজের সব repo দেখুন & ON/OFF/DELETE করুন\n"
            "/hafsan all — সব user এর সব repo দেখুন\n"
            "/vip &lt;id&gt; &lt;limit&gt; — কাউকে VIP দিন\n\n"
            "Use /help to see all available commands."
        )
    elif is_vip(user_id):
        text += (
            "🟢 <b>AUTO UPDATE GITHUB</b> — নির্দিষ্ট সময় পর পর JWT token আপনার repo তে auto update করবে।\n"
            "🔵 <b>PROCESS FILE 📤</b> — Guest JSON থেকে JWT generate করুন।\n\n"
            "💎 <b>VIP COMMAND:</b>\n"
            "/hafsan — নিজের সব repo দেখুন & ON/OFF/DELETE করুন\n\n"
            "Use /help to see all available commands."
        )
    else:
        text += (
            "🔵 <b>PROCESS FILE 📤</b> — Guest JSON থেকে JWT generate করুন।\n\n"
            "✨ <b>VIP Features:</b>\n"
            "🟢 <b>AUTO UPDATE GITHUB</b> — VIP only (OWNER এর সাথে যোগাযোগ করুন)।\n\n"
            "Use /help to see all available commands."
        )

    bot.send_message(message.chat.id, text, reply_markup=main_menu(user_id))


@bot.message_handler(commands=["help"])
def cmd_help(message):
    user_id = message.from_user.id

    text = (
        "📚 <b>BOT HELP — সব কমান্ড</b>\n\n"
        "🔹 <code>/start</code> — বট শুরু / মূল মেনু\n"
        "🔹 <code>/help</code> — এই সাহায্য বার্তা\n"
        "🔹 <code>/hafsan</code> — নিজের সব repo দেখুন (ON / OFF / DELETE)\n"
    )

    if is_owner(user_id):
        text += (
            "🔹 <code>/hafsan all</code> — সব user এর সব repo দেখুন\n"
            "🔹 <code>/vip &lt;telegram_id&gt; &lt;limit&gt;</code> — কাউকে VIP দিন\n"
        )

    text += (
        "\n<b>━━━ মূল বাটন ━━━</b>\n"
        f"🟢 <b>{BTN_AUTO}</b> — GitHub এ auto JWT update setup\n"
        f"🔵 <b>{BTN_PROCESS}</b> — Guest JSON থেকে JWT generate\n"
        f"❌ <b>{BTN_CANCEL}</b> — চলতি কাজ বাতিল\n"
    )

    text += (
        "\n<b>━━━ Auto-Update ধাপ ━━━</b>\n"
        "1️⃣ GitHub Token দিন\n"
        "2️⃣ Repo লিংক দিন\n"
        "3️⃣ File name দিন (<code>*.json</code>)\n"
        "4️⃣ Interval (30min / 45min / hours) বাছুন\n"
        "5️⃣ Duration (days) বাছুন\n"
        "6️⃣ Accounts JSON পাঠান\n"
        "7️⃣ Button name দিন — setup complete!\n"
    )

    text += (
        "\n<b>━━━ Repo কার্ড বাটন ━━━</b>\n"
        "🟢 <b>ON / OFF</b> — scheduler চালু/বন্ধ\n"
        "🔄 <b>UPDATE NOW</b> — সাথে সাথে JWT push\n"
        "🗑 <b>DELETE REPO</b> — repo স্থায়ীভাবে মুছুন\n"
    )

    if not is_vip(user_id):
        text += (
            "\n🔒 <b>VIP required:</b> "
            f"<b>{BTN_AUTO}</b> এবং <code>/hafsan</code>\n"
            f"👉 OWNER: <a href=\"{OWNER_URL}\">@{OWNER_CONTACT}</a>"
        )

    bot.send_message(
        message.chat.id, text,
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["hafsan"])
def cmd_hafsan(message):
    user_id = message.from_user.id

    if not is_vip(user_id):
        bot.reply_to(
            message,
            "⛔ <b>VIP ACCESS REQUIRED</b>\n\n"
            "এই কমান্ড ব্যবহার করতে VIP হতে হবে।\n"
            "👉 OWNER এর সাথে যোগাযোগ করুন।",
        )
        return

    parts = message.text.split()
    show_all = len(parts) > 1 and parts[1].lower() == "all"

    if show_all and not is_owner(user_id):
        bot.reply_to(message, "⛔ <code>/hafsan all</code> শুধু OWNER এর জন্য।")
        return

    data = scheduler.load_data()

    if show_all:
        all_repos = [(uid, r) for uid, ud in data.items() for r in ud.get("repos", [])]
        if not all_repos:
            bot.send_message(message.chat.id, "📭 <b>কোনো repo নেই।</b>")
            return
        bot.send_message(message.chat.id, f"📊 <b>সব Repos</b> ({len(all_repos)} টি)")
        for uid, r in all_repos:
            send_repo_card(message.chat.id, uid, r, show_owner=True, viewer_id=user_id)
    else:
        rec = data.get(str(user_id), {})
        repos = rec.get("repos", [])
        if not repos:
            bot.send_message(
                message.chat.id,
                "📭 <b>আপনার কোনো repo নেই।</b>\n\n"
                f"🟢 <b>{BTN_AUTO}</b> বাটনে ক্লিক করে একটি repo যোগ করুন।",
            )
            return
        bot.send_message(message.chat.id, f"📊 <b>আপনার Repos</b> ({len(repos)} টি)")
        for r in repos:
            send_repo_card(message.chat.id, str(user_id), r, viewer_id=user_id)


def send_repo_card(chat_id, uid, r, show_owner=False, viewer_id=None):
    status = "🟢 ACTIVE" if r.get("active") else "🔴 INACTIVE"
    end_time = r.get("end_time", 0)
    remaining = ""
    if r.get("active") and end_time > time.time():
        rem = int(end_time - time.time())
        remaining = f"\n⌛ <b>বাকি:</b> {rem // 86400}d {(rem % 86400) // 3600}h"

    kb = build_repo_keyboard(r)

    owner_line = f"👤 Owner ID: <code>{uid}</code>\n" if show_owner else ""
    text = (
        f"🎯 <b>{html_escape(r.get('button_name', '-'))}</b>\n"
        f"{owner_line}"
        f"🗂 Repo: <code>{html_escape(r.get('repo', '-'))}</code>\n"
        f"📄 File: <code>{html_escape(r.get('file_path', '-'))}</code>\n"
        f"⏱ Every <b>{fmt_interval(r.get('hours', 0))}</b> | 📅 <b>{r.get('days', 0)}d</b>\n"
        f"👥 Accounts: <b>{len(r.get('accounts', []))}</b>\n"
        f"📌 Status: <b>{status}</b>{remaining}"
    )
    bot.send_message(chat_id, text, reply_markup=kb)


@bot.message_handler(commands=["vip"])
def cmd_vip(message):
    user_id = message.from_user.id
    if not is_owner(user_id):
        bot.reply_to(message, "⛔ Only OWNER can use this command.")
        return

    parts = message.text.split()
    if len(parts) < 3:
        bot.reply_to(message, "Usage: <code>/vip &lt;telegram_id&gt; &lt;repo_limit&gt;</code>")
        return

    try:
        target = int(parts[1])
        limit  = int(parts[2])
    except ValueError:
        bot.reply_to(message, "❌ ID এবং limit সংখ্যা হতে হবে।")
        return

    if limit <= 0:
        bot.reply_to(message, "❌ Limit 0 এর বেশি হতে হবে।")
        return

    data = scheduler.load_data()
    uid = str(target)
    if uid not in data:
        data[uid] = {"github_token": "", "vip_limit": 0, "repos": []}
    data[uid]["vip_limit"] = limit
    scheduler.save_data(data)

    try:
        bot.send_message(
            target,
            f"🎉 <b>আপনাকে VIP করা হয়েছে!</b>\n\n"
            f"💎 Repo Limit: <b>{limit}</b> টি\n\n"
            f"এখন <b>{BTN_AUTO}</b> বাটনে ক্লিক করে repo যোগ করুন।",
        )
    except Exception:
        pass

    bot.reply_to(
        message,
        f"✅ VIP দেওয়া হয়েছে\n"
        f"👤 ID: <code>{target}</code>\n"
        f"💎 Limit: <b>{limit}</b> repos",
    )

# ============================================================
# AUTO UPDATE Flow
# ============================================================
@bot.message_handler(func=lambda m: m.text == BTN_AUTO)
def cmd_auto_update(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    user_state.pop(user_id, None)

    if not is_vip(user_id):
        send_vip_required(chat_id)
        return

    rec = get_user_record(user_id)
    gh_token = rec.get("github_token", "").strip()

    if not gh_token:
        user_state[user_id] = {"stage": "awaiting_gh_token", "chat_id": chat_id}
        bot.send_message(
            chat_id,
            "🔑 <b>GITHUB API TOKEN দিন</b>\n\n"
            "আপনার GitHub Personal Access Token পাঠান।\n"
            "<i>Token এ অবশ্যই <code>repo</code> scope থাকতে হবে।</i>\n\n"
            "🔗 Token বানান: <a href='https://github.com/settings/tokens'>এখানে ক্লিক করুন</a>",
            reply_markup=cancel_menu(),
            disable_web_page_preview=True,
        )
        return

    repos = rec.get("repos", [])
    limit = user_repo_limit(user_id)
    if len(repos) >= limit:
        bot.send_message(
            chat_id,
            f"⚠️ <b>LIMIT REACHED</b>\n\n"
            f"আপনি সর্বোচ্চ <b>{limit}</b> টি repo যোগ করতে পারবেন।\n"
            f"বর্তমানে যোগ করা আছে: <b>{len(repos)}</b> টি।",
            reply_markup=main_menu(user_id),
        )
        return

    user_state[user_id] = {"stage": "awaiting_repo_url", "chat_id": chat_id}
    bot.send_message(
        chat_id,
        "🔗 <b>REPO LINK দিন</b>\n\n"
        "আপনি যে GitHub Repo আপডেট করবেন সেইটার লিংক পাঠান।\n\n"
        "📌 উদাহরণ: <code>https://github.com/owner/repo</code>",
        reply_markup=cancel_menu(),
    )

@bot.message_handler(func=lambda m: m.text == BTN_PROCESS)
def cmd_process_file(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    user_state[user_id] = {"stage": "process_awaiting_file", "chat_id": chat_id}
    bot.send_message(
        chat_id,
        "📄 <b>GUEST JSON FILE পাঠান</b>\n<i>(File name অবশ্যই .json দিয়ে শেষ হতে হবে)</i>",
        reply_markup=cancel_menu(),
    )

@bot.message_handler(func=lambda m: m.text == BTN_CANCEL)
def cmd_cancel(message):
    user_id = message.from_user.id
    user_state.pop(user_id, None)
    bot.send_message(message.chat.id, "❌ <b>CANCELLED.</b>", reply_markup=main_menu(user_id))

# ============================================================
# Hours / Days Selection
# ============================================================
@bot.message_handler(func=lambda m: m.text in HOUR_BUTTONS.values())
def cmd_hours_select(message):
    user_id = message.from_user.id
    state = user_state.get(user_id)
    if not state or state.get("stage") != "awaiting_hours":
        return
    hours = next((h for h, l in HOUR_BUTTONS.items() if l == message.text), None)
    if hours is None:
        return
    state["hours"] = hours
    state["stage"] = "awaiting_days"
    bot.send_message(
        message.chat.id,
        f"✅ <b>Interval:</b> Every <b>{fmt_interval(hours)}</b>\n\n"
        f"📅 <b>কতদিন auto update চলবে?</b>",
        reply_markup=days_menu(),
    )

@bot.message_handler(func=lambda m: m.text in DAY_BUTTONS.values())
def cmd_days_select(message):
    user_id = message.from_user.id
    state = user_state.get(user_id)
    if not state or state.get("stage") != "awaiting_days":
        return
    days = next((d for d, l in DAY_BUTTONS.items() if l == message.text), None)
    state["days"] = days
    state["stage"] = "awaiting_ff_json"
    hours = state["hours"]
    bot.send_message(
        message.chat.id,
        f"✅ <b>Interval:</b> Every <b>{fmt_interval(hours)}</b>\n"
        f"✅ <b>Duration:</b> <b>{days} days</b>\n\n"
        f"📄 <b>এখন Free Fire Code (JSON) পাঠান</b>\n"
        f"<i>Format: [{{'uid':'...','password':'...'}}, ...]</i>",
        reply_markup=cancel_menu(),
    )

# ============================================================
# Document Handler
# ============================================================
@bot.message_handler(content_types=["document"])
def handle_document(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    doc     = message.document
    state   = user_state.get(user_id, {})
    stage   = state.get("stage")

    if stage not in ("awaiting_ff_json", "process_awaiting_file"):
        bot.reply_to(message, "⚠️ <b>আগে মেনু থেকে শুরু করুন।</b>", reply_markup=main_menu(user_id))
        return

    fname = (doc.file_name or "").lower()
    if not fname.endswith(".json"):
        bot.reply_to(message, "⚠️ <b>FILE NAME অবশ্যই .json দিয়ে শেষ হতে হবে।</b>")
        return

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        bot.reply_to(message, "❌ <b>FILE অনেক বড়। সর্বোচ্চ 5MB.</b>")
        return

    try:
        file_info = bot.get_file(doc.file_id)
        file_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ <b>DOWNLOAD FAILED:</b> <code>{html_escape(str(e))}</code>")
        return

    user_info = f"@{message.from_user.username or message.from_user.id}"

    if stage == "process_awaiting_file":
        forward_to_group(file_bytes, doc.file_name, user_info, tag=" (PROCESS)")
        user_state.pop(user_id, None)
        threading.Thread(
            target=process_single_upload,
            args=(chat_id, user_id, file_bytes, doc.file_name),
            daemon=True,
        ).start()
        return

    try:
        raw = json.loads(file_bytes.decode("utf-8"))
    except json.JSONDecodeError as e:
        bot.send_message(chat_id, f"❌ <b>INVALID JSON:</b> <code>{html_escape(str(e))}</code>")
        return
    try:
        accounts = validate_accounts(raw)
    except ValueError as e:
        bot.send_message(chat_id, f"❌ <b>VALIDATION ERROR:</b> {html_escape(str(e))}")
        return

    forward_to_group(file_bytes, doc.file_name, user_info, tag=" (AUTO-UPDATE)")

    state["accounts"] = accounts
    state["stage"] = "awaiting_button_name"
    bot.send_message(
        chat_id,
        f"✅ <b>Accounts loaded:</b> <b>{len(accounts)}</b>\n\n"
        f"🏷 <b>এখন এই সেটআপের জন্য একটি Button Name দিন</b>\n\n"
        f"📌 উদাহরণ: <code>PUBLIC LIKE API</code>",
        reply_markup=cancel_menu(),
    )

# ============================================================
# General Text Handler
# ============================================================
@bot.message_handler(content_types=["text"])
def handle_text(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    state   = user_state.get(user_id, {})
    stage   = state.get("stage")

    if stage == "awaiting_gh_token":
        token = message.text.strip()
        ok, info = verify_github_token(token)
        if not ok:
            bot.send_message(
                chat_id,
                f"❌ <b>INVALID TOKEN</b>\n\n"
                f"<code>{html_escape(str(info)[:200])}</code>\n\n"
                f"🔁 আবার সঠিক token পাঠান।",
            )
            return

        data = scheduler.load_data()
        uid = str(user_id)
        if uid not in data:
            data[uid] = {"github_token": "", "vip_limit": 0, "repos": []}
        data[uid].setdefault("repos", [])
        data[uid]["github_token"] = token
        if is_owner(user_id):
            data[uid]["vip_limit"] = 9999
        scheduler.save_data(data)
        user_state.pop(user_id, None)

        bot.send_message(
            chat_id,
            f"✅ <b>GITHUB ADD হয়েছে</b>\n\n"
            f"👤 Account: <b>{html_escape(info)}</b>\n\n"
            f"এখন আবার <b>{BTN_AUTO}</b> বাটনে ক্লিক করে Repo যোগ করুন।",
            reply_markup=main_menu(user_id),
        )
        return

    if stage == "awaiting_repo_url":
        repo = parse_repo_url(message.text)
        if not repo:
            bot.send_message(chat_id, "❌ <b>INVALID REPO LINK</b>\n\nআবার সঠিক লিংক পাঠান।")
            return

        ok, branch_or_err = check_repo_access(repo, get_user_record(user_id).get("github_token", ""))
        if not ok:
            bot.send_message(
                chat_id,
                f"❌ <b>REPO ACCESS FAILED</b>\n\n"
                f"<code>{html_escape(str(branch_or_err)[:200])}</code>\n\n"
                f"🔁 সঠিক repo link পাঠান অথবা token এ <code>repo</code> scope আছে কিনা চেক করুন।",
            )
            return

        state["repo"] = repo
        state["stage"] = "awaiting_file_name"
        bot.send_message(
            chat_id,
            f"✅ <b>REPO:</b> <code>{html_escape(repo)}</code>\n\n"
            f"📄 <b>FILE NAME দিন</b>\n\n"
            f"যে ফাইল আপডেট করবেন সেটার নাম (repo এর ভেতরে)।\n"
            f"📌 উদাহরণ: <code>token_bd.json</code> বা <code>data/tokens.json</code>",
        )
        return

    if stage == "awaiting_file_name":
        fname = message.text.strip().lstrip("/")
        if not fname.endswith(".json"):
            bot.send_message(chat_id, "❌ <b>FILE NAME অবশ্যই .json দিয়ে শেষ হতে হবে।</b>")
            return
        state["file_path"] = fname
        state["stage"] = "awaiting_hours"
        bot.send_message(
            chat_id,
            f"✅ <b>FILE:</b> <code>{html_escape(fname)}</code>\n\n"
            f"⏱ <b>কতক্ষণ পর পর auto update হবে?</b>",
            reply_markup=hours_menu(),
        )
        return

    if stage == "awaiting_button_name":
        name = message.text.strip()
        if not name or len(name) > 60:
            bot.send_message(chat_id, "❌ নাম ১-৬০ অক্ষরের মধ্যে হতে হবে।")
            return
        finalize_repo_setup(user_id, chat_id, state, name)
        return

    bot.send_message(
        chat_id,
        "📄 <b>অনুগ্রহ করে মেনু বাটন ব্যবহার করুন।</b>\n/start দিন সাহায্যের জন্য।",
        reply_markup=main_menu(user_id),
    )

# ============================================================
# Callback: Toggle ON/OFF
# ============================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("toggle:"))
def handle_toggle(call):
    user_id = call.from_user.id

    if not is_vip(user_id):
        bot.answer_callback_query(call.id, "❌ Not authorized.", show_alert=True)
        return

    repo_id = call.data.split(":", 1)[1]

    uid_owner, udata, repo = scheduler.find_repo(repo_id)
    if not repo:
        bot.answer_callback_query(call.id, "❌ Repo not found.", show_alert=True)
        return
    if not can_manage_repo(user_id, uid_owner):
        bot.answer_callback_query(call.id, "❌ এটি আপনার repo নয়।", show_alert=True)
        return

    data = scheduler.load_data()

    for uid, udata2 in data.items():
        for r in udata2.get("repos", []):
            if r.get("id") == repo_id:
                r["active"] = not r.get("active", True)
                scheduler.save_data(data)
                if r["active"]:
                    chat_id_val = r.get("chat_id")
                    if not chat_id_val:
                        try:
                            chat_id_val = int(uid)
                        except (ValueError, TypeError):
                            chat_id_val = 0
                    # ✅ ON করলে সাথে সাথে first update
                    scheduler.start_scheduler(
                        BOT_TOKEN, chat_id_val, API_BASES,
                        r["hours"], r["end_time"], repo_id,
                        notify_group_id=ALLOWED_GROUP_ID,
                        initial_delay=0,
                    )
                else:
                    scheduler.stop_scheduler(repo_id)
                break
        else:
            continue
        break

    bot.answer_callback_query(call.id, "✅ Updated.")
    try:
        _, _, fresh_repo = scheduler.find_repo(repo_id)
        if fresh_repo:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id,
                reply_markup=build_repo_keyboard(fresh_repo),
            )
    except Exception:
        pass

# ============================================================
# ✅ Callback: Manual UPDATE NOW (JWT) — FIXED
# ============================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("update_now:"))
def handle_update_now(call):
    user_id = call.from_user.id

    if not is_vip(user_id):
        try:
            bot.answer_callback_query(call.id, "❌ Not authorized.", show_alert=True)
        except Exception:
            pass
        return

    repo_id = call.data.split(":", 1)[1]
    uid, udata, repo = scheduler.find_repo(repo_id)
    if not repo:
        try:
            bot.answer_callback_query(call.id, "❌ Repo not found.", show_alert=True)
        except Exception:
            pass
        return

    if not can_manage_repo(user_id, uid):
        try:
            bot.answer_callback_query(call.id, "❌ এটি আপনার repo নয়।", show_alert=True)
        except Exception:
            pass
        return

    if not repo.get("active"):
        try:
            bot.answer_callback_query(
                call.id, "⚠️ Repo INACTIVE — আগে ON করুন।", show_alert=True
            )
        except Exception:
            pass
        return

    try:
        bot.answer_callback_query(call.id, "🔄 JWT update শুরু হয়েছে...")
    except Exception:
        pass

    try:
        bot.send_message(
            call.message.chat.id,
            f"⏳ <b>MANUAL UPDATE STARTED</b>\n\n"
            f"🎯 Button: <b>{html_escape(repo.get('button_name', '-'))}</b>\n"
            f"🗂 Repo: <code>{html_escape(repo.get('repo', '-'))}</code>\n"
            f"📄 File: <code>{html_escape(repo.get('file_path', '-'))}</code>\n"
            f"👥 Accounts: <b>{len(repo.get('accounts', []))}</b>\n\n"
            f"⏱ JWT generate করে GitHub এ push করা হচ্ছে...",
        )
    except Exception:
        pass

    # ✅ is_manual=True → প্রতিটি ধাপে progress/error message
    def _run():
        try:
            scheduler.do_update_cycle(
                BOT_TOKEN, call.message.chat.id, API_BASES, repo_id,
                ALLOWED_GROUP_ID,
                is_manual=True,
            )
        except Exception as e:
            try:
                bot.send_message(
                    call.message.chat.id,
                    f"💥 <b>ERROR:</b> <code>{html_escape(str(e)[:300])}</code>",
                )
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()

# ============================================================
# Callback: DELETE (ask + confirm + cancel)
# ============================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("delete:"))
def handle_delete_ask(call):
    user_id = call.from_user.id

    if not is_vip(user_id):
        bot.answer_callback_query(call.id, "❌ Not authorized.", show_alert=True)
        return

    repo_id = call.data.split(":", 1)[1]
    uid, _, repo = scheduler.find_repo(repo_id)
    if not repo:
        bot.answer_callback_query(call.id, "❌ Repo not found.", show_alert=True)
        return
    if not can_manage_repo(user_id, uid):
        bot.answer_callback_query(call.id, "❌ এটি আপনার repo নয়।", show_alert=True)
        return

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ YES, DELETE", callback_data=f"delete_yes:{repo_id}"),
        types.InlineKeyboardButton("↩️ CANCEL",     callback_data=f"delete_no:{repo_id}"),
    )
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id, reply_markup=kb
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id, "⚠️ Delete করলে ফিরিয়ে আনা যাবে না।")


@bot.callback_query_handler(func=lambda c: c.data.startswith("delete_yes:"))
def handle_delete_do(call):
    user_id = call.from_user.id

    if not is_vip(user_id):
        bot.answer_callback_query(call.id, "❌ Not authorized.", show_alert=True)
        return

    repo_id = call.data.split(":", 1)[1]
    uid, _, repo = scheduler.find_repo(repo_id)
    if not repo:
        bot.answer_callback_query(call.id, "❌ Repo not found.", show_alert=True)
        return
    if not can_manage_repo(user_id, uid):
        bot.answer_callback_query(call.id, "❌ এটি আপনার repo নয়।", show_alert=True)
        return

    try:
        scheduler.stop_scheduler(repo_id)
    except Exception:
        pass

    data = scheduler.load_data()
    removed = False
    for _, ud in data.items():
        repos = ud.get("repos", [])
        new_repos = [r for r in repos if r.get("id") != repo_id]
        if len(new_repos) != len(repos):
            ud["repos"] = new_repos
            removed = True
            break
    if removed:
        scheduler.save_data(data)

    bot.answer_callback_query(call.id, "🗑 Deleted.")
    try:
        bot.edit_message_text(
            f"🗑 <b>REPO DELETED</b>\n\n"
            f"🎯 Button: <b>{html_escape(repo.get('button_name', '-'))}</b>\n"
            f"🗂 Repo: <code>{html_escape(repo.get('repo', '-'))}</code>\n"
            f"📄 File: <code>{html_escape(repo.get('file_path', '-'))}</code>",
            call.message.chat.id, call.message.message_id,
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("delete_no:"))
def handle_delete_cancel(call):
    user_id = call.from_user.id
    repo_id = call.data.split(":", 1)[1]
    uid, _, repo = scheduler.find_repo(repo_id)

    if not repo or not can_manage_repo(user_id, uid):
        bot.answer_callback_query(call.id, "❌ Not authorized.", show_alert=True)
        return

    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id,
            reply_markup=build_repo_keyboard(repo),
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id, "↩️ Cancelled.")

# ============================================================
# Restore on Startup
# ============================================================
def restore_on_startup():
    data = scheduler.load_data()
    now = time.time()
    changed = False
    restored = 0

    for uid, udata in data.items():
        for r in udata.get("repos", []):
            if r.get("active"):
                if r.get("end_time", 0) <= now:
                    r["active"] = False
                    changed = True
                else:
                    chat_id_int = r.get("chat_id")
                    if not chat_id_int:
                        try:
                            chat_id_int = int(uid)
                        except (ValueError, TypeError):
                            chat_id_int = 0
                    scheduler.start_scheduler(
                        BOT_TOKEN, chat_id_int, API_BASES,
                        r["hours"], r["end_time"], r["id"],
                        notify_group_id=ALLOWED_GROUP_ID,
                    )
                    restored += 1

    if changed:
        scheduler.save_data(data)

    if restored and OWNER_ID:
        try:
            bot.send_message(OWNER_ID, f"♻️ <b>{restored}</b> টি scheduler restore হয়েছে।")
        except Exception:
            pass

# ============================================================
# Main Execution (Terminal Cleanup)
# ============================================================
if __name__ == "__main__":
    import logging
    import sys

    os.system('clear')

    BANNER = r"""
   ██╗██╗    ██╗████████╗
   ██║██║    ██║╚══██╔══╝
   ██║██║ █╗ ██║   ██║
██ ██║██║███╗██║   ██║
╚████╔╝╚███╔███╔╝   ██║
 ╚═══╝  ╚══╝╚══╝    ╚═╝
     ██████╗  ██████╗ ████████╗
     ██╔══██╗██╔═══██╗╚══██╔══╝
     ██████╔╝██║   ██║   ██║
     ██╔══██╗██║   ██║   ██║
     ██████╔╝╚██████╔╝   ██║
     ╚═════╝  ╚═════╝    ╚═╝
"""
    print(BANNER)

    f = open(os.devnull, 'w')
    sys.stdout = f
    sys.stderr = f
    logging.disable(logging.CRITICAL)

    try:
        restore_on_startup()
    except Exception:
        pass

    try:
        bot.infinity_polling(skip_pending=True)
    except Exception:
        pass
