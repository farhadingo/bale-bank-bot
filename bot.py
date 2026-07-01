import os
import time
import logging
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import psycopg2
from psycopg2 import pool
from datetime import datetime, timedelta, timezone
import threading

# ============================================
# تنظیمات پیشرفته لاگین
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# بارگذاری متغیرهای محیطی
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not DB_URL:
    logger.error("❌ BOT_TOKEN and DATABASE_URL environment variables are required!")
    logger.error("❌ Please add them in Render Environment Variables section")
    exit(1)

BASE_URL = f"https://tapi.bale.ai/bot{BOT_TOKEN}"
logger.info(f"✅ Environment variables loaded")
logger.info(f"✅ Bale API URL: {BASE_URL}")

# ============================================
# 🔧 Setup Session with Keep-Alive & Retry
# ============================================
def create_session():
    session = requests.Session()
    session.headers.update({
        'Connection': 'keep-alive',
        'User-Agent': 'Bale-Bank-Bot/2.0'
    })
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=20,
        pool_maxsize=20
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

requests_session = create_session()

# ============================================
# 🗄️ Database Connection Pool
# ============================================
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DB_URL)
    logger.info("✅ Database connection pool created.")
except Exception as e:
    logger.error(f"❌ Failed to create pool: {e}")
    db_pool = None

# ============================================
# 📦 State Management & Helpers
# ============================================
user_states = {}          # {chat_id: {state, data}}
last_activity = {}        # برای Keep-Alive هوشمند

def get_db_connection():
    if db_pool:
        try:
            return db_pool.getconn()
        except psycopg2.OperationalError:
            return psycopg2.connect(DB_URL)
    return psycopg2.connect(DB_URL)

def return_db_connection(conn):
    if db_pool:
        try:
            db_pool.putconn(conn)
        except Exception:
            conn.close()
    else:
        conn.close()

def get_iran_time():
    iran_timezone = timezone(timedelta(hours=3, minutes=30))
    return datetime.now(iran_timezone)

def get_shamsi_date(days_offset=0):
    now_iran = get_iran_time() + timedelta(days=days_offset)
    g_y, g_m, g_d = now_iran.year, now_iran.month, now_iran.day
    g_days_in_month = [31,28,31,30,31,30,31,31,30,31,30,31]
    if (g_y%4==0 and g_y%100!=0) or (g_y%400==0):
        g_days_in_month[1]=29
    gy = g_y - 1600
    gm = g_m - 1
    gd = g_d - 1
    g_day_no = 365*gy + gy//4 - gy//100 + gy//400
    for i in range(gm):
        g_day_no += g_days_in_month[i]
    g_day_no += gd
    jy = 979 + 33*(g_day_no//12053) + 4*((g_day_no%12053)//1461)
    g_day_no %= 1461
    if g_day_no >= 366:
        jy += (g_day_no-1)//365
        g_day_no = (g_day_no-1)%365
    if g_day_no < 186:
        jm = 1 + g_day_no//31
        jd = 1 + (g_day_no%31)
    else:
        jm = 7 + (g_day_no-186)//30
        jd = 1 + ((g_day_no-186)%30)
    return f"{jy}/{jm:02d}/{jd:02d}"

def get_shamsi_date_formatted(shamsi_str):
    if not shamsi_str:
        return "نامعلوم"
    months = {
        '01':'فروردین','02':'اردیبهشت','03':'خرداد',
        '04':'تیر','05':'مرداد','06':'شهریور',
        '07':'مهر','08':'آبان','09':'آذر',
        '10':'دی','11':'بهمن','12':'اسفند'
    }
    parts = shamsi_str.split('/')
    return f"{parts[2]} {months[parts[1]]} {parts[0]}"

def safe_format(value, default="0"):
    return value if value is not None else default

def send_message(chat_id, text, reply_markup=None):
    """ارسال پیام با مدیریت خطا و جلوگیری از ارسال دوباره"""
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests_session.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            return res.json()
        else:
            logger.error(f"❌ sendMessage failed: {res.status_code} - {res.text}")
            return None
    except Exception as e:
        logger.error(f"❌ Error sending message to {chat_id}: {e}")
        return None

# ============================================
# ⌨️ Keyboards
# ============================================
def get_deputy_keyboard():
    return {
        "keyboard": [
            [{"text": "💰 ثبت وصولی روزانه"}, {"text": "📊 گزارش وصولی"}],
            [{"text": "📈 مقایسه عملکرد"}, {"text": "📋 مشاهده ثبت امروز"}],
            [{"text": "🔙 خروج"}, {"text": "❓ راهنما"}]
        ],
        "resize_keyboard": True
    }

def get_admin_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 گزارش امروز"}, {"text": "📈 گزارش ۱۰ روز اخیر"}],
            [{"text": "🏆 رتبه‌بندی شعب"}, {"text": "💹 آمار مفصل"}],
            [{"text": "📉 مقایسه روزانه"}, {"text": "🎯 تحلیل عملکرد"}],
            [{"text": "📅 گزارش تاریخ خاص"}, {"text": "🗑 حذف رکورد"}],
            [{"text": "🔙 خروج"}, {"text": "❓ راهنما"}]
        ],
        "resize_keyboard": True
    }

