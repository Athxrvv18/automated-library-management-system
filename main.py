#!/usr/bin/env python3
"""
Automated Library Management System - Raspberry Pi client
- IR issue sensor: BCM GPIO 17
- IR return sensor: BCM GPIO 26
- Android IP Webcam stream
- Barcode scanning with OpenCV + pyzbar
- Optional 16x2 I2C LCD (PCF8574 backpack)

Before running:
1. Update CAMERA_URL to your phone's current IP Webcam stream URL.
2. Confirm LCD I2C address (commonly 0x27 or 0x3F).
3. Confirm the IR sensors output HIGH when triggered. If yours output LOW,
   change SENSOR_ACTIVE_HIGH to False and review the GPIO pull resistors.
"""

import time
import sys
import cv2
import requests
import RPi.GPIO as GPIO
from pyzbar.pyzbar import decode

# --------------------------- CONFIGURATION ---------------------------

API_URL = "https://automated-library-management-system-lbfi.onrender.com/api/update"
DEVICE_ID = "LIBRARY_PI_01"

IR_ISSUE = 17
IR_RETURN = 26

CAMERA_URL = "http://192.168.194.110:8080/video"  # Change if phone IP changes

SCAN_TIMEOUT = 20
SENSOR_DEBOUNCE = 0.25
STEP_DELAY = 1.0
BETWEEN_SCANS_DELAY = 1.5
AFTER_SERVER_DELAY = 2.0
GESTURE_COOLDOWN = 2.0
SENSOR_RELEASE_TIMEOUT = 10.0

# Existing code uses pull-downs and treats HIGH as triggered.
SENSOR_ACTIVE_HIGH = True

# LCD settings. LCD is optional; the program continues without it.
LCD_ENABLED = True
LCD_ADDRESS = 0x27  # Common values: 0x27 or 0x3F
LCD_PORT = 1
LCD_COLUMNS = 16
LCD_ROWS = 2

# ------------------------------- LCD --------------------------------

lcd = None

def lcd_message(line1="", line2=""):
    """Display up to two lines, while keeping the LCD optional."""
    line1 = str(line1)[:LCD_COLUMNS]
    line2 = str(line2)[:LCD_COLUMNS]
    print(f"[LCD] {line1} | {line2}")
    if lcd is None:
        return
    try:
        lcd.clear()
        lcd.cursor_pos = (0, 0)
        lcd.write_string(line1.ljust(LCD_COLUMNS))
        if LCD_ROWS > 1:
            lcd.cursor_pos = (1, 0)
            lcd.write_string(line2.ljust(LCD_COLUMNS))
    except Exception as exc:
        print(f"LCD write error: {exc}")

def setup_lcd():
    global lcd
    if not LCD_ENABLED:
        print("LCD disabled in configuration.")
        return
    try:
        from RPLCD.i2c import CharLCD
        lcd = CharLCD(
            "PCF8574",
            address=LCD_ADDRESS,
            port=LCD_PORT,
            cols=LCD_COLUMNS,
            rows=LCD_ROWS,
            charmap="A00",
            auto_linebreaks=False,
        )
        lcd.clear()
        lcd_message("Library System", "Starting...")
        print(f"LCD initialized at I2C address 0x{LCD_ADDRESS:02X}")
    except Exception as exc:
        lcd = None
        print("LCD unavailable; continuing with terminal messages.")
        print(f"LCD setup detail: {exc}")
        print("Check I2C is enabled, address, wiring, and RPLCD installation.")

# ------------------------------- GPIO -------------------------------

def sensor_triggered(pin):
    value = GPIO.input(pin)
    return bool(value) if SENSOR_ACTIVE_HIGH else not bool(value)

