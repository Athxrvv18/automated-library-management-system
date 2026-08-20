#!/usr/bin/env python3

"""
============================================================
AUTOMATED LIBRARY MANAGEMENT SYSTEM
Raspberry Pi IoT Terminal
============================================================

Hardware:
- Raspberry Pi
- Ethernet/LAN
- 16x2 I2C LCD
- USB/Pi Camera
- IR sensor on GPIO 17 -> ISSUE
- IR sensor on GPIO 26 -> RETURN
- No push buttons
- No buzzer

Flow:
WELCOME -> ISSUE/RETURN -> Scan Book -> Scan User
        -> Render ALMS API -> Show result on LCD
        -> Return to welcome screen
============================================================
"""

import time
import requests
import cv2
from pyzbar.pyzbar import decode
import RPi.GPIO as GPIO

try:
    from smbus2 import SMBus
except ImportError:
    from smbus import SMBus


# ============================================================
# CONFIGURATION
# ============================================================

# Your deployed ALMS API
API_URL = (
    "https://automated-library-management-system-lbfi.onrender.com"
    "/api/update"
)

DEVICE_ID = "LIBRARY_PI_01"

# I2C LCD address.
# Common addresses are 0x27 and 0x3F.
LCD_ADDRESS = 0x27
I2C_BUS = 1

# IR sensors
# IR ISSUE sensor  -> GPIO 17
# IR RETURN sensor -> GPIO 26
#
# Most common IR obstacle sensors are active LOW:
# object detected = LOW
# no object       = HIGH
IR_ISSUE_PIN = 17
IR_RETURN_PIN = 26

# Camera
CAMERA_INDEX = 0

# Barcode scanning timeout
SCAN_TIMEOUT = 15

# LCD timing
WELCOME_DELAY = 2
RESULT_DELAY = 3


# ============================================================
# PCF8574 16x2 I2C LCD DRIVER
# ============================================================

LCD_BACKLIGHT = 0x08
LCD_ENABLE = 0x04
LCD_RS = 0x01


class I2CLCD:
    def __init__(self, address=LCD_ADDRESS, bus_number=I2C_BUS):
        self.address = address
        self.bus = SMBus(bus_number)

        self._write_byte(0x00)
        time.sleep(0.05)

        # Initialize LCD in 4-bit mode
        self._write4bits(0x30)
        time.sleep(0.005)

        self._write4bits(0x30)
        time.sleep(0.001)

        self._write4bits(0x30)
        time.sleep(0.001)

        self._write4bits(0x20)
        time.sleep(0.001)

        self.command(0x28)  # 4-bit, 2 lines, 5x8
        self.command(0x08)  # Display off
        self.command(0x01)  # Clear
        time.sleep(0.002)
        self.command(0x06)  # Entry mode
        self.command(0x0C)  # Display on, cursor off

    def _write_byte(self, value):
        self.bus.write_byte(self.address, value | LCD_BACKLIGHT)

    def _pulse_enable(self, value):
        self._write_byte(value | LCD_ENABLE)
        time.sleep(0.0005)
        self._write_byte(value & ~LCD_ENABLE)
        time.sleep(0.0005)

    def _write4bits(self, value):
        self._write_byte(value)
        self._pulse_enable(value)

    def send(self, value, mode=0):
        high = value & 0xF0
        low = (value << 4) & 0xF0

        self._write4bits(high | mode)
        self._write4bits(low | mode)

    def command(self, value):
        self.send(value, 0)

    def write_char(self, value):
        self.send(value, LCD_RS)

    def clear(self):
        self.command(0x01)
        time.sleep(0.002)

    def set_cursor(self, row, col):
        row_offsets = [0x00, 0x40]
        self.command(0x80 | (row_offsets[row] + col))

    def write_line(self, row, text):
        text = str(text)[:16].ljust(16)
        self.set_cursor(row, 0)

        for char in text:
            self.write_char(ord(char))

    def display(self, line1="", line2=""):
        self.write_line(0, line1)
        self.write_line(1, line2)

    def close(self):
        try:
            self.clear()
            self.bus.close()
        except Exception:
            pass


# ============================================================
# LCD HELPER
# ============================================================

lcd = None


def lcd_show(line1="", line2=""):
    global lcd

    if lcd is None:
        return

    try:
        lcd.display(line1, line2)
    except Exception as error:
        print("LCD error:", error)


# ============================================================
# CAMERA
# ============================================================

camera = None


def open_camera():
    global camera

    print("📷 Opening camera...")

    camera = cv2.VideoCapture(CAMERA_INDEX)

    if not camera.isOpened():
        print("❌ Camera could not be opened")
        return False

    # Small delay for camera initialization
    time.sleep(1)

    print("✅ Camera ready")
    return True


