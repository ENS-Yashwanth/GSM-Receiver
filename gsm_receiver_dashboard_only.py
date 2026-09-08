import json
import re
import sys
import time
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import serial
import serial.tools.list_ports


# ============================================================
# CONFIGURATION
# ============================================================

SERIAL_PORT = "COM9"

# The RS2248/SIM800C USB stick normally works at 9600 baud.
BAUD_RATES = [9600, 115200, 57600, 38400, 19200]

SERIAL_TIMEOUT = 0.2

AUTO_ANSWER = True
ANSWER_DELAY_SEC = 0.2

# 0 = never automatically hang up.
HANGUP_AFTER_SEC = 0

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8080

LOG_FILE = "gsm_receiver.log"

# ============================================================
# GLOBAL STATE
# ============================================================

ser = None
current_baud = None

serial_lock = threading.Lock()
state_lock = threading.Lock()
stop_event = threading.Event()

state = {
    "status": "IDLE",
    "caller": "",
    "ring_count": 0,
    "connected_at": None,
    "last_event": "Starting receiver...",
    "audio": "NOT AVAILABLE VIA COM9",
    "audio_available": False,
    "muted": False,
}

last_answer_time = 0.0
ANSWER_COOLDOWN_SEC = 3.0


# ============================================================
# LOGGING
# ============================================================

def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    line = f"[{timestamp()}] {message}"
    print(line, flush=True)

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def set_state(**kwargs):
    with state_lock:
        state.update(kwargs)


def get_state():
    with state_lock:
        result = dict(state)

    connected_at = result.get("connected_at")
    if connected_at:
        result["duration"] = max(0, int(time.time() - connected_at))
    else:
        result["duration"] = 0

    return result


# ============================================================
# SERIAL HELPERS
# ============================================================

def send_raw(command, show=True):
    global ser

    if ser is None:
        return False

    try:
        with serial_lock:
            ser.write((command + "\r\n").encode("ascii", errors="ignore"))
            ser.flush()

        if show:
            log(f">> AT> {command}")

        return True

    except Exception as exc:
        log(f"SERIAL WRITE ERROR: {exc}")
        return False


def read_lines(seconds=1.0):
    if ser is None:
        return []

    end_time = time.time() + seconds
    buffer = ""
    lines = []

    while time.time() < end_time:
        try:
            with serial_lock:
                waiting = ser.in_waiting
                data = ser.read(waiting) if waiting else b""

            if data:
                buffer += data.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        lines.append(line)
            else:
                time.sleep(0.01)

        except Exception as exc:
            log(f"SERIAL READ ERROR: {exc}")
            break

    if buffer.strip():
        lines.append(buffer.strip())

    return lines


