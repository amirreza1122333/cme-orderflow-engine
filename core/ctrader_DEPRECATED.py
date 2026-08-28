"""DEPRECATED - cTrader Open API client. Not used. Kept for reference only.

ARCHIVED 2026-08-21 when the project pivoted from cTrader/XAUUSD to CME
futures (MGC) over the DTC protocol against a local Sierra Chart server.

Nothing in the running engine imports this module, and it will not even
import on a clean install: `ctrader-open-api` and Twisted were removed from
requirements.txt in the same pivot. It is here purely as a worked example of
the shapes the replacement has to reproduce -

    * symbol metadata lookup           -> DTC SECURITY_DEFINITION_RESPONSE
    * spot subscription                -> DTC MARKET_DATA_REQUEST
    * depth subscription               -> DTC MARKET_DEPTH_REQUEST
    * historical trendbars             -> DTC HISTORICAL_PRICE_DATA_REQUEST
    * market order with SL/TP          -> DTC SUBMIT_NEW_SINGLE_ORDER
    * execution events                 -> DTC ORDER_UPDATE / POSITION_UPDATE

Write the real implementation in core/dtc_client.py. Do not revive this file.
"""

# --- original cTrader implementation below, unchanged ---
# """Thin, typed wrapper over the cTrader Open API (Twisted, protobuf).
# 
# The vendor SDK gives you a raw message pump; this module turns it into a small
# set of Deferred-returning calls plus a handful of event callbacks, and does the
# protobuf bookkeeping (payload extraction, error responses becoming failures)
# in one place.
# 
# Everything runs on the Twisted reactor. Nothing here blocks; anything that
# would (HTTP calls, the Claude API) is pushed to a thread by its own module.
# """
from __future__ import annotations

import logging
from typing import Callable

from ctrader_open_api import Client, Protobuf, TcpProtocol
from ctrader_open_api.messages import OpenApiCommonMessages_pb2 as common
from ctrader_open_api.messages import OpenApiMessages_pb2 as msg
from ctrader_open_api.messages import OpenApiModelMessages_pb2 as model
from twisted.internet import defer

from config import Settings
from core.symbols import SymbolRegistry, SymbolSpec

log = logging.getLogger("ctrader")

HEARTBEAT_TYPE = common.ProtoHeartbeatEvent().payloadType
ERROR_TYPE = msg.ProtoOAErrorRes().payloadType
SPOT_TYPE = msg.ProtoOASpotEvent().payloadType
DEPTH_TYPE = msg.ProtoOADepthEvent().payloadType
EXECUTION_TYPE = msg.ProtoOAExecutionEvent().payloadType
ORDER_ERROR_TYPE = msg.ProtoOAOrderErrorEvent().payloadType


class CTraderError(Exception):
    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


