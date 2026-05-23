#!/usr/bin/env python3
"""
macOS Gesture Daemon  —  v8
──────────────────────────────────────────────────────
Headless by default.  Pass --debug to see camera + landmarks + live state.

  python3 gesture_daemon_mac.py            # silent daemon
  python3 gesture_daemon_mac.py --debug    # debug window with overlay

GESTURES
  Pinch (thumb+index, hold)        →  Launchpad
  Open-palm Swipe ← / →            →  Cycle Apps (forward/back, AppleScript)
  Open-palm Swipe ↑                →  Mission Control
  Open-palm Swipe ↓                →  Show Desktop
  3-finger Swipe ← / →             →  Switch Space  (Ctrl+←/→)
  Fist (hold ~1.2s)                →  Quit frontmost (Cmd+Q)
  Index+Middle pinch (hold)        →  App Switcher (Cmd+Tab tap)
  Pinky-only up (hold)             →  Screenshot   (Cmd+Shift+3)
  OK sign (hold)                   →  Lock Screen
"""

# Suppress MediaPipe / protobuf deprecation noise BEFORE importing them
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="google.protobuf")
import os
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["GLOG_minloglevel"] = "3"

import argparse
import cv2
import mediapipe as mp
import math
import sys
import time
import subprocess
from collections import deque

try:
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventPost,
        CGEventSetFlags,
        kCGHIDEventTap,
        kCGEventFlagMaskCommand,
        kCGEventFlagMaskControl,
        kCGEventFlagMaskShift,
    )
    QUARTZ_OK = True
except ImportError:
    QUARTZ_OK = False

# ── Virtual key codes ────────────────────────────────────────────────────────
VK_LEFT     = 0x7B
VK_RIGHT    = 0x7C
VK_TAB      = 0x30
VK_Q        = 0x0C
VK_3        = 0x14
VK_BACKTICK = 0x32

# ── Tuning ───────────────────────────────────────────────────────────────────
PINCH_TH         = 0.050
PINCH2_TH        = 0.055
OK_TH            = 0.055
PINCH_HOLD_SEC   = 0.25
HOLD_GESTURE_SEC = 0.5
FIST_HOLD_SEC    = 1.2
GESTURE_COOLDOWN = 1.2

SWIPE_HISTORY     = 10
SWIPE_VEL_TH      = 0.014
SWIPE_MIN_TRAVEL  = 0.12
PALM_GRACE_FRAMES = 5

CAM_W, CAM_H     = 640, 480

# ── CGEvent helpers ──────────────────────────────────────────────────────────
def _post(keycode: int, flags: int = 0):
    if not QUARTZ_OK:
        return
    dn = CGEventCreateKeyboardEvent(None, keycode, True)
    up = CGEventCreateKeyboardEvent(None, keycode, False)
    if flags:
        CGEventSetFlags(dn, flags)
        CGEventSetFlags(up, flags)
    CGEventPost(kCGHIDEventTap, dn)
    CGEventPost(kCGHIDEventTap, up)

