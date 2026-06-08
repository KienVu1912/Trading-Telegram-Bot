import urllib.request
import urllib.error
import json
import time
import datetime
import math
import logging
import sys
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Khắc phục lỗi hiển thị tiếng Việt có dấu và emoji trên Command Prompt / PowerShell của Windows
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# Configure beautiful console logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ultimate-v5-bot")

# ==========================================================
# CẤU HÌNH THÔNG TIN BOT TELEGRAM CỦA BẠN TẠI ĐÂY
# ==========================================================
TELEGRAM_BOT_TOKEN = "8872959175:AAHuNvRb629xV9kGVWIKBXOIMsEhwfKVhDY"     # Điền Telegram Bot Token của bạn
TELEGRAM_CHAT_ID = "7312073144"         # Điền Chat ID Telegram của bạn

# Danh sách các đồng coin quét song song (BTC, ETH, SOL)
# Danh sách các đồng coin quét song song (tăng lên 12 đồng coin top)
SYMBOLS = [
    "BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "XRPUSD", 
    "ADAUSD", "DOGEUSD", "LINKUSD", "LTCUSD", "NEARUSD", 
    "AVAXUSD", "DOTUSD"
]
INTERVAL = "15m"                           # Khung thời gian quét
STATE_FILE = "positions_state.json"        # Tệp lưu trữ trạng thái vị thế JSON
# ==========================================================

# Chỉ báo cấu hình mặc định (Khớp với Strategy V5)
EMA_LEN = 200
ST_MULT = 2.5
ST_PERIOD = 10
CHOP_LEN = 14
CHOP_THRESH = 50.0
ATR_LEN = 14
TP_RATIO = 1.0     # TP1 1:1 R:R
CONFIRMATION_TIMEOUT = 600  # Thời gian chờ xác nhận (10 phút)

# Khởi tạo cấu trúc lưu trạng thái vị thế độc lập
positions = {}
for sym in SYMBOLS:
    positions[sym] = {
        "active_position": None,
        "entry_price": 0.0,
        "initial_sl": 0.0,
        "current_sl": 0.0,
        "tp_part_price": 0.0,
        "is_partial_closed": False,
        "is_sl_moved_to_be": False,
        "pending_timestamp": 0.0,
        "pending_msg_id": None,
        "last_signal_time": 0
    }

def save_state():
    """Lưu trạng thái các vị thế xuống file JSON để tránh mất dữ liệu khi khởi động lại"""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(positions, f, indent=4)
        logger.info("💾 Đã lưu trạng thái các vị thế xuống file JSON.")
    except Exception as e:
        logger.error(f"🔴 Lỗi ghi file trạng thái JSON: {e}")

def load_state():
    """Khôi phục trạng thái vị thế từ file JSON nếu có"""
    global positions
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                loaded = json.load(f)
                for sym in SYMBOLS:
                    if sym in loaded:
                        positions[sym] = loaded[sym]
                        # Đảm bảo trường mới tồn tại để tránh lỗi KeyError
                        if "last_signal_time" not in positions[sym]:
                            positions[sym]["last_signal_time"] = 0
            logger.info("💾 Đã khôi phục thành công trạng thái các vị thế từ tệp JSON cũ!")
        except Exception as e:
            logger.error(f"🔴 Lỗi đọc file trạng thái JSON: {e}")

