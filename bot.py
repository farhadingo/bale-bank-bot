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
from flask import Flask, jsonify
import jdatetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import json
import re

# ============================================
# تنظیمات لاگین
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# متغیرهای محیطی
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 10000))
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "35667900")

if not BOT_TOKEN or not DB_URL:
    logger.error("❌ BOT_TOKEN and DATABASE_URL are required!")
    exit(1)

BASE_URL = f"https://tapi.bale.ai/bot{BOT_TOKEN}"
logger.info(f"✅ Bale API URL: {BASE_URL}")

# ============================================
# اپلیکیشن Flask برای Health Check
# ============================================
flask_app = Flask(__name__)

@flask_app.route('/health')
def health():
    return jsonify({"status": "healthy", "timestamp": time.time()})

@flask_app.route('/')
def root():
    return jsonify({"message": "Bot is running", "status": "active"})

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT)

# ============================================
# Session با Keep-Alive
# ============================================
def create_session():
    session = requests.Session()
    session.headers.update({'Connection': 'keep-alive', 'User-Agent': 'Bale-Bank-Bot/6.0'})
    retry_strategy = Retry(
        total=5, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

requests_session = create_session()

# ============================================
# Connection Pool دیتابیس
# ============================================
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DB_URL)
    logger.info("✅ Database pool created.")
except Exception as e:
    logger.error(f"❌ Pool error: {e}")
    db_pool = None

# ============================================
# State Management
# ============================================
user_states = {}
processed_updates = set()

def get_db_connection():
    if db_pool:
        try:
            return db_pool.getconn()
        except:
            return psycopg2.connect(DB_URL)
    return psycopg2.connect(DB_URL)

def return_db_connection(conn):
    if db_pool:
        try:
            db_pool.putconn(conn)
        except:
            conn.close()
    else:
        conn.close()

# ============================================
# توابع تاریخ (اصلاح شده با jdatetime)
# ============================================
def get_iran_time():
    return datetime.now(timezone(timedelta(hours=3, minutes=30)))

def get_shamsi_date(days_offset=0):
    now = get_iran_time() + timedelta(days=days_offset)
    shamsi = jdatetime.datetime.fromgregorian(datetime=now)
    return f"{shamsi.year}/{shamsi.month:02d}/{shamsi.day:02d}"

def get_shamsi_date_formatted(shamsi_str):
    if not shamsi_str:
        return "نامعلوم"
    parts = shamsi_str.split('/')
    if len(parts) != 3:
        return shamsi_str
    year, month, day = parts
    months = {
        '01':'فروردین','02':'اردیبهشت','03':'خرداد',
        '04':'تیر','05':'مرداد','06':'شهریور',
        '07':'مهر','08':'آبان','09':'آذر',
        '10':'دی','11':'بهمن','12':'اسفند'
    }
    return f"{int(day)} {months.get(month, '')} {year}"

def safe_format(value, default="0"):
    return value if value is not None else default

def parse_shamsi_to_date(shamsi_str):
    parts = shamsi_str.split('/')
    if len(parts) != 3:
        return None
    year, month, day = map(int, parts)
    try:
        return jdatetime.date(year, month, day).togregorian()
    except:
        return None

# ============================================
# توابع مدیریت تعطیلات
# ============================================
def is_holiday(shamsi_date=None):
    """بررسی اینکه تاریخ مشخص شده تعطیل است یا خیر"""
    if not shamsi_date:
        shamsi_date = get_shamsi_date()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM holidays WHERE shamsi_date = %s", (shamsi_date,))
            count = cur.fetchone()[0]
            return count > 0
    except Exception as e:
        logger.error(f"is_holiday error: {e}")
        return False
    finally:
        return_db_connection(conn)

def add_holiday(shamsi_date, description=""):
    """افزودن یک روز تعطیل"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO holidays (shamsi_date, description, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (shamsi_date) DO NOTHING
            """, (shamsi_date, description, get_iran_time()))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"add_holiday error: {e}")
        return False
    finally:
        return_db_connection(conn)

def remove_holiday(shamsi_date):
    """حذف یک روز تعطیل"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM holidays WHERE shamsi_date = %s", (shamsi_date,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"remove_holiday error: {e}")
        return False
    finally:
        return_db_connection(conn)

def get_all_holidays(limit=30):
    """دریافت لیست تعطیلات"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shamsi_date, description, created_at
                FROM holidays
                ORDER BY shamsi_date DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_holidays error: {e}")
        return []
    finally:
        return_db_connection(conn)

# ============================================
# توابع مدیریت وضعیت ربات
# ============================================
def get_bot_status():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = 'bot_status'")
            result = cur.fetchone()
            if result:
                return result[0] == 'active'
            return True
    except Exception as e:
        logger.error(f"get_bot_status: {e}")
        return True
    finally:
        return_db_connection(conn)

def set_bot_status(status):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO settings (key, value, updated_at) 
                VALUES ('bot_status', %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """, ('active' if status else 'inactive', get_iran_time()))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"set_bot_status: {e}")
        return False
    finally:
        return_db_connection(conn)

# ============================================
# ارسال پیام
# ============================================
def send_message(chat_id, text, reply_markup=None, remove_keyboard=False):
    if not get_bot_status() and not is_super_admin_user(chat_id):
        send_maintenance_message(chat_id)
        return None
    
    url = f"{BASE_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if remove_keyboard:
        payload["reply_markup"] = {"remove_keyboard": True}
    elif reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests_session.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            return res.json()
        else:
            logger.error(f"sendMessage failed: {res.status_code}")
            return None
    except Exception as e:
        logger.error(f"sendMessage error: {e}")
        return None

def send_maintenance_message(chat_id):
    msg = "🔧 با عرض پوزش، ربات در حال بروزرسانی می‌باشد.\nلطفاً بعداً مجدداً تلاش کنید."
    url = f"{BASE_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "reply_markup": {"remove_keyboard": True}}
    try:
        requests_session.post(url, json=payload, timeout=10)
    except:
        pass

def is_super_admin_user(chat_id):
    user = find_user_by_telegram_id(chat_id)
    if user:
        return user[7]
    return False

# ============================================
# کیبوردها
# ============================================
def get_deputy_keyboard():
    return {
        "keyboard": [
            [{"text": "💰 ثبت وصولی روزانه"}, {"text": "📊 گزارش وصولی"}],
            [{"text": "📈 مقایسه عملکرد"}, {"text": "📋 مشاهده ثبت امروز"}],
            [{"text": "📅 گزارش تاریخ خاص"}, {"text": "📊 تاریخچه کامل"}],
            [{"text": "📝 ثبت یادداشت"}, {"text": "📋 مشاهده یادداشت‌ها"}],
            [{"text": "🔙 خروج"}, {"text": "❓ راهنما"}]
        ],
        "resize_keyboard": True
    }

def get_admin_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 گزارش امروز"}, {"text": "📈 گزارش ۱۰ روز اخیر"}],
            [{"text": "🏆 رتبه‌بندی شعب"}, {"text": "💹 آمار مفصل امروز"}],
            [{"text": "📉 مقایسه روزانه"}, {"text": "🎯 تحلیل مدیریتی"}],
            [{"text": "📅 گزارش تاریخ خاص"}, {"text": "📊 بهترین/بدترین روز"}],
            [{"text": "📊 گزارش روند شعبه"}, {"text": "📋 عملکرد معاونان"}],
            [{"text": "📝 مشاهده یادداشت‌ها"}, {"text": "🔙 خروج"}],
            [{"text": "❓ راهنما"}]
        ],
        "resize_keyboard": True
    }

def get_super_admin_keyboard():
    return {
        "keyboard": [
            [{"text": "👥 مدیریت کاربران"}, {"text": "📊 مدیریت گزارش‌ها"}],
            [{"text": "📋 مشاهده لاگ‌ها"}, {"text": "📊 گزارش امروز"}],
            [{"text": "📈 گزارش ۱۰ روز اخیر"}, {"text": "🏆 رتبه‌بندی شعب"}],
            [{"text": "💹 آمار مفصل امروز"}, {"text": "🎯 تحلیل مدیریتی"}],
            [{"text": "📅 گزارش تاریخ خاص"}, {"text": "📊 بهترین/بدترین روز"}],
            [{"text": "📊 گزارش روند شعبه"}, {"text": "📋 عملکرد معاونان"}],
            [{"text": "📝 مشاهده یادداشت‌ها"}, {"text": "📋 لاگ ورود/خروج"}],
            [{"text": "🔧 وضعیت ربات"}, {"text": "🔄 ریست گزارش‌ها"}],
            [{"text": "📨 ارسال پیام به معاونین"}, {"text": "📅 مدیریت تعطیلات"}],
            [{"text": "🔙 خروج"}, {"text": "❓ راهنما"}]
        ],
        "resize_keyboard": True
    }

def get_cancel_keyboard():
    return {"keyboard": [[{"text": "🔙 انصراف"}]], "resize_keyboard": True}

# ============================================
# توابع دیتابیس
# ============================================

def find_user_by_employee_number(emp_num):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.employee_number, u.full_name, u.role, u.title, u.branch_id, b.name, u.is_super_admin
                FROM users u
                LEFT JOIN branches b ON u.branch_id = b.id
                WHERE u.employee_number = %s
            """, (emp_num,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"find_user_by_employee_number: {e}")
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
        logger.error(f"update_user_telegram_id: {e}")
    finally:
        return_db_connection(conn)

def find_user_by_telegram_id(chat_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.employee_number, u.full_name, u.role, u.title, u.branch_id, b.name, u.is_super_admin
                FROM users u
                LEFT JOIN branches b ON u.branch_id = b.id
                WHERE u.telegram_id = %s
            """, (chat_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"find_user_by_telegram_id: {e}")
        return None
    finally:
        return_db_connection(conn)

def log_user_activity(user_id, action, details=""):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_activity_log (user_id, action, details, created_at)
                VALUES (%s, %s, %s, %s)
            """, (user_id, action, details, get_iran_time()))
            conn.commit()
    except Exception as e:
        logger.error(f"log_user_activity: {e}")
    finally:
        return_db_connection(conn)

def get_user_activity_log(limit=100):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l.id, u.full_name, u.employee_number, l.action, l.details, l.created_at
                FROM user_activity_log l
                JOIN users u ON l.user_id = u.id
                ORDER BY l.created_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_user_activity_log: {e}")
        return []
    finally:
        return_db_connection(conn)

def save_note(collection_id, user_id, note_text):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO notes (collection_id, user_id, note_text, created_at)
                VALUES (%s, %s, %s, %s)
            """, (collection_id, user_id, note_text, get_iran_time()))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"save_note: {e}")
        return False
    finally:
        return_db_connection(conn)

def get_notes_for_collection(collection_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT n.id, u.full_name, n.note_text, n.created_at
                FROM notes n
                JOIN users u ON n.user_id = u.id
                WHERE n.collection_id = %s
                ORDER BY n.created_at DESC
            """, (collection_id,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_notes_for_collection: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_all_notes_with_collection(limit=50):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT n.id, b.name, c.shamsi_date, u.full_name, n.note_text, n.created_at
                FROM notes n
                JOIN collections c ON n.collection_id = c.id
                JOIN branches b ON c.branch_id = b.id
                JOIN users u ON n.user_id = u.id
                ORDER BY n.created_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_notes_with_collection: {e}")
        return []
    finally:
        return_db_connection(conn)

def save_or_update_collection_with_note(branch_id, deputy_amount_millions, others_amount_millions, shamsi_date, user_id, note_text=None, update_existing=False):
    conn = get_db_connection()
    created_at_iran = get_iran_time()
    deputy_amount = deputy_amount_millions * 1_000_000
    others_amount = others_amount_millions * 1_000_000
    try:
        with conn.cursor() as cur:
            if update_existing:
                cur.execute("""
                    UPDATE collections 
                    SET deputy_amount = %s, others_amount = %s, recorded_by = %s, updated_at = %s
                    WHERE branch_id = %s AND shamsi_date = %s
                    RETURNING id
                """, (deputy_amount, others_amount, user_id, created_at_iran, branch_id, shamsi_date))
                result = cur.fetchone()
                if result:
                    collection_id = result[0]
                else:
                    return False
            else:
                cur.execute("""
                    INSERT INTO collections (branch_id, deputy_amount, others_amount, shamsi_date, recorded_by, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (branch_id, deputy_amount, others_amount, shamsi_date, user_id, created_at_iran))
                result = cur.fetchone()
                collection_id = result[0] if result else None
            if note_text and collection_id:
                cur.execute("""
                    INSERT INTO notes (collection_id, user_id, note_text, created_at)
                    VALUES (%s, %s, %s, %s)
                """, (collection_id, user_id, note_text, created_at_iran))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"save_or_update_collection_with_note: {e}")
        return False
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
        logger.error(f"check_existing_collection: {e}")
        return None
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
        logger.error(f"get_branch_10_day_report: {e}")
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
        logger.error(f"get_today_province_report: {e}")
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
        logger.error(f"get_province_10_day_report: {e}")
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
        logger.error(f"get_top_5_branches: {e}")
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
        logger.error(f"get_today_statistics: {e}")
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
        logger.error(f"get_yesterday_vs_today: {e}")
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
        logger.error(f"get_detailed_report: {e}")
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
        logger.error(f"get_branch_performance: {e}")
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
        logger.error(f"get_daily_comparison: {e}")
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
        logger.error(f"get_deputy_vs_others_ratio: {e}")
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
        logger.error(f"get_report_by_date: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_branch_report_by_date(branch_id, shamsi_date):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT deputy_amount, others_amount, total_amount
                FROM collections
                WHERE branch_id = %s AND shamsi_date = %s
            """, (branch_id, shamsi_date))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"get_branch_report_by_date: {e}")
        return None
    finally:
        return_db_connection(conn)

def get_branch_full_history(branch_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shamsi_date, deputy_amount, others_amount, total_amount
                FROM collections
                WHERE branch_id = %s
                ORDER BY shamsi_date DESC
            """, (branch_id,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_branch_full_history: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_best_worst_days(limit=5):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shamsi_date, SUM(total_amount) as total
                FROM collections
                GROUP BY shamsi_date
                ORDER BY total DESC
                LIMIT %s
            """, (limit,))
            best = cur.fetchall()
            cur.execute("""
                SELECT shamsi_date, SUM(total_amount) as total
                FROM collections
                GROUP BY shamsi_date
                ORDER BY total ASC
                LIMIT %s
            """, (limit,))
            worst = cur.fetchall()
            return best, worst
    except Exception as e:
        logger.error(f"get_best_worst_days: {e}")
        return [], []
    finally:
        return_db_connection(conn)

def get_all_users():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, employee_number, full_name, role, title, branch_id, is_super_admin
                FROM users
                ORDER BY full_name
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_users: {e}")
        return []
    finally:
        return_db_connection(conn)

def update_user_role(user_id, new_role):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"update_user_role: {e}")
        return False
    finally:
        return_db_connection(conn)

