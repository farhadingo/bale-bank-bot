```python
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
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np
from PIL import Image
import arabic_reshaper
from bidi.algorithm import get_display
import os.path

# ============================================================
# تنظیمات لاگین
# ============================================================
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
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD")

if not BOT_TOKEN or not DB_URL:
    logger.error("❌ BOT_TOKEN and DATABASE_URL are required!")
    exit(1)

if not SUPER_ADMIN_PASSWORD:
    logger.error("❌ SUPER_ADMIN_PASSWORD environment variable is required!")
    exit(1)

BASE_URL = f"https://tapi.bale.ai/bot{BOT_TOKEN}"
logger.info(f"✅ Bale API URL: {BASE_URL}")

# ============================================================
# اپلیکیشن Flask برای Health Check
# ============================================================
flask_app = Flask(__name__)

@flask_app.route('/health')
def health():
    return jsonify({"status": "healthy", "timestamp": time.time()})

@flask_app.route('/')
def root():
    return jsonify({"message": "Bot is running", "status": "active"})

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT)

# ============================================================
# Session با Keep-Alive
# ============================================================
def create_session():
    session = requests.Session()
    session.headers.update({'Connection': 'keep-alive', 'User-Agent': 'Bale-Bank-Bot/8.2'})
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

# ============================================================
# Connection Pool دیتابیس
# ============================================================
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DB_URL)
    logger.info("✅ Database pool created.")
except Exception as e:
    logger.error(f"❌ Pool error: {e}")
    db_pool = None

# ============================================================
# State Management
# ============================================================
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
    if db_pool and conn:
        try:
            db_pool.putconn(conn)
        except:
            conn.close()
    elif conn:
        conn.close()

# ============================================================
# توابع کمکی - نرمال‌سازی ارقام فارسی/عربی به انگلیسی
# ============================================================
PERSIAN_DIGITS = '۰۱۲۳۴۵۶۷۸۹'
ARABIC_DIGITS = '٠١٢٣٤٥٦٧٨٩'
ENGLISH_DIGITS = '0123456789'
DIGIT_MAP = str.maketrans(PERSIAN_DIGITS + ARABIC_DIGITS, ENGLISH_DIGITS + ENGLISH_DIGITS)

def normalize_digits(text):
    """تبدیل ارقام فارسی/عربی به انگلیسی و حذف کاما و فاصله، پشتیبانی از ممیز"""
    if not text:
        return text
    text = str(text).translate(DIGIT_MAP)
    # حذف کاما و فاصله
    text = text.replace(',', '').replace('،', '').replace(' ', '')
    return text

def parse_number(text):
    """تبدیل متن به عدد صحیح (پشتیبانی از اعداد اعشاری)"""
    try:
        text = normalize_digits(text)
        # اگر ممیز داشت، به float تبدیل و سپس به int گرد کنید
        if '.' in text:
            return int(float(text))
        return int(text)
    except:
        return None

# ============================================================
# توابع تنظیم فونت فارسی برای Matplotlib
# ============================================================
def setup_persian_font():
    """تنظیم فونت فارسی برای matplotlib با استفاده از فونت داخلی"""
    try:
        # مسیر فونت Vazirmatn در پکیج
        import matplotlib.font_manager as fm
        # لیست مسیرهای احتمالی فونت
        font_paths = [
            '/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Regular.ttf',
            '/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Medium.ttf',
            '/usr/share/fonts/opentype/vazirmatn/Vazirmatn-Regular.otf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf'
        ]
        # فونت پیش‌فرض
        plt.rcParams['font.family'] = 'sans-serif'
        plt.rcParams['axes.unicode_minus'] = False
        
        for path in font_paths:
            if os.path.exists(path):
                fm.fontManager.addfont(path)
                prop = fm.FontProperties(fname=path)
                plt.rcParams['font.family'] = prop.get_name()
                logger.info(f"✅ Font loaded: {path}")
                return True
        
        # اگر هیچ فونتی پیدا نشد، از fallback استفاده کن
        logger.warning("⚠️ No Persian font found, using fallback")
        plt.rcParams['font.family'] = 'sans-serif'
        return False
    except Exception as e:
        logger.error(f"❌ Font setup error: {e}")
        plt.rcParams['font.family'] = 'sans-serif'
        return False

# ============================================================
# توابع ایجاد جداول جدید (در صورت عدم وجود)
# ============================================================
def create_tables_if_not_exists():
    """ایجاد جداول جدید در صورت عدم وجود (بدون DROP)"""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # جدول آمار واقعی (برای ثبت آمار توسط سوپرادمین)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS actual_stats (
                    id SERIAL PRIMARY KEY,
                    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                    shamsi_date VARCHAR(10) NOT NULL,
                    deputy_actual BIGINT NOT NULL DEFAULT 0,
                    others_actual BIGINT NOT NULL DEFAULT 0,
                    recorded_by INTEGER REFERENCES users(id),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT unique_branch_actual_date UNIQUE (branch_id, shamsi_date)
                )
            """)
            cur.execute("""
                COMMENT ON TABLE actual_stats IS 'آمار واقعی وصول ثبت شده توسط سوپرادمین';
                COMMENT ON COLUMN actual_stats.branch_id IS 'شناسه شعبه';
                COMMENT ON COLUMN actual_stats.shamsi_date IS 'تاریخ شمسی';
                COMMENT ON COLUMN actual_stats.deputy_actual IS 'مبلغ واقعی معاون';
                COMMENT ON COLUMN actual_stats.others_actual IS 'مبلغ واقعی همکاران';
                COMMENT ON COLUMN actual_stats.recorded_by IS 'ثبت کننده (سوپرادمین)';
            """)
            conn.commit()
            logger.info("✅ Table actual_stats created/verified successfully.")
    except Exception as e:
        logger.error(f"❌ Error creating tables: {e}")
    finally:
        if conn:
            return_db_connection(conn)

# ایجاد جداول در شروع برنامه
create_tables_if_not_exists()

# ============================================================
# توابع تاریخ
# ============================================================
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

def get_iran_time():
    return datetime.now(IRAN_TZ)

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
    month = normalize_digits(month).zfill(2)
    day = normalize_digits(day)
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
    shamsi_str = normalize_digits(shamsi_str)
    parts = shamsi_str.split('/')
    if len(parts) != 3:
        return None
    year, month, day = map(int, parts)
    try:
        return jdatetime.date(year, month, day).togregorian()
    except:
        return None

def is_last_day_of_shamsi_month(shamsi_date_str):
    try:
        parts = shamsi_date_str.split('/')
        year, month, day = map(int, parts)
        if month in [1, 2, 3, 4, 5, 6]:
            days_in_month = 31
        elif month in [7, 8, 9, 10, 11]:
            days_in_month = 30
        else:
            leap_years = [1, 5, 9, 13, 17, 22, 26, 30]
            if year % 33 in leap_years:
                days_in_month = 30
            else:
                days_in_month = 29
        return day == days_in_month
    except:
        return False

# ============================================================
# توابع مدیریت تنظیمات
# ============================================================
def get_feature_setting(key, default='active'):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM feature_settings WHERE key = %s", (key,))
            result = cur.fetchone()
            if result:
                return result[0]
            return default
    except Exception as e:
        logger.error(f"get_feature_setting error: {e}")
        return default
    finally:
        if conn:
            return_db_connection(conn)

def set_feature_setting(key, value):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO feature_settings (key, value, updated_at) 
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """, (key, value, get_iran_time()))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"set_feature_setting error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_bot_status():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def set_bot_status(status):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_auto_reminder_status():
    return get_feature_setting('auto_reminder', 'active') == 'active'

def set_auto_reminder_status(status):
    return set_feature_setting('auto_reminder', 'active' if status else 'inactive')

def get_auto_report_status():
    return get_feature_setting('auto_report', 'active') == 'active'

def set_auto_report_status(status):
    return set_feature_setting('auto_report', 'active' if status else 'inactive')

def get_auto_alert_status():
    return get_feature_setting('auto_alert', 'active') == 'active'

def set_auto_alert_status(status):
    return set_feature_setting('auto_alert', 'active' if status else 'inactive')

def get_auto_scoring_status():
    return get_feature_setting('auto_scoring', 'active') == 'active'

def set_auto_scoring_status(status):
    return set_feature_setting('auto_scoring', 'active' if status else 'inactive')

def get_weekly_report_status():
    return get_feature_setting('weekly_report', 'active') == 'active'

def set_weekly_report_status(status):
    return set_feature_setting('weekly_report', 'active' if status else 'inactive')

def get_monthly_report_status():
    return get_feature_setting('monthly_report', 'active') == 'active'

def set_monthly_report_status(status):
    return set_feature_setting('monthly_report', 'active' if status else 'inactive')

def get_instant_notification_status():
    return get_feature_setting('instant_notification', 'active') == 'active'

def set_instant_notification_status(status):
    return set_feature_setting('instant_notification', 'active' if status else 'inactive')

def get_adaptive_report_status():
    return get_feature_setting('adaptive_report', 'active') == 'active'

def set_adaptive_report_status(status):
    return set_feature_setting('adaptive_report', 'active' if status else 'inactive')

def get_forecast_report_status():
    return get_feature_setting('forecast_report', 'active') == 'active'

def set_forecast_report_status(status):
    return set_feature_setting('forecast_report', 'active' if status else 'inactive')

def get_survey_system_status():
    return get_feature_setting('survey_system', 'active') == 'active'

def set_survey_system_status(status):
    return set_feature_setting('survey_system', 'active' if status else 'inactive')

def get_chart_report_status():
    return get_feature_setting('chart_report', 'active') == 'active'

def set_chart_report_status(status):
    return set_feature_setting('chart_report', 'active' if status else 'inactive')

def get_actual_stats_status():
    return get_feature_setting('actual_stats', 'active') == 'active'

def set_actual_stats_status(status):
    return set_feature_setting('actual_stats', 'active' if status else 'inactive')

def is_holiday(shamsi_date=None):
    if not shamsi_date:
        shamsi_date = get_shamsi_date()
    shamsi_date = normalize_digits(shamsi_date)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM holidays WHERE shamsi_date = %s", (shamsi_date,))
            count = cur.fetchone()[0]
            return count > 0
    except Exception as e:
        logger.error(f"is_holiday error: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)

def add_holiday(shamsi_date, description=""):
    shamsi_date = normalize_digits(shamsi_date)
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def remove_holiday(shamsi_date):
    shamsi_date = normalize_digits(shamsi_date)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM holidays WHERE shamsi_date = %s", (shamsi_date,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"remove_holiday error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_all_holidays(limit=30):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

# ============================================================
# توابع امتیازدهی
# ============================================================
def calculate_score(collection_time, deputy_amount, others_amount, branch_id, shamsi_date):
    total_amount = deputy_amount + others_amount
    score = 0
    hour = collection_time.hour
    if hour < 12:
        score += 3
    elif hour < 15:
        score += 2
    else:
        score += 1
    monthly_avg = get_branch_monthly_avg(branch_id, 30)
    if monthly_avg > 0:
        ratio = total_amount / monthly_avg
        if ratio >= 1.5:
            score += 3
        elif ratio >= 1.2:
            score += 2
        elif ratio >= 0.8:
            score += 1
    else:
        if total_amount >= 5_000_000_000:
            score += 3
        elif total_amount >= 2_000_000_000:
            score += 2
        elif total_amount >= 500_000_000:
            score += 1
    if total_amount > 0 and (deputy_amount / total_amount) > 0.5:
        score += 1
    consecutive_days = get_consecutive_days(branch_id, shamsi_date)
    if consecutive_days >= 7:
        score += 2
    elif consecutive_days >= 3:
        score += 1
    return score

def get_consecutive_days(branch_id, shamsi_date):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shamsi_date
                FROM collections
                WHERE branch_id = %s
                ORDER BY shamsi_date DESC
                LIMIT 30
            """, (branch_id,))
            dates = [row[0] for row in cur.fetchall()]
            if not dates:
                return 0
            target_date = jdatetime.date(*map(int, shamsi_date.split('/')))
            count = 0
            for i in range(1, 30):
                check_date = target_date - timedelta(days=i)
                check_str = f"{check_date.year}/{check_date.month:02d}/{check_date.day:02d}"
                if check_str in dates:
                    count += 1
                else:
                    break
            return count
    except Exception as e:
        logger.error(f"get_consecutive_days error: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)

def save_score(collection_id, score):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scores (collection_id, score, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (collection_id) DO UPDATE SET score = EXCLUDED.score, updated_at = EXCLUDED.created_at
            """, (collection_id, score, get_iran_time()))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"save_score error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_branch_total_score(branch_id, days=30):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT SUM(s.score)
                FROM scores s
                JOIN collections c ON s.collection_id = c.id
                WHERE c.branch_id = %s
                AND c.shamsi_date >= %s
            """, (branch_id, get_shamsi_date(-days)))
            result = cur.fetchone()[0]
            return result or 0
    except Exception as e:
        logger.error(f"get_branch_total_score: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)

def get_all_branch_scores(days=30):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.id, b.name, COALESCE(SUM(s.score), 0) as total_score, COUNT(s.id) as score_count
                FROM branches b
                LEFT JOIN collections c ON b.id = c.branch_id AND c.shamsi_date >= %s
                LEFT JOIN scores s ON c.id = s.collection_id
                GROUP BY b.id, b.name
                ORDER BY total_score DESC
            """, (get_shamsi_date(-days),))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_branch_scores: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

# ============================================================
# توابع مشکلات
# ============================================================
def save_problem(user_id, problem_text, category="general"):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO problems (user_id, problem_text, category, status, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, problem_text, category, 'pending', get_iran_time()))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"save_problem error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_all_problems(status=None, limit=50):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if status:
                cur.execute("""
                    SELECT p.id, u.full_name, u.employee_number, p.problem_text, p.category, p.status, p.created_at
                    FROM problems p
                    JOIN users u ON p.user_id = u.id
                    WHERE p.status = %s
                    ORDER BY p.created_at DESC
                    LIMIT %s
                """, (status, limit))
            else:
                cur.execute("""
                    SELECT p.id, u.full_name, u.employee_number, p.problem_text, p.category, p.status, p.created_at
                    FROM problems p
                    JOIN users u ON p.user_id = u.id
                    ORDER BY p.created_at DESC
                    LIMIT %s
                """, (limit,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_problems: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

def update_problem_status(problem_id, new_status):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE problems 
                SET status = %s, updated_at = %s
                WHERE id = %s
            """, (new_status, get_iran_time(), problem_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"update_problem_status: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

# ============================================================
# توابع نظرسنجی
# ============================================================
def create_survey(title, description, questions, created_by):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO surveys (title, description, questions, created_by, created_at, is_active)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (title, description, json.dumps(questions), created_by, get_iran_time(), True))
            survey_id = cur.fetchone()[0]
            conn.commit()
            return survey_id
    except Exception as e:
        logger.error(f"create_survey error: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            return_db_connection(conn)

def get_active_surveys():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, description, questions, created_by, created_at
                FROM surveys
                WHERE is_active = TRUE
                ORDER BY created_at DESC
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_active_surveys error: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

def submit_survey_response(survey_id, user_id, answers):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO survey_responses (survey_id, user_id, answers, created_at)
                VALUES (%s, %s, %s, %s)
            """, (survey_id, user_id, json.dumps(answers), get_iran_time()))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"submit_survey_response error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_survey_responses(survey_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, answers, created_at
                FROM survey_responses
                WHERE survey_id = %s
            """, (survey_id,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_survey_responses error: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

# ============================================================
# توابع آمار واقعی (Actual Stats)
# ============================================================
def save_actual_stats(branch_id, shamsi_date, deputy_actual, others_actual, user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO actual_stats (branch_id, shamsi_date, deputy_actual, others_actual, recorded_by, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (branch_id, shamsi_date) DO UPDATE SET
                    deputy_actual = EXCLUDED.deputy_actual,
                    others_actual = EXCLUDED.others_actual,
                    recorded_by = EXCLUDED.recorded_by,
                    updated_at = CURRENT_TIMESTAMP
            """, (branch_id, shamsi_date, deputy_actual, others_actual, user_id, get_iran_time()))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"save_actual_stats error: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_actual_stats(branch_id, shamsi_date):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT deputy_actual, others_actual
                FROM actual_stats
                WHERE branch_id = %s AND shamsi_date = %s
            """, (branch_id, shamsi_date))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"get_actual_stats error: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)

def get_actual_stats_for_date(shamsi_date):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.id, b.name, a.deputy_actual, a.others_actual, (a.deputy_actual + a.others_actual) as total_actual
                FROM actual_stats a
                JOIN branches b ON a.branch_id = b.id
                WHERE a.shamsi_date = %s
                ORDER BY b.name
            """, (shamsi_date,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_actual_stats_for_date error: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

def compare_collection_with_actual(branch_id, shamsi_date):
    """مقایسه وصول ثبت شده با آمار واقعی برای یک شعبه در یک تاریخ"""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # وصول ثبت شده توسط معاون
            cur.execute("""
                SELECT deputy_amount, others_amount, total_amount
                FROM collections
                WHERE branch_id = %s AND shamsi_date = %s
            """, (branch_id, shamsi_date))
            collection = cur.fetchone()
            
            # آمار واقعی
            cur.execute("""
                SELECT deputy_actual, others_actual
                FROM actual_stats
                WHERE branch_id = %s AND shamsi_date = %s
            """, (branch_id, shamsi_date))
            actual = cur.fetchone()
            
            if not collection or not actual:
                return None
            
            dep_col, oth_col, total_col = collection
            dep_act, oth_act = actual
            total_act = dep_act + oth_act
            
            # محاسبه درصد تطابق
            if total_act > 0:
                match_percent = (min(total_col, total_act) / max(total_col, total_act)) * 100
            else:
                match_percent = 0 if total_col > 0 else 100
            
            return {
                'deputy_collected': dep_col,
                'others_collected': oth_col,
                'total_collected': total_col,
                'deputy_actual': dep_act,
                'others_actual': oth_act,
                'total_actual': total_act,
                'match_percent': match_percent,
                'diff_deputy': dep_col - dep_act,
                'diff_others': oth_col - oth_act,
                'diff_total': total_col - total_act
            }
    except Exception as e:
        logger.error(f"compare_collection_with_actual error: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)

def get_deputy_match_report(user_id, days=30):
    """گزارش تطابق عملکرد معاون با آمار واقعی در ۳۰ روز اخیر"""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # دریافت شعب معاون
            cur.execute("SELECT branch_id FROM users WHERE id = %s", (user_id,))
            branch = cur.fetchone()
            if not branch:
                return None
            branch_id = branch[0]
            
            shamsi_start = get_shamsi_date(-days)
            cur.execute("""
                SELECT 
                    c.shamsi_date,
                    c.total_amount as collected,
                    a.deputy_actual + a.others_actual as actual,
                    CASE 
                        WHEN (a.deputy_actual + a.others_actual) > 0 
                        THEN ROUND((c.total_amount * 100.0) / (a.deputy_actual + a.others_actual), 2)
                        ELSE 0 
                    END as match_percent
                FROM collections c
                LEFT JOIN actual_stats a ON c.branch_id = a.branch_id AND c.shamsi_date = a.shamsi_date
                WHERE c.branch_id = %s 
                AND c.shamsi_date >= %s
                AND a.deputy_actual IS NOT NULL
                ORDER BY c.shamsi_date DESC
            """, (branch_id, shamsi_start))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_deputy_match_report error: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)

# ============================================================
# توابع گزارش‌های تطبیقی و پیش‌بینی (بهبودیافته)
# ============================================================
def get_adaptive_comparison():
    shamsi_today = get_shamsi_date()
    shamsi_yesterday = get_shamsi_date(-1)
    shamsi_week_ago = get_shamsi_date(-7)
    shamsi_month_ago = get_shamsi_date(-30)
    
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(total_amount) FROM collections WHERE shamsi_date = %s", (shamsi_today,))
            today_total = cur.fetchone()[0] or 0
            cur.execute("SELECT SUM(total_amount) FROM collections WHERE shamsi_date = %s", (shamsi_yesterday,))
            yesterday_total = cur.fetchone()[0] or 0
            cur.execute("SELECT SUM(total_amount) FROM collections WHERE shamsi_date = %s", (shamsi_week_ago,))
            week_ago_total = cur.fetchone()[0] or 0
            cur.execute("SELECT SUM(total_amount) FROM collections WHERE shamsi_date = %s", (shamsi_month_ago,))
            month_ago_total = cur.fetchone()[0] or 0
            
            def calc_change(current, previous):
                if previous == 0:
                    return 0 if current == 0 else 100
                return ((current - previous) / previous) * 100
            
            return {
                'today': today_total,
                'yesterday': yesterday_total,
                'week_ago': week_ago_total,
                'month_ago': month_ago_total,
                'change_yesterday': calc_change(today_total, yesterday_total),
                'change_week': calc_change(today_total, week_ago_total),
                'change_month': calc_change(today_total, month_ago_total)
            }
    except Exception as e:
        logger.error(f"get_adaptive_comparison error: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)

def get_forecast(branch_id=None, days=7):
    """
    پیش‌بینی هوشمند با استفاده از رگرسیون خطی
    اگر branch_id مشخص باشد، برای آن شعبه، وگرنه برای کل استان
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # دریافت داده‌های ۴۵ روز اخیر
            if branch_id:
                cur.execute("""
                    SELECT shamsi_date, total_amount
                    FROM collections
                    WHERE branch_id = %s
                    ORDER BY shamsi_date DESC
                    LIMIT 45
                """, (branch_id,))
            else:
                cur.execute("""
                    SELECT shamsi_date, SUM(total_amount) as total
                    FROM collections
                    GROUP BY shamsi_date
                    ORDER BY shamsi_date DESC
                    LIMIT 45
                """)
            data = cur.fetchall()
            
            # اگر داده‌ها کمتر از ۳ روز باشد، پیش‌بینی ممکن نیست
            if len(data) < 3:
                logger.warning(f"get_forecast: فقط {len(data)} روز داده موجود است (حداقل ۳ روز نیاز است)")
                return None, {'error': 'حداقل ۳ روز داده نیاز است', 'available_days': len(data)}
            
            # تبدیل تاریخ‌ها و مبالغ
            dates = []
            amounts = []
            for row in reversed(data):
                shamsi_str = row[0]
                parts = shamsi_str.split('/')
                if len(parts) == 3:
                    try:
                        year, month, day = map(int, parts)
                        greg = jdatetime.date(year, month, day).togregorian()
                        dates.append(greg.toordinal())
                        amounts.append(float(row[1] or 0))
                    except Exception as e:
                        logger.warning(f"خطا در تبدیل تاریخ {shamsi_str}: {e}")
                        continue
            
            if len(dates) < 3:
                logger.warning(f"get_forecast: بعد از تبدیل فقط {len(dates)} تاریخ معتبر باقی ماند")
                return None, {'error': f'تعداد داده‌های معتبر: {len(dates)} (حداقل ۳ روز نیاز است)'}
            
            # تبدیل به آرایه numpy
            x = np.array(dates)
            y = np.array(amounts)
            
            # وزن‌دهی به روزهای اخیر (وزن بیشتر برای روزهای جدیدتر)
            n = len(x)
            weights = np.exp(np.linspace(0, 1, n))
            weights = weights / weights.sum() * n
            
            # محاسبه رگرسیون خطی وزنی
            x_mean = np.average(x, weights=weights)
            y_mean = np.average(y, weights=weights)
            cov = np.average((x - x_mean) * (y - y_mean), weights=weights)
            var = np.average((x - x_mean) ** 2, weights=weights)
            
            if var == 0:
                return None, {'error': 'داده‌ها تغییرات کافی ندارند'}
            
            slope = cov / var
            intercept = y_mean - slope * x_mean
            
            # محاسبه خطاها و معیارهای ارزیابی
            y_pred_all = slope * x + intercept
            mse = np.mean((y - y_pred_all) ** 2)
            rmse = np.sqrt(mse)
            
            ss_tot = np.sum((y - y_mean) ** 2)
            ss_res = np.sum((y - y_pred_all) ** 2)
            r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
            
            # پیش‌بینی روزهای آینده
            last_date = dates[-1]
            forecast = []
            for i in range(1, days + 1):
                future_date = last_date + i
                predicted = slope * future_date + intercept
                lower = predicted - 1.96 * rmse
                upper = predicted + 1.96 * rmse
                future_greg = datetime.fromordinal(future_date)
                future_shamsi = jdatetime.datetime.fromgregorian(datetime=future_greg)
                shamsi_str = f"{future_shamsi.year}/{future_shamsi.month:02d}/{future_shamsi.day:02d}"
                forecast.append({
                    'date': shamsi_str,
                    'predicted': max(0, predicted),
                    'lower': max(0, lower),
                    'upper': max(0, upper)
                })
            
            # تحلیل روند
            trend_analysis = {
                'slope': slope,
                'r2': r2,
                'rmse': rmse,
                'trend': 'صعودی' if slope > 0 else 'نزولی' if slope < 0 else 'ثابت',
                'strength': 'قوی' if r2 > 0.7 else 'متوسط' if r2 > 0.4 else 'ضعیف',
                'avg_amount': np.mean(y),
                'last_amount': y[-1] if len(y) > 0 else 0,
                'data_count': len(dates)
            }
            
            logger.info(f"پیش‌بینی با موفقیت انجام شد. تعداد داده‌ها: {len(dates)}، R²: {r2:.3f}")
            return forecast, trend_analysis
            
    except Exception as e:
        logger.error(f"get_forecast error: {e}")
        return None, {'error': str(e)}
    finally:
        if conn:
            return_db_connection(conn)

def get_forecast_for_all_branches(days=7):
    """دریافت پیش‌بینی برای همه شعب به صورت جداگانه"""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM branches ORDER BY name")
            branches = cur.fetchall()
            
            results = {}
            for branch_id, branch_name in branches:
                forecast, trend = get_forecast(branch_id, days)
                if forecast and trend:
                    results[branch_name] = {
                        'forecast': forecast,
                        'trend': trend
                    }
            return results
    except Exception as e:
        logger.error(f"get_forecast_for_all_branches error: {e}")
        return {}
    finally:
        if conn:
            return_db_connection(conn)

# ============================================================
# توابع نمودار (با پشتیبانی از فونت فارسی)
# ============================================================
def generate_chart(data, title, x_label, y_label, chart_type='bar', figsize=(10, 6)):
    """تولید تصویر نمودار با پشتیبانی از فارسی"""
    setup_persian_font()
    plt.figure(figsize=figsize)
    
    # بازسازی متن برای نمایش فارسی
    title_fa = arabic_reshaper.reshape(title) if title else ""
    title_fa = get_display(title_fa)
    x_label_fa = arabic_reshaper.reshape(x_label) if x_label else ""
    x_label_fa = get_display(x_label_fa)
    y_label_fa = arabic_reshaper.reshape(y_label) if y_label else ""
    y_label_fa = get_display(y_label_fa)
    
    labels = []
    for lbl in data['labels']:
        reshaped = arabic_reshaper.reshape(str(lbl))
        labels.append(get_display(reshaped))
    
    if chart_type == 'bar':
        plt.bar(labels, data['values'], color='skyblue', edgecolor='navy')
        # افزودن مقادیر روی ستون‌ها
        for i, v in enumerate(data['values']):
            plt.text(i, v + 0.02*max(data['values']), f"{int(v)//1_000_000:,.0f}", ha='center', va='bottom', fontsize=8)
    elif chart_type == 'line':
        plt.plot(labels, data['values'], marker='o', linestyle='-', color='blue', linewidth=2, markersize=8)
        for i, v in enumerate(data['values']):
            plt.text(i, v + 0.02*max(data['values']), f"{int(v)//1_000_000:,.0f}", ha='center', va='bottom', fontsize=8)
    elif chart_type == 'pie':
        # حذف مقادیر صفر از نمودار دایره‌ای
        non_zero = [(l, v) for l, v in zip(labels, data['values']) if v > 0]
        if non_zero:
            labels, values = zip(*non_zero)
            plt.pie(values, labels=labels, autopct='%1.1f%%', startangle=90)
        else:
            plt.pie([1], labels=['داده‌ای وجود ندارد'], colors=['lightgray'])
    elif chart_type == 'horizontal':
        plt.barh(labels, data['values'], color='skyblue', edgecolor='navy')
        for i, v in enumerate(data['values']):
            plt.text(v + 0.02*max(data['values']), i, f"{int(v)//1_000_000:,.0f}", va='center', fontsize=8)
    elif chart_type == 'stacked':
        # برای نمودارهای انباشته
        if 'values2' in data:
            plt.bar(labels, data['values'], label='معاون', color='blue', alpha=0.7)
            plt.bar(labels, data['values2'], label='همکاران', color='orange', alpha=0.7, bottom=data['values'])
            plt.legend()
        else:
            plt.bar(labels, data['values'], color='skyblue')
    
    plt.title(title_fa, fontsize=14, fontweight='bold')
    plt.xlabel(x_label_fa)
    plt.ylabel(y_label_fa)
    plt.xticks(rotation=45, ha='right')
    plt.tight_layout()
    
    img_bytes = io.BytesIO()
    plt.savefig(img_bytes, format='png', dpi=120, bbox_inches='tight')
    plt.close()
    img_bytes.seek(0)
    return img_bytes.getvalue()

def generate_branch_chart(branch_id, days=10):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shamsi_date, total_amount
                FROM collections
                WHERE branch_id = %s
                ORDER BY shamsi_date DESC
                LIMIT %s
            """, (branch_id, days))
            data = cur.fetchall()
            if not data:
                return None
            labels = [get_shamsi_date_formatted(row[0]) for row in reversed(data)]
            values = [row[1] for row in reversed(data)]
            return {
                'labels': labels,
                'values': values
            }
    except Exception as e:
        logger.error(f"generate_branch_chart error: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)

def generate_province_chart(days=10):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT shamsi_date, SUM(total_amount) as total
                FROM collections
                GROUP BY shamsi_date
                ORDER BY shamsi_date DESC
                LIMIT %s
            """, (days,))
            data = cur.fetchall()
            if not data:
                return None
            labels = [get_shamsi_date_formatted(row[0]) for row in reversed(data)]
            values = [row[1] for row in reversed(data)]
            return {
                'labels': labels,
                'values': values
            }
    except Exception as e:
        logger.error(f"generate_province_chart error: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)

def generate_comparison_chart(branch_id, shamsi_date):
    """نمودار مقایسه وصول ثبت شده با آمار واقعی برای یک شعبه"""
    comparison = compare_collection_with_actual(branch_id, shamsi_date)
    if not comparison:
        return None
    
    labels = ['معاون', 'همکاران', 'جمع کل']
    collected = [comparison['deputy_collected'], comparison['others_collected'], comparison['total_collected']]
    actual = [comparison['deputy_actual'], comparison['others_actual'], comparison['total_actual']]
    
    # تنظیم فونت
    setup_persian_font()
    x = np.arange(len(labels))
    width = 0.35
    
    fig, ax = plt.subplots(figsize=(10, 6))
    rects1 = ax.bar(x - width/2, [v//1_000_000 for v in collected], width, label='ثبت شده', color='blue', alpha=0.7)
    rects2 = ax.bar(x + width/2, [v//1_000_000 for v in actual], width, label='واقعی', color='orange', alpha=0.7)
    
    ax.set_ylabel('میلیون ریال')
    ax.set_title('مقایسه وصول ثبت شده با آمار واقعی')
    ax.set_xticks(x)
    ax.set_xticklabels([get_display(arabic_reshaper.reshape(l)) for l in labels])
    ax.legend()
    
    # افزودن درصد تطابق
    ax.text(0.5, -0.15, f"درصد تطابق: {comparison['match_percent']:.1f}%", 
            transform=ax.transAxes, ha='center', fontsize=12, fontweight='bold')
    
    plt.tight_layout()
    img_bytes = io.BytesIO()
    plt.savefig(img_bytes, format='png', dpi=100, bbox_inches='tight')
    plt.close()
    img_bytes.seek(0)
    return img_bytes.getvalue()

def generate_branch_comparison_chart(comparison_data):
    """نمودار مقایسه ای چند شعبه"""
    if not comparison_data:
        return None
    
    setup_persian_font()
    fig, ax = plt.subplots(figsize=(12, 6))
    
    branches = [item[1] for item in comparison_data]
    collected = [item[2] for item in comparison_data]
    actual = [item[3] for item in comparison_data]
    match_pct = [item[4] for item in comparison_data]
    
    x = np.arange(len(branches))
    width = 0.35
    
    rects1 = ax.bar(x - width/2, [c//1_000_000 for c in collected], width, label='ثبت شده', color='blue', alpha=0.7)
    rects2 = ax.bar(x + width/2, [a//1_000_000 for a in actual], width, label='واقعی', color='orange', alpha=0.7)
    
    ax.set_ylabel('میلیون ریال')
    ax.set_title('مقایسه وصول ثبت شده با آمار واقعی - همه شعب')
    ax.set_xticks(x)
    ax.set_xticklabels([get_display(arabic_reshaper.reshape(b[:10])) for b in branches], rotation=45, ha='right')
    ax.legend()
    
    # افزودن درصد تطابق
    for i, pct in enumerate(match_pct):
        ax.text(i, max(collected[i], actual[i])//1_000_000 + 0.5, f"{pct:.0f}%", ha='center', fontsize=9)
    
    plt.tight_layout()
    img_bytes = io.BytesIO()
    plt.savefig(img_bytes, format='png', dpi=100, bbox_inches='tight')
    plt.close()
    img_bytes.seek(0)
    return img_bytes.getvalue()

# ============================================================
# ارسال پیام و عکس
# ============================================================
def send_message(chat_id, text, reply_markup=None, remove_keyboard=False):
    if not get_bot_status() and not is_super_admin_user(chat_id):
        send_maintenance_message(chat_id)
        return None
    if len(text) > 4000:
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            send_message_chunk(chat_id, chunk, reply_markup, remove_keyboard)
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

def send_message_chunk(chat_id, text, reply_markup=None, remove_keyboard=False):
    url = f"{BASE_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if remove_keyboard:
        payload["reply_markup"] = {"remove_keyboard": True}
    elif reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests_session.post(url, json=payload, timeout=15)
    except Exception as e:
        logger.error(f"send_message_chunk error: {e}")

def send_photo(chat_id, photo_bytes, caption="", reply_markup=None):
    if not get_bot_status() and not is_super_admin_user(chat_id):
        send_maintenance_message(chat_id)
        return None
    url = f"{BASE_URL}/sendPhoto"
    files = {'photo': ('chart.png', photo_bytes, 'image/png')}
    data = {'chat_id': chat_id, 'caption': caption[:1024]}
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup)
    try:
        res = requests_session.post(url, data=data, files=files, timeout=20)
        if res.status_code == 200:
            return res.json()
        else:
            logger.error(f"sendPhoto failed: {res.status_code}")
            return None
    except Exception as e:
        logger.error(f"sendPhoto error: {e}")
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

# ============================================================
# کیبوردها
# ============================================================
def get_deputy_keyboard():
    return {
        "keyboard": [
            [{"text": "💰 ثبت وصولی روزانه"}, {"text": "📊 گزارش وصولی"}],
            [{"text": "📈 مقایسه عملکرد"}, {"text": "📋 مشاهده ثبت امروز"}],
            [{"text": "📅 گزارش تاریخ خاص"}, {"text": "📊 تاریخچه کامل"}],
            [{"text": "📝 ثبت یادداشت"}, {"text": "📋 مشاهده یادداشت‌ها"}],
            [{"text": "📝 ثبت مشکل"}, {"text": "📊 نظرسنجی"}],
            [{"text": "ℹ️ درباره توسعه‌دهنده"}, {"text": "🔙 خروج"}],
            [{"text": "❓ راهنما"}]
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
            [{"text": "👥 عملکرد همکاران"}, {"text": "📝 مشاهده یادداشت‌ها"}],
            [{"text": "📊 گزارش تطبیقی"}, {"text": "📈 پیش‌بینی عملکرد"}],
            [{"text": "📊 نمودار استان"}, {"text": "📊 نمودار شعبه"}],
            [{"text": "📊 نمودار تحلیلی"}, {"text": "📊 مقایسه انطباق"}],
            [{"text": "📝 ثبت مشکل"}, {"text": "📊 نظرسنجی"}],
            [{"text": "ℹ️ درباره توسعه‌دهنده"}, {"text": "🔙 خروج"}],
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
            [{"text": "👥 عملکرد همکاران"}, {"text": "📝 مشاهده یادداشت‌ها"}],
            [{"text": "📋 لاگ ورود/خروج"}, {"text": "🔧 کنترل خودکار"}],
            [{"text": "📅 مدیریت تعطیلات"}, {"text": "📨 ارسال پیام به معاونین"}],
            [{"text": "🔄 ریست گزارش‌ها"}, {"text": "⚙️ مدیریت مشکلات"}],
            [{"text": "📊 گزارش هفتگی"}, {"text": "📊 گزارش ماهانه"}],
            [{"text": "📊 گزارش تطبیقی"}, {"text": "📈 پیش‌بینی عملکرد"}],
            [{"text": "📊 نمودار استان"}, {"text": "📊 نمودار شعبه"}],
            [{"text": "📊 نمودار تحلیلی"}, {"text": "📊 مقایسه انطباق"}],
            [{"text": "📊 ثبت آمار واقعی"}, {"text": "📝 ثبت مشکل"}],
            [{"text": "📊 نظرسنجی"}, {"text": "🔧 وضعیت ربات"}],
            [{"text": "ℹ️ درباره توسعه‌دهنده"}, {"text": "🔙 خروج"}],
            [{"text": "❓ راهنما"}]
        ],
        "resize_keyboard": True
    }

def get_cancel_keyboard():
    return {"keyboard": [[{"text": "🔙 انصراف"}]], "resize_keyboard": True}

# ============================================================
# توابع دیتابیس (همه توابع مورد نیاز)
# ============================================================
def find_user_by_employee_number(emp_num):
    emp_num = normalize_digits(emp_num)
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def update_user_telegram_id(user_db_id, chat_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET telegram_id = %s WHERE id = %s", (chat_id, user_db_id))
            conn.commit()
    except Exception as e:
        logger.error(f"update_user_telegram_id: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            return_db_connection(conn)

def find_user_by_telegram_id(chat_id):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def log_user_activity(user_id, action, details=""):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_activity_log (user_id, action, details, created_at)
                VALUES (%s, %s, %s, %s)
            """, (user_id, action, details, get_iran_time()))
            conn.commit()
    except Exception as e:
        logger.error(f"log_user_activity: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            return_db_connection(conn)

def get_user_activity_log(limit=100):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def save_note(collection_id, user_id, note_text):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO notes (collection_id, user_id, note_text, created_at)
                VALUES (%s, %s, %s, %s)
            """, (collection_id, user_id, note_text, get_iran_time()))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"save_note: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_notes_for_collection(collection_id):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_all_notes_with_collection(limit=50):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def save_or_update_collection_with_note(branch_id, deputy_amount_millions, others_amount_millions, shamsi_date, user_id, note_text=None, update_existing=False):
    conn = None
    created_at_iran = get_iran_time()
    deputy_amount = deputy_amount_millions * 1_000_000
    others_amount = others_amount_millions * 1_000_000
    collection_id = None
    
    try:
        conn = get_db_connection()
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
                    return False, None
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
            
            if collection_id and get_instant_notification_status() and not is_holiday(shamsi_date):
                send_instant_notification(branch_id, deputy_amount_millions, others_amount_millions, shamsi_date, user_id)
            
            return True, collection_id
    except Exception as e:
        logger.error(f"save_or_update_collection_with_note: {e}")
        if conn:
            conn.rollback()
        return False, None
    finally:
        if conn:
            return_db_connection(conn)

def send_instant_notification(branch_id, deputy_amount, others_amount, shamsi_date, user_id):
    if not get_instant_notification_status():
        return
    if is_holiday(shamsi_date):
        return
    
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM branches WHERE id = %s", (branch_id,))
            branch_name = cur.fetchone()[0]
            cur.execute("SELECT full_name FROM users WHERE id = %s", (user_id,))
            user_name = cur.fetchone()[0]
            cur.execute("""
                SELECT telegram_id FROM users 
                WHERE role IN ('admin', 'super_admin') AND telegram_id IS NOT NULL
            """)
            admins = cur.fetchall()
            total = deputy_amount + others_amount
            msg = f"🔔 **ثبت وصول جدید**\n━━━━━━━━━━━━━━━━━━\n"
            msg += f"🏢 شعبه: {branch_name}\n"
            msg += f"👤 ثبت‌کننده: {user_name}\n"
            msg += f"📅 تاریخ: {get_shamsi_date_formatted(shamsi_date)}\n"
            msg += f"👤 وصولی معاون: {deputy_amount:,.0f} میلیون ریال\n"
            msg += f"👥 وصولی همکاران: {others_amount:,.0f} میلیون ریال\n"
            msg += f"💰 جمع کل: {total:,.0f} میلیون ریال\n"
            msg += f"⏰ زمان: {get_iran_time().strftime('%H:%M:%S')}"
            for admin in admins:
                chat_id = admin[0]
                if chat_id:
                    send_message(chat_id, msg)
    except Exception as e:
        logger.error(f"send_instant_notification error: {e}")
    finally:
        if conn:
            return_db_connection(conn)

def check_existing_collection(branch_id, shamsi_date):
    shamsi_date = normalize_digits(shamsi_date)
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_branch_10_day_report(branch_id):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_today_province_report(shamsi_date):
    """
    دریافت گزارش امروز استان با LEFT JOIN برای نمایش همه شعب
    حتی شعب بدون وصول با مقدار 0 نمایش داده می‌شوند
    """
    shamsi_date = normalize_digits(shamsi_date)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    b.name, 
                    COALESCE(c.deputy_amount, 0) as deputy_amount,
                    COALESCE(c.others_amount, 0) as others_amount,
                    COALESCE(c.total_amount, 0) as total_amount
                FROM branches b
                LEFT JOIN collections c ON c.branch_id = b.id AND c.shamsi_date = %s
                ORDER BY COALESCE(c.total_amount, 0) DESC
            """, (shamsi_date,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_today_province_report: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

def get_province_10_day_report():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_top_5_branches():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_today_statistics():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_yesterday_vs_today():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_detailed_report(shamsi_date):
    shamsi_date = normalize_digits(shamsi_date)
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_branch_performance(branch_id, days=10):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_daily_comparison():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_deputy_vs_others_ratio():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_report_by_date(shamsi_date):
    shamsi_date = normalize_digits(shamsi_date)
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_branch_report_by_date(branch_id, shamsi_date):
    shamsi_date = normalize_digits(shamsi_date)
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_branch_full_history(branch_id):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_best_worst_days(limit=5):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_all_users():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def update_user_role(user_id, new_role):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"update_user_role: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def update_user_branch(user_id, branch_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET branch_id = %s WHERE id = %s", (branch_id, user_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"update_user_branch: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def delete_user(user_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"delete_user: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_all_branches():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM branches ORDER BY name")
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_branches: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

def get_all_collections(limit=100):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def delete_collection(collection_id):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM collections WHERE id = %s", (collection_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"delete_collection: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def update_collection(collection_id, deputy_amount, others_amount):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def reset_all_collections():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM collections")
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"reset_all_collections: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)

def get_all_deputies():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_log_file_path():
    return "bot.log"

def get_branch_weekly_avg(branch_id, days=7):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_branch_monthly_avg(branch_id, days=30):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_today_performance_analysis():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_drop_alert_branches():
    conn = None
    try:
        conn = get_db_connection()
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
                GROUP BY b.id, b.name, c.total_amount
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
        if conn:
            return_db_connection(conn)

def get_branch_trend(branch_id, days=3):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_deputy_performance_report(user_id, days=30):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_unreported_branches():
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            shamsi_today = get_shamsi_date()
            cur.execute("""
                SELECT DISTINCT b.id, b.name, u.full_name, u.telegram_id
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
        if conn:
            return_db_connection(conn)

def get_all_admins():
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def get_branch_monthly_avg_for_name(branch_name):
    conn = None
    try:
        conn = get_db_connection()
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
        if conn:
            return_db_connection(conn)

def generate_management_analysis(analysis):
    lines = []
    today_total = analysis['today_total']
    branch_data = analysis['branch_data']
    deputy_total = analysis['deputy_total']
    others_total = analysis['others_total']
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

# ============================================================
# تابع جدید برای گزارش عملکرد همکاران (مجموع کل دوره)
# ============================================================
def get_others_performance_summary():
    """
    دریافت مجموع عملکرد همکاران (others_amount) برای کل دوره (همه تاریخ)
    به تفکیک هر شعبه
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    b.id,
                    b.name,
                    COALESCE(SUM(c.others_amount), 0) as total_others,
                    COALESCE(SUM(c.total_amount), 0) as total_branch,
                    COUNT(c.id) as report_days
                FROM branches b
                LEFT JOIN collections c ON b.id = c.branch_id
                GROUP BY b.id, b.name
                ORDER BY total_others DESC
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_others_performance_summary error: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

# ============================================================
# تابع برای دریافت داده‌های نمودار تحلیلی
# ============================================================
def get_analytical_chart_data(chart_type, days=10):
    """دریافت داده برای انواع نمودارهای تحلیلی"""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            if chart_type == 'branch_comparison':
                # مقایسه شعب برتر ۱۰ روز اخیر
                cur.execute("""
                    SELECT b.name, SUM(c.total_amount) as total
                    FROM collections c
                    JOIN branches b ON c.branch_id = b.id
                    WHERE c.shamsi_date >= %s
                    GROUP BY b.name
                    ORDER BY total DESC
                    LIMIT 10
                """, (get_shamsi_date(-days),))
                data = cur.fetchall()
                return {
                    'labels': [row[0] for row in data],
                    'values': [row[1] for row in data]
                }
            elif chart_type == 'deputy_others_ratio':
                # نسبت وصول معاون و همکاران
                cur.execute("""
                    SELECT 
                        SUM(deputy_amount) as deputy_total,
                        SUM(others_amount) as others_total
                    FROM collections
                    WHERE shamsi_date >= %s
                """, (get_shamsi_date(-days),))
                row = cur.fetchone()
                return {
                    'labels': ['معاونین', 'همکاران'],
                    'values': [row[0] or 0, row[1] or 0]
                }
            elif chart_type == 'daily_trend':
                # روند روزانه
                cur.execute("""
                    SELECT shamsi_date, SUM(total_amount) as total
                    FROM collections
                    WHERE shamsi_date >= %s
                    GROUP BY shamsi_date
                    ORDER BY shamsi_date DESC
                    LIMIT %s
                """, (get_shamsi_date(-days), days))
                data = cur.fetchall()
                return {
                    'labels': [get_shamsi_date_formatted(row[0]) for row in reversed(data)],
                    'values': [row[1] for row in reversed(data)]
                }
            elif chart_type == 'match_analysis':
                # تحلیل تطابق با آمار واقعی
                cur.execute("""
                    SELECT 
                        b.name,
                        COALESCE(AVG(CASE 
                            WHEN a.deputy_actual + a.others_actual > 0 
                            THEN (c.total_amount * 100.0) / (a.deputy_actual + a.others_actual)
                            ELSE 0 
                        END), 0) as match_percent
                    FROM branches b
                    LEFT JOIN collections c ON b.id = c.branch_id
                    LEFT JOIN actual_stats a ON b.id = a.branch_id AND c.shamsi_date = a.shamsi_date
                    WHERE c.shamsi_date >= %s
                    GROUP BY b.name
                    ORDER BY match_percent DESC
                """, (get_shamsi_date(-days),))
                data = cur.fetchall()
                return {
                    'labels': [row[0] for row in data],
                    'values': [row[1] for row in data]
                }
            else:
                return None
    except Exception as e:
        logger.error(f"get_analytical_chart_data error: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)

# ============================================================
# توابع ارسال خودکار
# ============================================================
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
    if not get_bot_status() or not get_auto_report_status():
        return
    shamsi_today = get_shamsi_date()
    if is_holiday(shamsi_today):
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
    if not get_bot_status() or not get_auto_reminder_status():
        return
    shamsi_today = get_shamsi_date()
    if is_holiday(shamsi_today):
        return
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

def check_and_send_drop_alerts():
    if not get_bot_status() or not get_auto_alert_status():
        return
    shamsi_today = get_shamsi_date()
    if is_holiday(shamsi_today):
        return
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

def check_and_auto_score():
    if not get_bot_status() or not get_auto_scoring_status():
        return
    shamsi_today = get_shamsi_date()
    if is_holiday(shamsi_today):
        return
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.branch_id, c.deputy_amount, c.others_amount, c.created_at, c.shamsi_date
                FROM collections c
                LEFT JOIN scores s ON c.id = s.collection_id
                WHERE c.shamsi_date = %s AND s.id IS NULL
            """, (shamsi_today,))
            collections_without_score = cur.fetchall()
            for col in collections_without_score:
                col_id, branch_id, deputy_amount, others_amount, created_at, shamsi_date = col
                score = calculate_score(created_at, deputy_amount, others_amount, branch_id, shamsi_date)
                save_score(col_id, score)
    except Exception as e:
        logger.error(f"check_and_auto_score error: {e}")
    finally:
        if conn:
            return_db_connection(conn)

def generate_weekly_report():
    shamsi_today = get_shamsi_date()
    shamsi_week_ago = get_shamsi_date(-7)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    b.id,
                    b.name,
                    COUNT(c.id) as report_count,
                    COALESCE(SUM(c.total_amount), 0) as total_amount,
                    COALESCE(AVG(c.total_amount), 0) as avg_amount,
                    COALESCE(COUNT(CASE WHEN EXTRACT(HOUR FROM c.created_at) < 12 THEN 1 END), 0) as early_count,
                    COALESCE(COUNT(CASE WHEN EXTRACT(HOUR FROM c.created_at) >= 12 AND EXTRACT(HOUR FROM c.created_at) < 15 THEN 1 END), 0) as on_time_count,
                    COALESCE(COUNT(CASE WHEN EXTRACT(HOUR FROM c.created_at) >= 15 THEN 1 END), 0) as late_count
                FROM branches b
                LEFT JOIN collections c ON b.id = c.branch_id AND c.shamsi_date >= %s AND c.shamsi_date <= %s
                GROUP BY b.id, b.name
                ORDER BY total_amount DESC
            """, (shamsi_week_ago, shamsi_today))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"generate_weekly_report: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

def generate_monthly_report():
    shamsi_today = get_shamsi_date()
    shamsi_month_ago = get_shamsi_date(-30)
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    b.id,
                    b.name,
                    COUNT(c.id) as report_count,
                    COALESCE(SUM(c.total_amount), 0) as total_amount,
                    COALESCE(AVG(c.total_amount), 0) as avg_amount,
                    COALESCE(COUNT(CASE WHEN EXTRACT(HOUR FROM c.created_at) < 12 THEN 1 END), 0) as early_count,
                    COALESCE(COUNT(CASE WHEN EXTRACT(HOUR FROM c.created_at) >= 12 AND EXTRACT(HOUR FROM c.created_at) < 15 THEN 1 END), 0) as on_time_count,
                    COALESCE(COUNT(CASE WHEN EXTRACT(HOUR FROM c.created_at) >= 15 THEN 1 END), 0) as late_count
                FROM branches b
                LEFT JOIN collections c ON b.id = c.branch_id AND c.shamsi_date >= %s AND c.shamsi_date <= %s
                GROUP BY b.id, b.name
                ORDER BY total_amount DESC
            """, (shamsi_month_ago, shamsi_today))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"generate_monthly_report: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

def send_weekly_report_to_all():
    if not get_bot_status() or not get_weekly_report_status():
        return
    shamsi_today = get_shamsi_date()
    if is_holiday(shamsi_today):
        return
    report_data = generate_weekly_report()
    if not report_data:
        return
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id, full_name, role FROM users WHERE telegram_id IS NOT NULL")
            users = cur.fetchall()
    finally:
        if conn:
            return_db_connection(conn)
    msg = f"📊 **گزارش هفتگی عملکرد شعب**\n"
    msg += f"📅 بازه: {get_shamsi_date_formatted(get_shamsi_date(-7))} تا {get_shamsi_date_formatted(shamsi_today)}\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n\n"
    total_all = 0
    for idx, row in enumerate(report_data[:10], 1):
        branch_id, name, count, total, avg, early, on_time, late = row
        total_all += total
        msg += f"{idx}. 🏢 {name}\n"
        msg += f"   📊 تعداد گزارش: {count}\n"
        msg += f"   💰 کل وصول: {total//1_000_000:,.0f} میلیون ریال\n"
        msg += f"   📈 میانگین: {avg//1_000_000:,.0f} میلیون ریال\n"
        msg += f"   ⏰ ثبت به‌موقع: {early} (قبل ۱۲) / {on_time} (۱۲-۱۵) / دیر: {late}\n\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"💰 جمع کل وصول استان: {total_all//1_000_000:,.0f} میلیون ریال"
    for user in users:
        chat_id = user[0]
        if chat_id:
            send_message(chat_id, msg)

def send_monthly_report_to_all():
    if not get_bot_status() or not get_monthly_report_status():
        return
    shamsi_today = get_shamsi_date()
    if not is_last_day_of_shamsi_month(shamsi_today):
        return
    if is_holiday(shamsi_today):
        return
    report_data = generate_monthly_report()
    if not report_data:
        return
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id, full_name, role FROM users WHERE telegram_id IS NOT NULL")
            users = cur.fetchall()
    finally:
        if conn:
            return_db_connection(conn)
    msg = f"📊 **گزارش ماهانه عملکرد شعب**\n"
    msg += f"📅 بازه: {get_shamsi_date_formatted(get_shamsi_date(-30))} تا {get_shamsi_date_formatted(shamsi_today)}\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n\n"
    total_all = 0
    for idx, row in enumerate(report_data[:10], 1):
        branch_id, name, count, total, avg, early, on_time, late = row
        total_all += total
        msg += f"{idx}. 🏢 {name}\n"
        msg += f"   📊 تعداد گزارش: {count}\n"
        msg += f"   💰 کل وصول: {total//1_000_000:,.0f} میلیون ریال\n"
        msg += f"   📈 میانگین: {avg//1_000_000:,.0f} میلیون ریال\n"
        msg += f"   ⏰ ثبت به‌موقع: {early} (قبل ۱۲) / {on_time} (۱۲-۱۵) / دیر: {late}\n\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"💰 جمع کل وصول استان: {total_all//1_000_000:,.0f} میلیون ریال"
    for user in users:
        chat_id = user[0]
        if chat_id:
            send_message(chat_id, msg)

# ============================================================
# پردازش پیام‌ها (نسخه نهایی با تمام اصلاحات)
# ============================================================
def handle_message(message):
    try:
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()
        
        if not get_bot_status() and not is_super_admin_user(chat_id):
            send_maintenance_message(chat_id)
            return
        
        user_state = user_states.get(chat_id, {"state": "LOGGED_OUT"})
        current_state = user_state.get("state", "LOGGED_OUT")
        
        # ===== ورود =====
        if current_state == "LOGGED_OUT" or current_state == "WAITING_FOR_EMP_NUM":
            if current_state != "WAITING_FOR_EMP_NUM":
                user_states[chat_id] = {"state": "WAITING_FOR_EMP_NUM"}
                send_message(chat_id, "👋 سلام! به ربات وصول مطالبات استان زنجان خوش آمدید.\n\n🔐 لطفاً شماره کارمندی خود را ارسال کنید:", remove_keyboard=True)
                return
            
            normalized_text = normalize_digits(text)
            if not re.match(r'^[0-9]+$', normalized_text):
                send_message(chat_id, "❌ لطفاً شماره کارمندی را فقط با **اعداد انگلیسی** وارد کنید.\nمثال: ۱۲۳۴۵۶")
                return
            
            emp_user = find_user_by_employee_number(normalized_text)
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
                        f"🏭 واحد: {branch_name or 'بدون شعبه'}\n"
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
        
        # ===== رمز سوپرادمین =====
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
                        f"🏭 واحد: {temp_data['branch_name'] or 'بدون شعبه'}\n"
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
        
        # ===== بازیابی user_data =====
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

        # ===== وضعیت‌های ورودی (ثبت مبلغ و یادداشت) =====
        if current_state == "WAITING_FOR_DEPUTY_AMOUNT":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                if is_super_admin:
                    keyboard = get_super_admin_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", keyboard)
                return
            try:
                amount = parse_number(text)
                if amount is None or amount < 0:
                    raise ValueError
                user_states[chat_id]["state"] = "WAITING_FOR_OTHERS_AMOUNT"
                user_states[chat_id]["deputy_amount"] = amount
                user_states[chat_id]["edit_mode"] = user_state.get("edit_mode", False)
                send_message(chat_id, "✏️ اکنون میزان وصولی سایر همکاران شعبه را به **میلیون ریال** وارد کنید:", get_cancel_keyboard())
            except ValueError:
                send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت (میلیون ریال) وارد کنید.\nمثال: ۴۷۰۰ برای ۴.۷ میلیارد ریال")
            return
        
        elif current_state == "WAITING_FOR_OTHERS_AMOUNT":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                if is_super_admin:
                    keyboard = get_super_admin_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", keyboard)
                return
            try:
                others_amount = parse_number(text)
                if others_amount is None or others_amount < 0:
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
                success, collection_id = save_or_update_collection_with_note(
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
                if is_super_admin:
                    keyboard = get_super_admin_keyboard()
                send_message(chat_id, msg, keyboard)
                return
            else:
                data = user_state.get("collection_data", {})
                note_text = text
                success, collection_id = save_or_update_collection_with_note(
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
                if is_super_admin:
                    keyboard = get_super_admin_keyboard()
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
                if is_super_admin:
                    keyboard = get_super_admin_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", keyboard)
            return

        # ===== گزارش تاریخ خاص برای معاونین =====
        if role == 'deputy' and current_state == "WAITING_FOR_BRANCH_DATE":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                send_message(chat_id, "❌ عملیات لغو شد.", get_deputy_keyboard())
                return
            shamsi_date = normalize_digits(text)
            if re.match(r'^\d{4}/\d{2}/\d{2}$', shamsi_date):
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
                    send_message(chat_id, f"📭 هیچ داده‌ای برای تاریخ {get_shamsi_date_formatted(shamsi_date)} یافت نشد.", get_deputy_keyboard())
            else:
                send_message(chat_id, "❌ فرمت تاریخ را به صورت YYYY/MM/DD وارد کنید (مثلاً 1403/01/15).")
                return
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ===== گزارش تاریخ خاص برای ادمین و سوپرادمین =====
        if (role == 'admin' or is_super_admin) and current_state == "WAITING_FOR_ADMIN_DATE":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.", keyboard)
                return
            shamsi_date = normalize_digits(text)
            if re.match(r'^\d{4}/\d{2}/\d{2}$', shamsi_date):
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
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, f"📭 هیچ داده‌ای برای تاریخ {get_shamsi_date_formatted(shamsi_date)} یافت نشد.", keyboard)
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
                "   • ثبت و مشاهده یادداشت‌ها\n"
                "   • ثبت مشکل\n"
                "   • شرکت در نظرسنجی‌ها\n"
                "   • درباره توسعه‌دهنده\n\n"
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
                "   • گزارش عملکرد همکاران (مجموع کل دوره)\n"
                "   • گزارش تطبیقی (مقایسه با دوره قبل)\n"
                "   • پیش‌بینی عملکرد (تحلیل روند هوشمند)\n"
                "   • نمودارهای تصویری (استان و شعبه)\n"
                "   • نمودارهای تحلیلی متنوع\n"
                "   • مقایسه انطباق با آمار واقعی\n"
                "   • مشاهده یادداشت‌ها\n"
                "   • ثبت مشکل\n"
                "   • شرکت در نظرسنجی‌ها\n"
                "   • درباره توسعه‌دهنده\n\n"
                "🔹 **سوپرادمین:**\n"
                "   • مدیریت کاربران و گزارش‌ها\n"
                "   • فعال/غیرفعال کردن ربات\n"
                "   • کنترل اعمال خودکار\n"
                "   • ریست کردن گزارش‌ها\n"
                "   • ارسال پیام به معاونین\n"
                "   • مدیریت تعطیلات\n"
                "   • مشاهده لاگ کامل فعالیت‌ها\n"
                "   • مدیریت مشکلات ثبت شده\n"
                "   • ارسال گزارش هفتگی و ماهانه\n"
                "   • فعال/غیرفعال کردن قابلیت‌ها\n"
                "   • ایجاد و مدیریت نظرسنجی‌ها\n"
                "   • ثبت آمار واقعی وصول\n"
                "   • مشاهده نمودارهای تحلیلی و انطباق\n\n"
                "💰 **واحد پول:** تمام مبالغ به **میلیون ریال** است.\n"
                "🔸 در هر مرحله می‌توانید با دکمه «انصراف» به منو برگردید.\n"
                "🔸 برای خروج کامل، گزینه «خروج» را انتخاب کنید."
            )
            keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
            if is_super_admin:
                keyboard = get_super_admin_keyboard()
            send_message(chat_id, help_text, keyboard)
            return

        # ===== درباره توسعه‌دهنده =====
        if text == "ℹ️ درباره توسعه‌دهنده":
            about_msg = (
                "🤖 **ربات وصول مطالبات استان زنجان**\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "این ربات توسط **سید فرهاد سید حسینی**\n"
                "کارشناس حقوقی مدیریت شعب استان زنجان\n\n"
                "با حمایت‌های **آقای هادی بیگدلی**\n"
                "معاونت محترم وقت اعتباری منطقه\n\n"
                "در تابستان سال ۱۴۰۵ توسعه یافته است.\n\n"
                "📅 نسخه: ۸.۲\n"
                "📧 پشتیبانی: farhad.s.hosseini@gmail.com"
            )
            keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
            if is_super_admin:
                keyboard = get_super_admin_keyboard()
            send_message(chat_id, about_msg, keyboard)
            return

        # ===== ثبت مشکل =====
        if text == "📝 ثبت مشکل":
            user_states[chat_id]["state"] = "WAITING_FOR_PROBLEM"
            send_message(chat_id, "📝 لطفاً مشکل یا پیشنهاد خود را به صورت کامل بنویسید:\n\n(مثال: در ثبت وصول امروز خطایی رخ داد...)", get_cancel_keyboard())
            return

        if current_state == "WAITING_FOR_PROBLEM":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                if is_super_admin:
                    keyboard = get_super_admin_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.", keyboard)
                return
            if save_problem(user_db_id, text, "general"):
                send_message(chat_id, "✅ مشکل شما با موفقیت ثبت شد. تیم پشتیبانی در اسرع وقت بررسی خواهد کرد.", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                log_user_activity(user_db_id, "add_problem", f"ثبت مشکل: {text[:50]}...")
            else:
                send_message(chat_id, "❌ خطا در ثبت مشکل. لطفاً مجدداً تلاش کنید.", get_cancel_keyboard())
                return
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ============================================================
        # بخش نظرسنجی (با رفع باگ‌های بحرانی)
        # ============================================================
        if text == "📊 نظرسنجی":
            if not get_survey_system_status():
                send_message(chat_id, "🔴 سیستم نظرسنجی در حال حاضر غیرفعال است.", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                return
            
            if is_super_admin:
                keyboard = {
                    "keyboard": [
                        [{"text": "➕ ایجاد نظرسنجی جدید"}],
                        [{"text": "📋 مشاهده نظرسنجی‌ها"}],
                        [{"text": "📊 نتایج نظرسنجی"}],
                        [{"text": "🔙 انصراف"}]
                    ],
                    "resize_keyboard": True
                }
                send_message(chat_id, "📊 **مدیریت نظرسنجی‌ها**\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:", keyboard)
                return
            else:
                surveys = get_active_surveys()
                if not surveys:
                    send_message(chat_id, "📭 هیچ نظرسنجی فعالی وجود ندارد.", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                    return
                msg = "📋 **نظرسنجی‌های فعال**\n━━━━━━━━━━━━━━━━━━\n"
                for s in surveys:
                    survey_id, title, description, questions, created_by, created_at = s
                    msg += f"📌 {title}\n"
                    msg += f"📝 {description[:100]}...\n"
                    msg += f"برای شرکت: /survey {survey_id}\n\n"
                send_message(chat_id, msg, get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                return

        # ===== ایجاد نظرسنجی =====
        if text == "➕ ایجاد نظرسنجی جدید" and is_super_admin:
            user_states[chat_id]["state"] = "WAITING_FOR_SURVEY_TITLE"
            send_message(chat_id, "📝 لطفاً **عنوان نظرسنجی** را وارد کنید:", get_cancel_keyboard())
            return

        if current_state == "WAITING_FOR_SURVEY_TITLE" and is_super_admin:
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                return
            user_states[chat_id]["survey_title"] = text
            user_states[chat_id]["state"] = "WAITING_FOR_SURVEY_DESCRIPTION"
            send_message(chat_id, "📝 لطفاً **توضیحات نظرسنجی** را وارد کنید (اختیاری، می‌توانید 'ندارد' بزنید):", get_cancel_keyboard())
            return

        if current_state == "WAITING_FOR_SURVEY_DESCRIPTION" and is_super_admin:
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                return
            user_states[chat_id]["survey_description"] = text if text != "ندارد" else ""
            user_states[chat_id]["state"] = "WAITING_FOR_SURVEY_QUESTIONS"
            send_message(chat_id, "📝 لطفاً **سوالات** نظرسنجی را به فرمت JSON وارد کنید.\n\n"
                                "مثال برای سوالات:\n"
                                "```json\n"
                                "[\n"
                                "  {\"question\": \"سوال اول؟\", \"type\": \"text\"},\n"
                                "  {\"question\": \"سوال دوم؟\", \"type\": \"choice\", \"options\": [\"گزینه ۱\", \"گزینه ۲\", \"گزینه ۳\"]}\n"
                                "]\n"
                                "```\n\n"
                                "نکته: type می‌تواند `text` یا `choice` باشد.", get_cancel_keyboard())
            return

        if current_state == "WAITING_FOR_SURVEY_QUESTIONS" and is_super_admin:
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                return
            try:
                questions = json.loads(text)
                if not isinstance(questions, list) or len(questions) == 0:
                    raise ValueError("سوالات باید یک آرایه باشند.")
                for q in questions:
                    if 'question' not in q:
                        raise ValueError("هر سوال باید دارای کلید 'question' باشد.")
                    if 'type' not in q:
                        raise ValueError("هر سوال باید دارای کلید 'type' باشد.")
                    if q['type'] not in ['text', 'choice']:
                        raise ValueError("نوع سوال باید 'text' یا 'choice' باشد.")
                    if q['type'] == 'choice' and 'options' not in q:
                        raise ValueError("سوالات انتخابی باید دارای کلید 'options' باشند.")
                title = user_state.get("survey_title", "نظرسنجی جدید")
                description = user_state.get("survey_description", "")
                survey_id = create_survey(title, description, questions, user_db_id)
                if survey_id:
                    send_message(chat_id, f"✅ نظرسنجی با موفقیت ایجاد شد. (شناسه: {survey_id})", get_super_admin_keyboard())
                    log_user_activity(user_db_id, "create_survey", f"ایجاد نظرسنجی {survey_id} - {title}")
                else:
                    send_message(chat_id, "❌ خطا در ایجاد نظرسنجی.", get_super_admin_keyboard())
            except json.JSONDecodeError:
                send_message(chat_id, "❌ فرمت JSON نامعتبر. لطفاً مجدداً تلاش کنید.", get_cancel_keyboard())
                return
            except Exception as e:
                send_message(chat_id, f"❌ خطا: {e}", get_cancel_keyboard())
                return
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ===== مشاهده نظرسنجی‌ها =====
        if text == "📋 مشاهده نظرسنجی‌ها" and is_super_admin:
            surveys = get_active_surveys()
            if not surveys:
                send_message(chat_id, "📭 هیچ نظرسنجی فعالی وجود ندارد.", get_super_admin_keyboard())
                return
            msg = "📋 **لیست نظرسنجی‌های فعال**\n━━━━━━━━━━━━━━━━━━\n"
            for s in surveys:
                survey_id, title, description, questions, created_by, created_at = s
                msg += f"🆔 {survey_id} | {title}\n"
                msg += f"📝 {description[:50]}...\n"
                responses = get_survey_responses(survey_id)
                msg += f"📊 تعداد پاسخ‌ها: {len(responses)}\n\n"
            send_message(chat_id, msg, get_super_admin_keyboard())
            return

        # ===== نتایج نظرسنجی (رفع باگ بحرانی) =====
        if text == "📊 نتایج نظرسنجی" and is_super_admin:
            user_states[chat_id]["state"] = "WAITING_FOR_SURVEY_RESULTS"
            send_message(chat_id, "📊 لطفاً **شناسه نظرسنجی** را وارد کنید تا نتایج آن را مشاهده کنید:", get_cancel_keyboard())
            return

        if current_state == "WAITING_FOR_SURVEY_RESULTS" and is_super_admin:
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                return
            try:
                survey_id = int(normalize_digits(text.strip()))
                conn = None
                try:
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute("SELECT title, questions FROM surveys WHERE id = %s", (survey_id,))
                        result = cur.fetchone()
                        if not result:
                            send_message(chat_id, "❌ نظرسنجی یافت نشد.", get_super_admin_keyboard())
                            return
                        title, questions_json = result
                        questions = json.loads(questions_json)
                        responses = get_survey_responses(survey_id)
                        if not responses:
                            send_message(chat_id, f"📭 نظرسنجی '{title}' هنوز پاسخی ندارد.", get_super_admin_keyboard())
                            return
                        msg = f"📊 **نتایج نظرسنجی: {title}**\n━━━━━━━━━━━━━━━━━━\n\n"
                        msg += f"📊 تعداد کل پاسخ‌ها: {len(responses)}\n\n"
                        for idx, q in enumerate(questions):
                            msg += f"❓ سوال {idx+1}: {q['question']}\n"
                            if q['type'] == 'text':
                                answers_text = []
                                for r in responses:
                                    answers = json.loads(r[1])
                                    if idx < len(answers):
                                        answers_text.append(answers[idx])
                                if answers_text:
                                    msg += f"پاسخ‌ها:\n"
                                    for i, ans in enumerate(answers_text[:5], 1):
                                        msg += f"   {i}. {ans}\n"
                                    if len(answers_text) > 5:
                                        msg += f"   ... و {len(answers_text)-5} پاسخ دیگر\n"
                                else:
                                    msg += "پاسخی برای این سوال ثبت نشده است.\n"
                            elif q['type'] == 'choice':
                                options = q['options']
                                counts = {opt: 0 for opt in options}
                                for r in responses:
                                    answers = json.loads(r[1])
                                    if idx < len(answers):
                                        choice = answers[idx]
                                        if choice in counts:
                                            counts[choice] += 1
                                msg += f"نتایج:\n"
                                for opt, count in counts.items():
                                    percent = (count / len(responses)) * 100
                                    msg += f"   • {opt}: {count} ({percent:.1f}%)\n"
                            msg += "\n"
                        send_message(chat_id, msg, get_super_admin_keyboard())
                except Exception as e:
                    logger.error(f"Survey results error: {e}")
                    send_message(chat_id, f"❌ خطا در دریافت نتایج: {e}", get_super_admin_keyboard())
                finally:
                    if conn:
                        return_db_connection(conn)
            except ValueError:
                send_message(chat_id, "❌ شناسه را به صورت عدد وارد کنید.", get_cancel_keyboard())
                return
            except Exception as e:
                send_message(chat_id, f"❌ خطا: {e}", get_super_admin_keyboard())
                return
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ===== شرکت در نظرسنجی (رفع باگ نشتی کانکشن) =====
        if text.startswith("/survey"):
            parts = text.split()
            if len(parts) == 2:
                try:
                    survey_id = int(normalize_digits(parts[1]))
                    conn = None
                    try:
                        conn = get_db_connection()
                        with conn.cursor() as cur:
                            cur.execute("SELECT title, questions FROM surveys WHERE id = %s AND is_active = TRUE", (survey_id,))
                            result = cur.fetchone()
                            if not result:
                                send_message(chat_id, "❌ نظرسنجی یافت نشد یا غیرفعال است.", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                                return
                            title, questions_json = result
                            questions = json.loads(questions_json)
                            user_states[chat_id]["survey_id"] = survey_id
                            user_states[chat_id]["survey_questions"] = questions
                            user_states[chat_id]["survey_answers"] = []
                            user_states[chat_id]["survey_index"] = 0
                            user_states[chat_id]["state"] = "WAITING_FOR_SURVEY_ANSWER"
                            q = questions[0]
                            msg = f"📊 **نظرسنجی: {title}**\n━━━━━━━━━━━━━━━━━━\n\n"
                            msg += f"سوال {1} از {len(questions)}:\n"
                            msg += f"❓ {q['question']}\n"
                            if q['type'] == 'text':
                                msg += "📝 پاسخ خود را به صورت متن ارسال کنید."
                            elif q['type'] == 'choice':
                                options = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(q['options'])])
                                msg += f"گزینه‌ها:\n{options}\n\nعدد گزینه مورد نظر را وارد کنید."
                            send_message(chat_id, msg, get_cancel_keyboard())
                    except Exception as e:
                        logger.error(f"Survey participation error: {e}")
                        send_message(chat_id, f"❌ خطا: {e}", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                    finally:
                        if conn:
                            return_db_connection(conn)
                except Exception as e:
                    send_message(chat_id, f"❌ خطا: {e}", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
            else:
                send_message(chat_id, "❌ فرمت: /survey [survey_id]", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
            return

        if current_state == "WAITING_FOR_SURVEY_ANSWER":
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                if is_super_admin:
                    keyboard = get_super_admin_keyboard()
                send_message(chat_id, "❌ نظرسنجی لغو شد.", keyboard)
                return
            survey_id = user_state.get("survey_id")
            questions = user_state.get("survey_questions", [])
            index = user_state.get("survey_index", 0)
            answers = user_state.get("survey_answers", [])
            
            if index >= len(questions):
                if submit_survey_response(survey_id, user_db_id, answers):
                    send_message(chat_id, "✅ پاسخ‌های شما با موفقیت ثبت شد. با تشکر از مشارکت شما!", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                    log_user_activity(user_db_id, "survey_submit", f"شرکت در نظرسنجی {survey_id}")
                else:
                    send_message(chat_id, "❌ خطا در ثبت پاسخ‌ها.", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                user_states[chat_id]["state"] = "LOGGED_IN"
                return
            
            q = questions[index]
            if q['type'] == 'choice':
                try:
                    choice = int(normalize_digits(text.strip()))
                    if 1 <= choice <= len(q['options']):
                        answers.append(q['options'][choice-1])
                        user_states[chat_id]["survey_answers"] = answers
                        user_states[chat_id]["survey_index"] = index + 1
                        if index + 1 < len(questions):
                            next_q = questions[index+1]
                            msg = f"📊 **نظرسنجی ادامه دارد**\n━━━━━━━━━━━━━━━━━━\n\n"
                            msg += f"سوال {index+2} از {len(questions)}:\n"
                            msg += f"❓ {next_q['question']}\n"
                            if next_q['type'] == 'text':
                                msg += "📝 پاسخ خود را به صورت متن ارسال کنید."
                            elif next_q['type'] == 'choice':
                                options = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(next_q['options'])])
                                msg += f"گزینه‌ها:\n{options}\n\nعدد گزینه مورد نظر را وارد کنید."
                            send_message(chat_id, msg, get_cancel_keyboard())
                        else:
                            if submit_survey_response(survey_id, user_db_id, answers):
                                send_message(chat_id, "✅ پاسخ‌های شما با موفقیت ثبت شد. با تشکر از مشارکت شما!", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                                log_user_activity(user_db_id, "survey_submit", f"شرکت در نظرسنجی {survey_id}")
                            else:
                                send_message(chat_id, "❌ خطا در ثبت پاسخ‌ها.", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                            user_states[chat_id]["state"] = "LOGGED_IN"
                    else:
                        send_message(chat_id, f"❌ عدد وارد شده باید بین ۱ تا {len(q['options'])} باشد.", get_cancel_keyboard())
                except ValueError:
                    send_message(chat_id, "❌ لطفاً یک عدد وارد کنید.", get_cancel_keyboard())
            else:  # text
                answers.append(text)
                user_states[chat_id]["survey_answers"] = answers
                user_states[chat_id]["survey_index"] = index + 1
                if index + 1 < len(questions):
                    next_q = questions[index+1]
                    msg = f"📊 **نظرسنجی ادامه دارد**\n━━━━━━━━━━━━━━━━━━\n\n"
                    msg += f"سوال {index+2} از {len(questions)}:\n"
                    msg += f"❓ {next_q['question']}\n"
                    if next_q['type'] == 'text':
                        msg += "📝 پاسخ خود را به صورت متن ارسال کنید."
                    elif next_q['type'] == 'choice':
                        options = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(next_q['options'])])
                        msg += f"گزینه‌ها:\n{options}\n\nعدد گزینه مورد نظر را وارد کنید."
                    send_message(chat_id, msg, get_cancel_keyboard())
                else:
                    if submit_survey_response(survey_id, user_db_id, answers):
                        send_message(chat_id, "✅ پاسخ‌های شما با موفقیت ثبت شد. با تشکر از مشارکت شما!", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                        log_user_activity(user_db_id, "survey_submit", f"شرکت در نظرسنجی {survey_id}")
                    else:
                        send_message(chat_id, "❌ خطا در ثبت پاسخ‌ها.", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())
                    user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ============================================================
        # بخش سوپرادمین (با همه قابلیت‌ها)
        # ============================================================
        if is_super_admin:
            # ===== کنترل خودکار =====
            if text == "🔧 کنترل خودکار":
                reminder_status = "فعال ✅" if get_auto_reminder_status() else "غیرفعال ❌"
                report_status = "فعال ✅" if get_auto_report_status() else "غیرفعال ❌"
                alert_status = "فعال ✅" if get_auto_alert_status() else "غیرفعال ❌"
                scoring_status = "فعال ✅" if get_auto_scoring_status() else "غیرفعال ❌"
                weekly_status = "فعال ✅" if get_weekly_report_status() else "غیرفعال ❌"
                monthly_status = "فعال ✅" if get_monthly_report_status() else "غیرفعال ❌"
                instant_status = "فعال ✅" if get_instant_notification_status() else "غیرفعال ❌"
                adaptive_status = "فعال ✅" if get_adaptive_report_status() else "غیرفعال ❌"
                forecast_status = "فعال ✅" if get_forecast_report_status() else "غیرفعال ❌"
                survey_status = "فعال ✅" if get_survey_system_status() else "غیرفعال ❌"
                chart_status = "فعال ✅" if get_chart_report_status() else "غیرفعال ❌"
                actual_status = "فعال ✅" if get_actual_stats_status() else "غیرفعال ❌"
                
                keyboard = {
                    "keyboard": [
                        [{"text": f"📌 یادآوری: {reminder_status}"}, {"text": f"📌 گزارش روزانه: {report_status}"}],
                        [{"text": f"📌 هشدار افت: {alert_status}"}, {"text": f"📌 امتیازدهی: {scoring_status}"}],
                        [{"text": f"📌 گزارش هفتگی: {weekly_status}"}, {"text": f"📌 گزارش ماهانه: {monthly_status}"}],
                        [{"text": f"🔔 نوتیفیکیشن: {instant_status}"}, {"text": f"📊 گزارش تطبیقی: {adaptive_status}"}],
                        [{"text": f"📈 پیش‌بینی: {forecast_status}"}, {"text": f"📊 نظرسنجی: {survey_status}"}],
                        [{"text": f"📊 نمودار: {chart_status}"}, {"text": f"📊 آمار واقعی: {actual_status}"}],
                        [{"text": "🔙 انصراف"}]
                    ],
                    "resize_keyboard": True
                }
                send_message(chat_id, "⚙️ **کنترل اعمال خودکار ربات**\n\nبرای تغییر وضعیت هر گزینه، روی آن کلیک کنید.", keyboard)
                return

            if text.startswith("📌 یادآوری:"):
                new_status = not get_auto_reminder_status()
                set_auto_reminder_status(new_status)
                send_message(chat_id, f"✅ وضعیت یادآوری خودکار به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📌 گزارش روزانه:"):
                new_status = not get_auto_report_status()
                set_auto_report_status(new_status)
                send_message(chat_id, f"✅ وضعیت گزارش روزانه خودکار به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📌 هشدار افت:"):
                new_status = not get_auto_alert_status()
                set_auto_alert_status(new_status)
                send_message(chat_id, f"✅ وضعیت هشدار افت عملکرد به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📌 امتیازدهی:"):
                new_status = not get_auto_scoring_status()
                set_auto_scoring_status(new_status)
                send_message(chat_id, f"✅ وضعیت امتیازدهی خودکار به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📌 گزارش هفتگی:"):
                new_status = not get_weekly_report_status()
                set_weekly_report_status(new_status)
                send_message(chat_id, f"✅ وضعیت گزارش هفتگی به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📌 گزارش ماهانه:"):
                new_status = not get_monthly_report_status()
                set_monthly_report_status(new_status)
                send_message(chat_id, f"✅ وضعیت گزارش ماهانه به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("🔔 نوتیفیکیشن:"):
                new_status = not get_instant_notification_status()
                set_instant_notification_status(new_status)
                send_message(chat_id, f"✅ وضعیت نوتیفیکیشن لحظه‌ای به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📊 گزارش تطبیقی:"):
                new_status = not get_adaptive_report_status()
                set_adaptive_report_status(new_status)
                send_message(chat_id, f"✅ وضعیت گزارش تطبیقی به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📈 پیش‌بینی:"):
                new_status = not get_forecast_report_status()
                set_forecast_report_status(new_status)
                send_message(chat_id, f"✅ وضعیت پیش‌بینی عملکرد به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📊 نظرسنجی:"):
                new_status = not get_survey_system_status()
                set_survey_system_status(new_status)
                send_message(chat_id, f"✅ وضعیت سیستم نظرسنجی به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📊 نمودار:"):
                new_status = not get_chart_report_status()
                set_chart_report_status(new_status)
                send_message(chat_id, f"✅ وضعیت گزارش‌های نموداری به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            if text.startswith("📊 آمار واقعی:"):
                new_status = not get_actual_stats_status()
                set_actual_stats_status(new_status)
                send_message(chat_id, f"✅ وضعیت ثبت آمار واقعی به {'فعال' if new_status else 'غیرفعال'} تغییر یافت.", get_super_admin_keyboard())
                return

            # ===== مدیریت مشکلات =====
            if text == "⚙️ مدیریت مشکلات":
                problems = get_all_problems('pending', 20)
                if problems:
                    msg = "📋 **مشکلات ثبت‌شده (در انتظار بررسی)**\n━━━━━━━━━━━━━━━━━━\n"
                    for p in problems:
                        p_id, name, emp_num, problem_text, category, status, created_at = p
                        shamsi_dt = jdatetime.datetime.fromgregorian(datetime=created_at)
                        shamsi_str = f"{shamsi_dt.year}/{shamsi_dt.month:02d}/{shamsi_dt.day:02d} {shamsi_dt.hour:02d}:{shamsi_dt.minute:02d}"
                        msg += f"🆔 {p_id} | {name} ({emp_num})\n"
                        msg += f"📝 {problem_text[:100]}...\n"
                        msg += f"⏰ {shamsi_str}\n"
                        msg += f"برای بررسی: /resolve_problem {p_id}\n\n"
                    send_message(chat_id, msg, get_super_admin_keyboard())
                else:
                    send_message(chat_id, "✅ هیچ مشکل جدیدی وجود ندارد.", get_super_admin_keyboard())
                return

            if text.startswith("/resolve_problem"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        problem_id = int(parts[1])
                        if update_problem_status(problem_id, 'resolved'):
                            send_message(chat_id, f"✅ مشکل {problem_id} با موفقیت بررسی و بسته شد.", get_super_admin_keyboard())
                            log_user_activity(user_db_id, "resolve_problem", f"بستن مشکل {problem_id}")
                        else:
                            send_message(chat_id, "❌ خطا در بستن مشکل.", get_super_admin_keyboard())
                    except:
                        send_message(chat_id, "❌ فرمت: /resolve_problem [problem_id]", get_super_admin_keyboard())
                else:
                    send_message(chat_id, "❌ فرمت: /resolve_problem [problem_id]", get_super_admin_keyboard())
                return

            # ===== وضعیت ربات =====
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

            # ===== ریست گزارش‌ها =====
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

            # ===== مدیریت تعطیلات =====
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
                shamsi_date = normalize_digits(text.split('|')[0].strip())
                description = text.split('|')[1].strip() if len(text.split('|')) > 1 else "تعطیل"
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

            # ===== ارسال پیام به معاونین =====
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

            # ===== دستورات مدیریتی سوپرادمین =====
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

            # ===== گزارش هفتگی و ماهانه =====
            if text == "📊 گزارش هفتگی":
                send_message(chat_id, "🔄 در حال تولید گزارش هفتگی...", get_super_admin_keyboard())
                send_weekly_report_to_all()
                send_message(chat_id, "✅ گزارش هفتگی به تمام کاربران ارسال شد.", get_super_admin_keyboard())
                return

            if text == "📊 گزارش ماهانه":
                send_message(chat_id, "🔄 در حال تولید گزارش ماهانه...", get_super_admin_keyboard())
                send_monthly_report_to_all()
                send_message(chat_id, "✅ گزارش ماهانه به تمام کاربران ارسال شد.", get_super_admin_keyboard())
                return

            # ===== مدیریت کاربران =====
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

            # ===== مدیریت گزارش‌ها =====
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

            # ===== مشاهده لاگ‌ها =====
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

            # ===== لاگ ورود/خروج =====
            if text == "📋 لاگ ورود/خروج":
                logs = get_user_activity_log(50)
                if logs:
                    msg = "📋 **لاگ فعالیت کاربران**\n━━━━━━━━━━━━━━━━━━\n"
                    for log in logs:
                        created_at = log[5]
                        iran_tz = timezone(timedelta(hours=3, minutes=30))
                        created_at_local = created_at.astimezone(iran_tz)
                        shamsi_dt = jdatetime.datetime.fromgregorian(datetime=created_at_local)
                        shamsi_str = f"{shamsi_dt.year}/{shamsi_dt.month:02d}/{shamsi_dt.day:02d} {shamsi_dt.hour:02d}:{shamsi_dt.minute:02d}"
                        msg += f"👤 {log[1]} ({log[2]}) | {log[3]}\n"
                        msg += f"📝 {log[4]}\n"
                        msg += f"⏰ {shamsi_str}\n\n"
                    send_message(chat_id, msg, get_super_admin_keyboard())
                else:
                    send_message(chat_id, "هیچ فعالیتی ثبت نشده است.", get_super_admin_keyboard())
                return

            # ===== مشاهده یادداشت‌ها =====
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

            # ===== ثبت آمار واقعی =====
            if text == "📊 ثبت آمار واقعی":
                if not get_actual_stats_status():
                    send_message(chat_id, "🔴 ثبت آمار واقعی در حال حاضر غیرفعال است.", get_super_admin_keyboard())
                    return
                # دریافت تاریخ برای ثبت
                user_states[chat_id]["state"] = "WAITING_FOR_ACTUAL_DATE"
                send_message(chat_id, "📅 لطفاً **تاریخ** مورد نظر برای ثبت آمار واقعی را به فرمت YYYY/MM/DD وارد کنید:\n\n(مثلاً 1403/01/15)", get_cancel_keyboard())
                return

            if current_state == "WAITING_FOR_ACTUAL_DATE" and is_super_admin:
                if text == "🔙 انصراف":
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                    return
                shamsi_date = normalize_digits(text)
                if re.match(r'^\d{4}/\d{2}/\d{2}$', shamsi_date):
                    user_states[chat_id]["actual_date"] = shamsi_date
                    user_states[chat_id]["state"] = "WAITING_FOR_ACTUAL_BRANCH"
                    # دریافت لیست شعب به ترتیب
                    branches = get_all_branches()
                    if not branches:
                        send_message(chat_id, "❌ هیچ شعبه‌ای یافت نشد.", get_super_admin_keyboard())
                        return
                    user_states[chat_id]["actual_branches"] = branches
                    user_states[chat_id]["actual_branch_index"] = 0
                    branch = branches[0]
                    msg = f"📊 **ثبت آمار واقعی برای تاریخ {get_shamsi_date_formatted(shamsi_date)}**\n"
                    msg += f"━━━━━━━━━━━━━━━━━━\n"
                    msg += f"🏢 شعبه: {branch[1]}\n\n"
                    msg += "📝 لطفاً **مبلغ وصولی معاون** را به میلیون ریال وارد کنید.\n"
                    msg += "(برای کاهش از علامت منفی استفاده کنید، برای افزایش مثبت)\n"
                    msg += "مثال: 4700- برای کاهش ۴.۷ میلیاردی"
                    send_message(chat_id, msg, get_cancel_keyboard())
                else:
                    send_message(chat_id, "❌ فرمت تاریخ نامعتبر. لطفاً به صورت YYYY/MM/DD وارد کنید.")
                return

            if current_state == "WAITING_FOR_ACTUAL_BRANCH" and is_super_admin:
                if text == "🔙 انصراف":
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                    return
                try:
                    deputy_value = parse_number(text)
                    if deputy_value is None:
                        raise ValueError
                    user_states[chat_id]["actual_deputy"] = deputy_value
                    user_states[chat_id]["state"] = "WAITING_FOR_ACTUAL_OTHERS"
                    msg = "📝 اکنون **مبلغ وصولی همکاران** را به میلیون ریال وارد کنید.\n"
                    msg += "(برای کاهش از علامت منفی استفاده کنید، برای افزایش مثبت)"
                    send_message(chat_id, msg, get_cancel_keyboard())
                except ValueError:
                    send_message(chat_id, "❌ لطفاً یک عدد معتبر وارد کنید.")
                return

            if current_state == "WAITING_FOR_ACTUAL_OTHERS" and is_super_admin:
                if text == "🔙 انصراف":
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    send_message(chat_id, "❌ عملیات لغو شد.", get_super_admin_keyboard())
                    return
                try:
                    others_value = parse_number(text)
                    if others_value is None:
                        raise ValueError
                    deputy_value = user_state.get("actual_deputy", 0)
                    shamsi_date = user_state.get("actual_date")
                    branches = user_state.get("actual_branches", [])
                    index = user_state.get("actual_branch_index", 0)
                    
                    # ذخیره آمار واقعی
                    if index < len(branches):
                        branch_id = branches[index][0]
                        success = save_actual_stats(branch_id, shamsi_date, deputy_value, others_value, user_db_id)
                        if success:
                            log_user_activity(user_db_id, "add_actual_stats", f"ثبت آمار واقعی برای شعبه {branches[index][1]} تاریخ {shamsi_date}")
                        else:
                            send_message(chat_id, "❌ خطا در ثبت آمار واقعی.", get_super_admin_keyboard())
                            return
                        
                        # رفتن به شعبه بعدی
                        index += 1
                        if index < len(branches):
                            user_states[chat_id]["actual_branch_index"] = index
                            branch = branches[index]
                            msg = f"📊 **ثبت آمار واقعی برای تاریخ {get_shamsi_date_formatted(shamsi_date)}**\n"
                            msg += f"━━━━━━━━━━━━━━━━━━\n"
                            msg += f"🏢 شعبه: {branch[1]}\n\n"
                            msg += "📝 لطفاً **مبلغ وصولی معاون** را به میلیون ریال وارد کنید.\n"
                            msg += "(برای کاهش از علامت منفی استفاده کنید، برای افزایش مثبت)"
                            send_message(chat_id, msg, get_cancel_keyboard())
                        else:
                            send_message(chat_id, "✅ ثبت آمار واقعی برای همه شعب با موفقیت انجام شد.", get_super_admin_keyboard())
                            user_states[chat_id]["state"] = "LOGGED_IN"
                    else:
                        send_message(chat_id, "✅ ثبت آمار واقعی کامل شد.", get_super_admin_keyboard())
                        user_states[chat_id]["state"] = "LOGGED_IN"
                except ValueError:
                    send_message(chat_id, "❌ لطفاً یک عدد معتبر وارد کنید.")
                return

        # ============================================================
        # ادامه منوی ادمین (با پشتیبانی از سوپرادمین)
        # ============================================================
        
        # ===== تحلیل مدیریتی =====
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

        # ===== گزارش روند شعبه =====
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
            conn = None
            try:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM branches WHERE name ILIKE %s", (f"%{text}%",))
                    result = cur.fetchone()
                    if result:
                        branch_id = result[0]
                        trend = get_branch_trend(branch_id, 5)
                        if trend:
                            msg = f"📊 **روند ۵ روز اخیر شعبه {text}**\n━━━━━━━━━━━━━━━━━━\n"
                            for i in range(len(trend)):
                                date, amount = trend[i]
                                if i == 0:
                                    trend_symbol = "📊"
                                else:
                                    prev_amount = trend[i-1][1]
                                    if amount > prev_amount:
                                        trend_symbol = "📈"
                                    elif amount < prev_amount:
                                        trend_symbol = "📉"
                                    else:
                                        trend_symbol = "➡️"
                                msg += f"{trend_symbol} 📅 {get_shamsi_date_formatted(date)}: {amount//1_000_000:,.0f} میلیون ریال\n"
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
                if conn:
                    return_db_connection(conn)
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ===== عملکرد معاونان (با اضافه کردن انطباق) =====
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
                    msg += f"   🏆 بهترین روز: {perf['best_day']//1_000_000:,.0f} میلیون ریال\n"
                    # اضافه کردن انطباق با آمار واقعی
                    match_data = get_deputy_match_report(dep_id, 30)
                    if match_data:
                        total_match = 0
                        count = 0
                        for row in match_data:
                            if row[2] > 0:  # actual > 0
                                total_match += row[3]  # match_percent
                                count += 1
                        if count > 0:
                            avg_match = total_match / count
                            msg += f"   📊 تطابق با آمار واقعی: {avg_match:.1f}%\n"
                    msg += "\n"
            keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
            send_message(chat_id, msg, keyboard)
            return

        # ===== عملکرد همکاران (مجموع کل دوره) =====
        if text == "👥 عملکرد همکاران" and (role == 'admin' or is_super_admin):
            report = get_others_performance_summary()
            if report:
                msg = f"📊 **عملکرد کلی همکاران (کل دوره)**\n━━━━━━━━━━━━━━━━━━\n\n"
                total_others_all = 0
                for idx, row in enumerate(report, 1):
                    branch_name = row[1]
                    total_others = int(row[2])
                    total_branch = int(row[3])
                    report_days = row[4]
                    msg += f"{idx}. 🏢 {branch_name}\n"
                    msg += f"   👥 کل وصولی همکاران: {total_others//1_000_000:,.0f} میلیون ریال\n"
                    msg += f"   📈 کل وصول شعبه: {total_branch//1_000_000:,.0f} میلیون ریال\n"
                    if total_branch > 0:
                        percent = (total_others / total_branch) * 100
                        msg += f"   📊 سهم همکاران: {percent:.1f}%\n"
                    else:
                        msg += f"   📊 سهم همکاران: ۰%\n"
                    msg += f"   📅 تعداد روزهای ثبت: {report_days}\n\n"
                    total_others_all += total_others
                msg += f"━━━━━━━━━━━━━━━━━━\n"
                msg += f"💰 کل وصولی همکاران استان: {total_others_all//1_000_000:,.0f} میلیون ریال"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, msg, keyboard)
            else:
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "📭 هیچ داده‌ای برای عملکرد همکاران یافت نشد.", keyboard)
            return

        # ===== گزارش تطبیقی =====
        if text == "📊 گزارش تطبیقی" and (role == 'admin' or is_super_admin):
            if not get_adaptive_report_status():
                send_message(chat_id, "🔴 گزارش تطبیقی در حال حاضر غیرفعال است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            if is_holiday():
                send_message(chat_id, "📅 امروز تعطیل است، گزارشی ثبت نشده است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            comparison = get_adaptive_comparison()
            if comparison:
                msg = f"📊 **گزارش تطبیقی** - {get_shamsi_date_formatted(get_shamsi_date())}\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                msg += f"💰 امروز: {comparison['today']//1_000_000:,.0f} میلیون ریال\n"
                msg += f"📅 دیروز: {comparison['yesterday']//1_000_000:,.0f} میلیون ریال\n"
                msg += f"📊 تغییر: {comparison['change_yesterday']:+.1f}%\n\n"
                msg += f"📅 هفته قبل: {comparison['week_ago']//1_000_000:,.0f} میلیون ریال\n"
                msg += f"📊 تغییر: {comparison['change_week']:+.1f}%\n\n"
                msg += f"📅 ماه قبل: {comparison['month_ago']//1_000_000:,.0f} میلیون ریال\n"
                msg += f"📊 تغییر: {comparison['change_month']:+.1f}%"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, msg, keyboard)
            else:
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "📊 داده‌های کافی برای گزارش تطبیقی وجود ندارد.", keyboard)
            return

        # ===== پیش‌بینی عملکرد با تحلیل هر شعبه =====
        if text == "📈 پیش‌بینی عملکرد" and (role == 'admin' or is_super_admin):
            if not get_forecast_report_status():
                send_message(chat_id, "🔴 پیش‌بینی عملکرد در حال حاضر غیرفعال است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            if is_holiday():
                send_message(chat_id, "📅 امروز تعطیل است، داده‌های کافی برای پیش‌بینی وجود ندارد.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            
            send_message(chat_id, "🔄 در حال تحلیل داده‌ها و پیش‌بینی عملکرد هر شعبه...", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
            
            # دریافت پیش‌بینی برای همه شعب
            all_forecasts = get_forecast_for_all_branches(7)
            
            if all_forecasts:
                msg = f"📈 **پیش‌بینی عملکرد شعب (۷ روز آینده)**\n━━━━━━━━━━━━━━━━━━\n\n"
                for branch_name, data in all_forecasts.items():
                    trend = data['trend']
                    forecast = data['forecast']
                    msg += f"🏢 {branch_name}\n"
                    msg += f"   📊 روند: {trend['trend']} (قدرت: {trend['strength']})\n"
                    msg += f"   📈 میانگین وصول: {trend['avg_amount']//1_000_000:,.0f} میلیون ریال\n"
                    msg += f"   🔮 پیش‌بینی روز بعد: {forecast[0]['predicted']//1_000_000:,.0f} میلیون ریال\n"
                    msg += f"   📉 محدوده: {forecast[0]['lower']//1_000_000:,.0f} - {forecast[0]['upper']//1_000_000:,.0f} میلیون ریال\n\n"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, msg, keyboard)
            else:
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "📈 داده‌های کافی برای پیش‌بینی وجود ندارد.", keyboard)
            return

        # ===== نمودار استان =====
        if text == "📊 نمودار استان" and (role == 'admin' or is_super_admin):
            if not get_chart_report_status():
                send_message(chat_id, "🔴 گزارش‌های نموداری در حال حاضر غیرفعال است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            data = generate_province_chart(10)
            if data:
                chart_bytes = generate_chart(data, 'روند ۱۰ روز اخیر استان', 'تاریخ', 'مبلغ (میلیون ریال)', 'line')
                caption = f"📊 **نمودار روند وصول استان**\n۱۰ روز اخیر"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_photo(chat_id, chart_bytes, caption, keyboard)
            else:
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "📊 داده‌های کافی برای نمودار وجود ندارد.", keyboard)
            return

        # ===== نمودار شعبه =====
        if text == "📊 نمودار شعبه" and (role == 'admin' or is_super_admin):
            if not get_chart_report_status():
                send_message(chat_id, "🔴 گزارش‌های نموداری در حال حاضر غیرفعال است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            user_states[chat_id]["state"] = "WAITING_FOR_BRANCH_CHART"
            send_message(chat_id, "🏢 لطفاً **نام شعبه** مورد نظر را برای نمایش نمودار وارد کنید:", get_cancel_keyboard())
            return

        if current_state == "WAITING_FOR_BRANCH_CHART" and (role == 'admin' or is_super_admin):
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.", keyboard)
                return
            conn = None
            try:
                conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM branches WHERE name ILIKE %s", (f"%{text}%",))
                    result = cur.fetchone()
                    if result:
                        branch_id = result[0]
                        data = generate_branch_chart(branch_id, 10)
                        if data:
                            chart_bytes = generate_chart(data, f'روند ۱۰ روز اخیر شعبه {text}', 'تاریخ', 'مبلغ (میلیون ریال)', 'bar')
                            caption = f"📊 **نمودار روند شعبه {text}**\n۱۰ روز اخیر"
                            keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                            send_photo(chat_id, chart_bytes, caption, keyboard)
                        else:
                            keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                            send_message(chat_id, f"📭 هیچ داده‌ای برای شعبه {text} یافت نشد.", keyboard)
                    else:
                        send_message(chat_id, f"❌ شعبه‌ای با نام {text} یافت نشد.", get_cancel_keyboard())
                        return
            except Exception as e:
                send_message(chat_id, f"❌ خطا: {e}", get_cancel_keyboard())
            finally:
                if conn:
                    return_db_connection(conn)
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ===== نمودار تحلیلی =====
        if text == "📊 نمودار تحلیلی" and (role == 'admin' or is_super_admin):
            if not get_chart_report_status():
                send_message(chat_id, "🔴 گزارش‌های نموداری در حال حاضر غیرفعال است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            keyboard = {
                "keyboard": [
                    [{"text": "📊 مقایسه شعب برتر"}, {"text": "📊 نسبت معاون/همکار"}],
                    [{"text": "📈 روند روزانه"}, {"text": "📊 تحلیل انطباق"}],
                    [{"text": "🔙 انصراف"}]
                ],
                "resize_keyboard": True
            }
            user_states[chat_id]["state"] = "WAITING_FOR_ANALYTICAL_CHART"
            send_message(chat_id, "📊 **نمودارهای تحلیلی**\n\nلطفاً نوع نمودار مورد نظر را انتخاب کنید:", keyboard)
            return

        if current_state == "WAITING_FOR_ANALYTICAL_CHART" and (role == 'admin' or is_super_admin):
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.", keyboard)
                return
            
            chart_type_map = {
                "📊 مقایسه شعب برتر": "branch_comparison",
                "📊 نسبت معاون/همکار": "deputy_others_ratio",
                "📈 روند روزانه": "daily_trend",
                "📊 تحلیل انطباق": "match_analysis"
            }
            chart_title_map = {
                "branch_comparison": "مقایسه ۱۰ شعبه برتر (۱۰ روز اخیر)",
                "deputy_others_ratio": "نسبت وصول معاونین و همکاران (۱۰ روز اخیر)",
                "daily_trend": "روند روزانه وصول (۱۰ روز اخیر)",
                "match_analysis": "تحلیل انطباق با آمار واقعی (۱۰ روز اخیر)"
            }
            chart_y_label = {
                "branch_comparison": "مبلغ (میلیون ریال)",
                "deputy_others_ratio": "مبلغ (میلیون ریال)",
                "daily_trend": "مبلغ (میلیون ریال)",
                "match_analysis": "درصد تطابق"
            }
            chart_type = {
                "branch_comparison": "horizontal",
                "deputy_others_ratio": "pie",
                "daily_trend": "line",
                "match_analysis": "bar"
            }
            
            chart_key = chart_type_map.get(text)
            if chart_key:
                data = get_analytical_chart_data(chart_key, 10)
                if data and data['values'] and any(v > 0 for v in data['values']):
                    chart_bytes = generate_chart(
                        data,
                        chart_title_map.get(chart_key, "نمودار تحلیلی"),
                        "شعبه" if chart_key != "daily_trend" else "تاریخ",
                        chart_y_label.get(chart_key, "مبلغ"),
                        chart_type.get(chart_key, "bar")
                    )
                    caption = f"📊 {chart_title_map.get(chart_key, 'نمودار تحلیلی')}"
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_photo(chat_id, chart_bytes, caption, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, "📊 داده‌های کافی برای این نمودار وجود ندارد.", keyboard)
            else:
                send_message(chat_id, "❌ گزینه نامعتبر.", get_cancel_keyboard())
            user_states[chat_id]["state"] = "LOGGED_IN"
            return

        # ===== مقایسه انطباق =====
        if text == "📊 مقایسه انطباق" and (role == 'admin' or is_super_admin):
            if not get_actual_stats_status():
                send_message(chat_id, "🔴 ثبت آمار واقعی در حال حاضر غیرفعال است.", get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard())
                return
            user_states[chat_id]["state"] = "WAITING_FOR_MATCH_DATE"
            send_message(chat_id, "📅 لطفاً **تاریخ** مورد نظر برای مقایسه انطباق را به فرمت YYYY/MM/DD وارد کنید:", get_cancel_keyboard())
            return

        if current_state == "WAITING_FOR_MATCH_DATE" and (role == 'admin' or is_super_admin):
            if text == "🔙 انصراف":
                user_states[chat_id]["state"] = "LOGGED_IN"
                keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                send_message(chat_id, "❌ عملیات لغو شد.", keyboard)
                return
            shamsi_date = normalize_digits(text)
            if re.match(r'^\d{4}/\d{2}/\d{2}$', shamsi_date):
                # دریافت وصول‌های ثبت شده و آمار واقعی برای این تاریخ
                actual_data = get_actual_stats_for_date(shamsi_date)
                if not actual_data:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, f"📭 هیچ آمار واقعی برای تاریخ {get_shamsi_date_formatted(shamsi_date)} ثبت نشده است.", keyboard)
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    return
                
                comparison_data = []
                for item in actual_data:
                    branch_id, branch_name, dep_act, oth_act, total_act = item
                    comp = compare_collection_with_actual(branch_id, shamsi_date)
                    if comp:
                        comparison_data.append((branch_id, branch_name, comp['total_collected'], comp['total_actual'], comp['match_percent']))
                
                if not comparison_data:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, f"📭 هیچ وصولی برای تاریخ {get_shamsi_date_formatted(shamsi_date)} ثبت نشده است.", keyboard)
                    user_states[chat_id]["state"] = "LOGGED_IN"
                    return
                
                # گزارش متنی
                msg = f"📊 **مقایسه انطباق - {get_shamsi_date_formatted(shamsi_date)}**\n━━━━━━━━━━━━━━━━━━\n\n"
                total_collected_all = 0
                total_actual_all = 0
                for branch_id, branch_name, collected, actual, match_pct in comparison_data:
                    msg += f"🏢 {branch_name}\n"
                    msg += f"   ثبت شده: {collected//1_000_000:,.0f} میلیون ریال\n"
                    msg += f"   واقعی: {actual//1_000_000:,.0f} میلیون ریال\n"
                    msg += f"   تطابق: {match_pct:.1f}%\n"
                    # نشان دادن اختلاف
                    diff = collected - actual
                    if diff > 0:
                        msg += f"   📈 {diff//1_000_000:,.0f} میلیون ریال بیشتر از واقعی\n"
                    elif diff < 0:
                        msg += f"   📉 {abs(diff)//1_000_000:,.0f} میلیون ریال کمتر از واقعی\n"
                    else:
                        msg += f"   ✅ کاملاً مطابق\n"
                    msg += "\n"
                    total_collected_all += collected
                    total_actual_all += actual
                
                # کل استان
                if total_actual_all > 0:
                    total_match_pct = (min(total_collected_all, total_actual_all) / max(total_collected_all, total_actual_all)) * 100
                else:
                    total_match_pct = 0 if total_collected_all > 0 else 100
                msg += f"━━━━━━━━━━━━━━━━━━\n"
                msg += f"💰 کل ثبت شده استان: {total_collected_all//1_000_000:,.0f} میلیون ریال\n"
                msg += f"💰 کل واقعی استان: {total_actual_all//1_000_000:,.0f} میلیون ریال\n"
                msg += f"📊 تطابق کلی: {total_match_pct:.1f}%\n"
                
                # ارسال نمودار مقایسه‌ای
                chart_bytes = generate_branch_comparison_chart(comparison_data)
                if chart_bytes:
                    caption = f"📊 مقایسه انطباق - {get_shamsi_date_formatted(shamsi_date)}"
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_photo(chat_id, chart_bytes, caption, keyboard)
                else:
                    keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
                    send_message(chat_id, msg, keyboard)
                user_states[chat_id]["state"] = "LOGGED_IN"
            else:
                send_message(chat_id, "❌ فرمت تاریخ نامعتبر. لطفاً به صورت YYYY/MM/DD وارد کنید.")
            return

        # ===== مشاهده یادداشت‌ها (ادمین) =====
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

        # ===== ثبت یادداشت (معاون) =====
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
            conn = None
            try:
                conn = get_db_connection()
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
                if conn:
                    return_db_connection(conn)
            return

        # ============================================================
        # منوی ادمین (گزارش‌های اصلی)
        # ============================================================
        if role == 'admin' or is_super_admin:
            # ===== گزارش امروز =====
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

            # ===== گزارش ۱۰ روز اخیر =====
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

            # ===== رتبه‌بندی شعب =====
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

            # ===== آمار مفصل امروز =====
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

            # ===== مقایسه روزانه =====
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

            # ===== گزارش تاریخ خاص (ادمین) =====
            if text == "📅 گزارش تاریخ خاص":
                user_states[chat_id]["state"] = "WAITING_FOR_ADMIN_DATE"
                send_message(chat_id, "📅 لطفاً تاریخ مورد نظر را به فرمت **YYYY/MM/DD** وارد کنید (مثلاً ۱۴۰۳/۰۱/۱۵):", get_cancel_keyboard())
                return

            # ===== بهترین/بدترین روز =====
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

            # ===== منوی پیش‌فرض =====
            keyboard = get_admin_keyboard() if role == 'admin' else get_super_admin_keyboard()
            send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", keyboard)
            return

        # ============================================================
        # منوی معاون
        # ============================================================
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
                    for i in range(len(perf)):
                        row = perf[i]
                        date = row[0]
                        daily = int(safe_format(row[1]))
                        avg = int(safe_format(row[2]))
                        if i == 0:
                            trend = "📊"
                        else:
                            prev_amount = int(safe_format(perf[i-1][1]))
                            if daily > prev_amount:
                                trend = "📈"
                            elif daily < prev_amount:
                                trend = "📉"
                            else:
                                trend = "➡️"
                        msg += f"{trend} {get_shamsi_date_formatted(date)}\n"
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

        # ===== نقش نامعتبر =====
        send_message(chat_id, "نقش شما نامعتبر است. لطفاً با پشتیبان تماس بگیرید.")

    except Exception as e:
        logger.error(f"❌ handle_message error: {e}", exc_info=True)
        try:
            send_message(message['chat']['id'], "❌ خطایی رخ داد. لطفاً مجدداً تلاش کنید.")
        except:
            pass

# ============================================================
# Keep-Alive داخلی
# ============================================================
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

# ============================================================
# راه‌اندازی زمان‌بندی کارها
# ============================================================
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
    
    scheduler.add_job(
        check_and_auto_score,
        CronTrigger(hour=20, minute=0),
        id='scoring_job',
        replace_existing=True
    )
    
    scheduler.add_job(
        send_weekly_report_to_all,
        CronTrigger(day_of_week='thu', hour=17, minute=0),
        id='weekly_report_job',
        replace_existing=True
    )
    
    scheduler.add_job(
        send_monthly_report_to_all,
        CronTrigger(hour=17, minute=0),
        id='monthly_report_job',
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("✅ Scheduler started")

# ============================================================
# Main Polling Loop
# ============================================================
def main():
    global requests_session
    offset = 0
    logger.info("🤖 Bot started successfully!")
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
```