def wait_for_sensor_release(timeout=SENSOR_RELEASE_TIMEOUT):
    """Wait for both sensors to return to their inactive state."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if not sensor_triggered(IR_ISSUE) and not sensor_triggered(IR_RETURN):
            time.sleep(0.2)
            # Require the sensors to stay released briefly.
            if not sensor_triggered(IR_ISSUE) and not sensor_triggered(IR_RETURN):
                return True
        time.sleep(0.05)
    print("Warning: sensor still triggered after release timeout.")
    return False

# ------------------------------ CAMERA ------------------------------

cap = None

def open_camera():
    global cap
    if cap is not None:
        cap.release()
    print("Connecting to Android IP camera:", CAMERA_URL)
    cap = cv2.VideoCapture(CAMERA_URL)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        cap.release()
        cap = None
        return False
    return True

def scan_barcode(scan_name, timeout=SCAN_TIMEOUT):
    """Show the live camera window and return the first decoded barcode."""
    if cap is None or not cap.isOpened():
        lcd_message("Camera error", "Check phone/Wi-Fi")
        print("Camera is not connected.")
        return None

    lcd_message(f"Scan {scan_name}", "Show barcode")
    print(f"\nWaiting up to {timeout}s for {scan_name} barcode...")
    start = time.monotonic()
    last_frame_error = None

    while time.monotonic() - start < timeout:
        ok, frame = cap.read()
        if not ok or frame is None:
            last_frame_error = "Camera frame read failed"
            time.sleep(0.2)
            continue

        try:
            barcodes = decode(frame)
        except Exception as exc:
            print(f"Barcode decode error: {exc}")
            barcodes = []

        for barcode in barcodes:
            try:
                value = barcode.data.decode("utf-8").strip()
            except (UnicodeDecodeError, AttributeError):
                continue

            if not value:
                continue

            # Draw a box around the barcode if polygon coordinates are available.
            points = getattr(barcode, "polygon", None)
            if points:
                pts = [(point.x, point.y) for point in points]
                for i in range(len(pts)):
                    cv2.line(frame, pts[i], pts[(i + 1) % len(pts)], (0, 255, 0), 2)

            cv2.imshow("ALMS Barcode Scanner - press q to cancel", frame)
            cv2.waitKey(300)
            print(f"Barcode detected ({scan_name}): {value}")
            lcd_message(f"{scan_name} scanned", value[:LCD_COLUMNS])
            time.sleep(STEP_DELAY)
            return value

        cv2.imshow("ALMS Barcode Scanner - press q to cancel", frame)
        if (cv2.waitKey(1) & 0xFF) == ord("q"):
            print("Scan cancelled by user.")
            lcd_message("Scan cancelled", "Please wait...")
            return None

    print(f"Barcode scan timed out. {last_frame_error or ''}")
    lcd_message("Scan timeout", f"Retry {scan_name}")
    time.sleep(STEP_DELAY)
    return None

# ------------------------------- API --------------------------------

def send_to_server(action, book_id, user_id):
    payload = {
        "device_id": DEVICE_ID,
        "action": action,
        "book_id": book_id,
        "user_id": user_id,
    }

    lcd_message("Connecting...", "Please wait")
    print("\nSending request to server:", payload)

    try:
        response = requests.post(API_URL, json=payload, timeout=(5, 20))
        print("HTTP status:", response.status_code)
        print("Server response:", response.text[:1000])

        if not response.ok:
            lcd_message("Server error", f"HTTP {response.status_code}")
            return False

        try:
            result = response.json()
        except ValueError:
            lcd_message("Invalid response", "Server returned")
            return False

        if not result.get("success"):
            message = str(result.get("message", "Operation failed"))
            lcd_message("Operation failed", message[:LCD_COLUMNS])
            print("Operation failed:", message)
            return False

        if action == "issue":
            due_date = result.get("due_date", "-")
            fine_per_day = result.get("fine_per_day", 5)
            print("Due date:", due_date)
            print(f"Fine per day: Rs {fine_per_day}")
            lcd_message("Book issued OK", f"Due: {str(due_date)[:LCD_COLUMNS-5]}")
        else:
            late_days = result.get("late_days", 0)
            fine = result.get("fine", 0)
            print("Late days:", late_days)
            print("Fine: Rs", fine)
            lcd_message("Book returned", f"Fine: Rs {fine}")

        print("Email sent:", bool(result.get("email_sent")))
        return True

    except requests.exceptions.Timeout:
        lcd_message("Server timeout", "Try again")
        print("Server request timed out.")
        return False
    except requests.exceptions.ConnectionError:
        lcd_message("No connection", "Check Wi-Fi")
        print("Cannot connect to server. Check network/API URL.")
        return False
    except requests.RequestException as exc:
        lcd_message("Network error", "Check terminal")
        print("Request error:", exc)
        return False
    finally:
        time.sleep(AFTER_SERVER_DELAY)

# ----------------------------- WORKFLOWS -----------------------------

def run_workflow(action):
    label = "ISSUE" if action == "issue" else "RETURN"
    lcd_message(f"{label} mode", "Starting...")
    print(f"\n{'=' * 12} {label} MODE {'=' * 12}")
    time.sleep(STEP_DELAY)

    book_id = scan_barcode("BOOK")
    if not book_id:
        lcd_message("Book not scanned", "Operation stopped")
        time.sleep(STEP_DELAY)
        return

    lcd_message("Book captured", "Next: user ID")
    time.sleep(BETWEEN_SCANS_DELAY)

    user_id = scan_barcode("USER")
    if not user_id:
        lcd_message("User not scanned", "Operation stopped")
        time.sleep(STEP_DELAY)
        return

    lcd_message("Scans complete", "Sending...")
    time.sleep(STEP_DELAY)

    success = send_to_server(action, book_id, user_id)
    if success:
        lcd_message("SUCCESS", "Thank you!")
        print(f"{label} operation successful.")
    else:
        # send_to_server normally displays a more specific error already.
        print(f"{label} operation failed.")
    time.sleep(AFTER_SERVER_DELAY)

# -------------------------------- MAIN -------------------------------

def main():
    setup_lcd()
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(IR_ISSUE, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(IR_RETURN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    lcd_message("Library System", "Opening camera")
    if not open_camera():
        lcd_message("Camera offline", "Check phone URL")
        print("Could not open Android IP Webcam stream.")
        print("Check phone server, Wi-Fi, and CAMERA_URL.")
        raise RuntimeError("Camera could not be opened")

    lcd_message("Camera ready", "Waiting gesture")
    print("\nAutomated Library Management System ready.")
    print("Issue sensor: BCM GPIO", IR_ISSUE)
    print("Return sensor: BCM GPIO", IR_RETURN)
    print("Press q in the camera window to cancel a scan; Ctrl+C to exit.")

    while True:
        if sensor_triggered(IR_ISSUE):
            time.sleep(SENSOR_DEBOUNCE)
            if sensor_triggered(IR_ISSUE):
                print("Issue gesture detected.")
                run_workflow("issue")
                wait_for_sensor_release()
                time.sleep(GESTURE_COOLDOWN)
                lcd_message("Ready", "Waiting gesture")

        elif sensor_triggered(IR_RETURN):
            time.sleep(SENSOR_DEBOUNCE)
            if sensor_triggered(IR_RETURN):
                print("Return gesture detected.")
                run_workflow("return")
                wait_for_sensor_release()
                time.sleep(GESTURE_COOLDOWN)
                lcd_message("Ready", "Waiting gesture")
        else:
            time.sleep(0.08)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSystem stopped by user.")
        lcd_message("System stopped", "Goodbye")
        time.sleep(1)
    except Exception as exc:
        print("Fatal error:", exc)
        try:
            lcd_message("System error", str(exc)[:LCD_COLUMNS])
        except Exception:
            pass
        sys.exit_code = 1
    finally:
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()
        GPIO.cleanup()
        if lcd is not None:
            try:
                lcd.clear()
                lcd.close(clear=True)
            except Exception:
                pass
        print("GPIO, camera, and LCD cleanup complete.")
