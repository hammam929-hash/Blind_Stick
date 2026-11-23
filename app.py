import json
import os
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, render_template, request

try:
    import serial  # type: ignore
except ImportError:
    serial = None

SERIAL_PORT = os.environ.get("SERIAL_PORT", "COM3")
BAUD_RATE = int(os.environ.get("BAUD_RATE", "115200"))
SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "med_schedule.json")

app = Flask(__name__)

serial_lock = threading.Lock()
state_lock = threading.Lock()

ser = None
logs = []
last_event = None
latest_distance = None
obstacle_detected = False
med_alert_state = False
active_med_name = None
serial_status = "cloud_only" if serial is None else "disconnected"
med_schedules = []


def add_log(direction: str, message: str, echo_to_console: bool = True):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "direction": direction,
        "message": message,
    }
    with state_lock:
        logs.append(entry)
        if len(logs) > 250:
            del logs[: len(logs) - 250]
    if echo_to_console:
        print(f"[{entry['time']}] [{direction}] {message}", flush=True)


def load_schedules():
    global med_schedules
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    med_schedules = data
                    return
        except Exception as e:
            add_log("SYS", f"تعذر قراءة جدول الأدوية: {e}")
    med_schedules = []


def save_schedules():
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(med_schedules, f, ensure_ascii=False, indent=2)
    except Exception as e:
        add_log("SYS", f"تعذر حفظ جدول الأدوية: {e}")


def parse_arduino_line(line: str):
    global last_event, latest_distance, obstacle_detected, med_alert_state, active_med_name

    if line.startswith("{") and line.endswith("}"):
        try:
            data = json.loads(line)
            event_name = data.get("event", "")

            if event_name == "DISTANCE":
                cm_val = data.get("cm")
                with state_lock:
                    if isinstance(cm_val, (int, float)):
                        latest_distance = round(float(cm_val), 1)
                    obstacle_detected = bool(data.get("obstacle", False))
                return

            with state_lock:
                last_event = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "event": event_name,
                    "raw": data,
                }
                if event_name in ("MED_STOPPED", "ALL_STOPPED"):
                    med_alert_state = False
                    active_med_name = None
                    obstacle_detected = False

            add_log("RX", line)
            return
        except json.JSONDecodeError:
            pass

    add_log("RX", line)


def serial_reader_loop():
    global ser, serial_status
    if serial is None:
        add_log("SYS", "وضع السحابة نشط (لا يوجد منفذ Serial على الخادم).")
        return

    while True:
        if ser is None or not ser.is_open:
            try:
                with state_lock:
                    serial_status = "connecting"
                ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.5)
                time.sleep(2.0)
                with state_lock:
                    serial_status = "connected"
                add_log("SYS", f"تم الاتصال بالمنفذ {SERIAL_PORT} بسرعة {BAUD_RATE}")
            except Exception:
                with state_lock:
                    serial_status = "disconnected"
                ser = None
                time.sleep(3.0)
                continue

        try:
            raw_line = ser.readline()
            if raw_line:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    parse_arduino_line(line)
        except Exception as e:
            add_log("SYS", f"انقطع الاتصال بالسيريال: {e}")
            with state_lock:
                serial_status = "disconnected"
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(2.0)


def send_serial_command(cmd: str, med_name: str | None = None) -> tuple[bool, str]:
    global med_alert_state, active_med_name, last_event, obstacle_detected
    clean_cmd = cmd.strip().upper()
    if not clean_cmd:
        return False, "الأمر فارغ"

    is_med_on = clean_cmd in ("MED_ALERT:1", "MED_ON", "ALERT:TRUE", "1")
    is_stop = clean_cmd in ("STOP_ALL", "STOP", "CLEAR_ALERT", "MED_ALERT:0", "MED_OFF", "ALERT:FALSE", "0")

    with serial_lock:
        serial_sent = False
        if ser is not None and ser.is_open:
            try:
                ser.write((clean_cmd + "\n").encode("utf-8"))
                ser.flush()
                serial_sent = True
            except Exception as e:
                add_log("SYS", f"تعذر الإرسال عبر السيريال: {e}")

        add_log("TX", clean_cmd + ("" if serial_sent else " [CLOUD/NO-SERIAL]"))

        with state_lock:
            if is_med_on:
                med_alert_state = True
                active_med_name = med_name or active_med_name or "موعد دواء"
            elif is_stop:
                med_alert_state = False
                active_med_name = None
                obstacle_detected = False
                if last_event and last_event.get("event") == "SOS":
                    last_event = {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "event": "ALL_STOPPED",
                        "raw": {"event": "ALL_STOPPED"},
                    }

    return True, f"تم تنفيذ الأمر: {clean_cmd}"


