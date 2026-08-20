import cv2
from pyzbar.pyzbar import decode
import time
import RPi.GPIO as GPIO
import requests


# ============================================================
# CONFIGURATION
# ============================================================

API_URL = "https://automated-library-management-system-lbfi.onrender.com/api/update"

DEVICE_ID = "LIBRARY_PI_01"

IR_ISSUE = 17
IR_RETURN = 26

SCAN_TIMEOUT = 10

GESTURE_COOLDOWN = 2


# ============================================================
# GPIO SETUP
# ============================================================

GPIO.setmode(GPIO.BCM)

GPIO.setup(
    IR_ISSUE,
    GPIO.IN,
    pull_up_down=GPIO.PUD_DOWN
)

GPIO.setup(
    IR_RETURN,
    GPIO.IN,
    pull_up_down=GPIO.PUD_DOWN
)


# ============================================================
# CAMERA
# ============================================================

cap = cv2.VideoCapture(0)

if not cap.isOpened():

    print("❌ Camera could not be opened")

    GPIO.cleanup()

    exit()


# ============================================================
# SERVER COMMUNICATION
# ============================================================

def send_to_server(
    action,
    book_id,
    user_id
):

    data = {

        "device_id":
            DEVICE_ID,

        "action":
            action,

        "book_id":
            book_id,

        "user_id":
            user_id

    }

    print()
    print("📡 Sending request...")
    print("-----------------------------")
    print("Device :", DEVICE_ID)
    print("Action :", action)
    print("Book   :", book_id)
    print("User   :", user_id)
    print("-----------------------------")

    try:

        response = requests.post(
            API_URL,
            json=data,
            timeout=15
        )

        print()
        print("========== SERVER RESPONSE ==========")

        print(
            response.text
        )

        print(
            "====================================="
        )

        if response.ok:

            try:

                result = response.json()

                if result.get(
                    "success"
                ):

                    print(
                        "✅ Operation successful"
                    )

                    if action == "issue":

                        print(
                            "📅 Due Date:",
                            result.get(
                                "due_date",
                                "-"
                            )
                        )

                        print(
                            "💰 Fine:",
                            "₹"
                            + str(
                                result.get(
                                    "fine_per_day",
                                    5
                                )
                            ),
                            "per day"
                        )

                    elif action == "return":

                        print(
                            "📅 Late Days:",
                            result.get(
                                "late_days",
                                0
                            )
                        )

                        print(
                            "💰 Fine:",
                            "₹"
                            + str(
                                result.get(
                                    "fine",
                                    0
                                )
                            )
                        )

                    if result.get(
                        "email_sent"
                    ):

                        print(
                            "📧 Email sent"
                        )

                    else:

                        print(
                            "📧 Email not sent"
                        )

                    return True

                else:

                    print(
                        "❌ Operation failed:"
                    )

                    print(
                        result.get(
                            "message",
                            "Unknown error"
                        )
                    )

                    return False

            except Exception:

                print(
                    "⚠️ Server returned "
                    "non-JSON response"
                )

                return False

        else:

            print(
                "❌ Server HTTP error:",
                response.status_code
            )

            return False

    except requests.exceptions.ConnectionError:

        print()
        print(
            "❌ Cannot connect to server"
        )

        print(
            "Check API_URL and network."
        )

        return False

    except requests.exceptions.Timeout:

        print()
        print(
            "❌ Server request timed out"
        )

        return False

    except Exception as error:

        print(
            "❌ Communication error:",
            error
        )

        return False


# ============================================================
# BARCODE SCANNER
# ============================================================

def scan_barcode(
    scan_name,
    timeout=SCAN_TIMEOUT
):

    print()
    print(
        "📷 Scan",
        scan_name
    )

    print(
        "Waiting for barcode..."
    )

    start_time = time.time()

    while (
        time.time()
        - start_time
        < timeout
    ):

        ret, frame = cap.read()

        if not ret:

            print(
                "❌ Camera frame error"
            )

            return None

        detected_barcodes = decode(
            frame
        )

        for barcode in detected_barcodes:

            try:

                value = (
                    barcode.data
                    .decode("utf-8")
                    .strip()
                )

            except Exception:

                continue

            if value:

                print()
                print(
                    "✅ Barcode detected:",
                    value
                )

                # Draw the barcode area
                points = barcode.polygon

                if points:

                    pts = []

                    for point in points:

                        pts.append(
                            (
                                point.x,
                                point.y
                            )
                        )

                    for i in range(
                        len(pts)
                    ):

                        cv2.line(
                            frame,
                            pts[i],
                            pts[
                                (i + 1)
                                % len(pts)
                            ],
                            (0, 255, 0),
                            2
                        )

                cv2.imshow(
                    "ALMS Barcode Scanner",
                    frame
                )

                cv2.waitKey(500)

                return value

        cv2.imshow(
            "ALMS Barcode Scanner",
            frame
        )

        key = cv2.waitKey(1) & 0xFF

        if key == ord("q"):

            print(
                "❌ Scanner cancelled"
            )

            return None

    print()
    print(
        "⏱️ Barcode scan timeout"
    )

    return None


