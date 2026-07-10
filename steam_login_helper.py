import json
import subprocess
import sys
import time
from pathlib import Path


def log(message: str) -> None:
    print(message, flush=True)


def load_payload() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        raise RuntimeError("missing stdin JSON payload")
    return json.loads(raw)


def matching_windows(pyautogui, titles: list[str]):
    windows = []
    for title in titles:
        try:
            for window in pyautogui.getWindowsWithTitle(title):
                if window not in windows:
                    windows.append(window)
        except Exception as exc:
            log(f"getWindowsWithTitle({title!r}) failed: {exc}")
    return windows


def window_process_name(window) -> str:
    hwnd = getattr(window, "_hWnd", None) or getattr(window, "hWnd", None)
    if not hwnd:
        return ""
    try:
        import ctypes

        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid.value}", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
        output = (result.stdout or "").strip()
        if not output or "INFO:" in output.upper():
            return ""
        first = output.splitlines()[0].split(",", 1)[0].strip().strip('"')
        return first.casefold()
    except Exception as exc:
        log(f"steam helper: process lookup failed for {getattr(window, 'title', '')!r}: {exc}")
        return ""


def is_steam_process_window(window) -> bool:
    process_name = window_process_name(window)
    return process_name in {"steam.exe", "steamwebhelper.exe"}


def is_browser_or_webui_window(title: str) -> bool:
    lowered = title.casefold()
    blocked_markers = [
        "google chrome",
        "microsoft edge",
        "mozilla firefox",
        "steam outlook 自动化",
        "run_steam_web_ui",
        "快捷方式",
        "shortcut",
        "localhost",
        "127.0.0.1",
    ]
    return any(marker in lowered for marker in blocked_markers)


def is_login_window(title: str) -> bool:
    if not title or is_browser_or_webui_window(title):
        return False
    lowered = title.casefold()
    return any(
        marker in lowered
        for marker in [
            "登录 steam",
            "sign in to steam",
            "steam login",
        ]
    )


def has_login_prompt_text(window) -> bool:
    text = window_combined_text(window).casefold()
    markers = [
        "account name",
        "password",
        "sign in",
        "登录",
        "密码",
        "账户名称",
        "账号",
    ]
    return any(marker in text for marker in markers)


def is_login_target(window) -> bool:
    title = getattr(window, "title", "") or ""
    if not is_steam_process_window(window):
        return False
    if is_login_window(title):
        return True
    if is_browser_or_webui_window(title):
        return False
    return title.casefold() == "steam" and has_login_prompt_text(window)


def window_combined_text(window) -> str:
    title = getattr(window, "title", "") or ""
    hwnd = getattr(window, "_hWnd", None) or getattr(window, "hWnd", None)
    if not hwnd:
        return title
    try:
        from pywinauto import Desktop

        uia_window = Desktop(backend="uia").window(handle=int(hwnd))
        texts = [title]
        for item in uia_window.descendants()[:80]:
            try:
                item_text = item.window_text()
                if item_text:
                    texts.append(item_text)
            except Exception:
                pass
        return " ".join(texts)
    except Exception as exc:
        log(f"steam helper: UI text lookup failed for {title!r}: {exc}")
        return title


def text_snippet(text: str, limit: int = 160) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def save_window_screenshot(pyautogui, window, path: str, label: str) -> None:
    if not path or window is None:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        region = (int(window.left), int(window.top), int(window.width), int(window.height))
        pyautogui.screenshot(region=region).save(target)
        log(f"steam helper: {label} screenshot saved: {target}")
    except Exception as exc:
        log(f"steam helper: {label} screenshot save failed: {exc}")


STEAM_LOGIN_BLOCKING_MARKERS = [
    (
        "Steam account/password rejected",
        [
            "incorrect",
            "account name or password",
            "please check your password",
            "please check your account name",
            "sign in with your account name and password again",
            "密码错误",
            "密码不正确",
            "账户名称或密码",
            "账号或密码",
        ],
    ),
    (
        "Steam captcha or human verification required",
        [
            "captcha",
            "verify you are human",
            "not a robot",
            "human verification",
            "人机验证",
            "验证码",
            "验证你是真人",
        ],
    ),
    (
        "Steam mobile authenticator or QR approval required",
        [
            "steam mobile app",
            "mobile authenticator",
            "approve sign in",
            "scan the qr",
            "qr code",
            "use the steam mobile app",
            "移动验证器",
            "手机令牌",
            "手机应用",
            "批准登录",
            "扫描二维码",
            "二维码",
        ],
    ),
    (
        "Steam network or service error",
        [
            "network",
            "connection",
            "service error",
            "try again later",
            "网络",
            "连接",
            "服务错误",
            "稍后再试",
        ],
    ),
    (
        "Steam account locked or limited",
        [
            "locked",
            "limited",
            "too many",
            "temporarily unavailable",
            "锁定",
            "受限",
            "次数过多",
            "暂时不可用",
        ],
    ),
]


