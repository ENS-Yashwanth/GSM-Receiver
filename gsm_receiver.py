import serial
import serial.tools.list_ports
import time
import sys
from datetime import datetime


# ============================================================
# CONFIGURATION
# ============================================================

SERIAL_PORT = "COM9"

BAUD_RATES = [
    115200,
    9600,
    57600,
    38400,
    19200,
]

SERIAL_TIMEOUT = 0.2

# Automatically answer incoming calls
AUTO_ANSWER = True

# Delay before sending ATA after incoming call detection
ANSWER_DELAY_SEC = 0.2

# Hang up automatically after this many seconds.
# 0 = do not automatically hang up.
HANGUP_AFTER_SEC = 0

# Log file
LOG_FILE = "gsm_receiver.log"


# ============================================================
# GLOBAL STATE
# ============================================================

ser = None

current_baud = None

ring_detected = False
call_answering = False
call_answered = False
call_active = False

ring_count = 0

caller_number = ""

call_start_time = None

last_ring_time = 0

# Prevent multiple ATA commands during the same incoming call
ANSWER_COOLDOWN_SEC = 3.0

last_answer_command_time = 0


# ============================================================
# LOGGING
# ============================================================

def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    line = f"[{timestamp()}] {message}"

    print(line)

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ============================================================
# SERIAL PORT DETECTION
# ============================================================

def list_com_ports():
    ports = list(serial.tools.list_ports.comports())

    if not ports:
        log("No COM ports detected.")
        return []

    log("Available COM ports:")

    for port in ports:
        log(
            f"  {port.device} - "
            f"{port.description}"
        )

    return ports


# ============================================================
# SERIAL WRITE
# ============================================================

def send_raw(command, show=True):
    global ser

    if ser is None:
        return False

    try:
        data = command + "\r\n"

        ser.write(data.encode("ascii", errors="ignore"))
        ser.flush()

        if show:
            log(f">> AT> {command}")

        return True

    except Exception as e:
        log(f">> SERIAL WRITE ERROR: {e}")
        return False


# ============================================================
# READ SERIAL
# ============================================================

def read_serial_for(seconds=1.0):
    """
    Read serial data for a specified amount of time.

    Returns a list of complete decoded lines.
    """

    global ser

    lines = []

    if ser is None:
        return lines

    end_time = time.time() + seconds

    buffer = ""

    while time.time() < end_time:

        try:
            waiting = ser.in_waiting

            if waiting:
                data = ser.read(waiting)

                if data:
                    buffer += data.decode(
                        "utf-8",
                        errors="replace"
                    )

                    while "\n" in buffer:
                        line, buffer = buffer.split(
                            "\n",
                            1
                        )

                        line = line.strip("\r").strip()

                        if line:
                            lines.append(line)

            else:
                time.sleep(0.01)

        except serial.SerialException as e:
            log(f"SERIAL READ ERROR: {e}")
            break

        except Exception as e:
            log(f"READ ERROR: {e}")
            break

    if buffer.strip():
        lines.append(buffer.strip())

    return lines


# ============================================================
# WAIT FOR AT RESPONSE
# ============================================================

def wait_for_response(
    expected=None,
    timeout=3.0,
    process_unsolicited=True
):
    """
    Wait for a modem response.

    expected:
        String or list of strings that indicate success.

    Returns:
        (success, response_lines)
    """

    if expected is None:
        expected = []

    if isinstance(expected, str):
        expected = [expected]

    responses = []

    end_time = time.time() + timeout

    buffer = ""

    while time.time() < end_time:

        try:
            waiting = ser.in_waiting

            if waiting:
                data = ser.read(waiting)

                if data:
                    buffer += data.decode(
                        "utf-8",
                        errors="replace"
                    )

                    while "\n" in buffer:

                        line, buffer = buffer.split(
                            "\n",
                            1
                        )

                        line = line.strip(
                            "\r"
                        ).strip()

                        if not line:
                            continue

                        responses.append(line)

                        log(f"GSM> {line}")

                        upper = line.upper()

                        # Don't let unsolicited call events
                        # get lost while waiting for ATA.
                        if process_unsolicited:
                            process_line(
                                line,
                                allow_answer=False
                            )

                        for item in expected:
                            if item.upper() in upper:
                                return True, responses

                        if upper in (
                            "ERROR",
                            "NO CARRIER",
                            "BUSY"
                        ):
                            return False, responses

            else:
                time.sleep(0.01)

        except Exception as e:
            log(f"RESPONSE READ ERROR: {e}")
            break

    return False, responses


