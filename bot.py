import os
import time
import tempfile
import logging
import requests
from dotenv import load_dotenv

from telegram import Update, InputFile
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    ConversationHandler, CallbackContext
)

# ================== AYAR ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

PDF_URL = "https://pdf-admin1.onrender.com/generate"  # Ücret formu endpoint'i
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Konuşma durumları
TC, NAME, SURNAME = range(3)

# ================== LOG ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("telegrampdf")


# ================== HANDLER'lar ==================
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text("Selam !")
    return ConversationHandler.END

def start_pdf(update: Update, context: CallbackContext):
    update.message.reply_text("Müşterinin TC numarasını yaz:")
    return TC

def get_tc(update: Update, context: CallbackContext):
    context.user_data["tc"] = update.message.text.strip()
    update.message.reply_text("Müşterinin Adını yaz:")
    return NAME

def get_name(update: Update, context: CallbackContext):
    context.user_data["name"] = update.message.text.strip()
    update.message.reply_text("Müşterinin Soyadını yaz:")
    return SURNAME

def get_surname(update: Update, context: CallbackContext):
    context.user_data["surname"] = update.message.text.strip()
    update.message.reply_text("⏳ PDF hazırlanıyor")

    pdf_path = generate_pdf(
    context.user_data["tc"],
    context.user_data["name"].upper(),
    context.user_data["surname"].upper()
)

    if not pdf_path:
        update.message.reply_text("❌ PDF oluşturulamadı veya sunucu yanıt vermedi.")
        return ConversationHandler.END

    # Boyut logu
    try:
        size_mb = os.path.getsize(pdf_path) / 1024 / 1024
        log.info(f"PDF size: {size_mb:.2f} MB")
    except Exception:
        pass

    # 3 deneme, uzun timeout ile gönder
    for attempt in range(1, 4):
        try:
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename="belge.pdf"),
                    timeout=180  # upload için geniş süre
                )
            break
        except (NetworkError, TimedOut) as e:
            log.warning(f"send_document timeout/network (attempt {attempt}): {e}")
            if attempt == 3:
                update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
            else:
                time.sleep(2 * attempt)  # 2s, 4s bekle ve tekrar dene
        except Exception as e:
            log.exception(f"send_document failed: {e}")
            update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
            break

    # tmp temizlik
    try:
        os.remove(pdf_path)
    except Exception:
        pass

    return ConversationHandler.END

def cmd_cancel(update: Update, context: CallbackContext):
    update.message.reply_text("İptal edildi.")
    return ConversationHandler.END


# ================== PDF OLUŞTURMA ==================
def generate_pdf(tc: str, name: str, surname: str) -> str:
    """
    Siteye formla POST eder, Content-Type application/pdf ise geçici dosyaya yazar ve yolu döner.
    Hata olursa "" döner.
    """
    try:
        data = {"tc": tc, "ad": name, "soyad": surname}
        r = requests.post(PDF_URL, data=data, headers=HEADERS, timeout=60)

        ct = (r.headers.get("Content-Type") or "").lower()
        if r.status_code == 200 and "application/pdf" in ct:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(r.content)
            tmp.close()
            return tmp.name
        else:
            # Hata durumunu logla (ilk 300 char)
            log.error(f"PDF alınamadı | status={r.status_code} ct={ct} body={r.text[:300]}")
            return ""
    except Exception as e:
        log.exception(f"generate_pdf hata: {e}")
        return ""


# ================== ERROR HANDLER ==================
def on_error(update: object, context: CallbackContext):
    log.exception("Unhandled error", exc_info=context.error)


# ================== MAIN ==================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env'de yok!")

    # Geniş timeout'lar ve connection pool
    request_kwargs = {
        "con_pool_size": 8,
        "connect_timeout": 30,
        "read_timeout": 180
    }

    updater = Updater(BOT_TOKEN, use_context=True, request_kwargs=request_kwargs)

    # Eski webhook’u temizle (çatışma olmasın)
    try:
        updater.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"delete_webhook uyarı: {e}")

    dp = updater.dispatcher
    dp.add_error_handler(on_error)

    # Konuşma akışı
    conv = ConversationHandler(
        entry_points=[CommandHandler("pdf", start_pdf)],
        states={
            TC: [MessageHandler(Filters.text & ~Filters.command, get_tc)],
            NAME: [MessageHandler(Filters.text & ~Filters.command, get_name)],
            SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_surname)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
    )

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(conv)

    log.info("Bot açılıyor...")
    updater.start_polling(drop_pending_updates=True)  # pending update'leri at
    updater.idle()


if __name__ == "__main__":

    main()
