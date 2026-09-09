"""Explicit, keyless web research with local retention and Prolog ranking."""

from __future__ import annotations

import html
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from marlin.events import EventBus
from marlin.storage import MarlinStore
from second_brain.reasoning.service import ReasoningService


def _enable_windows_trust_store() -> None:
    try:
        import truststore
        truststore.inject_into_ssl()
    except (ImportError, OSError):
        pass


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "svg"}: self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "svg"}: self._ignored = max(0, self._ignored - 1)

    def handle_data(self, data: str) -> None:
        if not self._ignored and data.strip(): self.parts.append(data.strip())


class InternetResearchService:
    def __init__(
        self,
        store: MarlinStore,
        reasoning: ReasoningService,
        events: EventBus,
        summarizer: Callable[[str], str] | None = None,
    ):
        self.store, self.reasoning, self.events, self.summarizer = store, reasoning, events, summarizer
        _enable_windows_trust_store()

    def search(self, query: str) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("A research query is required.")
        self.events.publish("research.started", query=query)
        try:
            candidates = self._browser_search(query)[:8]
        except Exception:
            candidates = []
        candidates = [item for item in candidates if item.get("title", "").strip() and item.get("url", "").startswith(("http://", "https://"))]
        if len(candidates) < 2:
            try:
                candidates = self._duckduckgo_search(query)[:8]
            except Exception:
                candidates = self._bing_rss_search(query)[:8]
        sources = []
        for candidate in candidates:
            if len(sources) >= 8 or not self.url_allowed(candidate.get("url", "")):
                continue
            note = candidate.get("snippet", "")[:900]
            if len(sources) < 4 and self._robots_allowed(candidate["url"]):
                try:
                    note = self._inspect(candidate["url"])[:900] or note
                except (OSError, ValueError):
                    pass
            domain = urlparse(candidate["url"]).netloc.lower().removeprefix("www.")
            sources.append({**candidate, "domain": domain, "note": note})
        sources.sort(key=lambda source: self._relevance(query, source), reverse=True)
        if sources:
            best = self._relevance(query, sources[0])
            if best:
                sources = [source for source in sources if self._relevance(query, source) >= max(1, int(best * .65 + .5))]
        for rank, source in enumerate(sources, 1): source["rank"] = rank
        if not sources:
            raise ConnectionError("Internet research is unavailable or no safe public results were found.")
        summary = self._summarize(query, sources)
        run = self.store.add_research(query, sources, summary)
        self._rank(run)
        run = self.store.get_research(run["id"]) or run
        self.events.publish("research.completed", research=run)
        return run

    def handle(self, text: str) -> dict[str, Any] | None:
        command = " ".join(text.strip().split())
        match = re.match(
            r"(?:search(?:\s+(?:the|on))?\s+(?:internet|web)(?:\s+for)?|"
            r"internet search(?:\s+for)?|web search(?:\s+for)?|"
            r"look up(?:\s+on(?:\s+the)?\s+(?:internet|web))?|"
            r"find(?:\s+on(?:\s+the)?\s+(?:internet|web))|research|web research)\s+(.+)$",
            command,
            re.I,
        )
        if match:
            return self.search(match.group(1))
        trailing = re.match(
            r"(?:find|look up|search for)\s+(.+?)\s+(?:on|using)\s+(?:the\s+)?(?:internet|web|online)$",
            command,
            re.I,
        )
        if trailing:
            return self.search(trailing.group(1))
        if re.search(r"\b(?:search|research)\b", command, re.I) and re.search(r"\b(?:internet|web|online)\b", command, re.I):
            return {"clarification": "Tell me what to search for, for example: search the internet for SWI-Prolog scheduling."}
        if command.lower() in {"show research", "show research sources", "recent research"}:
            return {"runs": self.store.recent_research()}
        if text.strip().lower() in {"compare these sources", "compare the sources", "explain web evidence"}:
            recent = self.store.recent_research(1)
            if not recent:
                return {"runs": []}
            run = recent[0]
            return {"comparison": [{"title": source["title"], "domain": source["domain"], "score": source.get("prolog_score"), "note": source["note"]} for source in run["sources"]], "runs": [run]}
        save = re.match(r"save (?:this )?source(?:\s+([a-f0-9]+))?$", text.strip(), re.I)
        if save:
            source_id = save.group(1)
            recent = self.store.recent_research(1)
            if not source_id and recent and recent[0]["sources"]:
                source_id = recent[0]["sources"][0]["id"]
            return {"saved": self.store.save_research_source(source_id) if source_id else None}
        return None

    def _browser_search(self, query: str) -> list[dict[str, str]]:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"https://www.bing.com/search?q={quote_plus(query)}", wait_until="domcontentloaded", timeout=12000)
            results = page.locator("li.b_algo").all()[:8]
            items = []
            for result in results:
                link = result.locator("h2 a").first
                if not link.count(): continue
                items.append({"title": link.inner_text().strip(), "url": link.get_attribute("href") or "", "snippet": result.inner_text()[:700]})
            browser.close()
            return items

    def _duckduckgo_search(self, query: str) -> list[dict[str, str]]:
        request = Request(f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}", headers={"User-Agent": "Mozilla/5.0 MARLIN/2"})
        with urlopen(request, timeout=12) as response:
            body = response.read(1_000_000).decode("utf-8", "ignore")
        results = []
        pattern = (
            r'<a[^>]+href="([^"]+)"[^>]*class=[\'\"]result-link[\'\"][^>]*>(.*?)</a>'
            r'.*?<td[^>]*class=[\'\"]result-snippet[\'\"][^>]*>(.*?)</td>'
        )
        for match in re.finditer(pattern, body, re.I | re.S):
            title = re.sub(r"<[^>]+>", " ", html.unescape(match.group(2)))
            snippet = re.sub(r"<[^>]+>", " ", html.unescape(match.group(3)))
            redirect = html.unescape(match.group(1))
            if redirect.startswith("//"): redirect = "https:" + redirect
            target = parse_qs(urlparse(redirect).query).get("uddg", [redirect])[0]
            results.append({"title": " ".join(title.split()), "url": target, "snippet": " ".join(snippet.split())[:700]})
        return results

    def _bing_rss_search(self, query: str) -> list[dict[str, str]]:
        request = Request(f"https://www.bing.com/search?format=rss&q={quote_plus(query)}", headers={"User-Agent": "Mozilla/5.0 MARLIN/2"})
        with urlopen(request, timeout=12) as response:
            body = response.read(1_000_000)
        root = ET.fromstring(body)
        results = []
        for item in root.findall(".//item")[:8]:
            title = "".join(item.findtext("title") or "").strip()
            url = "".join(item.findtext("link") or "").strip()
            snippet = re.sub(r"<[^>]+>", " ", html.unescape(item.findtext("description") or ""))
            if title and url:
                results.append({"title": title, "url": url, "snippet": " ".join(snippet.split())[:700]})
        return results

    @staticmethod
    def url_allowed(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.path.lower().endswith((".exe", ".msi", ".zip", ".7z", ".rar", ".iso")):
            return False
        if any(piece in parsed.path.lower() for piece in ("/login", "/signin", "/auth")):
            return False
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
            return bool(addresses) and all(not InternetResearchService._blocked_ip(address) for address in addresses)
        except OSError:
            return False

    @staticmethod
    def _blocked_ip(value: str) -> bool:
        address = ipaddress.ip_address(value)
        return bool(address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast)

    def _robots_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        robots = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
        parser = RobotFileParser(robots)
        try:
            request = Request(robots, headers={"User-Agent": "MARLINResearch/2.0"})
            with urlopen(request, timeout=5) as response:
                parser.parse(response.read(200_000).decode("utf-8", "ignore").splitlines())
            return parser.can_fetch("MARLINResearch/2.0", url)
        except OSError:
            return False

    @staticmethod
    def _inspect(url: str) -> str:
        request = Request(url, headers={"User-Agent": "MARLINResearch/2.0"})
        with urlopen(request, timeout=8) as response:
            content_type = response.headers.get_content_type()
            length = int(response.headers.get("Content-Length") or 0)
            if content_type not in {"text/html", "text/plain"} or length > 1_000_000:
                raise ValueError("Unsupported or oversized research page.")
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("Research page exceeds the response limit.")
        parser = _TextParser(); parser.feed(body.decode("utf-8", "ignore"))
        return " ".join(" ".join(parser.parts).split())

    def _summarize(self, query: str, sources: list[dict[str, str]]) -> str:
        prompt = "Summarise in under 120 words using only these sources. Cite claims as [1], [2].\n" + query + "\n" + "\n".join(f"[{i}] {s['title']}: {s['note']}" for i, s in enumerate(sources, 1))
        if self.summarizer:
            try:
                result = self.summarizer(prompt).strip()
                if result and not re.search(r"no sources|without content|supply the full", result, re.I): return result[:4000]
            except Exception:
                pass
        return " ".join(f"[{i}] {source['title']}: {source['note'][:180]}" for i, source in enumerate(sources[:4], 1))

    def _rank(self, run: dict[str, Any]) -> None:
        domains = Counter(source["domain"] for source in run["sources"])
        engine = self.reasoning.engine
        engine.clear_agent_facts()
        for source in run["sources"]:
            atom = "source_" + source["id"]
            engine.assert_agent_fact("research_source", [atom, int(source["rank"]), 0, len(domains)])
            for key, value in self._claims(source.get("note") or source.get("snippet") or ""):
                engine.assert_agent_fact("research_claim", [atom, key, value])
        for source in run["sources"]:
            atom = "source_" + source["id"]
            score = engine.evidence_score(atom)
            source["prolog_score"] = score
            source["evidence"] = engine.explain_web_evidence(atom)
            if score is not None:
                self.store.set_research_source_score(source["id"], score)

    @staticmethod
    def _relevance(query: str, source: dict[str, str]) -> int:
        words = {word for word in re.findall(r"[a-z0-9]+", query.lower()) if len(word) > 2 and word not in {"the", "for", "and", "official", "documentation"}}
        haystack = f"{source.get('title', '')} {source.get('url', '')} {source.get('snippet', '')}".lower()
        return sum(3 if word in source.get("title", "").lower() else 1 for word in words if word in haystack)

    @staticmethod
    def _claims(text: str) -> list[tuple[str, str]]:
        claims = []
        for match in re.finditer(r"\b([a-z][a-z0-9_-]{2,})\s+(?:is|was|at|in|:)\s*([0-9]{1,4}(?:[.,][0-9]+)?%?|20[0-9]{2}-[0-9]{2}-[0-9]{2})", text.lower()):
            key = re.sub(r"[^a-z0-9]+", "_", match.group(1)).strip("_")
            value = re.sub(r"[^a-z0-9]+", "_", match.group(2)).strip("_")
            if key and value: claims.append((key, "v_" + value))
        return claims[:20]
