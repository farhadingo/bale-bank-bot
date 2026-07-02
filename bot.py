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
from collections import defaultdict
import json

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
    session.headers.update({'Connection': 'keep-alive', 'User-Agent': 'Bale-Bank-Bot/5.0'})
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
    """تبدیل رشته شمسی به datetime برای محاسبات"""
    parts = shamsi_str.split('/')
    if len(parts) != 3:
        return None
    year, month, day = map(int, parts)
    try:
        return jdatetime.date(year, month, day).togregorian()
    except:
        return None

# ============================================
# ارسال پیام
# ============================================
def send_message(chat_id, text, reply_markup=None, remove_keyboard=False):
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
            [{"text": "🔙 خروج"}, {"text": "❓ راهنما"}]
        ],
        "resize_keyboard": True
    }

def get_cancel_keyboard():
    return {"keyboard": [[{"text": "🔙 انصراف"}]], "resize_keyboard": True}

# ============================================
# توابع دیتابیس جدید و به‌روز شده
# ============================================

# ---- توابع قبلی با اضافه شدن فیلدهای جدید ----
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

# ---- لاگ فعالیت کاربران ----
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

# ---- توابع یادداشت ----
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

# ---- توابع ذخیره وصول با پشتیبانی از یادداشت ----
def save_or_update_collection_with_note(branch_id, deputy_amount, others_amount, shamsi_date, user_id, note_text=None, update_existing=False):
    conn = get_db_connection()
    created_at_iran = get_iran_time()
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

# ---- توابع تحلیل و گزارش‌های پیشرفته ----
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
    """تحلیل عملکرد امروز استان"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            shamsi_today = get_shamsi_date()
            # کل وصول امروز
            cur.execute("""
                SELECT SUM(total_amount) FROM collections WHERE shamsi_date = %s
            """, (shamsi_today,))
            today_total = cur.fetchone()[0] or 0
            
            # وصول به تفکیک شعبه
            cur.execute("""
                SELECT b.name, c.total_amount
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                WHERE c.shamsi_date = %s
                ORDER BY c.total_amount DESC
            """, (shamsi_today,))
            branch_data = cur.fetchall()
            
            # سهم معاونین و همکاران
            cur.execute("""
                SELECT SUM(deputy_amount), SUM(others_amount)
                FROM collections
                WHERE shamsi_date = %s
            """, (shamsi_today,))
            deputy_others = cur.fetchone()
            deputy_total = deputy_others[0] or 0
            others_total = deputy_others[1] or 0
            
            # تعداد شعب ثبت‌کننده
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
    """تشخیص شعب با افت عملکرد نسبت به میانگین هفته"""
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
                if weekly_avg > 0 and today < (weekly_avg * 0.6):  # افت 40%
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
    """گزارش روند روزهای اخیر یک شعبه"""
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
    """گزارش عملکرد یک معاون در ۳۰ روز گذشته"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # تعداد روزهای ثبت به‌موقع (قبل از ۱۵)
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
    """شعبی که تا ساعت ۱۵ امروز ثبت نکرده‌اند"""
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
    """لیست تمام کاربران ادمین و سوپرادمین"""
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

def get_all_deputies():
    """لیست تمام معاونین"""
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

# ---- توابع قبلی (بدون تغییر) ----
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

# ---- توابع مدیریتی سوپرادمین ----
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

def get_log_file_path():
    return "bot.log"

# ============================================
# توابع ارسال خودکار و یادآوری
# ============================================

def send_reminder_to_deputy(chat_id, branch_name):
    """ارسال یادآوری به یک معاون"""
    msg = f"⏰ یادآوری: شما تا ساعت ۱۵ امروز گزارش وصول شعبه {branch_name} را ثبت نکرده‌اید. لطفاً هرچه سریعتر اقدام فرمایید."
    send_message(chat_id, msg)

def send_reminder_to_admin(chat_id, unreported_list):
    """ارسال گزارش شعب ثبت‌نشده به مدیران"""
    if not unreported_list:
        return
    msg = "📋 **شعب ثبت‌نشده امروز**\n━━━━━━━━━━━━━━━━━━\n"
    for branch in unreported_list:
        msg += f"🏢 {branch[1]} (معاون: {branch[2] or 'نامشخص'})\n"
    send_message(chat_id, msg)

