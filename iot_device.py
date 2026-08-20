import requests


# ============================================================
# CONFIGURATION
# ============================================================

SERVER_URL = "http://127.0.0.1:5000/api/update"

DEVICE_ID = "LIBRARY_PI_01"


# ============================================================
# SEND DATA
# ============================================================

def send_data(data):

    try:

        response = requests.post(
            SERVER_URL,
            json=data,
            timeout=10
        )

        print("\n========== SERVER RESPONSE ==========")

        try:
            print(response.json())

        except ValueError:
            print(response.text)

        print("=====================================\n")

    except requests.exceptions.ConnectionError:

        print("\n❌ Cannot connect to IoT server.")
        print("Make sure iot_server.py is running.\n")

    except requests.exceptions.Timeout:

        print("\n❌ Server request timed out.\n")

    except Exception as e:

        print("\n❌ Error:", e)


# ============================================================
# ISSUE BOOK
# ============================================================

def issue_book():

    print("\n========== ISSUE BOOK ==========")

    book_id = input("Enter Book ID: ").strip()

    user_id = input("Enter User ID: ").strip()

    if not book_id or not user_id:

        print("❌ Book ID and User ID are required.")

        return

    data = {

        "device_id": DEVICE_ID,

        "action": "issue",

        "book_id": book_id,

        "user_id": user_id
    }

    print("\n📖 Sending book issue request...")

    send_data(data)


# ============================================================
# RETURN BOOK
# ============================================================

def return_book():

    print("\n========== RETURN BOOK ==========")

    book_id = input("Enter Book ID: ").strip()

    user_id = input("Enter User ID: ").strip()

    if not book_id or not user_id:

        print("❌ Book ID and User ID are required.")

        return

    data = {

        "device_id": DEVICE_ID,

        "action": "return",

        "book_id": book_id,

        "user_id": user_id
    }

    print("\n📕 Sending book return request...")

    send_data(data)


# ============================================================
# MAIN MENU
# ============================================================

def main():

    print("\n========================================")
    print("       📚 LIBRARY IoT DEVICE")
    print("========================================")

    print("Device ID:", DEVICE_ID)

    print("Loan Period: 7 days")

    print("Fine: ₹5 per day after due date")

    while True:

        print("\n----------------------------------------")
        print("              MAIN MENU")
        print("----------------------------------------")

        print("1. Issue Book")

        print("2. Return Book")

        print("3. Exit")

        print("----------------------------------------")

        choice = input("Enter your choice: ").strip()

        if choice == "1":

            issue_book()

        elif choice == "2":

            return_book()

        elif choice == "3":

            print("\n👋 Library IoT Device shutting down.")

            break

        else:

            print("\n❌ Invalid choice.")

            print("Please select 1, 2, or 3.")


# ============================================================
# PROGRAM START
# ============================================================

if __name__ == "__main__":

    main()