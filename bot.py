# --- PY313 fix: provide imghdr stub before telegram imports ---
import sys, types
try:
    import imghdr  # Python 3.12'de var; 3.13'te yok.
except ModuleNotFoundError:
    m = types.ModuleType("imghdr")
    def what(file, h=None):  # PTB'nin ihtiyacı sadece import başarısı; fonk no-op
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
import pytz   # ✅ zoneinfo yerine pytz kullanıyoruz

GROUP_LIMITS_FILE = "group_limits.json"

def _load_limits():
    try:
        with open(GROUP_LIMITS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return {str(k): int(v) for k, v in d.items()}
    except Exception:
        return {}

def _save_limits(d: dict):
    with open(GROUP_LIMITS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

GROUP_LIMITS = _load_limits()

def _get_group_limit(chat_id: int) -> int:
    """Grubun kişi limitini getir (yoksa varsayılan 7)."""
    return int(GROUP_LIMITS.get(str(chat_id), 7))

def _set_group_limit(chat_id: int, limit: int):
    GROUP_LIMITS[str(chat_id)] = max(1, int(limit))
    _save_limits(GROUP_LIMITS)


TR_TZ = pytz.timezone("Europe/Istanbul")  # ✅ ZoneInfo yerine pytz

from telegram import Update, InputFile
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    ConversationHandler, CallbackContext
)

# ⏰ Scheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ================== AYAR ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_KEY   = os.getenv("BOT_KEY")  # 🔑 siteyle aynı olmalı

PDF_URL       = "https://pdf-admin1.onrender.com/generate"   # Ücret formu endpoint'i
KART_PDF_URL  = "https://pdf-admin1.onrender.com/generate2"
BURS_PDF_URL  = "https://pdf-admin1.onrender.com/generate3"  # ✅ Burs endpoint'i (sablon3.pdf)

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/pdf,application/octet-stream,*/*",
    "Referer": "https://pdf-admin1.onrender.com/",
    "X-Requested-With": "XMLHttpRequest",
}
def _headers():
    """Her istekte X-Bot-Key ekle (varsa)."""
    h = dict(HEADERS_BASE)
    if BOT_KEY:
        h["X-Bot-Key"] = BOT_KEY
    return h

# ✅ SADECE İZİN VERDİĞİN GRUPLAR
ALLOWED_CHAT_ID = {-1002955588714}

# ====== ADMIN KİLİDİ ======
ADMIN_ID = 6672759317  # 👈 sadece bu kullanıcı admin

def _is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)

def _require_admin(update: Update) -> bool:
    """Admin değilse kullanıcıyı uyarır, False döner."""
    if not _is_admin(update):
        try:
            update.message.reply_text("⛔ Bu komutu kullanma yetkin yok.@CengizzAtay")
        except Exception:
            pass
        return False
    return True

# ====== GEÇİCİ İZİN (SÜRELİ HAK) ======
PERMS_FILE = "temp_perms.json"  # geçici izinlerin saklandığı dosya

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

# ====== HAK (ADET) SİSTEMİ ======
QUOTA_FILE = "quota_rights.json"

def _load_quota():
    try:
        with open(QUOTA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # {chat_id_str: int}
            out = {}
            for k, v in data.items():
                try:
                    out[str(int(k))] = int(v)
                except Exception:
                    pass
            return out
    except Exception:
        return {}

def _save_quota(d: dict):
    try:
        with open(QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"quota yazılamadı: {e}")

QUOTA = _load_quota()

def _get_quota(chat_id: int) -> int:
    return int(QUOTA.get(str(chat_id), 0))

def _set_quota(chat_id: int, amount: int):
    global QUOTA
    QUOTA[str(chat_id)] = max(0, int(amount))
    _save_quota(QUOTA)

def _dec_quota_if_applicable(chat_id: int):
    """
    Sadece ALLOWED veya TEMP izni YOKSA düş.
    (Süre izni varsa sınırsız, hak azaltılmaz.)
    """
    if chat_id in ALLOWED_CHAT_ID or _is_temp_allowed(chat_id):
        return
    rem = _get_quota(chat_id)
    if rem > 0:
        _set_quota(chat_id, rem - 1)

# ====== GÜNLÜK RAPOR (GRUP BAŞI SAYAC) ======
REPORT_FILE = "daily_report.json"
TITLES_FILE = "group_titles.json"   # 👈 grup adlarını saklarız
import pytz
TR_TZ = pytz.timezone("Europe/Istanbul")   # ✅ ZoneInfo yerine pytz
MONTHS_TR = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran","Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]

def _today_tr_str():
    return datetime.now(TR_TZ).strftime("%Y-%m-%d")

def _today_tr_human():
    now = datetime.now(TR_TZ)
    return f"{now.day} {MONTHS_TR[now.month-1]}"

def _load_titles():
    try:
        with open(TITLES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return {str(k): str(v) for k, v in d.items()}
    except Exception:
        return {}

def _save_titles(d: dict):
    try:
        with open(TITLES_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"group_titles yazılamadı: {e}")

GROUP_TITLES = _load_titles()

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

def _inc_report(chat_id: int, kind: str, title: str = None):
    """Günlük sayaç artır. (title verilirse kaydederiz.)"""
    rep = _ensure_today_report()
    key = str(chat_id)
    node = rep["counts"].get(key) or {"pdf": 0, "kart": 0}
    if kind not in ("pdf", "kart"):
        kind = "pdf"
    node[kind] = int(node.get(kind, 0)) + 1
    rep["counts"][key] = node
    _save_report(rep)

    if title:
        GROUP_TITLES[key] = title
        _save_titles(GROUP_TITLES)

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
# /burs için durumlar
B_TC, B_NAME, B_SURNAME, B_MIKTAR = range(4)

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

def _has_time_or_whitelist(chat_id: int) -> bool:
    return (chat_id in ALLOWED_CHAT_ID) or _is_temp_allowed(chat_id)

def _check_group(update: Update, context: CallbackContext) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    chat_id = chat.id

    # Kara listedeyse kapat
    if chat_id in DENY_GROUPS:
        try:
            update.message.reply_text("Hakkın kapalıdır. Destek için @CengizzAtay yaz.")
        except Exception:
            pass
        return False

    # Süre/whitelist ise serbest
    if _has_time_or_whitelist(chat_id):
        return True

    # Değilse hak (adet) kontrolü
    if _get_quota(chat_id) > 0:
        return True

    # Hiçbiri yoksa kapalı
    try:
        update.message.reply_text("Bu grubun hakkı yoktur. /yetkiver veya /hakver kullanın.")
    except Exception:
        pass
    return False

def parse_pdf_inline(text: str):
    """
    /pdf komutu için inline parse:
    Çok satırlı:
      /pdf\nTC\nAD\nSOYAD\nMIKTAR
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
        adres   = rest_lines[1]
        ililce  = rest_lines[2]
        tarih   = rest_lines[3]
        return adsoyad, adres, ililce, tarih
    return None

def parse_burs_inline(text: str):
    """
    /burs komutu için inline parse:
      /burs\nTC\nAD\nSOYAD\nMIKTAR
    veya tek satır:
      /burs TC AD SOYAD ... MIKTAR
    """
    if not text:
        return None
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.lower().startswith('/burs'):
        return None

    rest = lines[1:]
    if len(rest) >= 4:
        tc = rest[0]
        ad = rest[1]
        soyad = rest[2]
        miktar = rest[3]
        return tc, ad, soyad, miktar

    parts = first.split()
    if len(parts) >= 5:
        tc = parts[1]
        ad = parts[2]
        miktar = parts[-1]
        soyad = " ".join(parts[3:-1])
        return tc, ad, soyad, miktar

    return None

# ================== HANDLER'lar ==================
def cmd_ekle(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    args = context.args
    if not args:
        update.message.reply_text("Kullanım: /ekle <kişi_sayısı> (örn: /ekle 10)")
        return
    try:
        limit = int(args[0])
        if limit < 1 or limit > 1000:
            update.message.reply_text("Limit 1 ile 1000 arasında olmalı.")
            return
        _set_group_limit(chat_id, limit)
        update.message.reply_text(f"✅ Bu grubun kişi limiti {limit} olarak ayarlandı.")
    except ValueError:
        update.message.reply_text("Geçerli bir sayı gir kanka. Örn: /ekle 10")

def cmd_start(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return ConversationHandler.END
    # admin için bilgi mesajı (normal /start artık kilitli)
    update.message.reply_text("Admin panel komutları: /yetkiver, /hakver, /kalanhak, /bitir, /rapor")
    return ConversationHandler.END

def cmd_whereami(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    cid = update.effective_chat.id if update.effective_chat else None
    uid = update.effective_user.id if update.effective_user else None
    update.message.reply_text(f"Chat ID: {cid}\nUser ID: {uid}")

# Süre verme komutu — SADECE ADMIN
def cmd_yetkiver(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    raw = " ".join(context.args or [])
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        update.message.reply_text("Kullanım: /yetkiver <gün>  (1–30 arası)")
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

# Hak verme (adet) — SADECE ADMIN
def cmd_hakver(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    raw = " ".join(context.args or [])
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        update.message.reply_text("Kullanım: /hakver <adet>  (örn: /hakver 20)")
        return
    amount = int(digits)
    if amount < 0:
        update.message.reply_text("Adet 0 veya üstü olmalı.")
        return
    _set_quota(chat_id, amount)

    # Eğer kara listedeyse aç (hak tanındıysa kullanabilsin)
    global DENY_GROUPS
    if chat_id in DENY_GROUPS:
        DENY_GROUPS.remove(chat_id)
        _save_deny(DENY_GROUPS)

    update.message.reply_text(f"✅ Bu gruba {amount} adet PDF hakkı tanımlandı.")

# Kalan hak — SADECE ADMIN
def cmd_hakdurum(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    rem = _get_quota(chat_id)
    msg = f"Kalan hak: {rem}"
    if _has_time_or_whitelist(chat_id):
        msg += "\n(Not: Süreli/whitelist izni olduğu için hak düşmez.)"
    update.message.reply_text(msg)

# Anında kapat — SADECE ADMIN
def cmd_bitir(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
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

# Günlük rapor — SADECE ADMIN (o anki grup için)
def cmd_rapor(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
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

# ✅ TÜM GÜNÜN GENEL RAPORU — SADECE ADMIN
def cmd_raporadmin(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    # özelden yazılmasını tavsiye et
    try:
        if update.effective_chat and getattr(update.effective_chat, "type", "") != "private":
            update.message.reply_text("Bu komutu bana özelden yaz: /raporadmin")
            return
    except Exception:
        pass
    try:
        text = _build_daily_message(context.bot)
        update.message.reply_text(text)
    except Exception as e:
        log.exception(f"/raporadmin hata: {e}")
        update.message.reply_text("Rapor hazırlanırken bir sorun oluştu.")

# ================== /pdf ==================
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

                # Grup kişi sayısı kontrolü
    try:
        member_count = context.bot.get_chat_members_count(chat_id)
        limit = _get_group_limit(chat_id)
        if member_count > limit:
            update.message.reply_text(f"❌ Bu grup {limit} kişiyle sınırlıdır.")
            return False
    except Exception as e:
        log.warning(f"Kişi sayısı kontrolü hatası: {e}")

        try:
            _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = False
        for attempt in range(1, 4):
            try:
                filename = f"{name_up}_{surname_up}.pdf".replace(" ", "_")
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                sent_ok = True
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

        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)

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
        _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = False
    for attempt in range(1, 4):
        try:
            filename = f"{name_up}_{surname_up}.pdf".replace(" ", "_")
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
                    timeout=180
                )
            sent_ok = True
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

    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)

    return ConversationHandler.END

def cmd_cancel(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    update.message.reply_text("İptal edildi.")
    return ConversationHandler.END

# ================== KART DURUMU: /kart ==================
def generate_kart_pdf(adsoyad: str, adres: str, ililce: str, tarih: str) -> str:
    try:
        data = {"adsoyad": adsoyad, "adres": adres, "ililce": ililce, "tarih": tarih}
        r = requests.post(KART_PDF_URL, data=data, headers=_headers(), timeout=90)
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
            _inc_report(update.effective_chat.id, "kart", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = False
        for attempt in range(1, 4):
            try:
                base = (adsoyad or "KART").strip().replace(" ", "_").upper()
                filename = f"{base}_KART.pdf"
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                sent_ok = True
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

        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)

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
        _inc_report(update.effective_chat.id, "kart", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = False
    for attempt in range(1, 4):
        try:
            base = (context.user_data.get("k_adsoyad") or "KART").strip().replace(" ", "_").upper()
            filename = f"{base}_KART.pdf"
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
                    timeout=180
                )
            sent_ok = True
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

    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)

    return ConversationHandler.END

# ================== BURS: /burs ==================
def generate_burs_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
    """sablon3.pdf üzerinden burs çıktısı üretir (/generate3)"""
    data = {"tc": tc, "ad": name, "soyad": surname, "miktar": miktar}
    try:
        r = requests.post(BURS_PDF_URL, data=data, headers=_headers(), timeout=120)
        path = _save_if_pdf_like(r)
        if path:
            return path
        else:
            log.error(f"[burs form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:300]}")
    except Exception as e:
        log.exception(f"[burs form] generate_burs_pdf hata: {e}")
    try:
        r2 = requests.post(BURS_PDF_URL, json=data, headers=_headers(), timeout=120)
        path2 = _save_if_pdf_like(r2)
        if path2:
            return path2
        else:
            log.error(f"[burs json] PDF alınamadı | status={r2.status_code} ct={(r2.headers.get('Content-Type') or '').lower()} body={r2.text[:300]}")
    except Exception as e:
        log.exception(f"[burs json] generate_burs_pdf hata: {e}")
    return ""

def start_burs(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    inline = parse_burs_inline(update.message.text or "")
    if inline:
        tc_raw, name_raw, surname_raw, miktar_raw = inline
        update.message.reply_text("⏳ BURS PDF hazırlanıyor")
        name_up = tr_upper(name_raw)
        surname_up = tr_upper(surname_raw)
        pdf_path = generate_burs_pdf(tc_raw.strip(), name_up, surname_up, miktar_raw.strip())
        if not pdf_path:
            update.message.reply_text("❌ BURS PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            # burs'u pdf sayıyorduk, aynı şekilde yazıyoruz ama başlık da kaydediyoruz
            _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = False
        for attempt in range(1, 4):
            try:
                filename = f"{name_up}_{surname_up}_BURS.pdf".replace(" ", "_")
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                sent_ok = True
                break
            except (NetworkError, TimedOut) as e:
                log.warning(f"burs send timeout/network (attempt {attempt}): {e}")
                if attempt == 3:
                    update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
                else:
                    time.sleep(2 * attempt)
            except Exception as e:
                log.exception(f"burs send failed: {e}")
                update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
                break

        try:
            os.remove(pdf_path)
        except Exception:
            pass

        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)

        return ConversationHandler.END

    update.message.reply_text("TC yaz:")
    return B_TC

def get_b_tc(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["b_tc"] = update.message.text.strip()
    update.message.reply_text("Ad yaz:")
    return B_NAME

def get_b_name(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["b_name"] = update.message.text
    update.message.reply_text("Soyad yaz:")
    return B_SURNAME

def get_b_surname(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["b_surname"] = update.message.text
    update.message.reply_text("Miktar yaz (örn: 5.000):")
    return B_MIKTAR

def get_b_miktar(update: Update, context: CallbackContext):
    if not _check_group(update):
        return ConversationHandler.END
    context.user_data["b_miktar"] = update.message.text.strip()
    update.message.reply_text("⏳ BURS PDF hazırlanıyor")
    name_up = tr_upper(context.user_data["b_name"])
    surname_up = tr_upper(context.user_data["b_surname"])
    pd
