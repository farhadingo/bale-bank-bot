import os
import time
import logging
import requests
import psycopg2
from psycopg2 import pool
from datetime import datetime, timedelta, timezone

# تنظیمات پیشرفته لاگین
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# بارگذاری متغیرهای محیطی
BOT_TOKEN = os.getenv("160966979:s3cnOPW18kZcUJRSpIUp8r68jnuvjUK72wQ")
DB_URL = os.getenv("postgresql://postgres.uvpwvhmwuklqqmhgdorx:Farhad35667900@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres")

if not BOT_TOKEN or not DB_URL:
    logger.error("BOT_TOKEN and DB_URL environment variables are required!")
    exit(1)

BASE_URL = f"https://api.bale.ai/bot{BOT_TOKEN}"

# راه‌اندازی Connection Pool برای مدیریت بهینه اتصال به دیتابیس Supabase
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DB_URL)
    logger.info("✅ Database connection pool established successfully.")
except Exception as e:
    logger.error(f"❌ Failed to create database connection pool: {e}")
    db_pool = None

# مدیریت وضعیت کاربران در حافظه موقت (State Machine)
user_states = {}

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
    """محاسبه دقیق زمان رسمی ایران (با اختلاف ۳:۳۰+ نسبت به UTC)"""
    iran_timezone = timezone(timedelta(hours=3, minutes=30))
    return datetime.now(iran_timezone)

