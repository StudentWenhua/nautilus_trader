# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

# mypy: ignore-errors

import asyncio
from functools import lru_cache

from nautilus_trader.adapters.bybit.config import BybitDataClientConfig
from nautilus_trader.adapters.bybit.config import BybitExecClientConfig
from nautilus_trader.adapters.bybit.constants import BYBIT_ALL_PRODUCTS
from nautilus_trader.adapters.bybit.data import BybitDataClient
from nautilus_trader.adapters.bybit.execution import BybitExecutionClient
from nautilus_trader.adapters.bybit.http.client import BybitHttpClient
from nautilus_trader.adapters.bybit.providers import BybitInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.core import nautilus_pyo3
from nautilus_trader.core.nautilus_pyo3 import BybitProductType
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory


@lru_cache(1)
def get_cached_bybit_http_client(
    api_key: str | None = None,
    api_secret: str | None = None,
    base_url: str | None = None,
    testnet: bool = False,
) -> nautilus_pyo3.BybitHttpClient:
    """
    Cache and return a Bybit HTTP client with the given key and secret.

    If a cached client with matching parameters already exists, the cached client will be returned.

    Parameters
    ----------
    api_key : str, optional
        The API key for the client.
    api_secret : str, optional
        The API secret for the client.
    base_url : str, optional
        The base URL for the API endpoints.
    testnet : bool, default False
        If the client is connecting to the testnet API.

    Returns
    -------
    BybitHttpClient

    """
    # Determine base URL if not provided
    if base_url is None:
        environment = (
            nautilus_pyo3.BybitEnvironment.Testnet
            if testnet
            else nautilus_pyo3.BybitEnvironment.Mainnet
        )
        base_url = nautilus_pyo3.get_bybit_http_base_url(environment)

    return nautilus_pyo3.BybitHttpClient(
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url,
    )


@lru_cache(1)
def get_cached_bybit_instrument_provider(
    client: nautilus_pyo3.BybitHttpClient,
    product_types: tuple[BybitProductType, ...],
    config: InstrumentProviderConfig | None = None,
) -> BybitInstrumentProvider:
    """
    Cache and return a Bybit instrument provider.

    If a cached provider already exists, then that provider will be returned.

    Parameters
    ----------
    client : BybitHttpClient
        The Bybit HTTP client.
    product_types : tuple[BybitProductType, ...]
        The product types to load.
    config : InstrumentProviderConfig, optional
        The instrument provider configuration, by default None.

    Returns
    -------
    BybitInstrumentProvider

    """
    return BybitInstrumentProvider(
        client=client,
        product_types=product_types,
        config=config,
    )


class BybitLiveDataClientFactory(LiveDataClientFactory):
    """
    Provides a Bybit live data client factory.
    """

    @staticmethod
    def create(  # type: ignore
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: BybitDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> BybitDataClient:
        """
        Create a new Bybit data client.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The event loop for the client.
        name : str
            The custom client ID.
        config : BybitDataClientConfig
            The client configuration.
        msgbus : MessageBus
            The message bus for the client.
        cache : Cache
            The cache for the client.
        clock: LiveClock
            The clock for the instrument provider.

        Returns
        -------
        BybitDataClient

        """
        product_types = config.product_types or BYBIT_ALL_PRODUCTS
        client: nautilus_pyo3.BybitHttpClient = get_cached_bybit_http_client(
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_url=config.base_url_http,
            testnet=config.testnet,
        )
        provider = get_cached_bybit_instrument_provider(
            client=client,
            product_types=tuple(product_types),
            config=config.instrument_provider,
        )
        return BybitDataClient(
            loop=loop,
            client=client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            name=name,
        )


class BybitLiveExecClientFactory(LiveExecClientFactory):
    """
    Provides a Bybit live execution client factory.
    """

    @staticmethod
    def create(  # type: ignore
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: BybitExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> BybitExecutionClient:
        """
        Create a new Bybit execution client.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The event loop for the client.
        name : str
            The custom client ID.
        config : BybitExecClientConfig
            The client configuration.
        msgbus : MessageBus
            The message bus for the client.
        cache : Cache
            The cache for the client.
        clock : LiveClock
            The clock for the client.

        Returns
        -------
        BybitExecutionClient

        """
        product_types = config.product_types or BYBIT_ALL_PRODUCTS
        # Use old Python HTTP client for execution (not yet converted to Rust)
        client: BybitHttpClient = BybitHttpClient(
            clock=clock,
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_url=config.base_url_http,
        )
        provider = BybitInstrumentProvider(
            client=client,
            clock=clock,
            product_types=product_types,
            config=config.instrument_provider,
        )
        return BybitExecutionClient(
            loop=loop,
            client=client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            product_types=product_types,
            base_url_ws_private=config.base_url_ws_private,
            base_url_ws_trade=config.base_url_ws_trade,
            config=config,
            name=name,
        )


# Backward compatibility wrappers
def get_bybit_http_client(
    clock: LiveClock,
    key: str | None = None,
    secret: str | None = None,
    base_url: str | None = None,
    is_demo: bool = False,
    is_testnet: bool = False,
    recv_window_ms: int = 5_000,
) -> nautilus_pyo3.BybitHttpClient:
    """
    Backward compatibility wrapper for get_cached_bybit_http_client.

    Note: clock and recv_window_ms parameters are ignored in the new Rust client.

    """
    return get_cached_bybit_http_client(
        api_key=key,
        api_secret=secret,
        base_url=base_url,
        testnet=is_testnet,
    )


def get_bybit_instrument_provider(
    client: nautilus_pyo3.BybitHttpClient,
    clock: LiveClock,
    product_types: (
        tuple[BybitProductType, ...] | list[BybitProductType] | frozenset[BybitProductType]
    ),
    config: InstrumentProviderConfig,
) -> BybitInstrumentProvider:
    """
    Backward compatibility wrapper for get_cached_bybit_instrument_provider.

    Note: clock parameter is ignored in the new provider.

    """
    product_types_tuple: tuple[BybitProductType, ...]
    if isinstance(product_types, tuple):
        product_types_tuple = product_types
    elif isinstance(product_types, list):
        product_types_tuple = tuple(product_types)
    else:  # frozenset
        product_types_tuple = tuple(product_types)

    return get_cached_bybit_instrument_provider(
        client=client,
        product_types=product_types_tuple,
        config=config,
    )
