import logging
from tenacity import retry, wait_exponential
from betfairlightweight import StreamListener
from betfairlightweight import BetfairError
from betfairlightweight.streaming.stream import BaseStream as BFBaseStream

from .basestream import BaseStream
from ..events.events import RawDataEvent
from ..exceptions import ListenerError

logger = logging.getLogger(__name__)


"""
Custom listener that doesn't do any processing,
helps reduce CPU.
"""


class FlumineListener(StreamListener):
    def _add_stream(self, unique_id, stream_type):
        if stream_type == "marketSubscription":
            return FlumineMarketStream(self)
        elif stream_type == "orderSubscription":
            raise ListenerError("Not expecting an order stream...")
        elif stream_type == "raceSubscription":
            return FlumineRaceStream(self)


class FlumineStream(BFBaseStream):
    def on_process(self, output: list) -> None:
        self.output_queue.put(RawDataEvent(output))

    def __str__(self):
        return "FlumineStream"

    def __repr__(self):
        return "<FlumineStream [%s]>" % len(self._caches)


class FlumineMarketStream(FlumineStream):

    _lookup = "mc"

    def _process(self, market_books, publish_time):
        for market_book in market_books:
            market_id = market_book.get("id")
            if (
                "marketDefinition" in market_book
                and market_book["marketDefinition"]["status"] == "CLOSED"
            ):
                if market_id in self._caches:
                    # removes closed market from cache
                    del self._caches[market_id]
                    logger.info(
                        "[MarketStream: %s] %s removed, %s markets in cache"
                        % (self.unique_id, market_id, len(self._caches))
                    )
            elif self._caches.get(market_id) is None:
                # adds empty object to cache to track live market count
                self._caches[market_id] = object()
                logger.info(
                    "[MarketStream: %s] %s added, %s markets in cache"
                    % (self.unique_id, market_id, len(self._caches))
                )

        self.on_process([self.unique_id, publish_time, market_books])
        self._updates_processed += len(market_books)


class FlumineRaceStream(FlumineStream):

    _lookup = "rc"

    def _process(self, race_updates, publish_time):
        for update in race_updates:
            market_id = update["mid"]
            if self._caches.get(market_id) is None:
                # adds empty object to cache to track live market count
                self._caches[market_id] = object()
                logger.info(
                    "[RaceStream: %s] %s added, %s markets in cache"
                    % (self.unique_id, market_id, len(self._caches))
                )

        self.on_process([self.unique_id, publish_time, race_updates])
        self._updates_processed += len(race_updates)


class DataStream(BaseStream):

    LISTENER = FlumineListener

    def __init__(self, *args, **kwargs):
        BaseStream.__init__(self, *args, **kwargs)
        self._listener = self.LISTENER(output_queue=self.flumine.handler_queue)

    @retry(wait=wait_exponential(multiplier=1, min=2, max=20))
    def run(self) -> None:
        logger.info("Starting DataStream")

        self._stream = self.betting_client.streaming.create_stream(
            unique_id=self.stream_id, listener=self._listener
        )
        try:
            self.stream_id = self._stream.subscribe_to_markets(
                market_filter=self.market_filter,
                market_data_filter=self.market_data_filter,
                conflate_ms=self.conflate_ms,
                initial_clk=self._listener.initial_clk,  # supplying these two values allows a reconnect
                clk=self._listener.clk,
            )
            self._stream.start()
        except BetfairError:
            logger.error("DataStream run error", exc_info=True)
            raise
        except Exception:
            logger.critical("DataStream run error", exc_info=True)
            raise
        logger.info("Stopped DataStream {0}".format(self.stream_id))