class CTraderClient:
    """Connection, authentication, subscriptions and order entry."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.account_id = settings.account_id
        self.registry = SymbolRegistry()

        self._client = Client(settings.host, settings.port, TcpProtocol)
        self._ready: defer.Deferred | None = None
        self._ready_fired = False
        self.connected = False

        # Event callbacks - assigned by the engine.
        self.on_spot: Callable | None = None
        self.on_depth: Callable | None = None
        self.on_execution: Callable | None = None
        self.on_order_error: Callable | None = None
        self.on_authenticated: Callable | None = None
        self.on_disconnected: Callable | None = None

    # ------------------------------------------------------------------ setup

    def start(self) -> defer.Deferred:
        """Connect and authenticate. Fires once, on the first successful auth.

        The underlying ClientService reconnects on its own; every later
        reconnect re-authenticates and calls `on_authenticated` again so the
        engine can re-subscribe.
        """
        self._ready = defer.Deferred()
        self._client.setConnectedCallback(self._handle_connected)
        self._client.setDisconnectedCallback(self._handle_disconnected)
        self._client.setMessageReceivedCallback(self._handle_message)
        self._client.startService()
        return self._ready

    def stop(self) -> None:
        try:
            self._client.stopService()
        except Exception:  # pragma: no cover - shutdown races are not fatal
            log.debug("stopService raised during shutdown", exc_info=True)

    def send(self, request, timeout: int = 20) -> defer.Deferred:
        """Send a request, resolve with the extracted response payload."""
        deferred = self._client.send(request, responseTimeoutInSeconds=timeout)
        deferred.addCallback(self._extract)
        return deferred

    @staticmethod
    def _extract(message):
        payload = Protobuf.extract(message)
        if payload.payloadType == ERROR_TYPE:
            raise CTraderError(payload.errorCode, payload.description)
        return payload

    # --------------------------------------------------------------- handlers

    def _handle_connected(self, _client) -> None:
        self.connected = True
        log.info("TCP connected to %s:%s", self.settings.host, self.settings.port)
        d = self.authenticate()
        d.addCallbacks(self._auth_ok, self._auth_failed)

    def _handle_disconnected(self, _client, reason) -> None:
        self.connected = False
        log.warning("Disconnected: %s", reason)
        if self.on_disconnected:
            self.on_disconnected(reason)

    def _auth_ok(self, _result) -> None:
        log.info("Authenticated on account %s", self.account_id)
        if self.on_authenticated:
            self.on_authenticated()
        if self._ready is not None and not self._ready_fired:
            self._ready_fired = True
            self._ready.callback(self)

    def _auth_failed(self, failure) -> None:
        log.error("Authentication failed: %s", failure.getErrorMessage())
        if self._ready is not None and not self._ready_fired:
            self._ready_fired = True
            self._ready.errback(failure)

    def _handle_message(self, _client, message) -> None:
        payload_type = message.payloadType
        if payload_type in (HEARTBEAT_TYPE, ERROR_TYPE):
            return
        try:
            if payload_type == SPOT_TYPE and self.on_spot:
                self.on_spot(Protobuf.extract(message))
            elif payload_type == DEPTH_TYPE and self.on_depth:
                self.on_depth(Protobuf.extract(message))
            elif payload_type == EXECUTION_TYPE and self.on_execution:
                self.on_execution(Protobuf.extract(message))
            elif payload_type == ORDER_ERROR_TYPE and self.on_order_error:
                self.on_order_error(Protobuf.extract(message))
        except Exception:  # a handler bug must not kill the reactor loop
            log.exception("Error handling message type %s", payload_type)

    # ------------------------------------------------------------------- auth

    def authenticate(self) -> defer.Deferred:
        app_auth = msg.ProtoOAApplicationAuthReq()
        app_auth.clientId = self.settings.client_id
        app_auth.clientSecret = self.settings.client_secret

        def account_auth(_response):
            request = msg.ProtoOAAccountAuthReq()
            request.ctidTraderAccountId = self.account_id
            request.accessToken = self.settings.access_token
            return self.send(request)

        return self.send(app_auth).addCallback(account_auth)

    # ---------------------------------------------------------------- account

    def trader(self) -> defer.Deferred:
        request = msg.ProtoOATraderReq()
        request.ctidTraderAccountId = self.account_id
        return self.send(request)

    def accounts_for_token(self) -> defer.Deferred:
        request = msg.ProtoOAGetAccountListByAccessTokenReq()
        request.accessToken = self.settings.access_token
        return self.send(request)

    def reconcile(self) -> defer.Deferred:
        """Open positions and pending orders currently on the account."""
        request = msg.ProtoOAReconcileReq()
        request.ctidTraderAccountId = self.account_id
        return self.send(request)

    # ---------------------------------------------------------------- symbols

    def load_symbols(self, wanted: list[str]) -> defer.Deferred:
        """Fill `self.registry` with full specs for `wanted` symbol names."""
        light_request = msg.ProtoOASymbolsListReq()
        light_request.ctidTraderAccountId = self.account_id
        light_request.includeArchivedSymbols = False

        wanted_upper = {name.upper() for name in wanted}
        light_by_id: dict[int, object] = {}

        def got_list(response):
            for light in response.symbol:
                if light.symbolName.upper() in wanted_upper:
                    light_by_id[light.symbolId] = light
            missing = wanted_upper - {
                light.symbolName.upper() for light in light_by_id.values()
            }
            if missing:
                log.warning(
                    "Symbols not offered by this account: %s", ", ".join(sorted(missing))
                )
            if not light_by_id:
                raise CTraderError(
                    "NO_SYMBOLS", "None of the configured symbols exist on this account"
                )
            detail_request = msg.ProtoOASymbolByIdReq()
            detail_request.ctidTraderAccountId = self.account_id
            detail_request.symbolId.extend(sorted(light_by_id))
            return self.send(detail_request)

        def got_details(response):
            for full in response.symbol:
                light = light_by_id.get(full.symbolId)
                if light is None:
                    continue
                self.registry.add(
                    SymbolSpec(
                        symbol_id=full.symbolId,
                        name=light.symbolName,
                        digits=full.digits,
                        pip_position=full.pipPosition,
                        lot_size=full.lotSize,
                        min_volume=full.minVolume,
                        max_volume=full.maxVolume,
                        step_volume=full.stepVolume,
                    )
                )
            return self.registry

        return self.send(light_request).addCallback(got_list).addCallback(got_details)

    def load_assets(self) -> defer.Deferred:
        """Attach base/quote currency names so we can sanity-check P&L maths.

        Best effort: brokers occasionally reject the asset list, and a missing
        currency check is not worth aborting the run for.
        """
        request = msg.ProtoOAAssetListReq()
        request.ctidTraderAccountId = self.account_id

        def got_assets(response):
            return {asset.assetId: asset.name for asset in response.asset}

        def failed(failure):
            log.debug("Asset list unavailable: %s", failure.getErrorMessage())
            return {}

        return self.send(request).addCallbacks(got_assets, failed)

    # ---------------------------------------------------------- subscriptions

    def subscribe_spots(self, symbol_ids: list[int]) -> defer.Deferred:
        request = msg.ProtoOASubscribeSpotsReq()
        request.ctidTraderAccountId = self.account_id
        request.symbolId.extend(symbol_ids)
        request.subscribeToSpotTimestamp = True
        return self.send(request)

    def subscribe_depth(self, symbol_ids: list[int]) -> defer.Deferred:
        request = msg.ProtoOASubscribeDepthQuotesReq()
        request.ctidTraderAccountId = self.account_id
        request.symbolId.extend(symbol_ids)
        return self.send(request)

    def subscribe_live_trendbars(self, symbol_id: int, period=None) -> defer.Deferred:
        request = msg.ProtoOASubscribeLiveTrendbarReq()
        request.ctidTraderAccountId = self.account_id
        request.symbolId = symbol_id
        request.period = period if period is not None else model.M1
        return self.send(request)

    def trendbars(
        self, symbol_id: int, from_ms: int, to_ms: int, period=None
    ) -> defer.Deferred:
        request = msg.ProtoOAGetTrendbarsReq()
        request.ctidTraderAccountId = self.account_id
        request.symbolId = symbol_id
        request.fromTimestamp = from_ms
        request.toTimestamp = to_ms
        request.period = period if period is not None else model.M1
        return self.send(request)

    # ------------------------------------------------------------------ trade

    def market_order(
        self,
        spec: SymbolSpec,
        side: str,
        volume: int,
        stop_distance: float,
        target_distance: float,
        label: str = "",
        comment: str = "",
    ) -> defer.Deferred:
        """Market order with server-side SL/TP.

        Market orders must use *relative* SL/TP: absolute prices are only valid
        on pending orders. Relative distances are in 1/100000 price units and
        are applied by the server against the actual fill, which is what we
        want - the protective levels can't drift on slippage.
        """
        request = msg.ProtoOANewOrderReq()
        request.ctidTraderAccountId = self.account_id
        request.symbolId = spec.symbol_id
        request.orderType = model.MARKET
        request.tradeSide = model.BUY if side.upper() == "BUY" else model.SELL
        request.volume = volume
        if stop_distance > 0:
            request.relativeStopLoss = spec.distance_to_relative(stop_distance)
        if target_distance > 0:
            request.relativeTakeProfit = spec.distance_to_relative(target_distance)
        if label:
            request.label = label[:100]
        if comment:
            request.comment = comment[:100]
        return self.send(request)

    def close_position(self, position_id: int, volume: int) -> defer.Deferred:
        request = msg.ProtoOAClosePositionReq()
        request.ctidTraderAccountId = self.account_id
        request.positionId = position_id
        request.volume = volume
        return self.send(request)

    def amend_sltp(
        self,
        position_id: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> defer.Deferred:
        request = msg.ProtoOAAmendPositionSLTPReq()
        request.ctidTraderAccountId = self.account_id
        request.positionId = position_id
        if stop_loss is not None:
            request.stopLoss = stop_loss
        if take_profit is not None:
            request.takeProfit = take_profit
        return self.send(request)
