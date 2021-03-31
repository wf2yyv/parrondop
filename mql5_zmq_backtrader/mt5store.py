from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import zmq
import collections
from datetime import datetime
import threading

from mql5_zmq_backtrader.adapter import PositionAdapter, OrderAdapter, BalanceAdapter

import backtrader as bt
from backtrader.metabase import MetaParams
from backtrader.utils.py3 import queue, with_metaclass
import sys


class MTraderError(Exception):
    def __init__(self, *args, **kwargs):
        default = 'Meta Trader 5 ERROR'
        if not (args or kwargs):
            args = (default)
        super(MTraderError, self).__init__(*args, **kwargs)


class ServerConfigError(MTraderError):
    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)


class ServerDataError(MTraderError):
    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)


class TimeFrameError(MTraderError):
    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)


class StreamError(MTraderError):
    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)


class MTraderAPI:
    """
    This class implements Python side for MQL5 JSON API
    See https://github.com/khramkov/MQL5-JSON-API for docs
    """
    # TODO: unify error handling

    def __init__(self, host=None):
        self.HOST = host or 'localhost'
        self.SYS_PORT = 15555       # REP/REQ port
        self.DATA_PORT = 15556      # PUSH/PULL port
        self.LIVE_PORT = 15557      # PUSH/PULL port
        self.EVENTS_PORT = 15558    # PUSH/PULL port

        # ZeroMQ timeout in miliseconds
        self.SYS_TIMEOUT = 1000
        self.DATA_TIMEOUT = 10000
        self.REQUEST_RETRIES = 3  # Lazy Pirate implementation
        self.sequence = 0  # Lazy Pirate request sequence
        # initialise ZMQ context
        self.context = zmq.Context()

        # connect to server sockets
        try:
            self.sys_socket = self.context.socket(zmq.REQ)
            # set port timeout
            self.sys_socket.RCVTIMEO = self.SYS_TIMEOUT
            self.sys_socket.connect(
                'tcp://{}:{}'.format(self.HOST, self.SYS_PORT))
            # Lazy Pirate implementation
            self.poll = zmq.Poller()
            self.poll.register(self.sys_socket, zmq.POLLIN)

            self.data_socket = self.context.socket(zmq.PULL)
            # set port timeout
            self.data_socket.RCVTIMEO = self.DATA_TIMEOUT
            self.data_socket.connect(
                'tcp://{}:{}'.format(self.HOST, self.DATA_PORT))
        except zmq.ZMQError:
            raise zmq.ZMQBindError("E: Binding ports ERROR")

    def _send_request(self, data: dict) -> None:
        """Send request to server via ZeroMQ System socket
        Lazy Pirate implementation.
        """
        # ram Caller's name
        print("I: Caller 2 ", sys._getframe(2).f_code.co_name)

        try:
            # ram sequence = 0
            retries_left = self.REQUEST_RETRIES
            while retries_left:
                self.sequence += 1
                request = str(self.sequence).encode()
                print("I: Sending (%s)" % self.sequence)
                print("data ", data)
                self.sys_socket.send_json(data)

                expect_reply = True
                while expect_reply:
                    socks = dict(self.poll.poll(self.SYS_TIMEOUT))
                    if socks.get(self.sys_socket) == zmq.POLLIN:
                        msg = self.sys_socket.recv_string()
                        if not msg:
                            break
                        # terminal received the request
                        if str(msg) == 'OK':
                            print("I: Server replied %s" % msg)
                            retries_left = 0
                            expect_reply = False
                        else:
                            print("E: Malformed reply from server: %s" % msg)

                    else:
                        print("W: No response from server, retrying…")
                        # Socket is confused. Close and remove it.
                        self.sys_socket.setsockopt(zmq.LINGER, 0)
                        self.sys_socket.close()
                        self.poll.unregister(self.sys_socket)
                        retries_left -= 1
                        if retries_left == 0:
                            print("E: Server seems to be offline, abandoning")
                            break
                        print("I: Reconnecting and resending (%s)" %
                              self.sequence)
                        # Create new connection
                        self.sys_socket = self.context.socket(zmq.REQ)
                        self.sys_socket.RCVTIMEO = self.SYS_TIMEOUT
                        self.sys_socket.connect(
                            'tcp://{}:{}'.format(self.HOST, self.SYS_PORT))
                        self.poll.register(self.sys_socket, zmq.POLLIN)
                        self.sys_socket.send_json(data)

            # ram self.context.term()
        except zmq.ZMQError:
            raise zmq.NotDone("E: Sending request ERROR")

    def _pull_reply(self):
        # Get reply from server via Data socket with timeout
        try:
            msg = self.data_socket.recv_json()
        #ram except zmq.ZMQError:
        #ram    raise zmq.NotDone('Data socket timeout ERROR')
        except zmq.Again as e:
            return None
        except zmq.ZMQError as e:
                logger.debug("W: Strange ZMQ behaviour during node-to-node message receiving, experienced {}".format(e))
        return msg

    def live_socket(self, context=None):
        """Connect to socket in a ZMQ context"""
        try:
            context = context or zmq.Context.instance()
            socket = context.socket(zmq.PULL)
            socket.connect('tcp://{}:{}'.format(self.HOST, self.LIVE_PORT))
        except zmq.ZMQError:
            raise zmq.ZMQBindError("E: Live port connection ERROR")
        return socket

    def streaming_socket(self, context=None):
        """Connect to socket in a ZMQ context"""
        try:
            context = context or zmq.Context.instance()
            socket = context.socket(zmq.PULL)
            socket.connect('tcp://{}:{}'.format(self.HOST, self.EVENTS_PORT))
        except zmq.ZMQError:
            raise zmq.ZMQBindError("E: Data port connection ERROR")
        return socket

    def construct_and_send(self, **kwargs) -> dict:
        """Construct a request dictionary from default and send it to server"""

        # default dictionary
        request = {
            "action": None,
            "actionType": None,
            "symbol": None,
            "chartTF": None,
            "fromDate": None,
            "toDate": None,
            "id": None,
            "magic": 1234,
            "volume": None,
            "price": None,
            "stoploss": None,
            "takeprofit": None,
            "expiration": None,
            "deviation": None,
            "comment": None
        }

        # update dict values if exist
        for key, value in kwargs.items():
            if key in request:
                request[key] = value
            else:
                raise KeyError('E: Unknown key in **kwargs ERROR')

        # send dict to server
        self._send_request(request)

        # return server reply
        return self._pull_reply()


