import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


GAME_NAME = "DOTA2 IM 觉醒 (S11赛季)"
SEARCH_KEYWORD = "觉醒"


DOTA_BLOCKING_PAGE_MARKERS = [
    (
        "自定义游戏权限受限",
        [
            "restricted from playing custom games",
            "restricted from creating custom games",
            "cannot create a custom game",
            "无法创建自定义游戏",
            "无法游玩自定义游戏",
            "自定义游戏受限",
            "受限制",
        ],
    ),
    (
        "Dota2 协调器或网络暂时不可用",
        [
            "connected to the dota 2 game coordinator",
            "dota 2 game coordinator",
            "coordinator is being updated",
            "network is offline",
            "reconnecting",
            "无法连接到 dota 2 游戏协调服务器",
            "游戏协调服务器",
            "正在重新连接",
            "网络离线",
        ],
    ),
    (
        "自定义游戏需要更新、下载或版本不匹配",
        [
            "custom game archive does not match",
            "custom game needs to be updated",
            "downloading custom game",
            "download queued",
            "update required",
            "版本不匹配",
            "需要更新",
            "正在下载自定义游戏",
            "下载队列",
        ],
    ),
    (
        "Dota2 客户端需要确认弹窗或接受协议",
        [
            "我同意",
            "i agree",
            "accept agreement",
            "confirm your age",
            "需要确认",
            "接受协议",
        ],
    ),
]


def classify_dota_blocking_text(text: str) -> str:
    lowered = (text or "").casefold()
    for label, markers in DOTA_BLOCKING_PAGE_MARKERS:
        if any(marker.casefold() in lowered for marker in markers):
            return label
    return ""


@dataclass
class DotaWindow:
    left: int
    top: int
    width: int
    height: int


