from datetime import datetime
from datetime import timedelta
from collections import defaultdict

import numpy as np
import logging

from . import db
from . import models
from . import actions

SNAPSHOT_INTERVAL_SECONDS = 60
DEFAULT_COMMIT_INTERVAL = 100

class OBSnapshotGenerator:
    def __init__(self, exchanges, timestamp,
                 session=db.session,
                 commit_interval=DEFAULT_COMMIT_INTERVAL,
                 snapshot_interval=SNAPSHOT_INTERVAL_SECONDS):
        self.exchanges = exchanges
        self.stop_time = timestamp
        self.commit_interval = commit_interval
        self.snapshot_interval = snapshot_interval
        self.actions_buffer = []
        self.commit_counter = 0  
        self.session = session  

    """
    def _get_connection_window(self, last_update_time, key):
        logging.debug("last time: {}".format(last_update_time))
        for e in self.event_log[key]:
            if e["timestamp"] <= last_update_time:       
                t_current = last_update_time + timedelta(seconds=self.snapshot_interval)
                t_disconnect = self._get_connection_time(t_current, "disconnect", key)
                return t_current, t_disconnect
        t_current =  self._get_connection_time(datetime.min, "connect", key)    
        t_disconnect = self._get_connection_time(datetime.min, "disconnect", key)
        return t_current, t_disconnect
    """

    def _get_connection_window(self, last_update, key):
        connect_time = None
        disconnect_time = self.stop_time
        for con in self.event_log[key]:
            if last_update is not None:
                if con["connection_event"] == "connect" and con["timestamp"] <= last_update:
                    connect_time = con["timestamp"]
                elif con["connection_event"] == "disconnect" and con["timestamp"] > last_update:
                    disconnect_time = con["timestamp"]
                    break
            else:
                if connect_time is None and con["connection_event"] == "connect":
                    connect_time = con["timestamp"]
                elif connect_time is not None and con["connection_event"] == "disconnect":
                    disconnect_time = con["timestamp"]
        return connect_time, disconnect_time

    # TODO: add test!
    def run(self):
        exchange_markets = self._query_exchange_markets()
        parsed_exchange_markets = self._parse_exchange_markets(exchange_markets)  
        connection_events = self._query_connection_events()
        self._parse_connection_events(connection_events)
        for exchange in parsed_exchange_markets:
            snapshot_times = self._query_latest_snapshot(exchange)
            parsed_snapshot_times = self._parse_snapshot_times(snapshot_times) 
            for market in parsed_exchange_markets[exchange]:
                market_key = exchange+market["buy_sym_id"]+market["sell_sym_id"]
                last_update_time = self._get_last_update_time(market_key, parsed_snapshot_times)
                logging.debug("last snapshot update: {}".format(last_update_time))
                connect_time, disconnect_time = self._get_connection_window(last_update_time, market_key)
                logging.info("order book snapshot - {} - '{}-{}'".format(exchange.upper(), market["buy_sym_id"], market["sell_sym_id"]))
                logging.debug("snapshot window - start time: {} - end time: {}".format(connect_time, disconnect_time))
                self._generate_snapshots(connect_time, disconnect_time, last_update_time, market, exchange)
        if len(self.actions_buffer) > 0:
            self.session.commit()
            self.commit_counter += 1
        logging.info("completed order book snapshots - total commits: {}".format(self.commit_counter))

    def _generate_snapshots(self, connect_time, disconnect_time, snapshot_time, market, exchange):
        market_key = exchange+market["buy_sym_id"]+market["sell_sym_id"]
        if snapshot_time is not None:
            snapshot_time += timedelta(seconds=self.snapshot_interval)
        else:
            snapshot_time = connect_time + timedelta(seconds=self.snapshot_interval)
        while snapshot_time < self.stop_time:
            start_time = datetime.strftime(connect_time, '%Y-%m-%d %H:%M:%S.%f')
            stop_time = datetime.strftime(snapshot_time, '%Y-%m-%d %H:%M:%S.%f')
            logging.debug("start: {}, end: {}".format(connect_time, snapshot_time))
            order_books = self._query_order_books(exchange, market["buy_sym_id"], market["sell_sym_id"], start_time, stop_time)
            full_ob, quartile_ob = self._parse_order_books(order_books)
            if full_ob is None:
                snapshot_time += timedelta(seconds=self.snapshot_interval)
                continue
            metadata = dict(timestamp=snapshot_time, exchange_id=market["exchange_id"], buy_sym_id=market["buy_sym_id"], sell_sym_id=market["sell_sym_id"])
            snapshot = self._generate_snapshot(full_ob, quartile_ob, metadata)
            action = actions.InsertAction([snapshot])
            action.execute(self.session)
            if len(self.actions_buffer) >= self.commit_interval:
                self.session.commit()
                self.commit_counter += 1
                logging.debug(" {}-{} - order book snapshot commit[{}]".format(market["buy_sym_id"], market["sell_sym_id"],self.commit_counter))
            else:
                self.actions_buffer.append(action)
            logging.debug("order book snapshot created - {}".format(snapshot_time))
            snapshot_time += timedelta(seconds=self.snapshot_interval)
            if snapshot_time >= disconnect_time:
                snapshot_time, disconnect_time = self._get_connection_window(snapshot_time, market_key)
                connect_time = snapshot_time
                if disconnect_time == self.stop_time:
                    snapshot_time = self.stop_time
                logging.debug("snapshot window - start time: {} - end time: {}".format(connect_time, disconnect_time))

    def _get_connection_time(self, prev_time, connection_event, key):
        for e in self.event_log[key]:
            if e["connection_event"] == connection_event and e["timestamp"] > prev_time:
                return e["timestamp"]
        return self.stop_time

    def _query_exchange_markets(self):
        query = (
            """
            select e.name, buy_sym_id, sell_sym_id, e.id
            from events
            inner join exchanges e on events.exchange_id = e.id
            where data_collected = 'agg_order_book'
            group by e.name, buy_sym_id, sell_sym_id, e.id
            """
        )
        return self.session.execute(query)

    def _query_latest_snapshot(self, exchange):
        query = (
            """
            select max(timestamp), buy_sym_id, sell_sym_id, exchange_id, e.name
            from order_book_snapshots
            inner join exchanges e on order_book_snapshots.exchange_id = e.id
            where e.name = :exchange
            group by buy_sym_id, sell_sym_id, exchange_id, e.name;
            """
        )
        return self.session.execute(query, {"exchange": exchange.lower()})

    def _query_connection_events(self):
        query = (
            """
            select events.id, e.name, timestamp, connection_event, data_collected, buy_sym_id, sell_sym_id
            from events
            inner join exchanges e on events.exchange_id = e.id
            where (data_collected = 'agg_order_book'
            or (connection_event = 'disconnect' and data_collected = 'all'))
            order by buy_sym_id, sell_sym_id, timestamp asc
            """
        )
        return self.session.execute(query)

    def _get_last_update_time(self, key, updates):
        if key in updates.keys():
            return updates[key]
        else:
            return None

    def _parse_snapshot_times(self, snapshot_updates):
        update_times = {}
        for update in list(snapshot_updates):
            update_times[str(update[4])+str(update[1])+str(update[2])] = update[0]
        return update_times

    def _parse_connection_events(self, events):
        self.event_log = defaultdict(list)
        for event in list(events):
            self.event_log[str(event[1])+str(event[5])+str(event[6])].append(dict(
                timestamp=event[2],
                id=event[0],
                connection_event=event[3]
            ))

    def _parse_exchange_markets(self, exchange_markets):
        exchange_markets = list(exchange_markets)
        if len(exchange_markets) == 0:
            return None
        all_markets = defaultdict(list)
        for market in exchange_markets:
            all_markets[market[0]].append(dict(buy_sym_id=market[1], sell_sym_id=market[2], exchange=market[0], exchange_id=market[3]))
        return all_markets

    def _query_order_books(self, exchange, buy_sym_id, sell_sym_id, start_time, stop_time):
        query = (
            """
            with order_book as (
            with latest_orders as (
            select order_type, price, max(last_update_id) max_update_id
            from aggregate_orders
            where timestamp <= :stop_time
            and timestamp >= :start_time
            group by aggregate_orders.price,
                        aggregate_orders.order_type)
            select aggregate_orders.id,
                order_type,
                price,
                size,
                last_update_id,
                timestamp,
                name,
                buy_sym_id,
                sell_sym_id
            from aggregate_orders
                    inner join exchanges on aggregate_orders.exchange_id = exchanges.id
            where (order_type, price, last_update_id) in (select * from latest_orders)
            and size > 0
            and buy_sym_id = :buy_sym_id
            and sell_sym_id = :sell_sym_id
            and name = :exchange
            and timestamp <= :stop_time
            and timestamp >= :start_time
            )
            select *, true as is_quartile
            from order_book where (order_book.order_type = 'bid' and order_book.price >= (
                select percentile_disc(0.75) within group (order by order_book.price)
                from order_book
                    where order_book.order_type = 'bid'
            )) or (order_book.order_type = 'ask' and order_book.price <= (
                select percentile_disc(0.25) within group (order by order_book.price)
                from order_book
                    where order_book.order_type = 'ask'
            ))
            union all
            select *, false as is_quartile from order_book
            order by timestamp desc
            """
        )
        return self.session.execute(query, {"stop_time": stop_time, "start_time": start_time, "buy_sym_id": buy_sym_id.upper(), "sell_sym_id": sell_sym_id.upper(), "exchange": exchange.lower()})

    # TODO: add test!
    def _parse_order_books(self, order_books):
        full_order_book = []
        quartile_order_book = []
        order_books = list(order_books)
        if len(order_books) == 0:
            logging.debug("empty order book to be parsed")
            return None, None
        for order in order_books:
            if order[9] == True:
                # order is in quartile order book
                quartile_order_book.append(dict(
                    order_type=order[1],
                    price=order[2],
                    size=order[3]
                ))
            else:
                # order is in full order book
                full_order_book.append(dict(
                    order_type=order[1],
                    price=order[2],
                    size=order[3]
                ))
        logging.debug("ob_snapshot_generator - parsed order books: 'full order book' ({} orders), 'quartile order book' ({} orders)". format(len(full_order_book), len(quartile_order_book)))
        return full_order_book, quartile_order_book
        
    # TODO: add test!
    def _generate_snapshot(self, full_ob, quartile_ob, metadata):
        full_ob_stats = self._compute_stats(full_ob)
        quartile_ob_stats = self._compute_stats(quartile_ob)
        return models.OrderBookSnapshot(
            exchange_id=metadata["exchange_id"],
            sell_sym_id=metadata["sell_sym_id"],
            buy_sym_id=metadata["buy_sym_id"],
            timestamp=metadata["timestamp"],
            spread=full_ob_stats["spread"],
            bids_volume=full_ob_stats["bids_volume"], 
            asks_volume=full_ob_stats["asks_volume"],
            bids_count=full_ob_stats["bids_count"],
            asks_count=full_ob_stats["asks_count"],
            bids_price_stddev=full_ob_stats["bids_price_stddev"], 
            asks_price_stddev=full_ob_stats["asks_price_stddev"], 
            bids_price_mean=full_ob_stats["bids_price_mean"],
            asks_price_mean= full_ob_stats["asks_price_mean"],
            min_ask_price=full_ob_stats["min_ask_price"],
            min_ask_size=full_ob_stats["min_ask_size"],
            max_bid_price=full_ob_stats["max_bid_price"], 
            max_bid_size=full_ob_stats["max_bid_size"], 
            bid_price_median=full_ob_stats["bid_price_median"], 
            ask_price_median=full_ob_stats["ask_price_median"], 
            bid_price_upper_quartile=quartile_ob_stats["bid_price_upper_quartile"],
            ask_price_lower_quartile=quartile_ob_stats["ask_price_lower_quartile"],
            bids_volume_upper_quartile=quartile_ob_stats["bids_volume"],
            asks_volume_lower_quartile=quartile_ob_stats["asks_volume"],
            bids_count_upper_quartile=quartile_ob_stats["bids_count"],
            asks_count_lower_quartile=quartile_ob_stats["asks_count"],
            bids_price_stddev_upper_quartile=quartile_ob_stats["bids_price_stddev"],
            asks_price_stddev_lower_quartile=quartile_ob_stats["asks_price_stddev"],
            bids_price_mean_upper_quartile=quartile_ob_stats["bids_price_mean"],
            asks_price_mean_lower_quartile=quartile_ob_stats["asks_price_mean"]
        )

    def _compute_stats(self, order_book):
        bids = list(filter(lambda x: x["order_type"] == "bid", order_book))
        asks = list(filter(lambda x: x["order_type"] == "ask", order_book))
        bid_prices = list(map(lambda x: x["price"], bids))
        ask_prices = list(map(lambda x: x["price"], asks))
        avg_bid_price = float(sum(d['price'] for d in bids)) / len(bids)
        avg_ask_price = float(sum(d['price'] for d in asks)) / len(asks)
        bids_volume = float(sum(d['price']*d['size'] for d in bids))
        asks_volume = float(sum(d['price']*d['size'] for d in asks))
        min_ask_size = max(list(d['size'] if d['price'] == min(ask_prices) else 0 for d in asks))
        max_bid_size = max(list(d['size'] if d['price'] == max(bid_prices) else 0 for d in bids))
        spread = float(float(min(ask_prices)) - float(max(bid_prices)))
        return dict(
            spread=spread,
            min_ask_price=min(ask_prices),
            min_ask_size=min_ask_size,
            max_bid_price=max(bid_prices),
            max_bid_size=max_bid_size,
            bids_volume=bids_volume,
            asks_volume=asks_volume,
            bids_count = len(bids),
            asks_count = len(asks),
            bids_price_stddev = np.std(bid_prices),
            asks_price_stddev = np.std(ask_prices),
            bids_price_mean = avg_bid_price,
            asks_price_mean = avg_ask_price,
            bid_price_median = np.median(bid_prices),
            ask_price_median = np.median(ask_prices),
            # the two fields below are only accurate for already preprocessed quartile orderbooks
            bid_price_upper_quartile= min(bid_prices),
            ask_price_lower_quartile=max(ask_prices) 
        )