def scan_barcode(prompt):
    """
    Scan one barcode using the camera.

    Returns:
        barcode string or None
    """

    if camera is None or not camera.isOpened():
        print("❌ Camera is not available")
        lcd_show("Camera Error", "Restart terminal")
        time.sleep(2)
        return None

    lcd_show(prompt, "Show barcode")
    print()
    print("📷", prompt)
    print("Show barcode to camera...")

    start_time = time.time()

    while time.time() - start_time < SCAN_TIMEOUT:

        success, frame = camera.read()

        if not success:
            print("❌ Camera frame error")
            time.sleep(0.1)
            continue

        barcodes = decode(frame)

        for barcode in barcodes:

            try:
                value = barcode.data.decode("utf-8").strip()
            except Exception:
                continue

            if value:

                print("✅ Scanned:", value)

                # Prevent the same barcode from being read repeatedly
                time.sleep(0.7)

                return value

        # Do not use cv2.imshow().
        # This keeps the program usable on a headless Raspberry Pi.

        time.sleep(0.03)

    print("⏱️ Barcode scan timeout")
    lcd_show("Scan Timeout", "Try again")
    time.sleep(2)

    return None


# ============================================================
# SERVER COMMUNICATION
# ============================================================

def send_to_server(action, book_id, user_id):
    """
    Send issue/return request to the deployed ALMS server.
    """

    payload = {
        "device_id": DEVICE_ID,
        "action": action,
        "book_id": book_id,
        "user_id": user_id
    }

    print()
    print("=" * 50)
    print("📡 ALMS REQUEST")
    print("=" * 50)
    print("URL   :", API_URL)
    print("Device:", DEVICE_ID)
    print("Action:", action)
    print("Book  :", book_id)
    print("User  :", user_id)

    lcd_show("Processing...", "Please wait")

    try:

        response = requests.post(
            API_URL,
            json=payload,
            timeout=40
        )

        print("HTTP :", response.status_code)

        try:
            result = response.json()
        except Exception:
            print("❌ Server returned non-JSON:")
            print(response.text)
            lcd_show("Server Error", "Invalid response")
            time.sleep(3)
            return None

        print("Response:", result)

        return result

    except requests.exceptions.Timeout:

        print("❌ Request timed out")
        lcd_show("Network Timeout", "Try again")
        time.sleep(3)

        return None

    except requests.exceptions.ConnectionError as error:

        print("❌ Network connection error:", error)
        lcd_show("Network Error", "Check LAN")
        time.sleep(3)

        return None

    except Exception as error:

        print("❌ Unexpected API error:", error)
        lcd_show("API Error", "Try again")
        time.sleep(3)

        return None


# ============================================================
# ISSUE RESULT
# ============================================================

def show_issue_result(result):

    if not result:
        return

    if result.get("success"):

        due_date = result.get("due_date", "-")
        email_sent = result.get("email_sent", False)

        lcd_show("BOOK ISSUED", "Successfully")
        time.sleep(2)

        # Keep the LCD readable by showing date separately
        lcd_show("Due Date:", str(due_date)[0:16])
        time.sleep(2)

        if email_sent:
            lcd_show("Email Sent", "Check inbox")
        else:
            lcd_show("Email Not Sent", "Contact admin")

        time.sleep(RESULT_DELAY)

    else:

        message = result.get(
            "message",
            "Issue failed"
        )

        print("❌ Issue failed:", message)

        lcd_show("ISSUE FAILED", str(message)[:16])
        time.sleep(RESULT_DELAY)


# ============================================================
# RETURN RESULT
# ============================================================

def show_return_result(result):

    if not result:
        return

    if result.get("success"):

        fine = result.get("fine", 0)
        late_days = result.get("late_days", 0)
        email_sent = result.get("email_sent", False)

        lcd_show("BOOK RETURNED", "Successfully")
        time.sleep(2)

        lcd_show(
            "Fine: Rs " + str(fine),
            "Late: " + str(late_days) + " days"
        )
        time.sleep(2)

        if email_sent:
            lcd_show("Email Sent", "Check inbox")
        else:
            lcd_show("Email Not Sent", "Contact admin")

        time.sleep(RESULT_DELAY)

    else:

        message = result.get(
            "message",
            "Return failed"
        )

        print("❌ Return failed:", message)

        lcd_show("RETURN FAILED", str(message)[:16])
        time.sleep(RESULT_DELAY)


# ============================================================
# ISSUE WORKFLOW
# ============================================================

def issue_workflow():

    print()
    print("📖 ISSUE MODE")

    lcd_show("ISSUE BOOK", "Scan Book")

    book_id = scan_barcode("Scan Book")

    if not book_id:
        return

    # Show scanned book
    lcd_show("Book:", book_id)
    time.sleep(1)

    user_id = scan_barcode("Scan User")

    if not user_id:
        return

    lcd_show("User:", user_id)
    time.sleep(1)

    result = send_to_server(
        "issue",
        book_id,
        user_id
    )

    show_issue_result(result)


# ============================================================
# RETURN WORKFLOW
# ============================================================

