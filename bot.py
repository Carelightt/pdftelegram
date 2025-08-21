# --- PY313 fix: provide imghdr stub before telegram imports ---
import sys, types
try:
    import imghdr  # Python 3.12'de var; 3.13'te yok.
except ModuleNotFoundError:
    m = types.ModuleType("imghdr")
    def what(file, h=None):  # PTB'nin ihtiyacı sadece import başarısı; fonk no-op
        return None
    m.what = what
    sys.modules["imghdr"] = m
# --- END PY313 fix ---
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
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/pdf,application/octet-stream,*/*"
}

# ✅ SADECE İZİN VERDİĞİN GRUP
ALLOWED_CHAT_ID = {-1002950346446, -1002955588715, -4959830304}

# Konuşma durumları
TC, NAME, SURNAME = range(3)

# ================== LOG ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("telegrampdf")

# ================== YARDIMCI ==================
def tr_upper(s: str) -> str:
    """Türkçe büyük harfe çevir (i→İ, ı→I fix) + kenar boşluklarını temizle."""
    if not isinstance(s, str):
        return s
    s = s.strip()
    s = s.replace("i", "İ").replace("ı", "I")
    return s.upper()

def _check_group(update: Update) -> bool:
    """İzinli grup kontrolü. Değilse uyarı ver."""
    if update.effective_chat and (update.effective_chat.id not in ALLOWED_CHAT_ID):
        try:
            update.message.reply_text("Hakkın kapalıdır. Destek için @CengizzAtay yazsın.")
        except Exception:
            pass
        return False
    return True

def parse_pdf_inline(text: str):
    """
    /pdf komutunu tek mesajda yakalar.
    Aşağıdaki formatları destekler:
    1) Çok satırlı:
       /PDF
       TC
       AD
       SOYAD
    2) Tek satır:
       /pdf TC AD SOYAD
    Başarılıysa (tc, ad, soyad) döner, yoksa None.
    """
    if not text:
        return None
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]

    if not lines:
        return None

    first = lines[0]
    if not first.lower().startswith('/pdf'):
        return None

    # Önce tek satır: "/pdf 123 ALI VELI"
    parts = first.split()
    if len(parts) >= 4:
        return parts[1], parts[2], " ".join(parts[3:])  # soyad birden fazla kelime olsa bile

    # Sonra çok satır: "/pdf\nTC\nAD\nSOYAD"
    rest = lines[1:]
    if len(rest) >= 3:
        tc = rest[0]
        ad = rest[1]
        soyad = " ".join(rest[2:])  # soyadı birden fazla kelime olabilir
        return tc, ad, soyad

    return None

# ================== HANDLER'lar ==================
def cmd_start(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    update.message.reply_text("Başlamak için /pdf yaz lütfen.")
    return ConversationHandler.END

def cmd_whereami(update: Update, context: CallbackContext):
    """Bulunduğun chat ve kullanıcı ID'sini gösterir (teşhis için)."""
    cid = update.effective_chat.id if update.effective_chat else None
    uid = update.effective_user.id if update.effective_user else None
    update.message.reply_text(f"Chat ID: {cid}\nUser ID: {uid}")

def start_pdf(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END

    # 🔥 Tek mesajdan direkt PDF üretim denemesi
    inline = parse_pdf_inline(update.message.text or "")
    if inline:
        tc_raw, name_raw, surname_raw = inline
        update.message.reply_text("⏳ PDF hazırlanıyor")

        # Türkçe doğru büyük harf
        name_up = tr_upper(name_raw)
        surname_up = tr_upper(surname_raw)

        pdf_path = generate_pdf(tc_raw.strip(), name_up, surname_up)

        if not pdf_path:
            update.message.reply_text("❌ PDF oluşturulamadı.")
            return ConversationHandler.END

        # Boyut logu (opsiyonel)
        try:
            size_mb = os.path.getsize(pdf_path) / 1024 / 1024
            log.info(f"PDF size: {size_mb:.2f} MB")
        except Exception:
            pass

        # Gönderim (retry)
        for attempt in range(1, 4):
            try:
                filename = f"{name_up}_{surname_up}.pdf".replace(" ", "_")
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                break
            except (NetworkError, TimedOut) as e:
                log.warning(f"send_document timeout/network (attempt {attempt}): {e}")
                if attempt == 3:
                    update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
                else:
                    time.sleep(2 * attempt)
            except Exception as e:
                log.exception(f"send_document failed: {e}")
                update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
                break

        # tmp temizliği
        try:
            os.remove(pdf_path)
        except Exception:
            pass

        return ConversationHandler.END

    # ❓ Eski davranış: adım adım sor
    update.message.reply_text("Müşterinin TC numarasını yaz:")
    return TC

def get_tc(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["tc"] = update.message.text.strip()
    update.message.reply_text("Müşterinin Adını yaz:")
    return NAME

def get_name(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["name"] = update.message.text  # tr_upper'ı en sonda uygulayacağız
    update.message.reply_text("Müşterinin Soyadını yaz:")
    return SURNAME

def get_surname(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["surname"] = update.message.text  # tr_upper'ı hemen aşağıda uygularız
    update.message.reply_text("⏳ PDF hazırlanıyor")

    # Türkçe doğru büyük harf dönüştürme
    name_up = tr_upper(context.user_data["name"])
    surname_up = tr_upper(context.user_data["surname"])

    pdf_path = generate_pdf(
        context.user_data["tc"],
        name_up,
        surname_up
    )

    if not pdf_path:
        update.message.reply_text("❌ PDF oluşturulamadı.")
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
            filename = f"{name_up}_{surname_up}.pdf".replace(" ", "_")
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
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
    if not _check_group(update):
        return ConversationHandler.END
    update.message.reply_text("İptal edildi.")
    return ConversationHandler.END

# ================== PDF OLUŞTURMA ==================
def _save_if_pdf_like(resp) -> str:
    """Yanıt PDF ise dosyaya kaydedip yolunu döner; aksi halde '' döner."""
    try:
        ct = (resp.headers.get("Content-Type") or "").lower()
        content = resp.content or b""
        looks_pdf = (b"%PDF" in content[:10]) or ("application/pdf" in ct) or ("application/octet-stream" in ct)
        if resp.status_code == 200 and looks_pdf and content:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(content)
            tmp.close()
            return tmp.name
        return ""
    except Exception as e:
        log.exception(f"_save_if_pdf_like hata: {e}")
        return ""

def generate_pdf(tc: str, name: str, surname: str) -> str:
    """
    Siteye POST eder, PDF gelirse geçici dosyaya yazar ve yolu döner.
    1) x-www-form-urlencoded (data=)
    2) JSON (json=) fallback
    Content-Type yanlış gelse bile %PDF imzasından doğrular.
    """
    data = {"tc": tc, "ad": name, "soyad": surname}

    # 1) Form-encoded dene
    try:
        r = requests.post(PDF_URL, data=data, headers=HEADERS, timeout=120)
        path = _save_if_pdf_like(r)
        if path:
            return path
        else:
            log.error(f"[form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:300]}")
    except Exception as e:
        log.exception(f"[form] generate_pdf hata: {e}")

    # 2) JSON dene
    try:
        r2 = requests.post(PDF_URL, json=data, headers=HEADERS, timeout=120)
        path2 = _save_if_pdf_like(r2)
        if path2:
            return path2
        else:
            log.error(f"[json] PDF alınamadı | status={r2.status_code} ct={(r2.headers.get('Content-Type') or '').lower()} body={r2.text[:300]}")
    except Exception as e:
        log.exception(f"[json] generate_pdf hata: {e}")

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
    dp.add_handler(CommandHandler("whereami", cmd_whereami))  # teşhis komutu
    dp.add_handler(conv)

    log.info("Bot açılıyor...")
    updater.start_polling(drop_pending_updates=True)  # pending update'leri at
    updater.idle()

if __name__ == "__main__":
    main()