def get_cancel_keyboard():
    return {"keyboard": [[{"text": "🔙 انصراف"}]], "resize_keyboard": True}

# ============================================
# 📊 Database Functions (افزوده‌ها و بهبودها)
# ============================================

def find_user_by_employee_number(emp_num):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.employee_number, u.full_name, u.role, u.title, u.branch_id, b.name 
                FROM users u
                LEFT JOIN branches b ON u.branch_id = b.id
                WHERE u.employee_number = %s
            """, (emp_num,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"❌ find_user_by_employee_number: {e}")
        return None
    finally:
        return_db_connection(conn)

def update_user_telegram_id(user_db_id, chat_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET telegram_id = %s WHERE id = %s", (chat_id, user_db_id))
            conn.commit()
    except Exception as e:
        logger.error(f"❌ update_user_telegram_id: {e}")
    finally:
        return_db_connection(conn)

def find_user_by_telegram_id(chat_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.employee_number, u.full_name, u.role, u.title, u.branch_id, b.name 
                FROM users u
                LEFT JOIN branches b ON u.branch_id = b.id
                WHERE u.telegram_id = %s
            """, (chat_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"❌ find_user_by_telegram_id: {e}")
        return None
    finally:
        return_db_connection(conn)

def check_existing_collection(branch_id, shamsi_date):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, deputy_amount, others_amount 
                FROM collections 
                WHERE branch_id = %s AND shamsi_date = %s
            """, (branch_id, shamsi_date))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"❌ check_existing_collection: {e}")
        return None
    finally:
        return_db_connection(conn)

def save_or_update_collection(branch_id, deputy_amount, others_amount, shamsi_date, user_id, update_existing=False):
    conn = get_db_connection()
    created_at_iran = get_iran_time()
    try:
        with conn.cursor() as cur:
            if update_existing:
                cur.execute("""
                    UPDATE collections 
                    SET deputy_amount = %s, others_amount = %s, recorded_by = %s, updated_at = %s
                    WHERE branch_id = %s AND shamsi_date = %s
                """, (deputy_amount, others_amount, user_id, created_at_iran, branch_id, shamsi_date))
            else:
                cur.execute("""
                    INSERT INTO collections (branch_id, deputy_amount, others_amount, shamsi_date, recorded_by, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (branch_id, deputy_amount, others_amount, shamsi_date, user_id, created_at_iran))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"❌ save_or_update_collection: {e}")
        return False
    finally:
        return_db_connection(conn)

