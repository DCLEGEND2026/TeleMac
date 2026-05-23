# TeleMac
TeleMac is a lightweight, headless macOS daemon that lets you control your computer with hand gestures.

<img width="1402" height="1122" alt="ChatGPT Image May 23, 2026, 08_47_35 PM" src="https://github.com/user-attachments/assets/f83c02e7-b862-4ecf-b59a-f35d391e1a49" />


---

## 🛠️ Installation & Setup

TeleMac relies on Python 3 and a few external libraries to process webcam data and execute native macOS keystrokes. Follow these steps to get it running smoothly.

### Prerequisites

* **OS:** macOS (Required for Quartz and AppleScript functionality)
* **Python:** Python 3.7 or higher

### Step 1: Clone the Repository

Open your terminal and clone the repository to your local machine:

```bash
git clone https://github.com/DCLEGEND2026/TeleMac.git
cd TeleMac

```

### Step 2: Set Up a Virtual Environment (Recommended)

It is highly recommended to run TeleMac inside a virtual environment to prevent dependency conflicts with other Python projects on your Mac.

```bash
# Create the virtual environment
python3 -m venv venv

# Activate the virtual environment
source venv/bin/activate

```

### Step 3: Install Dependencies

Install the required computer vision and macOS framework libraries using the included requirements file:

```bash
pip install -r requirements.txt

```

*(This will install `opencv-python`, `mediapipe`, and `pyobjc-framework-Quartz`.)*

### Step 4: Grant macOS System Permissions (Crucial)

Because TeleMac reads your webcam and simulates system-wide keyboard shortcuts, macOS will block it by default unless you explicitly grant it permissions.

1. **Camera Access:**
* Run the script for the first time using `python3 main.py --debug`.
* macOS will prompt you: *"Terminal would like to access the camera."* * Click **OK**.


2. **Accessibility Access (For Keystrokes):**
* Quartz needs permission to trigger commands like `Cmd+Q` or `Ctrl+Right`.
* Open **System Settings** > **Privacy & Security** > **Accessibility**.
* Toggle the switch **ON** for the terminal application you are using (e.g., Terminal, iTerm2, or VS Code). If it isn't listed, click the `+` icon and add your terminal app.



### Step 5: Run TeleMac

You can run the daemon in two modes:

**Headless Mode (Default):**
Runs silently in the background with no interface. Perfect for daily use.

```bash
python3 main.py

```

**Debug Mode:**
Opens a live webcam feed showing the MediaPipe landmarks, gesture recognition state, and cooldown timers. Great for testing and getting a feel for the gestures.

```bash
python3 main.py --debug

```

*(Press `q` while the debug window is active, or use `Ctrl+C` in the terminal to stop the daemon.)*
