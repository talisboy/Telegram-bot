#!/usr/bin/env python
#
# A library that provides a Python interface to the Telegram Bot API
# Copyright (C) 2015-2021
# Leandro Toledo de Souza <devs@python-telegram-bot.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser Public License for more details.
#
# You should have received a copy of the GNU Lesser Public License
# along with this program.  If not, see [http://www.gnu.org/licenses/].
"""This module contains the class Updater, which tries to make creating Telegram bots intuitive."""

import logging
import ssl
import warnings
from queue import Queue
from signal import SIGABRT, SIGINT, SIGTERM, signal
from threading import Event, Lock, Thread, current_thread
from time import sleep
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Tuple,
    Union,
    no_type_check,
    Generic,
    TypeVar,
    cast,
)

from telegram import TelegramError
from telegram.error import InvalidToken, RetryAfter, TimedOut, Unauthorized
from telegram.ext import Dispatcher
from telegram.utils.deprecate import TelegramDeprecationWarning, set_new_attribute_deprecated
from telegram.utils.helpers import get_signal_name
from telegram.ext.utils.types import BT
from telegram.ext.utils.webhookhandler import WebhookAppClass, WebhookServer


DT = TypeVar('DT', bound=Union[None, Dispatcher])


class Updater(Generic[BT, DT]):
    """
    This class, which employs the :class:`telegram.ext.Dispatcher`, provides a frontend to
    :class:`telegram.Bot` to the programmer, so they can focus on coding the bot. Its purpose is to
    receive the updates from Telegram and to deliver them to said dispatcher. It also runs in a
    separate thread, so the user can interact with the bot, for example on the command line. The
    dispatcher supports handlers for different kinds of data: Updates from Telegram, basic text
    commands and even arbitrary types. The updater can be started as a polling service or, for
    production, use a webhook to receive updates. This is achieved using the WebhookServer and
    WebhookHandler classes.

    Note:
        Must be initialized via :class:`telegram.ext.DispatcherBuilder`.

    Attributes:
        bot (:class:`telegram.Bot`): The bot used with this Updater.
        user_signal_handler (:obj:`function`): Optional. Function to be called when a signal is
            received.
        update_queue (:obj:`Queue`): Queue for the updates.
        dispatcher (:class:`telegram.ext.Dispatcher`): Optional. Dispatcher that handles the
            updates and dispatches them to the handlers.
        running (:obj:`bool`): Indicates if the updater is running.

    """

    __slots__ = (
        'dispatcher',
        'user_signal_handler',
        'bot',
        'logger',
        'update_queue',
        '__exception_event',
        'last_update_id',
        'running',
        '_request',
        'is_idle',
        'httpd',
        '__lock',
        '__threads',
        '__dict__',
    )

    def __init__(self, **kwargs: Any):
        if not kwargs.pop('builder_flag', False):
            warnings.warn(
                '`Dispatcher` instances should be built via the `DispatcherBuilder`.',
                UserWarning,
                stacklevel=2,
            )

        self.user_signal_handler = cast(
            Optional[Callable[[int, object], Any]], kwargs.pop('user_signal_handler')
        )
        self.dispatcher = cast(DT, kwargs.pop('dispatcher'))
        if self.dispatcher:
            self.bot = self.dispatcher.bot
            self.update_queue = self.dispatcher.update_queue
            self.__exception_event = self.dispatcher.exception_event
        else:
            self.bot = cast(BT, kwargs.pop('bot'))
            self.update_queue = cast(Queue, kwargs.pop('update_queue'))
            self.__exception_event = cast(Event, kwargs.pop('exception_event'))

        self.last_update_id = 0
        self.running = False
        self.is_idle = False
        self.httpd = None
        self.__lock = Lock()
        self.__threads: List[Thread] = []
        self.logger = logging.getLogger(__name__)

    def __setattr__(self, key: str, value: object) -> None:
        if key.startswith('__'):
            key = f"_{self.__class__.__name__}{key}"
        if issubclass(self.__class__, Updater) and self.__class__ is not Updater:
            object.__setattr__(self, key, value)
            return
        set_new_attribute_deprecated(self, key, value)

    def _init_thread(self, target: Callable, name: str, *args: object, **kwargs: object) -> None:
        thr = Thread(
            target=self._thread_wrapper,
            name=f"Bot:{self.bot.id}:{name}",
            args=(target,) + args,
            kwargs=kwargs,
        )
        thr.start()
        self.__threads.append(thr)

    def _thread_wrapper(self, target: Callable, *args: object, **kwargs: object) -> None:
        thr_name = current_thread().name
        self.logger.debug('%s - started', thr_name)
        try:
            target(*args, **kwargs)
        except Exception:
            self.__exception_event.set()
            self.logger.exception('unhandled exception in %s', thr_name)
            raise
        self.logger.debug('%s - ended', thr_name)

    def start_polling(
        self,
        poll_interval: float = 0.0,
        timeout: float = 10,
        clean: bool = None,
        bootstrap_retries: int = -1,
        read_latency: float = 2.0,
        allowed_updates: List[str] = None,
        drop_pending_updates: bool = None,
    ) -> Optional[Queue]:
        """Starts polling updates from Telegram.

        Args:
            poll_interval (:obj:`float`, optional): Time to wait between polling updates from
                Telegram in seconds. Default is ``0.0``.
            timeout (:obj:`float`, optional): Passed to :meth:`telegram.Bot.get_updates`.
            drop_pending_updates (:obj:`bool`, optional): Whether to clean any pending updates on
                Telegram servers before actually starting to poll. Default is :obj:`False`.

                .. versionadded :: 13.4
            clean (:obj:`bool`, optional): Alias for ``drop_pending_updates``.

                .. deprecated:: 13.4
                    Use ``drop_pending_updates`` instead.
            bootstrap_retries (:obj:`int`, optional): Whether the bootstrapping phase of the
                :class:`telegram.ext.Updater` will retry on failures on the Telegram server.

                * < 0 - retry indefinitely (default)
                *   0 - no retries
                * > 0 - retry up to X times

            allowed_updates (List[:obj:`str`], optional): Passed to
                :meth:`telegram.Bot.get_updates`.
            read_latency (:obj:`float` | :obj:`int`, optional): Grace time in seconds for receiving
                the reply from server. Will be added to the ``timeout`` value and used as the read
                timeout from server (Default: ``2``).

        Returns:
            :obj:`Queue`: The update queue that can be filled from the main thread.

        """
        if (clean is not None) and (drop_pending_updates is not None):
            raise TypeError('`clean` and `drop_pending_updates` are mutually exclusive.')

        if clean is not None:
            warnings.warn(
                'The argument `clean` of `start_polling` is deprecated. Please use '
                '`drop_pending_updates` instead.',
                category=TelegramDeprecationWarning,
                stacklevel=2,
            )

        drop_pending_updates = drop_pending_updates if drop_pending_updates is not None else clean

        with self.__lock:
            if not self.running:
                self.running = True

                # Create & start threads
                dispatcher_ready = Event()
                polling_ready = Event()
                self._init_thread(self.dispatcher.start, "dispatcher", ready=dispatcher_ready)
                self._init_thread(
                    self._start_polling,
                    "updater",
                    poll_interval,
                    timeout,
                    read_latency,
                    bootstrap_retries,
                    drop_pending_updates,
                    allowed_updates,
                    ready=polling_ready,
                )

                self.logger.debug('Waiting for Dispatcher and polling to start')
                dispatcher_ready.wait()
                polling_ready.wait()

                # Return the update queue so the main thread can insert updates
                return self.update_queue
            return None

    def start_webhook(
        self,
        listen: str = '127.0.0.1',
        port: int = 80,
        url_path: str = '',
        cert: str = None,
        key: str = None,
        clean: bool = None,
        bootstrap_retries: int = 0,
        webhook_url: str = None,
        allowed_updates: List[str] = None,
        force_event_loop: bool = None,
        drop_pending_updates: bool = None,
        ip_address: str = None,
        max_connections: int = 40,
    ) -> Optional[Queue]:
        """
        Starts a small http server to listen for updates via webhook. If :attr:`cert`
        and :attr:`key` are not provided, the webhook will be started directly on
        http://listen:port/url_path, so SSL can be handled by another
        application. Else, the webhook will be started on
        https://listen:port/url_path. Also calls :meth:`telegram.Bot.set_webhook` as required.

        .. versionchanged:: 13.4
            :meth:`start_webhook` now *always* calls :meth:`telegram.Bot.set_webhook`, so pass
            ``webhook_url`` instead of calling ``updater.bot.set_webhook(webhook_url)`` manually.

        Args:
            listen (:obj:`str`, optional): IP-Address to listen on. Default ``127.0.0.1``.
            port (:obj:`int`, optional): Port the bot should be listening on. Default ``80``.
            url_path (:obj:`str`, optional): Path inside url.
            cert (:obj:`str`, optional): Path to the SSL certificate file.
            key (:obj:`str`, optional): Path to the SSL key file.
            drop_pending_updates (:obj:`bool`, optional): Whether to clean any pending updates on
                Telegram servers before actually starting to poll. Default is :obj:`False`.

                .. versionadded :: 13.4
            clean (:obj:`bool`, optional): Alias for ``drop_pending_updates``.

                .. deprecated:: 13.4
                    Use ``drop_pending_updates`` instead.
            bootstrap_retries (:obj:`int`, optional): Whether the bootstrapping phase of the
                :class:`telegram.ext.Updater` will retry on failures on the Telegram server.

                * < 0 - retry indefinitely (default)
                *   0 - no retries
                * > 0 - retry up to X times

            webhook_url (:obj:`str`, optional): Explicitly specify the webhook url. Useful behind
                NAT, reverse proxy, etc. Default is derived from ``listen``, ``port`` &
                ``url_path``.
            ip_address (:obj:`str`, optional): Passed to :meth:`telegram.Bot.set_webhook`.

                .. versionadded :: 13.4
            allowed_updates (List[:obj:`str`], optional): Passed to
                :meth:`telegram.Bot.set_webhook`.
            force_event_loop (:obj:`bool`, optional): Legacy parameter formerly used for a
                workaround on Windows + Python 3.8+. No longer has any effect.

                .. deprecated:: 13.6
                   Since version 13.6, ``tornade>=6.1`` is required, which resolves the former
                   issue.

            max_connections (:obj:`int`, optional): Passed to
                :meth:`telegram.Bot.set_webhook`.

                .. versionadded:: 13.6

        Returns:
            :obj:`Queue`: The update queue that can be filled from the main thread.

        """
        if (clean is not None) and (drop_pending_updates is not None):
            raise TypeError('`clean` and `drop_pending_updates` are mutually exclusive.')

        if clean is not None:
            warnings.warn(
                'The argument `clean` of `start_webhook` is deprecated. Please use '
                '`drop_pending_updates` instead.',
                category=TelegramDeprecationWarning,
                stacklevel=2,
            )

        if force_event_loop is not None:
            warnings.warn(
                'The argument `force_event_loop` of `start_webhook` is deprecated and no longer '
                'has any effect.',
                category=TelegramDeprecationWarning,
                stacklevel=2,
            )

        drop_pending_updates = drop_pending_updates if drop_pending_updates is not None else clean

        with self.__lock:
            if not self.running:
                self.running = True

                # Create & start threads
                webhook_ready = Event()
                dispatcher_ready = Event()
                self._init_thread(self.dispatcher.start, "dispatcher", dispatcher_ready)
                self._init_thread(
                    self._start_webhook,
                    "updater",
                    listen,
                    port,
                    url_path,
                    cert,
                    key,
                    bootstrap_retries,
                    drop_pending_updates,
                    webhook_url,
                    allowed_updates,
                    ready=webhook_ready,
                    ip_address=ip_address,
                    max_connections=max_connections,
                )

                self.logger.debug('Waiting for Dispatcher and Webhook to start')
                webhook_ready.wait()
                dispatcher_ready.wait()

                # Return the update queue so the main thread can insert updates
                return self.update_queue
            return None

    @no_type_check
    def _start_polling(
        self,
        poll_interval,
        timeout,
        read_latency,
        bootstrap_retries,
        drop_pending_updates,
        allowed_updates,
        ready=None,
    ):  # pragma: no cover
        # Thread target of thread 'updater'. Runs in background, pulls
        # updates from Telegram and inserts them in the update queue of the
        # Dispatcher.

        self.logger.debug('Updater thread started (polling)')

        self._bootstrap(
            bootstrap_retries,
            drop_pending_updates=drop_pending_updates,
            webhook_url='',
            allowed_updates=None,
        )

        self.logger.debug('Bootstrap done')

        def polling_action_cb():
            updates = self.bot.get_updates(
                self.last_update_id,
                timeout=timeout,
                read_latency=read_latency,
                allowed_updates=allowed_updates,
            )

            if updates:
                if not self.running:
                    self.logger.debug('Updates ignored and will be pulled again on restart')
                else:
                    for update in updates:
                        self.update_queue.put(update)
                    self.last_update_id = updates[-1].update_id + 1

            return True

        def polling_onerr_cb(exc):
            # Put the error into the update queue and let the Dispatcher
            # broadcast it
            self.update_queue.put(exc)

        if ready is not None:
            ready.set()

        self._network_loop_retry(
            polling_action_cb, polling_onerr_cb, 'getting Updates', poll_interval
        )

    @no_type_check
    def _network_loop_retry(self, action_cb, onerr_cb, description, interval):
        """Perform a loop calling `action_cb`, retrying after network errors.

        Stop condition for loop: `self.running` evaluates :obj:`False` or return value of
        `action_cb` evaluates :obj:`False`.

        Args:
            action_cb (:obj:`callable`): Network oriented callback function to call.
            onerr_cb (:obj:`callable`): Callback to call when TelegramError is caught. Receives the
                exception object as a parameter.
            description (:obj:`str`): Description text to use for logs and exception raised.
            interval (:obj:`float` | :obj:`int`): Interval to sleep between each call to
                `action_cb`.

        """
        self.logger.debug('Start network loop retry %s', description)
        cur_interval = interval
        while self.running:
            try:
                if not action_cb():
                    break
            except RetryAfter as exc:
                self.logger.info('%s', exc)
                cur_interval = 0.5 + exc.retry_after
            except TimedOut as toe:
                self.logger.debug('Timed out %s: %s', description, toe)
                # If failure is due to timeout, we should retry asap.
                cur_interval = 0
            except InvalidToken as pex:
                self.logger.error('Invalid token; aborting')
                raise pex
            except TelegramError as telegram_exc:
                self.logger.error('Error while %s: %s', description, telegram_exc)
                onerr_cb(telegram_exc)
                cur_interval = self._increase_poll_interval(cur_interval)
            else:
                cur_interval = interval

            if cur_interval:
                sleep(cur_interval)

    @staticmethod
    def _increase_poll_interval(current_interval: float) -> float:
        # increase waiting times on subsequent errors up to 30secs
        if current_interval == 0:
            current_interval = 1
        elif current_interval < 30:
            current_interval *= 1.5
        else:
            current_interval = min(30.0, current_interval)
        return current_interval

    @no_type_check
    def _start_webhook(
        self,
        listen,
        port,
        url_path,
        cert,
        key,
        bootstrap_retries,
        drop_pending_updates,
        webhook_url,
        allowed_updates,
        ready=None,
        ip_address=None,
        max_connections: int = 40,
    ):
        self.logger.debug('Updater thread started (webhook)')

        # Note that we only use the SSL certificate for the WebhookServer, if the key is also
        # present. This is because the WebhookServer may not actually be in charge of performing
        # the SSL handshake, e.g. in case a reverse proxy is used
        use_ssl = cert is not None and key is not None

        if not url_path.startswith('/'):
            url_path = f'/{url_path}'

        # Create Tornado app instance
        app = WebhookAppClass(url_path, self.bot, self.update_queue)

        # Form SSL Context
        # An SSLError is raised if the private key does not match with the certificate
        if use_ssl:
            try:
                ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                ssl_ctx.load_cert_chain(cert, key)
            except ssl.SSLError as exc:
                raise TelegramError('Invalid SSL Certificate') from exc
        else:
            ssl_ctx = None

        # Create and start server
        self.httpd = WebhookServer(listen, port, app, ssl_ctx)

        if not webhook_url:
            webhook_url = self._gen_webhook_url(listen, port, url_path)

        # We pass along the cert to the webhook if present.
        with open(cert, 'rb') if cert is not None else None as cert_file:
            self._bootstrap(
                max_retries=bootstrap_retries,
                drop_pending_updates=drop_pending_updates,
                webhook_url=webhook_url,
                allowed_updates=allowed_updates,
                cert=cert_file,
                ip_address=ip_address,
                max_connections=max_connections,
            )

        self.httpd.serve_forever(ready=ready)

    @staticmethod
    def _gen_webhook_url(listen: str, port: int, url_path: str) -> str:
        return f'https://{listen}:{port}{url_path}'

    @no_type_check
    def _bootstrap(
        self,
        max_retries,
        drop_pending_updates,
        webhook_url,
        allowed_updates,
        cert=None,
        bootstrap_interval=5,
        ip_address=None,
        max_connections: int = 40,
    ):
        retries = [0]

        def bootstrap_del_webhook():
            self.logger.debug('Deleting webhook')
            if drop_pending_updates:
                self.logger.debug('Dropping pending updates from Telegram server')
            self.bot.delete_webhook(drop_pending_updates=drop_pending_updates)
            return False

        def bootstrap_set_webhook():
            self.logger.debug('Setting webhook')
            if drop_pending_updates:
                self.logger.debug('Dropping pending updates from Telegram server')
            self.bot.set_webhook(
                url=webhook_url,
                certificate=cert,
                allowed_updates=allowed_updates,
                ip_address=ip_address,
                drop_pending_updates=drop_pending_updates,
                max_connections=max_connections,
            )
            return False

        def bootstrap_onerr_cb(exc):
            if not isinstance(exc, Unauthorized) and (max_retries < 0 or retries[0] < max_retries):
                retries[0] += 1
                self.logger.warning(
                    'Failed bootstrap phase; try=%s max_retries=%s', retries[0], max_retries
                )
            else:
                self.logger.error('Failed bootstrap phase after %s retries (%s)', retries[0], exc)
                raise exc

        # Dropping pending updates from TG can be efficiently done with the drop_pending_updates
        # parameter of delete/start_webhook, even in the case of polling. Also we want to make
        # sure that no webhook is configured in case of polling, so we just always call
        # delete_webhook for polling
        if drop_pending_updates or not webhook_url:
            self._network_loop_retry(
                bootstrap_del_webhook,
                bootstrap_onerr_cb,
                'bootstrap del webhook',
                bootstrap_interval,
            )
            retries[0] = 0

        # Restore/set webhook settings, if needed. Again, we don't know ahead if a webhook is set,
        # so we set it anyhow.
        if webhook_url:
            self._network_loop_retry(
                bootstrap_set_webhook,
                bootstrap_onerr_cb,
                'bootstrap set webhook',
                bootstrap_interval,
            )

    def stop(self) -> None:
        """Stops the polling/webhook thread, the dispatcher and the job queue."""
        with self.__lock:
            if self.running or self.dispatcher.has_running_threads:
                self.logger.debug('Stopping Updater and Dispatcher...')

                self.running = False

                self._stop_httpd()
                self._stop_dispatcher()
                self._join_threads()

    @no_type_check
    def _stop_httpd(self) -> None:
        if self.httpd:
            self.logger.debug(
                'Waiting for current webhook connection to be '
                'closed... Send a Telegram message to the bot to exit '
                'immediately.'
            )
            self.httpd.shutdown()
            self.httpd = None

    @no_type_check
    def _stop_dispatcher(self) -> None:
        self.logger.debug('Requesting Dispatcher to stop...')
        self.dispatcher.stop()

    @no_type_check
    def _join_threads(self) -> None:
        for thr in self.__threads:
            self.logger.debug('Waiting for %s thread to end', thr.name)
            thr.join()
            self.logger.debug('%s thread has ended', thr.name)
        self.__threads = []

    @no_type_check
    def _signal_handler(self, signum, frame) -> None:
        self.is_idle = False
        if self.running:
            self.logger.info(
                'Received signal %s (%s), stopping...', signum, get_signal_name(signum)
            )
            self.stop()
            if self.user_signal_handler:
                self.user_signal_handler(signum, frame)
        else:
            self.logger.warning('Exiting immediately!')
            # pylint: disable=C0415,W0212
            import os

            os._exit(1)

    def idle(self, stop_signals: Union[List, Tuple] = (SIGINT, SIGTERM, SIGABRT)) -> None:
        """Blocks until one of the signals are received and stops the updater.

        Args:
            stop_signals (:obj:`list` | :obj:`tuple`): List containing signals from the signal
                module that should be subscribed to. :meth:`Updater.stop()` will be called on
                receiving one of those signals. Defaults to (``SIGINT``, ``SIGTERM``, ``SIGABRT``).

        """
        for sig in stop_signals:
            signal(sig, self._signal_handler)

        self.is_idle = True

        while self.is_idle:
            sleep(1)
