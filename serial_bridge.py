"""
ACPACS — Serial Bridge v3.0 (Python <-> Arduino Gate Controller)
Report S3.3 / S3.5 — SessionController and HardwareInterface

v3.0 changes:
  1. Forwards the hex TEMPLATE from each scan, per S3.3.2.2, instead of an
     enrolled id. The backend stores it in the session row and does the
     1:1 comparison.
  2. Handles 409 CAR PARK FULL from the capacity check (S3.4.2) and sends
     the FULL command so the LCD shows it.
  3. Latency instrumentation. Times each gate cycle from vehicle detection
     to the OPEN command and appends to latency_log.csv, so S4.2's 2.5s
     target can be reported as a measurement instead of a promise.

Usage:
  Real hardware:
      python serial_bridge.py --port /dev/tty.usbmodem1234 --camera 0

  Wokwi simulation (no camera, so supply test images):
      python serial_bridge.py --wokwi \
          --test-entry entry_image.jpg --test-exit exit_image.jpg

  In Wokwi, a SHORT press of the blue button is driver #1.
  HOLDING it for 1.5s+ produces a DIFFERENT template — use that to
  demonstrate the exit gate refusing the wrong driver.
"""

import serial
import time
import csv
import argparse
import requests
import cv2
import glob
import os
from datetime import datetime
from alpr_connector import read_plate_from_image

API_BASE_URL = "http://localhost:8000"

# Must match rfc2217ServerPort in wokwi.toml
WOKWI_PORT = "rfc2217://localhost:4000"

# The firmware gives the driver 15s to present a finger
# (FP_ATTEMPTS = 75, x 200ms per attempt). Python must wait LONGER than
# that, so the Arduino always gets to report its own FP_FAIL. If Python
# times out first it fires DENY into a buffer the Arduino isn't reading
# yet, and the command gets executed several seconds late.
FP_TIMEOUT = 20

# Report S4.2 sets a 2.5s target from vehicle detection to gate actuation.
# Every cycle is appended here so Chapter 4 can report a distribution.
LATENCY_LOG = "latency_log.csv"


# -------------------------------------------------------------------
# Serial Helpers
# -------------------------------------------------------------------
def send_command(ser, cmd):
    """Send a command string to Arduino."""
    print(f"  -> Arduino: {cmd}")
    ser.write((cmd + '\n').encode())
    time.sleep(0.1)


def wait_for_response(ser, expected_prefix, timeout=15):
    """
    Wait for a response from Arduino containing expected_prefix.
    Returns the full response line or None on timeout.
    """
    start = time.time()
    while time.time() - start < timeout:
        if ser.in_waiting:
            line = ser.readline().decode(errors='ignore').strip()
            if line:
                print(f"  <- Arduino: {line}")
                if expected_prefix in line:
                    return line
        time.sleep(0.05)
    print(f"  ! Timeout waiting for: {expected_prefix}")
    return None


def flush_input(ser):
    """
    Discard anything already sitting in the receive buffer.

    Called immediately before prompting for a fingerprint. Without it, a
    stale FP_OK or FP_FAIL left over from an earlier cycle is consumed by
    the next wait_for_response(), which returns instantly with a template
    that belongs to a DIFFERENT scan. That template would then be bound to
    this session, so the flush is a correctness requirement rather than
    tidiness.
    """
    try:
        ser.reset_input_buffer()
    except Exception as e:
        print(f"  ! Could not flush serial input: {e}")


def park_is_full(ser):
    """
    Check occupancy BEFORE any driver interaction (report S3.4.2).

    The authoritative check remains inside POST /entry, which is what
    guarantees correctness under concurrent lanes. This is a pre-check so
    that a driver is not asked to present a finger for a session that
    cannot be created. Returns True if entry should be refused.
    """
    try:
        resp = requests.get(f"{API_BASE_URL}/occupancy", timeout=5)
        if resp.status_code != 200:
            return False          # inconclusive: let POST /entry decide
        data = resp.json()
        if data.get("full"):
            print(f"  [x] CAR PARK FULL - {data['occupancy']}/{data['capacity']} bays occupied")
            return True
        print(f"  [i] Occupancy {data['occupancy']}/{data['capacity']}")
        return False
    except (requests.RequestException, ValueError):
        return False              # inconclusive: let POST /entry decide