def return_workflow():

    print()
    print("📕 RETURN MODE")

    lcd_show("RETURN BOOK", "Scan Book")

    book_id = scan_barcode("Scan Book")

    if not book_id:
        return

    lcd_show("Book:", book_id)
    time.sleep(1)

    user_id = scan_barcode("Scan User")

    if not user_id:
        return

    lcd_show("User:", user_id)
    time.sleep(1)

    result = send_to_server(
        "return",
        book_id,
        user_id
    )

    show_return_result(result)


# ============================================================
# WELCOME SCREEN
# ============================================================

def welcome_screen():

    lcd_show(
        "WELCOME TO",
        "LIBRARY SYSTEM"
    )

    print()
    print("========================================")
    print("📚 WELCOME TO LIBRARY SYSTEM")
    print("========================================")

    time.sleep(WELCOME_DELAY)


def selection_screen():

    lcd_show(
        "< ISSUE",
        "RETURN >"
    )

    print()
    print("========================================")
    print("SELECT OPERATION")
    print("IR SENSOR GPIO 17 = ISSUE")
    print("IR SENSOR GPIO 26 = RETURN")
    print("========================================")


# ============================================================
# IR SENSOR SELECTION
# ============================================================

def wait_for_selection():

    lcd_show(
        "< ISSUE",
        "RETURN >"
    )

    print()
    print("========================================")
    print("SELECT OPERATION")
    print("IR SENSOR 1 / GPIO 17 = ISSUE")
    print("IR SENSOR 2 / GPIO 26 = RETURN")
    print("========================================")

    # Sensors are assumed active LOW.
    # A short debounce prevents one detection from triggering
    # multiple operations.

    while True:

        if GPIO.input(IR_ISSUE_PIN) == GPIO.LOW:

            time.sleep(0.25)

            if GPIO.input(IR_ISSUE_PIN) == GPIO.LOW:

                print("➡️ ISSUE IR SENSOR DETECTED")

                # Wait until the object/user moves away.
                # This prevents immediate retriggering.
                while GPIO.input(IR_ISSUE_PIN) == GPIO.LOW:
                    time.sleep(0.05)

                return "issue"

        if GPIO.input(IR_RETURN_PIN) == GPIO.LOW:

            time.sleep(0.25)

            if GPIO.input(IR_RETURN_PIN) == GPIO.LOW:

                print("➡️ RETURN IR SENSOR DETECTED")

                while GPIO.input(IR_RETURN_PIN) == GPIO.LOW:
                    time.sleep(0.05)

                return "return"

        time.sleep(0.05)


# ============================================================
# GPIO SETUP
# ============================================================

def setup_gpio():

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)

    GPIO.setup(
        IR_ISSUE_PIN,
        GPIO.IN,
        pull_up_down=GPIO.PUD_UP
    )

    GPIO.setup(
        IR_RETURN_PIN,
        GPIO.IN,
        pull_up_down=GPIO.PUD_UP
    )

    print("✅ GPIO ready")


# ============================================================
# LCD SETUP
# ============================================================

def setup_lcd():

    global lcd

    try:

        lcd = I2CLCD(
            address=LCD_ADDRESS,
            bus_number=I2C_BUS
        )

        lcd_show(
            "LCD READY",
            "Library System"
        )

        print(
            f"✅ LCD ready at 0x{LCD_ADDRESS:02X}"
        )

        time.sleep(1)

        return True

    except Exception as error:

        print()
        print("❌ LCD ERROR")
        print(error)
        print()
        print(
            "Check LCD address and I2C wiring."
        )

        return False


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():

    print()
    print("=" * 60)
    print("📚 AUTOMATED LIBRARY MANAGEMENT SYSTEM")
    print("🥧 RASPBERRY PI IoT TERMINAL")
    print("=" * 60)

    setup_gpio()

    if not setup_lcd():

        print(
            "❌ LCD setup failed."
        )

        print(
            "Fix LCD/I2C before continuing."
        )

        return

    if not open_camera():

        lcd_show(
            "Camera Error",
            "Check camera"
        )

        return

    # --------------------------------------------------------
    # Main loop
    # --------------------------------------------------------

    while True:

        try:

            # Welcome
            welcome_screen()

            # Choose Issue / Return
            action = wait_for_selection()

            # Issue
            if action == "issue":

                issue_workflow()

            # Return
            elif action == "return":

                return_workflow()

            # Small pause before returning to welcome
            time.sleep(1)

        except KeyboardInterrupt:

            print()
            print("🛑 Program stopped by user.")
            break

        except Exception as error:

            print()
            print("❌ MAIN ERROR:")
            print(error)

            lcd_show(
                "System Error",
                "Restarting..."
            )

            time.sleep(3)


# ============================================================
# CLEANUP
# ============================================================

if __name__ == "__main__":

    try:

        main()

    finally:

        print()
        print("🧹 Cleaning up...")

        try:
            if camera is not None:
                camera.release()
        except Exception:
            pass

        try:
            GPIO.cleanup()
        except Exception:
            pass

        try:
            if lcd is not None:
                lcd.close()
        except Exception:
            pass

        print("✅ Raspberry Pi terminal stopped.")
