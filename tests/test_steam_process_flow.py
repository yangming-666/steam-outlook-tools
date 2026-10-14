import subprocess
import unittest
import inspect
import argparse
import json
import asyncio
import time
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import steam_login_helper
import steam_outlook_batch
import steam_web_ui
import dota2_arcade
import outlook_batch_login


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class SteamProcessFlowTests(unittest.TestCase):
    def test_email_scoped_outlook_profiles_are_stable_and_session_marked(self):
        class FakeContext:
            def __init__(self):
                self.cookies = []

            async def storage_state(self, path):
                Path(path).write_text(json.dumps({"cookies": [{"name": "session", "value": "token", "domain": ".example.com", "path": "/"}]}), encoding="utf-8")

            async def add_cookies(self, cookies):
                self.cookies.extend(cookies)

        with TemporaryDirectory() as folder:
            with patch.object(steam_outlook_batch, "OUTLOOK_SESSION_ROOT", Path(folder)):
                first = steam_outlook_batch.email_scoped_outlook_profile("target", "User@Example.com")
                second = steam_outlook_batch.email_scoped_outlook_profile("target", "user@example.com")
                security = steam_outlook_batch.email_scoped_outlook_profile("security", "user@example.com")
                self.assertEqual(first, second)
                self.assertNotEqual(first, security)
                context = FakeContext()
                asyncio.run(steam_outlook_batch.persist_email_scoped_outlook_session(context, "target", "User@Example.com"))
                marker = steam_outlook_batch.email_scoped_session_marker("target", "user@example.com")
                self.assertEqual("user@example.com", marker.read_text(encoding="utf-8"))
                restored = FakeContext()
                self.assertTrue(asyncio.run(steam_outlook_batch.restore_email_scoped_outlook_session(restored, "target", "user@example.com")))
                self.assertEqual("session", restored.cookies[0]["name"])
                steam_outlook_batch.clear_email_scoped_outlook_session("target", "user@example.com")
                self.assertFalse(marker.exists())
                self.assertFalse(steam_outlook_batch.email_scoped_storage_state("target", "user@example.com").exists())

    def test_extract_steam_guard_code_ignores_empty_placeholder(self):
        self.assertIsNone(steam_outlook_batch.extract_steam_guard_code("Steam Guard code found: EMPTY"))
        self.assertIsNone(steam_outlook_batch.extract_steam_guard_code("Steam Guard code found: FOUND"))
        self.assertIsNone(steam_outlook_batch.extract_steam_guard_code("Steam page contains ABCDE but no fixed mail format"))
        self.assertIsNone(steam_outlook_batch.extract_steam_guard_code("Your Steam code is STORE"))
        self.assertIsNone(steam_outlook_batch.extract_steam_guard_code("Your Steam code is Z9Y8X"))
        self.assertIsNone(steam_outlook_batch.extract_steam_guard_code("Steam Guard code you need to login SUPPORT"))
        self.assertEqual(
            "A1B2C",
            steam_outlook_batch.extract_steam_guard_code(
                "Here's the Steam Guard code you need to login to account user: A1B2C"
            ),
        )
        self.assertEqual(
            "Z9Y8X",
            steam_outlook_batch.extract_steam_guard_code("Steam Guard 登录验证码：Z9Y8X"),
        )

    def test_send_guard_code_rejects_invalid_placeholder(self):
        logs = []

        with patch.object(steam_outlook_batch, "run_steam_helper") as helper:
            ok = steam_outlook_batch.send_guard_code_to_steam("EMPTY", 10, logs.append)

        self.assertFalse(ok)
        helper.assert_not_called()
        self.assertTrue(any("invalid code" in line for line in logs))

    def test_steam_helper_window_filters_exclude_web_ui_chrome(self):
        self.assertFalse(steam_login_helper.is_login_window("Steam Outlook 自动化 - Google Chrome"))
        self.assertFalse(steam_login_helper.is_guard_window("run_steam_web_ui - 快捷方式"))
        self.assertTrue(steam_login_helper.is_login_window("登录 Steam"))
        self.assertTrue(steam_login_helper.is_guard_window("Steam Guard"))

    def test_steam_helper_targets_must_be_steam_process_windows(self):
        class FakeWindow:
            title = "登录 Steam"

        with patch.object(steam_login_helper, "window_process_name", return_value="chrome.exe"):
            self.assertFalse(steam_login_helper.is_login_target(FakeWindow()))
            self.assertFalse(steam_login_helper.is_guard_target(FakeWindow()))

        with patch.object(steam_login_helper, "window_process_name", return_value="steam.exe"):
            self.assertTrue(steam_login_helper.is_login_target(FakeWindow()))

    def test_login_target_accepts_plain_steam_title_only_with_login_text(self):
        class FakeWindow:
            title = "Steam"

        with patch.object(steam_login_helper, "window_process_name", return_value="steam.exe"), patch.object(
            steam_login_helper, "window_combined_text", return_value="Steam store library"
        ):
            self.assertFalse(steam_login_helper.is_login_target(FakeWindow()))

        with patch.object(steam_login_helper, "window_process_name", return_value="steamwebhelper.exe"), patch.object(
            steam_login_helper, "window_combined_text", return_value="Steam account name password sign in"
        ):
            self.assertTrue(steam_login_helper.is_login_target(FakeWindow()))

    def test_login_submit_requires_uia_controls_without_coordinate_fallback(self):
        source = inspect.getsource(steam_login_helper.submit_login)
        submit_controls_source = inspect.getsource(steam_login_helper.submit_login_with_uia_controls)

        self.assertIn("submit_login_with_uia_controls", source)
        self.assertIn("click_password_sign_in_option", submit_controls_source)
        self.assertIn("login UIA controls not ready; retrying", source)
        self.assertIn("login blocked before credential submit", source)
        self.assertIn("window_combined_text", source)
        self.assertIn("text_snippet", source)
        self.assertIn("last_error =", source)
        self.assertNotIn("user_x =", source)
        self.assertNotIn("login_x =", source)

    def test_steam_helper_can_switch_from_qr_to_account_password_login(self):
        source = inspect.getsource(steam_login_helper.click_password_sign_in_option)

        self.assertIn("sign in with account name", source)
        self.assertIn("use account name", source)
        self.assertIn("使用账号登录", source)
        self.assertIn("Button", source)
        self.assertIn("Hyperlink", source)
        self.assertIn("clicked account/password sign-in option", source)

    def test_steam_login_controls_require_two_edits_and_login_button(self):
        source = inspect.getsource(steam_login_helper.steam_login_controls)

        self.assertIn('control_type == "Edit"', source)
        self.assertIn('control_type == "Button"', source)
        self.assertIn("len(edits) >= 2", source)
        self.assertIn("login_button", source)

    def test_wait_guard_requires_guard_prompt_text(self):
        class FakeWindow:
            title = "登录 Steam"

        with patch.object(steam_login_helper, "window_process_name", return_value="steam.exe"), patch.object(
            steam_login_helper, "window_combined_text", return_value="登录 Steam account password"
        ):
            self.assertFalse(steam_login_helper.is_wait_guard_target(FakeWindow()))

        with patch.object(steam_login_helper, "window_process_name", return_value="steam.exe"), patch.object(
            steam_login_helper, "window_combined_text", return_value="登录 Steam Steam Guard enter the code sent to your email"
        ):
            self.assertTrue(steam_login_helper.is_wait_guard_target(FakeWindow()))

        with patch.object(steam_login_helper, "window_process_name", return_value="steamwebhelper.exe"), patch.object(
            steam_login_helper,
            "window_combined_text",
            return_value="登录 Steam 帐户：faku_lp 输入从您电子邮件地址 hotmail.com 中获得的代码",
        ):
            self.assertTrue(steam_login_helper.is_wait_guard_target(FakeWindow()))

    def test_wait_guard_does_not_accept_generic_captcha_text(self):
        class FakeWindow:
            title = "登录 Steam"

        with patch.object(steam_login_helper, "window_process_name", return_value="steamwebhelper.exe"), patch.object(
            steam_login_helper,
            "window_combined_text",
            return_value="登录 Steam 请输入验证码 验证你是真人 captcha",
        ):
            self.assertFalse(steam_login_helper.is_wait_guard_target(FakeWindow()))
            self.assertEqual(
                "Steam captcha or human verification required",
                steam_login_helper.classify_steam_login_blocking_text("登录 Steam 请输入验证码 验证你是真人 captcha"),
            )

        self.assertFalse(steam_login_helper.has_guard_text_value("验证码 captcha 人机验证"))

    def test_wait_guard_accepts_plain_steam_title_only_with_guard_text(self):
        class FakeWindow:
            title = "Steam"

        with patch.object(steam_login_helper, "window_process_name", return_value="steam.exe"), patch.object(
            steam_login_helper, "window_combined_text", return_value="Steam store library"
        ):
            self.assertFalse(steam_login_helper.is_wait_guard_target(FakeWindow()))

        with patch.object(steam_login_helper, "window_process_name", return_value="steamwebhelper.exe"), patch.object(
            steam_login_helper, "window_combined_text", return_value="Steam Guard enter the code sent to your email"
        ):
            self.assertTrue(steam_login_helper.is_wait_guard_target(FakeWindow()))

    def test_steam_helper_classifies_login_blocking_pages_before_guard_timeout(self):
        self.assertEqual(
            "",
            steam_login_helper.classify_steam_login_blocking_text("Steam account name password sign in"),
        )
        self.assertEqual(
            "Steam account/password rejected",
            steam_login_helper.classify_steam_login_blocking_text("The account name or password that you have entered is incorrect."),
        )
        self.assertEqual(
            "Steam captcha or human verification required",
            steam_login_helper.classify_steam_login_blocking_text("Verify you are human captcha"),
        )
        self.assertEqual(
            "Steam mobile authenticator or QR approval required",
            steam_login_helper.classify_steam_login_blocking_text("Approve sign in with the Steam Mobile App or scan the QR code."),
        )
        self.assertEqual(
            "",
            steam_login_helper.classify_steam_login_blocking_text(
                "登录 Steam 用帐户名称登录 felipexx 密码 记住我 登录 或者用二维码登录 通过二维码使用 Steam 手机应用登录"
            ),
        )
        self.assertEqual("", steam_login_helper.classify_steam_login_blocking_text("Steam Guard enter the code sent to your email"))

        source = inspect.getsource(steam_login_helper.wait_guard_prompt)
        self.assertIn("classify_steam_login_blocking_text", source)
        self.assertIn("login blocked before Guard prompt", source)
        self.assertIn("last_seen=", source)

    def test_task1_steam_flow_passes_log_callback_to_process_steps(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)

        self.assertIn("shutdown_steam(steam_exe, log=log, pause_controller=pause_controller)", source)
        self.assertIn("start_steam_login(steam_exe, account, log)", source)
        self.assertIn("send_guard_code_to_steam(", source)
        self.assertIn("steam_guard_screenshot", source)
        self.assertIn("wait_for_steam_id(", source)
        self.assertIn("pause_controller,", source)
        self.assertIn("ensure_steam_login_submitted", source)
        self.assertIn("steam_login_screenshot", source)
        self.assertIn("ensure_steam_guard_prompt_visible", source)
        self.assertIn("ensure_steam_guard_prompt_visible", source)
        mail_search_block = source[source.index("search_steam_guard_mail(") : source.index("send_guard_code_to_steam")]
        self.assertIn("on_manual_wait", mail_search_block)
        self.assertIn("on_manual_resume", mail_search_block)
        self.assertIn("safe_artifact_name(account.steam_username)", source)

    def test_steam_helper_failure_paths_save_screenshots(self):
        helper_source = inspect.getsource(steam_login_helper)
        login_source = inspect.getsource(steam_login_helper.submit_login)
        guard_source = inspect.getsource(steam_login_helper.submit_guard)
        wait_source = inspect.getsource(steam_login_helper.wait_guard_prompt)
        batch_login_source = inspect.getsource(steam_outlook_batch.ensure_steam_login_submitted)
        batch_wait_source = inspect.getsource(steam_outlook_batch.ensure_steam_guard_prompt_visible)
        batch_guard_source = inspect.getsource(steam_outlook_batch.send_guard_code_to_steam)

        self.assertIn("save_window_screenshot", helper_source)
        self.assertIn('payload.get("screenshot_path")', login_source)
        self.assertIn("login submit timeout", login_source)
        self.assertIn("login blocked", login_source)
        self.assertIn("guard submit timeout", guard_source)
        self.assertIn("wait guard timeout", wait_source)
        self.assertIn("wait guard blocked", wait_source)
        self.assertIn('"screenshot_path": str(screenshot_path or "")', batch_login_source)
        self.assertIn('"screenshot_path": str(screenshot_path or "")', batch_wait_source)
        self.assertIn('"screenshot_path": str(screenshot_path or "")', batch_guard_source)

    def test_shutdown_steam_can_be_stopped_from_web_ui(self):
        source = inspect.getsource(steam_outlook_batch.shutdown_steam)

        self.assertIn("pause_controller.should_stop()", source)
        self.assertIn("Stopped while shutting down Steam", source)
        self.assertIn("Stopped before force killing Steam", source)

    def test_wait_for_steam_id_timeout_includes_last_observed_accounts(self):
        source = inspect.getsource(steam_outlook_batch.wait_for_steam_id)

        self.assertIn("target account", source)
        self.assertIn("last_recent", source)
        self.assertIn("parsed_accounts", source)
        self.assertIn("target={steam_username}", source)
        self.assertIn("pause_controller.should_stop()", source)
        self.assertIn("Stopped while waiting for Steam ID", source)

    def test_task1_confirms_guard_prompt_before_mail_search(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        guard_prompt_index = source.index("ensure_steam_guard_prompt_visible")
        mail_search_index = source.index("search_steam_guard_mail(")

        self.assertLess(guard_prompt_index, mail_search_index)

    def test_steam_helper_has_wait_guard_mode(self):
        source = inspect.getsource(steam_login_helper.main)

        self.assertIn('"wait_guard"', source)
        self.assertIn("wait_guard_prompt(payload)", source)

    def test_submit_guard_uses_strict_guard_prompt_target(self):
        source = inspect.getsource(steam_login_helper.submit_guard)

        self.assertIn("is_expected_wait_guard_target(window, username)", source)
        self.assertNotIn("is_guard_target(window)", source)
        self.assertIn("skipped Guard submit candidate", source)

    def test_wait_guard_requires_current_steam_username_when_provided(self):
        class FakeWindow:
            title = "Steam"

        with patch.object(steam_login_helper, "window_process_name", return_value="steamwebhelper.exe"), patch.object(
            steam_login_helper,
            "window_combined_text",
            return_value="Steam Guard enter the code sent to your email for account old_user",
        ):
            self.assertFalse(steam_login_helper.is_expected_wait_guard_target(FakeWindow(), "new_user"))
            self.assertTrue(steam_login_helper.is_expected_wait_guard_target(FakeWindow(), "old_user"))

        wait_source = inspect.getsource(steam_login_helper.wait_guard_prompt)
        guard_source = inspect.getsource(steam_login_helper.submit_guard)
        batch_source = inspect.getsource(steam_outlook_batch.ensure_steam_guard_prompt_visible)

        self.assertIn('payload.get("username")', wait_source)
        self.assertIn('payload.get("username")', guard_source)
        self.assertIn('"username": steam_username', batch_source)

    def test_wait_guard_logs_candidate_text_instead_of_non_steam_label(self):
        source = inspect.getsource(steam_login_helper.wait_guard_prompt)

        self.assertIn("skipped wait-guard candidate", source)
        self.assertIn("text_snippet", source)
        self.assertNotIn("skipped non-Steam wait-guard", source)

    def test_outlook_waits_log_current_page_state(self):
        inbox_source = inspect.getsource(steam_outlook_batch.wait_for_outlook_inbox)
        session_source = inspect.getsource(steam_outlook_batch.current_outlook_mailbox_matches)

        self.assertIn("page_state_summary", inbox_source)
        self.assertIn("page_state_summary", session_source)
        self.assertIn("page_state_summary", inspect.getsource(steam_outlook_batch.wait_for_security_mailbox))
        self.assertIn("Stopped while waiting for Outlook inbox", inbox_source)
        self.assertIn("Stopped while checking Outlook session", session_source)
        self.assertIn("handle_outlook_blocking_page", inbox_source)
        self.assertIn("handle_outlook_blocking_page", session_source)
        self.assertIn("Last page", inbox_source)
        markers_block = inbox_source[inbox_source.index("markers = [") : inbox_source.index("if \"outlook.live.com\"")]
        self.assertNotIn('"Outlook"', markers_block)
        self.assertIn('"Inbox"', markers_block)
        self.assertIn('"New mail"', markers_block)
        self.assertIn("inbox was not confirmed", session_source)
        self.assertIn("will not reuse this session", session_source)
        self.assertIn("Current Outlook session did not confirm", session_source)
        self.assertNotIn("except Exception:\n                pass\n            log(f\"Current Outlook session already matches", session_source)

    def test_security_mailbox_manual_wait_has_periodic_status_refresh(self):
        source = inspect.getsource(steam_outlook_batch.wait_for_security_mailbox)
        controller_source = inspect.getsource(steam_outlook_batch.PauseController)

        self.assertIn("wait_timeout", controller_source)
        self.assertIn("reset_wait", controller_source)
        self.assertIn("wait_for_manual_continue", source)
        self.assertIn("security_mailbox_", source)
        self.assertIn("screenshot_path", source)
        self.assertIn("page=page", source)

    def test_pause_controller_timeout_wait_does_not_drop_existing_continue(self):
        controller = steam_outlook_batch.PauseController()
        controller.continue_next()

        self.assertTrue(controller.wait_timeout(0.01))

    def test_browser_mail_search_only_extracts_after_opening_mail(self):
        source = inspect.getsource(steam_outlook_batch.search_steam_guard_mail)
        opened_index = source.index("if await open_latest_steam_guard_mail")
        extract_index = source.index("extract_steam_guard_code_from_open_message", opened_index)

        self.assertGreater(extract_index, opened_index)
        self.assertIn("Steam Guard code extracted from latest Outlook mail", source)
        self.assertIn("handle_microsoft_blocking_page", source)
        self.assertIn("Outlook search box not found", source)
        self.assertIn("Stopped while waiting for Steam Guard email", source)
        self.assertGreaterEqual(source.count("pause_controller.should_stop()"), 3)
        self.assertIn("isread:no from:steampowered.com", source)
        self.assertIn("read_open_outlook_message_texts", inspect.getsource(steam_outlook_batch.extract_steam_guard_code_from_open_message))

    def test_task1_steam_guard_email_timeout_has_manual_recovery_path(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)

        self.assertIn("Steam Guard email auto-search timed out", source)
        self.assertIn("_steam_guard_manual.png", source)
        self.assertIn("manual Steam Guard recovery", source)
        self.assertIn("loginusers.vdf 验证是否真的登录成功", source)
        self.assertIn("noreply@steampowered.com", source)

    def test_process_check_timeout_logs_and_assumes_running(self):
        logs = []

        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout") or 0)

        with patch.object(steam_outlook_batch.subprocess, "run", side_effect=fake_run):
            running = steam_outlook_batch.steam_process_running(logs.append)

        self.assertTrue(running)
        self.assertTrue(any("process check timed out" in line for line in logs))

    def test_shutdown_logs_and_force_kills_when_shutdown_command_times_out(self):
        calls = []
        logs = []
        tasklist_calls = 0

        def fake_run(command, **kwargs):
            nonlocal tasklist_calls
            calls.append(command)
            joined = " ".join(str(part) for part in command)
            if "-shutdown" in joined:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout") or 0)
            if "taskkill" in joined:
                return FakeCompleted()
            if "tasklist" in joined:
                tasklist_calls += 1
                if tasklist_calls == 1:
                    return FakeCompleted(stdout="steam.exe                    1234 Console                    1     10,000 K")
                return FakeCompleted(stdout="INFO: No tasks are running which match the specified criteria.")
            raise AssertionError(f"unexpected command: {command}")

        with patch.object(steam_outlook_batch.subprocess, "run", side_effect=fake_run), patch.object(
            steam_outlook_batch.time, "sleep", return_value=None
        ):
            steam_outlook_batch.shutdown_steam(Path("D:/Steam/steam.exe"), timeout_seconds=0, log=logs.append)

        self.assertEqual(4, len(calls))
        self.assertTrue(any("timed out" in line for line in logs))
        self.assertTrue(any("force killing" in line for line in logs))
        self.assertTrue(any("force kill completed" in line for line in logs))

    def test_shutdown_skips_steam_shutdown_command_when_steam_is_not_running(self):
        calls = []
        logs = []

        def fake_run(command, **kwargs):
            calls.append(command)
            joined = " ".join(str(part) for part in command)
            if "tasklist" in joined:
                return FakeCompleted(stdout="INFO: No tasks are running which match the specified criteria.")
            raise AssertionError(f"unexpected command: {command}")

        with patch.object(steam_outlook_batch.subprocess, "run", side_effect=fake_run):
            steam_outlook_batch.shutdown_steam(Path("D:/Steam/steam.exe"), log=logs.append)

        self.assertEqual(1, len(calls))
        self.assertTrue(any("skipping shutdown" in line for line in logs))

    def test_shutdown_logs_while_waiting_for_steam_to_exit(self):
        logs = []
        monotonic_values = iter([0, 0, 0, 31])

        def fake_run(command, **kwargs):
            joined = " ".join(str(part) for part in command)
            if "tasklist" in joined:
                return FakeCompleted(stdout="steam.exe                    1234 Console                    1     10,000 K")
            if "taskkill" in joined:
                return FakeCompleted()
            return FakeCompleted()

        with patch.object(steam_outlook_batch.subprocess, "run", side_effect=fake_run), patch.object(
            steam_outlook_batch.time, "sleep", return_value=None
        ), patch.object(steam_outlook_batch.time, "monotonic", side_effect=lambda: next(monotonic_values)):
            with self.assertRaisesRegex(TimeoutError, "still running after force kill"):
                steam_outlook_batch.shutdown_steam(Path("D:/Steam/steam.exe"), timeout_seconds=30, log=logs.append)

        self.assertTrue(any("Waiting for Steam to exit" in line for line in logs))

    def test_shutdown_raises_if_steam_survives_force_kill(self):
        logs = []

        def fake_run(command, **kwargs):
            joined = " ".join(str(part) for part in command)
            if "tasklist" in joined:
                return FakeCompleted(stdout="steam.exe                    1234 Console                    1     10,000 K")
            return FakeCompleted()

        with patch.object(steam_outlook_batch.subprocess, "run", side_effect=fake_run), patch.object(
            steam_outlook_batch.time, "sleep", return_value=None
        ):
            with self.assertRaisesRegex(TimeoutError, "still running after force kill"):
                steam_outlook_batch.shutdown_steam(Path("D:/Steam/steam.exe"), timeout_seconds=0, log=logs.append)

        self.assertTrue(any("force killing" in line for line in logs))

    def test_shutdown_logs_taskkill_timeout_and_raises_if_steam_remains(self):
        logs = []

        def fake_run(command, **kwargs):
            joined = " ".join(str(part) for part in command)
            if "tasklist" in joined:
                return FakeCompleted(stdout="steam.exe                    1234 Console                    1     10,000 K")
            if "taskkill" in joined:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout") or 0)
            return FakeCompleted()

        with patch.object(steam_outlook_batch.subprocess, "run", side_effect=fake_run), patch.object(
            steam_outlook_batch.time, "sleep", return_value=None
        ):
            with self.assertRaisesRegex(TimeoutError, "still running after force kill"):
                steam_outlook_batch.shutdown_steam(Path("D:/Steam/steam.exe"), timeout_seconds=0, log=logs.append)

        self.assertTrue(any("taskkill steam.exe timed out" in line for line in logs))

    def test_start_steam_login_logs_pid_and_does_not_wait_on_process(self):
        logs = []
        account = SimpleNamespace(steam_username="user", steam_password="password")

        class FakeProcess:
            pid = 4242

        with patch.object(steam_outlook_batch.subprocess, "Popen", return_value=FakeProcess()) as popen:
            steam_outlook_batch.start_steam_login(Path("D:/Steam/steam.exe"), account, logs.append)

        popen.assert_called_once()
        self.assertTrue(any("pid=4242" in line for line in logs))

    def test_steam_helper_nonzero_error_includes_last_output(self):
        source = inspect.getsource(steam_outlook_batch.run_steam_helper)

        self.assertIn("last output", source)
        self.assertIn("exit code", source)
        self.assertIn("mode={mode}", source)
        self.assertIn("pause_controller.should_stop()", source)
        self.assertIn("Stopped Steam helper", source)
        self.assertIn("process.kill()", source)

    def test_web_ui_task1_defaults_enable_dota(self):
        args = steam_web_ui.build_args({"task_mode": "full", "enable_steam_login": True})

        self.assertTrue(args.enable_steam_login)
        self.assertTrue(args.enable_dota_check)
        self.assertTrue(args.enable_mail_maintenance)
        self.assertEqual(args.security_email, args.bind_security_email)

    def test_web_ui_security_pool_does_not_inject_conflicting_default_bind_email(self):
        with patch.object(steam_web_ui, "save_ui_settings"):
            args = steam_web_ui.build_args(
                {
                    "task_mode": "full",
                    "security_email": "fallback@example.com",
                    "security_mailbox_pool": "pool@example.com,password",
                }
            )

        self.assertEqual("", args.bind_security_email)
        self.assertEqual("pool@example.com,password", args.security_mailbox_pool)
        self.assertEqual("browser", args.security_code_source)
        self.assertFalse(args.pause_between)
        self.assertTrue(args.continue_on_error)

    def test_web_ui_security_pool_persists_first_mailbox_as_default(self):
        original_path = steam_web_ui.WEB_UI_SETTINGS
        temp_path = Path("test_steam_web_ui_settings.json")
        try:
            steam_web_ui.WEB_UI_SETTINGS = temp_path
            temp_path.write_text(
                '{"security_email":"security@example.com","security_mailbox_pool":"pool@example.com,password"}',
                encoding="utf-8",
            )

            settings = steam_web_ui.load_ui_settings()
            persisted = json.loads(temp_path.read_text(encoding="utf-8"))

            self.assertEqual("pool@example.com", settings["security_email"])
            self.assertEqual("pool@example.com", persisted["security_email"])
        finally:
            steam_web_ui.WEB_UI_SETTINGS = original_path
            if temp_path.exists():
                temp_path.unlink()

    def test_web_ui_security_pool_ignores_saved_single_bind_email(self):
        with patch.object(steam_web_ui, "save_ui_settings"):
            args = steam_web_ui.build_args(
                {
                    "task_mode": "full",
                    "security_email": "pool@example.com",
                    "bind_security_email": "stale@example.com",
                    "security_mailbox_pool": "pool@example.com,password",
                }
            )

        self.assertEqual("", args.bind_security_email)

    def test_cli_task_defaults_match_task1_complete_flow(self):
        args = steam_outlook_batch.parse_args([])

        steam_outlook_batch.normalize_task_args(args)

        self.assertEqual("full", args.task_mode)
        self.assertTrue(args.enable_steam_login)
        self.assertTrue(args.enable_dota_check)
        self.assertTrue(args.enable_mail_maintenance)
        self.assertEqual(args.security_email, args.bind_security_email)

    def test_task_arg_normalization_disables_irrelevant_steps_by_mode(self):
        mail_args = steam_outlook_batch.parse_args(["--task-mode", "mail_password"])
        forward_args = steam_outlook_batch.parse_args(["--task-mode", "forward_rule"])

        steam_outlook_batch.normalize_task_args(mail_args)
        steam_outlook_batch.normalize_task_args(forward_args)

        self.assertTrue(mail_args.enable_mail_maintenance)
        self.assertFalse(mail_args.enable_steam_login)
        self.assertFalse(mail_args.enable_dota_check)
        self.assertEqual(mail_args.security_email, mail_args.bind_security_email)
        self.assertTrue(forward_args.enable_mail_maintenance)
        self.assertFalse(forward_args.enable_steam_login)
        self.assertFalse(forward_args.enable_dota_check)
        self.assertEqual(forward_args.security_email, forward_args.bind_security_email)

    def test_task1_binds_security_email_without_changing_mail_password(self):
        run_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        normalize_source = inspect.getsource(steam_outlook_batch.normalize_task_args)
        maintenance_source = inspect.getsource(steam_outlook_batch.run_mail_security_maintenance)

        self.assertIn('task_mode == "full"', normalize_source)
        self.assertIn("enable_mail_maintenance", normalize_source)
        self.assertIn("normalize_task_args(args)", run_source)
        self.assertIn('change_password=task_mode == "mail_password"', run_source)
        self.assertIn("if not change_password:", maintenance_source)
        self.assertIn("Skipped mailbox password change", maintenance_source)

    def test_task1_security_mailbox_pool_opens_security_window_even_with_manual_target_verification(self):
        run_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        preflight_source = inspect.getsource(steam_web_ui.preflight)

        self.assertIn('task_mode in {"full", "mail_password", "forward_rule"} and bool(security_pool)', run_source)
        self.assertIn("Security mailbox pool is enabled", run_source)
        self.assertIn("使用安全邮箱池时会打开窗口2", preflight_source)

    def test_web_ui_has_no_graph_workflow_controls(self):
        html = steam_web_ui.HTML

        self.assertNotIn("Graph", html)
        self.assertNotIn("graph", html)
        self.assertNotIn("API 授权", html)
        self.assertNotIn("Client ID", html)

    def test_web_ui_security_mailbox_pool_is_common_setting(self):
        html = steam_web_ui.HTML
        security_section_index = html.index('data-common-section="security_verification"')
        full_index = html.index('data-section="full"')
        pool_index = html.index('id="security_mailbox_pool"')

        self.assertGreater(pool_index, security_section_index)
        self.assertLess(pool_index, full_index)
        self.assertIn("安全邮箱账号密码池（任务1、任务2、任务3共用；每行：邮箱,密码）", html)
        self.assertIn("任务1和任务3用于目标邮箱登录时收 Microsoft 安全码", html)
        self.assertIn("默认安全邮箱（兜底）", html)
        self.assertIn("已满后才切换下一个", html)

    def test_web_ui_keeps_security_settings_visible_for_task3_binding(self):
        html = steam_web_ui.HTML

        self.assertIn('data-common-section="security_verification"', html)
        self.assertIn("const visible = true", html)
        self.assertIn("安全邮箱账号密码池（任务1、任务2、任务3共用", html)

    def test_web_ui_loads_saved_settings_endpoint(self):
        html = steam_web_ui.HTML

        self.assertIn('api("/api/ui-settings")', html)
        self.assertIn("loadUiSettings().finally", html)

    def test_web_ui_keeps_full_security_mailbox_credentials_for_task3(self):
        original_path = steam_web_ui.WEB_UI_SETTINGS
        temp_path = Path("test_steam_web_ui_settings.json")
        try:
            steam_web_ui.WEB_UI_SETTINGS = temp_path
            temp_path.write_text(
                '{"security_mailbox_pool":"full@example.com,p1\\nopen@example.com,p2"}',
                encoding="utf-8",
            )

            removed = steam_web_ui.prune_full_security_mailboxes(["full@example.com"])
            data = temp_path.read_text(encoding="utf-8")

            self.assertEqual(0, removed)
            self.assertIn("full@example.com,p1", data)
            self.assertIn("open@example.com,p2", data)
        finally:
            steam_web_ui.WEB_UI_SETTINGS = original_path
            if temp_path.exists():
                temp_path.unlink()

    def test_web_ui_persists_blocked_security_mailbox_and_removes_from_pool(self):
        original_path = steam_web_ui.WEB_UI_SETTINGS
        temp_path = Path("test_steam_web_ui_settings.json")
        try:
            steam_web_ui.WEB_UI_SETTINGS = temp_path
            temp_path.write_text(
                '{"security_mailbox_pool":"limited@example.com,p1\\nopen@example.com,p2"}',
                encoding="utf-8",
            )

            removed = steam_web_ui.mark_security_mailbox_blocked(
                "limited@example.com",
                "Microsoft rejected code delivery",
            )
            data = json.loads(temp_path.read_text(encoding="utf-8"))

            self.assertEqual(1, removed)
            self.assertNotIn("limited@example.com", data["security_mailbox_pool"])
            self.assertIn("open@example.com,p2", data["security_mailbox_pool"])
            self.assertEqual("limited@example.com", data["blocked_security_mailboxes"][0]["email"])
            self.assertIn("Microsoft rejected", data["blocked_security_mailboxes"][0]["reason"])
        finally:
            steam_web_ui.WEB_UI_SETTINGS = original_path
            if temp_path.exists():
                temp_path.unlink()

    def test_read_pending_accounts_excludes_blocked_security_mailboxes_from_pool(self):
        rows = [
            ["steam账号", "密码", "邮箱账号", "密码", "steam id", "备注", "备用安全邮箱"],
            ["steam_user", "steam_pass", "target@example.com", "mail_pass", "", "", ""],
        ]
        args = argparse.Namespace(
            feishu_config="config.json",
            feishu_start_script="start.ps1",
            lark_mcp_root=".",
            feishu_auth="user",
            sheet_url="https://example.test/sheets/token",
            sheet_id=None,
            sheet_range="A1:K1000",
            task_mode="full",
            bind_security_email="",
            security_email_bind_limit=10,
            security_mailbox_pool="limited@example.com,p1\nopen@example.com,p2",
            blocked_security_mailboxes=["limited@example.com"],
            start_row=None,
            target_email="",
        )

        response = {"code": 0, "data": {"valueRange": {"values": rows}}}

        with patch.object(steam_outlook_batch.feishu_tool, "load_config", return_value={}), patch.object(
            steam_outlook_batch.feishu_tool, "start_mcp", return_value=None
        ), patch.object(
            steam_outlook_batch.feishu_tool,
            "resolve_spreadsheet",
            return_value={"spreadsheet_token": "token", "sheet_id": "sheet"},
        ), patch.object(
            steam_outlook_batch.feishu_tool, "read_sheet", return_value=response
        ):
            accounts, _, _, _ = steam_outlook_batch.read_pending_accounts(args)

        self.assertEqual("open@example.com", accounts[0].backup_security_email)
        self.assertEqual("p2", accounts[0].security_email_password)
        self.assertTrue(accounts[0].security_binding_required)
        self.assertEqual(1, args.skipped_blocked_security_mailbox_pool)

    def test_read_pending_accounts_applies_target_filter_before_security_pool_assignment(self):
        rows = [
            ["steam账号", "密码", "邮箱账号", "密码", "steam id", "备注", "备用安全邮箱"],
            ["outside", "steam_pass", "outside@example.com", "mail_pass", "", "", ""],
            ["target", "steam_pass", "target@example.com", "mail_pass", "", "", ""],
        ]
        args = argparse.Namespace(
            feishu_config="config.json",
            feishu_start_script="start.ps1",
            lark_mcp_root=".",
            feishu_auth="user",
            sheet_url="https://example.test/sheets/token",
            sheet_id=None,
            sheet_range="A1:K1000",
            task_mode="full",
            bind_security_email="",
            security_email_bind_limit=1,
            security_mailbox_pool="one@example.com,p1",
            blocked_security_mailboxes=[],
            start_row=None,
            target_email="target@example.com",
        )
        response = {"code": 0, "data": {"valueRange": {"values": rows}}}

        with patch.object(steam_outlook_batch.feishu_tool, "load_config", return_value={}), patch.object(
            steam_outlook_batch.feishu_tool, "start_mcp", return_value=None
        ), patch.object(
            steam_outlook_batch.feishu_tool,
            "resolve_spreadsheet",
            return_value={"spreadsheet_token": "token", "sheet_id": "sheet"},
        ), patch.object(
            steam_outlook_batch.feishu_tool, "read_sheet", return_value=response
        ):
            accounts, _, _, _ = steam_outlook_batch.read_pending_accounts(args)

        self.assertEqual(1, len(accounts))
        self.assertEqual("target@example.com", accounts[0].email)
        self.assertEqual("one@example.com", accounts[0].backup_security_email)
        self.assertTrue(accounts[0].security_binding_required)
        self.assertEqual(0, args.skipped_security_bind_limit)
        self.assertEqual(
            [{"email": "one@example.com", "count": 1, "limit": 1}],
            args.security_pool_usage,
        )

    def test_existing_feishu_security_email_marks_binding_stage_complete(self):
        rows = [
            ["steam账号", "密码", "邮箱账号", "密码", "steam id", "备注", "备用安全邮箱", "运营备注说明"],
            ["target", "steam_pass", "target@example.com", "mail_pass", "123", "", "one@example.com", ""],
            ["skip", "steam_pass", "skip@example.com", "mail_pass", "456", "", "one@example.com", "不要处理"],
        ]
        args = argparse.Namespace(
            feishu_config="config.json", feishu_start_script="start.ps1", lark_mcp_root=".",
            feishu_auth="user", sheet_url="https://example.test/sheets/token", sheet_id=None,
            sheet_range="A1:K1000", task_mode="forward_rule", bind_security_email="",
            security_email_bind_limit=10, security_mailbox_pool="one@example.com,p1",
            blocked_security_mailboxes=[], start_row=None, target_email="",
        )
        response = {"code": 0, "data": {"valueRange": {"values": rows}}}
        with patch.object(steam_outlook_batch.feishu_tool, "load_config", return_value={}), patch.object(
            steam_outlook_batch.feishu_tool, "start_mcp", return_value=None
        ), patch.object(
            steam_outlook_batch.feishu_tool, "resolve_spreadsheet",
            return_value={"spreadsheet_token": "token", "sheet_id": "sheet"},
        ), patch.object(steam_outlook_batch.feishu_tool, "read_sheet", return_value=response):
            accounts, _, _, _ = steam_outlook_batch.read_pending_accounts(args)

        self.assertEqual("one@example.com", accounts[0].backup_security_email)
        self.assertFalse(accounts[0].security_binding_required)
        self.assertEqual([3], args.skipped_remark_rows)

    def test_forward_rule_skips_any_nonempty_k_including_rich_link(self):
        rows = [
            ["steam账号", "密码", "邮箱账号", "密码", "steam id", "备注", "转发规则"],
            ["done", "steam_pass", "done@example.com", "mail_pass", "123", "", [{"type": "url", "text": "code@example.com", "link": "mailto:code@example.com"}]],
            ["pending", "steam_pass", "pending@example.com", "mail_pass", "456", "", ""],
        ]
        args = argparse.Namespace(
            feishu_config="config.json", feishu_start_script="start.ps1", lark_mcp_root=".",
            feishu_auth="user", sheet_url="https://example.test/sheets/token", sheet_id=None,
            sheet_range="A1:K1000", task_mode="forward_rule", bind_security_email="",
            security_email_bind_limit=10, security_mailbox_pool="", blocked_security_mailboxes=[],
            start_row=None, target_email="",
        )
        response = {"code": 0, "data": {"valueRange": {"values": rows}}}
        with patch.object(steam_outlook_batch.feishu_tool, "load_config", return_value={}), patch.object(
            steam_outlook_batch.feishu_tool, "start_mcp", return_value=None
        ), patch.object(
            steam_outlook_batch.feishu_tool, "resolve_spreadsheet",
            return_value={"spreadsheet_token": "token", "sheet_id": "sheet"},
        ), patch.object(steam_outlook_batch.feishu_tool, "read_sheet", return_value=response):
            accounts, _, _, _ = steam_outlook_batch.read_pending_accounts(args)

        self.assertEqual(["pending@example.com"], [account.email for account in accounts])

    def test_security_email_delivery_failure_records_limited_mailbox(self):
        class FakeLocator:
            async def inner_text(self, timeout=0):
                return "We can't send a code right now. Try again later."

        class FakePage:
            def locator(self, selector):
                return FakeLocator()

        blocked = []

        async def run_case():
            with patch.object(steam_outlook_batch, "submit_security_email", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "收码受限"):
                    await steam_outlook_batch.submit_security_email_or_record_failure(
                        FakePage(),
                        "limited@example.com",
                        "target@example.com",
                        lambda message: None,
                        lambda email, reason: blocked.append((email, reason)),
                    )

        asyncio.run(run_case())

        self.assertEqual("limited@example.com", blocked[0][0])
        self.assertIn("target@example.com", blocked[0][1])

    def test_security_email_full_address_failure_records_limited_mailbox(self):
        class FakeLocator:
            async def inner_text(self, timeout=0):
                return "发送验证码失败。验证失败，请稍后再试。"

        class FakePage:
            def locator(self, selector):
                return FakeLocator()

        blocked = []

        async def run_case():
            with patch.object(steam_outlook_batch, "submit_security_email", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "已持久化记录并从安全邮箱池移除"):
                    await steam_outlook_batch.submit_security_email_or_record_failure(
                        FakePage(),
                        "risk@example.com",
                        "target@example.com",
                        lambda message: None,
                        lambda email, reason: blocked.append((email, reason)),
                    )

        asyncio.run(run_case())

        self.assertEqual("risk@example.com", blocked[0][0])
        self.assertIn("Microsoft rejected security-email code delivery after submitting full address", blocked[0][1])

    def test_security_mailbox_login_delivery_failure_records_limited_mailbox(self):
        class FakeLocator:
            async def inner_text(self, timeout=0):
                return "We can't send a code right now. Try again later."

        class FakePage:
            url = "https://login.live.com/"

            def locator(self, selector):
                return FakeLocator()

        blocked = []

        async def run_case():
            with self.assertRaisesRegex(RuntimeError, "自身登录验证收码受限"):
                await steam_outlook_batch.wait_for_security_mailbox(
                    FakePage(),
                    "limited@example.com",
                    lambda message: None,
                    on_security_mailbox_blocked=lambda email, reason: blocked.append((email, reason)),
                )

        asyncio.run(run_case())
        self.assertEqual("limited@example.com", blocked[0][0])
        self.assertIn("cannot receive Microsoft verification code", blocked[0][1])

    def test_verify_security_mailbox_requires_outlook_inbox_not_login_prompt(self):
        class FakePage:
            def __init__(self, url):
                self.url = url

        async def run_case():
            self.assertFalse(
                await steam_outlook_batch.verify_security_mailbox(
                    FakePage("https://login.live.com/"),
                    "security@example.com",
                )
            )
            with patch.object(steam_outlook_batch, "read_active_outlook_mailbox", return_value=""):
                self.assertFalse(
                    await steam_outlook_batch.verify_security_mailbox(
                        FakePage("https://outlook.live.com/mail/"),
                        "security@example.com",
                    )
                )
            with patch.object(steam_outlook_batch, "read_active_outlook_mailbox", return_value="security@example.com"):
                self.assertTrue(
                    await steam_outlook_batch.verify_security_mailbox(
                        FakePage("https://outlook.live.com/mail/"),
                        "security@example.com",
                    )
                )

        asyncio.run(run_case())

    def test_password_change_failure_logs_page_state(self):
        source = inspect.getsource(steam_outlook_batch.try_change_microsoft_password)
        maintenance_source = inspect.getsource(steam_outlook_batch.run_mail_security_maintenance)

        self.assertIn("password_input_count", source)
        self.assertIn("page_state_summary", source)
        self.assertIn("submit button not found", source)
        self.assertIn("Password change requires manual completion", maintenance_source)
        self.assertIn("_bind_security.png", maintenance_source)
        self.assertIn("_password_change.png", maintenance_source)
        self.assertIn("screenshot_path", maintenance_source)
        self.assertIn("page=page", maintenance_source)
        self.assertIn("ensure_security_binding_ready_for_writeback", maintenance_source)
        self.assertIn("ensure_password_change_ready_for_writeback", maintenance_source)

    def test_forward_rule_fallback_logs_page_state(self):
        source = inspect.getsource(steam_outlook_batch.open_outlook_rules_settings)
        setup_source = inspect.getsource(steam_outlook_batch.run_forward_rule_setup)

        self.assertIn("forwarding", source)
        self.assertIn("Forwarding", source)
        self.assertIn("forwarding/rules URL did not show expected markers", source)
        self.assertIn("Falling back to Outlook settings menu", source)
        self.assertIn("page_state_summary", source)
        self.assertIn("handle_microsoft_blocking_page", source)
        self.assertIn("handle_outlook_blocking_page", source)
        self.assertIn("pause_controller", setup_source)
        self.assertIn("on_manual_wait", setup_source)

    def test_mail_code_searches_handle_outlook_blocking_pages(self):
        steam_source = inspect.getsource(steam_outlook_batch.search_steam_guard_mail)
        microsoft_source = inspect.getsource(steam_outlook_batch.get_microsoft_security_code_from_mailbox)

        self.assertIn("handle_microsoft_blocking_page", steam_source)
        self.assertIn("handle_outlook_blocking_page", steam_source)
        self.assertIn("目标邮箱 Steam Guard 收件箱", steam_source)
        self.assertIn("handle_microsoft_blocking_page", microsoft_source)
        self.assertIn("handle_outlook_blocking_page", microsoft_source)
        self.assertIn("安全邮箱窗口收取", microsoft_source)
        self.assertIn("min(20, remaining)", microsoft_source)
        self.assertIn("wait_for_timeout_with_stop(page, 1000", microsoft_source)
        self.assertIn("pause_controller", microsoft_source)

    def test_forward_rule_automatic_creation_precedes_verified_writeback(self):
        source = inspect.getsource(steam_outlook_batch.run_forward_rule_setup)
        create_source = inspect.getsource(steam_outlook_batch.create_or_update_outlook_forward_rule)
        click_source = inspect.getsource(steam_outlook_batch.click_outlook_rule_control)
        dom_click_source = inspect.getsource(steam_outlook_batch.click_outlook_dom_control)
        geometry_source = inspect.getsource(steam_outlook_batch.click_outlook_rule_dropdown_by_geometry)
        value_input_source = inspect.getsource(steam_outlook_batch.outlook_rule_input_by_geometry)
        value_wait_source = inspect.getsource(steam_outlook_batch.wait_for_outlook_rule_input)

        self.assertIn("create_or_update_outlook_forward_rule", source)
        self.assertIn("from_email", source)
        self.assertIn("forward_to", source)
        self.assertIn("on_verification_required", source)
        self.assertIn("_forward_rule_retry.png", source)
        self.assertIn("自动创建或核验 Outlook 规则失败", source)
        self.assertIn("Stop processing more rules", source)
        self.assertIn("page=page", source)
        self.assertIn("ensure_outlook_forward_rule_ready_for_writeback", source)
        self.assertIn("account.row_number, forward_to", source)
        self.assertNotIn("datetime.now", source)
        self.assertIn('r"^Add rule$"', create_source)
        self.assertIn('"combobox"', click_source)
        self.assertIn("condition_opened", create_source)
        self.assertIn('r"^From$"', create_source)
        self.assertIn("action_opened", create_source)
        self.assertIn('r"^Forward to$"', create_source)
        self.assertIn("click_outlook_dom_control", create_source)
        self.assertIn("page.frames", dom_click_source)
        self.assertIn("data-is-focusable", dom_click_source)
        self.assertIn("name_input.bounding_box", geometry_source)
        self.assertIn('page.keyboard.type("From"', create_source)
        self.assertIn('page.keyboard.type("Forward to"', create_source)
        self.assertIn("condition_geometry_used", create_source)
        self.assertIn("action_geometry_used", create_source)
        self.assertIn("wait_for_outlook_rule_input", create_source)
        self.assertIn("name_input.element_handle", create_source)
        self.assertIn("fill_outlook_rule_input", create_source)
        self.assertIn("[role='textbox']", value_input_source)
        self.assertIn("[contenteditable='true']", value_input_source)
        self.assertIn("Outlook rule value editor render", value_wait_source)

    def test_task3_handles_forward_rule_sign_in_verification_popup(self):
        create_source = inspect.getsource(steam_outlook_batch.create_or_update_outlook_forward_rule)
        verify_source = inspect.getsource(steam_outlook_batch.complete_outlook_forward_verification)
        reauth_source = inspect.getsource(steam_outlook_batch.complete_outlook_forward_reauthentication)
        recovery_source = inspect.getsource(steam_outlook_batch.is_recovery_email_setup_page)
        code_request_source = inspect.getsource(steam_outlook_batch.request_microsoft_security_email_code)

        self.assertIn("outlook_forward_verification_required", create_source)
        self.assertIn("on_verification_required", create_source)
        self.assertIn("complete_verification_before_continuing", create_source)
        self.assertIn("认证尚未完成", create_source)
        self.assertIn("Sign in", verify_source)
        self.assertIn("context.pages", verify_source)
        self.assertIn("complete_outlook_forward_reauthentication", verify_source)
        self.assertIn("complete_recovery_email_setup", verify_source)
        self.assertIn("account.email_password", reauth_source)
        self.assertIn("outlook_forward_verification_required(rule_page)", reauth_source)
        self.assertIn("verification_page.is_closed()", reauth_source)
        self.assertIn("security code input", reauth_source)
        self.assertIn("wait_for_outlook_forward_verification_commit", verify_source)
        self.assertIn("wait_for_outlook_forward_verification_commit", reauth_source)
        self.assertIn("submit_security_email", code_request_source)
        self.assertIn("microsoft_security_code_entry_visible", code_request_source)
        self.assertIn("未能发送验证码", code_request_source)

    def test_task3_recovery_popup_recognizes_security_management_add_flow(self):
        source = inspect.getsource(steam_outlook_batch.is_recovery_email_setup_page)
        self.assertIn("account.live.com/proofs/manage", source)
        self.assertIn("add a new way to sign in", source)
        self.assertIn("添加新的登录或验证方式", source)

    def test_task3_does_not_read_mailbox_before_microsoft_code_step(self):
        source = inspect.getsource(steam_outlook_batch.request_microsoft_security_email_code)
        self.assertLess(source.index("microsoft_security_code_entry_visible(page)"), source.index("submit_security_email(page"))
        self.assertIn("did not reach the code-entry page", source)

    def test_external_forward_targets_skip_feishu_row_writeback(self):
        account_source = inspect.getsource(steam_outlook_batch.SteamAccount)
        rule_source = inspect.getsource(steam_outlook_batch.run_forward_rule_setup)
        worker_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        self.assertIn("external_target", account_source)
        self.assertIn("External target forwarding verified", rule_source)
        self.assertIn("external_only", worker_source)

    def test_forward_verification_detects_account_without_your_variant(self):
        source = inspect.getsource(steam_outlook_batch.outlook_forward_verification_required)
        self.assertIn("登录并验证帐户", source)
        self.assertIn("sign in and verify account", source)

    def test_external_targets_require_capacity_checked_pool(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        self.assertIn("security_pool_full_emails", source)
        self.assertIn("禁止绕过满额过滤", source)

    def test_forward_verification_refreshes_parent_rule_editor_after_popup_closes(self):
        source = inspect.getsource(steam_outlook_batch.wait_for_outlook_forward_verification_commit)
        self.assertIn("rule_page.reload", source)
        self.assertIn("verification popup closed", source)

    def test_browser_password_prompt_is_closed_and_rule_failure_retries(self):
        prompt_source = inspect.getsource(steam_outlook_batch.dismiss_password_save_prompt)
        prompt_handler_source = inspect.getsource(steam_outlook_batch.handle_microsoft_post_login_prompts)
        rule_source = inspect.getsource(steam_outlook_batch.run_forward_rule_setup)
        retry_source = inspect.getsource(steam_outlook_batch.is_outlook_context_retryable_error)
        self.assertIn("保存密码", prompt_source)
        self.assertIn("dismiss_password_save_prompt", prompt_handler_source)
        self.assertIn("retrying this account once", rule_source)
        self.assertIn("规则编辑器未", retry_source)

    def test_forward_verification_waits_for_rule_editor_commit_after_popup_redirect(self):
        class FakePopup:
            url = "https://outlook.live.com/mail/"

            def is_closed(self):
                return False

        verification_states = iter([True, True, False])
        logs = []

        async def verification_required(_page):
            return next(verification_states)

        async def no_wait(*_args, **_kwargs):
            return None

        with patch.object(steam_outlook_batch, "outlook_forward_verification_required", verification_required), patch.object(
            steam_outlook_batch, "wait_for_optional_page", no_wait
        ):
            completed = asyncio.run(
                steam_outlook_batch.wait_for_outlook_forward_verification_commit(
                    object(), FakePopup(), logs.append, timeout_ms=1000
                )
            )

        self.assertTrue(completed)
        self.assertTrue(any("waiting for the rule editor to refresh" in line for line in logs))
        self.assertTrue(any("committed in the rule editor" in line for line in logs))

    def test_feishu_authorize_ocr_requires_exact_button_and_page_context(self):
        context = [
            {"text": "请求以下飞书账号进行授权", "score": 0.99},
            {"text": "可授予的权限（共 1 项）", "score": 0.99},
            {"text": "拒绝", "score": 0.99},
            {"text": "授权", "score": 0.99, "cx": 800, "cy": 500},
        ]
        self.assertEqual(steam_web_ui.select_feishu_authorize_ocr_item(context)["text"], "授权")
        self.assertIsNone(
            steam_web_ui.select_feishu_authorize_ocr_item(
                [{"text": "应用授权管理", "score": 0.99}, {"text": "授权", "score": 0.99}]
            )
        )
        source = inspect.getsource(steam_web_ui.auto_click_feishu_authorize)
        self.assertIn("pyautogui.screenshot", source)
        self.assertIn("RapidOCR", source)
        self.assertIn("pyautogui.click", source)
        self.assertIn("feishu_authorize_before.png", source)
        self.assertIn("click_feishu_authorize_control", source)
        self.assertIn("Feishu authorize action confirmed", inspect.getsource(steam_web_ui.click_feishu_authorize_control))
        self.assertIn("consent page", inspect.getsource(steam_web_ui.click_feishu_authorize_control))
        login_source = inspect.getsource(steam_outlook_batch.feishu_tool.run_lark_login)
        self.assertIn("180000", login_source)
        self.assertIn("LarkAuthHandlerLocal", login_source)
        self.assertNotIn("openModule", login_source)
        worker_source = inspect.getsource(steam_web_ui.feishu_login_worker)
        self.assertIn("webbrowser.open(line)", worker_source)

    def test_task3_dismisses_outlook_feedback_survey(self):
        dismiss_source = inspect.getsource(steam_outlook_batch.dismiss_outlook_nonbusiness_popup)
        create_source = inspect.getsource(steam_outlook_batch.create_or_update_outlook_forward_rule)
        recovery_source = inspect.getsource(steam_outlook_batch.is_recovery_email_setup_page)

        self.assertIn("how are we doing?", dismiss_source)
        self.assertIn('r"^Skip$"', dismiss_source)
        self.assertGreaterEqual(create_source.count("dismiss_outlook_nonbusiness_popup"), 3)
        self.assertIn("account.live.com/proofs/add", recovery_source)
        self.assertIn("让我们来保护你的帐户", recovery_source)

    def test_forward_rule_writeback_guard_requires_page_and_target(self):
        class FakeLocator:
            def __init__(self, text):
                self.text = text

            async def inner_text(self, timeout=0):
                return self.text

        class FakePage:
            def __init__(self, url, text):
                self.url = url
                self.text = text

            def locator(self, selector):
                return FakeLocator(self.text)

        async def run_case():
            await steam_outlook_batch.ensure_outlook_forward_rule_ready_for_writeback(
                FakePage("https://outlook.live.com/mail/options/mail/forwarding", "Forwarding Enable forwarding code@example.com saved"),
                "code@example.com",
                lambda message: None,
            )
            await steam_outlook_batch.ensure_outlook_forward_rule_ready_for_writeback(
                FakePage("https://outlook.live.com/mail/options/mail/forwarding", "Forwarding code@example.com Disable forwarding"),
                "code@example.com",
                lambda message: None,
            )
            await steam_outlook_batch.ensure_outlook_forward_rule_ready_for_writeback(
                FakePage("https://outlook.live.com/mail/options/mail/rules", "Rules Forward to code@example.com Rule is turned on"),
                "code@example.com",
                lambda message: None,
            )
            with self.assertRaisesRegex(RuntimeError, "不是 Outlook Rules/Forwarding"):
                await steam_outlook_batch.ensure_outlook_forward_rule_ready_for_writeback(
                    FakePage("https://outlook.live.com/mail/", "Inbox"),
                    "code@example.com",
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "未看到转发目标"):
                await steam_outlook_batch.ensure_outlook_forward_rule_ready_for_writeback(
                    FakePage("https://outlook.live.com/mail/options/mail/rules", "Rules Add new rule"),
                    "code@example.com",
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "不是 Outlook Rules/Forwarding"):
                await steam_outlook_batch.ensure_outlook_forward_rule_ready_for_writeback(
                    FakePage("https://outlook.live.com/mail/", "Inbox message mentions Rules and code@example.com"),
                    "code@example.com",
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "没有看到规则已启用/已保存状态"):
                await steam_outlook_batch.ensure_outlook_forward_rule_ready_for_writeback(
                    FakePage("https://outlook.live.com/mail/options/mail/forwarding", "Forwarding Enable forwarding code@example.com"),
                    "code@example.com",
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "没有看到规则已启用/已保存状态"):
                await steam_outlook_batch.ensure_outlook_forward_rule_ready_for_writeback(
                    FakePage("https://outlook.live.com/mail/options/mail/forwarding", "Forwarding Enable forwarding code@example.com Save 保存"),
                    "code@example.com",
                    lambda message: None,
                )

        asyncio.run(run_case())

    def test_browser_contexts_use_profile_fallback_with_logs(self):
        launcher_source = inspect.getsource(steam_outlook_batch.launch_persistent_context_with_fallback)
        run_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        security_source = inspect.getsource(steam_outlook_batch.connect_security_context)

        self.assertIn("using fallback profile", launcher_source)
        self.assertIn("Target Outlook", run_source)
        self.assertIn("launch_isolated_target_context", run_source)
        self.assertIn("launch_persistent_context_with_fallback", security_source)
        self.assertIn("log,", security_source)

    def test_outlook_retry_preserves_unaffected_security_session(self):
        run_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        reuse_source = inspect.getsource(steam_outlook_batch.try_reuse_email_scoped_outlook_session)

        self.assertIn("rebuilding only the affected Outlook context", run_source)
        self.assertIn("target_failed", run_source)
        self.assertIn("security_failed", run_source)
        self.assertNotIn('clear_email_scoped_outlook_session("security", security_context_email)', run_source)
        self.assertIn("time.monotonic() + 45", reuse_source)
        self.assertIn("redirect_microsoft_account_home_to_outlook", reuse_source)

    def test_security_context_instruction_matches_pool_mode(self):
        source = inspect.getsource(steam_outlook_batch.connect_security_context)

        self.assertIn("parse_security_mailbox_pool", source)
        self.assertIn("按当前飞书行", source)
        self.assertIn("不要固定停留在默认安全邮箱", source)
        self.assertIn("默认安全邮箱", source)

    def test_web_ui_reports_security_mailbox_shortage(self):
        source = inspect.getsource(steam_web_ui.Handler.do_POST)
        read_source = inspect.getsource(steam_outlook_batch.read_pending_accounts)

        self.assertIn('STATE["message"] = "安全邮箱：不够" if skipped_bind_limit else "安全邮箱：够"', source)
        self.assertNotIn("安全邮箱占用", source)
        self.assertNotIn("当前默认安全邮箱已绑定", source)
        self.assertIn("skipped_missing_required_fields", read_source)
        self.assertIn("skipped_missing_required_fields += 1", read_source)

    def test_web_ui_empty_queue_hints_list_required_fields(self):
        source = inspect.getsource(steam_web_ui.preflight)

        self.assertIn("Steam账号、Steam密码、邮箱账号、邮箱密码完整", source)
        self.assertIn("邮箱账号、邮箱密码完整", source)
        self.assertIn("已有 steam id、邮箱账号、邮箱密码完整", source)

    def test_web_ui_pending_signature_includes_execution_settings(self):
        base = steam_web_ui.build_args({"task_mode": "full", "security_code_source": "manual", "enable_dota_check": True})
        changed_code_source = steam_web_ui.build_args({"task_mode": "full", "security_code_source": "browser", "enable_dota_check": True})
        changed_forward = steam_web_ui.build_args({"task_mode": "forward_rule", "forward_to_email": "a@example.com"})
        changed_forward_target = steam_web_ui.build_args({"task_mode": "forward_rule", "forward_to_email": "b@example.com"})
        changed_forward_sender = steam_web_ui.build_args({"task_mode": "forward_rule", "forward_from_email": "sender@example.com", "forward_to_email": "a@example.com"})
        changed_dota = steam_web_ui.build_args({"task_mode": "full", "security_code_source": "manual", "enable_dota_check": False})

        self.assertNotEqual(steam_web_ui.args_signature(base), steam_web_ui.args_signature(changed_code_source))
        self.assertNotEqual(steam_web_ui.args_signature(changed_forward), steam_web_ui.args_signature(changed_forward_target))
        self.assertNotEqual(steam_web_ui.args_signature(changed_forward), steam_web_ui.args_signature(changed_forward_sender))
        self.assertNotEqual(steam_web_ui.args_signature(base), steam_web_ui.args_signature(changed_dota))

    def test_web_ui_security_mailbox_limited_state_is_not_overwritten_by_fatal(self):
        source = inspect.getsource(steam_web_ui.worker)

        self.assertIn("security_mailbox_limited_message", source)
        self.assertIn('STATE["status"] = "security mailbox limited"', source)
        self.assertIn('STATE["can_start"] = False', source)
        self.assertIn("补充安全邮箱账号密码后，必须点击“刷新队列”", source)
        self.assertIn("运行已中止", source)

    def test_preflight_warns_security_pool_missing_passwords_and_empty_pool(self):
        source = inspect.getsource(steam_web_ui.preflight)

        self.assertIn("浏览器收码需要能登录对应安全邮箱", source)
        self.assertIn("任务1和任务2使用安全邮箱池时必须填写每个安全邮箱密码", source)
        self.assertIn("安全邮箱池密码缺失", source)
        self.assertIn("未填写安全邮箱池，将使用默认安全邮箱", source)
        self.assertIn("当前使用安全邮箱池", source)
        self.assertIn("不是固定默认安全邮箱", source)

    def test_preflight_blocks_missing_security_pool_passwords_for_task1_and_task2(self):
        def run_case(mode):
            with steam_web_ui.STATE_LOCK:
                original = dict(steam_web_ui.STATE)
                steam_web_ui.STATE["pending_signature"] = "sig"
                steam_web_ui.STATE["pending"] = [object()]
            try:
                args = SimpleNamespace(
                    task_mode=mode,
                    security_code_source="manual",
                    security_mailbox_pool="missing@example.com",
                    bind_security_email="",
                    security_email="security@example.com",
                    feishu_config="steam_web_ui.py",
                    sheet_url="",
                    sheet_range="A1:K1000",
                    feishu_auth="user",
                    target_email="",
                    start_row=0,
                    limit=0,
                    blocked_security_mailboxes=[],
                    enable_steam_login=False,
                    enable_dota_check=False,
                    steam_exe="D:\\Steam\\steam.exe",
                )
                with patch.object(steam_web_ui, "args_signature", return_value="sig"):
                    return steam_web_ui.preflight(args)
            finally:
                with steam_web_ui.STATE_LOCK:
                    steam_web_ui.STATE.clear()
                    steam_web_ui.STATE.update(original)

        for mode in ["full", "mail_password"]:
            result = run_case(mode)
            self.assertFalse(result["can_start"])
            self.assertIn("安全邮箱池密码缺失", result["blockers"])

    def test_backend_start_validation_rejects_frontend_bypass_values(self):
        args = steam_web_ui.build_args({"task_mode": "full", "security_code_source": "manual"})
        args.steam_login_attempts = 0
        with self.assertRaisesRegex(RuntimeError, "Steam 登录重试次数"):
            steam_web_ui.validate_start_args(args)

        args = steam_web_ui.build_args({"task_mode": "full", "security_code_source": "manual"})
        args.dota_max_attempts = 99
        with self.assertRaisesRegex(RuntimeError, "Dota2 房间检测最多尝试次数"):
            steam_web_ui.validate_start_args(args)

        args = steam_web_ui.build_args({"task_mode": "full", "security_code_source": "manual"})
        args.start_row = 1
        with self.assertRaisesRegex(RuntimeError, "起始行"):
            steam_web_ui.validate_start_args(args)

        args = steam_web_ui.build_args({"task_mode": "forward_rule", "security_code_source": "browser"})
        with self.assertRaisesRegex(RuntimeError, "转发目标 Forward to"):
            steam_web_ui.validate_start_args(args)
        args.forward_to_email = "code@example.com"
        steam_web_ui.validate_start_args(args)

    def test_web_ui_dota_decision_buttons_explain_meaning(self):
        html = steam_web_ui.HTML

        self.assertIn("房间存在，标记大号", html)
        self.assertIn("房间不存在，不标记大号", html)
        self.assertIn("房间存在则判定为大号", html)
        self.assertIn("Dota2 房间检测大号", html)
        self.assertIn("绑定安全邮箱并写回 J 列", html)
        self.assertIn("任务1不修改邮箱密码", html)
        self.assertNotIn("当前 Steam 账号存在", html)
        self.assertIn('id="manual_decision"', html)
        self.assertIn('id="dota_found_button"', html)
        self.assertNotIn('data-action-section="manual"', html)

    def test_web_ui_dota_decisions_only_show_for_dota_room_wait(self):
        html = steam_web_ui.HTML
        worker_source = inspect.getsource(steam_web_ui.worker)

        self.assertIn('manualKind === "dota_room"', html)
        self.assertIn("等待确认 Dota2 房间是否存在", html)
        self.assertIn('"dota_room" if "房间存在" in message', worker_source)

    def test_web_ui_task_switch_keeps_security_settings_for_task3(self):
        html = steam_web_ui.HTML

        self.assertIn('data-common-section="security_verification"', html)
        self.assertIn("const visible = true", html)
        self.assertIn('data-section="full"', html)
        self.assertIn('data-section="mail_password"', html)
        self.assertIn('data-section="forward_rule"', html)

    def test_web_ui_form_changes_force_queue_refresh_before_start(self):
        html = steam_web_ui.HTML

        self.assertIn("let settingsDirty = true", html)
        self.assertIn("当前表单设置已变化，请先点击“刷新队列”", html)
        self.assertIn("settingsDirty || !canStart || currentRunning", html)
        self.assertIn("settingsDirty = false", html)
        self.assertIn("settingsDirty = true", html)
        self.assertIn("if (settingsDirty)", html)
        self.assertIn('document.querySelectorAll("input,select,textarea")', html)

    def test_web_ui_done_callback_does_not_create_fake_manual_wait(self):
        source = inspect.getsource(steam_web_ui.worker)
        done_block = source[source.index("def on_done") : source.index("def on_error")]
        finally_block = source[source.index("finally:") :]

        self.assertIn('STATE["waiting"] = False', done_block)
        self.assertIn('STATE["manual_kind"] = ""', done_block)
        self.assertNotIn('STATE["status"] = "waiting"', done_block)
        self.assertIn('STATE["waiting"] = False', finally_block)
        self.assertIn('STATE["manual_kind"] = ""', finally_block)

    def test_web_ui_stop_clears_manual_wait_and_logs(self):
        source = inspect.getsource(steam_web_ui.Handler.do_POST)
        stop_block = source[source.index('if path == "/api/stop":') :]

        self.assertIn('append_log("stop requested")', stop_block)
        self.assertIn('STATE["waiting"] = False', stop_block)
        self.assertIn('STATE["manual_kind"] = ""', stop_block)
        self.assertIn('STATE["status"] = "stopping"', stop_block)

    def test_web_ui_manual_action_apis_require_matching_wait_state(self):
        source = inspect.getsource(steam_web_ui.Handler.do_POST)
        continue_block = source[source.index('if path == "/api/continue":') : source.index('if path == "/api/dota-found":')]
        dota_found_block = source[source.index('if path == "/api/dota-found":') : source.index('if path == "/api/dota-not-found":')]
        dota_not_found_block = source[source.index('if path == "/api/dota-not-found":') : source.index('if path == "/api/stop":')]

        self.assertIn('STATE.get("waiting")', continue_block)
        self.assertIn('STATE.get("manual_kind") != "dota_room"', continue_block)
        self.assertIn("当前没有可继续的普通人工等待", continue_block)
        self.assertIn('STATE.get("manual_kind") == "dota_room"', dota_found_block)
        self.assertIn("当前没有等待 Dota2 房间确认", dota_found_block)
        self.assertIn('STATE.get("manual_kind") == "dota_room"', dota_not_found_block)
        self.assertIn("当前没有等待 Dota2 房间确认", dota_not_found_block)

    def test_web_ui_action_errors_are_visible_in_status_area(self):
        html = steam_web_ui.HTML
        call_action = html[html.index("async function callAction") : html.index("function localNotice")]

        self.assertIn("localError(err)", call_action)
        self.assertNotIn("alert(", call_action)
        success_block = call_action[call_action.index("try {") : call_action.index("} catch")]
        error_block = call_action[call_action.index("} catch") :]
        self.assertIn("refresh()", success_block)
        self.assertNotIn("refresh();", error_block)

    def test_web_ui_log_updates_recent_step_without_overriding_manual_wait(self):
        with steam_web_ui.STATE_LOCK:
            original = dict(steam_web_ui.STATE)
            steam_web_ui.STATE["running"] = True
            steam_web_ui.STATE["waiting"] = False
            steam_web_ui.STATE["status"] = "running"
            steam_web_ui.STATE["message"] = ""
            steam_web_ui.STATE["logs"] = []
        try:
            steam_web_ui.append_log("Steam login attempt 1")
            with steam_web_ui.STATE_LOCK:
                self.assertEqual("最近步骤：Steam login attempt 1", steam_web_ui.STATE["message"])
                steam_web_ui.STATE["waiting"] = True
                steam_web_ui.STATE["message"] = "等待人工确认"
            steam_web_ui.append_log("background detail")
            with steam_web_ui.STATE_LOCK:
                self.assertEqual("等待人工确认", steam_web_ui.STATE["message"])
        finally:
            with steam_web_ui.STATE_LOCK:
                steam_web_ui.STATE.clear()
                steam_web_ui.STATE.update(original)

    def test_web_ui_recent_step_uses_sanitized_log_message(self):
        with steam_web_ui.STATE_LOCK:
            original = dict(steam_web_ui.STATE)
            steam_web_ui.STATE["running"] = True
            steam_web_ui.STATE["waiting"] = False
            steam_web_ui.STATE["status"] = "running"
            steam_web_ui.STATE["message"] = ""
            steam_web_ui.STATE["logs"] = []
        try:
            steam_web_ui.append_log("请确认 a@example.com 的邮箱密码已成功改为：PlainSecret123。")
            with steam_web_ui.STATE_LOCK:
                message = steam_web_ui.STATE["message"]
                latest_log = steam_web_ui.STATE["logs"][-1]
            self.assertNotIn("PlainSecret123", message)
            self.assertNotIn("PlainSecret123", latest_log)
            self.assertIn("[hidden]", message)
        finally:
            with steam_web_ui.STATE_LOCK:
                steam_web_ui.STATE.clear()
                steam_web_ui.STATE.update(original)

    def test_web_ui_worker_error_state_uses_sanitized_exception_message(self):
        with steam_web_ui.STATE_LOCK:
            original = dict(steam_web_ui.STATE)
            steam_web_ui.STATE["logs"] = []
        account = SimpleNamespace(row_number=71, steam_username="user", email="target@example.com", backup_security_email="")

        def fake_run(args, callbacks):
            callbacks["on_account_error"](account, RuntimeError("邮箱密码已成功改为：PlainSecret123。"))

        try:
            with patch.object(steam_web_ui.steam_outlook_batch, "run_with_callbacks", side_effect=fake_run):
                steam_web_ui.worker(SimpleNamespace(task_mode="full"), steam_outlook_batch.PauseController())
            with steam_web_ui.STATE_LOCK:
                message = steam_web_ui.STATE["message"]
            self.assertNotIn("PlainSecret123", message)
            self.assertIn("[hidden]", message)
        finally:
            with steam_web_ui.STATE_LOCK:
                steam_web_ui.STATE.clear()
                steam_web_ui.STATE.update(original)

    def test_web_ui_worker_fatal_state_uses_sanitized_exception_message(self):
        with steam_web_ui.STATE_LOCK:
            original = dict(steam_web_ui.STATE)
            steam_web_ui.STATE["logs"] = []
        try:
            with patch.object(
                steam_web_ui.steam_outlook_batch,
                "run_with_callbacks",
                side_effect=RuntimeError("Microsoft security code for target@example.com: 123456"),
            ):
                steam_web_ui.worker(SimpleNamespace(task_mode="full"), steam_outlook_batch.PauseController())
            with steam_web_ui.STATE_LOCK:
                message = steam_web_ui.STATE["message"]
            self.assertNotIn("123456", message)
            self.assertIn("[hidden]", message)
        finally:
            with steam_web_ui.STATE_LOCK:
                steam_web_ui.STATE.clear()
                steam_web_ui.STATE.update(original)

    def test_web_ui_manual_wait_state_uses_sanitized_message(self):
        source = inspect.getsource(steam_web_ui.worker)
        manual_block = source[source.index("def on_manual_wait") : source.index("def on_manual_resume")]

        self.assertIn("safe_message = sanitize_log_message(message)", manual_block)
        self.assertIn('STATE["message"] = safe_message', manual_block)
        self.assertNotIn('STATE["message"] = message', manual_block)

    def test_web_ui_manual_wait_keeps_dota_room_decision_kind(self):
        source = inspect.getsource(steam_web_ui.worker)
        manual_block = source[source.index("def on_manual_wait") : source.index("def on_manual_resume")]

        self.assertIn('"dota_room" if "房间存在" in message and "标记大号" in message else "continue"', manual_block)
        self.assertNotIn('"房间存在" in safe_message', manual_block)

    def test_web_ui_start_and_pending_failures_are_sanitized(self):
        source = inspect.getsource(steam_web_ui.Handler.do_POST)
        pending_block = source[source.index('if path == "/api/pending":') : source.index('if path == "/api/start":')]
        start_block = source[source.index('if path == "/api/start":') : source.index('if path == "/api/debug":')]

        self.assertIn("safe_exc = sanitize_log_message(str(exc))", pending_block)
        self.assertIn('self._json({"ok": False, "error": safe_exc}', pending_block)
        self.assertIn("sanitize_log_message(str(exc))", start_block)
        self.assertIn("启动前检查失败", start_block)

    def test_web_ui_append_log_persists_to_run_log_file(self):
        original_path = steam_web_ui.WEB_UI_RUN_LOG
        original_max = steam_web_ui.WEB_UI_RUN_LOG_MAX_BYTES
        temp_path = Path("test_steam_web_ui_run.log")
        try:
            if temp_path.exists():
                temp_path.unlink()
            steam_web_ui.WEB_UI_RUN_LOG = temp_path
            steam_web_ui.WEB_UI_RUN_LOG_MAX_BYTES = original_max
            with steam_web_ui.STATE_LOCK:
                original = dict(steam_web_ui.STATE)
                steam_web_ui.STATE["logs"] = []
                steam_web_ui.STATE["running"] = False
            steam_web_ui.append_log("persistent log check")
            self.assertIn("persistent log check", temp_path.read_text(encoding="utf-8"))
            with steam_web_ui.STATE_LOCK:
                self.assertTrue(any("persistent log check" in line for line in steam_web_ui.STATE["logs"]))
        finally:
            steam_web_ui.WEB_UI_RUN_LOG = original_path
            steam_web_ui.WEB_UI_RUN_LOG_MAX_BYTES = original_max
            if temp_path.exists():
                temp_path.unlink()
            rotated = temp_path.with_suffix(temp_path.suffix + ".1")
            if rotated.exists():
                rotated.unlink()
            with steam_web_ui.STATE_LOCK:
                steam_web_ui.STATE.clear()
                steam_web_ui.STATE.update(original)

    def test_web_ui_run_log_rotates_and_is_gitignored(self):
        original_path = steam_web_ui.WEB_UI_RUN_LOG
        original_max = steam_web_ui.WEB_UI_RUN_LOG_MAX_BYTES
        temp_path = Path("test_steam_web_ui_rotate.log")
        rotated = temp_path.with_suffix(temp_path.suffix + ".1")
        try:
            for path in [temp_path, rotated]:
                if path.exists():
                    path.unlink()
            steam_web_ui.WEB_UI_RUN_LOG = temp_path
            steam_web_ui.WEB_UI_RUN_LOG_MAX_BYTES = 10
            temp_path.write_text("x" * 10, encoding="utf-8")
            steam_web_ui.append_log("after rotate")
            self.assertTrue(rotated.exists())
            self.assertIn("after rotate", temp_path.read_text(encoding="utf-8"))
            self.assertIn("*.log", Path(".gitignore").read_text(encoding="utf-8"))
        finally:
            steam_web_ui.WEB_UI_RUN_LOG = original_path
            steam_web_ui.WEB_UI_RUN_LOG_MAX_BYTES = original_max
            for path in [temp_path, rotated]:
                if path.exists():
                    path.unlink()

    def test_web_ui_append_log_redacts_passwords_and_codes(self):
        original_path = steam_web_ui.WEB_UI_RUN_LOG
        temp_path = Path("test_steam_web_ui_redact.log")
        try:
            if temp_path.exists():
                temp_path.unlink()
            steam_web_ui.WEB_UI_RUN_LOG = temp_path
            with steam_web_ui.STATE_LOCK:
                original = dict(steam_web_ui.STATE)
                steam_web_ui.STATE["logs"] = []
                steam_web_ui.STATE["running"] = False
            steam_web_ui.append_log("请确认 a@example.com 的邮箱密码已成功改为：Secret123!。")
            steam_web_ui.append_log("Submitting Microsoft security code for a@example.com: 123456")
            steam_web_ui.append_log("Steam Guard code found: A1B2C")
            written = temp_path.read_text(encoding="utf-8")
            self.assertNotIn("Secret123", written)
            self.assertNotIn("123456", written)
            self.assertNotIn("A1B2C", written)
            self.assertIn("[hidden]", written)
            with steam_web_ui.STATE_LOCK:
                joined = "\n".join(steam_web_ui.STATE["logs"])
            self.assertNotIn("Secret123", joined)
            self.assertNotIn("123456", joined)
            self.assertNotIn("A1B2C", joined)
        finally:
            steam_web_ui.WEB_UI_RUN_LOG = original_path
            if temp_path.exists():
                temp_path.unlink()
            with steam_web_ui.STATE_LOCK:
                steam_web_ui.STATE.clear()
                steam_web_ui.STATE.update(original)

    def test_backend_logs_do_not_emit_verification_codes(self):
        run_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        submit_source = inspect.getsource(steam_outlook_batch.submit_microsoft_security_code_with_validation)
        legacy_source = inspect.getsource(outlook_batch_login.run)

        self.assertIn('log("Steam Guard code found.")', run_source)
        self.assertNotIn("Steam Guard code found: {steam_code}", run_source)
        self.assertNotIn(": {code}", submit_source)
        self.assertNotIn(": {code}", legacy_source)

    def test_web_ui_sensitive_settings_file_is_gitignored(self):
        ignore = Path(".gitignore").read_text(encoding="utf-8")

        self.assertIn("steam_web_ui_settings.json", ignore)
        self.assertIn("*.log", ignore)

    def test_legacy_outlook_only_launcher_is_disabled(self):
        launcher = Path("run_outlook_batch_login.bat").read_text(encoding="utf-8")
        module_source = Path("outlook_batch_login.py").read_text(encoding="utf-8")

        self.assertIn("legacy Outlook-only launcher is disabled", launcher)
        self.assertIn("run_steam_web_ui.bat", launcher)
        self.assertNotIn("python outlook_batch_login.py", launcher)
        self.assertIn("legacy Outlook-only entry point is disabled", module_source)
        self.assertIn("raise SystemExit(main())", module_source)
        self.assertNotIn("asyncio.run(run(parse_args()))", module_source)

    def test_graph_docs_are_marked_legacy_not_web_ui_flow(self):
        doc = Path("GRAPH_MAIL.md").read_text(encoding="utf-8")
        error_source = inspect.getsource(__import__("graph_mail").load_client_id)

        self.assertIn("历史独立工具说明", doc)
        self.assertIn("三个任务流程不再使用 Microsoft Graph", doc)
        self.assertIn("功能3已经改为 Outlook 转发规则方案", doc)
        self.assertNotIn("Web UI 选择 `Microsoft Graph 多邮箱`", doc)
        self.assertIn("legacy standalone Graph tool", error_source)
        self.assertIn("Web UI task flows no longer use Graph", error_source)

    def test_web_ui_debug_version_was_bumped(self):
        self.assertEqual("2026-07-10-persistent-staged-flow", steam_web_ui.APP_VERSION)

    def test_web_ui_no_queue_hint_is_mode_specific(self):
        source = inspect.getsource(steam_web_ui.preflight)

        self.assertIn("任务1读取 steam id 为空", source)
        self.assertIn("功能2读取邮箱账号、邮箱密码完整", source)
        self.assertIn("功能3读取已有 steam id", source)

    def test_web_ui_function2_has_no_misleading_enable_toggle(self):
        html = steam_web_ui.HTML

        self.assertIn("绑定安全邮箱 / 修改邮箱密码（功能2固定执行）", html)
        self.assertIn('id="enable_mail_maintenance" type="hidden"', html)
        self.assertNotIn("<label>邮箱安全维护</label><select", html)

    def test_dota_manual_prompt_is_about_room_existence(self):
        source = inspect.getsource(dota2_arcade.Dota2Automation.search_room)

        self.assertIn("房间存在", source)
        self.assertIn("标记大号", source)
        self.assertIn("save_debug_screenshot", source)
        self.assertIn("room_decision_after", source)
        self.assertIn("OCR after decision", source)
        self.assertIn("final_visible", inspect.getsource(dota2_arcade.Dota2Automation.click_text))
        self.assertIn('decision == "stop"', source)
        self.assertIn("Stopped during Dota room existence confirmation", source)
        self.assertNotIn("Dota 找到了", source)

    def test_dota_screenshots_are_scoped_to_current_row(self):
        init_source = inspect.getsource(dota2_arcade.Dota2Automation.__init__)
        batch_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)

        self.assertIn("screenshot_path: Path | None = None", init_source)
        self.assertIn("_dota_room.png", batch_source)
        self.assertIn("safe_artifact_name(account.steam_username)", batch_source)
        self.assertIn("screenshot_path=dota_screenshot", batch_source)

    def test_dota_arcade_hard_failures_enter_manual_recovery(self):
        source = inspect.getsource(dota2_arcade.Dota2Automation.open_arcade_and_game)
        recovery_source = inspect.getsource(dota2_arcade.Dota2Automation.wait_for_manual_recovery)

        self.assertIn("arcade game search result not found", source)
        self.assertIn("wait_for_manual_recovery", source)
        self.assertIn("save_debug_screenshot", recovery_source)
        self.assertIn("ocr_summary", recovery_source)
        self.assertIn("已完成人工操作 / 继续", recovery_source)
        self.assertIn("Stopped during Dota manual recovery", recovery_source)
        self.assertIn("_after", recovery_source)
        self.assertIn("OCR after recovery", recovery_source)
        self.assertIn("save_debug_screenshot_to", inspect.getsource(dota2_arcade.Dota2Automation.save_debug_screenshot))

    def test_dota_run_check_has_stop_checks_between_major_steps(self):
        source = inspect.getsource(dota2_arcade.Dota2Automation.run_check)
        helper_source = inspect.getsource(dota2_arcade.Dota2Automation.raise_if_stopped)
        click_source = inspect.getsource(dota2_arcade.Dota2Automation.click_text)
        search_source = inspect.getsource(dota2_arcade.Dota2Automation.search_room)
        wait_window_source = inspect.getsource(dota2_arcade.Dota2Automation.wait_for_window)
        sleep_source = inspect.getsource(dota2_arcade.Dota2Automation.sleep_with_stop)

        self.assertIn("raise_if_stopped", source)
        self.assertIn("Stopped during Dota automation", helper_source)
        self.assertIn("raise_if_stopped", click_source)
        self.assertIn("sleep_with_stop", click_source)
        self.assertIn("before room list refresh", search_source)
        self.assertIn("waiting after room list refresh", search_source)
        self.assertIn("waiting for Dota 2 window", wait_window_source)
        self.assertIn("time.sleep(min(0.5", sleep_source)

    def test_task1_logs_when_dota_check_is_disabled(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)

        self.assertIn("Dota check skipped because Dota verification is disabled.", source)
        self.assertIn("Dota room not confirmed", source)
        self.assertIn("big-account flag was not written", source)

    def test_account_error_saves_target_and_security_screenshots(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)

        self.assertIn("log_error_page_state(\"Target Outlook\"", source)
        self.assertIn("log_error_page_state(\"Security mailbox\"", source)
        self.assertIn("page state at failure", inspect.getsource(steam_outlook_batch.log_error_page_state))
        self.assertIn("save_page_screenshot(outlook_page", source)
        self.assertIn("save_page_screenshot(security_page", source)
        self.assertIn("_target.png", source)
        self.assertIn("_security.png", source)

    def test_dota_launch_timeout_enters_manual_recovery(self):
        launch_source = inspect.getsource(dota2_arcade.Dota2Automation.launch)
        recovery_source = inspect.getsource(dota2_arcade.Dota2Automation.wait_for_launch_recovery)

        self.assertIn("except TimeoutError", launch_source)
        self.assertIn("wait_for_launch_recovery", launch_source)
        self.assertIn("Dota2 启动后没有找到可用窗口", recovery_source)
        self.assertIn("manual_wait", recovery_source)
        self.assertIn("Stopped during Dota launch recovery", recovery_source)

    def test_browser_contexts_close_in_finally(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        finally_block = source[source.index("finally:") :]

        self.assertIn("outlook_context = None", source)
        self.assertIn("security_context = None", source)
        self.assertIn("await outlook_context.close()", finally_block)
        self.assertIn("await security_context.close()", finally_block)
        self.assertIn("Target Outlook context closed.", finally_block)
        self.assertIn("Security mailbox context closed.", finally_block)

    def test_outlook_login_prefers_password_option_before_password_fill(self):
        source = inspect.getsource(outlook_batch_login.fill_login)
        prefer_source = inspect.getsource(outlook_batch_login.prefer_password_sign_in)

        fido_cancel_index = source.index("await cancel_microsoft_fido_bridge(page, pause_controller)")
        first_prefer_index = source.index("await prefer_password_sign_in(page, pause_controller)")
        password_fill_index = source.index("account.password")
        retry_prefer_index = source.index("if not password_filled and await prefer_password_sign_in(page, pause_controller):")

        self.assertLess(fido_cancel_index, first_prefer_index)
        self.assertLess(first_prefer_index, password_fill_index)
        self.assertGreater(retry_prefer_index, password_fill_index)
        self.assertIn("Sign in another way", prefer_source)
        self.assertIn("Other ways to sign in", prefer_source)
        self.assertIn("选择其他验证方式", prefer_source)
        self.assertIn("wait_for_timeout_maybe_stop", source)
        self.assertIn("pause_controller.should_stop()", inspect.getsource(outlook_batch_login.wait_for_timeout_maybe_stop))

    def test_fido_bridge_uses_microsoft_cancel_url(self):
        class FakePage:
            def __init__(self):
                self.url = (
                    "https://login.live.com/bridge/fido?"
                    "cancelUrl=https%3A%2F%2Flogin.live.com%2Fppsecure%2Fpost.srf%3Fcontextid%3Dabc"
                )
                self.visited = []

            async def goto(self, url, wait_until=None):
                self.visited.append((url, wait_until))

            async def go_back(self, wait_until=None):
                raise AssertionError("valid Microsoft cancelUrl must be used")

            async def wait_for_timeout(self, milliseconds):
                return None

        page = FakePage()
        result = asyncio.run(
            outlook_batch_login.cancel_microsoft_fido_bridge(page, timeout_seconds=1)
        )

        self.assertTrue(result)
        self.assertEqual("domcontentloaded", page.visited[0][1])
        self.assertTrue(page.visited[0][0].startswith("https://login.live.com/ppsecure/post.srf"))

    def test_new_microsoft_consumer_fido_route_goes_back(self):
        class FakePage:
            def __init__(self):
                self.url = "https://login.microsoft.com/consumers/fido/get?mkt=ZH-CN"
                self.went_back = False

            async def go_back(self, wait_until=None):
                self.went_back = wait_until == "domcontentloaded"

            async def wait_for_timeout(self, milliseconds):
                return None

        page = FakePage()
        result = asyncio.run(
            outlook_batch_login.cancel_microsoft_fido_bridge(page, timeout_seconds=1)
        )

        self.assertTrue(result)
        self.assertTrue(page.went_back)

    def test_consumer_fido_route_prefers_password_from_more_options(self):
        source = inspect.getsource(outlook_batch_login.cancel_microsoft_fido_bridge)
        consumer_index = source.index('"/consumers/fido/"')
        more_index = source.index("more_options = page.locator", consumer_index)
        password_index = source.index("prefer_password_sign_in(page", more_index)
        back_index = source.index("page.go_back", password_index)
        self.assertLess(more_index, password_index)
        self.assertLess(password_index, back_index)
        self.assertIn("visible_buttons.last.evaluate", source)

    def test_login_submit_does_not_wait_behind_native_fido_dialog(self):
        source = inspect.getsource(outlook_batch_login.click_submit)

        self.assertIn('"element => element.click()"', source)
        self.assertNotIn(".first.click(", source)
        fill_source = inspect.getsource(outlook_batch_login.fill_login)
        submit_index = fill_source.index("await click_submit(page)")
        cancel_index = fill_source.index("await cancel_microsoft_fido_bridge", submit_index)
        self.assertGreater(cancel_index, submit_index)

    def test_outlook_contexts_cancel_webauthn_before_native_dialog(self):
        module_source = inspect.getsource(steam_outlook_batch)
        installer_source = inspect.getsource(outlook_batch_login.install_webauthn_cancel)
        script = outlook_batch_login.WEBAUTHN_CANCEL_SCRIPT
        self.assertGreaterEqual(module_source.count("install_webauthn_cancel"), 4)
        self.assertIn("add_init_script", installer_source)
        self.assertIn("options.publicKey", script)
        self.assertIn("NotAllowedError", script)
        self.assertNotIn("--disable-features=WebAuthentication", module_source)

    def test_fido_cancel_reenters_account_when_microsoft_returns_to_email_page(self):
        source = inspect.getsource(outlook_batch_login.fill_login)
        cancel_index = source.index("fido_cancelled = await cancel_microsoft_fido_bridge")
        refill_index = source.index("email_filled_after_cancel = await fill_first", cancel_index)
        resubmit_index = source.index("await click_submit(page)", refill_index)
        self.assertLess(cancel_index, refill_index)
        self.assertLess(refill_index, resubmit_index)

    def test_outlook_login_handles_microsoft_account_chooser_before_email_input(self):
        source = inspect.getsource(outlook_batch_login.fill_login)

        chooser_index = source.index("use_another_account_patterns")
        first_email_index = source.index("email_filled = await fill_first")
        chooser_click_index = source.index("click_first(page, use_another_account_patterns")
        second_email_index = source.index("timeout_ms=10000", chooser_click_index)

        self.assertLess(chooser_index, first_email_index)
        self.assertGreater(chooser_click_index, first_email_index)
        self.assertGreater(second_email_index, chooser_click_index)
        self.assertIn("Use another account", source)
        self.assertIn("使用其他账户", source)

    def test_outlook_login_uses_stable_microsoft_input_attributes(self):
        source = inspect.getsource(outlook_batch_login.fill_login)

        self.assertIn("page.goto(OUTLOOK_MAIL_URL", source)
        self.assertIn("page.goto(LOGIN_URL", source)
        self.assertIn("Microsoft login fallback navigation", source)
        self.assertIn("input[autocomplete=username]", source)
        self.assertIn("input[inputmode=email]", source)
        self.assertIn("input[name=login]", source)
        self.assertIn("input[autocomplete=current-password]", source)

    def test_target_mail_login_prefers_password_before_security_code_challenge(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        login_index = source.index("await fill_login(outlook_page, account, pause_controller)")
        prefer_index = source.index("prefer_password_sign_in(outlook_page, pause_controller)", login_index)
        challenge_index = source.index("security_challenge_visible(outlook_page)", login_index)
        blocking_index = source.index("handle_microsoft_blocking_page(", challenge_index)

        self.assertLess(prefer_index, challenge_index)
        self.assertGreater(blocking_index, challenge_index)
        self.assertIn("_manual_security.png", source)
        self.assertIn("page=outlook_page", source)

    def test_microsoft_security_challenge_detection_requires_verification_context(self):
        class FakeLocator:
            def __init__(self, text):
                self.text = text

            async def inner_text(self, timeout=0):
                return self.text

        class FakePage:
            def __init__(self, text, url="https://login.live.com/"):
                self.text = text
                self.url = url

            def locator(self, selector):
                return FakeLocator(self.text)

        async def run_case():
            self.assertFalse(
                await outlook_batch_login.security_challenge_visible(
                    FakePage("Sign in Enter your email address to continue")
                )
            )
            self.assertTrue(
                await outlook_batch_login.security_challenge_visible(
                    FakePage("Verify your identity. Enter the full email address to receive a security code.")
                )
            )
            self.assertTrue(
                await outlook_batch_login.security_challenge_visible(
                    FakePage("验证你的身份。请输入备用电子邮件地址以接收安全代码。")
                )
            )
            self.assertFalse(
                await outlook_batch_login.security_challenge_visible(
                    FakePage(
                        "验证你的身份。请输入备用电子邮件地址以接收安全代码。",
                        "https://account.microsoft.com/account?pageName=Account",
                    )
                )
            )

        asyncio.run(run_case())

    def test_microsoft_account_home_redirects_to_outlook_inbox(self):
        class FakePage:
            url = "https://account.microsoft.com/account?pageName=Account"

            async def goto(self, url, wait_until=None):
                self.url = url

            async def wait_for_timeout(self, milliseconds):
                return None

        logs = []
        page = FakePage()
        redirected = asyncio.run(
            steam_outlook_batch.redirect_microsoft_account_home_to_outlook(page, logs.append)
        )

        self.assertTrue(redirected)
        self.assertEqual(outlook_batch_login.OUTLOOK_MAIL_URL, page.url)
        self.assertTrue(any("account home page" in message for message in logs))

    def test_microsoft_account_root_redirects_but_account_checkup_does_not(self):
        class FakePage:
            def __init__(self, url):
                self.url = url

            async def goto(self, url, wait_until=None):
                self.url = url

            async def wait_for_timeout(self, milliseconds):
                return None

        root = FakePage("https://account.microsoft.com/?lang=zh-CN")
        checkup = FakePage("https://account.microsoft.com/account-checkup?launchMethod=autoLaunch")

        self.assertTrue(asyncio.run(steam_outlook_batch.redirect_microsoft_account_home_to_outlook(root)))
        self.assertEqual(outlook_batch_login.OUTLOOK_MAIL_URL, root.url)
        self.assertFalse(asyncio.run(steam_outlook_batch.redirect_microsoft_account_home_to_outlook(checkup)))

    def test_closed_browser_context_retries_current_row_once(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        classifier = inspect.getsource(steam_outlook_batch.is_browser_context_closed_error)
        retry_classifier = inspect.getsource(steam_outlook_batch.is_outlook_context_retryable_error)

        self.assertIn("browser_retry_counts", source)
        self.assertIn("retrying this row once", source)
        self.assertIn("continue", source[source.index("retrying this row once"):])
        self.assertIn("target page, context or browser has been closed", classifier)
        self.assertIn("could not verify the active outlook mailbox", retry_classifier)
        self.assertIn("could not find microsoft password input", retry_classifier)

    def test_security_mailbox_assignment_change_signs_out_then_uses_isolated_context(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        isolation = inspect.getsource(steam_outlook_batch.launch_isolated_security_context)
        changed = source.index("Security mailbox assignment changed to")
        logout = source.index("await logout_outlook(security_page", changed)
        close = source.index("await security_context.close()", logout)
        isolated = source.index("launch_isolated_security_context(", close)
        login = source.index("ensure_security_mailbox_for_account(", isolated)

        self.assertLess(logout, close)
        self.assertLess(close, isolated)
        self.assertLess(isolated, login)
        self.assertIn('email_scoped_outlook_profile("security", email)', isolation)
        self.assertNotIn("tempfile.mkdtemp", isolation)

    def test_submit_security_email_does_not_target_generic_text_inputs(self):
        source = inspect.getsource(outlook_batch_login.submit_security_email)

        self.assertNotIn('"input[type=text]"', source)
        self.assertIn("outlook.live.com/mail", source)
        self.assertIn("input[name*='proof' i]", source)
        self.assertIn("input[name*='email' i]", source)
        self.assertIn("input[placeholder*='email' i]", source)

    def test_security_challenge_detection_rejects_outlook_search_shell(self):
        source = inspect.getsource(outlook_batch_login.security_challenge_visible)
        self.assertIn("outlook.live.com/mail", source)
        request_source = inspect.getsource(steam_outlook_batch.request_microsoft_security_email_code)
        self.assertIn("target page is an Outlook mailbox/search page", request_source)

        class MailShell:
            url = "https://outlook.live.com/mail/"

            def __init__(self):
                self.locator_called = False

            def locator(self, *_args, **_kwargs):
                self.locator_called = True
                raise AssertionError("Outlook search shell must not be inspected as a security challenge")

        page = MailShell()
        self.assertFalse(asyncio.run(outlook_batch_login.security_challenge_visible(page)))
        self.assertFalse(page.locator_called)

    def test_microsoft_six_box_code_input_fills_each_digit(self):
        class FakeInput:
            def __init__(self, values, index=None):
                self.values = values
                self.index = index

            async def get_attribute(self, _name, timeout=0):
                return ""

            async def fill(self, value, timeout=0):
                self.values[self.index] = value

        class FakeInputs:
            def __init__(self):
                self.values = [""] * 6

            async def count(self):
                return 6

            def nth(self, index):
                return FakeInput(self.values, index)

        class FakePage:
            def __init__(self):
                self.inputs = FakeInputs()

            def locator(self, _selector):
                return self.inputs

        page = FakePage()
        async def no_click(*_args, **_kwargs):
            return None

        async def no_wait(*_args, **_kwargs):
            return None

        with patch.object(outlook_batch_login, "click_submit", new=no_click), patch.object(
            outlook_batch_login, "wait_for_timeout_maybe_stop", new=no_wait
        ):
            asyncio.run(outlook_batch_login.submit_code(page, "123456"))
        self.assertEqual(["1", "2", "3", "4", "5", "6"], page.inputs.values)

    def test_microsoft_three_box_code_input_fills_two_characters_per_box(self):
        class FakeInput:
            def __init__(self, values, index=None):
                self.values = values
                self.index = index

            async def get_attribute(self, _name, timeout=0):
                return ""

            async def fill(self, value, timeout=0):
                self.values[self.index] = value

        class FakeInputs:
            def __init__(self):
                self.values = [""] * 3

            async def count(self):
                return 3

            def nth(self, index):
                return FakeInput(self.values, index)

        class FakePage:
            def __init__(self):
                self.inputs = FakeInputs()

            def locator(self, _selector):
                return self.inputs

            def is_closed(self):
                return False

        page = FakePage()
        async def no_click(*_args, **_kwargs):
            return None

        async def no_wait(*_args, **_kwargs):
            return None

        with patch.object(outlook_batch_login, "click_submit", new=no_click), patch.object(
            outlook_batch_login, "wait_for_timeout_maybe_stop", new=no_wait
        ):
            asyncio.run(outlook_batch_login.submit_code(page, "123456"))
        self.assertEqual(["12", "34", "56"], page.inputs.values)

    def test_manual_wait_includes_page_state_and_optional_screenshot(self):
        source = inspect.getsource(steam_outlook_batch.wait_for_manual_continue)

        self.assertIn("page_state_summary", source)
        self.assertIn("save_page_screenshot", source)
        self.assertIn("当前页面", source)
        self.assertIn("Manual action confirmed; current page after user action", source)
        self.assertIn("_after", source)
        self.assertIn("after manual confirmation", source)

    def test_blocking_page_manual_recovery_saves_screenshot_by_default(self):
        microsoft_source = inspect.getsource(steam_outlook_batch.handle_microsoft_blocking_page)
        outlook_source = inspect.getsource(steam_outlook_batch.handle_outlook_blocking_page)

        self.assertIn("microsoft_blocking_", microsoft_source)
        self.assertIn("outlook_blocking_", outlook_source)
        self.assertIn("safe_artifact_name(account_label)", microsoft_source)
        self.assertIn("safe_artifact_name(account_label)", outlook_source)
        self.assertIn("screenshot_path=screenshot_path", microsoft_source)
        self.assertIn("screenshot_path=screenshot_path", outlook_source)

    def test_microsoft_writeback_guard_blocks_high_risk_pages(self):
        class FakeLocator:
            async def inner_text(self, timeout=0):
                return "Your security info change is still pending. You can't access this site right now."

        class FakePage:
            url = "https://account.live.com/proofs/manage/additional"

            def locator(self, selector):
                return FakeLocator()

        async def run_case():
            with self.assertRaisesRegex(RuntimeError, "不能写回"):
                await steam_outlook_batch.ensure_microsoft_page_allows_writeback(FakePage(), "绑定安全邮箱", lambda message: None)

        asyncio.run(run_case())

    def test_microsoft_writeback_guard_allows_non_high_risk_prompt_with_log(self):
        class FakeLocator:
            async def inner_text(self, timeout=0):
                return "Add a new way to sign in or verify"

        class FakePage:
            url = "https://account.live.com/proofs/manage/additional"

            def locator(self, selector):
                return FakeLocator()

        logs = []

        async def run_case():
            await steam_outlook_batch.ensure_microsoft_page_allows_writeback(FakePage(), "绑定安全邮箱", logs.append)

        asyncio.run(run_case())
        self.assertTrue(any("writeback check saw Microsoft page" in line for line in logs))

    def test_security_binding_writeback_guard_requires_microsoft_security_page(self):
        class FakeLocator:
            def __init__(self, text):
                self.text = text

            async def inner_text(self, timeout=0):
                return self.text

        class FakePage:
            def __init__(self, url, text):
                self.url = url
                self.text = text

            def locator(self, selector):
                return FakeLocator(self.text)

        async def run_case():
            await steam_outlook_batch.ensure_security_binding_ready_for_writeback(
                FakePage("https://account.live.com/proofs/manage/additional", "Security info Ways to prove who you are Email backup@example.com"),
                "backup@example.com",
                lambda message: None,
            )
            with self.assertRaisesRegex(RuntimeError, "不是 Microsoft 账号安全页面"):
                await steam_outlook_batch.ensure_security_binding_ready_for_writeback(
                    FakePage("https://outlook.live.com/mail/", "Inbox"),
                    "backup@example.com",
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "没有安全信息/验证方式标记"):
                await steam_outlook_batch.ensure_security_binding_ready_for_writeback(
                    FakePage("https://account.live.com/", "Account home"),
                    "backup@example.com",
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "仍在等待安全邮箱验证码确认"):
                await steam_outlook_batch.ensure_security_binding_ready_for_writeback(
                    FakePage("https://account.live.com/proofs/manage/additional", "Security info Enter the security code we sent to backup@example.com"),
                    "backup@example.com",
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "未看到完整或掩码形式的安全邮箱"):
                await steam_outlook_batch.ensure_security_binding_ready_for_writeback(
                    FakePage("https://account.live.com/proofs/manage/additional", "Security info Add a new way to sign in or verify"),
                    "backup@example.com",
                    lambda message: None,
                )

        asyncio.run(run_case())

    def test_security_binding_writeback_guard_logs_masked_email_case(self):
        class FakeLocator:
            async def inner_text(self, timeout=0):
                return "Security info Ways to prove who you are Email b*****@example.com"

        class FakePage:
            url = "https://account.live.com/proofs/manage/additional"

            def locator(self, selector):
                return FakeLocator()

        logs = []

        async def run_case():
            await steam_outlook_batch.ensure_security_binding_ready_for_writeback(FakePage(), "backup@example.com", logs.append)

        asyncio.run(run_case())
        self.assertTrue(any("没有在页面文本中看到完整邮箱" in line for line in logs))

    def test_masked_security_email_detection_matches_target_domain(self):
        self.assertTrue(steam_outlook_batch.masked_security_email_present("backup@example.com", "Security info b*****@example.com"))
        self.assertTrue(steam_outlook_batch.masked_security_email_present("backup@example.com", "Security info *****@example.com"))
        self.assertFalse(steam_outlook_batch.masked_security_email_present("backup@example.com", "Security info b*****@other.com"))

    def test_password_change_writeback_guard_requires_microsoft_account_success_state(self):
        class FakeLocator:
            def __init__(self, text):
                self.text = text

            async def inner_text(self, timeout=0):
                return self.text

        class FakePage:
            def __init__(self, url, text):
                self.url = url
                self.text = text

            def locator(self, selector):
                return FakeLocator(self.text)

        async def run_case():
            await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                FakePage("https://account.live.com/password/Change", "Your password has been changed."),
                lambda message: None,
            )
            await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                FakePage("https://account.microsoft.com/security", "Security dashboard recent activity"),
                lambda message: None,
            )
            with self.assertRaisesRegex(RuntimeError, "不是 Microsoft 账号页面"):
                await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                    FakePage("https://outlook.live.com/mail/", "Inbox"),
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "改密表单或改密失败"):
                await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                    FakePage("https://account.live.com/password/Change", "Enter your current password and new password."),
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "改密表单或改密失败"):
                await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                    FakePage("https://account.live.com/password/Change", "更改密码 当前密码 新密码 保存"),
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "改密表单或改密失败"):
                await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                    FakePage("https://account.live.com/password/Change", "Incorrect password. Enter your current password and new password."),
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "改密表单或改密失败"):
                await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                    FakePage("https://account.live.com/password/Change", "Password does not meet password requirements. Try a different password."),
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "等待 Microsoft 身份验证或安全验证码"):
                await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                    FakePage("https://login.live.com/", "Verify your identity. Enter the security code we sent to your email."),
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "等待 Microsoft 身份验证或安全验证码"):
                await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                    FakePage("https://login.live.com/", "验证你的身份。输入验证码。"),
                    lambda message: None,
                )
            with self.assertRaisesRegex(RuntimeError, "改密表单或改密失败"):
                await steam_outlook_batch.ensure_password_change_ready_for_writeback(
                    FakePage("https://account.microsoft.com/security", "Security dashboard recent activity Change password"),
                    lambda message: None,
                )

        asyncio.run(run_case())

    def test_browser_security_code_uses_per_row_security_mailbox_before_submit(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        challenge_index = source.index("challenge_security_email = account.backup_security_email or args.security_email")
        ensure_index = source.index("ensure_security_mailbox_for_account(", challenge_index)
        submit_index = source.index("request_microsoft_security_email_code(", challenge_index)

        self.assertLess(ensure_index, submit_index)
        self.assertIn("Security mailbox pool is enabled", source)
        self.assertIn("an isolated mailbox context will be opened for the first assigned row", source)
        self.assertIn("else:\n                    security_context, security_page = await connect_security_context", source)

    def test_task1_prepares_security_mailbox_before_target_login_and_recovery_binding(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        row_start = source.index("security_binding_completed = not account.security_binding_required")
        security_login = source.index("ensure_security_mailbox_for_account(", row_start)
        target_login = source.index("await fill_login(outlook_page, account, pause_controller)", row_start)
        recovery_setup = source.index("complete_recovery_email_setup(", target_login)

        self.assertLess(security_login, target_login)
        self.assertGreater(recovery_setup, target_login)
        self.assertIn("security_binding_completed=security_binding_completed", source)

    def test_microsoft_account_checkup_is_recognized_as_recovery_email_setup(self):
        class FakeLocator:
            async def inner_text(self, timeout=0):
                return "你有挂起的安全操作 添加恢复电子邮件地址 立即添加"

        class FakePage:
            url = "https://account.microsoft.com/account-checkup?launchMethod=autoLaunch"

            def locator(self, _selector):
                return FakeLocator()

        self.assertTrue(asyncio.run(steam_outlook_batch.is_recovery_email_setup_page(FakePage())))

    def test_security_email_delivery_failure_is_polled_after_submit(self):
        source = inspect.getsource(steam_outlook_batch.submit_security_email_or_record_failure)

        self.assertIn("deadline = time.monotonic() + 10", source)
        self.assertIn("detect_security_email_delivery_failure", source)
        self.assertIn("wait_for_timeout_with_stop(page, 1000", source)
        self.assertIn("pause_controller", source)

    def test_browser_security_code_mailbox_search_handles_blocking_pages(self):
        source = inspect.getsource(steam_outlook_batch.get_microsoft_security_code_from_mailbox)
        run_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        legacy_source = inspect.getsource(outlook_batch_login.get_security_code)

        self.assertIn("page_state_summary", source)
        self.assertIn("handle_microsoft_blocking_page", source)
        self.assertIn("Microsoft security code not found yet", source)
        self.assertIn("Stopped while waiting for Microsoft security code", source)
        self.assertIn("get_microsoft_security_code_from_mailbox", run_source)
        self.assertNotIn("from outlook_batch_login import get_security_code", run_source)
        self.assertIn("open_latest_microsoft_security_mail", legacy_source)
        self.assertIn("extract_security_code_from_open_message", legacy_source)
        extraction_source = inspect.getsource(outlook_batch_login.read_open_outlook_message_texts)
        self.assertIn("page.frames", extraction_source)
        self.assertIn("[role=document]", extraction_source)

    def test_microsoft_security_code_search_queries_are_not_bare_terms(self):
        source = inspect.getsource(outlook_batch_login.get_security_code)
        open_source = inspect.getsource(outlook_batch_login.open_latest_microsoft_security_mail)
        gate_source = inspect.getsource(outlook_batch_login.should_accept_microsoft_security_mail_row)
        query_block = source[source.index("queries = [") : source.index("while time.monotonic() < deadline")]

        self.assertIn("accountprotection.microsoft.com", query_block)
        self.assertIn("isread:no", query_block)
        self.assertEqual(2, query_block.count("accountprotection.microsoft.com"))
        self.assertNotIn('"security code",', query_block)
        self.assertNotIn('"安全代码",', query_block)
        self.assertIn("not_before_epoch", source)
        self.assertIn("row_epoch < not_before_epoch", gate_source)

    def test_microsoft_security_code_extractor_requires_fixed_context(self):
        self.assertIsNone(outlook_batch_login.extract_code("Order number 123456"))
        self.assertIsNone(outlook_batch_login.extract_code("Use code 123456 for a coupon"))
        self.assertIsNone(outlook_batch_login.extract_code("Microsoft account page. Your security code is 654321"))
        self.assertEqual("123456", outlook_batch_login.extract_code("Microsoft account security code: 123456"))
        self.assertEqual("654321", outlook_batch_login.extract_code("Your Microsoft security code is 654321"))
        self.assertEqual("112233", outlook_batch_login.extract_code("Microsoft 安全代码：112233"))
        self.assertEqual(
            "778899",
            outlook_batch_login.extract_code("Microsoft 帐户安全团队：你的验证代码是 778899。"),
        )

    def test_microsoft_security_mail_time_filter_respects_relative_days(self):
        now = time.mktime((2026, 7, 10, 10, 15, 0, 0, 0, -1))
        yesterday = outlook_batch_login.parse_mail_row_time("昨天 23:59", now)
        day_before = outlook_batch_login.parse_mail_row_time("前天 23:59", now)
        numeric_date = outlook_batch_login.parse_mail_row_time("2026/07/09 23:59", now)

        self.assertIsNotNone(yesterday)
        self.assertIsNotNone(day_before)
        self.assertIsNotNone(numeric_date)
        self.assertLess(yesterday, now)
        self.assertLess(day_before, yesterday)
        self.assertEqual(yesterday, numeric_date)

    def test_microsoft_security_mail_row_uses_real_sender_subject_mask_and_code_shape(self):
        real_row_shape = (
            "未读 Microsoft 帐户团队 个人 Microsoft 帐户安全代码 17:06 "
            "Microsoft 帐户 安全代码 请对个人 Microsoft 帐户 ma**z@hotmail.com "
            "使用以下安全代码。 安全代码: 123456 仅在官方网站或应用上输入此代码。"
        )

        self.assertEqual("ma**z@hotmail.com", outlook_batch_login.microsoft_masked_email("matt_burrows_in_nz@hotmail.com"))
        self.assertTrue(
            outlook_batch_login.is_microsoft_security_mail_row(
                real_row_shape,
                "matt_burrows_in_nz@hotmail.com",
            )
        )
        self.assertFalse(
            outlook_batch_login.is_microsoft_security_mail_row(
                real_row_shape.replace("ma**z@hotmail.com", "ma**t@hotmail.com"),
                "matt_burrows_in_nz@hotmail.com",
            )
        )

    def test_microsoft_security_mail_replay_gate_separates_same_minute_ids(self):
        row = (
            "Microsoft 帐户团队 个人 Microsoft 帐户安全代码 17:18 "
            "Microsoft 帐户 安全代码: 123456 请对个人 Microsoft 帐户 ma**z@hotmail.com 使用"
        )
        request = time.mktime((2026, 7, 10, 17, 18, 28, 0, 0, -1))
        row_epoch = time.mktime((2026, 7, 10, 17, 18, 0, 0, 0, -1))

        self.assertFalse(
            outlook_batch_login.should_accept_microsoft_security_mail_row(
                row, row_epoch, {"old-id"}, request, {"old-id"},
                "matt_burrows_in_nz@hotmail.com",
            )
        )
        self.assertTrue(
            outlook_batch_login.should_accept_microsoft_security_mail_row(
                row, row_epoch, {"new-id"}, request, {"old-id"},
                "matt_burrows_in_nz@hotmail.com",
            )
        )
        self.assertFalse(
            outlook_batch_login.should_accept_microsoft_security_mail_row(
                row, row_epoch, set(), request, set(),
                "matt_burrows_in_nz@hotmail.com",
            )
        )

    def test_microsoft_security_mail_waits_for_delivery_and_captures_old_ids(self):
        source = inspect.getsource(outlook_batch_login.get_security_code)
        collect_source = inspect.getsource(outlook_batch_login.collect_microsoft_security_mail_ids)
        open_source = inspect.getsource(outlook_batch_login.open_latest_microsoft_security_mail)
        gate_source = inspect.getsource(outlook_batch_login.should_accept_microsoft_security_mail_row)

        self.assertIn("delivery_grace_seconds = 8", source)
        self.assertIn("microsoft_security_mail_ids_from_row", collect_source)
        self.assertIn("OUTLOOK_MAIL_URL", collect_source)
        self.assertIn("request_minute", gate_source)
        self.assertIn("row_time_slack_seconds: int = 0", open_source)

    def test_browser_security_code_submission_retries_rejected_codes(self):
        run_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        submit_source = inspect.getsource(steam_outlook_batch.submit_microsoft_security_code_with_validation)
        detect_source = inspect.getsource(steam_outlook_batch.detect_microsoft_code_rejected)
        reauth_source = inspect.getsource(steam_outlook_batch.complete_outlook_forward_reauthentication)

        self.assertIn("for microsoft_code_attempt in range(1, 3):", run_source)
        self.assertIn("Microsoft security code was rejected twice", run_source)
        self.assertIn("submit_microsoft_security_code_with_validation", run_source)
        self.assertIn("detect_microsoft_code_rejected", submit_source)
        self.assertIn("outlook_inbox_ready", submit_source)
        self.assertIn("verification already completed", submit_source)
        self.assertIn("MICROSOFT_CODE_REJECTED_MARKERS", detect_source)
        self.assertIn("wait_for_timeout_with_stop(", run_source)
        self.assertIn("Microsoft security code retry", run_source)
        self.assertIn("code has expired", steam_outlook_batch.MICROSOFT_CODE_REJECTED_MARKERS)
        self.assertIn("验证码错误", steam_outlook_batch.MICROSOFT_CODE_REJECTED_MARKERS)
        self.assertIn("restarting the email-delivery method once", reauth_source)
        self.assertIn("is_non_skippable_verification_error", run_source)
        self.assertIn("re-select the assigned email method", run_source)

    def test_microsoft_login_reidentifies_email_page_after_fido_return(self):
        source = inspect.getsource(outlook_batch_login.fill_login)
        self.assertIn("email_filled_retry", source)
        self.assertIn("Microsoft email re-submit after FIDO return", source)

    def test_microsoft_security_code_submission_waits_for_delayed_rejection(self):
        class FakeLocator:
            def __init__(self, page):
                self.page = page

            async def inner_text(self, timeout=0):
                if self.page.bodies:
                    return self.page.bodies.pop(0)
                return "验证码错误"

        class FakePage:
            url = "https://login.live.com/"

            def __init__(self):
                self.bodies = ["输入验证码", "输入验证码", "输入验证码", "输入验证码", "验证码错误"]

            def locator(self, selector):
                return FakeLocator(self)

            async def wait_for_timeout(self, milliseconds):
                return None

        logs = []

        async def run_case():
            with patch.object(steam_outlook_batch, "submit_code", return_value=None):
                return await steam_outlook_batch.submit_microsoft_security_code_with_validation(
                    FakePage(),
                    "123456",
                    "target@example.com",
                    logs.append,
                )

        self.assertFalse(asyncio.run(run_case()))
        self.assertTrue(any("rejected" in message for message in logs))

    def test_main_flow_long_page_waits_are_stoppable(self):
        helper_source = inspect.getsource(steam_outlook_batch.wait_for_timeout_with_stop)
        guard_source = inspect.getsource(steam_outlook_batch.search_steam_guard_mail)
        inbox_source = inspect.getsource(steam_outlook_batch.wait_for_outlook_inbox)
        maintenance_source = inspect.getsource(steam_outlook_batch.run_mail_security_maintenance)
        password_source = inspect.getsource(steam_outlook_batch.try_change_microsoft_password)
        rules_source = inspect.getsource(steam_outlook_batch.open_outlook_rules_settings)

        self.assertIn("pause_controller.should_stop()", helper_source)
        self.assertIn("Stopped during {step}", helper_source)
        for source in [guard_source, inbox_source, maintenance_source, password_source, rules_source]:
            self.assertIn("wait_for_timeout_with_stop", source)
        self.assertIn("Steam Guard mailbox load", guard_source)
        self.assertIn("Outlook inbox poll", inbox_source)
        self.assertIn("Microsoft security binding page load", maintenance_source)
        self.assertIn("Microsoft password change page load", password_source)
        self.assertIn("Outlook rules settings URL load", rules_source)

    def test_security_mailbox_login_prefers_password_option(self):
        source = inspect.getsource(steam_outlook_batch.ensure_security_mailbox_for_account)
        login_index = source.index("await fill_login(page, login_account, pause_controller)")
        prefer_index = source.index("prefer_password_sign_in(page, pause_controller)", login_index)
        blocking_index = source.index("handle_microsoft_blocking_page(", login_index)

        self.assertLess(prefer_index, blocking_index)
        self.assertIn("Security mailbox login offered password sign-in", source)

    def test_outlook_wait_cancels_passkey_through_browser_flow(self):
        source = inspect.getsource(steam_outlook_batch.wait_for_outlook_inbox)

        self.assertIn("cancel_microsoft_fido_bridge", source)
        self.assertNotIn("pywinauto", inspect.getsource(steam_outlook_batch))
        self.assertNotIn("pyautogui", inspect.getsource(steam_outlook_batch))

    def test_microsoft_post_login_prompts_log_non_dismissible_security_prompt(self):
        source = inspect.getsource(steam_outlook_batch.handle_microsoft_post_login_prompts)

        self.assertIn("account-protection prompt still visible", source)
        self.assertIn("was not dismissible automatically", source)
        self.assertIn("post-login prompt handling timed out", source)
        self.assertIn("page_state_summary", source)

    def test_main_flow_uses_hardened_outlook_inbox_waits(self):
        run_source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        security_source = inspect.getsource(steam_outlook_batch.ensure_security_mailbox_for_account)

        self.assertIn("wait_for_outlook_inbox", run_source)
        self.assertIn("wait_for_outlook_inbox", security_source)
        self.assertNotIn("ensure_mailbox_loaded(", run_source)
        self.assertNotIn("ensure_mailbox_loaded(", security_source)
        self.assertNotIn("page.goto(OUTLOOK_MAIL_URL", security_source)

    def test_microsoft_blocking_classifier_covers_common_unhandled_pages(self):
        self.assertIsNotNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://login.live.com/",
                "Your account has been locked.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://account.live.com/acsr",
                "Recover your account. Account recovery is required because this account is temporarily locked.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://login.live.com/",
                "Use a passkey or scan the QR code.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://login.live.com/",
                "Choose an account. Use another account.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://account.live.com/",
                "Add a new way to sign in or verify.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://account.live.com/",
                "Your security info change is still pending. You can't access this site right now.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://login.live.com/",
                "Too many requests. The service is temporarily unavailable, try again later.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://login.live.com/",
                "We detected something unusual. We won't send codes right now. Try another verification method.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://login.live.com/",
                "Usage limit exceeded. You have requested the maximum number of codes. Please try again later.",
            )
        )
        self.assertTrue(
            any("won't send codes" in marker for marker in steam_outlook_batch.SECURITY_EMAIL_DELIVERY_FAILURE_MARKERS)
        )
        self.assertIn("usage limit exceeded", steam_outlook_batch.SECURITY_EMAIL_DELIVERY_FAILURE_MARKERS)
        self.assertIsNone(
            steam_outlook_batch.classify_microsoft_page(
                "https://outlook.live.com/mail/",
                "Inbox Focused Other",
            )
        )

    def test_outlook_blocking_classifier_covers_common_unhandled_pages(self):
        self.assertIsNotNone(
            steam_outlook_batch.classify_outlook_page(
                "https://outlook.live.com/mail/",
                "Choose an account. Use another account.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_outlook_page(
                "https://outlook.live.com/mail/",
                "Something went wrong. We couldn't load your mailbox. Please try again later.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_outlook_page(
                "https://outlook.live.com/mail/",
                "Too many requests. Service is unavailable. Please try again later.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_outlook_page(
                "https://outlook.live.com/mail/",
                "Your mailbox is full. Storage is full.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_outlook_page(
                "https://outlook.live.com/mail/",
                "Working offline. Try refreshing the page.",
            )
        )
        self.assertIsNotNone(
            steam_outlook_batch.classify_outlook_page(
                "https://outlook.live.com/mail/",
                "Outlook.com and Outlook apps require a 1st party alias. Add an Outlook.com alias to continue.",
            )
        )
        self.assertIsNone(
            steam_outlook_batch.classify_outlook_page(
                "https://login.live.com/",
                "Choose an account.",
            )
        )

    def test_dota_blocking_classifier_covers_common_unhandled_pages(self):
        self.assertEqual(
            "自定义游戏权限受限",
            dota2_arcade.classify_dota_blocking_text(
                "One or more members are restricted from playing custom games at this time."
            ),
        )
        self.assertEqual(
            "Dota2 协调器或网络暂时不可用",
            dota2_arcade.classify_dota_blocking_text("Connected to the Dota 2 Game Coordinator; reconnecting."),
        )
        self.assertEqual(
            "自定义游戏需要更新、下载或版本不匹配",
            dota2_arcade.classify_dota_blocking_text("Custom game archive does not match the lobby leader."),
        )
        source = inspect.getsource(dota2_arcade.Dota2Automation.click_text)
        self.assertIn("detect_blocking_state", source)
        self.assertIn("Dota OCR saw blocking state", source)
        self.assertIn("recover_if_blocked", inspect.getsource(dota2_arcade.Dota2Automation.open_arcade_and_game))

    def test_outlook_blocking_handler_uses_manual_recovery_message(self):
        source = inspect.getsource(steam_outlook_batch.handle_outlook_blocking_page)

        self.assertIn("Outlook 页面需要人工处理", source)
        self.assertIn("Rules/规则页", source)
        self.assertIn("Forwarding/转发页", source)
        self.assertIn("wait_for_manual_continue", source)

    def test_steam_credentials_are_not_exposed_in_process_arguments(self):
        account = SimpleNamespace(steam_username="secret-user", steam_password="secret-password")

        class FakeProcess:
            pid = 7

        with patch.object(steam_outlook_batch.subprocess, "Popen", return_value=FakeProcess()) as popen:
            steam_outlook_batch.start_steam_login(Path("D:/Steam/steam.exe"), account, lambda _: None)

        command = popen.call_args.args[0]
        self.assertEqual(["D:\\Steam\\steam.exe"], command)
        self.assertNotIn(account.steam_username, command)
        self.assertNotIn(account.steam_password, command)

    def test_feishu_write_is_read_back_before_success(self):
        write_response = {"code": 0}
        read_response = {"code": 0, "data": {"valueRange": {"values": [["76561198000000000"]]}}}
        with patch.object(steam_outlook_batch.feishu_tool, "write_sheet", return_value=write_response), patch.object(
            steam_outlook_batch.feishu_tool, "read_sheet", return_value=read_response
        ):
            result = steam_outlook_batch.write_sheet_verified({}, "token", "sheet", "I72:I72", [["76561198000000000"]])
        self.assertIs(result, write_response)

    def test_feishu_write_readback_mismatch_fails_closed(self):
        with patch.object(steam_outlook_batch.feishu_tool, "write_sheet", return_value={"code": 0}), patch.object(
            steam_outlook_batch.feishu_tool,
            "read_sheet",
            return_value={"code": 0, "data": {"valueRange": {"values": [["wrong"]]}}},
        ):
            with self.assertRaisesRegex(RuntimeError, "write verification failed"):
                steam_outlook_batch.write_sheet_verified({}, "token", "sheet", "I72:I72", [["expected"]])

    def test_feishu_user_api_refreshes_once_after_expired_token(self):
        feishu = steam_outlook_batch.feishu_tool
        with patch.object(feishu, "get_stored_user_access_token", return_value="old-token"), patch.object(
            feishu, "refresh_stored_user_access_token", return_value="new-token"
        ) as refresh, patch.object(
            feishu,
            "http_json",
            side_effect=[feishu.FeishuError("HTTP PUT failed: 401 Unauthorized: token expired"), {"code": 0}],
        ) as request:
            result = feishu.feishu_api({}, "PUT", "/open-apis/sheets/v2/spreadsheets/s/values", {}, use_uat=True)

        self.assertEqual(result, {"code": 0})
        refresh.assert_called_once()
        self.assertEqual(request.call_count, 2)
        self.assertIn("new-token", request.call_args.args[-1]["Authorization"])

    def test_expired_feishu_write_is_non_skippable(self):
        self.assertTrue(
            steam_outlook_batch.is_non_skippable_verification_error(
                RuntimeError("HTTP PUT failed: 401 Unauthorized: code 99991677 token expired")
            )
        )

    def test_forward_rule_batch_stops_on_any_row_error(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        self.assertIn('getattr(args, "task_mode", "") == "forward_rule"', source)

    def test_blocked_security_email_in_column_j_is_reassigned_from_pool(self):
        source = inspect.getsource(steam_outlook_batch.read_pending_accounts)
        self.assertIn("Treat it as unbound", source)
        self.assertIn("backup_security_email.casefold() in blocked_security_mailboxes", source)

    def test_stale_forward_placeholders_cannot_overwrite_real_settings(self):
        source = inspect.getsource(steam_web_ui.save_ui_settings)
        build_source = inspect.getsource(steam_web_ui.build_args)
        self.assertIn("FORWARD_ADDRESS_PLACEHOLDERS", source)
        self.assertIn("stale browser", source)
        self.assertIn("stored_settings", build_source)

    def test_outlook_identity_mismatch_fails_closed(self):
        async def run_case():
            with patch.object(
                steam_outlook_batch,
                "read_active_outlook_mailbox",
                return_value="old@example.com",
            ):
                await steam_outlook_batch.ensure_active_outlook_mailbox(
                    SimpleNamespace(),
                    "target@example.com",
                    timeout_seconds=1,
                )

        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            asyncio.run(run_case())

    def test_fresh_isolated_login_keeps_confirmed_inbox_when_account_control_is_unreadable(self):
        async def run_unknown():
            with patch.object(steam_outlook_batch, "read_active_outlook_mailbox", return_value=""):
                await steam_outlook_batch.confirm_fresh_outlook_login_identity(
                    SimpleNamespace(), "target@example.com"
                )

        async def run_mismatch():
            with patch.object(
                steam_outlook_batch,
                "read_active_outlook_mailbox",
                return_value="other@example.com",
            ):
                await steam_outlook_batch.confirm_fresh_outlook_login_identity(
                    SimpleNamespace(), "target@example.com"
                )

        asyncio.run(run_unknown())
        with self.assertRaisesRegex(RuntimeError, "identity mismatch after fresh login"):
            asyncio.run(run_mismatch())

    def test_same_security_mailbox_reuses_confirmed_context_across_rows(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)

        self.assertIn('security_context_email = ""', source)
        self.assertIn("if security_context_email != expected_security_email", source)
        self.assertIn("security_context_email = expected_security_email", source)
        self.assertIn("Reusing confirmed security mailbox session", source)
        self.assertIn("security_page.is_closed()", source)
        self.assertIn("challenge_expected_security_email", source)
        self.assertIn("Using existing security mailbox session for verification", source)
        self.assertIn("Security-email binding stage committed", source)

    def test_security_mailbox_verification_does_not_trust_page_body(self):
        async def run_case():
            page = SimpleNamespace(url="https://outlook.live.com/mail/")
            page.locator = lambda *_args, **_kwargs: None
            with patch.object(steam_outlook_batch, "read_active_outlook_mailbox", return_value="other@example.com"):
                return await steam_outlook_batch.verify_security_mailbox(page, "target@example.com")

        self.assertFalse(asyncio.run(run_case()))

    def test_logout_clears_context_cookies_after_server_logout(self):
        class FakeContext:
            def __init__(self):
                self.cleared = False

            async def clear_cookies(self):
                self.cleared = True

        class FakePage:
            def __init__(self):
                self.context = FakeContext()
                self.url = ""

            async def goto(self, *_args, **_kwargs):
                self.url = steam_outlook_batch.LOGOUT_URL

            async def wait_for_timeout(self, _milliseconds):
                return None

        async def run_case():
            page = FakePage()
            await steam_outlook_batch.logout_outlook(page)
            return page.context.cleared

        self.assertTrue(asyncio.run(run_case()))

    def test_target_outlook_uses_per_account_isolated_context_and_fresh_login_gate(self):
        source = inspect.getsource(steam_outlook_batch.run_with_callbacks)
        isolation_source = inspect.getsource(steam_outlook_batch.launch_isolated_target_context)
        identity_source = inspect.getsource(steam_outlook_batch.read_active_outlook_mailbox)

        self.assertIn("launch_isolated_target_context", source)
        self.assertIn("confirm_fresh_outlook_login_identity", source)
        self.assertIn("target_context_email = expected_target_email", source)
        self.assertIn('email_scoped_outlook_profile("target", email)', isolation_source)
        self.assertNotIn("tempfile.mkdtemp", isolation_source)
        self.assertIn("try_reuse_email_scoped_outlook_session", source)
        self.assertNotIn('locator("body")', identity_source)
        self.assertIn("refusing to guess active account", identity_source)

    def test_dota_room_result_only_matches_unique_room_description(self):
        source = inspect.getsource(dota2_arcade.Dota2Automation.search_room)
        flow_source = "\n".join(
            [
                inspect.getsource(dota2_arcade.Dota2Automation.open_arcade_and_game),
                inspect.getsource(dota2_arcade.Dota2Automation.create_room),
                source,
            ]
        )

        self.assertIn("patterns = [re.escape(description)]", source)
        self.assertNotIn("coordinate fallback", flow_source)
        self.assertIn("wait_for_manual_recovery", flow_source)


if __name__ == "__main__":
    unittest.main()