def send_daily_report_to_admins():
    """ارسال گزارش پایان روز به مدیران (ساعت ۱۷:۳۰)"""
    shamsi_today = get_shamsi_date()
    analysis = get_today_performance_analysis()
    if not analysis:
        return
    
    admins = get_all_admins()
    if not admins:
        return
    
    # ساخت گزارش
    msg = f"📊 **گزارش پایان روز** - {get_shamsi_date_formatted(shamsi_today)}\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"💰 کل وصول استان: {analysis['today_total']:,.0f} ریال\n"
    msg += f"🏢 تعداد شعب ثبت‌کننده: {analysis['branches_count']}\n"
    msg += f"👤 سهم معاونین: {analysis['deputy_total']:,.0f} ریال\n"
    msg += f"👥 سهم همکاران: {analysis['others_total']:,.0f} ریال\n\n"
    
    # ۵ شعبه برتر
    if analysis['branch_data']:
        msg += "🏆 **۵ شعبه برتر امروز**\n"
        for i, (name, amount) in enumerate(analysis['branch_data'][:5], 1):
            msg += f"{i}. {name}: {amount:,.0f} ریال\n"
    
    # تحلیل مدیریتی
    msg += "\n📈 **تحلیل مدیریتی**\n"
    msg += generate_management_analysis(analysis)
    
    # ارسال به همه ادمین‌ها
    for admin in admins:
        admin_id = admin[1]
        if admin_id:
            send_message(admin_id, msg)

def generate_management_analysis(analysis):
    """تولید تحلیل مدیریتی از داده‌ها"""
    lines = []
    today_total = analysis['today_total']
    branch_data = analysis['branch_data']
    deputy_total = analysis['deputy_total']
    others_total = analysis['others_total']
    branches_count = analysis['branches_count']
    
    # تحلیل سهم شعب
    if branch_data and len(branch_data) >= 4:
        top4_sum = sum([amount for _, amount in branch_data[:4]])
        top4_percent = (top4_sum / today_total * 100) if today_total > 0 else 0
        lines.append(f"📊 {top4_percent:.0f}% وصول استان توسط ۴ شعبه انجام شده است.")
    
    # بیشترین سهم
    if branch_data:
        top_branch = branch_data[0]
        lines.append(f"🏆 بیشترین سهم وصول امروز مربوط به {top_branch[0]} است.")
    
    # مقایسه معاون و همکار
    if deputy_total + others_total > 0:
        dep_percent = (deputy_total / (deputy_total + others_total) * 100) if (deputy_total + others_total) > 0 else 0
        if dep_percent > 50:
            lines.append(f"👤 میانگین وصول معاونان ({dep_percent:.0f}%) از همکاران بیشتر بوده است.")
        else:
            lines.append(f"👥 میانگین وصول همکاران ({100-dep_percent:.0f}%) از معاونان بیشتر بوده است.")
    
    # تحلیل رشد برای هر شعبه (با استفاده از میانگین ماهانه)
    for branch_name, amount in branch_data[:3]:
        # دریافت میانگین ماهانه شعبه (در اینجا ساده‌سازی شده)
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

def get_branch_monthly_avg_for_name(branch_name):
    """دریافت میانگین ماهانه یک شعبه با نام"""
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

def check_and_send_reminders():
    """بررسی و ارسال یادآوری‌ها (ساعت ۱۵)"""
    logger.info("🔄 Running reminder check...")
    unreported = get_unreported_branches()
    
    if unreported:
        # ارسال به معاونین
        for branch in unreported:
            branch_id, name, deputy_name, deputy_chat_id = branch
            if deputy_chat_id:
                send_reminder_to_deputy(deputy_chat_id, name)
        
        # ارسال گزارش به مدیران
        admins = get_all_admins()
        for admin in admins:
            admin_id = admin[1]
            if admin_id:
                send_reminder_to_admin(admin_id, unreported)
        
        logger.info(f"✅ Reminders sent to {len(unreported)} branches")
    else:
        logger.info("✅ All branches have reported today")

def check_and_send_drop_alerts():
    """بررسی و ارسال هشدار افت عملکرد"""
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
                msg += f"   امروز: {drop['today']:,.0f} ریال\n"
                msg += f"   میانگین هفته: {drop['weekly_avg']:,.0f} ریال\n"
                msg += f"   📉 افت: {drop['drop_percent']}%\n\n"
            send_message(admin_id, msg)
        logger.info(f"✅ Drop alerts sent for {len(drops)} branches")
    else:
        logger.info("✅ No drop alerts")