def get_shamsi_date(days_offset=0):
    """تبدیل تاریخ جاری ایران به فرمت شمسی YYYY/MM/DD"""
    now_iran = get_iran_time() + timedelta(days=days_offset)
    g_y, g_m, g_d = now_iran.year, now_iran.month, now_iran.day
    
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if (g_y % 4 == 0 and g_y % 100 != 0) or (g_y % 400 == 0):
        g_days_in_month[1] = 29

    gy = g_y - 1600
    gm = g_m - 1
    gd = g_d - 1

    g_day_no = 365 * gy + gy // 4 - gy // 100 + gy // 400
    for i in range(gm):
        g_day_no += g_days_in_month[i]
    g_day_no += gd

    jy = 979 + 33 * (g_day_no // 12053) + 4 * ((g_day_no % 12053) // 1461)
    g_day_no %= 1461
    if g_day_no >= 366:
        jy += (g_day_no - 1) // 365
        g_day_no = (g_day_no - 1) % 365

    if g_day_no < 186:
        jm = 1 + g_day_no // 31
        jd = 1 + (g_day_no % 31)
    else:
        jm = 7 + (g_day_no - 186) // 30
        jd = 1 + ((g_day_no - 186) % 30)
        
    return f"{jy}/{jm:02d}/{jd:02d}"

def get_shamsi_date_formatted(shamsi_str):
    """تبدیل تاریخ شمسی YYYY/MM/DD به نام فارسی"""
    months = {
        '01': 'فروردین', '02': 'اردیبهشت', '03': 'خرداد',
        '04': 'تیر', '05': 'مرداد', '06': 'شهریور',
        '07': 'مهر', '08': 'آبان', '09': 'آذر',
        '10': 'دی', '11': 'بهمن', '12': 'اسفند'
    }
    parts = shamsi_str.split('/')
    return f"{parts[2]} {months[parts[1]]} {parts[0]}"

def send_message(chat_id, text, reply_markup=None):
    """ارسال پیام به پیام‌رسان بله"""
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json()
    except Exception as e:
        logger.error(f"❌ Error sending message to {chat_id}: {e}")
        return None

def get_deputy_keyboard():
    """کیبورد برای معاونین شعب"""
    return {
        "keyboard": [
            [{"text": "💰 ثبت وصولی روزانه"}, {"text": "📊 گزارش وصولی"}],
            [{"text": "📈 مقایسه عملکرد"}, {"text": "🔙 خروج"}]
        ],
        "resize_keyboard": True
    }

def get_admin_keyboard():
    """کیبورد برای کاربران ارشد"""
    return {
        "keyboard": [
            [{"text": "📊 گزارش امروز"}, {"text": "📈 گزارش ۱۰ روز اخیر"}],
            [{"text": "🏆 رتبه‌بندی شعب"}, {"text": "💹 آمار مفصل"}],
            [{"text": "📉 مقایسه روزانه"}, {"text": "🎯 تحلیل عملکرد"}],
            [{"text": "🔙 خروج"}]
        ],
        "resize_keyboard": True
    }

# --- متدهای کار با دیتابیس ---

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
        logger.error(f"❌ Error in find_user_by_employee_number: {e}")
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
        logger.error(f"❌ Error in update_user_telegram_id: {e}")
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
        logger.error(f"❌ Error in find_user_by_telegram_id: {e}")
        return None
    finally:
        return_db_connection(conn)

def check_existing_collection(branch_id, shamsi_date):
    """بررسی ثبت تراکنش برای شعبه در تاریخ خاص"""
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
        logger.error(f"❌ Error in check_existing_collection: {e}")
        return None
    finally:
        return_db_connection(conn)

def save_or_update_collection(branch_id, deputy_amount, others_amount, shamsi_date, user_id, update_existing=False):
    """ثبت یا ویرایش اطلاعات وصولی یک شعبه"""
    conn = get_db_connection()
    created_at_iran = get_iran_time()
    try:
        with conn.cursor() as cur:
            if update_existing:
                cur.execute("""
                    UPDATE collections 
                    SET deputy_amount = %s, others_amount = %s, recorded_by = %s, created_at = %s
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
        logger.error(f"❌ Error in save_or_update_collection: {e}")
        return False
    finally:
        return_db_connection(conn)

def get_branch_10_day_report(branch_id):
    """گزارش ۱۰ روز اخیر یک شعبه"""
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
        logger.error(f"❌ Error in get_branch_10_day_report: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_today_province_report(shamsi_date):
    """لیست ثبت شعب در روز جاری به همراه جمع کل"""
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
        logger.error(f"❌ Error in get_today_province_report: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_province_10_day_report():
    """گزارش جمع تجمیعی کل استان در ۱۰ روز گذشته به تفکیک روز"""
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
        logger.error(f"❌ Error in get_province_10_day_report: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_top_5_branches():
    """رتبه‌بندی ۵ شعبه برتر استان بر اساس کل مبلغ وصولی ثبت شده"""
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
        logger.error(f"❌ Error in get_top_5_branches: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_today_statistics():
    """آمار کلی امروز"""
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
        logger.error(f"❌ Error in get_today_statistics: {e}")
        return None
    finally:
        return_db_connection(conn)

def get_yesterday_vs_today():
    """مقایسه امروز با دیروز"""
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
        logger.error(f"❌ Error in get_yesterday_vs_today: {e}")
        return None
    finally:
        return_db_connection(conn)

def get_detailed_report(shamsi_date):
    """گزارش مفصل شعب برای روز خاص"""
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
        logger.error(f"❌ Error in get_detailed_report: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_branch_performance(branch_id, days=10):
    """تحلیل عملکرد شعبه"""
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
                ORDER BY shamsi_date DESC
                LIMIT %s
            """, (branch_id, days))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ Error in get_branch_performance: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_daily_comparison():
    """مقایسه روزانه تمام شعب"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    shamsi_date,
                    COUNT(DISTINCT branch_id) as branches_count,
                    SUM(total_amount) as total_collection
                FROM collections
                ORDER BY shamsi_date DESC
                LIMIT 7
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ Error in get_daily_comparison: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_deputy_vs_others_ratio():
    """نسبت وصولی معاونین به سایر همکاران"""
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
        logger.error(f"❌ Error in get_deputy_vs_others_ratio: {e}")
        return None
    finally:
        return_db_connection(conn)

# --- موتور پردازش پیام‌ها (Handlers) ---

def handle_message(message):
    chat_id = message['chat']['id']
    text = message.get('text', '').strip()
    
    # شناسایی و احراز هویت کاربر
    user = find_user_by_telegram_id(chat_id)
    
    # الف) مرحله ورود کاربر جدید
    if not user:
        user_state = user_states.get(chat_id, {})
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
                    f"⏰ زمان ورود: {get_shamsi_date_formatted(get_shamsi_date())}\n"
                    f"🕐 ساعت: {get_iran_time().strftime('%H:%M:%S')}\n\n"
                    f"خوش آمدید! 👋"
                )
                keyboard = get_admin_keyboard() if role == 'admin' else get_deputy_keyboard()
                send_message(chat_id, welcome_msg, keyboard)
            else:
                send_message(chat_id, "❌ شماره کارمندی در سیستم یافت نشد.\nلطفاً شمار�� کارمندی صحیح خود را بفرستید:")
            return
        else:
            user_states[chat_id] = {"state": "WAITING_FOR_EMP_NUM"}
            send_message(chat_id, "👋 سلام! به ربات وصول مطالبات استان زنجان خوش آمدید.\n\n🔐 جهت دسترسی، لطفاً شماره کارمندی خود را ارسال کنید:")
            return

    # استخراج اطلاعات کاربر تایید شده
    user_db_id, employee_number, full_name, role, title, branch_id, branch_name = user
    user_state = user_states.setdefault(chat_id, {"state": "LOGGED_IN"})
    current_state = user_state.get("state")

    # ب) بخش ثبت مبالغ وصولی روزانه (مرحله اول: مبلغ معاون)
    if current_state == "WAITING_FOR_DEPUTY_AMOUNT":
        if text == "🔙 انصراف":
            user_states[chat_id] = {"state": "LOGGED_IN"}
            send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", get_deputy_keyboard() if role == 'deputy' else get_admin_keyboard())
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
            cancel_keyboard = {"keyboard": [[{"text": "🔙 انصراف"}]], "resize_keyboard": True}
            send_message(chat_id, "✏️ اکنون میزان وصولی سایر همکاران شعبه را وارد کنید (برحسب ریال):", cancel_keyboard)
        except ValueError:
            send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت وارد کنید.")
        return

    # ج) بخش ثبت مبالغ وصولی روزانه (مرحله دوم: سایر همکاران و ذخیره نهایی)
    elif current_state == "WAITING_FOR_OTHERS_AMOUNT":
        if text == "🔙 انصراف":
            user_states[chat_id] = {"state": "LOGGED_IN"}
            send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", get_deputy_keyboard() if role == 'deputy' else get_admin_keyboard())
            return
        try:
            others_amount = int(text.replace(',', '').replace('،', ''))
            if others_amount < 0:
                raise ValueError
            
            deputy_amount = user_state.get("deputy_amount", 0)
            shamsi_today = get_shamsi_date()
            is_edit = user_state.get("edit_mode", False)
            
            # ذخیره یا بروزرسانی در دیتابیس
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
                success_msg = (
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
                keyboard = get_deputy_keyboard() if role == 'deputy' else get_admin_keyboard()
                send_message(chat_id, success_msg, keyboard)
            else:
                send_message(chat_id, "❌ خطا در ثبت اطلاعات. لطفا مجدداً تلاش کنید.", get_deputy_keyboard() if role == 'deputy' else get_admin_keyboard())
        except ValueError:
            send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت وارد کنید.")
        return

    # د) پردازش ویرایش رکورد موجود در صورت تایید کاربر
    elif current_state == "WAITING_FOR_EDIT_CONFIRMATION":
        if text == "📝 بله، ویرایش شود":
            user_states[chat_id] = {"state": "WAITING_FOR_DEPUTY_AMOUNT", "edit_mode": True}
            cancel_keyboard = {"keyboard": [[{"text": "🔙 انصراف"}]], "resize_keyboard": True}
            send_message(chat_id, "✏️ لطفاً مبلغ جدید وصولی خود (معاون) را وارد کنید:", cancel_keyboard)
        else:
            user_states[chat_id] = {"state": "LOGGED_IN"}
            keyboard = get_deputy_keyboard() if role == 'deputy' else get_admin_keyboard()
            send_message(chat_id, "❌ عملیات لغو شد.\n\nبه منوی اصلی بازگشتید.", keyboard)
        return

    # هـ) خروج از سیستم
    if text == "🔙 خروج":
        user_states[chat_id] = {"state": "LOGGED_OUT"}
        send_message(chat_id, "👋 شما از سیستم خارج شدید.\n\nبرای ورود مجدد، شماره کارمندی خود را ارسال کنید.")
        user_states[chat_id] = {"state": "WAITING_FOR_EMP_NUM"}
        return

    # و) پردازش منوهای معاونین شعبه (Deputy)
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
                    f"👤 وصولی معاون: {dep_val:,.0f} ریال\n"
                    f"👥 وصولی همکاران: {oth_val:,.0f} ریال\n"
                    f"💰 جمع کل: {(dep_val+oth_val):,.0f} ریال\n"
                    f"━━━━━━━━━━━━━━━\n\n"
                    f"❓ آیا مایل به ویرایش هستید؟"
                )
                send_message(chat_id, msg, confirm_keyboard)
            else:
                user_states[chat_id] = {"state": "WAITING_FOR_DEPUTY_AMOUNT", "edit_mode": False}
                cancel_keyboard = {"keyboard": [[{"text": "🔙 انصراف"}]], "resize_keyboard": True}
                send_message(chat_id, "📝 لطفاً میزان وصولی خود (معاون) را وارد کنید:", cancel_keyboard)

        elif text == "📊 گزارش وصولی":
            report = get_branch_10_day_report(branch_id)
            if report:
                msg = f"📊 گزارش وصول شعبه {branch_name}\n"
                msg += f"(۱۰ روز اخیر)\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                total_sum = 0
                for i, row in enumerate(report, 1):
                    msg += (
                        f"{i}. 📅 {get_shamsi_date_formatted(row[0])}\n"
                        f"   👤 معاون: {row[1]:,.0f}\n"
                        f"   👥 همکاران: {row[2]:,.0f}\n"
                        f"   💰 جمع: {row[3]:,.0f} ریال\n\n"
                    )
                    total_sum += row[3]
                msg += f"━━━━━━━━━━━━━━��━━━\n"
                msg += f"📈 جمع ۱۰ روز: {total_sum:,.0f} ریال\n"
                msg += f"📊 میانگین روزانه: {total_sum//len(report):,.0f} ریال"
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "📊 هیچ سابقه وصولی برای شعبه شما یافت نشد.")

        elif text == "📈 مقایسه عملکرد":
            perf = get_branch_performance(branch_id, 7)
            if perf:
                msg = f"📈 تحلیل عملکرد شعبه {branch_name}\n"
                msg += f"(۷ روز اخیر)\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                for i, row in enumerate(perf, 1):
                    trend = "📈" if i < len(perf) and perf[i-1][1] > row[1] else "📉"
                    msg += (
                        f"{trend} {get_shamsi_date_formatted(row[0])}\n"
                        f"   جمع روزانه: {row[1]:,.0f} ریال\n"
                        f"   میانگین متحرک: {row[2]:,.0f} ریال\n\n"
                    )
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "📈 داده کافی برای تحلیل وجود ندارد.")

        else:
            send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", get_deputy_keyboard())

    # ز) پردازش منوهای کاربران ارشد (Admin)
    elif role == 'admin':
        if text == "📊 گزارش امروز":
            shamsi_today = get_shamsi_date()
            report = get_today_province_report(shamsi_today)
            stats = get_today_statistics()
            
            if report:
                msg = f"📊 گزارش وصول امروز\n"
                msg += f"📅 تاریخ: {get_shamsi_date_formatted(shamsi_today)}\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                
                total_province = 0
                for idx, row in enumerate(report, 1):
                    msg += f"{idx}. 🏢 {row[0]}\n"
                    msg += f"   👤 معاون: {row[1]:,.0f}\n"
                    msg += f"   👥 همکاران: {row[2]:,.0f}\n"
                    msg += f"   💰 جمع: {row[3]:,.0f} ریال\n\n"
                    total_province += row[3]
                
                msg += f"━━━━━━━━━━━━━━━━━━\n"
                if stats:
                    msg += f"📈 خلاصه:\n"
                    msg += f"   تعداد شعب ثبت شده: {stats[0]}\n"
                    msg += f"   کل وصولی معاونین: {stats[1]:,.0f}\n"
                    msg += f"   کل وصولی همکاران: {stats[2]:,.0f}\n"
                    msg += f"   💰 جمع کل استان: {total_province:,.0f} ریال"
                send_message(chat_id, msg)
            else:
                send_message(chat_id, f"📊 امروز ({shamsi_today}) هنوز هیچ شعبه‌ای اطلاعات ثبت نکرده است.")

        elif text == "📈 گزارش ۱۰ روز اخیر":
            report = get_province_10_day_report()
            if report:
                msg = f"📈 گزارش ۱۰ روز اخیر استان زنجان\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                total_all = 0
                for row in report:
                    msg += (
                        f"📅 {get_shamsi_date_formatted(row[0])}\n"
                        f"   👤 معاونین: {row[1]:,.0f}\n"
                        f"   👥 سایر همکاران: {row[2]:,.0f}\n"
                        f"   💰 جمع: {row[3]:,.0f} ریال\n\n"
                    )
                    total_all += row[3]
                msg += f"━━━━━━━━━━━━━━━━━━\n"
                msg += f"📊 کل ۱۰ روز: {total_all:,.0f} ریال"
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "📈 دیتابیس خالی است.")

        elif text == "🏆 رتبه‌بندی شعب":
            report = get_top_5_branches()
            if report:
                msg = f"🏆 ۵ شعبه برتر استان زنجان\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
                for idx, row in enumerate(report):
                    msg += f"{medals[idx]} {row[0]}\n"
                    msg += f"    💰 کل وصولی: {row[1]:,.0f} ریال\n"
                    msg += f"    📊 تعداد ثبت: {row[2]} روز\n\n"
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "🏆 داده کافی برای رتبه‌بندی وجود ندارد.")

        elif text == "💹 آمار مفصل":
            shamsi_today = get_shamsi_date()
            report = get_detailed_report(shamsi_today)
            if report:
                msg = f"💹 آمار مفصل امروز\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                for idx, row in enumerate(report, 1):
                    msg += f"{idx}. 🏢 {row[0]}\n"
                    msg += f"   👤 معاون ({row[4]}): {row[1]:,.0f} ریال\n"
                    msg += f"   👥 سایرین: {row[2]:,.0f} ریال\n"
                    msg += f"   💰 جمع: {row[3]:,.0f} ریال\n\n"
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "💹 برای امروز اطلاعاتی وجود ندارد.")

        elif text == "📉 مقایسه روزانه":
            comparison = get_daily_comparison()
            if comparison:
                msg = f"📉 مقایسه روزانه (۷ روز اخیر)\n"
                msg += f"━━━━━━━━━━━━━━━━━━\n\n"
                for row in comparison:
                    msg += f"📅 {get_shamsi_date_formatted(row[0])}\n"
                    msg += f"    🏢 شعب ثبت‌کننده: {row[1]}\n"
                    msg += f"    💰 کل وصولی: {row[2]:,.0f} ریال\n\n"
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "📉 داده کافی وجود ندارد.")

        elif text == "🎯 تحلیل عملکرد":
            ratio = get_deputy_vs_others_ratio()
            yesterday_today = get_yesterday_vs_today()
            
            msg = f"🎯 تحلیل عملکرد استان\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n\n"
            
            if ratio:
                msg += f"📊 نسبت امروز:\n"
                msg += f"   👤 وصولی معاونین: {ratio[0]:,.0f} ریال ({ratio[2]:.1f}%)\n"
                msg += f"   👥 وصولی سایرین: {ratio[1]:,.0f} ریال ({100-ratio[2]:.1f}%)\n\n"
            
            if yesterday_today:
                today = yesterday_today[0] or 0
                yesterday = yesterday_today[1] or 0
                change = ((today - yesterday) / yesterday * 100) if yesterday > 0 else 0
                trend = "📈 افزایش" if change > 0 else "📉 کاهش" if change < 0 else "➡️ ثابت"
                
                msg += f"📅 مقایسه امروز/دیروز:\n"
                msg += f"   امروز: {today:,.0f} ریال\n"
                msg += f"   دیروز: {yesterday:,.0f} ریال\n"
                msg += f"   {trend}: {abs(change):.1f}%\n"
            
            send_message(chat_id, msg)

        else:
            send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید:", get_admin_keyboard())

    else:
        send_message(chat_id, "لطفاً یک گزینه از منو انتخاب کنید.", get_admin_keyboard() if role == 'admin' else get_deputy_keyboard())


# --- چرخه پولینگ اصلی ربات (Polling Loop) ---

def main():
    offset = 0
    logger.info("🤖 Bot initiated using Polling mechanism...")
    
    while True:
        try:
            url = f"{BASE_URL}/getUpdates"
            params = {"offset": offset, "timeout": 20}
            res = requests.get(url, params=params, timeout=25)
            
            if res.status_code == 200:
                data = res.json()
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        update_id = update["update_id"]
                        
                        if "message" in update:
                            handle_message(update["message"])
                        
                        offset = update_id + 1
            elif res.status_code == 409:
                logger.warning("⚠️ Conflict (409) detected. Retrying in 5 seconds...")
                time.sleep(5)
            else:
                logger.error(f"❌ Failed to fetch updates. Status code: {res.status_code}")
                time.sleep(5)
                
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Network error in polling loop: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"❌ Unexpected error in main execution loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