def already_parked(plate):
    """
    Check for an existing open session before prompting for a fingerprint.

    GET /session/{plate} returns 200 when a session is open and 404 when
    none is. As with the occupancy pre-check, POST /entry remains
    authoritative; this only avoids asking a driver to scan for an entry
    that will be refused anyway.
    """
    try:
        resp = requests.get(f"{API_BASE_URL}/session/{plate}", timeout=5)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def parse_template(response):
    """
    Parse a scan result from the firmware.

        FP_OK:<hex template>:<confidence>

    Returns (hex_template, confidence) or (None, None) if the line is
    FP_FAIL or malformed.

    The hex string IS the minutiae template. It goes to the backend as-is
    and is stored in the session row as a BLOB (report Table 3.3). Nothing
    is written to the sensor's onboard library, which is why the exit check
    is a single-row lookup rather than a search (S3.3.2.2).
    """
    if not response or not response.startswith("FP_OK:"):
        return None, None

    parts = response[len("FP_OK:"):].strip().split(":")
    if len(parts) < 2:
        return None, None

    template_hex = parts[0].strip()
    if not template_hex or len(template_hex) % 2 != 0:
        return None, None

    try:
        return template_hex, int(parts[1])
    except ValueError:
        return None, None


def log_latency(event, plate, timings):
    """
    Append one gate cycle to the latency log (S4.2), decomposed.

    Recording only the total would conflate the system's processing time
    with how long the driver took to present a finger, which is not a
    property of the system at all. Every segment is logged so Chapter 4
    can report both the end-to-end cycle and the processing latency the
    2.5s target actually applies to.
    """
    fields = ["timestamp", "event", "plate", "alpr_s", "backend_s",
              "driver_wait_s", "system_s", "total_s"]
    try:
        new_file = not os.path.exists(LATENCY_LOG)
        with open(LATENCY_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(fields)
            writer.writerow([
                datetime.now().isoformat(timespec="seconds"), event, plate,
                f"{timings.get('alpr', 0):.3f}",
                f"{timings.get('backend', 0):.3f}",
                f"{timings.get('driver_wait', 0):.3f}",
                f"{timings.get('system', 0):.3f}",
                f"{timings.get('total', 0):.3f}",
            ])
    except OSError as e:
        print(f"  ! Could not write {LATENCY_LOG}: {e}")


def error_detail(resp):
    """
    Pull an error message out of a response that might not be JSON.

    FastAPI returns JSON for a raised HTTPException, but an UNHANDLED
    exception produces Starlette's plain-text 'Internal Server Error'.
    Calling .json() on that raises JSONDecodeError and takes the bridge
    down mid-cycle, which is the last thing you want during a demo.
    """
    try:
        return resp.json().get("detail", "Unknown error")
    except ValueError:
        body = resp.text.strip().replace("\n", " ")[:120]
        return (f"HTTP {resp.status_code} (not JSON) - {body or 'empty body'}"
                f"  <- check the uvicorn terminal for the traceback")


# -------------------------------------------------------------------
# Camera Capture
# -------------------------------------------------------------------
def capture_image(camera_index=0, save_path="gate_capture.jpg"):
    """
    Capture a single frame from the camera.
    Falls back to a test image if camera not available.
    """
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print("  ! Camera not available - using test image")
        return None

    ret, frame = cap.read()
    cap.release()

    if ret:
        cv2.imwrite(save_path, frame)
        print(f"  [cam] Image captured: {save_path}")
        return save_path
    return None


# -------------------------------------------------------------------
# Entry Flow
# -------------------------------------------------------------------
def handle_entry(ser, image_path, t_detected=None):
    """
    Entry gate sequence (report S3.3.3.1):
    1. ALPR reads the plate
    2. Plate shown on the LCD
    3. Fingerprint captured, template returned
    4. Backend checks duplicates and capacity, then binds template to plate
    5. Gate opens or access is denied

    Timing is segmented so that the driver's fingerprint presentation is
    excluded from the processing figure quoted against S4.2's 2.5s target.
    """
    print("\n" + "="*45)
    print("  ENTRY GATE - Processing Vehicle")
    print("="*45)

    t = {}

    # Capacity is tested first, before any inference is run and before the
    # driver is asked for anything. A vehicle that cannot be admitted does
    # not cause a recognition pass or a fingerprint prompt.
    if park_is_full(ser):
        send_command(ser, "FULL")
        return

    t0 = time.time()
    plate, confidence = read_plate_from_image(image_path)
    t["alpr"] = time.time() - t0

    if not plate:
        print("  [x] Could not read plate - denying access")
        send_command(ser, "DENY")
        return

    print(f"  [ok] Plate: {plate} ({confidence:.1%} confidence)")
    print(f"  [t] ALPR inference: {t['alpr']:.2f}s")

    # Duplicate entry, tested before the driver is prompted.
    if already_parked(plate):
        print(f"  [x] {plate} already has an open session - denying access")
        send_command(ser, "DENY")
        return

    send_command(ser, f"PLATE:{plate}")
    time.sleep(0.5)

    flush_input(ser)              # discard any stale FP_ line from a prior cycle
    send_command(ser, "ENTRY")
    print("  ... waiting for fingerprint (press the BLUE button in Wokwi)")

    t0 = time.time()
    response = wait_for_response(ser, "FP_", timeout=FP_TIMEOUT)
    t["driver_wait"] = time.time() - t0

    if not response or "FP_FAIL" in response:
        print("  [x] Fingerprint capture failed - denying access")
        send_command(ser, "DENY")
        return

    template_hex, fp_conf = parse_template(response)
    if template_hex is None:
        print(f"  [x] Could not parse template: {response}")
        send_command(ser, "DENY")
        return

    print(f"  [bio] Template captured: {len(template_hex)//2} bytes, "
          f"confidence {fp_conf}")

    try:
        t0 = time.time()
        resp = requests.post(f"{API_BASE_URL}/entry", json={
            "plate": plate,
            "fingerprint_template": template_hex,
            "fingerprint_confidence": fp_conf
        }, timeout=5)
        t["backend"] = time.time() - t0

        if resp.status_code == 200:
            data = resp.json()
            print(f"  [ok] Session started - ID: {data['session_id']}")
            print(f"  Entry time: {data['entry_time']}")
            print(f"  Occupancy: {data['occupancy']}/{data['capacity']}")
            send_command(ser, "OPEN")

            if t_detected is not None:
                t["total"] = time.time() - t_detected
                # Everything except the driver presenting a finger.
                t["system"] = t["total"] - t["driver_wait"]
                verdict = "PASS" if t["system"] <= 2.5 else "OVER"
                print(f"  [t] Backend: {t['backend']:.2f}s | "
                      f"driver wait: {t['driver_wait']:.2f}s")
                print(f"  [t] System latency {t['system']:.2f}s ({verdict}, "
                      f"target 2.5s) | end-to-end {t['total']:.2f}s")
                log_latency("entry", plate, t)

        elif resp.status_code == 409:
            # Capacity check, report S3.4.2
            print(f"  [x] {error_detail(resp)}")
            send_command(ser, "FULL")

        else:
            print(f"  [x] Backend error: {error_detail(resp)}")
            send_command(ser, "DENY")

    except requests.ConnectionError:
        print("  [x] Backend not reachable - is the server running?")
        send_command(ser, "DENY")
    except requests.Timeout:
        print("  [x] Backend timeout")
        send_command(ser, "DENY")


# -------------------------------------------------------------------
# Exit Flow
# -------------------------------------------------------------------
def handle_exit(ser, image_path, t_detected=None):
    """
    Exit gate sequence (report S3.3.3.2):
    1. ALPR reads the plate
    2. Fingerprint captured
    3. Backend loads the ONE active session for that plate and runs a 1:1
       comparison against the template bound at entry
    4. If it clears the threshold, the bill is raised and shown, gate opens

    There is no local session lookup here. The backend owns session state,
    so restarting this script does not strand parked vehicles.

    NOTE for S4.2: the exit cycle also contains a deliberate 4s dwell while
    the fare is displayed on the LCD for the driver to read. That is a
    usability decision, not processing time, and is excluded from the
    system figure alongside the driver wait.
    """
    print("\n" + "="*45)
    print("  EXIT GATE - Processing Vehicle")
    print("="*45)

    t = {}
    bill_dwell = 0.0

    t0 = time.time()
    plate, confidence = read_plate_from_image(image_path)
    t["alpr"] = time.time() - t0

    if not plate:
        print("  [x] Could not read plate - denying access")
        send_command(ser, "DENY")
        return

    print(f"  [ok] Plate: {plate} ({confidence:.1%} confidence)")
    print(f"  [t] ALPR inference: {t['alpr']:.2f}s")

    send_command(ser, f"PLATE:{plate}")
    time.sleep(0.5)
    flush_input(ser)              # discard any stale FP_ line from a prior cycle
    send_command(ser, "EXIT")
    print("  ... waiting for fingerprint (press the BLUE button in Wokwi)")
    print("      short press = same driver, hold 1.5s = a different person")

    t0 = time.time()
    response = wait_for_response(ser, "FP_", timeout=FP_TIMEOUT)
    t["driver_wait"] = time.time() - t0

    if not response or "FP_FAIL" in response:
        print("  [x] Fingerprint capture failed - denying exit")
        send_command(ser, "DENY")
        return

    template_hex, fp_conf = parse_template(response)
    if template_hex is None:
        print(f"  [x] Could not parse template: {response}")
        send_command(ser, "DENY")
        return

    print(f"  [bio] Template captured: {len(template_hex)//2} bytes, "
          f"confidence {fp_conf}")

    try:
        t0 = time.time()
        resp = requests.post(f"{API_BASE_URL}/exit", json={
            "plate": plate,
            "fingerprint_template": template_hex,
            "fingerprint_confidence": fp_conf
        }, timeout=5)
        t["backend"] = time.time() - t0

        if resp.status_code == 200:
            data = resp.json()
            print(f"  [ok] 1:1 verification passed - match score "
                  f"{data['match_score']:.2f}")
            print(f"  Duration: {data['duration']}")
            print(f"  Billed: {data['billable_hours']} hour(s) = "
                  f"NGN {data['amount_naira']:,.0f}")

            send_command(ser, f"BILL:{int(data['amount_naira'])}")

            t0 = time.time()
            time.sleep(4.0)   # LCD fare dwell; the Arduino blocks ~3.4s here
            bill_dwell = time.time() - t0

            send_command(ser, "OPEN")

            if t_detected is not None:
                t["total"] = time.time() - t_detected
                t["system"] = t["total"] - t["driver_wait"] - bill_dwell
                verdict = "PASS" if t["system"] <= 2.5 else "OVER"
                print(f"  [t] Backend: {t['backend']:.2f}s | "
                      f"driver wait: {t['driver_wait']:.2f}s | "
                      f"fare dwell: {bill_dwell:.2f}s")
                print(f"  [t] System latency {t['system']:.2f}s ({verdict}, "
                      f"target 2.5s) | end-to-end {t['total']:.2f}s")
                log_latency("exit", plate, t)

        elif resp.status_code == 403:
            print(f"  [x] {error_detail(resp)}")
            print("      Gate held closed - this is the 1:1 check working.")
            send_command(ser, "DENY")

        else:
            print(f"  [x] Backend error: {error_detail(resp)}")
            send_command(ser, "DENY")

    except requests.ConnectionError:
        print("  [x] Backend not reachable")
        send_command(ser, "DENY")
    except requests.Timeout:
        print("  [x] Backend timeout")
        send_command(ser, "DENY")


# -------------------------------------------------------------------
# Auto-detect Arduino port
# -------------------------------------------------------------------
def find_arduino_port():
    candidates = (
        glob.glob('/dev/tty.usbmodem*') +
        glob.glob('/dev/ttyUSB*') +
        glob.glob('/dev/ttyACM*')
    )
    return candidates[0] if candidates else None


# -------------------------------------------------------------------
# Main Loop
# -------------------------------------------------------------------
def run(port, camera_index=0, test_entry=None, test_exit=None):
    is_wokwi = port.startswith("rfc2217://") or port.startswith("socket://")

    print(f"\nConnecting to Arduino on {port}...")
    try:
        # serial_for_url() handles BOTH a normal port ("COM3",
        # "/dev/ttyUSB0") and a Wokwi RFC2217 URL. Drop-in replacement
        # for serial.Serial().
        ser = serial.serial_for_url(port, baudrate=9600, timeout=1)
        if not is_wokwi:
            time.sleep(2)  # real boards reset on port open; Wokwi does not
    except serial.SerialException as e:
        print(f"[x] Could not connect to {port}: {e}")
        if is_wokwi:
            print("    - Is the Wokwi simulation running (green play button)?")
            print("    - Is the simulator tab VISIBLE in VS Code? Hidden = paused.")
            print("    - Does wokwi.toml have rfc2217ServerPort = 4000 ?")
        return

    # Wait for READY
    print("Waiting for Arduino...")
    ready = wait_for_response(ser, "READY", timeout=10)
    if not ready:
        if is_wokwi:
            print("(No READY seen. This is NORMAL on Wokwi - the sim boots")
            print(" the moment you press play, usually before this script")
            print(" connects, so the line is already gone. Continuing.)\n")
        else:
            print("! Arduino did not send READY - continuing anyway\n")
    else:
        print("[ok] Arduino ready\n")

    print("="*45)
    print("  ACPACS Gate System Running")
    print("  Listening for vehicle detection...")
    if is_wokwi:
        print("  Press the GREEN button in Wokwi to start an entry.")
        print("  Press the RED button to start an exit.")
    print("  Press Ctrl+C to stop")
    print("="*45 + "\n")

    try:
        while True:
            if ser.in_waiting:
                line = ser.readline().decode(errors='ignore').strip()
                if not line:
                    continue

                print(f"<- Arduino: {line}")

                # Car detected at entry gate
                if line == "CAR_AT_ENTRY":
                    t_detected = time.time()
                    if test_entry:
                        image_path = test_entry
                    else:
                        image_path = capture_image(camera_index, "entry_capture.jpg")

                    if image_path:
                        handle_entry(ser, image_path, t_detected)
                    else:
                        print("  [x] No image available")
                        send_command(ser, "DENY")

                # Car detected at exit gate
                elif line == "CAR_AT_EXIT":
                    t_detected = time.time()
                    if test_exit:
                        image_path = test_exit
                    else:
                        image_path = capture_image(camera_index, "exit_capture.jpg")

                    if image_path:
                        handle_exit(ser, image_path, t_detected)
                    else:
                        print("  [x] No image available")
                        send_command(ser, "DENY")

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n\nShutting down ACPACS Gate System...")
        ser.close()


# -------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ACPACS Serial Bridge")
    parser.add_argument("--port",       default=None, help="Serial port (auto-detected if omitted)")
    parser.add_argument("--camera",     type=int, default=0, help="Camera index (default: 0)")
    parser.add_argument("--test-entry", default=None, help="Test image for entry (bypasses camera)")
    parser.add_argument("--test-exit",  default=None, help="Test image for exit (bypasses camera)")
    parser.add_argument("--wokwi", action="store_true",
                        help="Connect to the Wokwi simulator instead of a real board")
    args = parser.parse_args()

    if args.wokwi:
        port = WOKWI_PORT
    else:
        port = args.port or find_arduino_port()

    if not port:
        print("[x] No Arduino port found.")
        print("    Real board:  --port /dev/ttyXXXX")
        print("    Simulation:  --wokwi")
        exit(1)

    run(port, args.camera, args.test_entry, args.test_exit)