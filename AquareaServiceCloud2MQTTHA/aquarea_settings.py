"""
Device settings — equivalent of aquareaDeviceSettings.go
"""

import asyncio
import json
import logging
import re
import time

from aquarea_diag import pick_headers
from aquarea_http import AquareaApiError, AquareaSessionError
from aquarea_types import AquareaCommand, AquareaEndUserJSON, AquareaFunctionSettingGetJSON
from aquarea_placeholder_ranges import compute_placeholder_ranges, load_from_js

logger = logging.getLogger(__name__)

_JS_OPTIONS_LOADED = False  # load once per process lifetime
SETTINGS_RETRY_DELAY_SEC = 3.0  # one immediate retry on the same session


class AquareaSettingsMixin:

    async def load_placeholder_options_from_js(self) -> None:
        """
        Fetch the Panasonic settings JS bundle and extract all placeholder
        option ranges dynamically (supports any future model/statusNo).

        Flow: GET /installer/functionSetting HTML → extract JS URL → GET JS → parse.
        Falls back to hardcoded values if anything fails.
        Called once at startup.
        """
        global _JS_OPTIONS_LOADED
        if _JS_OPTIONS_LOADED:
            return

        base = self.aquarea_service_cloud_url
        try:
            html = (await self.http_get_html(base + "installer/functionSetting")).decode(
                "utf-8", errors="replace"
            )
            m = re.search(r'src="(/statics[^"]+function-setting[^"]+\.js)"', html)
            if not m:
                logger.warning("Could not find settings JS URL in HTML — using fallback ranges")
                return

            js_url = "https://aquarea-service.panasonic.com" + m.group(1)
            logger.info("Loading placeholder option ranges from %s", js_url)
            js_bytes = await self.http_get(js_url)
            js = js_bytes.decode("utf-8", errors="replace")

            if load_from_js(js):
                _JS_OPTIONS_LOADED = True
            else:
                logger.warning("JS parse failed — using fallback ranges")

        except Exception as exc:
            logger.warning("load_placeholder_options_from_js failed (%s) — using fallback", exc)




    async def send_setting(self, cmd: AquareaCommand) -> None:
        if cmd.value == "----":
            return
        if not self.aquarea_settings.settings_background_data:
            return

        function_name = self.reverse_translation.get(cmd.setting)
        if not function_name:
            logger.warning(
                "Received command for unknown setting '%s' (device %s, value '%s') — "
                "no matching entry in translation.json, command ignored",
                cmd.setting, cmd.device_id, cmd.value,
            )
            return

        function_name_post = function_name.replace(
            "function-setting-user-select-", "userSelect"
        )
        function_info = self.translation.get(function_name)
        value = cmd.value

        if function_info:
            if function_info.kind == "basic":
                # Build a direct label→hex map from this setting's own values,
                # bypassing reverse_dictionary_web_ui which can have collisions
                # when the same label string (e.g. "Cool", "On", "Off") appears
                # under multiple 2010-xxxx codes.
                label_to_hex = {
                    self.dictionary_web_ui.get(code_2010, code_2010): hex_val
                    for hex_val, code_2010 in function_info.values.items()
                }
                if value in label_to_hex:
                    value = label_to_hex[value]
                else:
                    # Fallback to old two-step path
                    value = self.reverse_dictionary_web_ui.get(value, value)
                    value = function_info.reverse_values.get(value, value)
            elif function_info.kind == "placeholder":
                try:
                    i = int(value, 0)
                except (ValueError, OverflowError):
                    logger.warning(
                        "Ignoring command for '%s': expected numeric value, got '%s'",
                        cmd.setting, value,
                    )
                    return
                if "HolidayMode" not in cmd.setting:
                    i += 128
                value = f"0x{i & 0xFF:X}"

        user = self.users_map.get(cmd.device_id)
        if not user:
            return

        token = await self.get_end_user_shiesuahruefutohkun(user)
        bg = self.aquarea_settings.settings_background_data

        post_data = {
            "var.deviceId": user.device_id,
            "var.preOperation": bg.get("0x80", {}).get("value", ""),
            "var.preMode": bg.get("0xE0", {}).get("value", ""),
            "var.preTank": bg.get("0xE1", {}).get("value", ""),
            f"var.{function_name_post}": value,
            "shiesuahruefutohkun": token,
        }

        logger.info(
            "Sending command to device %s: %s = %s (raw: var.%s = %s)",
            cmd.device_id, cmd.setting, cmd.value, function_name_post, value,
        )
        logger.debug(
            "POST installer/api/function/setting/user/set — full payload: %s",
            json.dumps({k: v for k, v in post_data.items() if k != "shiesuahruefutohkun"}, ensure_ascii=False),
        )

        response = await self.http_post_with_referer(
            self.aquarea_service_cloud_url + "installer/api/function/setting/user/set",
            self.aquarea_service_cloud_url + "installer/functionSetting",
            post_data,
        )

        try:
            response_json = json.loads(response)
            error_code = response_json.get("errorCode", -1)
            if error_code == 0:
                logger.info(
                    "Command accepted by Panasonic API: device %s, %s = %s",
                    cmd.device_id, cmd.setting, cmd.value,
                )
            else:
                logger.warning(
                    "Command rejected by Panasonic API: device %s, %s = %s — errorCode=%s, response=%s",
                    cmd.device_id, cmd.setting, cmd.value, error_code, response_json,
                )
        except Exception:
            logger.debug("send_setting raw response: %s", response)

    async def get_device_settings(
        self, user: AquareaEndUserJSON, shiesuahruefutohkun: str
    ) -> dict[str, str]:
        base = self.aquarea_service_cloud_url

        # The browser performs a client-side SPA route change from functionStatus →
        # functionSetting (no network HTML request), so the next XHR carries
        # Referer: installer/functionSetting.  We must replicate that referer here.
        ref = base + "installer/functionSetting"

        first_ok = True
        try:
            raw, parsed = await self._settings_get_attempt(user, shiesuahruefutohkun, base, ref, attempt=1)
        except AquareaApiError as e:
            first_ok = False
            logger.info("setting/get attempt 1 failed for %s, retrying in %.0fs: %s",
                        user.gwid, SETTINGS_RETRY_DELAY_SEC, e)
            await asyncio.sleep(SETTINGS_RETRY_DELAY_SEC)
            try:
                raw, parsed = await self._settings_get_attempt(user, shiesuahruefutohkun, base, ref, attempt=2)
            except AquareaApiError:
                self.settings_diag(user.gwid).poll_result(first_ok=False, final_ok=False)
                raise
        self.settings_diag(user.gwid).poll_result(first_ok=first_ok, final_ok=True)
        if not first_ok:
            logger.info("setting/get retry succeeded for %s", user.gwid)
        return await self._publish_settings(user, raw, parsed)

    async def _settings_get_attempt(self, user, token: str, base: str, ref: str, attempt: int):
        """One instrumented setting/get call. Returns (raw, parsed) or raises
        AquareaApiError / AquareaSessionError. Every attempt is recorded."""
        now = time.monotonic()
        login_t = getattr(self, "_login_time", None)
        status_t = self._last_status_end.get(user.gwid)
        rec = {
            "gwid": user.gwid,
            "attempt": attempt,
            "since_login_s": round(now - login_t, 1) if login_t is not None else None,
            "since_status_s": round(now - status_t, 2) if status_t is not None else None,
        }
        try:
            status, headers, b, ms = await self.http_post_with_referer_full(
                base + "installer/api/function/setting/get",
                ref,
                {"var.deviceId": user.device_id, "shiesuahruefutohkun": token},
            )
        except Exception as e:
            self.settings_diag(user.gwid).record(**rec, ok=False, http=None, ms=0.0,
                                      codes=["transport"], msg=f"{type(e).__name__}: {e}"[:300])
            raise AquareaApiError(f"setting/get transport error: {type(e).__name__}: {e}") from e

        rec.update(http=status, ms=round(ms, 1), bytes=len(b), headers=pick_headers(headers))
        try:
            raw = json.loads(b)
        except ValueError:
            self.settings_diag(user.gwid).record(**rec, ok=False, codes=[f"http{status}-nonjson"],
                                      body=b[:500].decode("utf-8", "replace"))
            if status in (401, 403):
                raise AquareaSessionError(f"setting/get HTTP {status}")
            raise AquareaApiError(f"setting/get HTTP {status}, non-JSON body ({len(b)} bytes)")

        parsed = AquareaFunctionSettingGetJSON.from_dict(raw)
        # Validate BEFORE overwriting self.aquarea_settings: an empty response
        # would otherwise wipe settings_background_data and make send_setting()
        # silently drop every command.
        if parsed.error_code != 0 or not parsed.setting_data_info:
            # Panasonic reports intermittent backend failures of this endpoint as
            # errorCode=0 + message=[{errorCode: '9999-9999' | '1000-5400'}].
            # function/status succeeds on the same session in the same poll and
            # setting/get succeeds again one poll later without any re-login:
            # it is not a session problem.  Session loss is detected by
            # function/status (called first), so never reset from here.
            api_codes = [
                m.get("errorCode", "?")
                for m in (raw.get("message") or [])
                if isinstance(m, dict)
            ]
            self.settings_diag(user.gwid).record(**rec, ok=False, codes=api_codes or ["none"],
                                      error_code=parsed.error_code, body=str(raw)[:500])
            raise AquareaApiError(
                f"function/setting/get returned errorCode={parsed.error_code}, "
                f"api codes={api_codes or 'none'}, http={status}, {ms:.0f}ms, "
                f"{len(parsed.setting_data_info)} values: {str(raw)[:300]}"
            )
        self.settings_diag(user.gwid).record(**rec, ok=True, values=len(parsed.setting_data_info))
        return raw, parsed

    async def _publish_settings(self, user, raw: dict, parsed) -> dict[str, str]:
        self.aquarea_settings = parsed
        settings: dict[str, str] = {}

        # Compute correct min/max/step for placeholder settings from device config.
        # Only done once per device — hardware config doesn't change between polls.
        if user.gwid not in self._placeholder_ranges_applied:
            ranges = compute_placeholder_ranges(
                self.aquarea_settings.raw_setting_data_info,
                self.aquarea_settings.settings_background_data,
            )
            for tr_key, r in ranges.items():
                entry = self.translation.get(tr_key)
                if entry and entry.kind == "placeholder":
                    entry.min = r.min
                    entry.max = r.max
                    entry.step = r.step
                    logger.debug(
                        "Placeholder range %s (%s): min=%s, max=%s, step=%s",
                        tr_key, entry.name, r.min, r.max, r.step,
                    )
            self._placeholder_ranges_applied.add(user.gwid)


        # Log params{} of each setting — helps understand if options can be made dynamic
        logger.debug(
            "settingDataInfo structure for %s: %s",
            user.gwid,
            json.dumps(
                {k: {"type": v.type, "selected": v.selected_value, "params": v.params}
                 for k, v in self.aquarea_settings.setting_data_info.items()
                 if "user" in k},
                ensure_ascii=False,
            ),
        )

        for key, val in self.aquarea_settings.setting_data_info.items():
            if "user" not in key:
                continue

            if key in self.translation:
                # --- Known setting: use translation.json structure ---
                translation = self.translation[key]
                value = None

                if val.type == "basic-text":
                    value = self.dictionary_web_ui.get(val.text_value, "")
                elif val.type == "select":
                    if translation.kind == "basic":
                        if val.selected_value == "----":
                            # Idle state for one-shot actions (Sterilization, ForceDefrost)
                            value = "----"
                        else:
                            raw = translation.values.get(val.selected_value, "")
                            value = self.dictionary_web_ui.get(raw, raw)
                        options = "\n".join(
                            self.dictionary_web_ui.get(opt, opt)
                            for opt in translation.values.values()
                        )
                        settings[f"aquarea/{user.gwid}/settings/{translation.name}/options"] = options
                    elif translation.kind == "placeholder":
                        i = int(val.selected_value, 0)
                        if "HolidayMode" not in translation.name:
                            i -= 128
                        value = str(int.from_bytes((i & 0xFF).to_bytes(1, "big"), "big", signed=True))
                elif val.type == "placeholder-text":
                    value = val.placeholder

                if value is not None:
                    settings[f"aquarea/{user.gwid}/settings/{translation.name}"] = value
                    # Always publish a label (priority: API translation > display_name > internal name)
                    label_code = getattr(translation, "label_code", "")
                    display_name = getattr(translation, "display_name", "")
                    if label_code:
                        label = self.dictionary_web_ui.get(label_code, display_name or translation.name)
                    else:
                        label = display_name or translation.name
                    settings[f"aquarea/{user.gwid}/settings/{translation.name}/label"] = label.strip()

            else:
                # --- Unknown setting: passthrough using dictionary_web_ui ---
                # Works for any heat pump model without changes to translation.json.
                # We publish the current value translated via the 2010 dictionary.
                # We cannot generate a select/switch without the full options list,
                # so this publishes a read-only sensor — better than nothing.
                if val.selected_value:
                    internal_name = key.replace("function-setting-user-select-", "setting-")
                    value = self.dictionary_web_ui.get(val.selected_value, val.selected_value)
                    settings[f"aquarea/{user.gwid}/settings/{internal_name}"] = value
                    logger.debug("Passthrough unknown setting %s (%s) = %s", key, internal_name, value)
        logger.info("Get new Panasonic settings data for device %s", user.gwid)
        logger.debug(
            "Panasonic settings data for device %s (%d values): %s",
            user.gwid,
            len(settings),
            json.dumps(settings, ensure_ascii=False),
        )
        return settings