class Dota2Automation:
    def __init__(
        self,
        log=print,
        pause_controller=None,
        manual_wait=lambda message: None,
        manual_resume=lambda message: None,
        screenshot_path: Path | None = None,
    ) -> None:
        self.log = log
        self.pause_controller = pause_controller
        self.manual_wait = manual_wait
        self.manual_resume = manual_resume
        self.window: DotaWindow | None = None
        self._ocr = None
        self.last_screenshot_path = screenshot_path or Path("errors") / "dota_last_screen.png"

    def _import_gui(self):
        try:
            import pyautogui
            import pyperclip
            from pywinauto import Desktop
        except ImportError as exc:
            raise RuntimeError("Missing GUI dependencies. Run: python -m pip install -r requirements.txt") from exc
        pyautogui.FAILSAFE = False
        return pyautogui, pyperclip, Desktop

    def _import_ocr(self):
        if self._ocr:
            return self._ocr
        try:
            import cv2
            import numpy as np
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError("Missing OCR dependencies. Run: python -m pip install -r requirements.txt") from exc
        self._ocr = (RapidOCR(), cv2, np)
        return self._ocr

    def launch(self, steam_exe: Path | None = None) -> None:
        self.log("Launching Dota 2.")
        try:
            os.startfile("steam://rungameid/570")
        except Exception:
            if steam_exe:
                subprocess.Popen([str(steam_exe), "-applaunch", "570"], cwd=str(steam_exe.parent))
            else:
                raise
        try:
            self.window = self.wait_for_window(timeout_seconds=180)
        except TimeoutError:
            self.wait_for_launch_recovery()
            self.window = self.wait_for_window(timeout_seconds=180)
        self.focus()

    def wait_for_launch_recovery(self) -> None:
        message = (
            "Dota2 启动后没有找到可用窗口。\n"
            "请手动打开或切到 Dota2，确认主窗口已加载完成且不是最小化状态。"
            "处理完成后点击 Web UI 的“已完成人工操作 / 继续”；要中止点击“停止”。"
        )
        self.log(message)
        self.manual_wait(message)
        if self.pause_controller:
            if not self.pause_controller.wait():
                self.manual_resume("")
                raise RuntimeError("Stopped during Dota launch recovery.")
        else:
            input("Open Dota 2, then press Enter to continue...")
        self.manual_resume("")

    def wait_for_window(self, timeout_seconds: int = 120) -> DotaWindow:
        _, _, Desktop = self._import_gui()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self.raise_if_stopped("waiting for Dota 2 window")
            try:
                windows = Desktop(backend="uia").windows(title_re=r".*(Dota 2|DOTA 2).*")
                for window in windows:
                    rect = window.rectangle()
                    if rect.width() >= 1000 and rect.height() >= 700:
                        return DotaWindow(rect.left, rect.top, rect.width(), rect.height())
            except Exception:
                pass
            time.sleep(2)
        raise TimeoutError("Dota 2 window not found.")

    def focus(self) -> None:
        _, _, Desktop = self._import_gui()
        try:
            windows = Desktop(backend="uia").windows(title_re=r".*(Dota 2|DOTA 2).*")
            if windows:
                windows[0].set_focus()
                time.sleep(0.5)
        except Exception:
            pass

    def xy(self, rx: float, ry: float) -> tuple[int, int]:
        if not self.window:
            self.window = self.wait_for_window()
        return int(self.window.left + self.window.width * rx), int(self.window.top + self.window.height * ry)

    def click(self, rx: float, ry: float, delay: float = 1.0) -> None:
        pyautogui, _, _ = self._import_gui()
        self.focus()
        pyautogui.click(*self.xy(rx, ry))
        self.sleep_with_stop(delay, f"after coordinate click {rx:.2f},{ry:.2f}")

    def screenshot(self):
        pyautogui, _, _ = self._import_gui()
        if not self.window:
            self.window = self.wait_for_window()
        return pyautogui.screenshot(region=(self.window.left, self.window.top, self.window.width, self.window.height))

    def save_debug_screenshot(self, reason: str) -> None:
        self.save_debug_screenshot_to(self.last_screenshot_path, reason)

    def save_debug_screenshot_to(self, path: Path, reason: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.screenshot().save(path)
            self.log(f"Dota screenshot saved for {reason}: {path}")
        except Exception as exc:
            self.log(f"Dota screenshot save failed for {reason}: {exc}")

    def ocr_items(self) -> list[dict]:
        engine, cv2, np = self._import_ocr()
        image = self.screenshot()
        arr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        result, _ = engine(arr)
        items = []
        if not result:
            return items
        for box, text, score in result:
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            items.append(
                {
                    "text": str(text),
                    "score": float(score),
                    "left": min(xs),
                    "top": min(ys),
                    "right": max(xs),
                    "bottom": max(ys),
                    "cx": sum(xs) / 4,
                    "cy": sum(ys) / 4,
                }
            )
        return items

    def find_text(self, patterns: list[str], min_score: float = 0.45) -> dict | None:
        compiled = [re.compile(pattern, re.I) for pattern in patterns]
        for item in self.ocr_items():
            if item["score"] < min_score:
                continue
            normalized = item["text"].replace(" ", "")
            if any(pattern.search(normalized) for pattern in compiled):
                return item
        return None

    def ocr_summary(self, limit: int = 12) -> str:
        try:
            items = sorted(self.ocr_items(), key=lambda item: item["score"], reverse=True)[:limit]
        except Exception as exc:
            return f"OCR failed: {exc}"
        if not items:
            return "OCR found no text"
        return " | ".join(f"{item['text']}({item['score']:.2f})" for item in items)

    def detect_blocking_state(self) -> tuple[str, str]:
        summary = self.ocr_summary(limit=18)
        label = classify_dota_blocking_text(summary)
        if label:
            return label, summary
        return "", summary

    def click_text(self, patterns: list[str], timeout_seconds: int = 20, min_score: float = 0.45, delay: float = 1.0) -> bool:
        pyautogui, _, _ = self._import_gui()
        deadline = time.monotonic() + timeout_seconds
        next_log = 0.0
        while time.monotonic() < deadline:
            self.raise_if_stopped(f"OCR wait for {patterns}")
            self.focus()
            item = self.find_text(patterns, min_score=min_score)
            if item:
                if not self.window:
                    self.window = self.wait_for_window()
                pyautogui.click(int(self.window.left + item["cx"]), int(self.window.top + item["cy"]))
                self.sleep_with_stop(delay, f"after clicking OCR text {item['text']}")
                self.log(f"Clicked OCR text: {item['text']}")
                return True
            now = time.monotonic()
            if now >= next_log:
                label, summary = self.detect_blocking_state()
                if label:
                    self.log(f"Dota OCR saw blocking state while waiting for {patterns}: {label}; visible={summary}")
                else:
                    self.log(f"Dota OCR waiting for {patterns}; visible={summary}")
                next_log = now + 5
            self.sleep_with_stop(1, f"OCR retry for {patterns}")
        self.log(f"Dota OCR text not found: patterns={patterns}; final_visible={self.ocr_summary(limit=12)}")
        return False

    def text_visible(self, patterns: list[str], min_score: float = 0.45) -> bool:
        return self.find_text(patterns, min_score=min_score) is not None

    def press(self, key: str, delay: float = 0.5) -> None:
        pyautogui, _, _ = self._import_gui()
        self.focus()
        pyautogui.press(key)
        self.sleep_with_stop(delay, f"after pressing {key}")

    def paste(self, text: str, delay: float = 0.5) -> None:
        pyautogui, pyperclip, _ = self._import_gui()
        self.focus()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        self.sleep_with_stop(delay, "after paste")

    def raise_if_stopped(self, step: str) -> None:
        if self.pause_controller and self.pause_controller.should_stop():
            raise RuntimeError(f"Stopped during Dota automation: {step}")

    def sleep_with_stop(self, seconds: float, step: str) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            self.raise_if_stopped(step)
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def wait_for_manual_recovery(self, step: str, instruction: str) -> None:
        before_path = self.last_screenshot_path
        after_path = before_path.with_name(f"{before_path.stem}_after{before_path.suffix}")
        self.save_debug_screenshot_to(before_path, step)
        message = (
            f"Dota2 自动化在“{step}”没有识别到预期界面。\n"
            f"当前 OCR：{self.ocr_summary(limit=12)}\n"
            f"{instruction}\n"
            "处理完成后点击 Web UI 的“已完成人工操作 / 继续”；要中止点击“停止”。"
        )
        self.log(message)
        self.manual_wait(message)
        if self.pause_controller:
            if not self.pause_controller.wait():
                self.manual_resume("")
                raise RuntimeError(f"Stopped during Dota manual recovery: {step}")
        else:
            input("Finish Dota manual recovery, then press Enter to continue...")
        self.log(f"Dota manual recovery confirmed for {step}; OCR after recovery: {self.ocr_summary(limit=12)}")
        self.save_debug_screenshot_to(after_path, f"{step} after manual recovery")
        self.manual_resume("")

    def recover_if_blocked(self, step: str) -> bool:
        label, summary = self.detect_blocking_state()
        if not label:
            return False
        self.wait_for_manual_recovery(
            f"{step}: {label}",
            f"当前 Dota2 页面显示“{label}”。请处理该弹窗/更新/限制提示，直到可以继续创建或搜索自定义房间。OCR：{summary}",
        )
        return True

    def clear_popups_and_onboarding(self) -> None:
        self.log("Clearing Dota onboarding and reward popups.")
        self.recover_if_blocked("startup")
        self.click_text([r"我玩过Dota2", r"我玩过Dota", r"类似Dota", r"RTS"], timeout_seconds=8)
        for _ in range(8):
            clicked = self.click_text([r"接受", r"确定", r"继续", r"领取", r"关闭"], timeout_seconds=2)
            if not clicked:
                self.press("esc", delay=0.4)

    def open_arcade_and_game(self) -> None:
        self.log("Opening Arcade and searching custom game.")
        self.recover_if_blocked("open arcade")
        if not self.click_text([r"游廊"], timeout_seconds=15):
            if self.recover_if_blocked("opening Arcade tab"):
                return self.open_arcade_and_game()
            self.wait_for_manual_recovery("Arcade tab not verified", "请手动打开 Dota2 的游廊页面。")
        if not self.click_text([r"搜索自定义游戏", r"搜索"], timeout_seconds=5):
            if self.recover_if_blocked("opening custom-game search"):
                return self.open_arcade_and_game()
            self.wait_for_manual_recovery("custom-game search not verified", "请手动聚焦游廊的自定义游戏搜索框。")
        self.paste(GAME_NAME, delay=0.5)
        self.press("enter", delay=3.0)
        if not self.click_text([r"DOTA2IM觉醒", r"IM觉醒"], timeout_seconds=15):
            self.recover_if_blocked("arcade game search")
            self.wait_for_manual_recovery(
                "arcade game search result not found",
                f"请手动在 Dota2 游廊中搜索并打开自定义游戏：{GAME_NAME}。",
            )
        if not self.click_text([r"开始游戏", r"安装", r"进入"], timeout_seconds=10):
            self.recover_if_blocked("arcade game open")
            self.wait_for_manual_recovery(
                "arcade game open button not found",
                "请手动点击该自定义游戏的“开始游戏 / 安装 / 进入”，直到进入房间列表或可创建房间的页面。",
            )

    def install_or_open_game(self) -> None:
        self.log("Waiting for the selected arcade game page to finish opening.")
        self.sleep_with_stop(8, "waiting after opening arcade game")
        self.recover_if_blocked("waiting for arcade game page")
        self.press("esc", delay=1.0)

    def create_room(self, description: str) -> None:
        self.log(f"Creating custom lobby: {description}")
        self.recover_if_blocked("create lobby")
        if not self.click_text([r"房间列表"], timeout_seconds=10):
            if self.recover_if_blocked("opening room list for create"):
                return self.create_room(description)
            self.wait_for_manual_recovery("room list not verified for create", "请手动打开当前自定义游戏的房间列表。")
        if not self.click_text([r"创建自定义房间"], timeout_seconds=10):
            if self.recover_if_blocked("creating custom lobby"):
                return self.create_room(description)
            self.wait_for_manual_recovery("create-lobby button not verified", "请手动点击“创建自定义房间”。")
        if not self.click_text([r"描述", r"非必填"], timeout_seconds=8):
            self.wait_for_manual_recovery("lobby description field not verified", "请手动聚焦房间描述输入框。")
        self.paste(description, delay=0.5)
        if not self.click_text([r"创建"], timeout_seconds=10):
            self.wait_for_manual_recovery(
                "final create action not verified",
                f"请确认房间描述为 {description}，然后手动点击最终的“创建”按钮。",
            )
        self.sleep_with_stop(6, "waiting after custom lobby creation")

    def search_room(self, description: str, refresh_count: int = 5) -> bool:
        self.log(f"Searching room list for description: {description}")
        self.recover_if_blocked("search lobby")
        if not self.click_text([r"房间列表"], timeout_seconds=10):
            if self.recover_if_blocked("opening room list for search"):
                return self.search_room(description, refresh_count=refresh_count)
            self.wait_for_manual_recovery("room list not verified for search", "请手动打开当前自定义游戏的房间列表。")
        if not self.click_text([r"搜索房间"], timeout_seconds=8):
            self.wait_for_manual_recovery("room search field not verified", "请手动聚焦房间搜索框。")
        self.paste(SEARCH_KEYWORD, delay=0.4)
        self.press("enter", delay=1.0)
        patterns = [re.escape(description)]
        for index in range(refresh_count):
            self.raise_if_stopped(f"before room list refresh {index + 1}")
            self.log(f"Refreshing room list {index + 1}/{refresh_count}.")
            if self.text_visible(patterns, min_score=0.40):
                self.log(f"Dota room text visible before refresh: {description}")
                return True
            if not self.click_text([r"刷新"], timeout_seconds=3):
                self.wait_for_manual_recovery(
                    "room-list refresh not verified",
                    "请手动点击房间列表的刷新按钮，并确认仍停留在房间列表。",
                )
            self.sleep_with_stop(3, f"waiting after room list refresh {index + 1}")
            if self.text_visible(patterns, min_score=0.40):
                self.log(f"Dota room text visible after refresh: {description}")
                return True

        self.save_debug_screenshot("room existence manual confirmation")
        message = f"OCR 未确认房间描述 {description}。请目视确认房间列表：房间存在点“Dota2 检测：房间存在，标记大号”；房间不存在点“Dota2 检测：房间不存在，不标记大号”。"
        self.log(message)
        self.manual_wait(message)
        if self.pause_controller:
            decision = self.pause_controller.wait_decision({"dota_found", "dota_not_found", "stop"})
            self.manual_resume("")
            if decision == "stop":
                raise RuntimeError("Stopped during Dota room existence confirmation.")
            after_path = self.last_screenshot_path.with_name(f"{self.last_screenshot_path.stem}_room_decision_after{self.last_screenshot_path.suffix}")
            self.log(f"Dota room manual decision: {decision}; OCR after decision: {self.ocr_summary(limit=12)}")
            self.save_debug_screenshot_to(after_path, f"room existence manual decision {decision}")
            return decision == "dota_found"

        answer = input("Room found? y/N: ").strip().lower()
        self.manual_resume("")
        after_path = self.last_screenshot_path.with_name(f"{self.last_screenshot_path.stem}_room_decision_after{self.last_screenshot_path.suffix}")
        self.log(f"Dota room manual console decision: {answer}; OCR after decision: {self.ocr_summary(limit=12)}")
        self.save_debug_screenshot_to(after_path, f"room existence manual console decision {answer}")
        return answer == "y"

    def leave_room(self) -> None:
        self.log("Leaving current Dota lobby.")
        if not self.click_text([r"退出房间"], timeout_seconds=5):
            self.wait_for_manual_recovery("leave-lobby action not verified", "请手动退出当前自定义房间。")

    def run_check(self, steam_exe: Path | None, description: str, max_attempts: int = 3) -> bool:
        self.raise_if_stopped("before launch")
        self.launch(steam_exe)
        self.raise_if_stopped("after launch")
        self.clear_popups_and_onboarding()
        self.raise_if_stopped("after popup cleanup")
        self.open_arcade_and_game()
        self.raise_if_stopped("after arcade open")
        self.install_or_open_game()
        for attempt in range(1, max_attempts + 1):
            self.raise_if_stopped(f"before lobby attempt {attempt}")
            self.log(f"Dota lobby attempt {attempt}/{max_attempts}.")
            self.create_room(description)
            self.raise_if_stopped(f"after lobby creation attempt {attempt}")
            if self.search_room(description, refresh_count=5):
                self.log("Dota room confirmed.")
                return True
            self.leave_room()
        self.log("Dota room was not confirmed after max attempts.")
        return False
