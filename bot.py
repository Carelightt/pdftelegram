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
from datetime import datetime, date, timedelta, timezone
import json
from zoneinfo import ZoneInfo

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
    "Accept": "application/pdf,application/octet-stream,*/*",
    "Referer": "https://pdf-admin1.onrender.com/",
    "X-Requested-With": "XMLHttpRequest",
}

# ✅ SADECE İZİN VERDİĞİN GRUPLAR
ALLOWED_CHAT_ID = {-1002950346446, -1002955588715, -4959830304}

# ====== GEÇİCİ İZİN (SÜRELİ HAK) ======
PERMS_FILE = "temp_perms.json"  # geçici izinlerin saklandığı dosya

def _now_utc():
    return datetime.now(timezone.utc)

def _load_perms():
    try:
        with open(PERMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}

def _save_perms(perms: dict):
    try:
        with open(PERMS_FILE, "w", encoding="utf-8") as f:
            json.dump(perms, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"temp_perms yazılamadı: {e}")

def _prune_expired(perms: dict) -> dict:
    changed = False
    now = _now_utc()
    out = {}
    for k, iso in perms.items():
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt > now:
                out[k] = iso
            else:
                changed = True
        except Exception:
            changed = True
    if changed:
        _save_perms(out)
    return out

TEMP_PERMS = _prune_expired(_load_perms())

def _add_temp(chat_id: int, until_dt_utc: datetime):
    global TEMP_PERMS
    TEMP_PERMS[str(chat_id)] = until_dt_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_perms(TEMP_PERMS)

def _is_temp_allowed(chat_id: int) -> bool:
    global TEMP_PERMS
    TEMP_PERMS = _prune_expired(TEMP_PERMS)
    iso = TEMP_PERMS.get(str(chat_id))
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) > _now_utc()
    except Exception:
        return False

# ====== KARA LİSTE (ANINDA KAPAT /bitir) ======
DENY_FILE = "deny_groups.json"
def _load_deny():
    try:
        with open(DENY_FILE, "r", encoding="utf-8") as f:
            arr = json.load(f)
            return set(int(x) for x in arr)
    except Exception:
        return set()

def _save_deny(s: set):
    try:
        with open(DENY_FILE, "w", encoding="utf-8") as f:
            json.dump(list(s), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"deny_groups yazılamadı: {e}")

DENY_GROUPS = _load_deny()

# ====== GÜNLÜK RAPOR (GRUP BAŞI SAYAC) ======
REPORT_FILE = "daily_report.json"
TR_TZ = ZoneInfo("Europe/Istanbul")
MONTHS_TR = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran","Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]

def _today_tr_str():
    return datetime.now(TR_TZ).strftime("%Y-%m-%d")

def _today_tr_human():
    now = datetime.now(TR_TZ)
    return f"{now.day} {MONTHS_TR[now.month-1]}"

def _load_report():
    try:
        with open(REPORT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "date" in data and "counts" in data and isinstance(data["counts"], dict):
                migrated = False
                for k, v in list(data["counts"].items()):
                    if isinstance(v, int):
                        data["counts"][k] = {"pdf": int(v), "kart": 0}
                        migrated = True
                    elif isinstance(v, dict):
                        v.setdefault("pdf", 0)
                        v.setdefault("kart", 0)
                if migrated:
                    _save_report(data)
                return data
    except Exception:
        pass
    return {"date": _today_tr_str(), "counts": {}}

def _save_report(rep: dict):
    try:
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"daily_report yazılamadı: {e}")

def _ensure_today_report():
    rep = _load_report()
    today = _today_tr_str()
    if rep.get("date") != today:
        rep = {"date": today, "counts": {}}
        _save_report(rep)
    return rep

def _inc_report(chat_id: int, kind: str):
    rep = _ensure_today_report()
    key = str(chat_id)
    node = rep["counts"].get(key) or {"pdf": 0, "kart": 0}
    if kind not in ("pdf", "kart"):
        kind = "pdf"
    node[kind] = int(node.get(kind, 0)) + 1
    rep["counts"][key] = node
    _save_report(rep)

def _get_today_counts(chat_id: int):
    rep = _ensure_today_report()
    node = rep["counts"].get(str(chat_id)) or {"pdf": 0, "kart": 0}
    pdf_c = int(node.get("pdf", 0))
    kart_c = int(node.get("kart", 0))
    return pdf_c, kart_c, pdf_c + kart_c

