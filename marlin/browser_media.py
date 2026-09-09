"""YouTube playback in a dedicated, visible Chrome profile."""

from concurrent.futures import Future
import os
from pathlib import Path
from queue import Queue
import subprocess
import threading
from urllib.parse import urlencode
from urllib.parse import urlparse


class YouTubePlayer:
    def __init__(self, profile: Path):
        self.profile = profile
        self._requests = Queue()
        self._lock = threading.Lock()
        self._thread = None

    def play(self, query: str) -> dict:
        query = str(query).strip()
        if not query or len(query) > 300:
            raise ValueError('Please give a music or video search of 1 to 300 characters.')
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, daemon=True, name='marlin-youtube')
                self._thread.start()
        result = Future()
        self._requests.put((query, result))
        return result.result()

    def _worker(self):
        # Playwright objects stay on their owning thread, including later requests.
        driver = context = page = None
        while True:
            query, result = self._requests.get()
            try:
                if query is None:
                    result.set_result(self._close_page(page))
                    continue
                if driver is None:
                    from playwright.sync_api import sync_playwright
                    driver = sync_playwright().start()
                if context is not None and (page is None or page.is_closed()):
                    try:
                        page = context.new_page()
                    except Exception:
                        context = None
                if context is None:
                    context = self._launch_browser(driver)
                    # Chrome's startup tab can be replaced while a persistent
                    # profile is restoring. Use a fresh, stable automation tab.
                    page = context.new_page()
                try:
                    payload = self._play_page(page, query)
                except Exception as exc:
                    if not self._recoverable_navigation_error(exc):
                        raise
                    page = self._replacement_page(context, page)
                    payload = self._play_page(page, query)
                if not payload.get('ok') and self._open_desktop_player(payload.get('url', '')):
                    title = payload.get('title') or query
                    payload = {
                        **payload,
                        'ok': True,
                        'desktop_app': True,
                        'playback_verified': False,
                        'message': f'Opened {title} in YouTube Desktop and requested autoplay.',
                    }
                result.set_result(payload)
            except ImportError:
                result.set_exception(ValueError('YouTube control needs Playwright. Run py -m pip install playwright, then restart MARLIN.'))
            except Exception as exc:
                details = {}
                if page is not None and not page.is_closed():
                    details = {'url': page.url}
                result.set_result({'ok': False, 'message': 'YouTube playback could not be verified. Check the Chrome window for a consent, sign-in, network, or playback error.',
                                   'error': str(exc)[:500], 'query': query, **details})

    def _launch_browser(self, driver):
        # Separate profiles prevent locking or altering the user's everyday browser.
        errors = []
        for channel in ('chrome', 'msedge'):
            try:
                return driver.chromium.launch_persistent_context(
                    str(self.profile if channel == 'chrome' else self.profile / channel), channel=channel, headless=False,
                    no_viewport=True, accept_downloads=False, timeout=15000,
                )
            except Exception as exc:
                errors.append(str(exc))
        raise ValueError('Could not launch Chrome or Edge. Install either browser, then retry music playback. ' + errors[-1][:200])

    @staticmethod
    def _recoverable_navigation_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return 'err_aborted' in message or 'frame was detached' in message or 'target page' in message

    @staticmethod
    def _replacement_page(context, previous):
        pages = [page for page in context.pages if not page.is_closed() and page is not previous]
        for page in reversed(pages):
            if urlparse(page.url).hostname in {'youtube.com', 'www.youtube.com', 'music.youtube.com'}:
                return page
        return context.new_page()

    @staticmethod
    def _open_desktop_player(url: str) -> bool:
        if urlparse(str(url)).hostname not in {'youtube.com', 'www.youtube.com', 'music.youtube.com'}:
            return False
        executable = Path(os.getenv('LOCALAPPDATA', '')) / 'YouTube Desktop' / 'ytdesktop.exe'
        if not executable.is_file():
            return False
        separator = '&' if '?' in url else '?'
        try:
            subprocess.Popen(
                [str(executable), f'{url}{separator}autoplay=1'],
                close_fds=True,
            )
        except OSError:
            return False
        return True

    @staticmethod
    def _close_page(page) -> dict:
        if page is None or page.is_closed():
            return {'ok': False, 'message': 'There is no MARLIN YouTube tab to close.'}
        if urlparse(page.url).hostname not in {'youtube.com', 'www.youtube.com', 'music.youtube.com'}:
            return {'ok': False, 'message': 'That tab has left YouTube. I have left it open to protect any work there.'}
        page.close()
        return {'ok': True, 'message': 'Closed my YouTube tab.'}

    @staticmethod
    def access_message(text: str) -> str | None:
        text = text.lower().replace('\u2019', "'")
        if "confirm you're not a bot" in text:
            return "YouTube opened the video but requires you to sign in to confirm you are not a bot. Sign in in MARLIN's Chrome window, then ask me to play it again."
        if 'before you continue to youtube' in text:
            return "YouTube needs your cookie/privacy choice in its Chrome window before I can play a video."
        return None

    def close_tab(self) -> dict:
        if self._thread is None or not self._thread.is_alive():
            return {'ok': False, 'message': 'There is no MARLIN YouTube tab to close.'}
        result = Future()
        self._requests.put((None, result))
        return result.result()

    @staticmethod
    def _play_page(page, query: str) -> dict:
        page.goto('https://www.youtube.com/results?' + urlencode({'search_query': query}), wait_until='domcontentloaded', timeout=15000)
        page.bring_to_front()
        video_link = page.locator('ytd-video-renderer a#video-title[href*="/watch?"]').first
        video_link.wait_for(state='visible', timeout=12000)
        title = video_link.inner_text().strip()
        video_link.click(timeout=5000)
        page.wait_for_url('**/watch?**', timeout=10000)
        page.locator('video').first.wait_for(state='attached', timeout=10000)
        playing = """() => {
            const video = document.querySelector('video');
            return video && !video.paused && !video.muted && video.volume > 0 && video.currentTime > 0 && video.readyState >= 2;
        }"""
        from playwright.sync_api import TimeoutError as PlaybackTimeout
        try:
            page.wait_for_function(playing, timeout=6000)
        except PlaybackTimeout:
            blocked = YouTubePlayer.access_message(page.locator('body').inner_text(timeout=2000))
            if blocked:
                return {'ok': False, 'message': blocked, 'url': page.url, 'query': query, 'title': title}
            page.locator('#movie_player').hover(timeout=3000)
            video = page.locator('video').first
            if video.evaluate('(video) => video.muted'):
                page.locator('button.ytp-mute-button').click(timeout=3000)
            if video.evaluate('(video) => video.paused'):
                page.locator('button.ytp-play-button').click(timeout=5000)
            page.wait_for_function(playing, timeout=10000)
        advertising = page.locator('.html5-video-player.ad-showing').count() > 0
        return {'ok': True, 'query': query, 'title': title, 'url': page.url,
                'message': f'YouTube is playing an advertisement before {title}.' if advertising else f'Playing {title} on YouTube.'}
