"""
HTTP helpers — equivalent of aquareaHTTP.go
"""

import logging
import aiohttp

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

_HEADERS_HTML = {
    "Cache-Control": "max-age=0",
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}


def _make_base_headers(language: str) -> dict:
    lang_map = {
        "en": "en-US,en;q=0.9",
        "fr": "fr-FR,fr;q=0.9",
        "de": "de-DE,de;q=0.9",
        "es": "es-ES,es;q=0.9",
        "it": "it-IT,it;q=0.9",
        "nl": "nl-NL,nl;q=0.9",
        "pl": "pl-PL,pl;q=0.9",
        "pt": "pt-PT,pt;q=0.9",
        "cs": "cs-CZ,cs;q=0.9",
        "sv": "sv-SE,sv;q=0.9",
        "fi": "fi-FI,fi;q=0.9",
        "nb": "nb-NO,nb;q=0.9",
        "da": "da-DK,da;q=0.9",
        "el": "el-GR,el;q=0.9",
        "ro": "ro-RO,ro;q=0.9",
        "sk": "sk-SK,sk;q=0.9",
        "sl": "sl-SI,sl;q=0.9",
        "hr": "hr-HR,hr;q=0.9",
        "bg": "bg-BG,bg;q=0.9",
        "hu": "hu-HU,hu;q=0.9",
        "tr": "tr-TR,tr;q=0.9",
    }
    accept_language = lang_map.get(language.lower().strip(), f"{language};q=0.9")
    return {
        "Cache-Control": "max-age=0",
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": accept_language,
    }


def _make_multipart(data: dict) -> aiohttp.FormData:
    form = aiohttp.FormData()
    for k, v in data.items():
        form.add_field(k, str(v))
    return form


class AquareaHTTPMixin:

    def set_language(self, language: str) -> None:
        """Set Accept-Language header for all HTTP requests.

        Controls the language of labels returned by the Panasonic API
        (types 2000, 2006, 2903, 2010).  Call once after __init__.
        """
        self._headers_base = _make_base_headers(language)

    @property
    def _base_headers(self) -> dict:
        """Base headers, defaulting to English if set_language was never called."""
        if not hasattr(self, "_headers_base"):
            self._headers_base = _make_base_headers("en")
        return self._headers_base

    async def http_post(self, url: str, data: dict | None) -> bytes:
        """POST with multipart/form-data."""
        form = _make_multipart(data or {})
        async with self.session.post(url, data=form, headers=self._base_headers) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def http_get(self, url: str) -> bytes:
        async with self.session.get(url, headers=self._base_headers) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def http_get_html(self, url: str) -> bytes:
        """Simulate a browser page navigation (GET)."""
        async with self.session.get(url, headers=_HEADERS_HTML) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def http_get_with_referer(self, url: str, referer: str) -> bytes:
        headers = {
            **self._base_headers,
            "Referer": referer,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def http_post_with_referer(self, url: str, referer: str, data: dict | None) -> bytes:
        """POST with multipart/form-data and Referer header."""
        form = _make_multipart(data or {})
        headers = {
            **self._base_headers,
            "Referer": referer,
            "Origin": "https://aquarea-service.panasonic.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        async with self.session.post(url, data=form, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def http_post_navigate(self, url: str, referer: str, data: dict | None) -> bytes:
        """Simulate a browser form-submit navigation (application/x-www-form-urlencoded)."""
        headers = {
            **_HEADERS_HTML,
            "Referer": referer,
            "Origin": "https://aquarea-service.panasonic.com",
            "Sec-Fetch-User": "?1",
        }
        async with self.session.post(url, data=data or {}, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.read()