# Konuşma durumları
TC, NAME, SURNAME, MIKTAR = range(4)
# /kart için durumlar
K_ADSOYAD, K_ADRES, K_ILILCE, K_TARIH = range(4)

# ================== LOG ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("telegrampdf")

# ================== YARDIMCI ==================
def tr_upper(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = s.strip()
    s = s.replace("i", "İ").replace("ı", "I")
    return s.upper()

def _check_group(update: Update) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    chat_id = chat.id

    # Kara listedeyse kapat
    if chat_id in DENY_GROUPS:
        try:
            update.message.reply_text("Hakkın kapalıdır. Destek için @CengizzAtay yazsın.")
        except Exception:
            pass
        return False

    if chat_id in ALLOWED_CHAT_ID:
        return True

    if _is_temp_allowed(chat_id):
        return True

    try:
        update.message.reply_text("Hakkın kapalıdır. Destek için @CengizzAtay yazsın.")
    except Exception:
        pass
    return False

def parse_pdf_inline(text: str):
    """
    /pdf komutu için inline parse:
    Çok satırlı:
      /pdf\\nTC\\nAD\\nSOYAD\\nMIKTAR
    Tek satır (opsiyonel):
      /pdf TC AD SOYAD ... MIKTAR
    Dönüş: (tc, ad, soyad, miktar) ya da None
    """
    if not text:
        return None
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.lower().startswith('/pdf'):
        return None

    # Çok satırlı tercih
    rest = lines[1:]
    if len(rest) >= 4:
        tc = rest[0]
        ad = rest[1]
        soyad = rest[2]
        miktar = rest[3]
        return tc, ad, soyad, miktar

    # Tek satır varyantı
    parts = first.split()
    # /pdf TC AD (SOYAD BİRKAÇ KELİME) MIKTAR  -> en sondaki token'ı miktar say
    if len(parts) >= 5:
        tc = parts[1]
        ad = parts[2]
        miktar = parts[-1]
        soyad = " ".join(parts[3:-1])
        return tc, ad, soyad, miktar

    return None

def parse_kart_inline(text: str):
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None
    first_line_end = raw.find("\n")
    first_line = raw if first_line_end == -1 else raw[:first_line_end]
    if not first_line.lower().startswith("/kart"):
        return None
    rest_text = "" if first_line_end == -1 else raw[first_line_end+1:]
    rest_lines = [l.strip() for l in rest_text.splitlines() if l.strip()]
    if len(rest_lines) >= 4:
        adsoyad = rest_lines[0]
        adres   = rest_lines[1]
        ililce  = rest_lines[2]
        tarih   = rest_lines[3]
        return adsoyad, adres, ililce, tarih
    return None

# ================== HANDLER'lar ==================
def cmd_start(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    update.message.reply_text("Başlamak için /pdf veya /kart yaz.")
    return ConversationHandler.END

def cmd_whereami(update: Update, context: CallbackContext):
    cid = update.effective_chat.id if update.effective_chat else None
    uid = update.effective_user.id if update.effective_user else None
    update.message.reply_text(f"Chat ID: {cid}\nUser ID: {uid}")

# Süre verme komutu — sade mesaj
def cmd_yetkiver(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    raw = " ".join(context.args or [])
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        update.message.reply_text("Kullanım: /yetkiver <gün>  (1–30 arası)")
        return
    days = int(digits)
    if days < 1 or days > 30:
        update.message.reply_text("Gün 1 ile 30 arasında olmalı.")
        return
    until_utc = _now_utc() + timedelta(days=days)
    _add_temp(chat_id, until_utc)

    # bitir ile kapatılmışsa kaldır
    global DENY_GROUPS
    if chat_id in DENY_GROUPS:
        DENY_GROUPS.remove(chat_id)
        _save_deny(DENY_GROUPS)

    update.message.reply_text(f"Bu gruba {days} günlük izin verildi.")

# Anında kapat
def cmd_bitir(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id

    global TEMP_PERMS
    if str(chat_id) in TEMP_PERMS:
        del TEMP_PERMS[str(chat_id)]
        _save_perms(TEMP_PERMS)

    global DENY_GROUPS
    DENY_GROUPS.add(chat_id)
    _save_deny(DENY_GROUPS)

    update.message.reply_text("⛔ Bu grubun hakkı kapatıldı.")

# Günlük rapor
def cmd_rapor(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    human_day = _today_tr_human()
    pdf_c, kart_c, _ = _get_today_counts(chat_id)
    update.message.reply_text(
        f"{human_day}\n\n"
        f"Üretilen PDF : {pdf_c}\n"
        f"Üretilen KART PDF : {kart_c}"
    )

def start_pdf(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    inline = parse_pdf_inline(update.message.text or "")
    if inline:
        tc_raw, name_raw, surname_raw, miktar_raw = inline
        update.message.reply_text("⏳ PDF hazırlanıyor")
        name_up = tr_upper(name_raw)
        surname_up = tr_upper(surname_raw)
        pdf_path = generate_pdf(tc_raw.strip(), name_up, surname_up, miktar_raw.strip())
        if not pdf_path:
            update.message.reply_text("❌ PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            _inc_report(update.effective_chat.id, "pdf")
        except Exception:
            pass

        try:
            size_mb = os.path.getsize(pdf_path) / 1024 / 1024
            log.info(f"PDF size: {size_mb:.2f} MB")
        except Exception:
            pass

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

        try:
            os.remove(pdf_path)
        except Exception:
            pass

        return ConversationHandler.END

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
    context.user_data["name"] = update.message.text
    update.message.reply_text("Müşterinin Soyadını yaz:")
    return SURNAME

def get_surname(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["surname"] = update.message.text
    update.message.reply_text("Miktarı yaz (örn: 5.000):")
    return MIKTAR

def get_miktar(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["miktar"] = update.message.text.strip()
    update.message.reply_text("⏳ PDF hazırlanıyor")
    name_up = tr_upper(context.user_data["name"])
    surname_up = tr_upper(context.user_data["surname"])
    pdf_path = generate_pdf(
        context.user_data["tc"],
        name_up,
        surname_up,
        context.user_data["miktar"]
    )
    if not pdf_path:
        update.message.reply_text("❌ PDF oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "pdf")
    except Exception:
        pass

    try:
        size_mb = os.path.getsize(pdf_path) / 1024 / 1024
        log.info(f"PDF size: {size_mb:.2f} MB")
    except Exception:
        pass

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

# ================== KART DURUMU: /kart ==================
KART_PDF_URL = "https://pdf-admin1.onrender.com/generate2"

def generate_kart_pdf(adsoyad: str, adres: str, ililce: str, tarih: str) -> str:
    try:
        data = {"adsoyad": adsoyad, "adres": adres, "ililce": ililce, "tarih": tarih}
        r = requests.post(KART_PDF_URL, data=data, headers=HEADERS, timeout=90)
        ct = (r.headers.get("Content-Type") or "").lower()
        if r.status_code == 200 and "pdf" in ct:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(r.content)
            tmp.close()
            return tmp.name
        else:
            log.error(f"KART PDF alınamadı | status={r.status_code} ct={ct} body={r.text[:200]}")
            return ""
    except Exception as e:
        log.exception(f"generate_kart_pdf hata: {e}")
        return ""

def start_kart(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    inline = parse_kart_inline(update.message.text or "")
    if inline:
        adsoyad, adres, ililce, tarih = inline
        update.message.reply_text("⏳ Kart durumu PDF hazırlanıyor...")
        pdf_path = generate_kart_pdf(adsoyad, adres, ililce, tarih)
        if not pdf_path:
            update.message.reply_text("❌ Kart PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            _inc_report(update.effective_chat.id, "kart")
        except Exception:
            pass

        for attempt in range(1, 4):
            try:
                base = (adsoyad or "KART").strip().replace(" ", "_").upper()
                filename = f"{base}_KART.pdf"
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                break
            except (NetworkError, TimedOut) as e:
                log.warning(f"kart send timeout/network (attempt {attempt}): {e}")
                if attempt == 3:
                    update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
                else:
                    time.sleep(2 * attempt)
            except Exception as e:
                log.exception(f"kart send failed: {e}")
                update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
                break

        try:
            os.remove(pdf_path)
        except Exception:
            pass

        return ConversationHandler.END

    update.message.reply_text("Ad Soyad yaz:")
    return K_ADSOYAD

def get_k_adsoyad(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["k_adsoyad"] = update.message.text.strip()
    update.message.reply_text("Adres yaz:")
    return K_ADRES

def get_k_adres(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["k_adres"] = update.message.text.strip()
    update.message.reply_text("İl İlçe yaz:")
    return K_ILILCE

def get_k_ililce(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["k_ililce"] = update.message.text.strip()
    update.message.reply_text("Tarih yaz:")
    return K_TARIH

def get_k_tarih(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["k_tarih"] = update.message.text.strip()
    update.message.reply_text("⏳ Kart durumu PDF hazırlanıyor...")
    pdf_path = generate_kart_pdf(
        context.user_data["k_adsoyad"],
        context.user_data["k_adres"],
        context.user_data["k_ililce"],
        context.user_data["k_tarih"]
    )
    if not pdf_path:
        update.message.reply_text("❌ Kart PDF oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "kart")
    except Exception:
        pass

    for attempt in range(1, 4):
        try:
            base = (context.user_data.get("k_adsoyad") or "KART").strip().replace(" ", "_").upper()
            filename = f"{base}_KART.pdf"
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
                    timeout=180
                )
            break
        except (NetworkError, TimedOut) as e:
            log.warning(f"kart send timeout/network (attempt {attempt}): {e}")
            if attempt == 3:
                update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
            else:
                time.sleep(2 * attempt)
        except Exception as e:
            log.exception(f"kart send failed: {e}")
            update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
            break

    try:
        os.remove(pdf_path)
    except Exception:
        pass

    return ConversationHandler.END

# ================== PDF OLUŞTURMA ==================
def _save_if_pdf_like(resp) -> str:
    try:
        ct = (resp.headers.get("Content-Type") or "").lower()
        cd = (resp.headers.get("Content-Disposition") or "").lower()
        content = resp.content or b""
        looks_pdf = (b"%PDF" in content[:10]) or ("application/pdf" in ct) or ("filename=" in cd)
        if resp.status_code == 200 and looks_pdf and content:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(content)
            tmp.close()
            return tmp.name
        return ""
    except Exception as e:
        log.exception(f"_save_if_pdf_like hata: {e}")
        return ""

def generate_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
    data = {"tc": tc, "ad": name, "soyad": surname, "miktar": miktar}
    try:
        r = requests.post(PDF_URL, data=data, headers=HEADERS, timeout=120)
        path = _save_if_pdf_like(r)
        if path:
            return path
        else:
            log.error(f"[form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:300]}")
    except Exception as e:
        log.exception(f"[form] generate_pdf hata: {e}")
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

    request_kwargs = {
        "con_pool_size": 8,
        "connect_timeout": 30,
        "read_timeout": 180
    }

    updater = Updater(BOT_TOKEN, use_context=True, request_kwargs=request_kwargs)

    try:
        updater.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"delete_webhook uyarı: {e}")

    dp = updater.dispatcher
    dp.add_error_handler(on_error)

    conv = ConversationHandler(
        entry_points=[CommandHandler("pdf", start_pdf)],
        states={
            TC: [MessageHandler(Filters.text & ~Filters.command, get_tc)],
            NAME: [MessageHandler(Filters.text & ~Filters.command, get_name)],
            SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_surname)],
            MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_miktar)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )

    conv_kart = ConversationHandler(
        entry_points=[CommandHandler("kart", start_kart)],
        states={
            K_ADSOYAD: [MessageHandler(Filters.text & ~Filters.command, get_k_adsoyad)],
            K_ADRES:   [MessageHandler(Filters.text & ~Filters.command, get_k_adres)],
            K_ILILCE:  [MessageHandler(Filters.text & ~Filters.command, get_k_ililce)],
            K_TARIH:   [MessageHandler(Filters.text & ~Filters.command, get_k_tarih)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whereami", cmd_whereami))
    dp.add_handler(CommandHandler("yetkiver", cmd_yetkiver, pass_args=True))
    dp.add_handler(CommandHandler("bitir", cmd_bitir))
    dp.add_handler(CommandHandler("rapor", cmd_rapor))
    dp.add_handler(conv)
    dp.add_handler(conv_kart)

    log.info("Bot açılıyor...")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