def send_telegram_message(text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        req = urllib.request.Request(
            url, 
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode())
            if res_data.get("ok"):
                logger.info("🟢 Đã gửi tin nhắn thành công tới Telegram!")
                return res_data.get("result", {}).get("message_id")
            else:
                logger.error(f"🔴 Gửi tới Telegram thất bại: {res_data}")
    except Exception as e:
        logger.error(f"🔴 Lỗi kết nối Telegram: {e}")
    return None

def edit_telegram_message(message_id, text, reply_markup=None):
    if not message_id:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        req = urllib.request.Request(
            url, 
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode())
            return res_data.get("ok", False)
    except Exception as e:
        logger.error(f"🔴 Lỗi chỉnh sửa tin nhắn Telegram: {e}")
    return False

telegram_update_offset = 0

def init_telegram_offset():
    global telegram_update_offset
    # Tự động hủy Webhook cũ để tránh lỗi HTTP 409 Conflict khi dùng getUpdates (polling)
    url_del = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
    try:
        req_del = urllib.request.Request(url_del, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req_del, timeout=5) as response:
            res_data = json.loads(response.read().decode())
            if res_data.get("ok"):
                logger.info("🧹 Đã hủy webhook cũ trên Telegram để kích hoạt chế độ Polling (Tránh lỗi 409).")
    except Exception as e:
        logger.warning(f"⚠️ Không thể xóa webhook Telegram (Có thể bỏ qua): {e}")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            if data.get("ok"):
                updates = data.get("result", [])
                if updates:
                    telegram_update_offset = updates[-1].get("update_id") + 1
                    logger.info(f"💾 Đã bỏ qua các cập nhật cũ của Telegram. Offset mới: {telegram_update_offset}")
    except Exception as e:
        logger.error(f"🔴 Không thể khởi tạo Telegram offset: {e}")

def poll_telegram_updates():
    global telegram_update_offset
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = "?timeout=1"
    if telegram_update_offset > 0:
        params += f"&offset={telegram_update_offset}"
    
    try:
        req = urllib.request.Request(url + params, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            if not data.get("ok"):
                return
            
            updates = data.get("result", [])
            for update in updates:
                telegram_update_offset = update.get("update_id") + 1
                callback_query = update.get("callback_query")
                if callback_query:
                    callback_id = callback_query.get("id")
                    callback_data = callback_query.get("data")
                    message = callback_query.get("message", {})
                    msg_id = message.get("message_id")
                    
                    # Trả lời callback query ngay lập tức để tắt vòng xoay loading
                    answer_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
                    answer_payload = {"callback_query_id": callback_id}
                    try:
                        ans_req = urllib.request.Request(
                            answer_url,
                            data=json.dumps(answer_payload).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        urllib.request.urlopen(ans_req, timeout=2).close()
                    except Exception:
                        pass
                    
                    if callback_data:
                        if callback_data.startswith("confirm_"):
                            parts = callback_data.split("_")
                            if len(parts) == 3:
                                dir_val = parts[1]
                                sym_val = parts[2]
                                pos = positions.get(sym_val)
                                if pos and pos["active_position"] == f"PENDING_{dir_val}":
                                    pos["active_position"] = dir_val
                                    save_state()
                                    
                                    risk = abs(pos["entry_price"] - pos["initial_sl"])
                                    tp_15r = pos["entry_price"] + (risk * 1.5 if dir_val == "LONG" else -risk * 1.5)
                                    action_str = "MUA" if dir_val == "LONG" else "BÁN"
                                    color_emoji = "🟢" if dir_val == "LONG" else "🔴"
                                    
                                    new_text = (
                                        f"{color_emoji} <b>{action_str} {sym_val} ({INTERVAL}) - ĐÃ VÀO LỆNH</b>\n\n"
                                        f"👉 <b>Giá vào:</b> {pos['entry_price']:.2f}\n"
                                        f"🛡️ <b>Stop Loss:</b> {pos['initial_sl']:.2f} (Rủi ro: -{risk:.2f})\n"
                                        f"🎯 <b>TP1 (50%):</b> {pos['tp_part_price']:.2f} (+{risk:.2f} | 1.0R)\n"
                                        f"🎯 <b>TP2 (50%):</b> {tp_15r:.2f} (+{risk*1.5:.2f} | 1.5R)\n\n"
                                        f"✅ <i>Vị thế đã được kích hoạt và đang được bot quản lý tự động.</i>"
                                    )
                                    edit_telegram_message(msg_id, new_text, reply_markup={"inline_keyboard": []})
                                    logger.info(f"✅ Người dùng đã xác nhận vào lệnh {dir_val} {sym_val}")
                                    
                        elif callback_data.startswith("cancel_"):
                            sym_val = callback_data.split("_")[1]
                            pos = positions.get(sym_val)
                            if pos and pos["active_position"] in ["PENDING_LONG", "PENDING_SHORT"]:
                                old_dir = pos["active_position"]
                                pos["active_position"] = None
                                save_state()
                                
                                new_text = (
                                    f"❌ <b>ĐÃ HỦY TÍN HIỆU {sym_val} ({INTERVAL})</b>\n\n"
                                    f"Người dùng đã bấm bỏ qua tín hiệu {old_dir}."
                                )
                                edit_telegram_message(msg_id, new_text, reply_markup={"inline_keyboard": []})
                                logger.info(f"❌ Người dùng đã hủy bỏ tín hiệu {sym_val}")
    except urllib.error.URLError as ue:
        err_msg = str(ue)
        if "503" in err_msg or "Tunnel" in err_msg or "timed out" in err_msg:
            logger.warning("⚠️ Kết nối Telegram tạm thời bị gián đoạn (Proxy/Tunnel 503). Đang tự động kết nối lại...")
        else:
            logger.error(f"🔴 Lỗi kết nối poll Telegram: {ue}")
    except Exception as e:
        logger.error(f"🔴 Lỗi poll Telegram updates: {e}")

# Helper to map common trading symbols to Yahoo Finance format
def map_symbol_to_yahoo(sym: str) -> str:
    s = sym.upper().strip()
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}-USD"
    if s.endswith("USD"):
        base = s[:-3]
        # Ngoại lệ cho các cặp Forex truyền thống nếu có
        forex_bases = ["EUR", "GBP", "AUD", "NZD", "USD"]
        if base in forex_bases and len(s) == 6:
            return f"{s}=X"
        return f"{base}-USD"
        
    if len(s) == 6 and s.isalpha(): 
        return f"{s}=X"
    return s

def fetch_candles(sym: str, interval: str) -> list:
    yahoo_symbol = map_symbol_to_yahoo(sym)
    range_val = "10d" if interval in ["15m", "30m", "1h"] else "30d"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?interval={interval}&range={range_val}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    retries = 3
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.loads(response.read().decode())
                result = data.get("chart", {}).get("result", [])
                if not result:
                    return []
                
                chart_data = result[0]
                timestamps = chart_data.get("timestamp", [])
                indicators = chart_data.get("indicators", {}).get("quote", [{}])[0]
                
                opens = indicators.get("open", [])
                highs = indicators.get("high", [])
                lows = indicators.get("low", [])
                closes = indicators.get("close", [])
                volumes = indicators.get("volume", [])
                
                candles = []
                for i in range(len(timestamps)):
                    if opens[i] is not None and closes[i] is not None and highs[i] is not None and lows[i] is not None:
                        v_val = volumes[i] if (volumes and i < len(volumes) and volumes[i] is not None) else 0
                        candles.append({
                            "time": timestamps[i],
                            "open": opens[i],
                            "high": highs[i],
                            "low": lows[i],
                            "close": closes[i],
                            "volume": int(v_val)
                        })
                return candles
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"⚠️ Thử lại lần {attempt + 2} tải dữ liệu {sym} do gặp lỗi tạm thời: {e}")
                time.sleep(1)
            else:
                logger.error(f"Lỗi tải dữ liệu nến cho {sym} sau {retries} lần thử: {e}")
                return []
    return []

# Mathematical calculations for EMA
def calculate_ema(prices: list, length: int) -> list:
    ema = [0.0] * len(prices)
    if len(prices) < length:
        return ema
    sma = sum(prices[:length]) / length
    ema[length - 1] = sma
    alpha = 2.0 / (length + 1)
    for i in range(length, len(prices)):
        ema[i] = prices[i] * alpha + ema[i - 1] * (1 - alpha)
    return ema

# Mathematical calculations for ATR
def calculate_atr(candles: list, length: int = 14) -> list:
    n = len(candles)
    atr = [0.0] * n
    if n <= length:
        return atr
    tr = [0.0] * n
    for i in range(1, n):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr[length] = sum(tr[1:length+1]) / length
    for i in range(length + 1, n):
        atr[i] = (atr[i-1] * (length - 1) + tr[i]) / length
    return atr

# Mathematical calculations for Choppiness Index (CHOP)
def calculate_chop(candles: list, length: int = 14) -> list:
    n = len(candles)
    chop = [100.0] * n
    if n <= length:
        return chop
    tr = [0.0] * n
    for i in range(1, n):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        
    for i in range(length, n):
        sum_tr = sum(tr[i-length+1 : i+1])
        max_high = max([c["high"] for c in candles[i-length+1 : i+1]])
        min_low = min([c["low"] for c in candles[i-length+1 : i+1]])
        
        diff = max_high - min_low
        if diff > 0 and sum_tr > 0:
            chop[i] = 100.0 * math.log10(sum_tr / diff) / math.log10(length)
        else:
            chop[i] = 50.0
    return chop

# Mathematical calculations for Supertrend (10, 2.5)
def calculate_supertrend(candles: list, period: int = 10, multiplier: float = 2.5) -> tuple:
    n = len(candles)
    atr = calculate_atr(candles, period)
    hl2 = [(c["high"] + c["low"]) / 2 for c in candles]
    
    basic_upper = [0.0] * n
    basic_lower = [0.0] * n
    final_upper = [0.0] * n
    final_lower = [0.0] * n
    st = [0.0] * n
    direction = [1] * n
    
    for i in range(period, n):
        basic_upper[i] = hl2[i] + multiplier * atr[i]
        basic_lower[i] = hl2[i] - multiplier * atr[i]
        
        if basic_upper[i] < final_upper[i-1] or candles[i-1]["close"] > final_upper[i-1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i-1]
            
        if basic_lower[i] > final_lower[i-1] or candles[i-1]["close"] < final_lower[i-1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i-1]
            
        if candles[i]["close"] > final_upper[i-1]:
            direction[i] = -1
        elif candles[i]["close"] < final_lower[i-1]:
            direction[i] = 1
        else:
            direction[i] = direction[i-1]
            
        st[i] = final_lower[i] if direction[i] == -1 else final_upper[i]
        
    return st, direction

def check_signals_for_symbol(sym: str):
    pos = positions[sym]
    
    # Kiểm tra quá hạn timeout cho tín hiệu chờ xác nhận
    if pos["active_position"] in ["PENDING_LONG", "PENDING_SHORT"]:
        time_elapsed = time.time() - pos.get("pending_timestamp", 0)
        if time_elapsed > CONFIRMATION_TIMEOUT:
            old_dir = pos["active_position"]
            msg_id = pos.get("pending_msg_id")
            pos["active_position"] = None
            save_state()
            
            new_text = (
                f"⏰ <b>TÍN HIỆU HẾT HẠN - {sym} ({INTERVAL})</b>\n\n"
                f"Tín hiệu {old_dir} đã tự động hủy do quá {CONFIRMATION_TIMEOUT // 60} phút không có xác nhận."
            )
            edit_telegram_message(msg_id, new_text, reply_markup={"inline_keyboard": []})
            logger.info(f"⏰ Tín hiệu {sym} {old_dir} đã tự động hủy do hết hạn chờ xác nhận.")
        return
        
    candles = fetch_candles(sym, INTERVAL)
    if len(candles) < 220:
        logger.warning(f"Không tải đủ số lượng nến cho {sym}.")
        return
    
    closes = [c["close"] for c in candles]
    
    # Calculate indicators
    ema200 = calculate_ema(closes, EMA_LEN)
    st, direction = calculate_supertrend(candles, ST_PERIOD, ST_MULT)
    chop = calculate_chop(candles, CHOP_LEN)
    atr = calculate_atr(candles, ATR_LEN)
    
    # Xác định nến đóng hoàn chỉnh gần nhất dựa trên thời gian thực
    # (Tránh lỗi Yahoo Finance trễ, lúc ẩn lúc hiện nến chạy trực tiếp ở cuối danh sách)
    now_ts = time.time()
    last_candle = candles[-1]
    
    # Một nến 15m dài 900 giây. Nếu thời gian hiện tại đã vượt qua thời gian bắt đầu nến cuối + 900 giây,
    # nghĩa là nến cuối cùng đã đóng cửa hoàn chỉnh.
    if last_candle["time"] + 900 <= now_ts:
        idx = len(candles) - 1  # Nến cuối cùng trong danh sách đã đóng cửa
    else:
        idx = len(candles) - 2  # Nến cuối cùng vẫn đang chạy live, lấy nến kế cuối làm nến đã đóng
        
    c = candles[idx]
    c_live = candles[-1]        # Nến chạy trực tiếp để kiểm tra SL/TP tức thời
    
    is_trending = chop[idx] < CHOP_THRESH
    atr_sma = sum(atr[idx-49:idx+1]) / 50.0
    volatility_ok = atr[idx] > atr_sma
    
    # ==========================================
    # QUẢN LÝ VỊ THẾ ĐANG CHẠY (POSITION MANAGEMENT)
    # ==========================================
    if pos["active_position"] == 'LONG':
        # 1. Chốt lời 50% khi chạm TP1 (1R)
        if c_live["high"] >= pos["tp_part_price"] and not pos["is_partial_closed"]:
            pos["is_partial_closed"] = True
            pos["is_sl_moved_to_be"] = True
            pos["current_sl"] = pos["entry_price"]
            save_state()  # Lưu lại trạng thái
            
            risk_val = pos["entry_price"] - pos["initial_sl"]
            msg = (
                f"🎯 <b>[CHỐT LỜI 50% - LONG {sym}]</b>\n\n"
                f"👉 Đã chạm mục tiêu TP1 (1.0R) tại: <b>{pos['tp_part_price']:.2f}</b> (+{risk_val:.2f})\n"
                f"🛡️ <b>HÀNH ĐỘNG:</b>\n"
                f" - Chốt lời 50% vị thế.\n"
                f" - Dời Stop-loss về hòa vốn (Entry): <b>{pos['current_sl']:.2f}</b>"
            )
            send_telegram_message(msg)
            
        # 2. Cập nhật Trailing Stop theo Supertrend
        new_st_sl = st[idx]
        if new_st_sl > pos["current_sl"]:
            old_sl = pos["current_sl"]
            pos["current_sl"] = new_st_sl
            save_state()
            msg = (
                f"🔄 <b>[CẬP NHẬT STOP-LOSS - LONG {sym}]</b>\n\n"
                f"🛡️ Supertrend dịch chuyển lên.\n"
                f"👉 <b>SL mới:</b> <code>{pos['current_sl']:.2f}</code>\n"
                f"*(Mức cũ: {old_sl:.2f})*"
            )
            send_telegram_message(msg)
            
        # 3. Quét SL
        if c_live["low"] <= pos["current_sl"]:
            msg = (
                f"🛑 <b>[ĐÓNG VỊ THẾ - LONG {sym}]</b>\n\n"
                f"📉 Giá chạm Stop-loss tại: <b>{pos['current_sl']:.2f}</b>\n"
                f"👉 Đã thoát hoàn toàn phần vị thế còn lại."
            )
            send_telegram_message(msg)
            pos["active_position"] = None
            save_state()
            
    elif pos["active_position"] == 'SHORT':
        # 1. Chốt lời 50% khi chạm TP1 (1R)
        if c_live["low"] <= pos["tp_part_price"] and not pos["is_partial_closed"]:
            pos["is_partial_closed"] = True
            pos["is_sl_moved_to_be"] = True
            pos["current_sl"] = pos["entry_price"]
            save_state()
            
            risk_val = pos["initial_sl"] - pos["entry_price"]
            msg = (
                f"🎯 <b>[CHỐT LỜI 50% - SHORT {sym}]</b>\n\n"
                f"👉 Đã chạm mục tiêu TP1 (1.0R) tại: <b>{pos['tp_part_price']:.2f}</b> (+{risk_val:.2f})\n"
                f"🛡️ <b>HÀNH ĐỘNG:</b>\n"
                f" - Chốt lời 50% vị thế.\n"
                f" - Dời Stop-loss về hòa vốn (Entry): <b>{pos['current_sl']:.2f}</b>"
            )
            send_telegram_message(msg)
            
        # 2. Cập nhật Trailing Stop
        new_st_sl = st[idx]
        if new_st_sl < pos["current_sl"]:
            old_sl = pos["current_sl"]
            pos["current_sl"] = new_st_sl
            save_state()
            msg = (
                f"🔄 <b>[CẬP NHẬT STOP-LOSS - SHORT {sym}]</b>\n\n"
                f"🛡️ Supertrend dịch chuyển xuống.\n"
                f"👉 <b>SL mới:</b> <code>{pos['current_sl']:.2f}</code>\n"
                f"*(Mức cũ: {old_sl:.2f})*"
            )
            send_telegram_message(msg)
            
        # 3. Quét SL
        if c_live["high"] >= pos["current_sl"]:
            msg = (
                f"🛑 <b>[ĐÓNG VỊ THẾ - SHORT {sym}]</b>\n\n"
                f"📈 Giá chạm Stop-loss tại: <b>{pos['current_sl']:.2f}</b>\n"
                f"👉 Đã thoát hoàn toàn phần vị thế còn lại."
            )
            send_telegram_message(msg)
            pos["active_position"] = None
            save_state()
            
    # ==========================================
    # TÌM KIẾM TÍN HIỆU VÀO LỆNH MỚI
    # ==========================================
    else:
        # Chỉ kích hoạt tín hiệu nếu nến đóng hiện tại chưa từng phát tín hiệu
        is_new_candle = (c["time"] != pos.get("last_signal_time", 0))
        
        buy_signal = (direction[idx] < 0 and direction[idx-1] > 0) and (c["close"] > ema200[idx]) and is_trending and volatility_ok and is_new_candle
        sell_signal = (direction[idx] > 0 and direction[idx-1] < 0) and (c["close"] < ema200[idx]) and is_trending and volatility_ok and is_new_candle
        
        if buy_signal:
            pos["active_position"] = 'PENDING_LONG'
            pos["entry_price"] = c["close"]
            pos["initial_sl"] = st[idx]
            pos["current_sl"] = pos["initial_sl"]
            pos["pending_timestamp"] = time.time()
            pos["last_signal_time"] = c["time"]
            
            risk = pos["entry_price"] - pos["initial_sl"]
            pos["tp_part_price"] = pos["entry_price"] + risk * TP_RATIO
            tp_15r = pos["entry_price"] + risk * 1.5
            
            msg = (
                f"🔔 <b>[TÍN HIỆU LONG] {sym} ({INTERVAL})</b>\n\n"
                f"👉 <b>Giá vào:</b> {pos['entry_price']:.2f}\n"
                f"🛡️ <b>Stop Loss:</b> {pos['initial_sl']:.2f} (Rủi ro: -{risk:.2f})\n"
                f"🎯 <b>TP1 (50%):</b> {pos['tp_part_price']:.2f} (+{risk:.2f} | 1.0R)\n"
                f"🎯 <b>TP2 (50%):</b> {tp_15r:.2f} (+{risk*1.5:.2f} | 1.5R)\n\n"
                f"⏳ <i>Vui lòng xác nhận vào lệnh trong {CONFIRMATION_TIMEOUT // 60} phút...</i>"
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Đã vào lệnh", "callback_data": f"confirm_LONG_{sym}"},
                        {"text": "❌ Bỏ qua", "callback_data": f"cancel_{sym}"}
                    ]
                ]
            }
            msg_id = send_telegram_message(msg, reply_markup=keyboard)
            pos["pending_msg_id"] = msg_id
            save_state()
            
        elif sell_signal:
            pos["active_position"] = 'PENDING_SHORT'
            pos["entry_price"] = c["close"]
            pos["initial_sl"] = st[idx]
            pos["current_sl"] = pos["initial_sl"]
            pos["pending_timestamp"] = time.time()
            pos["last_signal_time"] = c["time"]
            
            risk = pos["initial_sl"] - pos["entry_price"]
            pos["tp_part_price"] = pos["entry_price"] - risk * TP_RATIO
            tp_15r = pos["entry_price"] - risk * 1.5
            
            msg = (
                f"🔔 <b>[TÍN HIỆU SHORT] {sym} ({INTERVAL})</b>\n\n"
                f"👉 <b>Giá vào:</b> {pos['entry_price']:.2f}\n"
                f"🛡️ <b>Stop Loss:</b> {pos['initial_sl']:.2f} (Rủi ro: -{risk:.2f})\n"
                f"🎯 <b>TP1 (50%):</b> {pos['tp_part_price']:.2f} (+{risk:.2f} | 1.0R)\n"
                f"🎯 <b>TP2 (50%):</b> {tp_15r:.2f} (+{risk*1.5:.2f} | 1.5R)\n\n"
                f"⏳ <i>Vui lòng xác nhận vào lệnh trong {CONFIRMATION_TIMEOUT // 60} phút...</i>"
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Đã vào lệnh", "callback_data": f"confirm_SHORT_{sym}"},
                        {"text": "❌ Bỏ qua", "callback_data": f"cancel_{sym}"}
                    ]
                ]
            }
            msg_id = send_telegram_message(msg, reply_markup=keyboard)
            pos["pending_msg_id"] = msg_id
            save_state()

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")
        
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        
    def log_message(self, format, *args):
        # Tắt log yêu cầu HTTP để giữ màn hình console sạch sẽ
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"🌐 Đã khởi chạy máy chủ Health Check trên cổng {port} để Render xác thực trạng thái.")
    server.serve_forever()