class MetaSingleton(MetaParams):
    """Metaclass to make a metaclassed class a singleton"""
    def __init__(cls, name, bases, dct):
        super(MetaSingleton, cls).__init__(name, bases, dct)
        cls._singleton = None

    def __call__(cls, *args, **kwargs):
        if cls._singleton is None:
            cls._singleton = (
                super(MetaSingleton, cls).__call__(*args, **kwargs))

        return cls._singleton


class MTraderStore(with_metaclass(MetaSingleton, object)):
    """
    Singleton class wrapping to control the connections to MetaTrader.

    Balance update occurs at the beginning and after each
    transaction registered by '_t_streaming_events'.
    """

    # TODO: implement stop_limit
    # TODO: Check position ticket

    BrokerCls = None  # broker class will autoregister
    DataCls = None  # data class will auto register

    params = ()

    # The Unix epoch (or Unix time or POSIX time or Unix timestamp)
    _DTEPOCH = datetime(1970, 1, 1)

    # MTrader supported granularities
    _GRANULARITIES = {
        # (bt.TimeFrame.Ticks, 1): 'Ticks',
        (bt.TimeFrame.Minutes, 1): 'M1',
        (bt.TimeFrame.Minutes, 2): 'M2',
        (bt.TimeFrame.Minutes, 3): 'M3',
        (bt.TimeFrame.Minutes, 4): 'M4',
        (bt.TimeFrame.Minutes, 5): 'M5',
        (bt.TimeFrame.Minutes, 6): 'M6',
        (bt.TimeFrame.Minutes, 10): 'M10',
        (bt.TimeFrame.Minutes, 12): 'M12',
        (bt.TimeFrame.Minutes, 15): 'M15',
        (bt.TimeFrame.Minutes, 20): 'M20',
        (bt.TimeFrame.Minutes, 30): 'M30',
        (bt.TimeFrame.Minutes, 60): 'H1',
        (bt.TimeFrame.Minutes, 120): 'H2',
        (bt.TimeFrame.Minutes, 180): 'H3',
        (bt.TimeFrame.Minutes, 240): 'H4',
        (bt.TimeFrame.Minutes, 360): 'H6',
        (bt.TimeFrame.Minutes, 480): 'H8',
        (bt.TimeFrame.Minutes, 720): 'H12',
        (bt.TimeFrame.Days, 1): 'D1',
        (bt.TimeFrame.Weeks, 1): 'W1',
        (bt.TimeFrame.Months, 1): 'MN1',
    }

    # Order type matching with MetaTrader 5
    _ORDEREXECS = {
        # Market Buy order
        (bt.Order.Market, 'buy'): 'ORDER_TYPE_BUY',
        # Market Sell order
        (bt.Order.Market, 'sell'): 'ORDER_TYPE_SELL',
        # Buy Limit pending order
        (bt.Order.Limit, 'buy'): 'ORDER_TYPE_BUY_LIMIT',
        # Sell Limit pending order
        (bt.Order.Limit, 'sell'): 'ORDER_TYPE_SELL_LIMIT',
        # Buy Stop pending order
        (bt.Order.Stop, 'buy'): 'ORDER_TYPE_BUY_STOP',
        # Sell Stop pending order
        (bt.Order.Stop, 'sell'): 'ORDER_TYPE_SELL_STOP',
        # Upon reaching the order price, a pending Buy Limit
        (bt.Order.StopLimit, 'buy'): 'ORDER_TYPE_BUY_STOP_LIMIT',
        # order is placed at the StopLimit price
        # Upon reaching the order price, a pending Sell Limit
        (bt.Order.StopLimit, 'sell'): 'ORDER_TYPE_SELL_STOP_LIMIT',
        # order is placed at the StopLimit price
    }

    @classmethod
    def getdata(cls, *args, **kwargs):
        """Returns `DataCls` with args, kwargs"""
        return cls.DataCls(*args, **kwargs)

    @classmethod
    def getbroker(cls, *args, **kwargs):
        """Returns broker with *args, **kwargs from registered `BrokerCls`"""
        return cls.BrokerCls(*args, **kwargs)

    def __init__(self, host='localhost'):
        super(MTraderStore, self).__init__()

        self.notifs = collections.deque()  # store notifications for cerebro

        self._env = None  # reference to cerebro for general notifications
        self.broker = None  # broker instance
        self.datas = list()  # datas that have registered over start

        self._orders = collections.OrderedDict()  # map order.ref to oid
        self._ordersrev = collections.OrderedDict()  # map oid to order.ref
        self._orders_type = dict()  # keeps order types

        self.oapi = MTraderAPI(host)

        self._cash = 0.0
        self._value = 0.0

        self.q_livedata = queue.Queue()

        self._cancel_flag = False

        self.debug = True

    def start(self, data=None, broker=None):
        # Datas require some processing to kickstart data reception
        if data is None and broker is None:
            self.cash = None
            return

        if data is not None:
            self._env = data._env
            # For datas simulate a queue with None to kickstart co
            self.datas.append(data)

            if self.broker is not None:
                self.broker.data_started(data)

        elif broker is not None:
            self.broker = broker
            self.broker_threads()
            self.streaming_events()

    def stop(self):
        # signal end of thread
        if self.broker is not None:
            self.q_ordercreate.put(None)
            self.q_orderclose.put(None)

    def put_notification(self, msg, *args, **kwargs):
        self.notifs.append((msg, args, kwargs))

    def get_notifications(self):
        """Return the pending "store" notifications"""
        self.notifs.append(None)  # put a mark / threads could still append
        return [x for x in iter(self.notifs.popleft, None)]

    def get_positions(self):
        positions = self.oapi.construct_and_send(action="POSITIONS")
        # Error handling
        # if positions["error"]:
        #     raise ServerDataError(positions)
        pos_list = positions.get('positions', [])
        if self.debug:
            print('Open positions: {}.'.format(pos_list))
        return [PositionAdapter(o) for o in pos_list]

    def get_granularity(self, timeframe, compression):
        granularity = self._GRANULARITIES.get((timeframe, compression), None)
        if granularity is None:
            raise ValueError("W: Metatrader 5 doesn't support frame %s with compression %s" %
                             (bt.TimeFrame.getname(timeframe), compression))
        return granularity

    def get_cash(self):
        return self._cash

    def get_value(self):
        return self._value

    def get_balance(self):
        try:
            bal = self.oapi.construct_and_send(action="BALANCE")
        except Exception as e:
            self.put_notification(e)
        # TODO: error handling
        # if bal['error']:
        #     self.put_notification(bal)
        #     continue
        try:
            self._cash = float(bal["balance"])
            self._value = float(bal["equity"])
        except KeyError as e:
            #ram
            self.put_notification(e)
            pass

    def streaming_events(self):
        t = threading.Thread(target=self._t_livedata, daemon=True)
        t.start()

        t = threading.Thread(target=self._t_streaming_events, daemon=True)
        t.start()

    def _t_livedata(self):
        # create socket connection for the Thread
        socket = self.oapi.live_socket()
        while True:
            try:
                last_candle = socket.recv_json()
            except zmq.ZMQError:
                raise zmq.NotDone("Live data ERROR")

            self.q_livedata.put(last_candle)

    def _t_streaming_events(self):
        # create socket connection for the Thread
        socket = self.oapi.streaming_socket()
        while True:
            try:
                transaction = socket.recv_json()
            except zmq.ZMQError:
                raise zmq.NotDone("E: Streaming data ERROR")

            self._transaction(transaction)

    def broker_threads(self):
        self.q_ordercreate = queue.Queue()
        t = threading.Thread(target=self._t_order_create, daemon=True)
        t.start()

        self.q_orderclose = queue.Queue()
        t = threading.Thread(target=self._t_order_cancel, daemon=True)
        t.start()

    def order_create(self, order, stopside=None, takeside=None, **kwargs):
        """Creates an order"""
        okwargs = dict()
        okwargs['action'] = 'TRADE'

        side = 'buy' if order.isbuy() else 'sell'
        order_type = self._ORDEREXECS.get((order.exectype, side), None)
        if order_type is None:
            raise ValueError("W: Wrong order type: %s or side: %s" %
                             (order.exectype, side))

        okwargs['actionType'] = order_type
        okwargs['symbol'] = order.data._dataname
        okwargs['volume'] = abs(order.created.size)

        if order.exectype != bt.Order.Market:
            okwargs['price'] = format(order.created.price)

        if order.valid is None:
            okwargs['expiration'] = 0  # good to cancel
        else:
            okwargs['expiration'] = order.valid  # good to date

        if order.exectype == bt.Order.StopLimit:
            okwargs['price'] = order.created.pricelimit

        # TODO: implement StopTrail
        # if order.exectype == bt.Order.StopTrail:
        #     okwargs['distance'] = order.trailamount

        okwargs['comment'] = dict()

        if stopside is not None and stopside.price is not None:
            okwargs['stoploss'] = stopside.price
            okwargs['comment']['stopside'] = stopside.ref

        if takeside is not None and takeside.price is not None:
            okwargs['takeprofit'] = takeside.price
            okwargs['comment']['takeside'] = takeside.ref

        # set store backtrader order ref as MT5 order magic number
        try:
            okwargs['magic'] = order.info["magic"] #Ram Magic number must be inmutable
        except KeyError:
            print(KeyError)

        okwargs.update(**kwargs)  # anything from the user
        self.q_ordercreate.put((order.ref, okwargs,))

        # notify orders of being submitted
        self.broker._submit(order.ref)
        if stopside is not None and stopside.price is not None:
            self.broker._submit(stopside.ref)
        if takeside is not None and takeside.price is not None:
            self.broker._submit(takeside.ref)

        return order

    def _t_order_create(self):
        while True:
            msg = self.q_ordercreate.get()
            if msg is None:
                break

            oref, okwargs = msg

            try:
                o = self.oapi.construct_and_send(**okwargs)
            except Exception as e:
                self.put_notification(e)
                self.broker._reject(oref)
                return

            if self.debug:
                print(o)

            if o['error']:
                self.put_notification(o['description'])
                self.broker._reject(oref)
                return
            else:
                oid = o['order']

            self._orders[oref] = oid
            self.broker._submit(oref)

            # keeps orders types
            self._orders_type[oref] = okwargs['actionType']
            # maps ids to backtrader order
            self._ordersrev[oid] = oref

    def order_cancel(self, order):
        self.q_orderclose.put(order.ref)
        return order

    def _t_order_cancel(self):
        while True:
            oref = self.q_orderclose.get()
            if oref is None:
                break

            oid = self._orders.get(oref, None)
            if oid is None:
                continue  # the order is no longer there

            # get symbol name
            order = self.broker.orders[oref]
            symbol = order.data._dataname
            # get order type
            order_type = self._orders_type.get(oref, None)

            try:
                if order_type in ['ORDER_TYPE_BUY', 'ORDER_TYPE_SELL']:
                    self.close_position(oid, symbol)
                else:
                    self.cancel_order(oid, symbol)
            except Exception as e:
                self.put_notification(
                    "Order not cancelled: {}, {}".format(oid, e))
                continue

            self._cancel_flag = True
            self.broker._cancel(oref)

    def candles(self, dataname, dtbegin, dtend, timeframe, compression, include_first=False):
        tf = self.get_granularity(timeframe, compression)

        begin = end = None
        if dtbegin:
            begin = int((dtbegin - self._DTEPOCH).total_seconds())
        if dtend:
            end = int((dtbegin - self._DTEPOCH).total_seconds())

        if self.debug:
            print('Fetching: {}, Timeframe: {}, Fromdate: {}'.format(
                dataname, tf, dtbegin))

        data = self.oapi.construct_and_send(action="HISTORY", actionType="DATA", symbol=dataname,
                                            chartTF=tf, fromDate=begin, toDate=end)
        candles = data['data']
        # Remove last unclosed candle
        if not include_first:
            try:
                del candles[-1]
            except:
                pass

        q = queue.Queue()
        for c in candles:
            q.put(c)

        q.put({})
        return q

    '''ram
    def config_server(self, symbol: str, timeframe: str) -> None:
        """Set server terminal symbol and time frame"""
        conf = self.oapi.construct_and_send(action="CONFIG", symbol=symbol, chartTF=timeframe)

        # TODO Error
        # Error handling
        if conf["error"]:
            print(conf)
            if conf["description"] == "Wrong symbol dosn't exist":
                raise ServerConfigError("Symbol dosn't exist")
            self.put_notification(conf["description"])
    '''

    def check_account(self) -> None:
        """Get MetaTrader 5 account settings"""
        # ram Caller's name
        print("I: Caller 3 ", sys._getframe(2).f_code.co_name)

        conf = self.oapi.construct_and_send(action="ACCOUNT")

        # Error handling
        if conf["error"]:
            raise ServerDataError(conf)

        for key, value in conf.items():
            print(key, value, sep=' - ')

    def close_position(self, oid, symbol):
        if self.debug:
            print('Closing position: {}, on symbol: {}'.format(oid, symbol))

        conf = self.oapi.construct_and_send(
            action="TRADE", actionType='POSITION_CLOSE_ID', symbol=symbol, id=oid)
        print(conf)
        # Error handling
        if conf["error"]:
            raise ServerDataError(conf)

    def cancel_order(self, oid, symbol):
        if self.debug:
            print('Cancelling order: {}, on symbol: {}'.format(oid, symbol))

        conf = self.oapi.construct_and_send(
            action="TRADE", actionType='ORDER_CANCEL', symbol=symbol, id=oid)
        print(conf)
        # Error handling
        if conf["error"]:
            raise ServerDataError(conf)

    def _transaction(self, trans):
        # Invoked from Streaming Events. May actually receive an event for an
        # oid which has not yet been returned after creating an order. Hence
        # store if not yet seen, else forward to processer

        oid = oref = None

        try:
            request, reply = trans.values()
        except KeyError:
            raise KeyError(trans)

        # Update balance after transaction
        # self.get_balance()

        if self.debug:
            print(request, reply, sep='\n')

        if request['action'] == 'TRADE_ACTION_DEAL':
            # get order id (matches transaction id)
            oid = request['order']
        elif request['action'] == 'TRADE_ACTION_PENDING':
            oid = request['order']

        elif request['action'] == 'TRADE_ACTION_SLTP':
            pass

        elif request['action'] == 'TRADE_ACTION_MODIFY':
            pass

        elif request['action'] == 'TRADE_ACTION_REMOVE':
            pass

        elif request['action'] == 'TRADE_ACTION_CLOSE_BY':
            pass
        else:
            return

        # try:
        #     oref = self._ordersrev.pop(oid)
        # except KeyError:
        #     raise KeyError(oid)

        if oid in self._orders.values():
            # when an order id exists process transaction
            self._process_transaction(oid, request, reply)
        else:
            # external order created this transaction
            if self._cancel_flag and reply['result'] == 'TRADE_RETCODE_DONE':
                self._cancel_flag = False

                size = float(reply['volume'])
                price = float(reply['price'])
                if request['type'].endswith('_SELL'):
                    size = -size
                for data in self.datas:
                    if data._name == request['symbol']:
                        self.broker._fill_external(data, size, price)
                        break

    def _process_transaction(self, oid, request, reply):
        try:
            # get a reference to a backtrader order based on the order id / trade id
            oref = self._ordersrev[oid]
        except KeyError:
            return

        if request['action'] == 'TRADE_ACTION_PENDING':
            pass

        if reply['result'] == 'TRADE_RETCODE_DONE':
            size = float(reply['volume'])
            price = float(reply['price'])
            if request['type'].endswith('_SELL'):
                size = -size
            self.broker._fill(oref, size, price, reason=request['type'])