def _osa(script: str):
    subprocess.Popen(["osascript", "-e", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _osa_blocking(script: str) -> str:
    """Run osascript and capture stdout (used for app cycling logic)."""
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=2)
        return out.stdout.strip()
    except Exception as e:
        return f"[err:{e}]"

def _shell(cmd: list):
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ── macOS actions ────────────────────────────────────────────────────────────
def act_launchpad():
    _shell(["open", "-a", "Launchpad"]); _log("→ Launchpad")

def act_mission_control():
    _shell(["open", "-a", "Mission Control"]); _log("↑ Mission Control")

def act_show_desktop():
    _osa('tell application "System Events" to key code 103'); _log("↓ Show Desktop")

# ── Spaces  (open-palm horizontal swipe) ─────────────────────────────────────
def act_next_space():
    _post(VK_RIGHT, kCGEventFlagMaskControl); _log("→ Next Space (Ctrl+→)")

def act_prev_space():
    _post(VK_LEFT, kCGEventFlagMaskControl); _log("← Prev Space (Ctrl+←)")

# ── App cycling  (3-finger swipe) — AppleScript, key-interception-proof ──────
# Strategy:
#   Get a stable list of visible app processes.
#   Find the frontmost one.
#   Activate the next/prev in the list.
#
# This is robust because:
#   1. It does NOT send keystrokes — terminal can't eat the input
#   2. It walks the actual app list, not just the most-recent-two
#   3. Forward / backward both work properly

_APP_LIST_SCRIPT = '''
set AppleScript's text item delimiters to "|||"
tell application "System Events"
    set visibleApps to (name of every application process whose visible is true and background only is false)
    set frontApp to name of first application process whose frontmost is true
    set appListStr to visibleApps as string
end tell
set AppleScript's text item delimiters to ""
return frontApp & "###" & appListStr
'''

def _get_app_state():
    """Return (frontmost_name, ordered_app_list)."""
    raw = _osa_blocking(_APP_LIST_SCRIPT)
    if "###" not in raw:
        return None, []
    front, rest = raw.split("###", 1)
    apps = [a.strip() for a in rest.split("|||") if a.strip()]
    return front.strip(), apps

def _activate_app(name: str):
    safe = name.replace('"', '\\"')
    _osa(f'tell application "System Events" to set frontmost of (first application process whose name is "{safe}") to true')

def _cycle_app(direction: int):
    """direction: +1 = forward (next), -1 = backward (prev)."""
    front, apps = _get_app_state()
    if not apps or front is None:
        _log(f"[!] couldn't read app list (front={front!r})")
        return
    if front not in apps:
        target = apps[0] if direction > 0 else apps[-1]
    else:
        idx = apps.index(front)
        target_idx = (idx + direction) % len(apps)
        target = apps[target_idx]
    _activate_app(target)
    arrow = "→" if direction > 0 else "←"
    _log(f"{arrow} App: {front}  →  {target}   ({len(apps)} apps)")

def act_next_app():
    _cycle_app(+1)

def act_prev_app():
    _cycle_app(-1)

# ── Index+middle pinch hold: classic Cmd+Tab tap ─────────────────────────────
def act_app_switcher():
    if not QUARTZ_OK: return
    dn = CGEventCreateKeyboardEvent(None, VK_TAB, True)
    CGEventSetFlags(dn, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, dn)
    time.sleep(0.08)
    up = CGEventCreateKeyboardEvent(None, VK_TAB, False)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, up)
    _log("→ App Switcher (Cmd+Tab tap)")

def act_quit_frontmost():
    _post(VK_Q, kCGEventFlagMaskCommand); _log("→ Quit frontmost")

def act_screenshot():
    _post(VK_3, kCGEventFlagMaskCommand | kCGEventFlagMaskShift); _log("→ Screenshot")

def act_lock():
    _osa('tell application "System Events" to keystroke "q" using {command down, control down}')
    _log("→ Lock Screen")

# ── Logging ──────────────────────────────────────────────────────────────────
def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}]  {msg}", flush=True)

# ── Hand geometry ────────────────────────────────────────────────────────────
def _dist(a, b) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)

def _is_fist(lm) -> bool:
    tips = [8, 12, 16, 20]; pips = [6, 10, 14, 18]
    return all(lm[t].y > lm[p].y for t, p in zip(tips, pips))

def _fingers_up(lm):
    tips = [8, 12, 16, 20]; pips = [6, 10, 14, 18]
    return tuple(lm[t].y < lm[p].y for t, p in zip(tips, pips))

# ── Hold debouncer ───────────────────────────────────────────────────────────
class HoldDebounce:
    def __init__(self, threshold_sec: float):
        self.threshold = threshold_sec
        self.start_t   = None
        self.fired     = False

    def update(self, active: bool) -> bool:
        if active:
            if self.start_t is None:
                self.start_t = time.time()
                self.fired   = False
            elif not self.fired and (time.time() - self.start_t) >= self.threshold:
                self.fired = True
                return True
        else:
            self.start_t = None
            self.fired   = False
        return False

    def progress(self) -> float:
        if self.start_t is None: return 0.0
        return min(1.0, (time.time() - self.start_t) / self.threshold)

