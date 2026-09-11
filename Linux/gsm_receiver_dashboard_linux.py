import json
import re
import sys
import os
import signal
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

# Linux serial device. Leave as None for automatic detection.
# Examples: /dev/ttyUSB0, /dev/ttyUSB1, /dev/ttyACM0
# You can also set GSM_SERIAL_PORT in the environment:
#   GSM_SERIAL_PORT=/dev/ttyUSB0 python3 gsm_receiver_dashboard_linux.py
SERIAL_PORT = os.environ.get("GSM_SERIAL_PORT") or None

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
# Prevent dashboard/command threads from competing with the serial reader.
command_lock = threading.RLock()
state_lock = threading.Lock()
stop_event = threading.Event()

state = {
    "status": "IDLE",
    "caller": "",
    "ring_count": 0,
    "connected_at": None,
    "last_event": "Starting receiver...",
    "audio": "NOT AVAILABLE VIA SERIAL PORT",
    "audio_available": False,
    "muted": False,
    "last_sms_sender": "",
    "last_sms_message": "",
    "last_sms_time": "",
    "pending_call": False,
}

last_answer_time = 0.0
ANSWER_COOLDOWN_SEC = 0.0  # No ring suppression; failed calls must be retried.


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
    """Send a command while preventing another thread from reading its response."""
    with command_lock:
        if not send_raw(command):
            return False

        expected_list = [expected] if isinstance(expected, str) else expected
        deadline = time.time() + timeout
        buffer = ""

        while time.time() < deadline:
            try:
                with serial_lock:
                    waiting = ser.in_waiting if ser else 0
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

                    # Unsolicited call/SMS indications can arrive while a command is active.
                    # Do not treat them as command failures.
                    if upper.startswith("+CMTI:"):
                        handle_sms_notification(line)
                        continue

                    if upper.startswith("+CRING:") or upper == "RING":
                        set_state(status="INCOMING", last_event="Incoming voice call detected.")
                        continue

                    if upper.startswith("+CLIP:"):
                        number = extract_caller_number(line)
                        if number:
                            set_state(caller=number, last_event=f"Caller ID received: {number}")
                        continue

                    if any(item.upper() in upper for item in expected_list):
                        return True

                    if upper in ("ERROR", "BUSY"):
                        return False

                # Keep a partial line for the next read.
            except Exception as exc:
                log(f"COMMAND READ ERROR: {exc}")
                return False

        return False


# ============================================================
# MODEM DETECTION
# ============================================================

def list_serial_ports():
    ports = list(serial.tools.list_ports.comports())

    log("Available serial ports:")
    if not ports:
        log("  No serial ports detected.")
        return

    for port in ports:
        log(f"  {port.device} - {port.description}")


