import logging
import sys
import traceback
from typing import Dict, List, Set

import gevent
from eth_utils import encode_hex, is_address, is_checksum_address, is_same_address
from web3 import Web3

from monitoring_service.exceptions import ServiceNotRegistered, StateDBInvalid
from monitoring_service.state_db import StateDBSqlite
from monitoring_service.tasks import OnChannelClose, OnChannelSettle, StoreMonitorRequest
from monitoring_service.utils import BlockchainListener, BlockchainMonitor, is_service_registered
from monitoring_service.utils.blockchain_listener import (
    create_channel_event_topics,
    create_registry_event_topics,
)
from raiden_contracts.constants import (
    CONTRACT_MONITORING_SERVICE,
    CONTRACT_TOKEN_NETWORK,
    CONTRACT_TOKEN_NETWORK_REGISTRY,
    ChannelEvent,
)
from raiden_contracts.contract_manager import ContractManager
from raiden_libs.gevent_error_handler import register_error_handler
from raiden_libs.messages import BalanceProof, Message, MonitorRequest
from raiden_libs.private_contract import PrivateContract
from raiden_libs.transport import Transport
from raiden_libs.types import Address
from raiden_libs.utils import is_channel_identifier, private_key_to_address

log = logging.getLogger(__name__)


def order_participants(p1: str, p2: str):
    return (p1, p2) if p1 < p2 else (p2, p1)


def error_handler(context, exc_info):
    log.fatal("Unhandled exception terminating the program")
    traceback.print_exception(
        etype=exc_info[0],
        value=exc_info[1],
        tb=exc_info[2],
    )
    sys.exit()