def update_user_branch(user_id, branch_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET branch_id = %s WHERE id = %s", (branch_id, user_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"update_user_branch: {e}")
        return False
    finally:
        return_db_connection(conn)

def delete_user(user_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"delete_user: {e}")
        return False
    finally:
        return_db_connection(conn)

def get_all_branches():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM branches ORDER BY name")
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_branches: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_all_collections(limit=100):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, b.name, c.shamsi_date, c.deputy_amount, c.others_amount, c.total_amount, u.full_name
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                JOIN users u ON c.recorded_by = u.id
                ORDER BY c.id DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_collections: {e}")
        return []
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
        logger.error(f"delete_collection: {e}")
        return False
    finally:
        return_db_connection(conn)

def update_collection(collection_id, deputy_amount, others_amount):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE collections 
                SET deputy_amount = %s, others_amount = %s, updated_at = %s
                WHERE id = %s
            """, (deputy_amount, others_amount, get_iran_time(), collection_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"update_collection: {e}")
        return False
    finally:
        return_db_connection(conn)

def reset_all_collections():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM collections")
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"reset_all_collections: {e}")
        return False
    finally:
        return_db_connection(conn)

def get_all_deputies():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.telegram_id, u.full_name, u.branch_id, b.name
                FROM users u
                LEFT JOIN branches b ON u.branch_id = b.id
                WHERE u.role = 'deputy'
                AND u.telegram_id IS NOT NULL
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_deputies: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_log_file_path():
    return "bot.log"

def get_branch_weekly_avg(branch_id, days=7):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT AVG(total_amount) as avg_total
                FROM collections
                WHERE branch_id = %s
                AND shamsi_date >= %s
            """, (branch_id, get_shamsi_date(-days)))
            return cur.fetchone()[0] or 0
    except Exception as e:
        logger.error(f"get_branch_weekly_avg: {e}")
        return 0
    finally:
        return_db_connection(conn)

def get_branch_monthly_avg(branch_id, days=30):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT AVG(total_amount) as avg_total
                FROM collections
                WHERE branch_id = %s
                AND shamsi_date >= %s
            """, (branch_id, get_shamsi_date(-days)))
            return cur.fetchone()[0] or 0
    except Exception as e:
        logger.error(f"get_branch_monthly_avg: {e}")
        return 0
    finally:
        return_db_connection(conn)

def get_today_performance_analysis():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            shamsi_today = get_shamsi_date()
            cur.execute("SELECT SUM(total_amount) FROM collections WHERE shamsi_date = %s", (shamsi_today,))
            today_total = cur.fetchone()[0] or 0
            cur.execute("""
                SELECT b.name, c.total_amount
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                WHERE c.shamsi_date = %s
                ORDER BY c.total_amount DESC
            """, (shamsi_today,))
            branch_data = cur.fetchall()
            cur.execute("""
                SELECT SUM(deputy_amount), SUM(others_amount)
                FROM collections
                WHERE shamsi_date = %s
            """, (shamsi_today,))
            deputy_others = cur.fetchone()
            deputy_total = deputy_others[0] or 0
            others_total = deputy_others[1] or 0
            cur.execute("""
                SELECT COUNT(DISTINCT branch_id) FROM collections WHERE shamsi_date = %s
            """, (shamsi_today,))
            branches_count = cur.fetchone()[0] or 0
            return {
                "today_total": today_total,
                "branch_data": branch_data,
                "deputy_total": deputy_total,
                "others_total": others_total,
                "branches_count": branches_count
            }
    except Exception as e:
        logger.error(f"get_today_performance_analysis: {e}")
        return None
    finally:
        return_db_connection(conn)

def get_drop_alert_branches():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            shamsi_today = get_shamsi_date()
            shamsi_week_ago = get_shamsi_date(-7)
            cur.execute("""
                SELECT 
                    b.id,
                    b.name,
                    c.total_amount as today_amount,
                    COALESCE((
                        SELECT AVG(c2.total_amount) 
                        FROM collections c2 
                        WHERE c2.branch_id = b.id 
                        AND c2.shamsi_date >= %s 
                        AND c2.shamsi_date < %s
                    ), 0) as weekly_avg
                FROM branches b
                LEFT JOIN collections c ON c.branch_id = b.id AND c.shamsi_date = %s
                WHERE c.total_amount IS NOT NULL
            """, (shamsi_week_ago, shamsi_today, shamsi_today))
            results = []
            for row in cur.fetchall():
                branch_id, name, today, weekly_avg = row
                if weekly_avg > 0 and today < (weekly_avg * 0.6):
                    drop_percent = int(((weekly_avg - today) / weekly_avg) * 100)
                    results.append({
                        "branch_id": branch_id,
                        "name": name,
                        "today": today,
                        "weekly_avg": weekly_avg,
                        "drop_percent": drop_percent
                    })
            return results
    except Exception as e:
        logger.error(f"get_drop_alert_branches: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_branch_trend(branch_id, days=3):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shamsi_date, total_amount
                FROM collections
                WHERE branch_id = %s
                ORDER BY shamsi_date DESC
                LIMIT %s
            """, (branch_id, days))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_branch_trend: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_deputy_performance_report(user_id, days=30):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            shamsi_start = get_shamsi_date(-days)
            cur.execute("""
                SELECT 
                    COUNT(*) as total_days,
                    SUM(CASE WHEN EXTRACT(HOUR FROM created_at) < 15 THEN 1 ELSE 0 END) as on_time_days,
                    AVG(total_amount) as avg_amount,
                    MAX(total_amount) as best_day
                FROM collections
                WHERE recorded_by = %s
                AND shamsi_date >= %s
            """, (user_id, shamsi_start))
            result = cur.fetchone()
            if result:
                total_days = result[0] or 0
                on_time = result[1] or 0
                avg = result[2] or 0
                best = result[3] or 0
                late = total_days - on_time
                return {
                    "total_days": total_days,
                    "on_time": on_time,
                    "late": late,
                    "avg_amount": avg,
                    "best_day": best
                }
            return None
    except Exception as e:
        logger.error(f"get_deputy_performance_report: {e}")
        return None
    finally:
        return_db_connection(conn)

