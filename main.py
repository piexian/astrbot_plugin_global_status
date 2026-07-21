"""AstrBot plugin entry point for global vendor status monitoring."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain

from .renderer import build_alert_fallback, render_alert_card, render_overview
from .sources import (
    Issue,
    SourceResult,
    build_source_specs,
    fetch_all_sources,
)
from .translation import TranslationService, normalize_language

STATE_KEY = "monitor_state_v1"
STATE_VERSION = 1
TRANSLATION_CACHE_KEY = "translation_cache_v1"


class GlobalStatusMonitor(star.Star):
    """Monitor official vendor status sources and push image alerts."""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._fetch_lock = asyncio.Lock()
        self._state = self._empty_state()
        self._last_results: list[SourceResult] = []
        self._platform_warning = ""
        self._translator = TranslationService(context)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "sources": {},
            "deliveries": {},
            "initialized_sources": [],
        }

    async def initialize(self) -> None:
        """Load persistent state, create the HTTP client, and start polling."""
        stored = await self.get_kv_data(STATE_KEY, self._empty_state())
        if (
            isinstance(stored, dict)
            and stored.get("version") == STATE_VERSION
            and isinstance(stored.get("sources"), dict)
            and isinstance(stored.get("deliveries"), dict)
        ):
            self._state = stored
            initialized_sources = self._state.get("initialized_sources")
            if not isinstance(initialized_sources, list):
                # States created before this option already completed their baseline.
                self._state["initialized_sources"] = list(self._state["sources"])
        else:
            self._state = self._empty_state()
        translation_cache = await self.get_kv_data(TRANSLATION_CACHE_KEY, {})
        self._translator.load_cache(translation_cache)

        timeout = aiohttp.ClientTimeout(total=15)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            trust_env=True,
            headers={"User-Agent": "AstrBot-Global-Status-Monitor/1.0"},
        )
        if bool(self.config.get("enabled", True)):
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(),
                name="astrbot-global-status-monitor",
            )
            logger.info("Global status monitor started.")
        else:
            logger.info(
                "Global status monitor is disabled; query command remains available."
            )

    async def terminate(self) -> None:
        """Stop polling and close the shared HTTP client."""
        self._stop_event.set()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
        self._monitor_task = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("Global status monitor stopped.")

    def _source_specs(self):
        source_config = self.config.get("sources", {})
        custom_sources = self.config.get("custom_statuspage_sources", [])
        return build_source_specs(
            source_config if isinstance(source_config, dict) else {},
            custom_sources if isinstance(custom_sources, list) else [],
        )

    def _display_timezone_name(self) -> str:
        """Return the plugin timezone override or AstrBot's global timezone."""
        configured = str(self.config.get("timezone", "") or "").strip()
        if configured:
            return configured
        try:
            astrbot_config = self.context.get_config()
        except (AttributeError, TypeError):
            return ""
        return str(astrbot_config.get("timezone", "") or "").strip()

    def _display_datetime(self) -> datetime:
        """Return the current time in the configured display timezone."""
        timezone_name = self._display_timezone_name()
        if not timezone_name:
            return datetime.now().astimezone()
        try:
            return datetime.now(ZoneInfo(timezone_name))
        except (ValueError, ZoneInfoNotFoundError):
            logger.warning(
                "Invalid status image timezone %r; using the system timezone.",
                timezone_name,
            )
            return datetime.now().astimezone()

    async def _fetch_sources(self) -> list[SourceResult]:
        """Fetch all enabled sources under a lock shared with the query command."""
        if self._session is None or self._session.closed:
            raise RuntimeError("Status monitor HTTP session is not available")
        async with self._fetch_lock:
            specs = self._source_specs()
            results = await fetch_all_sources(
                self._session,
                specs,
                bool(self.config.get("notify_maintenance", False)),
            )
            self._last_results = results
            for result in results:
                if not result.success:
                    logger.warning(
                        "Status source %s failed: %s",
                        result.spec.name,
                        result.error,
                    )
            return results

    async def _monitor_loop(self) -> None:
        interval_value = self.config.get("poll_interval_seconds", 300)
        try:
            interval = max(60, int(interval_value))
        except (TypeError, ValueError):
            interval = 300
        while not self._stop_event.is_set():
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected global status monitor cycle failure.")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    def _configured_groups(self) -> list[str]:
        """Normalize group_whitelist entries: trim, deduplicate, and return.

        Entries may be pure digit group IDs, group_openid strings, or full
        UMO strings (platform_id:MessageType:session_id). Format-specific
        validation is deferred to _targets().
        """
        raw_groups = self.config.get("group_whitelist", [])
        if not isinstance(raw_groups, list):
            return []
        entries: list[str] = []
        for raw_group in raw_groups:
            entry = str(raw_group).strip()
            if not entry or entry in entries:
                continue
            entries.append(entry)
        return entries

    def _platform_type(self) -> str:
        """Return the configured platform adapter type name."""
        raw = str(self.config.get("platform_type", "aiocqhttp") or "").strip()
        if not raw:
            return "aiocqhttp"
        known = {"aiocqhttp", "qq_official"}
        if raw not in known:
            logger.warning(
                "Unknown platform_type %r in configuration; known types: %s. "
                "Falling back to 'aiocqhttp' for plain group ID resolution. "
                "Use full UMO in group_whitelist to target other platforms.",
                raw,
                ", ".join(sorted(known)),
            )
            return "aiocqhttp"
        return raw

    def _resolve_platform_id(self) -> str | None:
        configured_id = str(self.config.get("platform_id", "")).strip()
        adapter_type = self._platform_type()
        manager = getattr(self.context, "platform_manager", None)
        platforms = manager.get_insts() if manager is not None else []
        matched_platforms = []
        for platform in platforms:
            try:
                metadata = platform.meta()
            except Exception:
                continue
            if metadata.name == adapter_type:
                matched_platforms.append(metadata)

        resolved: str | None = None
        if configured_id:
            match = next(
                (item for item in matched_platforms if str(item.id) == configured_id),
                None,
            )
            if match is not None:
                resolved = str(match.id)
            else:
                warning = (
                    f"Configured platform_id {configured_id!r} is not an active "
                    f"{adapter_type} instance."
                )
                if warning != self._platform_warning:
                    logger.warning(warning)
                    self._platform_warning = warning
                return None
        elif len(matched_platforms) == 1:
            resolved = str(matched_platforms[0].id)
        else:
            warning = (
                f"Cannot auto-select {adapter_type} platform: expected exactly one "
                f"active instance, found {len(matched_platforms)}."
            )
            if warning != self._platform_warning:
                logger.warning(warning)
                self._platform_warning = warning
            return None
        self._platform_warning = ""
        return resolved

    def _targets(self, groups: list[str]) -> dict[str, str]:
        if not groups:
            return {}
        targets: dict[str, str] = {}
        plain_groups: list[str] = []
        active_ids: set[str] | None = None
        for entry in groups:
            if ":" in entry:
                # Full UMO — validate format and platform existence.
                try:
                    from astrbot.core.platform.message_session import MessageSesion

                    session = MessageSesion.from_str(entry)
                except Exception:
                    logger.warning(
                        "Invalid UMO format in group_whitelist: %r, skipping.", entry
                    )
                    continue
                if active_ids is None:
                    manager = getattr(self.context, "platform_manager", None)
                    platforms = manager.get_insts() if manager is not None else []
                    active_ids = set()
                    for p in platforms:
                        try:
                            active_ids.add(str(p.meta().id))
                        except Exception:
                            continue
                if str(session.platform_id) not in active_ids:
                    logger.warning(
                        "UMO %r references unknown platform_id %r, skipping.",
                        entry,
                        session.platform_id,
                    )
                    continue
                targets[entry] = entry
            else:
                plain_groups.append(entry)
        if plain_groups:
            platform_id = self._resolve_platform_id()
            if platform_id:
                seen_umos = set(targets.values())
                for group_id in plain_groups:
                    umo = f"{platform_id}:GroupMessage:{group_id}"
                    if umo in seen_umos:
                        continue
                    seen_umos.add(umo)
                    targets[f"{platform_id}|{group_id}"] = umo
        return targets

    def _prune_disabled_sources(self, enabled_source_ids: set[str]) -> None:
        sources_state = self._state["sources"]
        disabled = set(sources_state) - enabled_source_ids
        for source_id in disabled:
            sources_state.pop(source_id, None)
        if not disabled:
            return
        for delivered in self._state["deliveries"].values():
            if not isinstance(delivered, dict):
                continue
            for issue_key in list(delivered):
                if issue_key.split(":", 1)[0] in disabled:
                    delivered.pop(issue_key, None)

    def _prune_removed_groups(self, groups: list[str]) -> None:
        allowed = set(groups)
        deliveries = self._state["deliveries"]
        for target_key in list(deliveries):
            group_id = target_key.rsplit("|", 1)[-1]
            if group_id not in allowed:
                deliveries.pop(target_key, None)

    def _reconcile_source(
        self,
        result: SourceResult,
        targets: dict[str, str],
    ) -> dict[str, list[tuple[str, Issue]]]:
        """Reconcile one successful source result with persisted delivery state.

        Args:
            result: Successfully parsed current source state.
            targets: Target keys mapped to unified message origins.

        Returns:
            Per-target alert events that still need delivery.
        """
        sources_state = self._state["sources"]
        source_state = sources_state.setdefault(
            result.spec.source_id,
            {"issues": {}, "missing_counts": {}, "recoveries": {}},
        )
        previous_raw = source_state.get("issues", {})
        previous = {
            issue_id: Issue.from_dict(value)
            for issue_id, value in previous_raw.items()
            if isinstance(value, dict)
        }
        missing_counts = source_state.get("missing_counts", {})
        if not isinstance(missing_counts, dict):
            missing_counts = {}
        recoveries_raw = source_state.get("recoveries", {})
        if not isinstance(recoveries_raw, dict):
            recoveries_raw = {}
        recoveries = {
            issue_id: Issue.from_dict(value)
            for issue_id, value in recoveries_raw.items()
            if isinstance(value, dict)
        }

        current = dict(result.issues)
        for issue_id in current:
            missing_counts.pop(issue_id, None)
            recoveries.pop(issue_id, None)

        for issue_id, old_issue in previous.items():
            if issue_id in current:
                continue
            explicitly_resolved = issue_id in result.resolved_issue_ids
            if result.spec.kind == "rss" and not explicitly_resolved:
                missing_count = int(missing_counts.get(issue_id, 0)) + 1
                missing_counts[issue_id] = missing_count
                if missing_count < 2:
                    current[issue_id] = old_issue
                    continue
            missing_counts.pop(issue_id, None)
            recoveries[issue_id] = old_issue

        source_state["issues"] = {
            issue_id: issue.to_dict() for issue_id, issue in current.items()
        }
        source_state["missing_counts"] = missing_counts
        source_state["recoveries"] = {
            issue_id: issue.to_dict() for issue_id, issue in recoveries.items()
        }

        deliveries = self._state["deliveries"]
        pending: dict[str, list[tuple[str, Issue]]] = {}
        for target_key in targets:
            delivered = deliveries.setdefault(target_key, {})
            if not isinstance(delivered, dict):
                delivered = {}
                deliveries[target_key] = delivered
            events: list[tuple[str, Issue]] = []
            for issue_id, issue in current.items():
                issue_key = issue.key
                delivered_fingerprint = delivered.get(issue_key)
                if delivered_fingerprint == issue.fingerprint:
                    continue
                if issue_id not in previous:
                    stage = "new"
                elif delivered_fingerprint is None:
                    stage = "current"
                else:
                    stage = "update"
                events.append((stage, issue))

            for issue in recoveries.values():
                issue_key = issue.key
                recovery_fingerprint = f"recovered:{issue.fingerprint}"
                delivered_fingerprint = delivered.get(issue_key)
                if delivered_fingerprint is None:
                    delivered[issue_key] = recovery_fingerprint
                    continue
                if delivered_fingerprint != recovery_fingerprint:
                    events.append(("recovered", issue))
            if events:
                pending[target_key] = events
        return pending

    async def _send_events(
        self,
        unified_message_origin: str,
        source_name: str,
        events: list[tuple[str, Issue]],
        translations: dict[str, str] | None = None,
    ) -> bool:
        language = normalize_language(self.config.get("display_language", "bilingual"))
        if translations is None:
            translations = await self._translator.translate_issues(
                (issue for _, issue in events),
                bool(self.config.get("enable_ai_translation", True))
                and language != "en-US",
                str(self.config.get("translation_provider_id", "")).strip(),
            )
        try:
            generated_at = self._display_datetime()
            png = await asyncio.to_thread(
                render_alert_card,
                source_name,
                events,
                translations,
                language,
                generated_at.tzinfo,
                str(self.config.get("card_theme", "paper")),
                generated_at,
            )
            chain = MessageChain([Image.fromBytes(png)])
        except Exception:
            logger.exception("Failed to render status alert card; using text fallback.")
            chain = MessageChain(
                [
                    Plain(
                        build_alert_fallback(
                            source_name,
                            events,
                            translations,
                            language,
                        )
                    )
                ]
            )
        try:
            sent = await self.context.send_message(unified_message_origin, chain)
            if not sent:
                logger.warning(
                    "No platform matched status alert target %s.",
                    unified_message_origin,
                )
            return bool(sent)
        except Exception:
            logger.exception(
                "Failed to send status alert to %s.",
                unified_message_origin,
            )
            return False

    def _mark_delivered(
        self,
        target_key: str,
        events: list[tuple[str, Issue]],
    ) -> None:
        delivered = self._state["deliveries"].setdefault(target_key, {})
        for stage, issue in events:
            delivered[issue.key] = (
                f"recovered:{issue.fingerprint}"
                if stage == "recovered"
                else issue.fingerprint
            )

    def _cleanup_recoveries(
        self,
        source_id: str,
        target_keys: set[str],
        has_configured_groups: bool,
    ) -> None:
        source_state = self._state["sources"].get(source_id, {})
        recoveries_raw = source_state.get("recoveries", {})
        if not isinstance(recoveries_raw, dict):
            return
        if has_configured_groups and not target_keys:
            return
        deliveries = self._state["deliveries"]
        for issue_id, value in list(recoveries_raw.items()):
            if not isinstance(value, dict):
                recoveries_raw.pop(issue_id, None)
                continue
            issue = Issue.from_dict(value)
            recovery_fingerprint = f"recovered:{issue.fingerprint}"
            if all(
                deliveries.get(target_key, {}).get(issue.key) == recovery_fingerprint
                for target_key in target_keys
            ):
                recoveries_raw.pop(issue_id, None)
                for delivered in deliveries.values():
                    if isinstance(delivered, dict):
                        delivered.pop(issue.key, None)

    async def _run_cycle(self) -> None:
        results = await self._fetch_sources()
        successful_results = [result for result in results if result.success]
        self._prune_disabled_sources({result.spec.source_id for result in results})
        groups = self._configured_groups()
        self._prune_removed_groups(groups)
        targets = self._targets(groups)

        reconciled: list[tuple[SourceResult, dict[str, list[tuple[str, Issue]]]]] = []
        initialized_sources = set(self._state.get("initialized_sources", []))
        notify_existing = bool(
            self.config.get("notify_existing_on_first_startup", True)
        )
        for result in successful_results:
            pending = self._reconcile_source(result, targets)
            if not notify_existing and result.spec.source_id not in initialized_sources:
                for target_key, events in pending.items():
                    self._mark_delivered(target_key, events)
                pending = {}
                logger.info(
                    "Recorded initial status baseline for %s without notification.",
                    result.spec.name,
                )
            initialized_sources.add(result.spec.source_id)
            reconciled.append((result, pending))
        self._state["initialized_sources"] = sorted(initialized_sources)

        language = normalize_language(self.config.get("display_language", "bilingual"))
        changed_issues = [
            issue
            for _, pending in reconciled
            for events in pending.values()
            for _, issue in events
        ]
        translations = await self._translator.translate_issues(
            changed_issues,
            bool(self.config.get("enable_ai_translation", True))
            and language != "en-US",
            str(self.config.get("translation_provider_id", "")).strip(),
        )

        for result, pending in reconciled:
            for target_key, events in pending.items():
                if await self._send_events(
                    targets[target_key],
                    result.spec.name,
                    events,
                    translations,
                ):
                    self._mark_delivered(target_key, events)
            self._cleanup_recoveries(
                result.spec.source_id,
                set(targets),
                bool(groups),
            )
        await self.put_kv_data(STATE_KEY, self._state)
        if self._translator.dirty:
            await self.put_kv_data(
                TRANSLATION_CACHE_KEY,
                self._translator.dump_cache(),
            )
            self._translator.dirty = False

    @filter.command("厂商状态", alias={"vendor_status"})
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL
    )
    async def vendor_status(self, event: AstrMessageEvent):
        """Query all enabled vendor sources and return a current status image."""
        try:
            results = await self._fetch_sources()
            issues = [
                issue
                for result in results
                if result.success
                for issue in result.issues.values()
            ]
            translations = await self._translator.translate_issues(
                issues,
                bool(self.config.get("enable_ai_translation", True))
                and normalize_language(self.config.get("display_language", "bilingual"))
                != "en-US",
                str(self.config.get("translation_provider_id", "")).strip(),
            )
            png = await asyncio.to_thread(
                render_overview,
                results,
                translations,
                normalize_language(self.config.get("display_language", "bilingual")),
                self._display_datetime(),
                str(self.config.get("card_theme", "paper")),
            )
            if self._translator.dirty:
                await self.put_kv_data(
                    TRANSLATION_CACHE_KEY,
                    self._translator.dump_cache(),
                )
                self._translator.dirty = False
            yield event.chain_result([Image.fromBytes(png)])
        except Exception:
            logger.exception("Failed to build on-demand vendor status overview.")
            yield event.plain_result(
                "厂商状态查询失败，请检查 AstrBot 日志和网络配置。"
            )