class MonitoringService(gevent.Greenlet):
    def __init__(
        self,
        web3: Web3,
        contract_manager: ContractManager,
        private_key: str,
        state_db: StateDBSqlite,
        transport: Transport,
        registry_address: Address,
        monitor_contract_address: Address,
        sync_start_block: int = 0,
        required_confirmations: int = 8,
        poll_interval: int = 10,
    ):
        super().__init__()

        assert isinstance(private_key, str)
        assert isinstance(transport, Transport)
        assert is_checksum_address(private_key_to_address(private_key))

        self.web3 = web3
        self.contract_manager = contract_manager
        self.private_key = private_key
        self.transport = transport
        self.state_db = state_db
        self.stop_event = gevent.event.Event()
        self.transport.add_message_callback(lambda message: self.on_message_event(message))
        self.transport.privkey = lambda: self.private_key
        self.address = private_key_to_address(self.private_key)
        self.monitor_contract = PrivateContract(
            self.web3.eth.contract(
                abi=contract_manager.get_contract_abi(CONTRACT_MONITORING_SERVICE),
                address=monitor_contract_address,
            ),
        )
        self.registry_address = registry_address
        self.sync_start_block = sync_start_block
        self.required_confirmations = required_confirmations
        self.poll_interval = poll_interval
        self.open_channels: Set[int] = set()
        self.token_networks: Set[Address] = set()
        self.token_network_listeners: List[BlockchainListener] = []

        # some sanity checks
        chain_id = int(self.web3.version.network)
        if state_db.is_initialized() is False:
            state_db.setup_db(chain_id, monitor_contract_address, self.address)
        if state_db.chain_id() != chain_id:
            raise StateDBInvalid("Chain id doesn't match!")
        if not is_same_address(state_db.server_address(), self.address):
            raise StateDBInvalid("Monitor service address doesn't match!")
        if not is_same_address(state_db.monitoring_contract_address(), monitor_contract_address):
            raise StateDBInvalid("Monitoring contract address doesn't match!")
        self.task_list: List[gevent.Greenlet] = []
        if not is_service_registered(
            self.web3,
            contract_manager,
            monitor_contract_address,
            self.address,
        ):
            raise ServiceNotRegistered(
                "Monitoring service %s is not registered in the Monitoring smart contract (%s)" %
                (self.address, monitor_contract_address),
            )

        log.info('Starting TokenNetworkRegistry Listener (required confirmations: {})...'.format(
            self.required_confirmations,
        ))
        self.token_network_registry_listener = BlockchainListener(
            web3=self.web3,
            contract_manager=self.contract_manager,
            contract_name=CONTRACT_TOKEN_NETWORK_REGISTRY,
            contract_address=self.registry_address,
            required_confirmations=self.required_confirmations,
            poll_interval=self.poll_interval,
            sync_start_block=self.sync_start_block,
        )
        log.info(
            f'Listening to token network registry @ {registry_address} '
            f'from block {sync_start_block}',
        )
        self._setup_token_networks()

    def _setup_token_networks(self):
        self.token_network_registry_listener.add_confirmed_listener(
            topics=create_registry_event_topics(self.contract_manager),
            callback=self.handle_token_network_created,
        )

    def handle_token_network_created(self, event):
        token_network_address = event['args']['token_network_address']
        token_address = event['args']['token_address']
        event_block_number = event['blockNumber']

        assert is_checksum_address(token_network_address)
        assert is_checksum_address(token_address)

        if token_network_address not in self.token_networks:
            log.info(f'Found token network for token {token_address} @ {token_network_address}')

            log.info('Creating token network for %s', token_network_address)
            token_network_listener = BlockchainMonitor(
                web3=self.web3,
                contract_manager=self.contract_manager,
                contract_address=token_network_address,
                contract_name=CONTRACT_TOKEN_NETWORK,
                required_confirmations=self.required_confirmations,
                poll_interval=self.poll_interval,
                sync_start_block=event_block_number,
            )

            # subscribe to event notifications from blockchain monitor
            token_network_listener.add_confirmed_listener(
                topics=create_channel_event_topics(),
                callback=self.on_channel_event,
            )
            token_network_listener.start()
            self.token_networks.add(token_network_address)
            self.token_network_listeners.append(token_network_listener)

    def _run(self):
        register_error_handler(error_handler)
        self.transport.start()
        self.token_network_registry_listener.start()

        # this loop will wait until spawned greenlets complete
        while self.stop_event.is_set() is False:
            tasks = gevent.wait(self.task_list, timeout=5, count=1)
            if len(tasks) == 0:
                gevent.sleep(1)
                continue
            task = tasks[0]
            log.info('%s completed (%s)' % (task, task.value))
            self.task_list.remove(task)

    def stop(self):
        self.token_network_registry_listener.stop()
        for task in self.token_network_listeners:
            task.stop()
        self.stop_event.set()

    def on_channel_event(self, event: Dict, tx: Dict):
        event_name = event['event']

        if event_name == ChannelEvent.OPENED:
            self.on_channel_open(event, tx)
        elif event_name == ChannelEvent.CLOSED:
            self.on_channel_close(event, tx)
        elif event_name == ChannelEvent.SETTLED:
            self.on_channel_settled(event, tx)
        else:
            log.info('Unhandled event: %s', event_name)

    def on_channel_open(self, event: Dict, tx: Dict):
        log.info('on channel open: event=%s tx=%s' % (event, tx))
        channel_id = event['args']['channel_identifier']
        self.open_channels.add(channel_id)

    def on_channel_close(self, event: Dict, tx: Dict):
        log.info('on channel close: event=%s tx=%s' % (event, tx))
        # check if we have balance proof for the closing
        closing_participant = event['args']['closing_participant']
        channel_id = event['args']['channel_identifier']
        tx_data = tx[1]
        tx_balance_proof = BalanceProof(
            channel_identifier=tx_data[0],
            token_network_address=event['address'],
            balance_hash=tx_data[1],
            nonce=tx_data[2],
            additional_hash=tx_data[3],
            chain_id=int(self.web3.version.network),
            signature=encode_hex(tx_data[4]),
        )
        assert tx_balance_proof is not None
        assert is_address(closing_participant)
        assert is_channel_identifier(channel_id)
        if channel_id not in self.state_db.monitor_requests:
            return
        monitor_request = self.state_db.monitor_requests[channel_id]
        # submit monitor request
        self.start_task(
            OnChannelClose(self.monitor_contract, monitor_request, self.private_key),
        )
        self.open_channels.discard(channel_id)

    def on_channel_settled(self, event: Dict, tx: Dict):
        channel_id = event['args']['channel_identifier']
        monitor_request = self.state_db.monitor_requests.get(channel_id, None)
        if monitor_request is None:
            return
        self.start_task(
            OnChannelSettle(monitor_request, self.monitor_contract, self.private_key),
        )
        self.state_db.delete_monitor_request(event['args']['channel_identifier'])

    def on_message_event(self, message):
        """This handles messages received over the Transport"""
        print(message)
        assert isinstance(message, Message)
        if isinstance(message, MonitorRequest):
            self.on_monitor_request(message)
        else:
            log.warn('Ignoring unknown message type %s' % type(message))

    def on_monitor_request(
        self,
        monitor_request: MonitorRequest,
    ):
        """Called whenever a monitor proof message is received.
        This will spawn a greenlet and store its reference in an internal list.
        Return value of the greenlet is then checked in the main loop."""
        assert isinstance(monitor_request, MonitorRequest)
        channel_id = monitor_request.balance_proof.channel_identifier
        if channel_id not in self.open_channels:
            return
        self.start_task(
            StoreMonitorRequest(self.web3, self.state_db, monitor_request),
        )

    def start_task(self, task: gevent.Greenlet):
        task.start()
        self.task_list.append(task)

    @property
    def monitor_requests(self):
        return self.state_db.monitor_requests

    def wait_tasks(self):
        """Wait until all internal tasks are finished"""
        while True:
            if len(self.task_list) == 0:
                return
            gevent.sleep(1)