# ============================================================
# SEND AT COMMAND AND WAIT
# ============================================================

def send_command(
    command,
    expected="OK",
    timeout=3.0
):

    if not send_raw(command):
        return False

    success, responses = wait_for_response(
        expected=expected,
        timeout=timeout
    )

    if success:
        log(
            f"<< {command}: command successful"
        )
    else:
        log(
            f"<< {command}: no expected response"
        )

    return success


# ============================================================
# TEST MODEM
# ============================================================

def test_modem(baud):
    global ser
    global current_baud

    log(
        f"Testing {SERIAL_PORT} at "
        f"{baud} baud..."
    )

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
            write_timeout=2
        )

        current_baud = baud

        time.sleep(0.5)

        # Clear input buffer
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass

        # Send AT manually
        ser.write(b"AT\r\n")
        ser.flush()

        end_time = time.time() + 2.0

        response = ""

        while time.time() < end_time:

            if ser.in_waiting:

                data = ser.read(
                    ser.in_waiting
                )

                response += data.decode(
                    "utf-8",
                    errors="replace"
                )

                if "OK" in response.upper():
                    log(
                        f"Modem detected at "
                        f"{baud} baud."
                    )

                    return True

            time.sleep(0.02)

        log(
            f"No valid AT response at "
            f"{baud} baud."
        )

        try:
            ser.close()
        except Exception:
            pass

        ser = None

        return False

    except Exception as e:

        log(
            f"Could not open {SERIAL_PORT} "
            f"at {baud}: {e}"
        )

        ser = None

        return False


# ============================================================
# FIND MODEM BAUD RATE
# ============================================================

def find_modem():

    list_com_ports()

    # If configured port exists, test it first
    ports = [
        p.device
        for p in serial.tools.list_ports.comports()
    ]

    if SERIAL_PORT not in ports:
        log(
            f"Warning: configured port "
            f"{SERIAL_PORT} was not found "
            f"in the current COM-port list."
        )

    for baud in BAUD_RATES:

        if test_modem(baud):
            return True

    return False


# ============================================================
# CONFIGURE GSM MODULE
# ============================================================

def configure_modem():

    log("Configuring GSM module...")

    # Disable command echo
    send_command(
        "ATE0",
        expected="OK",
        timeout=2
    )

    # Caller ID
    send_command(
        "AT+CLIP=1",
        expected="OK",
        timeout=2
    )

    # Extended result codes
    send_command(
        "AT+CRC=1",
        expected="OK",
        timeout=2
    )

    # Text SMS mode
    send_command(
        "AT+CMGF=1",
        expected="OK",
        timeout=2
    )

    # SMS notification mode
    send_command(
        "AT+CNMI=2,2,0,0,0",
        expected="OK",
        timeout=2
    )

    # SIM status
    send_command(
        "AT+CPIN?",
        expected="OK",
        timeout=3
    )

    # Signal quality
    send_command(
        "AT+CSQ",
        expected="OK",
        timeout=3
    )

    # Network registration
    send_command(
        "AT+CREG?",
        expected="OK",
        timeout=3
    )

    # Operator
    send_command(
        "AT+COPS?",
        expected="OK",
        timeout=5
    )

    log("GSM module configuration complete.")


# ============================================================
# ANSWER CALL
# ============================================================

