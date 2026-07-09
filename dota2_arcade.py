import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


GAME_NAME = "DOTA2 IM 觉醒 (S11赛季)"
SEARCH_KEYWORD = "觉醒"


@dataclass
class DotaWindow:
    left: int
    top: int
    width: int
    height: int


class Dota2Automation:
    def __init__(self, log=print, pause_controller=None, manual_wait=lambda message: None, manual_resume=lambda message: None) -> None:
        self.log = log
        self.pause_controller = pause_controller
        self.manual_wait = manual_wait
        self.manual_resume = manual_resume
        self.window: DotaWindow | None = None
        self._ocr = None

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
        self.window = self.wait_for_window(timeout_seconds=180)
        self.focus()

    def wait_for_window(self, timeout_seconds: int = 120) -> DotaWindow:
        _, _, Desktop = self._import_gui()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
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
        time.sleep(delay)

    def screenshot(self):
        pyautogui, _, _ = self._import_gui()
        if not self.window:
            self.window = self.wait_for_window()
        return pyautogui.screenshot(region=(self.window.left, self.window.top, self.window.width, self.window.height))

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

    def click_text(self, patterns: list[str], timeout_seconds: int = 20, min_score: float = 0.45, delay: float = 1.0) -> bool:
        pyautogui, _, _ = self._import_gui()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self.focus()
            item = self.find_text(patterns, min_score=min_score)
            if item:
                if not self.window:
                    self.window = self.wait_for_window()
                pyautogui.click(int(self.window.left + item["cx"]), int(self.window.top + item["cy"]))
                time.sleep(delay)
                self.log(f"Clicked OCR text: {item['text']}")
                return True
            time.sleep(1)
        return False

    def text_visible(self, patterns: list[str], min_score: float = 0.45) -> bool:
        return self.find_text(patterns, min_score=min_score) is not None

    def press(self, key: str, delay: float = 0.5) -> None:
        pyautogui, _, _ = self._import_gui()
        self.focus()
        pyautogui.press(key)
        time.sleep(delay)

    def paste(self, text: str, delay: float = 0.5) -> None:
        pyautogui, pyperclip, _ = self._import_gui()
        self.focus()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(delay)

    def clear_popups_and_onboarding(self) -> None:
        self.log("Clearing Dota onboarding and reward popups.")
        self.click_text([r"我玩过Dota2", r"我玩过Dota", r"类似Dota", r"RTS"], timeout_seconds=8)
        for _ in range(8):
            clicked = self.click_text([r"接受", r"确定", r"继续", r"领取", r"关闭"], timeout_seconds=2)
            if not clicked:
                self.press("esc", delay=0.4)

    def open_arcade_and_game(self) -> None:
        self.log("Opening Arcade and searching custom game.")
        if not self.click_text([r"游廊"], timeout_seconds=15):
            self.click(0.62, 0.035, delay=3.0)
        if not self.click_text([r"搜索自定义游戏", r"搜索"], timeout_seconds=5):
            self.click(0.86, 0.082, delay=0.5)
        self.paste(GAME_NAME, delay=0.5)
        self.press("enter", delay=3.0)
        self.click_text([r"DOTA2IM觉醒", r"IM觉醒"], timeout_seconds=15)
        self.click_text([r"开始游戏", r"安装", r"进入"], timeout_seconds=10)

    def install_or_open_game(self) -> None:
        self.log("Installing/opening arcade game page.")
        self.click_text([r"安装", r"开始游戏"], timeout_seconds=20)
        time.sleep(8)
        self.press("esc", delay=1.0)

    def create_room(self, description: str) -> None:
        self.log(f"Creating custom lobby: {description}")
        if not self.click_text([r"房间列表"], timeout_seconds=10):
            self.click(0.36, 0.10, delay=1.5)
        if not self.click_text([r"创建自定义房间"], timeout_seconds=10):
            self.click(0.68, 0.955, delay=2.0)
        if not self.click_text([r"描述", r"非必填"], timeout_seconds=8):
            self.click(0.28, 0.53, delay=0.5)
        self.paste(description, delay=0.5)
        if not self.click_text([r"创建"], timeout_seconds=10):
            self.click(0.39, 0.90, delay=6.0)
        time.sleep(6)

    def search_room(self, description: str, refresh_count: int = 5) -> bool:
        self.log(f"Searching room list for description: {description}")
        if not self.click_text([r"房间列表"], timeout_seconds=10):
            self.click(0.36, 0.10, delay=1.0)
        if not self.click_text([r"搜索房间"], timeout_seconds=8):
            self.click(0.17, 0.955, delay=0.4)
        self.paste(SEARCH_KEYWORD, delay=0.4)
        self.press("enter", delay=1.0)
        patterns = [re.escape(description), r"DOTA2IM觉醒", r"IM觉醒"]
        for index in range(refresh_count):
            self.log(f"Refreshing room list {index + 1}/{refresh_count}.")
            if self.text_visible(patterns, min_score=0.40):
                return True
            if not self.click_text([r"刷新"], timeout_seconds=3):
                self.click(0.93, 0.20, delay=0.5)
            time.sleep(3)
            if self.text_visible(patterns, min_score=0.40):
                return True

        message = f"OCR 未确认房间描述 {description}。请目视确认，找到点“Dota 找到了”，没找到点“Dota 没找到”。"
        self.log(message)
        self.manual_wait(message)
        if self.pause_controller:
            decision = self.pause_controller.wait_decision({"dota_found", "dota_not_found", "stop"})
            self.manual_resume("")
            return decision == "dota_found"

        answer = input("Room found? y/N: ").strip().lower()
        self.manual_resume("")
        return answer == "y"

    def leave_room(self) -> None:
        self.log("Leaving current Dota lobby.")
        if not self.click_text([r"退出房间"], timeout_seconds=5):
            self.click(0.90, 0.86, delay=2.0)

    def run_check(self, steam_exe: Path | None, description: str, max_attempts: int = 3) -> bool:
        self.launch(steam_exe)
        self.clear_popups_and_onboarding()
        self.open_arcade_and_game()
        self.install_or_open_game()
        for attempt in range(1, max_attempts + 1):
            self.log(f"Dota lobby attempt {attempt}/{max_attempts}.")
            self.create_room(description)
            if self.search_room(description, refresh_count=5):
                self.log("Dota room confirmed.")
                return True
            self.leave_room()
        self.log("Dota room was not confirmed after max attempts.")
        return False