def test_modem(port, baud):
    """Try one serial port at one baud rate and verify it responds to AT."""
    global ser, current_baud, SERIAL_PORT

    log(f"Testing {port} at {baud} baud...")

    try:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            ser = None

        ser = serial.Serial(
            port=port,
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
                    # We have found the modem. Save the detected port.
                    SERIAL_PORT = port
                    log(f"Modem detected on {port} at {baud} baud.")
                    return True

            time.sleep(0.02)

        try:
            ser.close()
        except Exception:
            pass

        ser = None
        current_baud = None
        return False

    except Exception as exc:
        log(f"Could not open {port} at {baud}: {exc}")

        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

        ser = None
        current_baud = None
        return False


def find_modem():
    """Scan Linux serial devices and detect the SIM800C."""

    global SERIAL_PORT

    configured_port = SERIAL_PORT
    if configured_port:
        # Explicit Linux device requested through GSM_SERIAL_PORT.
        ports = [p for p in serial.tools.list_ports.comports()
                 if p.device == configured_port]
        if not ports:
            # pyserial can still open a valid device even if enumeration
            # metadata is unavailable.
            class PortInfo:
                def __init__(self, device):
                    self.device = device
                    self.description = "Configured Linux serial device"
            ports = [PortInfo(configured_port)]
    else:
        SERIAL_PORT = None
        all_ports = list(serial.tools.list_ports.comports())
        # Prefer USB/ACM devices on Linux. Keep other serial devices as
        # fallback because some USB-to-serial adapters enumerate differently.
        preferred = [p for p in all_ports
                     if p.device.startswith(("/dev/ttyUSB", "/dev/ttyACM"))]
        other = [p for p in all_ports if p not in preferred]
        ports = preferred + other

    log("")
    log("Scanning for SIM800C modem...")
    log("Available serial ports:")

    if not ports:
        log("  No serial ports detected.")
        return False

    # Show all detected ports first.
    for port_info in ports:
        log(
            f"  {port_info.device} - "
            f"{port_info.description or 'Unknown device'}"
        )

    log("")
    log("Testing available serial devices...")

    # Try every available COM port at every supported baud rate.
    # This makes the script independent of the COM number assigned
    # by Windows on a particular laptop/USB port.
    for port_info in ports:
        port = port_info.device

        for baud in BAUD_RATES:
            if test_modem(port, baud):
                log("")
                log("=" * 64)
                log("SIM800C MODEM DETECTED")
                log(f"Detected serial device : {SERIAL_PORT}")
                log(f"Detected baud     : {current_baud}")
                log("=" * 64)
                log("")
                return True

    log("")
    log("SIM800C was not detected on any available Linux serial device.")
    return False


# ============================================================
# MODEM CONFIGURATION
# ============================================================

def configure_modem():
    log("Configuring SIM800C...")

    # Core commands. SMS configuration is required for +CMTI notifications.
    commands = [
        ("ATE0", "OK", 2),
        ("AT+CLIP=1", "OK", 2),
        ("AT+CRC=1", "OK", 2),
        ("AT+CMGF=1", "OK", 2),
        ("AT+CNMI=2,1,0,0,0", "OK", 2),
        ("AT+CLVL=100", "OK", 2),
        ("AT+CMIC=0,12", "OK", 2),
        ("AT+CPIN?", "OK", 3),
        ("AT+CSQ", "OK", 3),
        ("AT+CREG?", "OK", 3),
        ("AT+COPS?", "OK", 5),
    ]

    for command, expected, timeout in commands:
        ok = send_command(command, expected, timeout)
        if not ok:
            log(f"WARNING: Command failed or timed out: {command}")

    # These two commands are not required for basic calling/SMS operation.
    # Your log shows ERROR for both on this firmware, so do not let them
    # appear as fatal configuration errors.
    log("Audio startup: AT+CHFA=1 and AT+FMMUTE=0 are intentionally not sent.")
    log("Reason: your modem returned ERROR for both; they are not required for SMS/call control.")

    log("SIM800C configuration complete.")
    set_state(
        audio="NOT AVAILABLE VIA SERIAL PORT",
        audio_available=False,
        last_event="Modem configured; waiting for incoming calls/SMS.",
    )


# ============================================================
# CALL CONTROL
# ============================================================

def extract_caller_number(line):
    match = re.search(r'\+CLIP:\s*"([^"]*)"', line, re.IGNORECASE)
    return match.group(1) if match else ""


def answer_call():
    """Answer the current incoming GSM voice call reliably.

    IMPORTANT:
    The serial-monitor thread is the owner of the incoming serial stream.
    It holds command_lock while dispatching an unsolicited event, so this
    function can safely take the same RLock and perform the ATA transaction
    without another thread stealing the response.
    """
    global last_answer_time

    with command_lock:
        with state_lock:
            current_status = state["status"]
            caller = state.get("caller", "")

        if current_status in ("ANSWERING", "CONNECTED"):
            log(">> ATA skipped: call is already being handled.")
            return True

        # Do NOT use a fixed cooldown to suppress later RING/+CRING events.
        # A failed ATA followed by another ring must be allowed to retry.
        set_state(status="ANSWERING", last_event="Answering incoming call...")
        log(">> AUTO ANSWER: Sending ATA...")
        if caller:
            log(f">> CALLER: {caller}")

        # Give the modem a very small amount of time after the URC before ATA.
        # This is intentionally short so the call is answered quickly.
        if ANSWER_DELAY_SEC > 0:
            time.sleep(ANSWER_DELAY_SEC)

        # First attempt.
        if not send_raw("ATA"):
            set_state(status="IDLE", connected_at=None,
                      last_event="Failed to send ATA.")
            return False

        deadline = time.time() + 5.0
        buffer = ""
        got_ok = False
        got_connect = False
        got_voice_begin = False

        while time.time() < deadline:
            try:
                with serial_lock:
                    waiting = ser.in_waiting if ser else 0
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

                    # Caller ID can arrive immediately before/after ATA.
                    if upper.startswith("+CLIP:"):
                        number = extract_caller_number(line)
                        if number:
                            set_state(caller=number,
                                      last_event=f"Caller ID received: {number}")
                        continue

                    # Other unsolicited notifications are not ATA failures.
                    if upper.startswith("+CMTI:"):
                        # Do not recursively read another SMS while answering.
                        log(">> SMS notification received during ATA; it will be handled after call processing.")
                        continue

                    if upper.startswith("+CRING:") or upper == "RING":
                        log(">> Additional ring indication received while answering.")
                        continue

                    if upper in ("OK", "CONNECT"):
                        got_ok = got_ok or upper == "OK"
                        got_connect = got_connect or upper == "CONNECT"
                    elif "VOICE CALL: BEGIN" in upper:
                        got_voice_begin = True
                    elif upper in ("NO CARRIER", "BUSY", "ERROR") or "+CME ERROR" in upper:
                        log(f">> ATA failed: {line}")
                        set_state(status="IDLE", connected_at=None,
                                  last_event=f"Call answer failed: {line}")
                        return False

                    if got_ok or got_connect or got_voice_begin:
                        last_answer_time = time.time()
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

        # ATA may occasionally succeed at the modem level but its OK/URC can
        # be delayed. Verify the call state before declaring failure.
        log(">> ATA response timeout; verifying call state with AT+CLCC...")

        try:
            if send_raw("AT+CLCC"):
                verify_deadline = time.time() + 2.0
                verify_buffer = ""
                clcc_connected = False

                while time.time() < verify_deadline:
                    with serial_lock:
                        waiting = ser.in_waiting if ser else 0
                        data = ser.read(waiting) if waiting else b""

                    if not data:
                        time.sleep(0.01)
                        continue

                    verify_buffer += data.decode("utf-8", errors="replace")
                    while "\n" in verify_buffer:
                        line, verify_buffer = verify_buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue

                        log(f"GSM> {line}")
                        upper = line.upper()

                        # +CLCC: <idx>,<dir>,<stat>,...
                        if upper.startswith("+CLCC:"):
                            parts = line.split(":", 1)[1].strip().split(",")
                            if len(parts) >= 3:
                                try:
                                    # stat=0 means active call.
                                    clcc_connected = int(parts[2].strip()) == 0
                                except ValueError:
                                    pass

                        if upper == "OK":
                            break

                if clcc_connected:
                    last_answer_time = time.time()
                    set_state(
                        status="CONNECTED",
                        connected_at=time.time(),
                        last_event="Call connected (verified by AT+CLCC).",
                    )
                    log(">> CALL ANSWERED SUCCESSFULLY (CLCC VERIFIED)")
                    return True

        except Exception as exc:
            log(f">> AT+CLCC verification error: {exc}")

        set_state(
            status="IDLE",
            connected_at=None,
            last_event="ATA timed out and no active call was detected.",
        )
        log(">> WARNING: ATA did not produce a valid answer response.")
        return False

def hangup_call():
    # The command lock is essential: otherwise the dashboard thread and
    # serial-monitor thread can both read from the same COM port. That is
    # exactly what caused your 'GSM> OK' followed by 'Hang-up response was not OK'.
    with command_lock:
        log(">> HANGING UP CALL...")

        if not send_raw("ATH"):
            set_state(status="IDLE", connected_at=None, caller="", last_event="Failed to send ATH.")
            return False

        deadline = time.time() + 4.0
        buffer = ""

        while time.time() < deadline:
            try:
                with serial_lock:
                    waiting = ser.in_waiting if ser else 0
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
                        set_state(
                            status="IDLE",
                            connected_at=None,
                            caller="",
                            last_event="Call ended.",
                        )
                        log(">> CALL HUNG UP.")
                        return True

                    if upper == "NO CARRIER":
                        set_state(
                            status="IDLE",
                            connected_at=None,
                            caller="",
                            last_event="Call ended by modem/network.",
                        )
                        log(">> CALL ENDED.")
                        return True

            except Exception as exc:
                log(f"ATH response error: {exc}")
                break

        set_state(
            status="IDLE",
            connected_at=None,
            caller="",
            last_event="ATH timed out; call state reset.",
        )
        log(">> WARNING: No ATH response received within timeout.")
        return False


# ============================================================
# SERIAL EVENT PROCESSING
# ============================================================

def extract_sms_fields_from_cmgr(header):
    sender = ""
    sms_time = ""
    fields = re.findall(r'"([^"]*)"', header)
    if len(fields) >= 2:
        sender = fields[1]
    if len(fields) >= 4:
        sms_time = fields[3]
    return sender, sms_time


def read_sms(index):
    """Read one stored SMS. Runs under command_lock so the serial reader cannot steal the response."""
    with command_lock:
        if not send_raw(f"AT+CMGR={index}"):
            log("ERROR: Could not send AT+CMGR.")
            return

        deadline = time.time() + 4.0
        buffer = ""
        header_seen = False
        sender = ""
        sms_time = ""
        body = []
        response_done = False
        saw_call = False

        while time.time() < deadline and not response_done:
            with serial_lock:
                waiting = ser.in_waiting if ser else 0
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

                if upper.startswith("+CMGR:"):
                    header_seen = True
                    sender, sms_time = extract_sms_fields_from_cmgr(line)
                    continue

                if upper == "OK":
                    response_done = True
                    break

                if upper == "ERROR":
                    log(f"ERROR: AT+CMGR={index} failed.")
                    return

                # If a URC arrives while reading SMS, handle it instead of
                # accidentally appending it to the SMS body.
                if upper.startswith("+CRING:") or upper == "RING":
                    saw_call = True
                    set_state(status="INCOMING", last_event="Incoming voice call detected while reading SMS.")
                    continue

                if upper.startswith("+CLIP:"):
                    number = extract_caller_number(line)
                    if number:
                        set_state(caller=number)
                    continue

                if upper.startswith("+CMTI:"):
                    handle_sms_notification(line)
                    continue

                if header_seen:
                    body.append(line)

            if header_seen and (body or buffer.strip() == ""):
                # Continue briefly for final OK, but don't block for seconds.
                pass

        message = "\n".join(body).strip()

        set_state(
            last_sms_sender=sender,
            last_sms_message=message,
            last_sms_time=sms_time,
            last_event=f"SMS received from {sender or 'Unknown'}",
        )

        log("")
        log("-" * 64)
        log("SMS CONTENT")
        log("-" * 64)
        log(f"From    : {sender or 'Unknown'}")
        log(f"Time    : {sms_time or 'Unknown'}")
        log(f"Message : {message or '[EMPTY]'}")
        log("-" * 64)
        log("")

        if saw_call:
            set_state(pending_call=True)


def handle_sms_notification(line):
    match = re.search(r'\+CMTI:\s*"([^"]+)"\s*,\s*(\d+)', line, re.IGNORECASE)
    if not match:
        log(f"WARNING: Could not parse SMS notification: {line}")
        return

    storage = match.group(1)
    index = int(match.group(2))

    log("")
    log("=" * 64)
    log("SMS RECEIVED")
    log("=" * 64)
    log(f"Storage : {storage}")
    log(f"Index   : {index}")

    # Read synchronously here, but with command_lock. The serial monitor is
    # the only normal reader, so this avoids the previous response collision.
    read_sms(index)


def process_line(line):
    line = line.strip()
    if not line:
        return

    upper = line.upper()
    log(f"GSM> {line}")

    if upper.startswith("+CMTI:"):
        handle_sms_notification(line)
        with state_lock:
            pending = state.get("pending_call", False)
            state["pending_call"] = False
        if pending and AUTO_ANSWER:
            answer_call()
        return

    if upper == "RING" or upper.startswith("RING") or upper.startswith("+CRING:"):
        with state_lock:
            current_status = state["status"]
            state["ring_count"] += 1
            ring_count = state["ring_count"]

        if current_status in ("ANSWERING", "CONNECTED"):
            return

        set_state(status="INCOMING", last_event="Incoming voice call detected.")
        log(f">> INCOMING VOICE CALL DETECTED (ring {ring_count})")

        if AUTO_ANSWER:
            if ANSWER_DELAY_SEC > 0:
                time.sleep(ANSWER_DELAY_SEC)
            answer_call()
        return

    if upper.startswith("+CLIP:"):
        number = extract_caller_number(line)
        if number:
            set_state(caller=number, last_event=f"Caller ID received: {number}")
            log(f">> CALLER NUMBER: {number}")
        return

    if upper == "CONNECT" or "VOICE CALL: BEGIN" in upper:
        set_state(status="CONNECTED", connected_at=time.time(), last_event="Voice call is active.")
        log(">> VOICE CALL IS ACTIVE")
        return

    if upper == "NO CARRIER":
        with state_lock:
            was_active = state["status"] in ("INCOMING", "ANSWERING", "CONNECTED")
        log(">> CALL ENDED" if was_active else ">> NO CARRIER")
        set_state(status="IDLE", connected_at=None, caller="", last_event="Call ended by network/remote side.")
        return

    if upper == "BUSY":
        set_state(status="IDLE", connected_at=None, caller="", last_event="Call status: BUSY.")
        log(">> CALL STATUS: BUSY")
        return


# ============================================================
# SERIAL MONITOR THREAD
# ============================================================

def serial_monitor():
    """Single owner of the SIM800C unsolicited serial stream.

    The critical reliability rule is that command_lock remains held while
    received lines are dispatched.  This prevents the following race:

        serial thread reads +CRING -> releases lock
        serial thread reads +CLIP -> consumes it
        answer_call() sends ATA -> waits for response

    In that race the ATA/CLIP responses can be split between readers.
    Keeping the lock through process_line() means answer_call() runs in the
    same serialized transaction and no other thread can steal bytes.
    """
    log("")
    log("=" * 64)
    log("SIM800C GSM RECEIVER + LINUX DASHBOARD")
    log("=" * 64)
    log(f"Serial device : {SERIAL_PORT} (auto-detected)")
    log(f"Baud rate   : {current_baud}")
    log(f"Auto answer : {AUTO_ANSWER}")
    log(f"Dashboard   : http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    log("")
    log("Waiting for incoming calls/SMS...")
    log("")

    partial = ""

    while not stop_event.is_set():
        try:
            # Hold command_lock not only while reading bytes, but also while
            # dispatching every complete line. Because command_lock is an
            # RLock, answer_call()/read_sms()/hangup_call() can safely be
            # called from this same thread.
            with command_lock:
                with serial_lock:
                    waiting = ser.in_waiting if ser else 0
                    data = ser.read(waiting) if waiting else b""

                if data:
                    partial += data.decode("utf-8", errors="replace")

                    while "\n" in partial:
                        line, partial = partial.split("\n", 1)
                        line = line.strip()
                        if line:
                            process_line(line)

                # If an incoming call was detected while an SMS command was
                # being processed, process it now while we still own the port.
                with state_lock:
                    pending = state.get("pending_call", False)
                    current_status = state.get("status")
                    if pending and current_status not in ("ANSWERING", "CONNECTED"):
                        state["pending_call"] = False
                    else:
                        pending = False

                if pending and AUTO_ANSWER:
                    log(">> Processing pending incoming call after SMS transaction...")
                    answer_call()

                if HANGUP_AFTER_SEC > 0:
                    current = get_state()
                    if current["status"] == "CONNECTED" and current["duration"] >= HANGUP_AFTER_SEC:
                        hangup_call()

            if not data:
                time.sleep(0.02)

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

        <div class="audio-box" style="margin-bottom:22px;">
            <div><span class="label">Last SMS From</span> <span id="smsSender" class="value">--</span></div>
            <div class="row"><span class="label">SMS Time</span> <span id="smsTime" class="value">--</span></div>
            <div><span class="label">Message</span><div id="smsMessage" style="margin-top:10px;padding:12px;background:#111827;border-radius:8px;white-space:pre-wrap;word-break:break-word;">No SMS received yet.</div></div>
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
            This RS2248/SIM800C USB serial connection provides the GSM
            control/serial interface. The GSM voice audio is not transferred
            as PCM audio through the serial port. Therefore this dashboard can display
            and control the call, but it cannot play the transmitter voice
            from the USB serial device alone.
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
    const smsSender = document.getElementById('smsSender');
    const smsTime = document.getElementById('smsTime');
    const smsMessage = document.getElementById('smsMessage');

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
    if (smsSender) smsSender.textContent = data.last_sms_sender || '--';
    if (smsTime) smsTime.textContent = data.last_sms_time || '--';
    if (smsMessage) smsMessage.textContent = data.last_sms_message || 'No SMS received yet.';
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
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browser refresh/close can terminate a keep-alive request while
            # the server is writing. This is harmless and must not print a
            # traceback into the console.
            return

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            payload = DASHBOARD_HTML.encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
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

def handle_signal(signum, frame):
    log(f"Received signal {signum}; shutting down...")
    stop_event.set()


def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log("")
    log("=" * 64)
    log("STARTING SIM800C GSM RECEIVER + LINUX DASHBOARD")
    log("=" * 64)

    if not find_modem():
        log("")
        log("ERROR: Could not detect SIM800C on any available Linux serial device.")
        log("Check the USB connection, SIM800C power, USB cable, and Linux serial-port permissions.")
        log("Also close minicom, screen, PuTTY, GTKTerm, or any other program using the modem device.")
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