def answer_call():

    global call_answering
    global call_answered
    global call_active
    global call_start_time
    global last_answer_command_time

    now = time.time()

    # Prevent duplicate ATA commands
    if (
        last_answer_command_time > 0
        and now - last_answer_command_time
        < ANSWER_COOLDOWN_SEC
    ):
        log(
            ">> ATA skipped: answer cooldown active."
        )
        return False

    if call_answered or call_active:
        log(
            ">> ATA skipped: call is already active."
        )
        return False

    last_answer_command_time = now

    call_answering = True

    log(">> AUTO ANSWER: Sending ATA...")

    if not send_raw("ATA"):
        call_answering = False
        return False

    # Wait for the immediate response.
    #
    # SIM800-family modules can respond with:
    #
    #   OK
    #
    # followed by:
    #
    #   VOICE CALL: BEGIN
    #
    # Some firmware/configurations may produce
    # CONNECT instead.
    #
    success = False
    got_ok = False
    got_connect = False
    got_voice_begin = False

    end_time = time.time() + 4.0

    buffer = ""

    while time.time() < end_time:

        try:

            if ser.in_waiting:

                data = ser.read(
                    ser.in_waiting
                )

                if data:
                    buffer += data.decode(
                        "utf-8",
                        errors="replace"
                    )

                    while "\n" in buffer:

                        line, buffer = buffer.split(
                            "\n",
                            1
                        )

                        line = line.strip(
                            "\r"
                        ).strip()

                        if not line:
                            continue

                        log(f"GSM> {line}")

                        upper = line.upper()

                        if upper == "OK":
                            got_ok = True

                        elif upper == "CONNECT":
                            got_connect = True

                        elif (
                            "VOICE CALL: BEGIN"
                            in upper
                        ):
                            got_voice_begin = True

                        elif upper in (
                            "NO CARRIER",
                            "BUSY",
                            "ERROR"
                        ):
                            log(
                                ">> CALL ANSWER FAILED: "
                                f"{line}"
                            )

                            call_answering = False
                            call_answered = False
                            call_active = False

                            return False

                        # Caller ID can still arrive
                        # around this time.
                        elif upper.startswith("+CLIP:"):
                            process_line(
                                line,
                                allow_answer=False
                            )

                        if (
                            got_ok
                            or got_connect
                            or got_voice_begin
                        ):
                            success = True

            else:
                time.sleep(0.01)

        except Exception as e:

            log(
                f">> ATA response error: {e}"
            )

            break

    # Some modules may send OK and then
    # VOICE CALL: BEGIN after a small delay.
    if success:

        call_answered = True
        call_active = True
        call_answering = False

        call_start_time = time.time()

        log(
            ">> CALL ANSWERED SUCCESSFULLY"
        )

        if got_voice_begin:
            log(
                ">> VOICE CALL: BEGIN received."
            )
        elif got_connect:
            log(
                ">> CONNECT received."
            )
        elif got_ok:
            log(
                ">> OK received from ATA."
            )

        return True

    # It is possible that ATA was accepted but
    # the module has not yet sent the final
    # unsolicited call-state message.
    #
    # Give it a short grace period.
    grace_end = time.time() + 1.5

    while time.time() < grace_end:

        try:

            if ser.in_waiting:

                data = ser.read(
                    ser.in_waiting
                )

                if data:

                    text = data.decode(
                        "utf-8",
                        errors="replace"
                    )

                    for raw_line in text.splitlines():

                        line = raw_line.strip()

                        if not line:
                            continue

                        log(f"GSM> {line}")

                        upper = line.upper()

                        if (
                            "VOICE CALL: BEGIN"
                            in upper
                            or upper == "CONNECT"
                        ):

                            call_answered = True
                            call_active = True
                            call_answering = False

                            call_start_time = time.time()

                            log(
                                ">> CALL ANSWERED SUCCESSFULLY"
                            )

                            return True

                        if upper in (
                            "NO CARRIER",
                            "BUSY",
                            "ERROR"
                        ):

                            call_answering = False
                            call_answered = False
                            call_active = False

                            log(
                                ">> Call was not answered: "
                                f"{line}"
                            )

                            return False

            else:
                time.sleep(0.01)

        except Exception:
            break

    # If ATA was transmitted but the modem did not
    # give a definitive response, don't immediately
    # send another ATA. Mark it as answered provisionally.
    #
    # The main loop will still monitor NO CARRIER,
    # BUSY, etc.
    if got_ok:

        call_answered = True
        call_active = True
        call_answering = False

        call_start_time = time.time()

        log(
            ">> ATA returned OK."
        )

        log(
            ">> CALL ANSWERED "
            "(waiting for call-state notification)"
        )

        return True

    call_answering = False

    log(
        ">> ATA sent, but no valid answer response "
        "was received."
    )

    return False


# ============================================================
# HANG UP CALL
# ============================================================

def hangup_call():

    global call_answered
    global call_active
    global call_answering

    log(">> HANGING UP CALL...")

    success = send_command(
        "ATH",
        expected="OK",
        timeout=3
    )

    if success:
        log(">> CALL HUNG UP.")
    else:
        log(
            ">> Hang-up command did not return OK."
        )

    call_answered = False
    call_active = False
    call_answering = False

    reset_call_state()

    return success