# ============================================================
# WAIT FOR HAND GESTURE TO FINISH
# ============================================================

def wait_for_gesture_release():

    time.sleep(
        GESTURE_COOLDOWN
    )

    start = time.time()

    while (
        time.time() - start
        < 3
    ):

        issue_state = GPIO.input(
            IR_ISSUE
        )

        return_state = GPIO.input(
            IR_RETURN
        )

        if (
            not issue_state
            and
            not return_state
        ):

            return

        time.sleep(0.05)


# ============================================================
# ISSUE WORKFLOW
# ============================================================

def issue_workflow():

    print()
    print(
        "========================================"
    )

    print(
        "📖 ISSUE MODE"
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # BOOK
    # --------------------------------------------------------

    book_id = scan_barcode(
        "BOOK BARCODE"
    )

    if not book_id:

        print(
            "❌ Book barcode not scanned"
        )

        return

    # --------------------------------------------------------
    # USER
    # --------------------------------------------------------

    user_id = scan_barcode(
        "USER BARCODE"
    )

    if not user_id:

        print(
            "❌ User barcode not scanned"
        )

        return

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------

    success = send_to_server(
        "issue",
        book_id,
        user_id
    )

    if success:

        print()
        print(
            "🎉 BOOK ISSUED SUCCESSFULLY"
        )

    else:

        print()
        print(
            "❌ BOOK ISSUE FAILED"
        )


# ============================================================
# RETURN WORKFLOW
# ============================================================

def return_workflow():

    print()
    print(
        "========================================"
    )

    print(
        "📕 RETURN MODE"
    )

    print(
        "========================================"
    )

    # --------------------------------------------------------
    # BOOK
    # --------------------------------------------------------

    book_id = scan_barcode(
        "BOOK BARCODE"
    )

    if not book_id:

        print(
            "❌ Book barcode not scanned"
        )

        return

    # --------------------------------------------------------
    # USER
    # --------------------------------------------------------

    user_id = scan_barcode(
        "USER BARCODE"
    )

    if not user_id:

        print(
            "❌ User barcode not scanned"
        )

        return

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------

    success = send_to_server(
        "return",
        book_id,
        user_id
    )

    if success:

        print()
        print(
            "🎉 BOOK RETURNED SUCCESSFULLY"
        )

    else:

        print()
        print(
            "❌ BOOK RETURN FAILED"
        )


# ============================================================
# MAIN LOOP
# ============================================================

print()
print(
    "========================================"
)

print(
    "📚 AUTOMATED LIBRARY MANAGEMENT SYSTEM"
)

print(
    "========================================"
)

print(
    "Device:",
    DEVICE_ID
)

print(
    "IR ISSUE GPIO:",
    IR_ISSUE
)

print(
    "IR RETURN GPIO:",
    IR_RETURN
)

print(
    "API:",
    API_URL
)

print(
    "========================================"
)

print()
print(
    "Waiting for hand gesture..."
)

try:

    while True:

        # ====================================================
        # ISSUE SENSOR
        # ====================================================

        if GPIO.input(
            IR_ISSUE
        ):

            print()
            print(
                "👋 LEFT / ISSUE gesture detected"
            )

            # Wait until hand leaves sensor

            time.sleep(0.5)

            issue_workflow()

            wait_for_gesture_release()

            print()
            print(
                "👉 Waiting for next gesture..."
            )


        # ====================================================
        # RETURN SENSOR
        # ====================================================

        elif GPIO.input(
            IR_RETURN
        ):

            print()
            print(
                "👋 RIGHT / RETURN gesture detected"
            )

            # Wait until hand leaves sensor

            time.sleep(0.5)

            return_workflow()

            wait_for_gesture_release()

            print()
            print(
                "👉 Waiting for next gesture..."
            )


        # ====================================================
        # WAIT
        # ====================================================

        else:

            time.sleep(
                0.05
            )


# ============================================================
# CLEANUP
# ============================================================

except KeyboardInterrupt:

    print()
    print(
        "🛑 System stopped by user"
    )


except Exception as error:

    print()
    print(
        "❌ Unexpected error:",
        error
    )


finally:

    print(
        "Cleaning up..."
    )

    cap.release()

    cv2.destroyAllWindows()

    GPIO.cleanup()

    print(
        "✅ GPIO and camera released"
    )