def send_command(command, expected="OK", timeout=3.0):
    if not send_raw(command):
        return False

    expected_list = [expected] if isinstance(expected, str) else expected
    end_time = time.time() + timeout
    buffer = ""

    while time.time() < end_time:
        try:
            with serial_lock:
                waiting = ser.in_waiting
                data = ser.read(waiting) if waiting else b""

            if not data:
                time.sleep(0.01)
                continue

            buffer += data.decode("utf-8", errors="replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()

                if not line:
                    continue

                log(f"GSM> {line}")
                upper = line.upper()

                for item in expected_list:
                    if item.upper() in upper:
                        return True

                if upper in ("ERROR", "NO CARRIER", "BUSY"):
                    return False

        except Exception as exc:
            log(f"COMMAND READ ERROR: {exc}")
            return False

    return False


# ============================================================
# MODEM DETECTION
# ============================================================

def list_com_ports():
    ports = list(serial.tools.list_ports.comports())

    log("Available COM ports:")
    if not ports:
        log("  No COM ports detected.")
        return

    for port in ports:
        log(f"  {port.device} - {port.description}")


def test_modem(baud):
    global ser, current_baud

    log(f"Testing {SERIAL_PORT} at {baud} baud...")

    try:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

        ser = serial.Serial(
            port=SERIAL_PORT,
            baudrate=baud,
            timeout=SERIAL_TIMEOUT,
            write_timeout=2,
        )

        current_baud = baud
        time.sleep(0.5)

        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass

        with serial_lock:
            ser.write(b"AT\r\n")
            ser.flush()

        end_time = time.time() + 2.0
        response = b""

        while time.time() < end_time:
            with serial_lock:
                data = ser.read(ser.in_waiting) if ser.in_waiting else b""

            if data:
                response += data
                if b"OK" in response.upper():
                    log(f"Modem detected at {baud} baud.")
                    return True

            time.sleep(0.02)

        try:
            ser.close()
        except Exception:
            pass

        ser = None
        return False

    except Exception as exc:
        log(f"Could not open {SERIAL_PORT} at {baud}: {exc}")
        ser = None
        return False


def find_modem():
    list_com_ports()

    available = [p.device for p in serial.tools.list_ports.comports()]
    if SERIAL_PORT not in available:
        log(f"WARNING: {SERIAL_PORT} is not currently listed.")

    for baud in BAUD_RATES:
        if test_modem(baud):
            return True

    return False


# ============================================================
# MODEM CONFIGURATION
# ============================================================

def configure_modem():
    log("Configuring SIM800C...")

    commands = [
        ("ATE0", "OK", 2),
        ("AT+CLIP=1", "OK", 2),
        ("AT+CRC=1", "OK", 2),

        # Audio-related modem settings.
        # These configure the SIM800C audio path itself.
        ("AT+CHFA=1", "OK", 2),
        ("AT+CLVL=90", "OK", 2),
        ("AT+CMIC=0,12", "OK", 2),
        ("AT+FMMUTE=0", "OK", 2),

        ("AT+CPIN?", "OK", 3),
        ("AT+CSQ", "OK", 3),
        ("AT+CREG?", "OK", 3),
        ("AT+COPS?", "OK", 5),
    ]

    for command, expected, timeout in commands:
        ok = send_command(command, expected, timeout)
        if not ok:
            log(f"Command did not return expected response: {command}")

    log("SIM800C configuration complete.")

    # IMPORTANT:
    # COM9 is the modem control/data interface. GSM voice audio is
    # not delivered as PCM audio through this serial port.
    set_state(
        audio="NOT AVAILABLE VIA COM9",
        audio_available=False,
        last_event="Modem configured; waiting for incoming call.",
    )


# ============================================================
# CALL CONTROL
# ============================================================

def extract_caller_number(line):
    match = re.search(r'\+CLIP:\s*"([^"]*)"', line, re.IGNORECASE)
    return match.group(1) if match else ""


def answer_call():
    global last_answer_time

    now = time.time()

    if now - last_answer_time < ANSWER_COOLDOWN_SEC:
        log("ATA skipped: answer cooldown active.")
        return False

    with state_lock:
        if state["status"] in ("ANSWERING", "CONNECTED"):
            log("ATA skipped: call is already being handled.")
            return False

    last_answer_time = now

    set_state(status="ANSWERING", last_event="Answering incoming call...")
    log(">> AUTO ANSWER: Sending ATA...")

    if not send_raw("ATA"):
        set_state(
            status="IDLE",
            last_event="Failed to send ATA.",
        )
        return False

    end_time = time.time() + 4.0
    got_ok = False
    got_connect = False
    got_voice_begin = False
    buffer = ""

    while time.time() < end_time:
        try:
            with serial_lock:
                waiting = ser.in_waiting
                data = ser.read(waiting) if waiting else b""

            if not data:
                time.sleep(0.01)
                continue

            buffer += data.decode("utf-8", errors="replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()

                if not line:
                    continue

                log(f"GSM> {line}")
                upper = line.upper()

                if upper == "OK":
                    got_ok = True

                elif upper == "CONNECT":
                    got_connect = True

                elif "VOICE CALL: BEGIN" in upper:
                    got_voice_begin = True

                elif upper.startswith("+CLIP:"):
                    number = extract_caller_number(line)
                    if number:
                        set_state(caller=number)

                elif upper in ("NO CARRIER", "BUSY", "ERROR"):
                    set_state(
                        status="IDLE",
                        connected_at=None,
                        last_event=f"Call answer failed: {line}",
                    )
                    return False

                if got_ok or got_connect or got_voice_begin:
                    set_state(
                        status="CONNECTED",
                        connected_at=time.time(),
                        last_event="Call connected.",
                    )

                    log(">> CALL ANSWERED SUCCESSFULLY")
                    return True

        except Exception as exc:
            log(f"ATA response error: {exc}")
            break

    if got_ok:
        # SIM800-family modules can return OK before the unsolicited
        # VOICE CALL: BEGIN notification.
        set_state(
            status="CONNECTED",
            connected_at=time.time(),
            last_event="ATA accepted; call is active.",
        )
        log(">> ATA returned OK; call marked active.")
        return True

    set_state(
        status="IDLE",
        connected_at=None,
        last_event="ATA sent but no valid answer response received.",
    )
    return False


def hangup_call():
    log(">> HANGING UP CALL...")

    ok = send_command("ATH", "OK", 3)

    set_state(
        status="IDLE",
        connected_at=None,
        caller="",
        last_event="Call ended.",
    )

    log(">> CALL HUNG UP." if ok else ">> Hang-up response was not OK.")
    return ok


# ============================================================
# SERIAL EVENT PROCESSING
# ============================================================

def process_line(line):
    line = line.strip()
    if not line:
        return

    upper = line.upper()
    log(f"GSM> {line}")

    # -------------------- RING --------------------

    if (
        upper == "RING"
        or upper.startswith("RING")
        or upper.startswith("+CRING:")
    ):
        with state_lock:
            current_status = state["status"]

        if current_status in ("ANSWERING", "CONNECTED"):
            return

        with state_lock:
            state["ring_count"] += 1
            ring_count = state["ring_count"]

        set_state(
            status="INCOMING",
            last_event="Incoming voice call detected.",
        )

        log(f">> INCOMING VOICE CALL DETECTED (ring {ring_count})")

        if AUTO_ANSWER:
            if ANSWER_DELAY_SEC > 0:
                time.sleep(ANSWER_DELAY_SEC)
            answer_call()

        return

    # -------------------- CALLER ID --------------------

    if upper.startswith("+CLIP:"):
        number = extract_caller_number(line)

        if number:
            set_state(
                caller=number,
                last_event=f"Caller ID received: {number}",
            )
            log(f">> CALLER NUMBER: {number}")

        return

    # -------------------- CONNECTED --------------------

    if upper == "CONNECT" or "VOICE CALL: BEGIN" in upper:
        set_state(
            status="CONNECTED",
            connected_at=time.time(),
            last_event="Voice call is active.",
        )

        log(">> VOICE CALL IS ACTIVE")
        return

    # -------------------- CALL ENDED --------------------

    if upper == "NO CARRIER":
        with state_lock:
            was_active = state["status"] in ("INCOMING", "ANSWERING", "CONNECTED")

        log(">> CALL ENDED" if was_active else ">> NO CARRIER")
        set_state(
            status="IDLE",
            connected_at=None,
            caller="",
            last_event="Call ended by network/remote side.",
        )
        return

    # -------------------- BUSY --------------------

    if upper == "BUSY":
        set_state(
            status="IDLE",
            connected_at=None,
            caller="",
            last_event="Call status: BUSY.",
        )
        log(">> CALL STATUS: BUSY")
        return


# ============================================================
# SERIAL MONITOR THREAD
# ============================================================

def serial_monitor():
    log("")
    log("=" * 64)
    log("SIM800C GSM RECEIVER + DASHBOARD")
    log("=" * 64)
    log(f"Serial port : {SERIAL_PORT}")
    log(f"Baud rate   : {current_baud}")
    log(f"Auto answer : {AUTO_ANSWER}")
    log(f"Dashboard   : http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    log("")
    log("Waiting for incoming calls...")
    log("")

    partial = ""

    while not stop_event.is_set():
        try:
            with serial_lock:
                waiting = ser.in_waiting
                data = ser.read(waiting) if waiting else b""

            if data:
                partial += data.decode("utf-8", errors="replace")

                while "\n" in partial:
                    line, partial = partial.split("\n", 1)
                    line = line.strip()
                    if line:
                        process_line(line)

            else:
                time.sleep(0.02)

            if HANGUP_AFTER_SEC > 0:
                current = get_state()
                if (
                    current["status"] == "CONNECTED"
                    and current["duration"] >= HANGUP_AFTER_SEC
                ):
                    hangup_call()

        except serial.SerialException as exc:
            log(f"SERIAL ERROR: {exc}")
            stop_event.set()
            break

        except Exception as exc:
            log(f"SERIAL MONITOR ERROR: {exc}")
            time.sleep(0.2)


# ============================================================
# DASHBOARD HTML
# ============================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GSM Call Dashboard</title>
<style>
    body {
        margin: 0;
        font-family: Arial, sans-serif;
        background: #111827;
        color: #f9fafb;
    }

    .container {
        max-width: 760px;
        margin: 40px auto;
        padding: 20px;
    }

    .card {
        background: #1f2937;
        border-radius: 16px;
        padding: 28px;
        box-shadow: 0 12px 30px rgba(0,0,0,.30);
    }

    h1 {
        margin-top: 0;
        text-align: center;
    }

    .status {
        text-align: center;
        font-size: 24px;
        font-weight: bold;
        margin: 25px 0;
    }

    .connected {
        color: #22c55e;
    }

    .incoming {
        color: #f59e0b;
    }

    .idle {
        color: #9ca3af;
    }

    .row {
        display: flex;
        justify-content: space-between;
        padding: 14px 0;
        border-bottom: 1px solid #374151;
    }

    .label {
        color: #9ca3af;
    }

    .value {
        font-weight: bold;
    }

    .audio-box {
        margin-top: 22px;
        padding: 16px;
        border-radius: 10px;
        background: #111827;
    }

    .meter {
        height: 16px;
        margin-top: 12px;
        background: #374151;
        border-radius: 8px;
        overflow: hidden;
    }

    .meter-fill {
        height: 100%;
        width: 0%;
        background: #22c55e;
        transition: width .15s;
    }

    button {
        border: 0;
        border-radius: 10px;
        padding: 13px 20px;
        margin: 8px 5px 0 0;
        cursor: pointer;
        font-size: 15px;
        font-weight: bold;
    }

    .hangup {
        background: #ef4444;
        color: white;
    }

    .mute {
        background: #374151;
        color: white;
    }

    button:disabled {
        opacity: .4;
        cursor: not-allowed;
    }

    .note {
        margin-top: 20px;
        padding: 14px;
        background: #3b2f12;
        color: #fbbf24;
        border-radius: 10px;
        line-height: 1.5;
    }

    .event {
        margin-top: 16px;
        color: #9ca3af;
        font-size: 13px;
    }
</style>
</head>

<body>
<div class="container">
    <div class="card">
        <h1>GSM CALL DASHBOARD</h1>

        <div id="status" class="status idle">IDLE</div>

        <div class="row">
            <span class="label">Caller</span>
            <span id="caller" class="value">--</span>
        </div>

        <div class="row">
            <span class="label">Call Duration</span>
            <span id="duration" class="value">00:00</span>
        </div>

        <div class="audio-box">
            <div>
                <span class="label">Audio</span>
                <span id="audioStatus" class="value">Checking...</span>
            </div>

            <div class="meter">
                <div id="meterFill" class="meter-fill"></div>
            </div>

            <button id="muteButton" class="mute" disabled>🔇 MUTE</button>
            <button id="hangupButton" class="hangup">📞 HANG UP</button>
        </div>

        <div class="note">
            <b>Audio limitation:</b>
            This RS2248/SIM800C USB COM9 connection provides the GSM
            control/serial interface. The GSM voice audio is not transferred
            as PCM audio through COM9. Therefore this dashboard can display
            and control the call, but it cannot play the transmitter voice
            from COM9 alone.
        </div>

        <div id="event" class="event">Starting...</div>
    </div>
</div>

<script>
let muted = false;

function formatDuration(seconds) {
    seconds = Number(seconds || 0);
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return String(m).padStart(2, '0') + ':' +
           String(s).padStart(2, '0');
}

function updateDashboard(data) {
    const status = document.getElementById('status');
    const caller = document.getElementById('caller');
    const duration = document.getElementById('duration');
    const audioStatus = document.getElementById('audioStatus');
    const meter = document.getElementById('meterFill');
    const event = document.getElementById('event');
    const hangup = document.getElementById('hangupButton');
    const mute = document.getElementById('muteButton');

    status.textContent = data.status;
    status.className = 'status ' +
        (data.status === 'CONNECTED' ? 'connected' :
         data.status === 'INCOMING' || data.status === 'ANSWERING'
             ? 'incoming' : 'idle');

    caller.textContent = data.caller || '--';
    duration.textContent = formatDuration(data.duration);

    audioStatus.textContent = data.audio;
    meter.style.width = data.audio_available ? '45%' : '0%';

    event.textContent = data.last_event || '';

    hangup.disabled =
        !(data.status === 'CONNECTED' ||
          data.status === 'ANSWERING' ||
          data.status === 'INCOMING');

    mute.disabled = !data.audio_available;
    mute.textContent = data.muted ? '🔊 UNMUTE' : '🔇 MUTE';
}

async function refresh() {
    try {
        const response = await fetch('/api/status', {
            cache: 'no-store'
        });

        const data = await response.json();
        updateDashboard(data);
    } catch (error) {
        document.getElementById('event').textContent =
            'Receiver server disconnected.';
    }
}

document.getElementById('hangupButton').onclick = async function() {
    await fetch('/api/hangup', {method: 'POST'});
    refresh();
};

document.getElementById('muteButton').onclick = async function() {
    muted = !muted;

    await fetch('/api/mute', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({muted: muted})
    });

    refresh();
};

setInterval(refresh, 500);
refresh();
</script>
</body>
</html>
"""


# ============================================================
# DASHBOARD HTTP SERVER
# ============================================================

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format_string, *args):
        # Keep normal HTTP access messages out of the GSM log.
        return

    def send_json(self, data, status=200):
        payload = json.dumps(data).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            payload = DASHBOARD_HTML.encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/status":
            self.send_json(get_state())
            return

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/hangup":
            ok = hangup_call()
            self.send_json({"success": ok})
            return

        if path == "/api/mute":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                muted = bool(data.get("muted", False))
            except Exception:
                muted = False

            set_state(muted=muted)
            self.send_json({"success": True, "muted": muted})
            return

        self.send_json({"error": "Not found"}, 404)


def dashboard_server():
    try:
        server = ThreadingHTTPServer(
            (DASHBOARD_HOST, DASHBOARD_PORT),
            DashboardHandler,
        )

        log(
            f"Dashboard running at "
            f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
        )

        while not stop_event.is_set():
            server.handle_request()

        server.server_close()

    except OSError as exc:
        log(f"Dashboard server error: {exc}")
        stop_event.set()


# ============================================================
# CLEANUP
# ============================================================

def cleanup():
    stop_event.set()

    global ser

    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass

        ser = None

    log("GSM receiver stopped.")


# ============================================================
# MAIN
# ============================================================

def main():
    log("")
    log("=" * 64)
    log("STARTING SIM800C GSM RECEIVER + DASHBOARD")
    log("=" * 64)

    if not find_modem():
        log("")
        log(f"ERROR: Could not detect SIM800C on {SERIAL_PORT}.")
        log("Check USB connection and make sure PuTTY/another serial")
        log("terminal is not using COM9.")
        return 1

    try:
        configure_modem()
    except Exception as exc:
        log(f"Modem configuration error: {exc}")
        cleanup()
        return 1

    serial_thread = threading.Thread(
        target=serial_monitor,
        name="SIM800C-Serial",
        daemon=True,
    )

    dashboard_thread = threading.Thread(
        target=dashboard_server,
        name="Dashboard",
        daemon=True,
    )

    serial_thread.start()
    dashboard_thread.start()

    try:
        while not stop_event.is_set():
            time.sleep(0.5)

    except KeyboardInterrupt:
        log("Keyboard interrupt received.")

    finally:
        cleanup()

    return 0


if __name__ == "__main__":
    sys.exit(main())