def scheduler_loop():
    while True:
        now_dt = datetime.now()
        current_hm = now_dt.strftime("%H:%M")
        current_stamp = now_dt.strftime("%Y-%m-%d %H:%M")

        due_item = None
        with state_lock:
            for item in med_schedules:
                if not item.get("enabled", True):
                    continue
                if item.get("time") == current_hm and item.get("last_triggered_minute") != current_stamp:
                    item["last_triggered_minute"] = current_stamp
                    due_item = dict(item)
                    save_schedules()
                    break

        if due_item:
            med_label = f"{due_item.get('name', 'دواء')} ({due_item.get('time')})"
            add_log("SYS", f"حان موعد الدواء: {med_label}")
            send_serial_command("MED_ALERT:1", med_name=med_label)

        time.sleep(1.0)


# تهيئة التطبيق والخيوط الخلفية عند الاستيراد بواسطة Gunicorn أو التشغيل المباشر
load_schedules()
threading.Thread(target=serial_reader_loop, daemon=True).start()
threading.Thread(target=scheduler_loop, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def get_status():
    with state_lock:
        return jsonify({
            "serial_status": serial_status,
            "port": SERIAL_PORT,
            "baud": BAUD_RATE,
            "med_alert": med_alert_state,
            "active_med_name": active_med_name,
            "schedules": med_schedules,
            "distance_cm": latest_distance,
            "obstacle": obstacle_detected,
            "last_event": last_event,
            "logs": logs[-80:],
        })


@app.route("/api/schedules", methods=["GET", "POST"])
def manage_schedules():
    if request.method == "GET":
        with state_lock:
            return jsonify({"schedules": med_schedules})

    data = request.get_json(silent=True) or {}
    med_time = str(data.get("time", "")).strip()
    med_name = str(data.get("name", "")).strip()

    try:
        parsed = datetime.strptime(med_time, "%H:%M")
        normalized_time = parsed.strftime("%H:%M")
    except ValueError:
        return jsonify({"ok": False, "message": "صيغة الوقت غير صحيحة، استخدم HH:MM"}), 400

    if not med_name:
        med_name = "دواء غير مسمّى"

    new_item = {
        "id": f"{int(time.time() * 1000)}",
        "time": normalized_time,
        "name": med_name,
        "enabled": True,
        "last_triggered_minute": "",
    }

    with state_lock:
        med_schedules.append(new_item)
        med_schedules.sort(key=lambda x: x.get("time", ""))
        save_schedules()

    add_log("SYS", f"تمت إضافة موعد دواء: {med_name} الساعة {normalized_time}")
    return jsonify({"ok": True, "schedule": new_item, "schedules": med_schedules}), 201


@app.route("/api/schedules/<sched_id>", methods=["PATCH", "DELETE"])
def modify_schedule(sched_id):
    with state_lock:
        idx = next((i for i, s in enumerate(med_schedules) if s.get("id") == sched_id), None)
        if idx is None:
            return jsonify({"ok": False, "message": "الموعد غير موجود"}), 404

        if request.method == "DELETE":
            removed = med_schedules.pop(idx)
            save_schedules()
            add_log("SYS", f"تم حذف موعد الدواء: {removed.get('name')} ({removed.get('time')})")
            return jsonify({"ok": True, "schedules": med_schedules})

        data = request.get_json(silent=True) or {}
        if "enabled" in data:
            med_schedules[idx]["enabled"] = bool(data["enabled"])
        save_schedules()
        return jsonify({"ok": True, "schedule": med_schedules[idx], "schedules": med_schedules})


@app.route("/api/command", methods=["POST"])
def post_command():
    data = request.get_json(silent=True) or {}
    cmd = str(data.get("command", ""))
    med_name = data.get("med_name")
    ok, msg = send_serial_command(cmd, med_name=med_name if isinstance(med_name, str) else None)
    return jsonify({
        "ok": ok,
        "message": msg,
        "med_alert": med_alert_state,
        "active_med_name": active_med_name,
        "last_event": last_event,
    }), (200 if ok else 400)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)