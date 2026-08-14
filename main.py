import os
import io
import json
import time
import base64
import threading
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

# =============================================================================
# PetFortune Bot — "Evcil Hayvanınızın Fotoğrafını Atın, Geleceğinizle İlgili
# Bilgiler Verelim"
#
# Flow:
#   1. User sends a photo of their pet.
#   2. Gemini (vision, free tier via Google AI Studio) looks at the photo and
#      writes a short, fun "fortune"
#      about the user, framed through the pet's expression/pose/vibe.
#   3. We render that text onto a nice shareable card using the pet photo.
#   4. We send a BLURRED preview for free, then a Telegram Stars invoice.
#   5. On successful payment, we send the full, unblurred card.
#
# Required environment variables (set these in Railway):
#   TOKEN              - Telegram bot token from @BotFather
#   GEMINI_API_KEY      - free API key from aistudio.google.com
#   STARS_PRICE        - price in Telegram Stars for one fortune (default 50 ≈ $1)
# =============================================================================

VERSION = "PetFortune V1.0"
TOKEN = os.getenv("TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
STARS_PRICE = int(os.getenv("STARS_PRICE", "50"))  # Telegram Stars, ~50 ≈ $1

if not TOKEN:
    raise RuntimeError("TOKEN env variable missing (Telegram bot token from @BotFather)")
if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY missing — fortune generation will fail until it's set.", flush=True)

TG_API = f"https://api.telegram.org/bot{TOKEN}"
# Fonts are bundled in the repo root (same folder as this file) rather than
# relying on system fonts, so this works the same on Railway regardless of
# the base image.
FONT_DIR = os.path.dirname(os.path.abspath(__file__))

# In-memory session store: chat_id -> pending fortune data waiting for payment
pending_fortunes = {}
pending_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def tg_call(method, data=None, files=None, timeout=30):
    url = f"{TG_API}/{method}"
    if files:
        boundary = "----PetFortuneBoundary"
        body = b""
        for key, value in (data or {}).items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        for key, (filename, filedata) in files.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
            body += filedata
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    else:
        body = urllib.parse.urlencode(data or {}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def send_text(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        return tg_call("sendMessage", data)
    except Exception as e:
        print("SEND TEXT ERROR:", repr(e), flush=True)


def send_photo_bytes(chat_id, photo_bytes, caption=None, reply_markup=None):
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        return tg_call("sendPhoto", data, files={"photo": ("card.jpg", photo_bytes)})
    except Exception as e:
        print("SEND PHOTO ERROR:", repr(e), flush=True)


def get_file_bytes(file_id):
    info = tg_call("getFile", {"file_id": file_id})
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def send_invoice(chat_id, title, description, payload):
    data = {
        "chat_id": chat_id,
        "title": title,
        "description": description,
        "payload": payload,
        "provider_token": "",  # empty for Telegram Stars
        "currency": "XTR",
        "prices": json.dumps([{"label": "Fal Kartı", "amount": STARS_PRICE}]),
    }
    try:
        return tg_call("sendInvoice", data)
    except Exception as e:
        print("SEND INVOICE ERROR:", repr(e), flush=True)


def answer_pre_checkout(pre_checkout_query_id, ok=True, error_message=None):
    data = {"pre_checkout_query_id": pre_checkout_query_id, "ok": "true" if ok else "false"}
    if error_message:
        data["error_message"] = error_message
    try:
        return tg_call("answerPreCheckoutQuery", data)
    except Exception as e:
        print("PRE-CHECKOUT ERROR:", repr(e), flush=True)


# ---------------------------------------------------------------------------
# Gemini vision — generate the fortune text from the pet photo
# ---------------------------------------------------------------------------

def generate_fortune(image_bytes):
    """Send the pet photo to Gemini (free tier, via Google AI Studio) and get
    back a short, fun fortune written as if the pet is speaking."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "Bu bir evcil hayvan fotoğrafı. Sanki bu hayvan konuşabiliyormuş ve "
        "sahibi hakkında (fotoğraftaki pozundan, ifadesinden, gözlerinden yola çıkarak) "
        "sıcak, eğlenceli, esprili bir şeyler söylüyormuş gibi yaz — hayvanın ağzından, "
        "birinci tekil şahıs gibi konuş ('Sahibimin bugün...' tarzı). "
        "Ciddi bir kehanet değil, samimi ve gülümseten bir ton kullan. "
        "Türkçe yaz. En fazla 3 kısa cümle, toplam 40 kelimeyi geçme. "
        "Sadece metni yaz, başka açıklama ekleme."
    )
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                {"text": prompt},
            ]
        }]
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent?" + urllib.parse.urlencode({"key": GEMINI_API_KEY})
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------

def _font(size, bold=False):
    name = "DejaVuSerif-Bold.ttf" if bold else "DejaVuSerif.ttf"
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


def _wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_card(pet_photo_bytes, fortune_text, blurred=False):
    """Compose the pet photo + decorative frame + fortune text into a card."""
    W, H = 1080, 1350
    pet_img = Image.open(io.BytesIO(pet_photo_bytes)).convert("RGB")
    pet_img = ImageOps.fit(pet_img, (W, int(H * 0.62)), Image.LANCZOS)

    canvas = Image.new("RGB", (W, H), (24, 20, 32))
    canvas.paste(pet_img, (0, 0))

    if blurred:
        blurred_region = canvas.crop((0, 0, W, int(H * 0.62))).filter(ImageFilter.GaussianBlur(28))
        canvas.paste(blurred_region, (0, 0))

    # Gradient-ish overlay band where the photo meets the text area
    overlay = Image.new("RGBA", (W, 120), (24, 20, 32, 0))
    for y in range(120):
        alpha = int(255 * (y / 120))
        ImageDraw.Draw(overlay).line([(0, y), (W, y)], fill=(24, 20, 32, alpha))
    canvas.paste(Image.alpha_composite(
        canvas.crop((0, int(H * 0.62) - 120, W, int(H * 0.62))).convert("RGBA"), overlay
    ).convert("RGB"), (0, int(H * 0.62) - 120))

    draw = ImageDraw.Draw(canvas)

    # Title (no emoji here — the built-in fonts don't have emoji glyphs;
    # Telegram itself renders emoji fine in the caption text instead)
    title_font = _font(56, bold=True)
    title = "* SENİN İÇİN NE DİYOR? *"
    tw = draw.textlength(title, font=title_font)
    draw.text(((W - tw) / 2, int(H * 0.62) + 30), title, font=title_font, fill=(255, 215, 130))

    # Fortune text (blurred out with dots if this is the preview)
    body_font = _font(40)
    text_area_width = W - 140
    if blurred:
        display_text = "● " * 5 + "\n" + "● " * 7 + "\n" + "● " * 4
        lines = display_text.split("\n")
    else:
        lines = _wrap_text(draw, fortune_text, body_font, text_area_width)

    y = int(H * 0.62) + 130
    for line in lines:
        lw = draw.textlength(line, font=body_font)
        draw.text(((W - lw) / 2, y), line, font=body_font, fill=(240, 235, 250))
        y += 54

    # Footer
    footer_font = _font(28)
    footer = "@PetFortuneBot ile oluşturuldu"
    fw = draw.textlength(footer, font=footer_font)
    draw.text(((W - fw) / 2, H - 60), footer, font=footer_font, fill=(150, 140, 170))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

def handle_photo(chat_id, file_id):
    send_text(chat_id, "🐾 Fotoğrafını inceliyorum, birkaç saniye sürebilir...")
    try:
        photo_bytes = get_file_bytes(file_id)
    except Exception as e:
        print("PHOTO FETCH ERROR:", repr(e), flush=True)
        send_text(chat_id, "❌ Fotoğrafı indiremedim, tekrar dener misin?")
        return

    try:
        fortune_text = generate_fortune(photo_bytes)
    except Exception as e:
        print("FORTUNE ERROR:", repr(e), flush=True)
        send_text(chat_id, "❌ Fal oluşturulurken bir hata oldu, tekrar dener misin?")
        return

    try:
        preview_bytes = render_card(photo_bytes, fortune_text, blurred=True)
    except Exception as e:
        print("RENDER ERROR:", repr(e), flush=True)
        send_text(chat_id, "❌ Kart oluşturulurken bir hata oldu, tekrar dener misin?")
        return

    with pending_lock:
        pending_fortunes[chat_id] = {
            "photo_bytes": photo_bytes,
            "fortune_text": fortune_text,
            "created": time.time(),
        }

    send_photo_bytes(
        chat_id, preview_bytes,
        caption="✨ Falın hazır! Tam halini açmak için aşağıya dokun.",
    )
    send_invoice(
        chat_id,
        title="Sana Ne Diyor?",
        description="Evcil hayvanının senin hakkında söylediklerinin tam halini aç.",
        payload=f"fortune_{chat_id}_{int(time.time())}",
    )


def handle_successful_payment(chat_id):
    with pending_lock:
        rec = pending_fortunes.pop(chat_id, None)
    if not rec:
        send_text(chat_id, "Ödemen alındı ama bekleyen bir falın bulunamadı — lütfen tekrar fotoğraf gönder.")
        return
    try:
        full_bytes = render_card(rec["photo_bytes"], rec["fortune_text"], blurred=False)
    except Exception as e:
        print("FULL RENDER ERROR:", repr(e), flush=True)
        send_text(chat_id, "❌ Kart oluşturulurken hata oldu, lütfen bize ulaşın.")
        return
    send_photo_bytes(
        chat_id, full_bytes,
        caption="🎉 İşte tam falın! Beğendiysen arkadaşlarınla paylaş 🐾",
    )


def process_update(update):
    if "message" in update:
        message = update["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        if "successful_payment" in message:
            handle_successful_payment(chat_id)
            return

        if "photo" in message:
            file_id = message["photo"][-1]["file_id"]  # largest size
            threading.Thread(target=handle_photo, args=(chat_id, file_id), daemon=True).start()
            return

        if text == "/start":
            send_text(
                chat_id,
                "🐾 Evcil hayvanın konuşsaydı, senin hakkında ne söylerdi hiç merak ettin mi?\n\n"
                "Bir fotoğrafını gönder, sana ne diyor öğrenelim. "
                f"Tam falı görmek sadece {STARS_PRICE} ⭐."
            )
            return

        if text == "/help":
            send_text(chat_id, "Sadece bir fotoğraf gönder, gerisini ben hallederim 🐾")
            return

        send_text(chat_id, "Bir evcil hayvan fotoğrafı gönder, sana özel fal hazırlayayım 🐾")

    elif "pre_checkout_query" in update:
        pcq = update["pre_checkout_query"]
        answer_pre_checkout(pcq["id"], ok=True)


# ---------------------------------------------------------------------------
# Polling loop + health server (same pattern as HunterElite)
# ---------------------------------------------------------------------------

def polling():
    offset = None
    while True:
        try:
            data = {"timeout": 25, "allowed_updates": json.dumps(["message", "pre_checkout_query"])}
            if offset is not None:
                data["offset"] = offset
            response = tg_call("getUpdates", data, timeout=35)
            for update in response.get("result", []):
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = update_id + 1
                try:
                    process_update(update)
                except Exception as e:
                    print("UPDATE PROCESS ERROR:", repr(e), flush=True)
        except Exception as e:
            print("POLL ERROR:", repr(e), flush=True)
            time.sleep(3)


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"{VERSION} ONLINE".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def health_server():
    port = int(os.getenv("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


if __name__ == "__main__":
    print(f"{VERSION} STARTING", flush=True)
    try:
        tg_call("deleteWebhook", {"drop_pending_updates": "false"})
    except Exception as e:
        print("WEBHOOK CLEAN WARNING:", repr(e), flush=True)
    threading.Thread(target=health_server, daemon=True).start()
    polling()
