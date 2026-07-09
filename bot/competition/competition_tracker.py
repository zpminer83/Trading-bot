from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal


@dataclass(frozen=True)
class CompetitionConfig:
    raffle_volume_per_ticket: Decimal = Decimal("2500")
    max_raffle_tickets: int = 100
    default_pair_boost: Decimal = Decimal("1")
    default_challenge_multiplier: Decimal = Decimal("1")


@dataclass(frozen=True)
class CompetitionSnapshot:
    week_start: datetime
    weekly_volume: Decimal
    estimated_score: Decimal
    raffle_tickets: int
    pair_volumes: dict[str, Decimal] = field(default_factory=dict)
    pair_boosts: dict[str, Decimal] = field(default_factory=dict)
    challenge_multiplier: Decimal = Decimal("1")


class CompetitionTracker:
    """
    Tracks Algo Arena-style weekly competition metrics.

    Current model:
    - weekly volume is tracked by symbol
    - estimated score = sum(pair_volume * pair_boost) * challenge_multiplier
    - raffle tickets = floor(weekly_volume / 2500), capped at 100
    - weekly window starts Monday 00:00 UTC
    """

    def __init__(
        self,
        config: CompetitionConfig | None = None,
        now: datetime | None = None,
    ):
        self.config = config or CompetitionConfig()
        self.week_start = self.get_week_start(now or self._utc_now())
        self.pair_volumes: dict[str, Decimal] = {}
        self.pair_boosts: dict[str, Decimal] = {}
        self.challenge_multiplier = self.config.default_challenge_multiplier

    def record_trade(
        self,
        symbol: str,
        notional: Decimal,
        timestamp: datetime | None = None,
    ) -> None:
        if not symbol:
            raise ValueError("symbol must not be empty")

        if notional <= 0:
            raise ValueError("notional must be greater than zero")

        ts = timestamp or self._utc_now()
        self.ensure_current_week(ts)

        current_volume = self.pair_volumes.get(symbol, Decimal("0"))
        self.pair_volumes[symbol] = current_volume + notional

    def set_pair_boost(
        self,
        symbol: str,
        boost: Decimal,
    ) -> None:
        if not symbol:
            raise ValueError("symbol must not be empty")

        if boost <= 0:
            raise ValueError("boost must be greater than zero")

        self.pair_boosts[symbol] = boost

    def set_challenge_multiplier(self, multiplier: Decimal) -> None:
        if multiplier <= 0:
            raise ValueError("challenge multiplier must be greater than zero")

        self.challenge_multiplier = multiplier

    def reset_week(self, now: datetime | None = None) -> None:
        self.week_start = self.get_week_start(now or self._utc_now())
        self.pair_volumes.clear()

    def ensure_current_week(self, now: datetime | None = None) -> None:
        current_week_start = self.get_week_start(now or self._utc_now())

        if current_week_start != self.week_start:
            self.week_start = current_week_start
            self.pair_volumes.clear()

    @property
    def weekly_volume(self) -> Decimal:
        return sum(self.pair_volumes.values(), Decimal("0"))

    @property
    def estimated_score(self) -> Decimal:
        score = Decimal("0")

        for symbol, volume in self.pair_volumes.items():
            boost = self.get_pair_boost(symbol)
            score += volume * boost

        return score * self.challenge_multiplier

    @property
    def raffle_tickets(self) -> int:
        tickets = int(self.weekly_volume // self.config.raffle_volume_per_ticket)
        return min(tickets, self.config.max_raffle_tickets)

    def get_pair_boost(self, symbol: str) -> Decimal:
        return self.pair_boosts.get(symbol, self.config.default_pair_boost)

    def snapshot(self) -> CompetitionSnapshot:
        return CompetitionSnapshot(
            week_start=self.week_start,
            weekly_volume=self.weekly_volume,
            estimated_score=self.estimated_score,
            raffle_tickets=self.raffle_tickets,
            pair_volumes=dict(self.pair_volumes),
            pair_boosts=dict(self.pair_boosts),
            challenge_multiplier=self.challenge_multiplier,
        )

    @staticmethod
    def get_week_start(dt: datetime) -> datetime:
        dt = CompetitionTracker._to_utc(dt)

        monday = dt.date() - timedelta(days=dt.weekday())

        return datetime.combine(
            monday,
            time.min,
            tzinfo=timezone.utc,
        )

    @staticmethod
    def _to_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)