def get_unreported_branches():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            shamsi_today = get_shamsi_date()
            cur.execute("""
                SELECT b.id, b.name, u.full_name, u.telegram_id
                FROM branches b
                LEFT JOIN users u ON u.branch_id = b.id AND u.role = 'deputy'
                WHERE NOT EXISTS (
                    SELECT 1 FROM collections c 
                    WHERE c.branch_id = b.id AND c.shamsi_date = %s
                )
            """, (shamsi_today,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_unreported_branches: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_all_admins():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, telegram_id, full_name, role, is_super_admin
                FROM users
                WHERE role IN ('admin', 'super_admin')
                AND telegram_id IS NOT NULL
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_admins: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_branch_monthly_avg_for_name(branch_name):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT AVG(total_amount)
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                WHERE b.name = %s
                AND c.shamsi_date >= %s
            """, (branch_name, get_shamsi_date(-30)))
            result = cur.fetchone()[0]
            return result or 0
    except Exception as e:
        logger.error(f"get_branch_monthly_avg_for_name: {e}")
        return 0
    finally:
        return_db_connection(conn)

def generate_management_analysis(analysis):
    lines = []
    today_total = analysis['today_total']
    branch_data = analysis['branch_data']
    deputy_total = analysis['deputy_total']
    others_total = analysis['others_total']
    branches_count = analysis['branches_count']
    if branch_data and len(branch_data) >= 4:
        top4_sum = sum([amount for _, amount in branch_data[:4]])
        top4_percent = (top4_sum / today_total * 100) if today_total > 0 else 0
        lines.append(f"📊 {top4_percent:.0f}% وصول استان توسط ۴ شعبه انجام شده است.")
    if branch_data:
        top_branch = branch_data[0]
        lines.append(f"🏆 بیشترین سهم وصول امروز مربوط به {top_branch[0]} است.")
    if deputy_total + others_total > 0:
        dep_percent = (deputy_total / (deputy_total + others_total) * 100) if (deputy_total + others_total) > 0 else 0
        if dep_percent > 50:
            lines.append(f"👤 میانگین وصول معاونان ({dep_percent:.0f}%) از همکاران بیشتر بوده است.")
        else:
            lines.append(f"👥 میانگین وصول همکاران ({100-dep_percent:.0f}%) از معاونان بیشتر بوده است.")
    for branch_name, amount in branch_data[:3]:
        monthly_avg = get_branch_monthly_avg_for_name(branch_name)
        if monthly_avg and monthly_avg > 0:
            growth = ((amount - monthly_avg) / monthly_avg) * 100
            if growth > 10:
                lines.append(f"📈 شعبه {branch_name} نسبت به میانگین ماه، {growth:.0f}% رشد داشته است.")
            elif growth < -10:
                lines.append(f"📉 شعبه {branch_name} نسبت به میانگین ماه، {abs(growth):.0f}% کاهش داشته است.")
    if not lines:
        lines.append("📊 داده‌های کافی برای تحلیل مدیریتی وجود ندارد.")
    return "\n".join(lines)

# ============================================
# توابع ارسال خودکار و یادآوری (با پشتیبانی از تعطیلات)
# ============================================

def send_reminder_to_deputy(chat_id, branch_name):
    msg = f"⏰ یادآوری: شما تا ساعت ۱۵ امروز گزارش وصول شعبه {branch_name} را ثبت نکرده‌اید. لطفاً هرچه سریعتر اقدام فرمایید."
    send_message(chat_id, msg)

def send_reminder_to_admin(chat_id, unreported_list):
    if not unreported_list:
        return
    msg = "📋 **شعب ثبت‌نشده امروز**\n━━━━━━━━━━━━━━━━━━\n"
    for branch in unreported_list:
        msg += f"🏢 {branch[1]} (معاون: {branch[2] or 'نامشخص'})\n"
    send_message(chat_id, msg)

def send_daily_report_to_admins():
    """ارسال گزارش پایان روز به مدیران (در صورت عدم تعطیلی)"""
    shamsi_today = get_shamsi_date()
    if is_holiday(shamsi_today):
        logger.info(f"📅 امروز {get_shamsi_date_formatted(shamsi_today)} تعطیل است، گزارش ارسال نشد.")
        return
    if not get_bot_status():
        logger.info("ربات غیرفعال است، گزارش پایان روز ارسال نشد.")
        return
    analysis = get_today_performance_analysis()
    if not analysis:
        return
    admins = get_all_admins()
    if not admins:
        return
    msg = f"📊 **گزارش پایان روز** - {get_shamsi_date_formatted(shamsi_today)}\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"💰 کل وصول استان: {analysis['today_total']//1_000_000:,.0f} میلیون ریال\n"
    msg += f"🏢 تعداد شعب ثبت‌کننده: {analysis['branches_count']}\n"
    msg += f"👤 سهم معاونین: {analysis['deputy_total']//1_000_000:,.0f} میلیون ریال\n"
    msg += f"👥 سهم همکاران: {analysis['others_total']//1_000_000:,.0f} میلیون ریال\n\n"
    if analysis['branch_data']:
        msg += "🏆 **۵ شعبه برتر امروز**\n"
        for i, (name, amount) in enumerate(analysis['branch_data'][:5], 1):
            msg += f"{i}. {name}: {amount//1_000_000:,.0f} میلیون ریال\n"
    msg += "\n📈 **تحلیل مدیریتی**\n"
    msg += generate_management_analysis(analysis)
    for admin in admins:
        admin_id = admin[1]
        if admin_id:
            send_message(admin_id, msg)

def check_and_send_reminders():
    """ارسال یادآوری به شعب ثبت‌نشده (در صورت عدم تعطیلی)"""
    shamsi_today = get_shamsi_date()
    if is_holiday(shamsi_today):
        logger.info(f"📅 امروز {get_shamsi_date_formatted(shamsi_today)} تعطیل است، یادآوری ارسال نشد.")
        return
    if not get_bot_status():
        logger.info("ربات غیرفعال است، یادآوری ارسال نشد.")
        return
    logger.info("🔄 Running reminder check...")
    unreported = get_unreported_branches()
    if unreported:
        for branch in unreported:
            branch_id, name, deputy_name, deputy_chat_id = branch
            if deputy_chat_id:
                send_reminder_to_deputy(deputy_chat_id, name)
        admins = get_all_admins()
        for admin in admins:
            admin_id = admin[1]
            if admin_id:
                send_reminder_to_admin(admin_id, unreported)
        logger.info(f"✅ Reminders sent to {len(unreported)} branches")
    else:
        logger.info("✅ All branches have reported today")

def check_and_send_drop_alerts():
    """ارسال هشدار افت عملکرد (در صورت عدم تعطیلی)"""
    shamsi_today = get_shamsi_date()
    if is_holiday(shamsi_today):
        logger.info(f"📅 امروز {get_shamsi_date_formatted(shamsi_today)} تعطیل است، هشدار افت عملکرد ارسال نشد.")
        return
    if not get_bot_status():
        logger.info("ربات غیرفعال است، هشدار افت عملکرد ارسال نشد.")
        return
    logger.info("🔄 Checking for drop alerts...")
    drops = get_drop_alert_branches()
    if drops:
        admins = get_all_admins()
        for admin in admins:
            admin_id = admin[1]
            if not admin_id:
                continue
            msg = "⚠️ **هشدار افت عملکرد**\n━━━━━━━━━━━━━━━━━━\n"
            for drop in drops:
                msg += f"🏢 شعبه {drop['name']}\n"
                msg += f"   امروز: {drop['today']//1_000_000:,.0f} میلیون ریال\n"
                msg += f"   میانگین هفته: {drop['weekly_avg']//1_000_000:,.0f} میلیون ریال\n"
                msg += f"   📉 افت: {drop['drop_percent']}%\n\n"
            send_message(admin_id, msg)
        logger.info(f"✅ Drop alerts sent for {len(drops)} branches")
    else:
        logger.info("✅ No drop alerts")

# ============================================
# پردازش پیام‌ها
# ============================================
def handle_message(message):
    try:
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()
        if not get_bot_status() and not is_super_admin_user(chat_id):
            send_maintenance_message(chat_id)
            return
        user_state = user_states.get(chat_id, {"state": "LOGGED_OUT"})
        current_state = user_state.get("state", "LOGGED_OUT")
        if current_state == "LOGGED_OUT" or current_state == "WAITING_FOR_EMP_NUM":
            if current_state != "WAITING_FOR_EMP_NUM":
                user_states[chat_id] = {"state": "WAITING_FOR_EMP_NUM"}
                send_message(chat_id, "👋 سلام! به ربات وصول مطالبات استان زنجان خوش آمدید.\n\n🔐 لطفاً شماره کارمندی خود را ارسال کنید:", remove_keyboard=True)
                return
            if not re.match(r'^[0-9]+$', text):
                send_message(chat_id, "❌ لطفاً شماره کارمندی را فقط با **اعداد انگلیسی** وارد کنید.\nمثال: ۱۲۳۴۵۶")
                return
            emp_user = find_user_by_employee_number(text)
            if emp_user:
                db_id, emp_num, name, role, title, branch_id, branch_name, is_super_admin = emp_user
                if is_super_admin:
                    user_states[chat_id] = {
                        "state": "WAITING_FOR_SUPER_ADMIN_PASSWORD",
                        "temp_user_data": {
                            "db_id": db_id,
                            "emp_num": emp_num,
                            "name": name,
                            "role": role,
                            "title": title,
                            "branch_id": branch_id,
                            "branch_name": branch_name,
                            "is_super_admin": is_super_admin
                        }
                    }
                    send_message(chat_id, "🔐 شما یک کاربر سوپرادمین هستید. لطفاً رمز عبور خود را وارد کنید:", remove_keyboard=True)
                    return
                else:
                    update_user_telegram_id(db_id, chat_id)
                    log_user_activity(db_id, "login", f"ورود از chat_id: {chat_id}")
                    user_states[chat_id] = {
                        "state": "LOGGED_IN",
                        "user_data": {
                            "db_id": db_id,
                            "emp_num": emp_num,
                            "name": name,
                            "role": role,
                            "title": title,
                            "branch_id": branch_id,
                            "branch_name": branch_name,
                            "is_super_admin": is_super_admin
                        }
                    }
                    welcome_msg = (
                        f"✅ هویت شما تایید شد.\n\n"
                        f"👤 {name}\n"
                        f"🏢 {title}\n"
                        f"🏭 واحد: {branch_name or 'ستاد استان'}\n"
                        f"🔑 شماره کارمندی: {emp_num}\n"
                        f"⏰ زمان ورود: {get_shamsi_date_formatted(get_shamsi_date())} {get_iran_time().strftime('%H:%M:%S')}\n\n"
                        f"خوش آمدید! 👋\n\n"
                        f"📌 **راهنمای ثبت مبلغ:**\n"
                        f"مبالغ را به **میلیون ریال** وارد کنید.\n"
                        f"مثال: ۷۵۷ = ۷۵۷ میلیون ریال"
                    )
                    keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                    send_message(chat_id, welcome_msg, keyboard)
            else:
                send_message(chat_id, "❌ شماره کارمندی در سیستم یافت نشد.\nلطفاً شماره کارمندی صحیح خود را بفرستید.")
            return
        if current_state == "WAITING_FOR_SUPER_ADMIN_PASSWORD":
            if text == SUPER_ADMIN_PASSWORD:
                temp_data = user_state.get("temp_user_data")
                if temp_data:
                    db_id = temp_data["db_id"]
                    update_user_telegram_id(db_id, chat_id)
                    log_user_activity(db_id, "login", "ورود سوپرادمین")
                    user_states[chat_id] = {
                        "state": "LOGGED_IN",
                        "user_data": temp_data
                    }
                    welcome_msg = (
                        f"✅ هویت سوپرادمین تایید شد.\n\n"
                        f"👤 {temp_data['name']}\n"
                        f"🏢 {temp_data['title']}\n"
                        f"🏭 واحد: {temp_data['branch_name'] or 'ستاد استان'}\n"
                        f"🔑 شماره کارمندی: {temp_data['emp_num']}\n"
                        f"⏰ زمان ورود: {get_shamsi_date_formatted(get_shamsi_date())} {get_iran_time().strftime('%H:%M:%S')}\n\n"
                        f"شما دسترسی کامل مدیریتی دارید."
                    )
                    send_message(chat_id, welcome_msg, get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ خطا در احراز هویت. لطفاً دوباره شماره کارمندی را وارد کنید.")
                    user_states[chat_id] = {"state": "LOGGED_OUT"}
            else:
                send_message(chat_id, "❌ رمز عبور اشتباه است. لطفاً دوباره تلاش کنید.")
            return
        user_data = user_state.get("user_data", {})
        if not user_data:
            user = find_user_by_telegram_id(chat_id)
            if not user:
                user_states[chat_id] = {"state": "LOGGED_OUT"}
                send_message(chat_id, "⚠️ نشست شما منقضی شده است. لطفاً شماره کارمندی خود را وارد کنید.", remove_keyboard=True)
                return
            db_id, emp_num, name, role, title, branch_id, branch_name, is_super_admin = user
            user_data = {
                "db_id": db_id,
                "emp_num": emp_num,
                "name": name,
                "role": role,
                "title": title,
                "branch_id": branch_id,
                "branch_name": branch_name,
                "is_super_admin": is_super_admin
            }
            user_states[chat_id]["user_data"] = user_data
        role = user_data["role"]
        branch_id = user_data["branch_id"]
        branch_name = user_data["branch_name"]
        user_db_id = user_data["db_id"]
        is_super_admin = user_data.get("is_super_admin", False)

        # ===== مدیریت وضعیت‌های ورودی (ثبت مبلغ و یادداشت) =====
        if current_state == "WAITING_FOR_DEPUTY_AMOUNT":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", keyboard)
                return
            try:
                amount = int(text.replace(',', '').replace('،', ''))
                if amount < 0:
                    raise ValueError
                user_states[chat_id]["state"] = "WAITING_FOR_OTHERS_AMOUNT"
                user_states[chat_id]["deputy_amount"] = amount
                user_states[chat_id]["edit_mode"] = user_state.get("edit_mode", False)
                send_message(chat_id, "✏️ اکنون میزان وصولی سایر همکاران شعبه را به **میلیون ریال** وارد کنید:", get_cancel_keyboard())
            except ValueError:
                send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت (میلیون ریال) وارد کنید.")
            return
        elif current_state == "WAITING_FOR_OTHERS_AMOUNT":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
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
                user_states[chat_id]["state"] = "WAITING_FOR_NOTE"
                user_states[chat_id]["collection_data"] = {
                    "deputy_amount": deputy_amount,
                    "others_amount": others_amount,
                    "shamsi_date": shamsi_today,
                    "is_edit": is_edit
                }
                send_message(chat_id, "📝 آیا می‌خواهید یادداشتی برای این وصول ثبت کنید؟ (اختیاری)\nلطفاً متن یادداشت را ارسال کنید یا روی «🔙 انصراف» بزنید تا بدون یادداشت ذخیره شود.", get_cancel_keyboard())
            except ValueError:
                send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت (میلیون ریال) وارد کنید.")
            return
        elif current_state == "WAITING_FOR_NOTE":
            if text == "🔙 انصراف":
                data = user_state.get("collection_data", {})
                success = save_or_update_collection_with_note(
                    branch_id=branch_id,
                    deputy_amount_millions=data.get("deputy_amount", 0),
                    others_amount_millions=data.get("others_amount", 0),
                    shamsi_date=data.get("shamsi_date", get_shamsi_date()),
                    user_id=user_db_id,
                    note_text=None,
                    update_existing=data.get("is_edit", False)
                )
                user_states[chat_id]["state"] = "LOGGED_IN"
                if success:
                    total = data.get("deputy_amount", 0) + data.get("others_amount", 0)
                    msg = f"✅ ثبت شد.\n💰 جمع کل: {total:,.0f} میلیون ریال"
                    log_user_activity(user_db_id, "collection_add", f"ثبت وصول شعبه {branch_name} - مبلغ: {total} میلیون ریال")
                else:
                    msg = "❌ خطا در ثبت اطلاعات."
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                send_message(chat_id, msg, keyboard)
                return
            else:
                data = user_state.get("collection_data", {})
                note_text = text
                success = save_or_update_collection_with_note(
                    branch_id=branch_id,
                    deputy_amount_millions=data.get("deputy_amount", 0),
                    others_amount_millions=data.get("others_amount", 0),
                    shamsi_date=data.get("shamsi_date", get_shamsi_date()),
                    user_id=user_db_id,
                    note_text=note_text,
                    update_existing=data.get("is_edit", False)
                )
                user_states[chat_id]["state"] = "LOGGED_IN"
                if success:
                    total = data.get("deputy_amount", 0) + data.get("others_amount", 0)
                    msg = f"✅ ثبت شد.\n💰 جمع کل: {total:,.0f} میلیون ریال\n📝 یادداشت: {note_text}"
                    log_user_activity(user_db_id, "collection_add_with_note", f"ثبت وصول با یادداشت برای شعبه {branch_name}")
                else:
                    msg = "❌ خطا در ثبت اطلاعات."
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                send_message(chat_id, msg, keyboard)
                return
        elif current_state == "WAITING_FOR_EDIT_CONFIRMATION":
            if text == "📝 بله، ویرایش شود":
                user_states[chat_id]["state"] = "WAITING_FOR_DEPUTY_AMOUNT"
                user_states[chat_id]["edit_mode"] = True
                send_message(chat_id, "✏️ لطفاً مبلغ جدید وصولی خود (معاون) را به **میلیون ریال** وارد کنید:", get_cancel_keyboard())
            else:
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", keyboard)
            return

        # ===== گزارش تاریخ خاص برای معاونین =====
        if role == 'deputy' and current_state == "WAITING_FOR_BRANCH_DATE":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                send_message(chat_id, "❌ عملیات لغو شد.", get_deputy_keyboard())
                return
            parts = text.split('/')
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                shamsi_date = text
                record = get_branch_report_by_date(branch_id, shamsi_date)
                if record:
                    dep, oth, total = record
                    msg = (
                        f"📋 گزارش شعبه {branch_name} برای تاریخ {get_shamsi_date_formatted(shamsi_date)}\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"👤 وصولی معاون: {dep//1_000_000:,.0f} میلیون ریال\n"
                        f"👥 وصولی همکاران: {oth//1_000_000:,.0f} میلیون ریال\n"
                        f"💰 جمع کل: {total//1_000_000:,.0f} میلیون ریال"
                    )
                    col_id = check_existing_collection(branch_id, shamsi_date)
                    if col_id:
                        notes = get_notes_for_collection(col_id[0])
                        if notes:
                            msg += "\n\n📝 **یادداشت‌ها:**\n"
                            for n in notes:
                                msg += f"• {n[1]}: {n[2]} ({n[3].strftime('%H:%M')})\n"
                    send_message(chat_id, msg, get_deputy_keyboard())
                else:
                    send_message(chat_id, f"📭 هیچ داده‌ای برای تاریخ {shamsi_date} یافت نشد.", get_deputy_keyboard())
            else:
                send_message(chat_id, "❌ فرمت تاریخ را به صورت YYYY/MM/DD وارد کنید (مثلاً 1403/01/15).")
                return
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ===== گزارش تاریخ خاص برای ادمین =====
        if role == 'admin' and current_state == "WAITING_FOR_ADMIN_DATE":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                send_message(chat_id, "❌ عملیات لغو شد.", get_admin_keyboard())
                return
            parts = text.split('/')
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                shamsi_date = text
                report = get_report_by_date(shamsi_date)
                if report:
                    msg = f"📅 گزارش استان برای تاریخ {get_shamsi_date_formatted(shamsi_date)}\n━━━━━━━━━━━━━━━━━━\n\n"
                    total_all = 0
                    for idx, row in enumerate(report, 1):
                        dep = int(safe_format(row[1]))
                        oth = int(safe_format(row[2]))
                        tot = int(safe_format(row[3]))
                        msg += f"{idx}. 🏢 {row[0]}\n"
                        msg += f"   👤 معاون ({row[4]}): {dep//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   👥 همکاران: {oth//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   💰 جمع: {tot//1_000_000:,.0f} میلیون ریال\n\n"
                        total_all += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n💰 جمع کل استان: {total_all//1_000_000:,.0f} میلیون ریال"
                    send_message(chat_id, msg, get_admin_keyboard())
                else:
                    send_message(chat_id, f"📭 هیچ داده‌ای برای تاریخ {shamsi_date} یافت نشد.", get_admin_keyboard())
            else:
                send_message(chat_id, "❌ فرمت تاریخ را به صورت YYYY/MM/DD وارد کنید.")
                return
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ===== خروج =====
        if text == "🔙 خروج":
            log_user_activity(user_db_id, "logout", "خروج از سیستم")
            user_states[chat_id] = {"state": "LOGGED_OUT"}
            send_message(chat_id, "👋 شما از سیستم خارج شدید.\n\nبرای ورود مجدد، شماره کارمندی خود را ارسال کنید.", remove_keyboard=True)
            return

        # ===== راهنما =====
        if text == "❓ راهنما":
            help_text = (
                "📌 **راهنمای ربات وصول مطالبات**\n\n"
                "🔹 **معاونین شعب:**\n"
                "   • ثبت وصولی روزانه (با قابلیت ویرایش و یادداشت)\n"
                "   • مشاهده گزارش ۱۰ روز اخیر شعبه\n"
                "   • مقایسه عملکرد روزانه شعبه\n"
                "   • مشاهده ثبت امروز\n"
                "   • گزارش یک تاریخ خاص برای شعبه خود\n"
                "   • مشاهده تاریخچه کامل شعبه\n"
                "   • ثبت و مشاهده یادداشت‌ها\n\n"
                "🔹 **کاربران ارشد (ادمین):**\n"
                "   • گزارش امروز (همه شعب)\n"
                "   • گزارش ۱۰ روز اخیر استان\n"
                "   • رتبه‌بندی شعب برتر\n"
                "   • آمار مفصل امروز\n"
                "   • مقایسه روزانه ۷ روز اخیر\n"
                "   • تحلیل مدیریتی (تحلیل هوشمند داده‌ها)\n"
                "   • گزارش تاریخ خاص برای کل استان\n"
                "   • نمایش بهترین/بدترین روزهای استان\n"
                "   • گزارش روند هر شعبه\n"
                "   • گزارش عملکرد معاونان\n"
                "   • مشاهده یادداشت‌ها\n\n"
                "🔹 **سوپرادمین:**\n"
                "   • مدیریت کاربران و گزارش‌ها\n"
                "   • فعال/غیرفعال کردن ربات\n"
                "   • ریست کردن گزارش‌ها\n"
                "   • ارسال پیام به معاونین (انتخابی)\n"
                "   • مدیریت تعطیلات (افزودن/حذف)\n"
                "   • مشاهده لاگ کامل فعالیت‌ها\n\n"
                "💰 **واحد پول:** تمام مبالغ به **میلیون ریال** است.\n"
                "🔸 در هر مرحله می‌توانید با دکمه «انصراف» به منو برگردید.\n"
                "🔸 برای خروج کامل، گزینه «خروج» را انتخاب کنید."
            )
            keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
            if is_super_admin:
                keyboard = get_super_admin_keyboard()
            send_message(chat_id, help_text, keyboard)
            return

        # ========================================
        # بخش سوپرادمین
        # ========================================
        if is_super_admin:
            if text == "👥 مدیریت کاربران":
                users = get_all_users()
                if users:
                    msg = "📋 **لیست کاربران**\n━━━━━━━━━━━━━━━━━━\n"
                    for u in users:
                        msg += f"🆔 {u[0]} | {u[1]} | {u[2]} | نقش: {u[3]} | شعبه: {u[5]}\n"
                    msg += "\nبرای مدیریت، از گزینه‌های زیر استفاده کنید:\n"
                    msg += "▪️ /edit_role [user_id] [admin|deputy|super_admin]\n"
                    msg += "▪️ /edit_branch [user_id] [branch_id]\n"
                    msg += "▪️ /delete_user [user_id]"
                    send_message(chat_id, msg, get_super_admin_keyboard())
                else:
                    send_message(chat_id, "هیچ کاربری یافت نشد.", get_super_admin_keyboard())
                return

            if text == "📊 مدیریت گزارش‌ها":
                collections = get_all_collections(20)
                if collections:
                    msg = "📊 **۲۰ گزارش اخیر**\n━━━━━━━━━━━━━━━━━━\n"
                    for c in collections:
                        msg += f"🆔 {c[0]} | {c[1]} | {c[2]} | {c[5]//1_000_000:,.0f} میلیون ریال | ثبت: {c[6]}\n"
                    msg += "\nبرای حذف: /delete_collection [id]\n"
                    msg += "برای ویرایش: /edit_collection [id] [deputy_amount] [others_amount] (به میلیون ریال)"
                    send_message(chat_id, msg, get_super_admin_keyboard())
                else:
                    send_message(chat_id, "هیچ گزارشی یافت نشد.", get_super_admin_keyboard())
                return

            if text == "📋 مشاهده لاگ‌ها":
                log_file = get_log_file_path()
                if os.path.exists(log_file):
                    try:
                        with open(log_file, 'r', encoding='utf-8') as f:
                            lines = f.readlines()[-50:]
                            log_text = "".join(lines)
                            if len(log_text) > 4000:
                                log_text = log_text[-4000:]
                            send_message(chat_id, f"📋 **آخرین لاگ‌ها**\n```\n{log_text}\n```", get_super_admin_keyboard())
                    except Exception as e:
                        send_message(chat_id, f"❌ خطا در خواندن لاگ: {e}", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "فایل لاگ وجود ندارد.", get_super_admin_keyboard())
                return

            if text == "📋 لاگ ورود/خروج":
                logs = get_user_activity_log(50)
                if logs:
                    msg = "📋 **لاگ فعالیت کاربران**\n━━━━━━━━━━━━━━━━━━\n"
                    for log in logs:
                        created_at = log[5]
                        shamsi_dt = jdatetime.datetime.fromgregorian(datetime=created_at)
                        shamsi_str = f"{shamsi_dt.year}/{shamsi_dt.month:02d}/{shamsi_dt.day:02d} {shamsi_dt.hour:02d}:{shamsi_dt.minute:02d}"
                        msg += f"👤 {log[1]} ({log[2]}) | {log[3]}\n"
                        msg += f"📝 {log[4]}\n"
                        msg += f"⏰ {shamsi_str}\n\n"
                    send_message(chat_id, msg, get_super_admin_keyboard())
                else:
                    send_message(chat_id, "هیچ فعالیتی ثبت نشده است.", get_super_admin_keyboard())
                return

            if text == "📝 مشاهده یادداشت‌ها":
                notes = get_all_notes_with_collection(30)
                if notes:
                    msg = "📝 **یادداشت‌های اخیر**\n━━━━━━━━━━━━━━━━━━\n"
                    for note in notes:
                        msg += f"🏢 {note[1]} | 📅 {note[2]}\n"
                        msg += f"👤 {note[3]}: {note[4]}\n"
                        msg += f"⏰ {note[5].strftime('%H:%M')}\n\n"
                    send_message(chat_id, msg, get_super_admin_keyboard())
                else:
                    send_message(chat_id, "هیچ یادداشتی وجود ندارد.", get_super_admin_keyboard())
                return

            if text == "🔧 وضعیت ربات":
                current_status = get_bot_status()
                status_text = "فعال ✅" if current_status else "غیرفعال ❌"
                keyboard = {
                    "keyboard": [
                        [{"text": "🔛 فعال کردن ربات" if not current_status else "🔛 فعال است"}],
                        [{"text": "🔴 غیرفعال کردن ربات" if current_status else "🔴 غیرفعال است"}],
                        [{"text": "🔙 انصراف"}]
                    ],
                    "resize_keyboard": True
                }
                send_message(chat_id, f"📊 **وضعیت فعلی ربات:** {status_text}", keyboard)
                return

            if text == "🔛 فعال کردن ربات":
                if set_bot_status(True):
                    send_message(chat_id, "✅ ربات با موفقیت **فعال** شد.", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ خطا در فعال‌سازی ربات.", get_super_admin_keyboard())
                return

            if text == "🔴 غیرفعال کردن ربات":
                if set_bot_status(False):
                    send_message(chat_id, "✅ ربات با موفقیت **غیرفعال** شد.", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ خطا در غیرفعال‌سازی ربات.", get_super_admin_keyboard())
                return

            if text == "🔄 ریست گزارش‌ها":
                keyboard = {
                    "keyboard": [
                        [{"text": "✅ بله، ریست کن"}, {"text": "❌ خیر، لغو"}]
                    ],
                    "resize_keyboard": True
                }
                send_message(chat_id, "⚠️ **هشدار!**\nآیا از ریست کردن تمام گزارش‌ها اطمینان دارید؟\nاین عمل غیرقابل بازگشت است.", keyboard)
                user_states[chat_id]["state"] = "WAITING_FOR_RESET_CONFIRM"
                return

            if current_state == "WAITING_FOR_RESET_CONFIRM":
                if text == "✅ بله، ریست کن":
                    if reset_all_collections():
                        send_message(chat_id, "✅ تمام گزارش‌ها با موفقیت ریست شدند.", get_super_admin_keyboard())
                        log_user_activity(user_db_id, "reset_reports", "ریست کامل گزارش‌ها")
                    else:
                        send_message(chat_id, "❌ خطا در ریست گزارش‌ها.", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ عملیات ریست لغو شد.", get_super_admin_keyboard())
                user_states[chat_id]["state"] = "LOGGED_IN"
                return

            # ===== بخش مدیریت تعطیلات =====
            if text == "📅 مدیریت تعطیلات":
                keyboard = {
                    "keyboard": [
                        [{"text": "➕ افزودن روز تعطیل"}, {"text": "➖ حذف روز تعطیل"}],
                        [{"text": "📋 مشاهده تعطیلات"}, {"text": "🔙 انصراف"}]
                    ],
                    "resize_keyboard": True
                }
                send_message(chat_id, "📅 **مدیریت تعطیلات**\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:", keyboard)
                return

            if text == "➕ افزودن روز تعطیل":
                user_states[chat_id]["state"] = "WAITING_FOR_ADD_HOLIDAY"
                send_message(chat_id, "📅 لطفاً تاریخ مورد نظر را به فرمت **YYYY/MM/DD** وارد کنید (مثلاً ۱۴۰۴/۰۱/۱۵)\nو در صورت تمایل توضیحی وارد کنید:\n\n`تاریخ | توضیح`\nمثال: `۱۴۰۴/۰۱/۱۵ | تعطیلات رسمی`", get_cancel_keyboard())
                return

            if current_state == "WAITING_FOR_ADD_HOLIDAY":
                if text == "🔙 انصراف":
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                    return
                parts = text.split('|')
                shamsi_date = parts[0].strip()
                description = parts[1].strip() if len(parts) > 1 else "تعطیل"
                if re.match(r'^\d{4}/\d{2}/\d{2}$', shamsi_date):
                    if add_holiday(shamsi_date, description):
                        send_message(chat_id, f"✅ روز {get_shamsi_date_formatted(shamsi_date)} با موفقیت به عنوان تعطیل ثبت شد.\nتوضیح: {description}", get_super_admin_keyboard())
                        log_user_activity(user_db_id, "add_holiday", f"افزودن تعطیل: {shamsi_date} - {description}")
                    else:
                        send_message(chat_id, "❌ خطا در ثبت تعطیل. این تاریخ قبلاً ثبت شده است.", get_cancel_keyboard())
                        return
                else:
                    send_message(chat_id, "❌ فرمت تاریخ نامعتبر. لطفاً به صورت YYYY/MM/DD وارد کنید.", get_cancel_keyboard())
                    return
                user_states[chat_id]["state"] = "LOGGED_IN"
                return

            if text == "➖ حذف روز تعطیل":
                holidays = get_all_holidays(20)
                if not holidays:
                    send_message(chat_id, "📭 هیچ روز تعطیلی ثبت نشده است.", get_super_admin_keyboard())
                    return
                msg = "📋 **لیست تعطیلات ثبت‌شده**\n━━━━━━━━━━━━━━━━━━\n"
                for i, h in enumerate(holidays, 1):
                    msg += f"{i}. {get_shamsi_date_formatted(h[0])} - {h[1]}\n"
                msg += "\nلطفاً شماره مورد نظر برای حذف را وارد کنید، یا 🔙 انصراف بزنید."
                user_states[chat_id]["state"] = "WAITING_FOR_REMOVE_HOLIDAY"
                user_states[chat_id]["holidays_list"] = holidays
                send_message(chat_id, msg, get_cancel_keyboard())
                return

            if current_state == "WAITING_FOR_REMOVE_HOLIDAY":
                if text == "🔙 انصراف":
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                    return
                try:
                    index = int(text) - 1
                    holidays = user_state.get("holidays_list", [])
                    if 0 <= index < len(holidays):
                        shamsi_date = holidays[index][0]
                        if remove_holiday(shamsi_date):
                            send_message(chat_id, f"✅ روز {get_shamsi_date_formatted(shamsi_date)} از تعطیلات حذف شد.", get_super_admin_keyboard())
                            log_user_activity(user_db_id, "remove_holiday", f"حذف تعطیل: {shamsi_date}")
                        else:
                            send_message(chat_id, "❌ خطا در حذف تعطیل.", get_super_admin_keyboard())
                    else:
                        send_message(chat_id, "❌ شماره نامعتبر.", get_cancel_keyboard())
                        return
                except:
                    send_message(chat_id, "❌ لطفاً یک عدد معتبر وارد کنید.", get_cancel_keyboard())
                    return
                user_states[chat_id]["state"] = "LOGGED_IN"
                return

            if text == "📋 مشاهده تعطیلات":
                holidays = get_all_holidays(30)
                if holidays:
                    msg = "📋 **تعطیلات ثبت‌شده**\n━━━━━━━━━━━━━━━━━━\n"
                    for h in holidays:
                        msg += f"📅 {get_shamsi_date_formatted(h[0])} - {h[1]}\n"
                    send_message(chat_id, msg, get_super_admin_keyboard())
                else:
                    send_message(chat_id, "📭 هیچ روز تعطیلی ثبت نشده است.", get_super_admin_keyboard())
                return

            # ===== ارسال پیام به معاونین (نسخه انتخاب‌گر) =====
            if text == "📨 ارسال پیام به معاونین":
                deputies = get_all_deputies()
                if not deputies:
                    send_message(chat_id, "هیچ معاونی یافت نشد.", get_super_admin_keyboard())
                    return
                msg = "📨 **ارسال پیام به معاونین**\n\n"
                msg += "لیست معاونین:\n"
                for i, dep in enumerate(deputies, 1):
                    msg += f"{i}. {dep[2]} - {dep[4] or 'بدون شعبه'}\n"
                msg += "\nبرای انتخاب مخاطب، یکی از گزینه‌های زیر را وارد کنید:\n"
                msg += "▪️ `همه` برای ارسال به همه\n"
                msg += "▪️ شماره ردیف (مثلاً `1` یا `1,2,3`)\n"
                msg += "▪️ نام معاون (مثلاً `علی محمدی`)\n"
                msg += "سپس پیام خود را ارسال کنید."
                user_states[chat_id] = {
                    "state": "WAITING_FOR_MESSAGE_RECIPIENT",
                    "deputies": deputies
                }
                send_message(chat_id, msg, get_cancel_keyboard())
                return

            if current_state == "WAITING_FOR_MESSAGE_RECIPIENT":
                if text == "🔙 انصراف":
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                    return
                deputies = user_state.get("deputies", [])
                if not deputies:
                    send_message(chat_id, "خطا در دریافت لیست معاونین.", get_super_admin_keyboard())
                    return
                recipients = []
                if text == "همه":
                    recipients = deputies
                elif text.isdigit():
                    idx = int(text)
                    if 1 <= idx <= len(deputies):
                        recipients = [deputies[idx-1]]
                    else:
                        send_message(chat_id, "❌ شماره نامعتبر.", get_cancel_keyboard())
                        return
                elif ',' in text:
                    indices = [int(x.strip()) for x in text.split(',') if x.strip().isdigit()]
                    for idx in indices:
                        if 1 <= idx <= len(deputies):
                            recipients.append(deputies[idx-1])
                    if not recipients:
                        send_message(chat_id, "❌ هیچ شماره معتبری یافت نشد.", get_cancel_keyboard())
                        return
                else:
                    for dep in deputies:
                        if text in dep[2]:
                            recipients.append(dep)
                    if not recipients:
                        send_message(chat_id, f"❌ معاونی با نام '{text}' یافت نشد.", get_cancel_keyboard())
                        return
                if not recipients:
                    send_message(chat_id, "❌ هیچ مخاطبی انتخاب نشد.", get_cancel_keyboard())
                    return
                user_states[chat_id]["state"] = "WAITING_FOR_MESSAGE_TEXT"
                user_states[chat_id]["recipients"] = recipients
                recipient_names = ", ".join([f"{r[2]} ({r[4] or 'بدون شعبه'})" for r in recipients])
                send_message(chat_id, f"📨 مخاطبین انتخاب شدند:\n{recipient_names}\n\n✏️ حالا متن پیام خود را بنویسید:", get_cancel_keyboard())
                return

            if current_state == "WAITING_FOR_MESSAGE_TEXT":
                if text == "🔙 انصراف":
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                    return
                recipients = user_state.get("recipients", [])
                if not recipients:
                    send_message(chat_id, "خطا: مخاطبی انتخاب نشده است.", get_super_admin_keyboard())
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    return
                message_text = text
                success_count = 0
                for dep in recipients:
                    dep_id, dep_chat_id, dep_name, branch_id, branch_name = dep
                    if dep_chat_id:
                        msg = f"📨 **پیام از سوی مدیریت**\n━━━━━━━━━━━━━━━━━━\n\n{message_text}"
                        if send_message(dep_chat_id, msg):
                            success_count += 1
                            log_user_activity(user_db_id, "send_message_to_deputy", f"ارسال پیام به {dep_name} (ID: {dep_id})")
                        else:
                            logger.error(f"Failed to send message to {dep_name} (chat_id: {dep_chat_id})")
                    else:
                        logger.warning(f"Deputy {dep_name} has no chat_id")
                final_msg = f"✅ پیام به {success_count} از {len(recipients)} مخاطب ارسال شد."
                if success_count < len(recipients):
                    final_msg += f"\n⚠️ {len(recipients) - success_count} مخاطب پیام را دریافت نکردند (احتمالاً ربات را استارت نکرده‌اند)."
                send_message(chat_id, final_msg, get_super_admin_keyboard())
                log_user_activity(user_db_id, "send_message_to_deputies", f"ارسال پیام به {success_count} معاون")
                user_states[chat_id]["state"] = "LOGGED_IN"
                return

            # دستورات متنی سوپرادمین
            if text.startswith("/edit_role"):
                parts = text.split()
                if len(parts) == 3:
                    try:
                        user_id = int(parts[1])
                        new_role = parts[2]
                        if new_role in ['admin', 'deputy', 'super_admin']:
                            if update_user_role(user_id, new_role):
                                send_message(chat_id, f"✅ نقش کاربر {user_id} به {new_role} تغییر یافت.", get_super_admin_keyboard())
                            else:
                                send_message(chat_id, "❌ خطا در تغییر نقش.", get_super_admin_keyboard())
                        else:
                            send_message(chat_id, "❌ نقش نامعتبر. فقط admin, deputy یا super_admin مجاز است.", get_super_admin_keyboard())
                    except:
                        send_message(chat_id, "❌ فرمت: /edit_role [user_id] [role]", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ فرمت: /edit_role [user_id] [role]", get_super_admin_keyboard())
                return

            if text.startswith("/edit_branch"):
                parts = text.split()
                if len(parts) == 3:
                    try:
                        user_id = int(parts[1])
                        branch_id = int(parts[2])
                        if update_user_branch(user_id, branch_id):
                            send_message(chat_id, f"✅ شعبه کاربر {user_id} به {branch_id} تغییر یافت.", get_super_admin_keyboard())
                        else:
                            send_message(chat_id, "❌ خطا در تغییر شعبه.", get_super_admin_keyboard())
                    except:
                        send_message(chat_id, "❌ فرمت: /edit_branch [user_id] [branch_id]", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ فرمت: /edit_branch [user_id] [branch_id]", get_super_admin_keyboard())
                return

            if text.startswith("/delete_user"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        user_id = int(parts[1])
                        if delete_user(user_id):
                            send_message(chat_id, f"✅ کاربر {user_id} حذف شد.", get_super_admin_keyboard())
                        else:
                            send_message(chat_id, "❌ خطا در حذف کاربر.", get_super_admin_keyboard())
                    except:
                        send_message(chat_id, "❌ فرمت: /delete_user [user_id]", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ فرمت: /delete_user [user_id]", get_super_admin_keyboard())
                return

            if text.startswith("/delete_collection"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        col_id = int(parts[1])
                        if delete_collection(col_id):
                            send_message(chat_id, f"✅ گزارش {col_id} حذف شد.", get_super_admin_keyboard())
                        else:
                            send_message(chat_id, "❌ خطا در حذف گزارش.", get_super_admin_keyboard())
                    except:
                        send_message(chat_id, "❌ فرمت: /delete_collection [id]", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ فرمت: /delete_collection [id]", get_super_admin_keyboard())
                return

            if text.startswith("/edit_collection"):
                parts = text.split()
                if len(parts) == 4:
                    try:
                        col_id = int(parts[1])
                        deputy = int(parts[2]) * 1_000_000
                        others = int(parts[3]) * 1_000_000
                        if update_collection(col_id, deputy, others):
                            send_message(chat_id, f"✅ گزارش {col_id} به‌روزرسانی شد.", get_super_admin_keyboard())
                        else:
                            send_message(chat_id, "❌ خطا در ویرایش گزارش.", get_super_admin_keyboard())
                    except:
                        send_message(chat_id, "❌ فرمت: /edit_collection [id] [deputy_amount_millions] [others_amount_millions]", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ فرمت: /edit_collection [id] [deputy_amount_millions] [others_amount_millions]", get_super_admin_keyboard())
                return

            if text not in ["👥 مدیریت کاربران", "📊 مدیریت گزارش‌ها", "📋 مشاهده لاگ‌ها", "📋 لاگ ورود/خروج", "📝 مشاهده یادداشت‌ها", "📊 گزارش امروز", "📈 گزارش ۱۰ روز اخیر", "🏆 رتبه‌بندی شعب", "💹 آمار مفصل امروز", "🎯 تحلیل مدیریتی", "📅 گزارش تاریخ خاص", "📊 بهترین/بدترین روز", "📊 گزارش روند شعبه", "📋 عملکرد معاونان", "🔧 وضعیت ربات", "🔄 ریست گزارش‌ها", "📨 ارسال پیام به معاونین", "📅 مدیریت تعطیلات", "🔙 خروج", "❓ راهنما", "➕ افزودن روز تعطیل", "➖ حذف روز تعطیل", "📋 مشاهده تعطیلات"]:
                send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", get_super_admin_keyboard())
                return

        # ========================================
        # بخش تحلیل مدیریتی
        # ========================================
        if text == "🎯 تحلیل مدیریتی" and (role == 'admin' or is_super_admin):
            if is_holiday():
                send_message(chat_id, "📅 امروز تعطیل است، گزارشی ثبت نشده است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            analysis = get_today_performance_analysis()
            if analysis:
                msg = f"📈 **تحلیل مدیریتی امروز** - {get_shamsi_date_formatted(get_shamsi_date())}\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                msg += generate_management_analysis(analysis)
                msg += f"\n\n💰 کل وصول: {analysis['today_total']//1_000_000:,.0f} میلیون ریال"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, msg, keyboard)
            else:
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "📊 داده‌های کافی برای تحلیل وجود ندارد.", keyboard)
            return

        # ========================================
        # بخش گزارش روند شعبه
        # ========================================
        if text == "📊 گزارش روند شعبه" and (role == 'admin' or is_super_admin):
            user_states[chat_id]["state"] = "WAITING_FOR_BRANCH_TREND"
            send_message(chat_id, "🏢 لطفاً **نام شعبه** مورد نظر را برای مشاهده روند وارد کنید:", get_cancel_keyboard())
            return

        if current_state == "WAITING_FOR_BRANCH_TREND" and (role == 'admin' or is_super_admin):
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.", keyboard)
                return
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM branches WHERE name ILIKE %s", (f"%{text}%",))
                    result = cur.fetchone()
                    if result:
                        branch_id = result[0]
                        trend = get_branch_trend(branch_id, 5)
                        if trend:
                            msg = f"📊 **روند ۵ روز اخیر شعبه {text}**\n━━━━━━━━━━━━━━━━━━\n"
                            for i, (date, amount) in enumerate(trend, 1):
                                msg += f"{i}. 📅 {get_shamsi_date_formatted(date)}: {amount//1_000_000:,.0f} میلیون ریال\n"
                            keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                            send_message(chat_id, msg, keyboard)
                        else:
                            keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                            send_message(chat_id, f"📭 هیچ داده‌ای برای شعبه {text} یافت نشد.", keyboard)
                    else:
                        send_message(chat_id, f"❌ شعبه‌ای با نام {text} یافت نشد. لطفاً نام دقیق شعبه را وارد کنید.", get_cancel_keyboard())
                        return
            except Exception as e:
                send_message(chat_id, f"❌ خطا: {e}", get_cancel_keyboard())
            finally:
                return_db_connection(conn)
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ========================================
        # بخش عملکرد معاونان
        # ========================================
        if text == "📋 عملکرد معاونان" and (role == 'admin' or is_super_admin):
            deputies = get_all_deputies()
            if not deputies:
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "هیچ معاونی یافت نشد.", keyboard)
                return
            msg = "📋 **گزارش عملکرد معاونان (۳۰ روز اخیر)**\n━━━━━━━━━━━━━━━━━━\n\n"
            for dep in deputies:
                dep_id, dep_chat_id, dep_name, branch_id, branch_name = dep
                perf = get_deputy_performance_report(dep_id, 30)
                if perf:
                    msg += f"👤 {dep_name} - {branch_name or 'بدون شعبه'}\n"
                    msg += f"   📅 ثبت به‌موقع: {perf['on_time']} روز\n"
                    msg += f"   📅 تاخیر: {perf['late']} روز\n"
                    msg += f"   💰 میانگین وصول: {perf['avg_amount']//1_000_000:,.0f} میلیون ریال\n"
                    msg += f"   🏆 بهترین روز: {perf['best_day']//1_000_000:,.0f} میلیون ریال\n\n"
            keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
            send_message(chat_id, msg, keyboard)
            return

        # ========================================
        # بخش مشاهده یادداشت‌ها
        # ========================================
        if text == "📝 مشاهده یادداشت‌ها" and (role == 'admin' or is_super_admin):
            notes = get_all_notes_with_collection(30)
            if notes:
                msg = "📝 **یادداشت‌های اخیر**\n━━━━━━━━━━━━━━━━━━\n"
                for note in notes:
                    msg += f"🏢 {note[1]} | 📅 {note[2]}\n"
                    msg += f"👤 {note[3]}: {note[4]}\n"
                    msg += f"⏰ {note[5].strftime('%H:%M')}\n\n"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, msg, keyboard)
            else:
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "هیچ یادداشتی وجود ندارد.", keyboard)
            return

        # ========================================
        # بخش یادداشت برای معاونین
        # ========================================
        if text == "📝 ثبت یادداشت" and role == 'deputy':
            user_states[chat_id]["state"] = "WAITING_FOR_NOTE_FOR_COLLECTION"
            send_message(chat_id, "📝 لطفاً **شناسه وصول** (ID) که در گزارش‌ها مشاهده می‌کنید و متن یادداشت را به این فرمت وارد کنید:\n\n`[شناسه] | [متن یادداشت]`\n\nمثال: `42 | وصول از پرونده شماره ۱۲۳۴۵`", get_cancel_keyboard())
            return

        if current_state == "WAITING_FOR_NOTE_FOR_COLLECTION" and role == 'deputy':
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                send_message(chat_id, "❌ عملیات لغو شد.", get_deputy_keyboard())
                return
            try:
                parts = text.split('|')
                if len(parts) == 2:
                    collection_id = int(parts[0].strip())
                    note_text = parts[1].strip()
                    if save_note(collection_id, user_db_id, note_text):
                        send_message(chat_id, f"✅ یادداشت برای وصول {collection_id} با موفقیت ثبت شد.", get_deputy_keyboard())
                    else:
                        send_message(chat_id, "❌ خطا در ثبت یادداشت.", get_deputy_keyboard())
                else:
                    send_message(chat_id, "❌ فرمت نامعتبر. لطفاً به شکل `[شناسه] | [متن یادداشت]` وارد کنید.", get_cancel_keyboard())
                    return
            except Exception as e:
                send_message(chat_id, f"❌ خطا: {e}", get_cancel_keyboard())
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        if text == "📋 مشاهده یادداشت‌ها" and role == 'deputy':
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT n.id, b.name, c.shamsi_date, n.note_text, n.created_at
                        FROM notes n
                        JOIN collections c ON n.collection_id = c.id
                        JOIN branches b ON c.branch_id = b.id
                        WHERE n.user_id = %s
                        ORDER BY n.created_at DESC
                        LIMIT 20
                    """, (user_db_id,))
                    notes = cur.fetchall()
                    if notes:
                        msg = "📝 **یادداشت‌های شما**\n━━━━━━━━━━━━━━━━━━\n"
                        for note in notes:
                            msg += f"🏢 {note[1]} | 📅 {note[2]}\n"
                            msg += f"📝 {note[3]}\n"
                            msg += f"⏰ {note[4].strftime('%H:%M')}\n\n"
                        send_message(chat_id, msg, get_deputy_keyboard())
                    else:
                        send_message(chat_id, "شما هیچ یادداشتی ثبت نکرده‌اید.", get_deputy_keyboard())
            except Exception as e:
                send_message(chat_id, f"❌ خطا: {e}", get_deputy_keyboard())
            finally:
                return_db_connection(conn)
            return

        # ========================================
        # منوی ادمین (ادامه)
        # ========================================
        if role == 'admin' or is_super_admin:
            if text == "📊 گزارش امروز":
                shamsi_today = get_shamsi_date()
                if is_holiday(shamsi_today):
                    send_message(chat_id, f"📅 امروز {get_shamsi_date_formatted(shamsi_today)} تعطیل است و گزارشی ثبت نشده است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                    return
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
                        msg += f"   👤 معاون: {dep//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   👥 همکاران: {oth//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   💰 جمع: {tot//1_000_000:,.0f} میلیون ریال\n\n"
                        total_province += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    if stats:
                        s0 = int(safe_format(stats[0]))
                        s1 = int(safe_format(stats[1]))
                        s2 = int(safe_format(stats[2]))
                        msg += f"📈 خلاصه:\n"
                        msg += f"   تعداد شعب ثبت شده: {s0}\n"
                        msg += f"   کل وصولی معاونین: {s1//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   کل وصولی همکاران: {s2//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   💰 جمع کل استان: {total_province//1_000_000:,.0f} میلیون ریال"
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, f"📊 امروز ({shamsi_today}) هنوز هیچ شعبه‌ای اطلاعات ثبت نکرده است.", keyboard)
                return

            if text == "📈 گزارش ۱۰ روز اخیر":
                report = get_province_10_day_report()
                if report:
                    msg = f"📈 گزارش ۱۰ روز اخیر استان زنجان\n━━━━━━━━━━━━━━━━━━\n\n"
                    total_all = 0
                    for row in report:
                        r1 = int(safe_format(row[1]))
                        r2 = int(safe_format(row[2]))
                        r3 = int(safe_format(row[3]))
                        msg += f"📅 {get_shamsi_date_formatted(row[0])}\n"
                        msg += f"   👤 معاونین: {r1//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   👥 سایر همکاران: {r2//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   💰 جمع: {r3//1_000_000:,.0f} میلیون ریال\n\n"
                        total_all += r3
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"📊 کل ۱۰ روز: {total_all//1_000_000:,.0f} میلیون ریال"
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, "📈 دیتابیس خالی است.", keyboard)
                return

            if text == "🏆 رتبه‌بندی شعب":
                report = get_top_5_branches()
                if report:
                    msg = f"🏆 ۵ شعبه برتر استان زنجان\n━━━━━━━━━━━━━━━━━━\n\n"
                    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
                    for idx, row in enumerate(report):
                        tot = int(safe_format(row[1]))
                        cnt = int(safe_format(row[2]))
                        msg += f"{medals[idx]} {row[0]}\n"
                        msg += f"    💰 کل وصولی: {tot//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"    📊 تعداد ثبت: {cnt} روز\n\n"
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, "🏆 داده کافی برای رتبه‌بندی وجود ندارد.", keyboard)
                return

            if text == "💹 آمار مفصل امروز":
                shamsi_today = get_shamsi_date()
                if is_holiday(shamsi_today):
                    send_message(chat_id, f"📅 امروز {get_shamsi_date_formatted(shamsi_today)} تعطیل است، گزارشی ثبت نشده است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                    return
                report = get_detailed_report(shamsi_today)
                if report:
                    msg = f"💹 آمار مفصل امروز\n━━━━━━━━━━━━━━━━━━\n\n"
                    for idx, row in enumerate(report, 1):
                        dep = int(safe_format(row[1]))
                        oth = int(safe_format(row[2]))
                        tot = int(safe_format(row[3]))
                        msg += f"{idx}. 🏢 {row[0]}\n"
                        msg += f"   👤 معاون ({row[4]}): {dep//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   👥 سایرین: {oth//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   💰 جمع: {tot//1_000_000:,.0f} میلیون ریال\n\n"
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, "💹 برای امروز اطلاعاتی وجود ندارد.", keyboard)
                return

            if text == "📉 مقایسه روزانه":
                comparison = get_daily_comparison()
                if comparison:
                    msg = f"📉 مقایسه روزانه (۷ روز اخیر)\n━━━━━━━━━━━━━━━━━━\n\n"
                    for row in comparison:
                        br = int(safe_format(row[1]))
                        tot = int(safe_format(row[2]))
                        msg += f"📅 {get_shamsi_date_formatted(row[0])}\n"
                        msg += f"    🏢 شعب ثبت‌کننده: {br}\n"
                        msg += f"    💰 کل وصولی: {tot//1_000_000:,.0f} میلیون ریال\n\n"
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, "📉 داده کافی وجود ندارد.", keyboard)
                return

            if text == "📅 گزارش تاریخ خاص":
                user_states[chat_id]["state"] = "WAITING_FOR_ADMIN_DATE"
                send_message(chat_id, "📅 لطفاً تاریخ مورد نظر را به فرمت **YYYY/MM/DD** وارد کنید (مثلاً ۱۴۰۳/۰۱/۱۵):", get_cancel_keyboard())
                return

            if text == "📊 بهترین/بدترین روز":
                best, worst = get_best_worst_days(5)
                msg = "📊 **بهترین روزهای استان**\n━━━━━━━━━━━━━━━━━━\n"
                if best:
                    for i, row in enumerate(best, 1):
                        msg += f"{i}. 📅 {get_shamsi_date_formatted(row[0])} -> {int(row[1])//1_000_000:,.0f} میلیون ریال\n"
                else:
                    msg += "هیچ داده‌ای موجود نیست.\n"
                msg += "\n📊 **بدترین روزهای استان**\n━━━━━━━━━━━━━━━━━━\n"
                if worst:
                    for i, row in enumerate(worst, 1):
                        msg += f"{i}. 📅 {get_shamsi_date_formatted(row[0])} -> {int(row[1])//1_000_000:,.0f} میلیون ریال\n"
                else:
                    msg += "هیچ داده‌ای موجود نیست."
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, msg, keyboard)
                return

            keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
            send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", keyboard)
            return

        # ========================================
        # منوی معاون
        # ========================================
        if role == 'deputy':
            if text == "💰 ثبت وصولی روزانه":
                shamsi_today = get_shamsi_date()
                if is_holiday(shamsi_today):
                    send_message(chat_id, f"📅 امروز {get_shamsi_date_formatted(shamsi_today)} تعطیل است، نیازی به ثبت وصول نیست.", get_deputy_keyboard())
                    return
                existing = check_existing_collection(branch_id, shamsi_today)
                if existing:
                    col_id, dep_val, oth_val = existing
                    user_states[chat_id]["state"] = "WAITING_FOR_EDIT_CONFIRMATION"
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
                        f"👤 وصولی معاون: {int(safe_format(dep_val))//1_000_000:,.0f} میلیون ریال\n"
                        f"👥 وصولی همکاران: {int(safe_format(oth_val))//1_000_000:,.0f} میلیون ریال\n"
                        f"💰 جمع کل: {(int(safe_format(dep_val)) + int(safe_format(oth_val)))//1_000_000:,.0f} میلیون ریال\n"
                        f"━━━━━━━━━━━━━━━\n\n"
                        f"❓ آیا مایل به ویرایش هستید؟"
                    )
                    send_message(chat_id, msg, confirm_keyboard)
                else:
                    user_states[chat_id]["state"] = "WAITING_FOR_DEPUTY_AMOUNT"
                    user_states[chat_id]["edit_mode"] = False
                    send_message(chat_id, "📝 لطفاً میزان وصولی خود (معاون) را به **میلیون ریال** وارد کنید:", get_cancel_keyboard())
                return

            if text == "📊 گزارش وصولی":
                report = get_branch_10_day_report(branch_id)
                if report:
                    msg = f"📊 گزارش وصول شعبه {branch_name}\n(۱۰ روز اخیر)\n━━━━━━━━━━━━━━━━━━\n\n"
                    total_sum = 0
                    for i, row in enumerate(report, 1):
                        dep = int(safe_format(row[1]))
                        oth = int(safe_format(row[2]))
                        tot = int(safe_format(row[3]))
                        msg += f"{i}. 📅 {get_shamsi_date_formatted(row[0])}\n"
                        msg += f"   👤 معاون: {dep//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   👥 همکاران: {oth//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   💰 جمع: {tot//1_000_000:,.0f} میلیون ریال\n\n"
                        total_sum += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"📈 جمع ۱۰ روز: {total_sum//1_000_000:,.0f} میلیون ریال\n"
                    msg += f"📊 میانگین روزانه: {total_sum//len(report)//1_000_000:,.0f} میلیون ریال"
                    send_message(chat_id, msg, get_deputy_keyboard())
                else:
                    send_message(chat_id, "📊 هیچ سابقه وصولی برای شعبه شما یافت نشد.", get_deputy_keyboard())
                return

            if text == "📈 مقایسه عملکرد":
                perf = get_branch_performance(branch_id, 7)
                if perf:
                    msg = f"📈 تحلیل عملکرد شعبه {branch_name}\n(۷ روز اخیر)\n━━━━━━━━━━━━━━━━━━\n\n"
                    for i, row in enumerate(perf, 1):
                        daily = int(safe_format(row[1]))
                        avg = int(safe_format(row[2]))
                        trend = "📈" if i < len(perf) and perf[i-1][1] and row[1] and perf[i-1][1] > row[1] else "📉"
                        msg += f"{trend} {get_shamsi_date_formatted(row[0])}\n"
                        msg += f"   جمع روزانه: {daily//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   میانگین متحرک: {avg//1_000_000:,.0f} میلیون ریال\n\n"
                    send_message(chat_id, msg, get_deputy_keyboard())
                else:
                    send_message(chat_id, "📈 داده کافی برای تحلیل وجود ندارد.", get_deputy_keyboard())
                return

            if text == "📋 مشاهده ثبت امروز":
                shamsi_today = get_shamsi_date()
                if is_holiday(shamsi_today):
                    send_message(chat_id, f"📅 امروز {get_shamsi_date_formatted(shamsi_today)} تعطیل است، ثبت وصولی وجود ندارد.", get_deputy_keyboard())
                    return
                existing = check_existing_collection(branch_id, shamsi_today)
                if existing:
                    col_id, dep_val, oth_val = existing
                    msg = (
                        f"📋 ثبت امروز شعبه {branch_name}\n"
                        f"📅 تاریخ: {get_shamsi_date_formatted(shamsi_today)}\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"👤 وصولی معاون: {int(safe_format(dep_val))//1_000_000:,.0f} میلیون ریال\n"
                        f"👥 وصولی همکاران: {int(safe_format(oth_val))//1_000_000:,.0f} میلیون ریال\n"
                        f"💰 جمع کل: {(int(safe_format(dep_val)) + int(safe_format(oth_val)))//1_000_000:,.0f} میلیون ریال"
                    )
                    send_message(chat_id, msg, get_deputy_keyboard())
                else:
                    send_message(chat_id, f"📭 امروز ({shamsi_today}) هنوز ثبت نشده است.", get_deputy_keyboard())
                return

            if text == "📅 گزارش تاریخ خاص":
                user_states[chat_id]["state"] = "WAITING_FOR_BRANCH_DATE"
                send_message(chat_id, "📅 لطفاً تاریخ مورد نظر را به فرمت **YYYY/MM/DD** وارد کنید (مثلاً ۱۴۰۳/۰۱/۱۵):", get_cancel_keyboard())
                return

            if text == "📊 تاریخچه کامل":
                history = get_branch_full_history(branch_id)
                if history:
                    msg = f"📊 تاریخچه کامل شعبه {branch_name}\n━━━━━━━━━━━━━━━━━━\n\n"
                    total_all = 0
                    for i, row in enumerate(history, 1):
                        dep = int(safe_format(row[1]))
                        oth = int(safe_format(row[2]))
                        tot = int(safe_format(row[3]))
                        msg += f"{i}. 📅 {get_shamsi_date_formatted(row[0])}\n"
                        msg += f"   👤 معاون: {dep//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   👥 همکاران: {oth//1_000_000:,.0f} میلیون ریال\n"
                        msg += f"   💰 جمع: {tot//1_000_000:,.0f} میلیون ریال\n\n"
                        total_all += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"📈 جمع کل از ابتدا: {total_all//1_000_000:,.0f} میلیون ریال"
                    send_message(chat_id, msg, get_deputy_keyboard())
                else:
                    send_message(chat_id, "📭 هیچ سابقه‌ای برای شعبه شما وجود ندارد.", get_deputy_keyboard())
                return

            send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", get_deputy_keyboard())
            return

        # ========================================
        # نقش نامعتبر
        # ========================================
        send_message(chat_id, "نقش شما نامعتبر است. لطفاً با پشتیبان تماس بگیرید.")

    except Exception as e:
        logger.error(f"❌ handle_message error: {e}", exc_info=True)
        try:
            send_message(message['chat']['id'], "❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        except:
            pass

# ============================================
# Keep-Alive داخلی
# ============================================
def keep_alive_loop():
    while True:
        try:
            time.sleep(30)
            url = f"{BASE_URL}/getMe"
            res = requests_session.get(url, timeout=10)
            if res.status_code == 200:
                logger.debug("🔄 Keep-alive ping sent.")
            else:
                logger.warning(f"⚠️ Keep-alive ping failed: {res.status_code}")
        except Exception as e:
            logger.error(f"❌ Keep-alive error: {e}")

# ============================================
# راه‌اندازی زمان‌بندی کارها
# ============================================
def start_scheduler():
    scheduler = BackgroundScheduler(timezone='Asia/Tehran')
    scheduler.add_job(
        check_and_send_reminders,
        CronTrigger(hour=15, minute=0),
        id='reminder_job',
        replace_existing=True
    )
    scheduler.add_job(
        send_daily_report_to_admins,
        CronTrigger(hour=17, minute=30),
        id='daily_report_job',
        replace_existing=True
    )
    scheduler.add_job(
        check_and_send_drop_alerts,
        CronTrigger(hour=18, minute=30),
        id='drop_alert_job',
        replace_existing=True
    )
    scheduler.start()
    logger.info("✅ Scheduler started (reminders at 15:00, reports at 17:30, alerts at 18:30)")

# ============================================
# Main Polling Loop
# ============================================
def main():
    global requests_session
    offset = 0
    logger.info("🤖 Bot started successfully!")
    logger.info("📡 Waiting for messages...")
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info(f"🌐 Flask server started on port {PORT}")
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    start_scheduler()
    while True:
        try:
            url = f"{BASE_URL}/getUpdates"
            params = {"offset": offset, "timeout": 30}
            res = requests_session.get(url, params=params, timeout=45)
            if res.status_code == 200:
                data = res.json()
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        update_id = update["update_id"]
                        if update_id in processed_updates:
                            continue
                        processed_updates.add(update_id)
                        if len(processed_updates) > 1000:
                            processed_updates.clear()
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
            requests_session = create_session()
            time.sleep(5)
        except Exception as e:
            logger.error(f"❌ Unexpected error in main loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