# ── Gesture processor ────────────────────────────────────────────────────────
class GestureProcessor:
    def __init__(self):
        self._last_fire = 0.0
        self._wx        = deque(maxlen=SWIPE_HISTORY)
        self._wy        = deque(maxlen=SWIPE_HISTORY)
        self._palm_miss = 0

        self._wx3        = deque(maxlen=SWIPE_HISTORY)
        self._wy3        = deque(maxlen=SWIPE_HISTORY)
        self._three_miss = 0

        self.hd_pinch   = HoldDebounce(PINCH_HOLD_SEC)
        self.hd_pinch2  = HoldDebounce(PINCH_HOLD_SEC)
        self.hd_ok      = HoldDebounce(HOLD_GESTURE_SEC)
        self.hd_pinky   = HoldDebounce(HOLD_GESTURE_SEC)
        self.hd_fist    = HoldDebounce(FIST_HOLD_SEC)

        self.dbg = {
            "pose": "—", "fingers": (False,)*4,
            "dx": 0.0, "dy": 0.0, "vx": 0.0, "vy": 0.0,
            "buf_len": 0, "buf3_len": 0,
        }

    def _can_fire(self) -> bool:
        return (time.time() - self._last_fire) >= GESTURE_COOLDOWN

    def cooldown_remaining(self) -> float:
        return max(0.0, GESTURE_COOLDOWN - (time.time() - self._last_fire))

    def _fire(self, fn):
        if self._can_fire():
            fn()
            self._last_fire = time.time()
            self._wx.clear(); self._wy.clear()
            self._wx3.clear(); self._wy3.clear()

    def update(self, lm):
        thumb, index, middle, wrist = lm[4], lm[8], lm[12], lm[0]

        d_pi = _dist(thumb, index)
        d_im = _dist(index, middle)
        d_tm = _dist(thumb, middle)

        pinching   = d_pi < PINCH_TH
        pinching2  = d_im < PINCH2_TH and not pinching
        ok_sign    = d_tm < OK_TH and not pinching and not pinching2
        fist_now   = _is_fist(lm) and not pinching
        ix, mx, rx, px = _fingers_up(lm)

        pinky_only = (px and not ix and not mx and not rx
                      and not pinching and not pinching2 and not fist_now)

        three_fingers = (ix and mx and rx and not px
                         and not pinching and not pinching2 and not fist_now)

        fingers_up_count = sum((ix, mx, rx, px))
        open_palm = (fingers_up_count >= 3 and px
                     and not pinching and not pinching2
                     and not fist_now and not ok_sign)

        # Open-palm motion buffer
        if open_palm:
            self._wx.append(wrist.x)
            self._wy.append(wrist.y)
            self._palm_miss = 0
        else:
            self._palm_miss += 1
            if self._palm_miss > PALM_GRACE_FRAMES:
                self._wx.clear(); self._wy.clear()

        # 3-finger motion buffer
        if three_fingers:
            self._wx3.append(wrist.x)
            self._wy3.append(wrist.y)
            self._three_miss = 0
        else:
            self._three_miss += 1
            if self._three_miss > PALM_GRACE_FRAMES:
                self._wx3.clear(); self._wy3.clear()

        # ── Open-palm swipe → Spaces / Mission Control / Show Desktop ───────
        dx = dy = vx = vy = 0.0
        if len(self._wx) >= SWIPE_HISTORY:
            dx = self._wx[-1] - self._wx[0]
            dy = self._wy[-1] - self._wy[0]
            vx = dx / (SWIPE_HISTORY - 1)
            vy = dy / (SWIPE_HISTORY - 1)

            if self._can_fire():
                horiz = abs(dx) > abs(dy)
                if horiz and abs(vx) > SWIPE_VEL_TH and abs(dx) > SWIPE_MIN_TRAVEL:
                    if vx > 0: self._fire(act_next_app)
                    else:      self._fire(act_prev_app)
                elif (not horiz) and abs(vy) > SWIPE_VEL_TH and abs(dy) > SWIPE_MIN_TRAVEL:
                    if vy < 0: self._fire(act_mission_control)
                    else:      self._fire(act_show_desktop)

        # ── 3-finger swipe → Spaces (Ctrl+←/→) ──────────────────────────────
        if len(self._wx3) >= SWIPE_HISTORY and self._can_fire():
            dx3 = self._wx3[-1] - self._wx3[0]
            dy3 = self._wy3[-1] - self._wy3[0]
            vx3 = dx3 / (SWIPE_HISTORY - 1)
            if abs(dx3) > abs(dy3) and abs(vx3) > SWIPE_VEL_TH and abs(dx3) > SWIPE_MIN_TRAVEL:
                if vx3 > 0: self._fire(act_next_space)
                else:       self._fire(act_prev_space)

        # ── Hold-debounced gestures ─────────────────────────────────────────
        if self.hd_pinch.update(pinching):   self._fire(act_launchpad)
        if self.hd_pinch2.update(pinching2): self._fire(act_app_switcher)
        if self.hd_ok.update(ok_sign):       self._fire(act_lock)
        if self.hd_pinky.update(pinky_only): self._fire(act_screenshot)
        if self.hd_fist.update(fist_now):    self._fire(act_quit_frontmost)

        # ── Debug snapshot ──────────────────────────────────────────────────
        if pinching:        pose = "pinch (thumb+index)"
        elif pinching2:     pose = "pinch2 (index+middle)"
        elif ok_sign:       pose = "OK sign"
        elif fist_now:      pose = "fist"
        elif pinky_only:    pose = "pinky-only"
        elif three_fingers: pose = "three-finger (I+M+R)"
        elif open_palm:     pose = f"open-palm ({fingers_up_count}/4)"
        else:               pose = f"unknown ({fingers_up_count}/4)"

        self.dbg.update(pose=pose, fingers=(ix, mx, rx, px),
                        dx=dx, dy=dy, vx=vx, vy=vy,
                        buf_len=len(self._wx), buf3_len=len(self._wx3))

    def reset(self):
        self._wx.clear(); self._wy.clear()
        self._wx3.clear(); self._wy3.clear()
        self._palm_miss = 0
        self._three_miss = 0
        for hd in (self.hd_pinch, self.hd_pinch2, self.hd_ok,
                   self.hd_pinky, self.hd_fist):
            hd.start_t = None; hd.fired = False
        self.dbg["pose"] = "no hand"