def classify_steam_login_blocking_text(text: str) -> str:
    lowered = (text or "").casefold()
    if has_guard_text_value(lowered):
        return ""
    account_password_form_visible = (
        any(marker in lowered for marker in ["account name", "帐户名称", "账户名称", "账号"])
        and any(marker in lowered for marker in ["password", "密码"])
    )
    for label, markers in STEAM_LOGIN_BLOCKING_MARKERS:
        if any(marker.casefold() in lowered for marker in markers):
            if label == "Steam mobile authenticator or QR approval required" and account_password_form_visible:
                # Steam's normal password form always advertises QR/mobile login
                # as an alternative. That is not evidence that mobile approval is required.
                continue
            return label
    return ""


def is_guard_window(title: str) -> bool:
    if not title or is_browser_or_webui_window(title):
        return False
    lowered = title.casefold()
    return any(
        marker in lowered
        for marker in [
            "steam guard",
            "验证码",
            "登录 steam",
            "sign in to steam",
        ]
    )


def is_guard_target(window) -> bool:
    title = getattr(window, "title", "") or ""
    return is_guard_window(title) and is_steam_process_window(window)


def has_guard_prompt_text(window) -> bool:
    text = window_combined_text(window).casefold()
    return has_guard_text_value(text)


def has_guard_text_value(text: str) -> bool:
    lowered = (text or "").casefold()
    strong_markers = [
        "steam guard",
        "email code",
        "sent to your email",
        "电子邮件地址",
        "获得的代码",
        "输入从您电子邮件地址",
    ]
    if any(marker in lowered for marker in strong_markers):
        return True
    code_markers = [
        "verification code",
        "enter the code",
        "输入代码",
        "输入验证码",
    ]
    context_markers = [
        "guard",
        "email",
        "mail",
        "电子邮件",
        "邮箱",
        "令牌",
    ]
    return any(marker in lowered for marker in code_markers) and any(marker in lowered for marker in context_markers)


def is_wait_guard_target(window) -> bool:
    title = getattr(window, "title", "") or ""
    if not is_steam_process_window(window):
        return False
    if is_browser_or_webui_window(title):
        return False
    lowered = title.casefold()
    if "steam guard" in lowered:
        return True
    return has_guard_prompt_text(window)


def text_contains_account(text: str, username: str) -> bool:
    expected = (username or "").strip().casefold()
    if not expected:
        return True
    compact_text = recompact_account_text(text)
    compact_expected = recompact_account_text(expected)
    return compact_expected in compact_text


def recompact_account_text(text: str) -> str:
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum() or ch in {"_", "-", "."})


def is_expected_wait_guard_target(window, username: str = "") -> bool:
    if not is_wait_guard_target(window):
        return False
    if not username:
        return True
    return text_contains_account(window_combined_text(window), username)


def paste_text(pyautogui, pyperclip, value: str) -> None:
    pyperclip.copy(value)
    pyautogui.hotkey("ctrl", "v")


def rect_center(rect) -> tuple[int, int]:
    return int((rect.left + rect.right) / 2), int((rect.top + rect.bottom) / 2)


def click_uia_center(pyautogui, item) -> None:
    x, y = rect_center(item.rectangle())
    pyautogui.click(x, y)


def steam_login_controls(window):
    hwnd = getattr(window, "_hWnd", None) or getattr(window, "hWnd", None)
    if not hwnd:
        return None
    try:
        from pywinauto import Desktop

        uia_window = Desktop(backend="uia").window(handle=int(hwnd))
        edits = []
        login_button = None
        for item in uia_window.descendants()[:120]:
            try:
                control_type = item.element_info.control_type
                text = item.window_text()
                if control_type == "Edit":
                    edits.append(item)
                elif control_type == "Button" and text in {"登录", "Sign in", "Login"}:
                    login_button = item
            except Exception:
                pass
        if len(edits) >= 2 and login_button:
            return edits[0], edits[1], login_button
    except Exception as exc:
        log(f"steam helper: login controls lookup failed for {getattr(window, 'title', '')!r}: {exc}")
    return None