def delete_collection(collection_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM collections WHERE id = %s", (collection_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"❌ delete_collection: {e}")
        return False
    finally:
        return_db_connection(conn)

def get_branch_10_day_report(branch_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shamsi_date, deputy_amount, others_amount, total_amount 
                FROM collections 
                WHERE branch_id = %s 
                ORDER BY shamsi_date DESC 
                LIMIT 10
            """, (branch_id,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ get_branch_10_day_report: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_today_province_report(shamsi_date):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.name, c.deputy_amount, c.others_amount, c.total_amount
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                WHERE c.shamsi_date = %s
                ORDER BY c.total_amount DESC
            """, (shamsi_date,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ get_today_province_report: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_province_10_day_report():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shamsi_date, SUM(deputy_amount), SUM(others_amount), SUM(total_amount)
                FROM collections
                GROUP BY shamsi_date
                ORDER BY shamsi_date DESC
                LIMIT 10
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ get_province_10_day_report: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_top_5_branches():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.name, SUM(c.total_amount) as total, COUNT(*) as record_count
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                GROUP BY b.name
                ORDER BY total DESC
                LIMIT 5
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ get_top_5_branches: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_today_statistics():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            shamsi_today = get_shamsi_date()
            cur.execute("""
                SELECT COUNT(DISTINCT branch_id), SUM(deputy_amount), SUM(others_amount), SUM(total_amount)
                FROM collections
                WHERE shamsi_date = %s
            """, (shamsi_today,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"❌ get_today_statistics: {e}")
        return None
    finally:
        return_db_connection(conn)

def get_yesterday_vs_today():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            shamsi_today = get_shamsi_date()
            shamsi_yesterday = get_shamsi_date(-1)
            cur.execute("""
                SELECT 
                    (SELECT SUM(total_amount) FROM collections WHERE shamsi_date = %s) as today_total,
                    (SELECT SUM(total_amount) FROM collections WHERE shamsi_date = %s) as yesterday_total
            """, (shamsi_today, shamsi_yesterday))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"❌ get_yesterday_vs_today: {e}")
        return None
    finally:
        return_db_connection(conn)

def get_detailed_report(shamsi_date):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.name, c.deputy_amount, c.others_amount, c.total_amount, u.full_name
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                JOIN users u ON c.recorded_by = u.id
                WHERE c.shamsi_date = %s
                ORDER BY c.total_amount DESC
            """, (shamsi_date,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ get_detailed_report: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_branch_performance(branch_id, days=10):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    shamsi_date,
                    SUM(total_amount) as daily_total,
                    AVG(total_amount) OVER (ORDER BY shamsi_date DESC ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) as avg_3day
                FROM collections
                WHERE branch_id = %s
                GROUP BY shamsi_date
                ORDER BY shamsi_date DESC
                LIMIT %s
            """, (branch_id, days))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ get_branch_performance: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_daily_comparison():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    shamsi_date,
                    COUNT(DISTINCT branch_id) as branches_count,
                    SUM(total_amount) as total_collection
                FROM collections
                GROUP BY shamsi_date
                ORDER BY shamsi_date DESC
                LIMIT 7
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ get_daily_comparison: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_deputy_vs_others_ratio():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    SUM(deputy_amount) as deputy_total,
                    SUM(others_amount) as others_total,
                    ROUND(100.0 * SUM(deputy_amount) / NULLIF(SUM(deputy_amount) + SUM(others_amount), 0), 2) as deputy_percentage
                FROM collections
                WHERE shamsi_date = %s
            """, (get_shamsi_date(),))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"❌ get_deputy_vs_others_ratio: {e}")
        return None
    finally:
        return_db_connection(conn)

def get_collection_by_id(collection_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, b.name, c.shamsi_date, c.deputy_amount, c.others_amount, c.total_amount, u.full_name
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                JOIN users u ON c.recorded_by = u.id
                WHERE c.id = %s
            """, (collection_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"❌ get_collection_by_id: {e}")
        return None
    finally:
        return_db_connection(conn)

def get_report_by_date(shamsi_date):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.name, c.deputy_amount, c.others_amount, c.total_amount, u.full_name
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                JOIN users u ON c.recorded_by = u.id
                WHERE c.shamsi_date = %s
                ORDER BY c.total_amount DESC
            """, (shamsi_date,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ get_report_by_date: {e}")
        return []
    finally:
        return_db_connection(conn)

# ============================================
# 📨 Message Handlers (با رفع ارسال دوبله)
# ============================================

def handle_message(message):
    try:
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()
        last_activity[chat_id] = time.time()

        user = find_user_by_telegram_id(chat_id)
        user_state = user_states.get(chat_id, {"state": "LOGGED_OUT"})

        # ------------------- ورود کاربر جدید -------------------
        if not user:
            if user_state.get("state") == "WAITING_FOR_EMP_NUM":
                emp_user = find_user_by_employee_number(text)
                if emp_user:
                    db_id, emp_num, name, role, title, branch_id, branch_name = emp_user
                    update_user_telegram_id(db_id, chat_id)
                    user_states[chat_id] = {"state": "LOGGED_IN"}
                    welcome_msg = (
                        f"✅ هویت شما تایید شد.\n\n"
                        f"👤 {name}\n"
                        f"🏢 {title}\n"
                        f"🏭 واحد: {branch_name or 'ستاد استان'}\n"
                        f"🔑 شماره کارمندی: {emp_num}\n"
                        f"⏰ زمان ورود: {get_shamsi_date_formatted(get_shamsi_date())} {get_iran_time().strftime('%H:%M:%S')}\n\n"
                        f"خوش آمدید! 👋"
                    )
                    keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                    send_message(chat_id, welcome_msg, keyboard)
                else:
                    send_message(chat_id, "❌ شماره کارمندی در سیستم یافت نشد.\nلطفاً شماره کارمندی صحیح خود را بفرستید:")
                return
            else:
                user_states[chat_id] = {"state": "WAITING_FOR_EMP_NUM"}
                send_message(chat_id, "👋 سلام! به ربات وصول مطالبات استان زنجان خوش آمدید.\n\n🔐 جهت دسترسی، لطفاً شماره کارمندی خود را ارسال کنید:")
                return

        # کاربر احراز شده
        user_db_id, employee_number, full_name, role, title, branch_id, branch_name = user
        state = user_state.get("state", "LOGGED_IN")

        # ---------- مدیریت وضعیت‌های ورودی (ثبت مبالغ) ----------
        if state == "WAITING_FOR_DEPUTY_AMOUNT":
            if text == "🔙 انصراف":
                user_states[chat_id] = {"state": "LOGGED_IN"}
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", keyboard)
                return
            try:
                amount = int(text.replace(',', '').replace('،', ''))
                if amount < 0:
                    raise ValueError
                user_states[chat_id] = {
                    "state": "WAITING_FOR_OTHERS_AMOUNT",
                    "deputy_amount": amount,
                    "edit_mode": user_state.get("edit_mode", False)
                }
                send_message(chat_id, "✏️ اکنون میزان وصولی سایر همکاران شعبه را وارد کنید (برحسب ریال):", get_cancel_keyboard())
            except ValueError:
                send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت وارد کنید.")
            return

        elif state == "WAITING_FOR_OTHERS_AMOUNT":
            if text == "🔙 انصراف":
                user_states[chat_id] = {"state": "LOGGED_IN"}
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", keyboard)
                return
            try:
                others_amount = int(text.replace(',', '').replace('،', ''))
                if others_amount < 0:
                    raise ValueError
                deputy_amount = user_state.get("deputy_amount", 0)
                shamsi_today = get_shamsi_date()
                is_edit = user_state.get("edit_mode", False)
                success = save_or_update_collection(
                    branch_id=branch_id,
                    deputy_amount=deputy_amount,
                    others_amount=others_amount,
                    shamsi_date=shamsi_today,
                    user_id=user_db_id,
                    update_existing=is_edit
                )
                user_states[chat_id] = {"state": "LOGGED_IN"}
                if success:
                    total = deputy_amount + others_amount
                    status_text = "✏️ بروزرسانی" if is_edit else "✅ ثبت"
                    msg = (
                        f"{status_text} شد.\n\n"
                        f"📊 خلاصه ثبت:\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"🏢 شعبه: {branch_name}\n"
                        f"📅 تاریخ: {get_shamsi_date_formatted(shamsi_today)}\n"
                        f"👤 وصولی معاون: {deputy_amount:,.0f} ریال\n"
                        f"👥 وصولی سایر همکاران: {others_amount:,.0f} ریال\n"
                        f"💰 جمع کل: {total:,.0f} ریال\n"
                        f"━━━━━━━━━━━━━━━"
                    )
                    keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                    send_message(chat_id, "❌ خطا در ثبت اطلاعات. لطفا مجدداً تلاش کنید.", keyboard)
            except ValueError:
                send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت وارد کنید.")
            return

        elif state == "WAITING_FOR_EDIT_CONFIRMATION":
            if text == "📝 بله، ویرایش شود":
                user_states[chat_id] = {"state": "WAITING_FOR_DEPUTY_AMOUNT", "edit_mode": True}
                send_message(chat_id, "✏️ لطفاً مبلغ جدید وصولی خود (معاون) را وارد کنید:", get_cancel_keyboard())
            else:
                user_states[chat_id] = {"state": "LOGGED_IN"}
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", keyboard)
            return

        # ---------- دستورات ویژه ادمین (حذف رکورد) ----------
        if role == 'admin' and state == "WAITING_FOR_DELETE_ID":
            if text == "🔙 انصراف":
                user_states[chat_id] = {"state": "LOGGED_IN"}
                send_message(chat_id, "❌ عملیات لغو شد.", get_admin_keyboard())
                return
            try:
                col_id = int(text.strip())
                record = get_collection_by_id(col_id)
                if not record:
                    send_message(chat_id, "❌ رکوردی با این شناسه یافت نشد.")
                    user_states[chat_id] = {"state": "LOGGED_IN"}
                    send_message(chat_id, "به منوی اصلی بازگشتید.", get_admin_keyboard())
                    return
                # تأیید نهایی
                msg = (
                    f"⚠️ آیا از حذف رکورد زیر اطمینان دارید؟\n"
                    f"شناسه: {record[0]}\n"
                    f"شعبه: {record[1]}\n"
                    f"تاریخ: {get_shamsi_date_formatted(record[2])}\n"
                    f"مبلغ کل: {record[5]:,.0f} ریال\n"
                    f"ثبت‌کننده: {record[6]}\n\n"
                    f"برای تأیید، عدد {col_id} را دوباره وارد کنید."
                )
                user_states[chat_id] = {"state": "WAITING_FOR_DELETE_CONFIRM", "delete_id": col_id}
                send_message(chat_id, msg, get_cancel_keyboard())
            except ValueError:
                send_message(chat_id, "❌ شناسه را به صورت عدد وارد کنید.")
            return

        if role == 'admin' and state == "WAITING_FOR_DELETE_CONFIRM":
            if text == "🔙 انصراف":
                user_states[chat_id] = {"state": "LOGGED_IN"}
                send_message(chat_id, "❌ عملیات لغو شد.", get_admin_keyboard())
                return
            try:
                confirm_id = int(text.strip())
                expected_id = user_state.get("delete_id")
                if confirm_id == expected_id:
                    if delete_collection(expected_id):
                        send_message(chat_id, f"✅ رکورد با شناسه {expected_id} با موفقیت حذف شد.", get_admin_keyboard())
                    else:
                        send_message(chat_id, "❌ خطا در حذف رکورد.", get_admin_keyboard())
                else:
                    send_message(chat_id, "❌ شناسه تأیید اشتباه است. عملیات لغو شد.", get_admin_keyboard())
                user_states[chat_id] = {"state": "LOGGED_IN"}
            except ValueError:
                send_message(chat_id, "❌ شناسه را به صورت عدد وارد کنید.")
            return

        # ---------- دستورات ادمین: گزارش تاریخ خاص ----------
        if role == 'admin' and state == "WAITING_FOR_REPORT_DATE":
            if text == "🔙 انصراف":
                user_states[chat_id] = {"state": "LOGGED_IN"}
                send_message(chat_id, "❌ عملیات لغو شد.", get_admin_keyboard())
                return
            # بررسی فرمت YYYY/MM/DD
            parts = text.split('/')
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                shamsi_date = text
                report = get_report_by_date(shamsi_date)
                if report:
                    msg = f"📅 گزارش تاریخ {get_shamsi_date_formatted(shamsi_date)}\n━━━━━━━━━━━━━━━━━━\n\n"
                    total_all = 0
                    for idx, row in enumerate(report, 1):
                        dep = int(safe_format(row[1]))
                        oth = int(safe_format(row[2]))
                        tot = int(safe_format(row[3]))
                        msg += f"{idx}. 🏢 {row[0]}\n"
                        msg += f"   👤 معاون ({row[4]}): {dep:,.0f}\n"
                        msg += f"   👥 همکاران: {oth:,.0f}\n"
                        msg += f"   💰 جمع: {tot:,.0f} ریال\n\n"
                        total_all += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"💰 جمع کل: {total_all:,.0f} ریال"
                    send_message(chat_id, msg, get_admin_keyboard())
                else:
                    send_message(chat_id, f"📭 هیچ داده‌ای برای تاریخ {shamsi_date} یافت نشد.", get_admin_keyboard())
            else:
                send_message(chat_id, "❌ فرمت تاریخ را به صورت YYYY/MM/DD وارد کنید (مثلاً 1403/01/15).")
                return
            user_states[chat_id] = {"state": "LOGGED_IN"}
            return

        # ---------- خروج از سیستم ----------
        if text == "🔙 خروج":
            user_states[chat_id] = {"state": "WAITING_FOR_EMP_NUM"}
            send_message(chat_id, "👋 شما از سیستم خارج شدید.\n\nبرای ورود مجدد، شماره کارمندی خود را ارسال کنید.")
            return

        # ---------- راهنما ----------
        if text == "❓ راهنما":
            help_text = (
                "📌 **راهنمای ربات وصول مطالبات**\n\n"
                "🔹 **معاونین شعب:**\n"
                "   • ثبت وصولی روزانه (با قابلیت ویرایش)\n"
                "   • مشاهده گزارش ۱۰ روز اخیر شعبه\n"
                "   • مقایسه عملکرد روزانه شعبه\n"
                "   • مشاهده ثبت امروز (در صورت وجود)\n\n"
                "🔹 **کاربران ارشد (ادمین):**\n"
                "   • گزارش امروز (همه شعب)\n"
                "   • گزارش ۱۰ روز اخیر استان\n"
                "   • رتبه‌بندی شعب برتر\n"
                "   • آمار مفصل امروز\n"
                "   • مقایسه روزانه ۷ روز اخیر\n"
                "   • تحلیل عملکرد (نسبت معاون/سایر)\n"
                "   • گزارش تاریخ خاص (وارد کردن YYYY/MM/DD)\n"
                "   • حذف رکورد (با شناسه)\n\n"
                "🔸 در هر مرحله می‌توانید با دکمه «انصراف» به منو برگردید.\n"
                "🔸 برای خروج کامل از سیستم، گزینه «خروج» را انتخاب کنید."
            )
            keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
            send_message(chat_id, help_text, keyboard)
            return

        # ---------- منوی معاونین ----------
        if role == 'deputy':
            if text == "💰 ثبت وصولی روزانه":
                shamsi_today = get_shamsi_date()
                existing = check_existing_collection(branch_id, shamsi_today)
                if existing:
                    col_id, dep_val, oth_val = existing
                    user_states[chat_id] = {"state": "WAITING_FOR_EDIT_CONFIRMATION"}
                    confirm_keyboard = {
                        "keyboard": [[{"text": "📝 بله، ویرایش شود"}, {"text": "❌ خیر، لغو شود"}]],
                        "resize_keyboard": True
                    }
                    msg = (
                        f"⚠️ اطلاعات امروز قبلاً ثبت شده است.\n\n"
                        f"📋 وضعیت فعلی:\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"🏢 شعبه: {branch_name}\n"
                        f"📅 تاریخ: {get_shamsi_date_formatted(shamsi_today)}\n"
                        f"👤 وصولی معاون: {int(safe_format(dep_val)):,.0f} ریال\n"
                        f"👥 وصولی همکاران: {int(safe_format(oth_val)):,.0f} ریال\n"
                        f"💰 جمع کل: {int(safe_format(dep_val)) + int(safe_format(oth_val)):,.0f} ریال\n"
                        f"━━━━━━━━━━━━━━━\n\n"
                        f"❓ آیا مایل به ویرایش هستید؟"
                    )
                    send_message(chat_id, msg, confirm_keyboard)
                else:
                    user_states[chat_id] = {"state": "WAITING_FOR_DEPUTY_AMOUNT", "edit_mode": False}
                    send_message(chat_id, "📝 لطفاً میزان وصولی خود (معاون) را وارد کنید:", get_cancel_keyboard())

            elif text == "📊 گزارش وصولی":
                report = get_branch_10_day_report(branch_id)
                if report:
                    msg = f"📊 گزارش وصول شعبه {branch_name}\n(۱۰ روز اخیر)\n━━━━━━━━━━━━━━━━━━\n\n"
                    total_sum = 0
                    for i, row in enumerate(report, 1):
                        dep = int(safe_format(row[1]))
                        oth = int(safe_format(row[2]))
                        tot = int(safe_format(row[3]))
                        msg += f"{i}. 📅 {get_shamsi_date_formatted(row[0])}\n"
                        msg += f"   👤 معاون: {dep:,.0f}\n"
                        msg += f"   👥 همکاران: {oth:,.0f}\n"
                        msg += f"   💰 جمع: {tot:,.0f} ریال\n\n"
                        total_sum += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"📈 جمع ۱۰ روز: {total_sum:,.0f} ریال\n"
                    msg += f"📊 میانگین روزانه: {total_sum//len(report):,.0f} ریال"
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, "📊 هیچ سابقه وصولی برای شعبه شما یافت نشد.")

            elif text == "📈 مقایسه عملکرد":
                perf = get_branch_performance(branch_id, 7)
                if perf:
                    msg = f"📈 تحلیل عملکرد شعبه {branch_name}\n(۷ روز اخیر)\n━━━━━━━━━━━━━━━━━━\n\n"
                    for i, row in enumerate(perf, 1):
                        daily = int(safe_format(row[1]))
                        avg = int(safe_format(row[2]))
                        trend = "📈" if i < len(perf) and perf[i-1][1] and row[1] and perf[i-1][1] > row[1] else "📉"
                        msg += f"{trend} {get_shamsi_date_formatted(row[0])}\n"
                        msg += f"   جمع روزانه: {daily:,.0f} ریال\n"
                        msg += f"   میانگین متحرک: {avg:,.0f} ریال\n\n"
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, "📈 داده کافی برای تحلیل وجود ندارد.")

            elif text == "📋 مشاهده ثبت امروز":
                shamsi_today = get_shamsi_date()
                existing = check_existing_collection(branch_id, shamsi_today)
                if existing:
                    col_id, dep_val, oth_val = existing
                    msg = (
                        f"📋 ثبت امروز شعبه {branch_name}\n"
                        f"📅 تاریخ: {get_shamsi_date_formatted(shamsi_today)}\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"👤 وصولی معاون: {int(safe_format(dep_val)):,.0f} ریال\n"
                        f"👥 وصولی همکاران: {int(safe_format(oth_val)):,.0f} ریال\n"
                        f"💰 جمع کل: {int(safe_format(dep_val)) + int(safe_format(oth_val)):,.0f} ریال"
                    )
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, f"📭 امروز ({shamsi_today}) هنوز ثبت نشده است.")
            else:
                send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", get_deputy_keyboard())

        # ---------- منوی ادمین ----------
        elif role == 'admin':
            if text == "📊 گزارش امروز":
                shamsi_today = get_shamsi_date()
                report = get_today_province_report(shamsi_today)
                stats = get_today_statistics()
                if report:
                    msg = f"📊 گزارش وصول امروز\n📅 تاریخ: {get_shamsi_date_formatted(shamsi_today)}\n━━━━━━━━━━━━━━━━━━\n\n"
                    total_province = 0
                    for idx, row in enumerate(report, 1):
                        dep = int(safe_format(row[1]))
                        oth = int(safe_format(row[2]))
                        tot = int(safe_format(row[3]))
                        msg += f"{idx}. 🏢 {row[0]}\n"
                        msg += f"   👤 معاون: {dep:,.0f}\n"
                        msg += f"   👥 همکاران: {oth:,.0f}\n"
                        msg += f"   💰 جمع: {tot:,.0f} ریال\n\n"
                        total_province += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    if stats:
                        s0 = int(safe_format(stats[0]))
                        s1 = int(safe_format(stats[1]))
                        s2 = int(safe_format(stats[2]))
                        msg += f"📈 خلاصه:\n"
                        msg += f"   تعداد شعب ثبت شده: {s0}\n"
                        msg += f"   کل وصولی معاونین: {s1:,.0f}\n"
                        msg += f"   کل وصولی همکاران: {s2:,.0f}\n"
                        msg += f"   💰 جمع کل استان: {total_province:,.0f} ریال"
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, f"📊 امروز ({shamsi_today}) هنوز هیچ شعبه‌ای اطلاعات ثبت نکرده است.")

            elif text == "📈 گزارش ۱۰ روز اخیر":
                report = get_province_10_day_report()
                if report:
                    msg = f"📈 گزارش ۱۰ روز اخیر استان زنجان\n━━━━━━━━━━━━━━━━━━\n\n"
                    total_all = 0
                    for row in report:
                        r1 = int(safe_format(row[1]))
                        r2 = int(safe_format(row[2]))
                        r3 = int(safe_format(row[3]))
                        msg += f"📅 {get_shamsi_date_formatted(row[0])}\n"
                        msg += f"   👤 معاونین: {r1:,.0f}\n"
                        msg += f"   👥 سایر همکاران: {r2:,.0f}\n"
                        msg += f"   💰 جمع: {r3:,.0f} ریال\n\n"
                        total_all += r3
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"📊 کل ۱۰ روز: {total_all:,.0f} ریال"
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, "📈 دیتابیس خالی است.")

            elif text == "🏆 رتبه‌بندی شعب":
                report = get_top_5_branches()
                if report:
                    msg = f"🏆 ۵ شعبه برتر استان زنجان\n━━━━━━━━━━━━━━━━━━\n\n"
                    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
                    for idx, row in enumerate(report):
                        tot = int(safe_format(row[1]))
                        cnt = int(safe_format(row[2]))
                        msg += f"{medals[idx]} {row[0]}\n"
                        msg += f"    💰 کل وصولی: {tot:,.0f} ریال\n"
                        msg += f"    📊 تعداد ثبت: {cnt} روز\n\n"
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, "🏆 داده کافی برای رتبه‌بندی وجود ندارد.")

            elif text == "💹 آمار مفصل":
                shamsi_today = get_shamsi_date()
                report = get_detailed_report(shamsi_today)
                if report:
                    msg = f"💹 آمار مفصل امروز\n━━━━━━━━━━━━━━━━━━\n\n"
                    for idx, row in enumerate(report, 1):
                        dep = int(safe_format(row[1]))
                        oth = int(safe_format(row[2]))
                        tot = int(safe_format(row[3]))
                        msg += f"{idx}. 🏢 {row[0]}\n"
                        msg += f"   👤 معاون ({row[4]}): {dep:,.0f} ریال\n"
                        msg += f"   👥 سایرین: {oth:,.0f} ریال\n"
                        msg += f"   💰 جمع: {tot:,.0f} ریال\n\n"
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, "💹 برای امروز اطلاعاتی وجود ندارد.")

            elif text == "📉 مقایسه روزانه":
                comparison = get_daily_comparison()
                if comparison:
                    msg = f"📉 مقایسه روزانه (۷ روز اخیر)\n━━━━━━━━━━━━━━━━━━\n\n"
                    for row in comparison:
                        br = int(safe_format(row[1]))
                        tot = int(safe_format(row[2]))
                        msg += f"📅 {get_shamsi_date_formatted(row[0])}\n"
                        msg += f"    🏢 شعب ثبت‌کننده: {br}\n"
                        msg += f"    💰 کل وصولی: {tot:,.0f} ریال\n\n"
                    send_message(chat_id, msg)
                else:
                    send_message(chat_id, "📉 داده کافی وجود ندارد.")

            elif text == "🎯 تحلیل عملکرد":
                ratio = get_deputy_vs_others_ratio()
                yesterday_today = get_yesterday_vs_today()
                msg = f"🎯 تحلیل عملکرد استان\n━━━━━━━━━━━━━━━━━━\n\n"
                if ratio:
                    r0 = int(safe_format(ratio[0]))
                    r1 = int(safe_format(ratio[1]))
                    r2 = float(safe_format(ratio[2], "0"))
                    msg += f"📊 نسبت امروز:\n"
                    msg += f"   👤 وصولی معاونین: {r0:,.0f} ریال ({r2:.1f}%)\n"
                    msg += f"   👥 وصولی سایرین: {r1:,.0f} ریال ({100-r2:.1f}%)\n\n"
                if yesterday_today:
                    today = int(safe_format(yesterday_today[0]))
                    yesterday = int(safe_format(yesterday_today[1]))
                    change = ((today - yesterday) / yesterday * 100) if yesterday > 0 else 0
                    trend = "📈 افزایش" if change > 0 else "📉 کاهش" if change < 0 else "➡️ ثابت"
                    msg += f"📅 مقایسه امروز/دیروز:\n"
                    msg += f"   امروز: {today:,.0f} ریال\n"
                    msg += f"   دیروز: {yesterday:,.0f} ریال\n"
                    msg += f"   {trend}: {abs(change):.1f}%\n"
                send_message(chat_id, msg)

            elif text == "📅 گزارش تاریخ خاص":
                user_states[chat_id] = {"state": "WAITING_FOR_REPORT_DATE"}
                send_message(chat_id, "📅 لطفاً تاریخ مورد نظر را به فرمت **YYYY/MM/DD** وارد کنید (مثلاً 1403/01/15):", get_cancel_keyboard())

            elif text == "🗑 حذف رکورد":
                user_states[chat_id] = {"state": "WAITING_FOR_DELETE_ID"}
                send_message(chat_id, "🗑 لطفاً شناسه (ID) رکورد مورد نظر برای حذف را وارد کنید.\n\nبرای پیدا کردن شناسه، از گزارش‌های مفصل استفاده کنید.", get_cancel_keyboard())

            else:
                send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", get_admin_keyboard())

        else:
            send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید.", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())

    except Exception as e:
        logger.error(f"❌ Error in handle_message: {e}", exc_info=True)
        try:
            send_message(message['chat']['id'], "❌ خطایی در پردازش پیام رخ داد. لطفاً مجدداً تلاش کنید.")
        except:
            pass

# ============================================
# ♻️ Keep-Alive Thread
# ============================================
def keep_alive_loop():
    """هر ۶۰ ثانیه یک درخواست getMe می‌فرستد تا اتصال زنده بماند"""
    while True:
        try:
            time.sleep(60)
            url = f"{BASE_URL}/getMe"
            res = requests_session.get(url, timeout=10)
            if res.status_code == 200:
                logger.debug("🔄 Keep-alive ping sent.")
            else:
                logger.warning(f"⚠️ Keep-alive ping failed: {res.status_code}")
        except Exception as e:
            logger.error(f"❌ Keep-alive error: {e}")

# ============================================
# 🚀 Main Polling Loop
# ============================================
def main():
    offset = 0
    logger.info("🤖 Bot started successfully!")
    logger.info("📡 Waiting for messages...")

    # راه‌اندازی ترد keep-alive
    threading.Thread(target=keep_alive_loop, daemon=True).start()

    while True:
        try:
            url = f"{BASE_URL}/getUpdates"
            params = {"offset": offset, "timeout": 50}
            res = requests_session.get(url, params=params, timeout=65)

            if res.status_code == 200:
                data = res.json()
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        update_id = update["update_id"]
                        if "message" in update:
                            handle_message(update["message"])
                        offset = update_id + 1
                else:
                    if data.get("error_code") == 409:
                        logger.warning("⚠️ Conflict (409) – another instance is running? Retrying...")
                        time.sleep(5)
                    else:
                        logger.warning(f"⚠️ API response not ok: {data}")
                        time.sleep(2)
            else:
                logger.error(f"❌ HTTP error: {res.status_code} - {res.text}")
                time.sleep(5)

        except requests.exceptions.Timeout:
            logger.warning("⏳ Timeout in long polling (normal). Reconnecting...")
            time.sleep(2)
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Connection error: {e} – recreating session...")
            global requests_session
            requests_session = create_session()
            time.sleep(5)
        except Exception as e:
            logger.error(f"❌ Unexpected error in main loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