# ── Debug overlay ────────────────────────────────────────────────────────────
def draw_debug(frame, proc, landmarks=None):
    h, w = frame.shape[:2]
    if landmarks is not None:
        mp_drawing = mp.solutions.drawing_utils
        mp_styles  = mp.solutions.drawing_styles
        mp_hands_d = mp.solutions.hands
        mp_drawing.draw_landmarks(
            frame, landmarks, mp_hands_d.HAND_CONNECTIONS,
            mp_styles.get_default_hand_landmarks_style(),
            mp_styles.get_default_hand_connections_style(),
        )

    d = proc.dbg
    ix, mx_, rx, px = d["fingers"]
    cd = proc.cooldown_remaining()
    cd_txt = f"cooldown: {cd:.2f}s" if cd > 0 else "ready"
    overlay = [
        f"pose:    {d['pose']}",
        f"fingers: I:{int(ix)} M:{int(mx_)} R:{int(rx)} P:{int(px)}",
        f"palm buf: {d['buf_len']}/{SWIPE_HISTORY}   3f buf: {d['buf3_len']}/{SWIPE_HISTORY}",
        f"dx={d['dx']:+.3f}  dy={d['dy']:+.3f}",
        f"vx={d['vx']:+.4f}  vy={d['vy']:+.4f}",
        f"thresh:  vel>{SWIPE_VEL_TH}  travel>{SWIPE_MIN_TRAVEL}",
        f"{cd_txt}",
    ]
    cv2.rectangle(frame, (0, 0), (380, 22 * len(overlay) + 10), (0, 0, 0), -1)
    for i, line in enumerate(overlay):
        cv2.putText(frame, line, (8, 22 * (i + 1)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 200), 1, cv2.LINE_AA)

    bars = [
        ("pinch",  proc.hd_pinch.progress()),
        ("pinch2", proc.hd_pinch2.progress()),
        ("ok",     proc.hd_ok.progress()),
        ("pinky",  proc.hd_pinky.progress()),
        ("fist",   proc.hd_fist.progress()),
    ]
    bx = w - 150
    for i, (name, prog) in enumerate(bars):
        y = 20 + i * 26
        cv2.rectangle(frame, (bx, y), (bx + 140, y + 18), (40, 40, 40), -1)
        cv2.rectangle(frame, (bx, y), (bx + int(140 * prog), y + 18),
                      (0, 200, 255), -1)
        cv2.putText(frame, name, (bx + 4, y + 13),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    return frame

# ── Entry ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true",
                    help="Show camera window with landmarks and live state")
    args = ap.parse_args()

    print("╔══════════════════════════════════════════════════╗")
    print("║   macOS Gesture Daemon  v8                       ║")
    print("╠══════════════════════════════════════════════════╣")
    print("║  Pinch (hold 0.25s)         →  Launchpad         ║")
    print("║  Open-palm Swipe ←/→        →  Cycle Apps (osa)  ║")
    print("║  Open-palm Swipe ↑          →  Mission Control   ║")
    print("║  Open-palm Swipe ↓          →  Show Desktop      ║")
    print("║  3-finger Swipe ←/→         →  Spaces (⌃←/⌃→)    ║")
    print("║  Fist (hold 1.2s)           →  Quit frontmost    ║")
    print("║  Index+Middle pinch (0.25s) →  App Switcher      ║")
    print("║  Pinky-only up (0.5s)       →  Screenshot        ║")
    print("║  OK sign (hold 0.5s)        →  Lock Screen       ║")
    print("╠══════════════════════════════════════════════════╣")
    if args.debug:
        print("║  Mode: DEBUG (window visible, press q to quit)   ║")
    else:
        print("║  Mode: HEADLESS (no window)                      ║")
    print("║  Ctrl+C to stop                                  ║")
    print("╚══════════════════════════════════════════════════╝\n")

    if not QUARTZ_OK:
        print("[!] No Quartz — install pyobjc-framework-Quartz\n")

    mp_hands = mp.solutions.hands
    hands = mp_hands.Hands(
        static_image_mode=False,
        max_num_hands=1,
        min_detection_confidence=0.75,
        min_tracking_confidence=0.70,
        model_complexity=0,
    )

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[!] Cannot open camera. Grant Camera permission to Terminal.")
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAM_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
    cap.set(cv2.CAP_PROP_FPS,          30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)

    proc = GestureProcessor()
    tick = 0
    _log("Watching for gestures...")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.01); continue

            tick += 1
            if tick % 2 != 0:
                continue

            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            res = hands.process(rgb)

            landmarks_msg = None
            if res.multi_hand_landmarks:
                landmarks_msg = res.multi_hand_landmarks[0]
                proc.update(landmarks_msg.landmark)
            else:
                proc.reset()

            if args.debug:
                vis = draw_debug(frame, proc, landmarks_msg)
                cv2.imshow("Gesture Daemon — DEBUG", vis)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

    except KeyboardInterrupt:
        print("\n[*] Stopped.")
    finally:
        cap.release()
        hands.close()
        if args.debug:
            cv2.destroyAllWindows()

if __name__ == "__main__":
    main()