# ============================================
# پردازش پیام‌ها (با قابلیت‌های جدید)
# ============================================
def handle_message(message):
    try:
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()
        
        user_state = user_states.get(chat_id, {"state": "LOGGED_OUT"})
        current_state = user_state.get("state", "LOGGED_OUT")

        # ===== حالت خروج یا عدم احراز هویت =====
        if current_state == "LOGGED_OUT" or current_state == "WAITING_FOR_EMP_NUM":
            if current_state != "WAITING_FOR_EMP_NUM":
                user_states[chat_id] = {"state": "WAITING_FOR_EMP_NUM"}
                send_message(chat_id, "👋 سلام! به ربات وصول مطالبات استان زنجان خوش آمدید.\n\n🔐 لطفاً شماره کارمندی خود را ارسال کنید:", remove_keyboard=True)
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
                        f"خوش آمدید! 👋"
                    )
                    keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                    send_message(chat_id, welcome_msg, keyboard)
            else:
                send_message(chat_id, "❌ شماره کارمندی در سیستم یافت نشد.\nلطفاً شماره کارمندی صحیح خود را بفرستید:")
            return

        # ===== وضعیت انتظار برای رمز سوپرادمین =====
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

        # ===== کاربر لاگین کرده =====
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
                send_message(chat_id, "✏️ اکنون میزان وصولی سایر همکاران شعبه را وارد کنید (برحسب ریال):", get_cancel_keyboard())
            except ValueError:
                send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت وارد کنید.")
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
                
                # ذخیره با قابلیت یادداشت
                user_states[chat_id]["state"] = "WAITING_FOR_NOTE"
                user_states[chat_id]["collection_data"] = {
                    "deputy_amount": deputy_amount,
                    "others_amount": others_amount,
                    "shamsi_date": shamsi_date,
                    "is_edit": is_edit
                }
                send_message(chat_id, "📝 آیا می‌خواهید یادداشتی برای این وصول ثبت کنید؟ (اختیاری)\nلطفاً متن یادداشت را ارسال کنید یا روی «🔙 انصراف» بزنید تا بدون یادداشت ذخیره شود.", get_cancel_keyboard())
            except ValueError:
                send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت وارد کنید.")
            return

        elif current_state == "WAITING_FOR_NOTE":
            if text == "🔙 انصراف":
                # ذخیره بدون یادداشت
                data = user_state.get("collection_data", {})
                success = save_or_update_collection_with_note(
                    branch_id=branch_id,
                    deputy_amount=data.get("deputy_amount", 0),
                    others_amount=data.get("others_amount", 0),
                    shamsi_date=data.get("shamsi_date", get_shamsi_date()),
                    user_id=user_db_id,
                    note_text=None,
                    update_existing=data.get("is_edit", False)
                )
                user_states[chat_id]["state"] = "LOGGED_IN"
                if success:
                    total = data.get("deputy_amount", 0) + data.get("others_amount", 0)
                    msg = f"✅ ثبت شد.\n💰 جمع کل: {total:,.0f} ریال"
                    log_user_activity(user_db_id, "collection_add", f"ثبت وصول شعبه {branch_name} - مبلغ: {total}")
                else:
                    msg = "❌ خطا در ثبت اطلاعات."
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                send_message(chat_id, msg, keyboard)
                return
            else:
                # ذخیره با یادداشت
                data = user_state.get("collection_data", {})
                note_text = text
                success = save_or_update_collection_with_note(
                    branch_id=branch_id,
                    deputy_amount=data.get("deputy_amount", 0),
                    others_amount=data.get("others_amount", 0),
                    shamsi_date=data.get("shamsi_date", get_shamsi_date()),
                    user_id=user_db_id,
                    note_text=note_text,
                    update_existing=data.get("is_edit", False)
                )
                user_states[chat_id]["state"] = "LOGGED_IN"
                if success:
                    total = data.get("deputy_amount", 0) + data.get("others_amount", 0)
                    msg = f"✅ ثبت شد.\n💰 جمع کل: {total:,.0f} ریال\n📝 یادداشت: {note_text}"
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
                send_message(chat_id, "✏️ لطفاً مبلغ جدید وصولی خود (معاون) را وارد کنید:", get_cancel_keyboard())
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
                        f"👤 وصولی معاون: {int(dep):,.0f} ریال\n"
                        f"👥 وصولی همکاران: {int(oth):,.0f} ریال\n"
                        f"💰 جمع کل: {int(total):,.0f} ریال"
                    )
                    # نمایش یادداشت‌های این وصول
                    col_id = check_existing_collection(branch_id, shamsi_date)
                    if col_id:
                        notes = get_notes_for_collection(col_id[0])
                        if notes:
                            msg += "\n\n📝 **یادداشت‌ها:**\n"
                            for n in notes:
                                msg += f"• {n[1]}: {n[2]} ({get_shamsi_date_formatted(get_shamsi_date())})\n"
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
                        msg += f"   👤 معاون ({row[4]}): {dep:,.0f}\n"
                        msg += f"   👥 همکاران: {oth:,.0f}\n"
                        msg += f"   💰 جمع: {tot:,.0f} ریال\n\n"
                        total_all += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n💰 جمع کل استان: {total_all:,.0f} ریال"
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
                        msg += f"🆔 {c[0]} | {c[1]} | {c[2]} | {c[5]:,} ریال | ثبت: {c[6]}\n"
                    msg += "\nبرای حذف: /delete_collection [id]\n"
                    msg += "برای ویرایش: /edit_collection [id] [deputy_amount] [others_amount]"
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
                        msg += f"👤 {log[1]} ({log[2]}) | {log[3]} | {log[4]}\n"
                        msg += f"⏰ {log[5].strftime('%Y-%m-%d %H:%M')}\n\n"
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
                        msg += f"⏰ {note[5].strftime('%Y-%m-%d %H:%M')}\n\n"
                    send_message(chat_id, msg, get_super_admin_keyboard())
                else:
                    send_message(chat_id, "هیچ یادداشتی وجود ندارد.", get_super_admin_keyboard())
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
                        deputy = int(parts[2])
                        others = int(parts[3])
                        if update_collection(col_id, deputy, others):
                            send_message(chat_id, f"✅ گزارش {col_id} به‌روزرسانی شد.", get_super_admin_keyboard())
                        else:
                            send_message(chat_id, "❌ خطا در ویرایش گزارش.", get_super_admin_keyboard())
                    except:
                        send_message(chat_id, "❌ فرمت: /edit_collection [id] [deputy_amount] [others_amount]", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ فرمت: /edit_collection [id] [deputy_amount] [others_amount]", get_super_admin_keyboard())
                return

            # اگر سوپرادمین بود و دستور خاصی نبود، منوی خودش را نمایش بده
            if text not in ["👥 مدیریت کاربران", "📊 مدیریت گزارش‌ها", "📋 مشاهده لاگ‌ها", "📋 لاگ ورود/خروج", "📝 مشاهده یادداشت‌ها", "📊 گزارش امروز", "📈 گزارش ۱۰ روز اخیر", "🏆 رتبه‌بندی شعب", "💹 آمار مفصل امروز", "🎯 تحلیل مدیریتی", "📅 گزارش تاریخ خاص", "📊 بهترین/بدترین روز", "📊 گزارش روند شعبه", "📋 عملکرد معاونان", "🔙 خروج", "❓ راهنما"]:
                send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", get_super_admin_keyboard())
                return

        # ========================================
        # بخش تحلیل مدیریتی (برای ادمین و سوپرادمین)
        # ========================================
        if text == "🎯 تحلیل مدیریتی" and (role == 'admin' or is_super_admin):
            analysis = get_today_performance_analysis()
            if analysis:
                msg = f"📈 **تحلیل مدیریتی امروز** - {get_shamsi_date_formatted(get_shamsi_date())}\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                msg += generate_management_analysis(analysis)
                send_message(chat_id, msg, get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
            else:
                send_message(chat_id, "📊 داده‌های کافی برای تحلیل وجود ندارد.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
            return

        # ========================================
        # بخش گزارش روند شعبه (برای ادمین و سوپرادمین)
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
            # پیدا کردن شعبه با نام
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
                                msg += f"{i}. 📅 {get_shamsi_date_formatted(date)}: {amount:,.0f} ریال\n"
                            send_message(chat_id, msg, get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                        else:
                            send_message(chat_id, f"📭 هیچ داده‌ای برای شعبه {text} یافت نشد.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
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
        # بخش عملکرد معاونان (برای ادمین و سوپرادمین)
        # ========================================
        if text == "📋 عملکرد معاونان" and (role == 'admin' or is_super_admin):
            deputies = get_all_deputies()
            if not deputies:
                send_message(chat_id, "هیچ معاونی یافت نشد.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            msg = "📋 **گزارش عملکرد معاونان (۳۰ روز اخیر)**\n━━━━━━━━━━━━━━━━━━\n\n"
            for dep in deputies:
                dep_id, dep_chat_id, dep_name, branch_id, branch_name = dep
                perf = get_deputy_performance_report(dep_id, 30)
                if perf:
                    msg += f"👤 {dep_name} - {branch_name or 'بدون شعبه'}\n"
                    msg += f"   📅 ثبت به‌موقع: {perf['on_time']} روز\n"
                    msg += f"   📅 تاخیر: {perf['late']} روز\n"
                    msg += f"   💰 میانگین وصول: {perf['avg_amount']:,.0f} ریال\n"
                    msg += f"   🏆 بهترین روز: {perf['best_day']:,.0f} ریال\n\n"
            send_message(chat_id, msg, get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
            return

        # ========================================
        # بخش مشاهده یادداشت‌ها (برای ادمین و سوپرادمین)
        # ========================================
        if text == "📝 مشاهده یادداشت‌ها" and (role == 'admin' or is_super_admin):
            notes = get_all_notes_with_collection(30)
            if notes:
                msg = "📝 **یادداشت‌های اخیر**\n━━━━━━━━━━━━━━━━━━\n"
                for note in notes:
                    msg += f"🏢 {note[1]} | 📅 {note[2]}\n"
                    msg += f"👤 {note[3]}: {note[4]}\n"
                    msg += f"⏰ {note[5].strftime('%Y-%m-%d %H:%M')}\n\n"
                send_message(chat_id, msg, get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
            else:
                send_message(chat_id, "هیچ یادداشتی وجود ندارد.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
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
            # نمایش یادداشت‌های خود معاون
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
                            msg += f"⏰ {note[4].strftime('%Y-%m-%d %H:%M')}\n\n"
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
            # گزارش امروز
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
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, f"📊 امروز ({shamsi_today}) هنوز هیچ شعبه‌ای اطلاعات ثبت نکرده است.", keyboard)
                return

            # سایر گزارش‌های ادمین (همانند قبل)
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
                        msg += f"   👤 معاونین: {r1:,.0f}\n"
                        msg += f"   👥 سایر همکاران: {r2:,.0f}\n"
                        msg += f"   💰 جمع: {r3:,.0f} ریال\n\n"
                        total_all += r3
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"📊 کل ۱۰ روز: {total_all:,.0f} ریال"
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
                        msg += f"    💰 کل وصولی: {tot:,.0f} ریال\n"
                        msg += f"    📊 تعداد ثبت: {cnt} روز\n\n"
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, "🏆 داده کافی برای رتبه‌بندی وجود ندارد.", keyboard)
                return

            if text == "💹 آمار مفصل امروز":
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
                        msg += f"    💰 کل وصولی: {tot:,.0f} ریال\n\n"
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, "📉 داده کافی وجود ندارد.", keyboard)
                return

            if text == "📅 گزارش تاریخ خاص":
                user_states[chat_id]["state"] = "WAITING_FOR_ADMIN_DATE"
                send_message(chat_id, "📅 لطفاً تاریخ مورد نظر را به فرمت **YYYY/MM/DD** وارد کنید (مثلاً 1403/01/15):", get_cancel_keyboard())
                return

            if text == "📊 بهترین/بدترین روز":
                best, worst = get_best_worst_days(5)
                msg = "📊 **بهترین روزهای استان**\n━━━━━━━━━━━━━━━━━━\n"
                if best:
                    for i, row in enumerate(best, 1):
                        msg += f"{i}. 📅 {get_shamsi_date_formatted(row[0])} -> {int(row[1]):,.0f} ریال\n"
                else:
                    msg += "هیچ داده‌ای موجود نیست.\n"
                msg += "\n📊 **بدترین روزهای استان**\n━━━━━━━━━━━━━━━━━━\n"
                if worst:
                    for i, row in enumerate(worst, 1):
                        msg += f"{i}. 📅 {get_shamsi_date_formatted(row[0])} -> {int(row[1]):,.0f} ریال\n"
                else:
                    msg += "هیچ داده‌ای موجود نیست."
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, msg, keyboard)
                return

            # اگر دستور خاصی نبود
            keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
            send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", keyboard)
            return

        # ========================================
        # منوی معاون (ادامه)
        # ========================================
        if role == 'deputy':
            if text == "💰 ثبت وصولی روزانه":
                shamsi_today = get_shamsi_date()
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
                        f"👤 وصولی معاون: {int(safe_format(dep_val)):,.0f} ریال\n"
                        f"👥 وصولی همکاران: {int(safe_format(oth_val)):,.0f} ریال\n"
                        f"💰 جمع کل: {int(safe_format(dep_val)) + int(safe_format(oth_val)):,.0f} ریال\n"
                        f"━━━━━━━━━━━━━━━\n\n"
                        f"❓ آیا مایل به ویرایش هستید؟"
                    )
                    send_message(chat_id, msg, confirm_keyboard)
                else:
                    user_states[chat_id]["state"] = "WAITING_FOR_DEPUTY_AMOUNT"
                    user_states[chat_id]["edit_mode"] = False
                    send_message(chat_id, "📝 لطفاً میزان وصولی خود (معاون) را وارد کنید:", get_cancel_keyboard())
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
                        msg += f"   👤 معاون: {dep:,.0f}\n"
                        msg += f"   👥 همکاران: {oth:,.0f}\n"
                        msg += f"   💰 جمع: {tot:,.0f} ریال\n\n"
                        total_sum += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"📈 جمع ۱۰ روز: {total_sum:,.0f} ریال\n"
                    msg += f"📊 میانگین روزانه: {total_sum//len(report):,.0f} ریال"
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
                        msg += f"   جمع روزانه: {daily:,.0f} ریال\n"
                        msg += f"   میانگین متحرک: {avg:,.0f} ریال\n\n"
                    send_message(chat_id, msg, get_deputy_keyboard())
                else:
                    send_message(chat_id, "📈 داده کافی برای تحلیل وجود ندارد.", get_deputy_keyboard())
                return

            if text == "📋 مشاهده ثبت امروز":
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
                    send_message(chat_id, msg, get_deputy_keyboard())
                else:
                    send_message(chat_id, f"📭 امروز ({shamsi_today}) هنوز ثبت نشده است.", get_deputy_keyboard())
                return

            if text == "📅 گزارش تاریخ خاص":
                user_states[chat_id]["state"] = "WAITING_FOR_BRANCH_DATE"
                send_message(chat_id, "📅 لطفاً تاریخ مورد نظر را به فرمت **YYYY/MM/DD** وارد کنید (مثلاً 1403/01/15):", get_cancel_keyboard())
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
                        msg += f"   👤 معاون: {dep:,.0f}\n"
                        msg += f"   👥 همکاران: {oth:,.0f}\n"
                        msg += f"   💰 جمع: {tot:,.0f} ریال\n\n"
                        total_all += tot
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"📈 جمع کل از ابتدا: {total_all:,.0f} ریال"
                    send_message(chat_id, msg, get_deputy_keyboard())
                else:
                    send_message(chat_id, "📭 هیچ سابقه‌ای برای شعبه شما وجود ندارد.", get_deputy_keyboard())
                return

            # اگر دستور خاصی نبود
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
    
    # یادآوری شعب ثبت‌نشده (ساعت ۱۵)
    scheduler.add_job(
        check_and_send_reminders,
        CronTrigger(hour=15, minute=0),
        id='reminder_job',
        replace_existing=True
    )
    
    # گزارش پایان روز (ساعت ۱۷:۳۰)
    scheduler.add_job(
        send_daily_report_to_admins,
        CronTrigger(hour=17, minute=30),
        id='daily_report_job',
        replace_existing=True
    )
    
    # هشدار افت عملکرد (ساعت ۱۸:۳۰)
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
    
    # راه‌اندازی زمان‌بندی
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