def click_password_sign_in_option(pyautogui, window) -> bool:
    hwnd = getattr(window, "_hWnd", None) or getattr(window, "hWnd", None)
    if not hwnd:
        return False
    patterns = [
        "sign in with account name",
        "use account name",
        "enter account name",
        "account name",
        "password",
        "使用帐户名称登录",
        "使用账户名称登录",
        "使用账号登录",
        "账号密码",
        "帐户名称",
        "账户名称",
    ]
    try:
        from pywinauto import Desktop

        uia_window = Desktop(backend="uia").window(handle=int(hwnd))
        for item in uia_window.descendants()[:160]:
            try:
                text = (item.window_text() or "").casefold()
                control_type = item.element_info.control_type
                if control_type not in {"Button", "Hyperlink", "Text"}:
                    continue
                if not text or not any(pattern.casefold() in text for pattern in patterns):
                    continue
                rect = item.rectangle()
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                click_uia_center(pyautogui, item)
                log(f"steam helper: clicked account/password sign-in option: {item.window_text()!r}")
                time.sleep(1)
                return True
            except Exception:
                pass
    except Exception as exc:
        log(f"steam helper: account/password option lookup failed for {getattr(window, 'title', '')!r}: {exc}")
    return False


def submit_login_with_uia_controls(pyautogui, pyperclip, window, username: str, password: str) -> bool:
    controls = steam_login_controls(window)
    if not controls and click_password_sign_in_option(pyautogui, window):
        controls = steam_login_controls(window)
    if not controls:
        return False
    user_edit, password_edit, login_button = controls

    click_uia_center(pyautogui, user_edit)
    pyautogui.hotkey("ctrl", "a")
    paste_text(pyautogui, pyperclip, username)
    log("steam helper: username pasted via UIA field")

    click_uia_center(pyautogui, password_edit)
    pyautogui.hotkey("ctrl", "a")
    paste_text(pyautogui, pyperclip, password)
    log("steam helper: password pasted via UIA field")

    click_uia_center(pyautogui, login_button)
    log("steam helper: login clicked via UIA button")
    return True


def submit_login(payload: dict) -> None:
    log("steam helper: importing GUI modules for login")
    import pyautogui
    import pyperclip

    username = payload["username"]
    password = payload["password"]
    timeout_seconds = int(payload.get("timeout_seconds") or 60)
    screenshot_path = payload.get("screenshot_path") or ""
    deadline = time.monotonic() + timeout_seconds
    next_log = 0.0
    last_error = ""
    last_window = None

    log("steam helper: login mode started")
    while time.monotonic() < deadline:
        windows = matching_windows(pyautogui, ["登录 Steam", "Sign in to Steam", "Steam Login", "Steam"])
        now = time.monotonic()
        if now >= next_log:
            log(f"steam helper: windows={[getattr(w, 'title', '') for w in windows] or 'none'}")
            next_log = now + 3

        for window in windows:
            title = getattr(window, "title", "") or ""
            if not is_login_target(window):
                log(f"steam helper: skipped non-Steam login window={title!r} process={window_process_name(window)!r}")
                continue
            last_window = window
            try:
                if getattr(window, "isMinimized", False):
                    window.restore()
                window.activate()
                time.sleep(0.5)
                left, top, width, height = int(window.left), int(window.top), int(window.width), int(window.height)
                log(f"steam helper: active window={title!r} rect=({left},{top},{width},{height}) user={username}")

                if submit_login_with_uia_controls(pyautogui, pyperclip, window, username, password):
                    return
                current_text = window_combined_text(window)
                state = (
                    f"window={title!r} process={window_process_name(window)!r} "
                    f"text={text_snippet(current_text)!r}"
                )
                blocking_label = classify_steam_login_blocking_text(current_text)
                if blocking_label:
                    save_window_screenshot(pyautogui, window, screenshot_path, "login blocked")
                    raise RuntimeError(f"steam helper: login blocked before credential submit: {blocking_label}; {state}")
                last_error = f"Steam login UIA controls not ready; {state}"
                log(f"steam helper: login UIA controls not ready; retrying; {state}")
            except Exception as exc:
                last_error = str(exc)
                log(f"steam helper: input failed: {exc}")
        time.sleep(1)

    save_window_screenshot(pyautogui, last_window, screenshot_path, "login submit timeout")
    raise TimeoutError(f"steam helper: login submit timeout; last_error={last_error or 'no matching window'}")