if __name__ == "__main__":
    logger.info("🚀 Khởi chạy Bot V5 quét đa sản phẩm & quản lý vị thế nâng cao!")
    if "YOUR_BOT" in TELEGRAM_BOT_TOKEN:
        logger.warning("⚠️ LƯU Ý: Bạn cần điền thông tin Token Telegram Bot và Chat ID trước khi khởi động!")
        sys.exit(1)
        
    # Khởi chạy server kiểm tra sức khỏe của Render trong thread phụ
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()
        
    # Khôi phục trạng thái vị thế từ tệp JSON nếu có
    load_state()
    
    # Khởi tạo offset Telegram để tránh xử lý tin nhắn cũ trước khi bot chạy
    init_telegram_offset()
        
    logger.info("⚡ Đang gửi thử tin nhắn Telegram kiểm tra kết nối...")
    send_telegram_message(
        "🔔 <b>[TEST CONNECT] Bot Quét Đa Sản Phẩm V5 PRO - Khung 15m</b>\n\n"
        "Đã kết nối thành công! Tôi sẽ quét song song các đồng coin, lưu trạng thái xuống tệp JSON và tính toán R:R chi tiết trong tin nhắn."
    )
        
    poll_interval = 10 
    
    while True:
        try:
            # Poll Telegram Updates
            poll_telegram_updates()
            
            for symbol in SYMBOLS:
                check_signals_for_symbol(symbol)
                poll_telegram_updates()
                time.sleep(2)
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("Đã dừng Bot.")
            break
        except Exception as e:
            logger.error(f"Lỗi vòng lặp quét đa sản phẩm: {e}")
            time.sleep(10)