# ============================================================
# RESET CALL STATE
# ============================================================

def reset_call_state():

    global ring_detected
    global call_answering
    global call_answered
    global call_active
    global ring_count
    global caller_number
    global call_start_time

    ring_detected = False
    call_answering = False
    call_answered = False
    call_active = False

    ring_count = 0

    caller_number = ""

    call_start_time = None


# ============================================================
# PROCESS SERIAL LINE
# ============================================================

def process_line(
    line,
    allow_answer=True
):

    global ring_detected
    global call_answering
    global call_answered
    global call_active
    global ring_count
    global caller_number
    global call_start_time
    global last_ring_time

    line = line.strip()

    if not line:
        return

    upper = line.upper()

    # --------------------------------------------------------
    # INCOMING CALL DETECTION
    # --------------------------------------------------------
    #
    # SIM800C can report incoming voice calls as:
    #
    #     RING
    #
    # or:
    #
    #     +CRING: VOICE
    #
    # Your module is reporting:
    #
    #     +CRING: VOICE
    #
    # so this is the important fix.
    # --------------------------------------------------------

    incoming_call = (
        upper == "RING"
        or upper.startswith("RING")
        or upper.startswith("+CRING:")
    )

    if incoming_call:

        now = time.time()

        # Avoid processing the same ringing state
        # repeatedly.
        if (
            ring_detected
            and (
                now - last_ring_time
            ) < 3.5
        ):
            log(
                f"GSM> {line}"
            )
            return

        last_ring_time = now

        # If already answering/active, simply log
        # the repeated ring notification.
        if (
            call_answering
            or call_answered
            or call_active
        ):
            log(
                f"GSM> {line}"
            )
            return

        ring_detected = True
        ring_count += 1

        log(
            f"GSM> {line}"
        )

        log(
            ">> INCOMING VOICE CALL DETECTED"
        )

        log(
            f">> RING COUNT: {ring_count}"
        )

        if AUTO_ANSWER and allow_answer:

            log(
                f">> Waiting "
                f"{ANSWER_DELAY_SEC:.2f} seconds "
                f"before answering..."
            )

            if ANSWER_DELAY_SEC > 0:
                time.sleep(
                    ANSWER_DELAY_SEC
                )

            # Make sure call didn't disappear
            # during the delay.
            if not call_answered and not call_active:

                answer_call()

        return

    # --------------------------------------------------------
    # CALLER ID
    # --------------------------------------------------------

    if upper.startswith("+CLIP:"):

        log(
            f"GSM> {line}"
        )

        log(
            f">> CALLER ID: {line}"
        )

        # Extract phone number
        #
        # Typical:
        # +CLIP: "+91xxxxxxxxxx",145,...
        try:

            start = line.find('"')

            if start >= 0:

                end = line.find(
                    '"',
                    start + 1
                )

                if end > start:

                    caller_number = (
                        line[
                            start + 1:end
                        ]
                    )

                    log(
                        f">> CALLER NUMBER: "
                        f"{caller_number}"
                    )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # CALL CONNECTED
    # --------------------------------------------------------

    if (
        upper == "CONNECT"
        or "VOICE CALL: BEGIN" in upper
        or upper == "VOICE CALL: BEGIN"
    ):

        log(
            f"GSM> {line}"
        )

        call_answering = False
        call_answered = True
        call_active = True

        if call_start_time is None:
            call_start_time = time.time()

        log(
            ">> VOICE CALL IS ACTIVE"
        )

        return

    # --------------------------------------------------------
    # NO CARRIER
    # --------------------------------------------------------

    if upper == "NO CARRIER":

        log(
            f"GSM> {line}"
        )

        if call_active or call_answered:
            log(
                ">> CALL ENDED"
            )

        elif ring_detected:
            log(
                ">> INCOMING CALL ENDED "
                "BEFORE ANSWER"
            )

        reset_call_state()

        return

    # --------------------------------------------------------
    # BUSY
    # --------------------------------------------------------

    if upper == "BUSY":

        log(
            f"GSM> {line}"
        )

        log(
            ">> CALL STATUS: BUSY"
        )

        reset_call_state()

        return

    # --------------------------------------------------------
    # ERROR
    # --------------------------------------------------------

    if upper == "ERROR":

        log(
            f"GSM> {line}"
        )

        # Don't reset everything if it is just
        # an unrelated AT command response.
        if (
            call_answering
            or ring_detected
        ):
            log(
                ">> Call operation returned ERROR."
            )

            call_answering = False

        return

    # --------------------------------------------------------
    # SMS
    # --------------------------------------------------------

    if upper.startswith("+CMT:"):

        log(
            f"GSM> {line}"
        )

        log(
            ">> INCOMING SMS NOTIFICATION"
        )

        return

    # --------------------------------------------------------
    # NORMAL GSM OUTPUT
    # --------------------------------------------------------

    log(
        f"GSM> {line}"
    )


