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
DB_URL = os.getenv("postgresql://postgres.uvpwvhmwuklqqmhgdorx:Farhad35667900@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require")
BASE_URL = f"https://api.bale.ai/bot{BOT_TOKEN}"

# راه‌اندازی Connection Pool برای مدیریت بهینه اتصال به دیتابیس Supabase
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DB_URL)
    logger.info("Database connection pool established successfully.")
except Exception as e:
    logger.error(f"Failed to create database connection pool: {e}")
    db_pool = None

# مدیریت وضعیت کاربران در حافظه موقت (State Machine)
# ساختار: {chat_id: {"state": "...", "deputy_amount": 0, "edit_mode": False}}
user_states = {}

def get_db_connection():
    if db_pool:
        try:
            return db_pool.getconn()
        except psycopg2.OperationalError:
            # در صورت خراب شدن کانکشن‌های استخر، اتصال مستقیم برقرار می‌شود
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

def get_shamsi_date():
    """تبدیل تاریخ جاری ایران به فرمت شمسی YYYY/MM/DD"""
    now_iran = get_iran_time()
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
        logger.error(f"Error sending message to {chat_id}: {e}")
        return None

def get_main_keyboard(role):
    """تولید کیبورد سفارشی بر اساس نقش کاربر"""
    if role == 'admin':
        return {
            "keyboard": [
                [{"text": "📊 گزارش شعب امروز"}, {"text": "📈 گزارش ۱۰ روز اخیر استان"}],
                [{"text": "🏆 ۵ شعب برتر استان"}]
            ],
            "resize_keyboard": True
        }
    else:  # deputy
        return {
            "keyboard": [
                [{"text": "💰 ثبت وصولی روزانه"}, {"text": "📅 گزارش ۱۰ روز اخیر شعبه"}]
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
        logger.error(f"Error in find_user_by_employee_number: {e}")
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
        logger.error(f"Error in update_user_telegram_id: {e}")
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
        logger.error(f"Error in find_user_by_telegram_id: {e}")
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
        logger.error(f"Error in check_existing_collection: {e}")
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
        logger.error(f"Error in save_or_update_collection: {e}")
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
        logger.error(f"Error in get_branch_10_day_report: {e}")
        return []
    finally:
        return_db_connection(conn)

# --- متدهای دیتابیس بخش ادمین (گزارش‌های جامع) ---

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
        logger.error(f"Error in get_today_province_report: {e}")
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
        logger.error(f"Error in get_province_10_day_report: {e}")
        return []
    finally:
        return_db_connection(conn)

def get_top_5_branches():
    """رتبه‌بندی ۵ شعبه برتر استان بر اساس کل مبلغ وصولی ثبت شده"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.name, SUM(c.total_amount) as total
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                GROUP BY b.name
                ORDER BY total DESC
                LIMIT 5
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error in get_top_5_branches: {e}")
        return []
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
                    f"👤 {title}\n"
                    f"🏢 واحد: {branch_name or 'ستاد'}\n"
                    f"🔑 دسترسی: {role == 'admin' and 'کاربر ارشد (ستاد)' or 'معاون شعبه'}"
                )
                send_message(chat_id, welcome_msg, get_main_keyboard(role))
            else:
                send_message(chat_id, "❌ شماره کارمندی در سیستم زنجان یافت نشد. مجدداً شماره کارمندی صحیح خود را بفرستید:")
            return
        else:
            user_states[chat_id] = {"state": "WAITING_FOR_EMP_NUM"}
            send_message(chat_id, "سلام. به ربات وصول مطالبات استان زنجان خوش آمدید.\nجهت دسترسی، لطفاً شماره کارمندی خود را ارسال کنید:")
            return

    # استخراج اطلاعات کاربر تایید شده
    user_db_id, employee_number, full_name, role, title, branch_id, branch_name = user
    user_state = user_states.setdefault(chat_id, {"state": "LOGGED_IN"})
    current_state = user_state.get("state")

    # ب) بخش ثبت مبالغ وصولی روزانه (مرحله اول: مبلغ معاون)
    if current_state == "WAITING_FOR_DEPUTY_AMOUNT":
        if text == "🔙 انصراف":
            user_states[chat_id] = {"state": "LOGGED_IN"}
            send_message(chat_id, "عملیات لغو شد. به منوی اصلی بازگشتید.", get_main_keyboard(role))
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
            send_message(chat_id, "مبلغ وصولی سایر همکاران شعبه را به ریال وارد کنید:", cancel_keyboard)
        except ValueError:
            send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت و به ریال وارد کنید:")
        return

    # ج) بخش ثبت مبالغ وصولی روزانه (مرحله دوم: سایر همکاران و ذخیره نهایی)
    elif current_state == "WAITING_FOR_OTHERS_AMOUNT":
        if text == "🔙 انصراف":
            user_states[chat_id] = {"state": "LOGGED_IN"}
            send_message(chat_id, "عملیات لغو شد. به منوی اصلی بازگشتید.", get_main_keyboard(role))
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
                status_text = "بروزرسانی" if is_edit else "ثبت"
                success_msg = (
                    f"✅ اطلاعات با موفقیت {status_text} شد.\n\n"
                    f"🏢 شعبه: {branch_name}\n"
                    f"📅 تاریخ: {shamsi_today}\n"
                    f"👤 وصولی معاون: {deputy_amount:,.0f} ریال\n"
                    f"👥 وصولی سایر همکاران: {others_amount:,.0f} ریال\n"
                    f"📊 جمع کل وصولی: {total:,.0f} ریال"
                )
                send_message(chat_id, success_msg, get_main_keyboard(role))
            else:
                send_message(chat_id, "❌ خطا در ثبت اطلاعات دیتابیس. لطفا مجدداً تلاش کنید.", get_main_keyboard(role))
        except ValueError:
            send_message(chat_id, "❌ خطا: لطفاً مبلغ را به صورت عدد مثبت و به ریال وارد کنید:")
        return

    # د) پردازش ویرایش رکورد موجود در صورت تایید کاربر
    elif current_state == "WAITING_FOR_EDIT_CONFIRMATION":
        if text == "📝 بله، ویرایش شود":
            user_states[chat_id] = {"state": "WAITING_FOR_DEPUTY_AMOUNT", "edit_mode": True}
            cancel_keyboard = {"keyboard": [[{"text": "🔙 انصراف"}]], "resize_keyboard": True}
            send_message(chat_id, "لطفاً مبلغ جدید وصولی شخص خودتان (معاون) را به ریال وارد کنید:", cancel_keyboard)
        else:
            user_states[chat_id] = {"state": "LOGGED_IN"}
            send_message(chat_id, "عملیات لغو شد. به منوی اصلی بازگشتید.", get_main_keyboard(role))
        return

    # هـ) پردازش منوهای اصلی معاون شعبه (Deputy)
    if role == 'deputy':
        if text == "💰 ثبت وصولی روزانه":
            shamsi_today = get_shamsi_date()
            existing = check_existing_collection(branch_id, shamsi_today)
            
            if existing:
                # رکورد قبلا ثبت شده است، پیشنهاد ویرایش داده می‌شود
                col_id, dep_val, oth_val = existing
                user_states[chat_id] = {"state": "WAITING_FOR_EDIT_CONFIRMATION"}
                confirm_keyboard = {
                    "keyboard": [[{"text": "📝 بله، ویرایش شود"}, {"text": "❌ خیر، لغو شود"}]],
                    "resize_keyboard": True
                }
                msg = (
                    f"⚠️ اطلاعات وصولی شعبه {branch_name} امروز ({shamsi_today}) قبلاً ثبت شده است:\n\n"
                    f"👤 وصولی معاون: {dep_val:,.0f} ریال\n"
                    f"👥 وصولی همکاران: {oth_val:,.0f} ریال\n"
                    f"جمع کل: {(dep_val+oth_val):,.0f} ریال\n\n"
                    f"آیا مایل به ویرایش و جایگزینی مقادیر هستید؟"
                )
                send_message(chat_id, msg, confirm_keyboard)
            else:
                user_states[chat_id] = {"state": "WAITING_FOR_DEPUTY_AMOUNT", "edit_mode": False}
                cancel_keyboard = {"keyboard": [[{"text": "🔙 انصراف"}]], "resize_keyboard": True}
                send_message(chat_id, "لطفاً مبلغ وصولی شخص خودتان (معاون) را به ریال وارد کنید:", cancel_keyboard)

        elif text == "📅 گزارش ۱۰ روز اخیر شعبه":
            report = get_branch_10_day_report(branch_id)
            if report:
                msg = f"📊 گزارش عملکرد ۱۰ روز اخیر شعبه {branch_name}:\n\n"
                for row in report:
                    msg += (
                        f"📅 تاریخ: {row[0]}\n"
                        f"👤 وصولی معاون: {row[1]:,.0f} ریال\n"
                        f"👥 وصولی همکاران: {row[2]:,.0f} ریال\n"
                        f"💰 جمع کل: {row[3]:,.0f} ریال\n"
                        f"---------------------------------\n"
                    )
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "هیچ سابقه وصولی ثبت شده‌ای برای شعبه شما یافت نشد.")
        else:
            send_message(chat_id, "لطفاً یک گزینه از کیبورد زیر انتخاب کنید:", get_main_keyboard(role))

    # و) پردازش منوهای اصلی کاربران ارشد (Admin)
    elif role == 'admin':
        if text == "📊 گزارش شعب امروز":
            shamsi_today = get_shamsi_date()
            report = get_today_province_report(shamsi_today)
            if report:
                msg = f"📊 گزارش وصولی کل شعب استان زنجان در تاریخ {shamsi_today}:\n\n"
                total_province = 0
                for idx, row in enumerate(report, 1):
                    msg += f"{idx}. {row[0]}: {row[3]:,.0f} ریال (معاون: {row[1]:,.0f} | همکاران: {row[2]:,.0f})\n"
                    total_province += row[3]
                msg += f"\n📈 جمع کل وصولی استان امروز: {total_province:,.0f} ریال"
                send_message(chat_id, msg)
            else:
                send_message(chat_id, f"امروز ({shamsi_today}) هنوز هیچ شعبه‌ای اطلاعات وصولی خود را ثبت نکرده است.")

        elif text == "📈 گزارش ۱۰ روز اخیر استان":
            report = get_province_10_day_report()
            if report:
                msg = "📈 گزارش وصول مطالبات استان زنجان (۱۰ روز اخیر):\n\n"
                for row in report:
                    msg += (
                        f"📅 تاریخ: {row[0]}\n"
                        f"👤 وصولی معاونین: {row[1]:,.0f} ریال\n"
                        f"👥 وصولی همکاران: {row[2]:,.0f} ریال\n"
                        f"💰 جمع کل استان: {row[3]:,.0f} ریال\n"
                        f"---------------------------------\n"
                    )
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "دیتابیس در حال حاضر خالی است و هیچ تراکنشی یافت نشد.")

        elif text == "🏆 ۵ شعب برتر استان":
            report = get_top_5_branches()
            if report:
                msg = "🏆 ۵ شعبه برتر استان زنجان از نظر میزان کل وصول مطالبات:\n\n"
                medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
                for idx, row in enumerate(report):
                    msg += f"{medals[idx]} {row[0]} با مجموع وصولی {row[1]:,.0f} ریال\n"
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "دیتابیس در حال حاضر خالی است.")
        else:
            send_message(chat_id, "لطفاً یک گزینه از کیبورد زیر انتخاب کنید:", get_main_keyboard(role))


# --- چرخه پولینگ اصلی ربات (Polling Loop) ---

def main():
    offset = 0
    logger.info("Bot initiated using Polling mechanism...")
    
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
                        
                        # بروزرسانی آنی آفست برای جلوگیری از پردازش چندباره
                        offset = update_id + 1
            elif res.status_code == 409:
                logger.warning("Conflict (409) detected. Retrying in 5 seconds...")
                time.sleep(5)
            else:
                logger.error(f"Failed to fetch updates. Status code: {res.status_code}")
                time.sleep(5)
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error in polling loop: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Unexpected error in main execution loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