def submit_guard(payload: dict) -> None:
    log("steam helper: importing GUI modules for guard")
    import pyautogui
    import pyperclip

    code = payload["code"]
    username = payload.get("username") or ""
    timeout_seconds = int(payload.get("timeout_seconds") or 90)
    screenshot_path = payload.get("screenshot_path") or ""
    deadline = time.monotonic() + timeout_seconds
    next_log = 0.0
    last_error = ""
    last_window = None

    log("steam helper: guard mode started")
    while time.monotonic() < deadline:
        windows = matching_windows(pyautogui, ["Steam Guard", "验证码", "登录 Steam", "Sign in to Steam", "Steam"])
        now = time.monotonic()
        if now >= next_log:
            log(f"steam helper: guard windows={[getattr(w, 'title', '') for w in windows] or 'none'}")
            next_log = now + 3

        for window in windows:
            title = getattr(window, "title", "") or ""
            text = window_combined_text(window)
            last_window = window
            if not is_expected_wait_guard_target(window, username):
                log(
                    f"steam helper: skipped Guard submit candidate window={title!r} "
                    f"process={window_process_name(window)!r} text={text_snippet(text)!r}"
                )
                continue
            try:
                if getattr(window, "isMinimized", False):
                    window.restore()
                window.activate()
                time.sleep(0.3)
                pyperclip.copy(code)
                pyautogui.hotkey("ctrl", "a")
                pyautogui.hotkey("ctrl", "v")
                pyautogui.press("enter")
                log(f"steam helper: guard submitted in {title!r}")
                return
            except Exception as exc:
                last_error = str(exc)
                log(f"steam helper: guard input failed: {exc}")
        time.sleep(1)

    save_window_screenshot(pyautogui, last_window, screenshot_path, "guard submit timeout")
    raise TimeoutError(f"steam helper: guard submit timeout; last_error={last_error or 'no matching window'}")


def wait_guard_prompt(payload: dict) -> None:
    log("steam helper: wait guard mode started")
    import pyautogui

    username = payload.get("username") or ""
    timeout_seconds = int(payload.get("timeout_seconds") or 45)
    screenshot_path = payload.get("screenshot_path") or ""
    deadline = time.monotonic() + timeout_seconds
    next_log = 0.0
    last_seen = ""
    last_window = None

    while time.monotonic() < deadline:
        windows = matching_windows(pyautogui, ["Steam Guard", "验证码", "登录 Steam", "Sign in to Steam", "Steam"])
        now = time.monotonic()
        if now >= next_log:
            log(f"steam helper: wait guard windows={[getattr(w, 'title', '') for w in windows] or 'none'}")
            next_log = now + 3
        for window in windows:
            title = getattr(window, "title", "") or ""
            text = window_combined_text(window)
            last_window = window
            if is_expected_wait_guard_target(window, username):
                log(f"steam helper: guard prompt confirmed in {title!r} for user={username or 'any'}")
                return
            last_seen = f"window={title!r} process={window_process_name(window)!r} text={text_snippet(text)!r}"
            blocking_label = classify_steam_login_blocking_text(text)
            if blocking_label:
                save_window_screenshot(pyautogui, window, screenshot_path, "wait guard blocked")
                raise RuntimeError(
                    f"steam helper: login blocked before Guard prompt: {blocking_label}; text={text_snippet(text)!r}"
                )
            log(
                f"steam helper: skipped wait-guard candidate window={title!r} "
                f"process={window_process_name(window)!r} text={text_snippet(text)!r}"
            )
        time.sleep(1)

    save_window_screenshot(pyautogui, last_window, screenshot_path, "wait guard timeout")
    raise TimeoutError(
        "steam helper: guard prompt not confirmed in a Steam process window; "
        f"last_seen={last_seen or 'no Steam login/Guard candidate window'}"
    )


def main() -> int:
    try:
        payload = load_payload()
        mode = payload.get("mode")
        if mode == "login":
            submit_login(payload)
        elif mode == "guard":
            submit_guard(payload)
        elif mode == "wait_guard":
            wait_guard_prompt(payload)
        else:
            raise RuntimeError(f"unknown mode: {mode}")
        return 0
    except Exception as exc:
        log(f"steam helper: fatal: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
