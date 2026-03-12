"""APScheduler setup with DST-aware scheduling and quiet hours."""

from datetime import datetime, time
from typing import Callable, Optional

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from .config import get_config

log = structlog.get_logger()


class Scheduler:
    """Manages scheduled jobs for digest generation and cleanup."""

    def __init__(self):
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._paused = False

        # Callbacks
        self.on_digest_time: Optional[Callable] = None
        self.on_cleanup_time: Optional[Callable] = None

    def setup(self) -> None:
        """Set up the scheduler with configured jobs."""
        config = get_config()
        tz = pytz.timezone(config.digest.timezone)

        self._scheduler = AsyncIOScheduler(timezone=tz)

        # Parse digest schedule time
        digest_hour, digest_minute = self._parse_time(config.digest.schedule)

        # Daily digest job
        self._scheduler.add_job(
            self._run_digest,
            CronTrigger(hour=digest_hour, minute=digest_minute, timezone=tz),
            id="daily_digest",
            name="Daily Digest Generation",
            replace_existing=True,
        )

        # Parse cleanup schedule time
        cleanup_hour, cleanup_minute = self._parse_time(
            config.data_retention.cleanup_schedule
        )

        # Daily cleanup job
        self._scheduler.add_job(
            self._run_cleanup,
            CronTrigger(hour=cleanup_hour, minute=cleanup_minute, timezone=tz),
            id="daily_cleanup",
            name="Daily Data Cleanup",
            replace_existing=True,
        )

        log.info(
            "scheduler_configured",
            digest_time=config.digest.schedule,
            cleanup_time=config.data_retention.cleanup_schedule,
            timezone=config.digest.timezone,
        )

    def _parse_time(self, time_str: str) -> tuple[int, int]:
        """Parse a HH:MM time string into hour and minute."""
        parts = time_str.split(":")
        return int(parts[0]), int(parts[1])

    def _is_quiet_hours(self) -> bool:
        """Check if currently in quiet hours."""
        config = get_config()

        if not config.quiet_hours.enabled:
            return False

        tz = pytz.timezone(config.digest.timezone)
        now = datetime.now(tz).time()

        start_hour, start_minute = self._parse_time(config.quiet_hours.start)
        end_hour, end_minute = self._parse_time(config.quiet_hours.end)

        start = time(start_hour, start_minute)
        end = time(end_hour, end_minute)

        # Handle overnight quiet hours (e.g., 22:00 - 08:00)
        if start > end:
            return now >= start or now <= end
        else:
            return start <= now <= end

    async def _run_digest(self) -> None:
        """Run the digest generation job."""
        if self._paused:
            log.info("digest_skipped_paused")
            return

        if self._is_quiet_hours():
            log.info("digest_skipped_quiet_hours")
            return

        if self.on_digest_time:
            log.info("digest_job_triggered")
            try:
                await self.on_digest_time()
            except Exception as e:
                log.error("digest_job_error", error=str(e))
        else:
            log.warning("digest_callback_not_set")

    async def _run_cleanup(self) -> None:
        """Run the data cleanup job."""
        if self.on_cleanup_time:
            log.info("cleanup_job_triggered")
            try:
                await self.on_cleanup_time()
            except Exception as e:
                log.error("cleanup_job_error", error=str(e))
        else:
            log.warning("cleanup_callback_not_set")

    def start(self) -> None:
        """Start the scheduler."""
        if self._scheduler is None:
            self.setup()

        self._scheduler.start()
        log.info("scheduler_started")

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            log.info("scheduler_stopped")

    def pause(self) -> None:
        """Pause scheduled jobs."""
        self._paused = True
        log.info("scheduler_paused")

    def resume(self) -> None:
        """Resume scheduled jobs."""
        self._paused = False
        log.info("scheduler_resumed")

    def is_paused(self) -> bool:
        """Check if scheduler is paused."""
        return self._paused

    def is_quiet_hours(self) -> bool:
        """Public method to check quiet hours status."""
        return self._is_quiet_hours()

    def get_next_digest_time(self) -> Optional[datetime]:
        """Get the next scheduled digest time."""
        if self._scheduler:
            job = self._scheduler.get_job("daily_digest")
            if job:
                return job.next_run_time
        return None

    def get_jobs(self) -> list[dict]:
        """Get info about scheduled jobs."""
        if not self._scheduler:
            return []

        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": job.next_run_time.isoformat()
                    if job.next_run_time
                    else None,
                }
            )
        return jobs


# Global scheduler instance
_scheduler: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