# ============================================================
# SERIAL MONITOR
# ============================================================

def monitor_serial():

    global ser
    global call_active
    global call_answered
    global call_start_time

    log("")
    log("=" * 60)
    log("GSM RECEIVER STARTED")
    log("=" * 60)

    log(
        f"Serial port : {SERIAL_PORT}"
    )

    log(
        f"Baud rate   : {current_baud}"
    )

    log(
        f"Auto answer : {AUTO_ANSWER}"
    )

    log(
        f"Answer delay: "
        f"{ANSWER_DELAY_SEC} sec"
    )

    if HANGUP_AFTER_SEC > 0:
        log(
            f"Auto hangup: "
            f"{HANGUP_AFTER_SEC} sec"
        )
    else:
        log(
            "Auto hangup: Disabled"
        )

    log("")
    log(
        "Waiting for incoming calls..."
    )

    log(
        "The receiver accepts both "
        "'RING' and '+CRING: VOICE'."
    )

    log("")

    while True:

        try:

            # ------------------------------------------------
            # READ AVAILABLE DATA
            # ------------------------------------------------

            if ser.in_waiting:

                data = ser.read(
                    ser.in_waiting
                )

                if data:

                    text = data.decode(
                        "utf-8",
                        errors="replace"
                    )

                    # GSM responses are normally
                    # line based.
                    lines = text.splitlines()

                    for line in lines:

                        line = line.strip()

                        if line:
                            process_line(line)

            else:

                time.sleep(0.02)

            # ------------------------------------------------
            # AUTO HANGUP
            # ------------------------------------------------

            if (
                HANGUP_AFTER_SEC > 0
                and call_active
                and call_start_time is not None
            ):

                elapsed = (
                    time.time()
                    - call_start_time
                )

                if elapsed >= HANGUP_AFTER_SEC:

                    log(
                        f">> Auto hangup timer reached "
                        f"({elapsed:.1f} sec)."
                    )

                    hangup_call()

        except KeyboardInterrupt:

            log("")
            log(
                "Keyboard interrupt received."
            )

            break

        except serial.SerialException as e:

            log(
                f"SERIAL ERROR: {e}"
            )

            break

        except Exception as e:

            log(
                f"MAIN LOOP ERROR: {e}"
            )

            time.sleep(0.2)


# ============================================================
# CLEANUP
# ============================================================

def cleanup():

    global ser

    log(
        "Closing GSM serial connection..."
    )

    if ser is not None:

        try:
            ser.close()
        except Exception:
            pass

        ser = None

    log(
        "GSM receiver stopped."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global ser

    log("")
    log("=" * 60)
    log("STARTING GSM RECEIVER")
    log("=" * 60)

    # --------------------------------------------------------
    # FIND MODEM
    # --------------------------------------------------------

    if not find_modem():

        log("")
        log(
            "ERROR: GSM modem could not be detected."
        )

        log(
            f"Please check {SERIAL_PORT}, "
            "USB connection and power."
        )

        return 1

    # --------------------------------------------------------
    # CONFIGURE MODEM
    # --------------------------------------------------------

    try:

        configure_modem()

    except Exception as e:

        log(
            f"Modem configuration error: {e}"
        )

        cleanup()

        return 1

    # --------------------------------------------------------
    # START MONITORING
    # --------------------------------------------------------

    try:

        monitor_serial()

    except KeyboardInterrupt:

        log(
            "Stopping receiver..."
        )

    finally:

        cleanup()

    return 0


# ============================================================
# PROGRAM ENTRY
# ============================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )